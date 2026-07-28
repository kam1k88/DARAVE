/* ============================================================
   DARAVE — AI Chat Panel (Agent Mode)
   Streams responses from Ollama via SSE, supports tool calls.
   Agent mode: AI can control decks, load tracks, play, mix.
   ============================================================ */

import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Square, Trash2, BotMessageSquare, Wrench, Zap, ZapOff } from 'lucide-react'
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
  { label: 'Load & play', text: 'Load the first track from my library into Deck A and play it' },
  { label: 'Mix two tracks', text: 'Load two tracks, check compatibility, and start a mix' },
  { label: 'List library', text: 'List all tracks in my library' },
  { label: 'Recommend a transition', text: 'Recommend a transition for my two most recent tracks' },
]

const AGENT_PROMPTS = [
  'Load the first track from my library into Deck A and play it',
  'Load two tracks and start mixing them',
  'Check compatibility between the loaded tracks',
  'Set crossfader to the center',
  'Stop playback',
  'What tracks are in my library?',
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

interface ChatPanelProps {
  agentCallbacks?: AgentCallbacks
}

export function ChatPanel({ agentCallbacks }: ChatPanelProps) {
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
  const [agentMode, setAgentMode] = useState(true)
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

    // Build system prompt with agent capabilities
    const agentSystemPrompt = agentMode && agentCallbacks ? [
      'You are DARAVE AI DJ Agent. You can control the DJ interface directly.',
      'You have access to these frontend tools that execute actions in the browser:',
      ...FRONTEND_TOOLS.map((t) => `- ${t.name}: ${t.description}`),
      '',
      'When the user asks you to do something with the decks, use the appropriate tool.',
      'For example, to load a track: call load_track_to_deck with deck="A" and track_name="..."',
      'To play: call play_deck. To mix: call load_track_to_deck for both decks then check_compatibility.',
      '',
      'IMPORTANT: When you want to use a frontend tool, respond with a JSON tool call like:',
      '{"tool_call": {"id": "call_123", "name": "tool_name", "arguments": {...}}}',
      'The frontend will execute it and send you the result. Then continue your response.',
    ].join('\n') : ''

    // Add placeholder for assistant response
    const assistantMsg: ChatMessage = { role: 'assistant', content: '', timestamp: new Date().toISOString() }
    addChatMessage(assistantMsg)

    try {
      // Build messages for the API
      const apiMessages: ChatMessage[] = [...chatMessages, userMsg].map((m) => ({
        role: m.role as ChatMessage['role'],
        content: m.content,
        toolCallId: m.toolCallId,
      }))

      // Prepend system prompt if agent mode
      if (agentSystemPrompt) {
        apiMessages.unshift({ role: 'system', content: agentSystemPrompt })
      }

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
              // Stream complete — check if last message has a frontend tool call
              if (agentMode && agentCallbacks) {
                const store = useAppStore.getState()
                const lastAssistant = [...store.chatMessages].reverse().find((m) => m.role === 'assistant')
                if (lastAssistant?.content) {
                  // Look for JSON tool call in the response
                  const toolCallMatch = lastAssistant.content.match(/\{"tool_call"\s*:\s*(\{[^}]+\})\}/)
                  if (toolCallMatch) {
                    try {
                      const toolCall = JSON.parse(toolCallMatch[1]) as FrontendToolCall
                      // Execute the frontend tool
                      const result = await executeFrontendTool(toolCall, agentCallbacks)
                      // Add tool result message
                      const toolMsg: ChatMessage = {
                        role: 'tool',
                        content: `[${result.name}] ${result.result}`,
                        toolCallId: result.id,
                        timestamp: new Date().toISOString(),
                      }
                      useAppStore.setState((s) => ({
                        chatMessages: [...s.chatMessages, toolMsg],
                      }))
                      // Send result back to AI for follow-up
                      // (This creates a loop — AI sees result and responds)
                    } catch {
                      // Not a valid tool call, ignore
                    }
                  }
                }
              }
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
  }, [chatMessages, chatStreaming, addChatMessage, appendToLastAssistantMessage, setChatStreaming, setChatError, agentMode, agentCallbacks])

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
          className={`chat-agent-toggle ${agentMode ? 'chat-agent-toggle--active' : ''}`}
          onClick={() => setAgentMode(!agentMode)}
          title={agentMode ? 'Agent mode ON — AI controls decks' : 'Agent mode OFF — AI only suggests'}
        >
          {agentMode ? <Zap size={10} /> : <ZapOff size={10} />}
          {agentMode ? 'Agent' : 'Chat'}
        </button>
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
            <div className="chat-splash__title">
              {agentMode ? 'DJ Agent' : 'DJ Assistant'}
            </div>
            <div className="chat-splash__hint">
              {agentMode
                ? 'I can control the decks, load tracks, play, mix — just ask!'
                : 'Ask about mixing, transitions, track compatibility, or let me optimize your set.'}
            </div>
            <div className="chat-splash__prompts">
              {(agentMode ? AGENT_PROMPTS : QUICK_PROMPTS.map((p) => p.text)).map((text, i) => (
                <button
                  key={i}
                  className="chat-prompt-btn"
                  onClick={() => handleQuickPrompt(text)}
                  disabled={chatStreaming}
                >
                  {text.length > 40 ? text.slice(0, 40) + '...' : text}
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
          placeholder={agentMode ? 'Tell the agent what to do...' : 'Ask the DJ assistant...'}
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
