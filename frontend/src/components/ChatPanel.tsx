/* ============================================================
   DARAVE — AI Chat Panel (Agent Mode)
   Streams responses from Ollama via SSE, supports tool calls.
   Agent mode: AI can control decks, load tracks, play, mix.
   Multi-deck: parallel agents control A/B/C/D simultaneously.
   Agent loop: call tool → result → LLM → next action → repeat.
   ============================================================ */

import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Square, Trash2, BotMessageSquare, Wrench, Zap, ZapOff, Mic, MicOff } from 'lucide-react'
import { useShallow } from 'zustand/react/shallow'
import { useAppStore } from '@/stores/appStore'
import { chatApi } from '@/lib/api'
import {
  FRONTEND_TOOLS,
  executeFrontendTool,
  type FrontendToolCall,
  type AgentCallbacks,
} from '@/lib/agentTools'
import type { ChatMessage, ChatToolCall } from '@/types'
import './ChatPanel.css'

const QUICK_PROMPTS = [
  'Загрузи первый трек из библиотеки в Deck A и включи',
  'Загрузи два трека и начни миксовать',
  'Список треков в библиотеке',
  'Проверь совместимость загруженных треков',
  'Поставь кроссфейдер в центр',
  'Стоп воспроизведение',
]

const AGENT_PROMPTS = [
  'Загрузи два трека из библиотеки и сведи их',
  'Загрузи первый трек в Deck A и включи',
  'Загрузи трек в Deck B и примени echo эффект',
  'Поставь кроссфейдер в центр',
  'Воспроизведение стоп',
  'Что есть в библиотеке?',
]

// --- Agent loop helpers ---

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

// --- Agent loop helpers ---

function extractToolCalls(text: string): FrontendToolCall[] {
  const calls: FrontendToolCall[] = []

  // Find all {"tool_call": patterns and extract balanced JSON
  const marker = '"tool_call"'
  let searchFrom = 0

  while (true) {
    const idx = text.indexOf(marker, searchFrom)
    if (idx === -1) break

    // Walk backwards to find the opening { of the outer object
    let start = idx - 1
    while (start >= 0 && text[start] !== '{') start--
    if (start < 0) { searchFrom = idx + marker.length; continue }

    // Walk forward with bracket counting to find the matching closing }
    let depth = 0
    let end = start
    let inString = false
    let escape = false

    for (let i = start; i < text.length; i++) {
      const ch = text[i]

      if (escape) {
        escape = false
        continue
      }

      if (ch === '\\' && inString) {
        escape = true
        continue
      }

      if (ch === '"') {
        inString = !inString
        continue
      }

      if (inString) continue

      if (ch === '{') depth++
      else if (ch === '}') {
        depth--
        if (depth === 0) {
          end = i
          break
        }
      }
    }

    const jsonStr = text.slice(start, end + 1)

    try {
      const parsed = JSON.parse(jsonStr)
      // It's {"tool_call": {...}} format — extract the inner tool_call object
      const tc = parsed.tool_call
      if (tc && tc.name && typeof tc.name === 'string') {
        calls.push({
          id: tc.id || `call_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          name: tc.name,
          arguments: tc.arguments || {},
        })
      }
    } catch { /* not valid JSON, skip */ }

    searchFrom = end + 1
  }

  return calls
}

function addAssistantMessage(content: string, toolCalls?: ChatToolCall[]) {
  useAppStore.setState((s) => {
    const msgs = [...s.chatMessages]
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'assistant' && !msgs[i].content && (!msgs[i].toolCalls || msgs[i].toolCalls!.length === 0)) {
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

// --- Agent loop helpers ---

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
          onChunk(parsed.data.content)
          fullText += parsed.data.content
        }
      } catch { /* skip */ }
    }
  }

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
  const [agentMode, setAgentMode] = useState(true)
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

  // Agent loop: execute tool calls and continue
  const agentLoop = useCallback(async (_userText: string) => {
    if (agentLoopRef.current) return
    agentLoopRef.current = true

    const MAX_ROUNDS = 10
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
          'You are DARAVE AI DJ Agent. You control the DJ interface directly.',
          'Available tools:',
          ...FRONTEND_TOOLS.map((t) => `  ${t.name}: ${t.description} | args: ${JSON.stringify(Object.keys(t.parameters))}`),
          '',
          'RULES:',
          '1. When user asks to do something, IMMEDIATELY call the appropriate tool.',
          '2. Do NOT explain what you will do — just DO it by outputting the tool call.',
          '3. Output EXACTLY one JSON tool call per response:',
          '   {"tool_call": {"id": "call_N", "name": "TOOL_NAME", "arguments": {}}}',
          '4. After seeing tool result, continue with next action if needed.',
          '5. Be concise. No explanations unless asked.',
          '6. You can control Deck A and Deck B simultaneously.',
          '7. Always load a track first if the deck is empty.',
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
          // Check if text contains "tool_call" but parsing failed
          if (responseText.includes('tool_call')) {
            console.warn('[Agent] tool_call found in text but extractToolCalls returned empty. Raw text:', responseText.slice(0, 500))
          }
          break
        }

        console.log('[Agent] Extracted tool calls:', toolCalls)

        for (const tc of toolCalls) {
          if (!agentCallbacks) {
            console.warn('[Agent] No agentCallbacks — cannot execute tool:', tc.name)
            addToolMessage(`Error: No agent callbacks available`, tc.id)
            break
          }

          addAssistantMessage(responseText, [{
            id: tc.id,
            name: tc.name,
            arguments: tc.arguments,
          }])

          console.log('[Agent] Executing tool:', tc.name, tc.arguments)
          const result = await executeFrontendTool(tc, agentCallbacks)
          console.log('[Agent] Tool result:', result.name, result.success, result.result?.slice(0, 100))

          addToolMessage(
            result.success ? result.result : `Error: ${result.result}`,
            result.id,
          )

          if (!result.success) break
        }
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

    if (agentMode && agentCallbacks) {
      await agentLoop(trimmed)
    } else {
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
  }, [chatMessages, chatStreaming, agentMode, agentCallbacks, agentLoop, setChatStreaming, setChatError])

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
          className={`chat-agent-toggle ${agentMode ? 'chat-agent-toggle--active' : ''}`}
          onClick={() => setAgentMode(!agentMode)}
          title={agentMode ? 'Agent mode ON — AI controls decks' : 'Agent mode OFF — AI only suggests'}
        >
          {agentMode ? <Zap size={10} /> : <ZapOff size={10} />}
          {agentMode ? 'Agent' : 'Chat'}
        </button>
        <button
          className={`chat-voice-toggle ${voiceActive ? 'chat-voice-toggle--active' : ''}`}
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
            <div className="chat-splash__title">
              {agentMode ? 'DJ Agent' : 'DJ Assistant'}
            </div>
            <div className="chat-splash__hint">
              {agentMode
                ? 'Я управлю деками — загружаю треки, включаю, микшую. Просто скажи!'
                : 'Ask about mixing, transitions, track compatibility.'}
            </div>
            <div className="chat-splash__prompts">
              {(agentMode ? AGENT_PROMPTS : QUICK_PROMPTS).map((text, i) => (
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
          placeholder={agentMode ? 'Скажи агенту что делать...' : 'Ask the DJ assistant...'}
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
