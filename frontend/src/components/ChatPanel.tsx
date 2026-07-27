/* ============================================================
   AI Chat Panel — DJ Assistant in the Right Inspector
   Streams responses from Ollama via SSE, supports tool calls.
   ============================================================ */

import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Square, Trash2, BotMessageSquare, Wrench } from 'lucide-react'
import { useShallow } from 'zustand/react/shallow'
import { useAppStore } from '@/stores/appStore'
import { chatApi } from '@/lib/api'
import type { ChatMessage, ChatToolCall } from '@/types'
import './ChatPanel.css'

const QUICK_PROMPTS = [
  { label: 'List my library', text: 'List all tracks in my library' },
  { label: 'Recommend a transition', text: 'Recommend a transition for my two most recent tracks' },
  { label: 'What effects are available?', text: 'What DJ effects are available?' },
  { label: 'Optimize my set', text: 'Optimize a setlist from my library' },
]

// --- Sub-components ---

function ToolCallBlock({ tc }: { tc: ChatToolCall }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="chat-tool-call">
      <button
        className={`chat-tool-badge ${open ? 'chat-tool-badge--open' : ''}`}
        onClick={() => setOpen(!open)}
        title={open ? 'Hide details' : 'Show details'}
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
        <button
          className="chat-tool-badge"
          onClick={() => setShowToolDetail(!showToolDetail)}
        >
          <Wrench size={10} />
          tool result
        </button>
      )}
      {msg.role === 'tool' && showToolDetail && (
        <div className="chat-tool-detail">
          {msg.content}
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

// --- Main component ---

export function ChatPanel() {
  const {
    chatMessages, chatStreaming, chatError,
    addChatMessage, appendToLastAssistantMessage,
    setChatStreaming, setChatError, clearChatHistory,
  } = useAppStore(
    useShallow((s) => ({
      chatMessages: s.chatMessages,
      chatStreaming: s.chatStreaming,
      chatError: s.chatError,
      addChatMessage: s.addChatMessage,
      appendToLastAssistantMessage: s.appendToLastAssistantMessage,
      setChatStreaming: s.setChatStreaming,
      setChatError: s.setChatError,
      clearChatHistory: s.clearChatHistory,
    })),
  )

  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages, chatStreaming])

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = `${Math.min(ta.scrollHeight, 80)}px`
    }
  }, [input])

  const sendMessage = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || chatStreaming) return

    // Add user message
    const userMsg: ChatMessage = { role: 'user', content: trimmed, timestamp: new Date().toISOString() }
    addChatMessage(userMsg)
    setInput('')
    setChatError(null)
    setChatStreaming(true)

    // Add placeholder for assistant response
    const assistantMsg: ChatMessage = { role: 'assistant', content: '', timestamp: new Date().toISOString() }
    addChatMessage(assistantMsg)

    try {
      // Build messages for the API (include conversation history)
      const apiMessages = [...chatMessages, userMsg].map((m) => ({
        role: m.role,
        content: m.content,
        tool_call_id: m.toolCallId,
      }))

      const body = await chatApi.stream(apiMessages)
      const reader = body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      abortRef.current = new AbortController()

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6)
          if (!data || data === ': ping') continue

          try {
            const parsed = JSON.parse(data)
            const type = parsed.type
            const d = parsed.data

            if (type === 'chunk' && d.content) {
              appendToLastAssistantMessage(d.content)
            } else if (type === 'tool_call') {
              // Add tool call to the assistant message
              const store = useAppStore.getState()
              const msgs = [...store.chatMessages]
              for (let i = msgs.length - 1; i >= 0; i--) {
                if (msgs[i].role === 'assistant') {
                  const existing = msgs[i].toolCalls || []
                  msgs[i] = {
                    ...msgs[i],
                    toolCalls: [...existing, { id: d.id, name: d.name, arguments: d.arguments }],
                  }
                  break
                }
              }
              useAppStore.setState({ chatMessages: msgs })
            } else if (type === 'tool_result') {
              // Store tool result
              const toolMsg: ChatMessage = {
                role: 'tool',
                content: `[${d.name}] ${d.result}`,
                toolCallId: d.id,
                timestamp: new Date().toISOString(),
              }
              useAppStore.setState((s) => ({
                chatMessages: [...s.chatMessages, toolMsg],
              }))
            } else if (type === 'error') {
              setChatError(d.message || 'Unknown error')
            } else if (type === 'done') {
              // Stream complete
            }
          } catch {
            // Skip unparseable lines
          }
        }
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        setChatError(err?.message || 'Failed to send message')
      }
    } finally {
      setChatStreaming(false)
      abortRef.current = null
    }
  }, [chatMessages, chatStreaming, addChatMessage, appendToLastAssistantMessage, setChatStreaming, setChatError])

  const handleSend = () => sendMessage(input)

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleStop = () => {
    abortRef.current?.abort()
    setChatStreaming(false)
  }

  const handleClear = async () => {
    try { await chatApi.clearHistory() } catch { /* ignore */ }
    clearChatHistory()
  }

  const handleQuickPrompt = (text: string) => {
    setInput(text)
    sendMessage(text)
  }

  return (
    <div className="chat-panel">
      {/* Toolbar */}
      <div className="chat-toolbar">
        <button
          className="chat-toolbar__btn"
          onClick={handleClear}
          title="Clear chat history"
        >
          <Trash2 size={10} style={{ marginRight: 3, verticalAlign: '-1px' }} />
          Clear
        </button>
      </div>

      {/* Messages */}
      <div className="chat-messages scroll-y">
        {chatMessages.length === 0 && !chatStreaming && (
          <div className="chat-splash">
            <div className="chat-splash__icon">
              <BotMessageSquare size={20} />
            </div>
            <div className="chat-splash__title">DJ Assistant</div>
            <div className="chat-splash__hint">
              Ask about mixing, transitions, track compatibility, or let me optimize your set.
            </div>
            <div className="chat-splash__prompts">
              {QUICK_PROMPTS.map((p) => (
                <button
                  key={p.label}
                  className="chat-prompt-btn"
                  onClick={() => handleQuickPrompt(p.text)}
                  disabled={chatStreaming}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {chatMessages.map((msg, i) => (
          <MessageBubble key={`${msg.role}-${i}-${msg.timestamp || ''}`} msg={msg} />
        ))}

        {chatStreaming && <StreamingDots />}

        {chatError && (
          <div className="chat-error">
            {chatError}
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="chat-input">
        <textarea
          ref={textareaRef}
          className="chat-input__textarea"
          placeholder="Ask the DJ assistant..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={chatStreaming}
        />
        {chatStreaming ? (
          <button
            className="chat-input__stop"
            onClick={handleStop}
            title="Stop generating"
          >
            <Square size={12} />
          </button>
        ) : (
          <button
            className="chat-input__send"
            onClick={handleSend}
            disabled={!input.trim()}
            title="Send message"
          >
            <Send size={12} />
          </button>
        )}
      </div>
    </div>
  )
}
