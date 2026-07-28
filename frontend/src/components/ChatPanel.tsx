/* ============================================================
   DARAVE — AI Chat Panel (Unified Agent)
   One smart agent that decides: respond with text OR execute tools.
   No toggle — the AI figures it out from the prompt.

   PERFORMANCE: Streaming text accumulates in a ref and commits
   to the Zustand store only ONCE when streaming finishes. This
   prevents 80ms×N store updates from re-rendering the entire
   MixDeck component tree (turntables, waveforms, etc.).
   ============================================================ */

import { useState, useRef, useEffect, useCallback, memo } from 'react'
import { Send, Square, Trash2, BotMessageSquare, Wrench, Mic, MicOff } from 'lucide-react'
import { useShallow } from 'zustand/react/shallow'
import { useAppStore } from '@/stores/appStore'
import { chatApi } from '@/lib/api'
import { extractToolCalls, stripToolCallJson } from '@/lib/toolCallParser'
import {
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

const MessageBubble = memo(function MessageBubble({ msg }: { msg: ChatMessage }) {
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
})

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
      <Mic size={12} className="chat-chat-voice-pulse" />
      <span className="chat-voice-text">{transcript || 'Слушаю...'}</span>
    </div>
  )
}

// --- Store helpers (batched, not per-chunk) ---

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

// Stream LLM response — returns full text WITHOUT touching the store.
// The caller is responsible for committing the result.
async function streamLLMResponse(
  messages: ChatMessage[],
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
  const [voiceActive, setVoiceActive] = useState(false)
  const [voiceTranscript, setVoiceTranscript] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const agentLoopRef = useRef(false)
  const recognitionRef = useRef<any>(null)

  // Streaming preview — stored in ref, NOT in Zustand store
  const streamingTextRef = useRef('')

  // Debounced scroll — max once per 300ms
  const scrollTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const scrollToBottom = useCallback(() => {
    if (scrollTimerRef.current) return
    scrollTimerRef.current = setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
      scrollTimerRef.current = undefined
    }, 300)
  }, [])

  useEffect(() => {
    scrollToBottom()
  }, [chatMessages, chatStreaming, scrollToBottom])

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

    const MAX_ROUNDS = 3
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
          'You are DARAVE — a DJ assistant that controls a 2-deck DJ interface.',
          '',
          '## TOOLS',
          'You have these tools. Output ONLY the JSON, nothing else:',
          '',
          'list_library → Lists all tracks. No parameters needed.',
          '  Example: {"name": "list_library", "parameters": {}}',
          '',
          'load_track_to_deck → Loads a track. Parameters: deck ("A" or "B"), track_name (exact name from library).',
          '  Example: {"name": "load_track_to_deck", "parameters": {"deck": "A", "track_name": "Artist - Title"}}',
          '',
          'get_deck_info → Shows what tracks are loaded and their BPM/key.',
          '  Example: {"name": "get_deck_info", "parameters": {}}',
          '',
          'check_compatibility → Checks if two loaded tracks mix well (BPM, key).',
          '  Example: {"name": "check_compatibility", "parameters": {}}',
          '',
          'play_deck → Starts playback.',
          '  Example: {"name": "play_deck", "parameters": {}}',
          '',
          'pause_deck → Pauses playback.',
          '  Example: {"name": "pause_deck", "parameters": {}}',
          '',
          'set_crossfader → Crossfader position. 0.0 = Deck A, 1.0 = Deck B, 0.5 = center.',
          '  Example: {"name": "set_crossfader", "parameters": {"position": 0.5}}',
          '',
          'set_volume → Volume for a deck. 0.0 = silent, 1.0 = max.',
          '  Example: {"name": "set_volume", "parameters": {"deck": "A", "volume": 0.8}}',
          '',
          'set_effect → Apply effect. Effects: "echo", "reverb", "filter", "delay", "none".',
          '  Example: {"name": "set_effect", "parameters": {"deck": "B", "effect": "filter"}}',
          '',
          'start_remix → Server-side mix of two loaded tracks.',
          '  Example: {"name": "start_remix", "parameters": {}}',
          '',
          '## WORKFLOW',
          'Follow this order when user asks to mix/DJ:',
          '1. list_library → see available tracks',
          '2. load_track_to_deck → load track into Deck A',
          '3. load_track_to_deck → load track into Deck B',
          '4. check_compatibility → see if tracks match',
          '5. start_remix → create the mix',
          '',
          '## RULES',
          '- Output ONLY JSON tool calls. No explanations before or after.',
          '- One tool call per JSON object.',
          '- NEVER call start_remix before both decks have tracks loaded.',
          '- NEVER call list_library more than once.',
          '- For track names use EXACT names from library, including spaces and special chars.',
          '- If user says "загрузи трек" → call list_library first to show options.',
          '- For Russian commands: загрузи=load_track_to_deck, играй=play_deck, стоп=stop_deck, своди=start_remix.',
          '- Crossfader: 0.0=full A, 0.5=center, 1.0=full B.',
          '- Volume: 0.0=silent, 0.5=50%, 1.0=max.',
        ].join('\n')

        apiMessages.unshift({ role: 'system', content: systemPrompt })

        // Stream WITHOUT touching the store — accumulate in ref
        streamingTextRef.current = ''

        const responseText = await streamLLMResponse(
          apiMessages,
          abortRef.current?.signal,
        )

        // Clear streaming preview
        streamingTextRef.current = ''

        const toolCalls = extractToolCalls(responseText)

        if (toolCalls.length === 0) {
          // No tool calls — AI chose to respond with text. Commit to store once.
          addAssistantMessage(responseText)
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

          // Truncate long tool results to avoid overwhelming the model context
          const MAX_TOOL_RESULT = 500
          const toolResult = result.result.length > MAX_TOOL_RESULT
            ? result.result.slice(0, MAX_TOOL_RESULT) + '... [truncated]'
            : result.result

          addToolMessage(
            result.success ? toolResult : `Error: ${toolResult}`,
            result.id,
          )

          if (!result.success) break
        }

        // Yield to main thread between rounds
        await new Promise((r) => setTimeout(r, 50))
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        setChatError(err?.message || 'Agent loop failed')
      }
    } finally {
      streamingTextRef.current = ''
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
  }, [chatStreaming, agentCallbacks, agentLoop, setChatStreaming, setChatError])

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
