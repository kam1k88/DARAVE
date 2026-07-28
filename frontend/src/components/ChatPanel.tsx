/* ============================================================
   DARAVE — AI Chat Panel (Unified Agent)
   One smart agent that decides: respond with text OR execute tools.
   No toggle — the AI figures it out from the prompt.
   ============================================================ */

import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Square, Trash2, BotMessageSquare, Wrench, Mic, MicOff } from 'lucide-react'
import { useShallow } from 'zustand/react/shallow'
import { useAppStore } from '@/stores/appStore'
import { chatApi } from '@/lib/api'
import { extractToolCalls, stripToolCallJson } from '@/lib/toolCallParser'
import {
  FRONTEND_TOOLS,
  executeFrontendTool,
  type AgentCallbacks,
} from '@/lib/agentTools'
import type { ChatMessage, ChatToolCall } from '@/types'
import './ChatPanel.css'

const QUICK_PROMPTS = [
  'Загрузи два трека и сведи их',
  'Загрузи трек в Deck A и включи',
  'Что такое кроссфейдер?',
  'Проверь совместимость треков',
  'Поставь echo на Deck B',
  'Список треков в библиотеке',
]

// --- Sub-components ---

function ToolCallBlock({ tc }: { tc: ChatToolCall }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="chat-tool-call">
      <button
        className={`chat-tool-badge ${open ? 'chat-tool-badge--open' : ''}`}
        onClick={() => setOpen(!open)}
      >
        <Wrench size={10} />
        {tc.name}
      </button>
      {open && (
        <div className="chat-tool-detail">
          {JSON.stringify(tc.arguments, null, 2)}
        </div>
      )}
    </div>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const [showToolDetail, setShowToolDetail] = useState(false)

  return (
    <div className={`chat-msg chat-msg--${msg.role}`}>
      {msg.role === 'assistant' && msg.toolCalls && msg.toolCalls.length > 0 && (
        <div className="chat-msg__tool-calls">
          {msg.toolCalls.map((tc) => (
            <ToolCallBlock key={tc.id} tc={tc} />
          ))}
        </div>
      )}
      {msg.content && (
        <div className="chat-msg__bubble">
          {msg.content}
        </div>
      )}
      {msg.role === 'tool' && (
        <div className="chat-tool-result">
          <Wrench size={10} />
          <span className="chat-tool-result__name">{msg.content.split(']')[0]?.replace('[', '')}</span>
          <button
            className="chat-tool-badge"
            onClick={() => setShowToolDetail(!showToolDetail)}
          >
            {showToolDetail ? 'Hide' : 'Show'}
          </button>
          {showToolDetail && (
            <div className="chat-tool-detail">
              {msg.content}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StreamingDots() {
  return (
    <div className="chat-streaming">
      <span className="chat-streaming__dot" />
      <span className="chat-streaming__dot" />
      <span className="chat-streaming__dot" />
    </div>
  )
}

function VoiceIndicator({ active, transcript }: { active: boolean; transcript: string }) {
  if (!active) return null
  return (
    <div className="chat-voice-indicator">
      <Mic size={12} className="chat-voice-pulse" />
      <span className="chat-voice-text">{transcript || 'Слушаю...'}</span>
    </div>
  )
}

// --- Store helpers ---

function addAssistantMessage(content: string, toolCalls?: ChatToolCall[]) {
  useAppStore.setState((s) => {
    const msgs = [...s.chatMessages]
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant' && (!msgs[i].toolCalls || msgs[i].toolCalls!.length === 0)) {
        msgs[i] = { ...msgs[i], content, toolCalls }
        return { chatMessages: msgs }
      }
    }
    return {
      chatMessages: [...msgs, {
        role: 'assistant',
        content,
        toolCalls,
        timestamp: new Date().toISOString(),
      }],
    }
  })
}

function addToolMessage(content: string, toolCallId?: string) {
  useAppStore.setState((s) => ({
    chatMessages: [...s.chatMessages, {
      role: 'tool',
      content,
      toolCallId,
      timestamp: new Date().toISOString(),
    }],
  }))
}

function addUserMessage(content: string) {
  useAppStore.setState((s) => ({
    chatMessages: [...s.chatMessages, {
      role: 'user',
      content,
      timestamp: new Date().toISOString(),
    }],
  }))
}

function getMessages(): ChatMessage[] {
  return useAppStore.getState().chatMessages

}

async function streamLLMResponse(
  messages: ChatMessage[],
  onChunk: (text: string) => void,
  abortSignal?: AbortSignal,
): Promise<string> {
  let fullText = ''

  const body = await chatApi.stream(messages)
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let chunkBuffer = ''
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  const flushChunks = () => {
    if (chunkBuffer) {
      onChunk(chunkBuffer)
      chunkBuffer = ''
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    if (abortSignal?.aborted) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6)
      if (!data || data === ': ping') continue

      try {
        const parsed = JSON.parse(data)
        if (parsed.type === 'chunk' && parsed.data?.content) {
          chunkBuffer += parsed.data.content
          fullText += parsed.data.content
          // Throttle: flush at most every 80ms
          if (!flushTimer) {
            flushTimer = setTimeout(() => {
              flushChunks()
              flushTimer = null
            }, 80)
          }
        }
      } catch { /* skip */ }
    }
  }

  // Final flush
  if (flushTimer) clearTimeout(flushTimer)
  flushChunks()

  return fullText
}

// --- Main component ---

interface ChatPanelProps {
  agentCallbacks?: AgentCallbacks
}

export function ChatPanel({ agentCallbacks }: ChatPanelProps) {
  const {
    chatMessages, chatStreaming, chatError,
    setChatStreaming, setChatError, clearChatHistory,
  } = useAppStore(
    useShallow((s) => ({
      chatMessages: s.chatMessages,
      chatStreaming: s.chatStreaming,
      chatError: s.chatError,
      setChatStreaming: s.setChatStreaming,
      setChatError: s.setChatError,
      clearChatHistory: s.clearChatHistory,
    })),
  )

  const [input, setInput] = useState('')
  const [voiceActive, setVoiceActive] = useState(false)
  const [voiceTranscript, setVoiceTranscript] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const agentLoopRef = useRef(false)
  const recognitionRef = useRef<any>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages, chatStreaming])

  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = `${Math.min(ta.scrollHeight, 80)}px`
    }
  }, [input])

  // Voice recognition
  const toggleVoice = useCallback(() => {
    if (voiceActive) {
      recognitionRef.current?.stop()
      recognitionRef.current = null
      setVoiceActive(false)
      setVoiceTranscript('')
      return
    }

    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognition) {
      setChatError('Speech recognition not supported in this browser')
      return
    }

    const recognition = new SpeechRecognition()
    recognition.continuous = true
    recognition.interimResults = true
    recognition.lang = 'ru-RU'

    recognition.onresult = (event: any) => {
      let transcript = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        transcript += event.results[i][0].transcript
      }
      setVoiceTranscript(transcript)

      if (event.results[event.resultIndex].isFinal) {
        const text = transcript.trim()
        if (text) {
          sendMessage(text)
          setVoiceTranscript('')
        }
      }
    }

    recognition.onerror = () => setVoiceActive(false)
    recognition.onend = () => { if (voiceActive) recognition.start() }

    recognition.start()
    recognitionRef.current = recognition
    setVoiceActive(true)
  }, [voiceActive])

  // Unified agent loop — AI decides: tool call OR text response
  const agentLoop = useCallback(async (_userText: string) => {
    if (agentLoopRef.current) return
    agentLoopRef.current = true

    const MAX_ROUNDS = 5
    let round = 0

    try {
      while (round < MAX_ROUNDS) {
        round++

        const allMessages = getMessages()
        const apiMessages: ChatMessage[] = allMessages.map((m) => ({
          role: m.role as ChatMessage['role'],
          content: m.content,
          toolCallId: m.toolCallId,
        }))

        const systemPrompt = [
          'You are DARAVE — a smart DJ assistant and controller.',
          '',
          'You have TWO abilities:',
          '1. ANSWER questions — explain mixing, effects, techniques, music theory.',
          '2. CONTROL the DJ interface — load tracks, play, mix, apply effects.',
          '',
          'Available tools (use when user wants you to DO something):',
          ...FRONTEND_TOOLS.map((t) => `  ${t.name}: ${t.description} | args: ${JSON.stringify(Object.keys(t.parameters))}`),
          '',
          'DECISION RULES:',
          '- If user asks "what is...", "explain...", "how does..." → ANSWER with text.',
          '- If user asks to DO something (load, play, mix, set, apply) → use a tool.',
          '- If user gives a command like "загрузи", "включи", "поставь" → use a tool.',
          '- You can BOTH answer AND use tools in the same response if needed.',
          '',
          'TOOL FORMAT — output ONE JSON object per action:',
          '  {"name": "TOOL_NAME", "parameters": {}}',
          '',
          'IMPORTANT: Each tool call MUST be a separate JSON object on its own line.',
          'Do NOT wrap in {"tool_call": ...}. Just output the plain JSON.',
          '',
          'You control Deck A and Deck B. If a deck is empty, load a track first.',
          'Be concise in answers. Be fast in actions.',
        ].join('\n')

        apiMessages.unshift({ role: 'system', content: systemPrompt })

        addAssistantMessage('')

        const { appendToLastAssistantMessage } = useAppStore.getState()
        const responseText = await streamLLMResponse(
          apiMessages,
          (chunk) => appendToLastAssistantMessage(chunk),
          abortRef.current?.signal,
        )

        const toolCalls = extractToolCalls(responseText)

        if (toolCalls.length === 0) {
          // No tool calls — AI chose to respond with text. Done.
          break
        }

        // Execute each tool call
        for (const tc of toolCalls) {
          if (!agentCallbacks) {
            addToolMessage(`Error: No agent callbacks available`, tc.id)
            break
          }

          const cleanText = stripToolCallJson(responseText)
          addAssistantMessage(cleanText || `${tc.name}(...)`, [{
            id: tc.id,
            name: tc.name,
            arguments: tc.arguments,
          }])

          const result = await executeFrontendTool(tc, agentCallbacks)

          addToolMessage(
            result.success ? result.result : `Error: ${result.result}`,
            result.id,
          )

          if (!result.success) break
        }

        // Yield to main thread between rounds to prevent page freeze
        await new Promise((r) => setTimeout(r, 50))
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        setChatError(err?.message || 'Agent loop failed')
      }
    } finally {
      agentLoopRef.current = false
      setChatStreaming(false)
      abortRef.current = null
    }
  }, [agentCallbacks, setChatStreaming, setChatError])

  const sendMessage = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || chatStreaming) return

    addUserMessage(trimmed)
    setInput('')
    setChatError(null)
    setChatStreaming(true)

    // Always use the unified agent loop
    if (agentCallbacks) {
      await agentLoop(trimmed)
    } else {
      // Fallback: no callbacks, just chat
      try {
        addAssistantMessage('')
        const allMessages = getMessages()
        const apiMessages: ChatMessage[] = allMessages.map((m) => ({
          role: m.role as ChatMessage['role'],
          content: m.content,
          toolCallId: m.toolCallId,
        }))

        await streamLLMResponse(
          apiMessages,
          (chunk) => {
            const store = useAppStore.getState()
            store.appendToLastAssistantMessage(chunk)
          },
          abortRef.current?.signal,
        )
      } catch (err: any) {
        if (err?.name !== 'AbortError') {
          setChatError(err?.message || 'Failed to send message')
        }
      } finally {
        setChatStreaming(false)
      }
    }
  }, [chatMessages, chatStreaming, agentCallbacks, agentLoop, setChatStreaming, setChatError])

  const handleSend = () => sendMessage(input)
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }
  const handleStop = () => { abortRef.current?.abort(); setChatStreaming(false) }
  const handleClear = async () => {
    try { await chatApi.clearHistory() } catch { /* ignore */ }
    clearChatHistory()
  }
  const handleQuickPrompt = (text: string) => { setInput(text); sendMessage(text) }

  return (
    <div className="chat-panel">
      <div className="chat-toolbar">
        <button
          className="chat-toolbar__btn chat-voice-toggle"
          onClick={toggleVoice}
          title={voiceActive ? 'Stop voice input' : 'Start voice input'}
        >
          {voiceActive ? <MicOff size={10} /> : <Mic size={10} />}
          {voiceActive ? 'Stop' : 'Voice'}
        </button>
        <button className="chat-toolbar__btn" onClick={handleClear} title="Clear chat history">
          <Trash2 size={10} style={{ marginRight: 3, verticalAlign: '-1px' }} />
          Clear
        </button>
      </div>

      <VoiceIndicator active={voiceActive} transcript={voiceTranscript} />

      <div className="chat-messages scroll-y">
        {chatMessages.length === 0 && !chatStreaming && (
          <div className="chat-splash">
            <div className="chat-splash__icon">
              <BotMessageSquare size={20} />
            </div>
            <div className="chat-splash__title">DARAVE AI</div>
            <div className="chat-splash__hint">
              Управляй деками голосом или текстом. Спроси что угодно о микшировании.
            </div>
            <div className="chat-splash__prompts">
              {QUICK_PROMPTS.map((text, i) => (
                <button key={i} className="chat-prompt-btn" onClick={() => handleQuickPrompt(text)} disabled={chatStreaming}>
                  {text.length > 45 ? text.slice(0, 45) + '...' : text}
                </button>
              ))}
            </div>
          </div>
        )}

        {chatMessages.map((msg, i) => (
          <MessageBubble key={`${msg.role}-${i}-${msg.timestamp || ''}`} msg={msg} />
        ))}

        {chatStreaming && <StreamingDots />}
        {chatError && <div className="chat-error">{chatError}</div>}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input">
        <textarea
          ref={textareaRef}
          className="chat-input__textarea"
          placeholder="Спроси или скажи что сделать..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={chatStreaming}
        />
        {chatStreaming ? (
          <button className="chat-input__stop" onClick={handleStop} title="Stop">
            <Square size={12} />
          </button>
        ) : (
          <button className="chat-input__send" onClick={handleSend} disabled={!input.trim()} title="Send">
            <Send size={12} />
          </button>
        )}
      </div>
    </div>
  )
}
