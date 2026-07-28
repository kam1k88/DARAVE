/* ============================================================
   DARAVE — Multi-Deck Parallel Agent.

   Key feature: Multiple AI agents run simultaneously, each
   controlling a different deck. Real-time audio monitoring,
   immediate response to commands.

   Architecture:
   - One agent per deck (A, B, C, D) running in parallel
   - Each agent has its own LLM conversation
   - Shared session state via WebSocket
   - Voice input processed live while mixing
   ============================================================ */

import { DJWebSocket, getDJWebSocket } from '@/lib/websocket'
import { FRONTEND_TOOLS, executeFrontendTool, type AgentCallbacks, type FrontendToolCall } from '@/lib/agentTools'

const OLLAMA_BASE = import.meta.env.VITE_OLLAMA_BASE || 'http://localhost:11501'
const OLLAMA_MODEL = import.meta.env.VITE_OLLAMA_MODEL || 'llama3.1:8b'
const MAX_AGENT_ROUNDS = 15

// ---------------------------------------------------------------------------
// Agent state per deck
// ---------------------------------------------------------------------------

export interface DeckAgentState {
  deckId: string
  active: boolean
  streaming: boolean
  messages: Array<{ role: string; content: string; toolCalls?: any[] }>
  lastAction: string
  lastError: string | null
  round: number
}

// ---------------------------------------------------------------------------
// Multi-Deck Agent
// ---------------------------------------------------------------------------

export class MultiDeckAgent {
  private ws: DJWebSocket
  private agents: Map<string, DeckAgentState> = new Map()
  private abortControllers: Map<string, AbortController> = new Map()
  private callbacks: AgentCallbacks | null = null
  private _onStateChange: ((deckId: string, state: DeckAgentState) => void) | null = null
  private _onMessage: ((deckId: string, msg: { role: string; content: string }) => void) | null = null
  private _onLog: ((msg: string) => void) | null = null

  constructor(sessionId: string = 'default') {
    this.ws = getDJWebSocket(sessionId)
    for (const deckId of ['A', 'B', 'C', 'D']) {
      this.agents.set(deckId, {
        deckId,
        active: false,
        streaming: false,
        messages: [],
        lastAction: '',
        lastError: null,
        round: 0,
      })
    }
  }

  setCallbacks(callbacks: AgentCallbacks) {
    this.callbacks = callbacks
  }

  onStateChange(handler: (deckId: string, state: DeckAgentState) => void) {
    this._onStateChange = handler
  }

  onMessage(handler: (deckId: string, msg: { role: string; content: string }) => void) {
    this._onMessage = handler
  }

  onLog(handler: (msg: string) => void) {
    this._onLog = handler
  }

  private log(msg: string) {
    this._onLog?.(msg)
    console.log(`[MultiDeckAgent] ${msg}`)
  }

  private emitState(deckId: string) {
    const state = this.agents.get(deckId)
    if (state) this._onStateChange?.(deckId, { ...state })
  }

  // -----------------------------------------------------------------------
  // Start an agent for a specific deck
  // -----------------------------------------------------------------------

  async startAgent(deckId: string, task: string) {
    const agent = this.agents.get(deckId)
    if (!agent) return

    agent.active = true
    agent.streaming = false
    agent.messages = [{ role: 'user', content: task }]
    agent.lastAction = ''
    agent.lastError = null
    agent.round = 0
    this.emitState(deckId)
    this.log(`Agent ${deckId} started: ${task.slice(0, 60)}...`)

    // Run agent loop
    this.runAgentLoop(deckId)
  }

  // -----------------------------------------------------------------------
  // Stop an agent
  // -----------------------------------------------------------------------

  stopAgent(deckId: string) {
    const agent = this.agents.get(deckId)
    if (!agent) return

    agent.active = false
    agent.streaming = false
    this.abortControllers.get(deckId)?.abort()
    this.abortControllers.delete(deckId)
    this.emitState(deckId)
    this.log(`Agent ${deckId} stopped`)
  }

  // -----------------------------------------------------------------------
  // Stop all agents
  // -----------------------------------------------------------------------

  stopAll() {
    for (const deckId of this.agents.keys()) {
      this.stopAgent(deckId)
    }
  }

  // -----------------------------------------------------------------------
  // Get agent state
  // -----------------------------------------------------------------------

  getAgentState(deckId: string): DeckAgentState | undefined {
    return this.agents.get(deckId)
  }

  getAllStates(): DeckAgentState[] {
    return Array.from(this.agents.values())
  }

  // -----------------------------------------------------------------------
  // Agent loop — runs until done or max rounds
  // -----------------------------------------------------------------------

  private async runAgentLoop(deckId: string) {
    const agent = this.agents.get(deckId)
    if (!agent || !agent.active) return

    const abort = new AbortController()
    this.abortControllers.set(deckId, abort)

    try {
      while (agent.active && agent.round < MAX_AGENT_ROUNDS) {
        agent.round++
        agent.streaming = true
        this.emitState(deckId)

        // Build system prompt
        const systemPrompt = this.buildSystemPrompt(deckId)

        // Build messages for LLM
        const apiMessages = [
          { role: 'system', content: systemPrompt },
          ...agent.messages.map((m) => ({ role: m.role, content: m.content })),
        ]

        // Stream LLM response
        let responseText = ''
        try {
          responseText = await this.streamLLM(apiMessages, (chunk) => {
            responseText += chunk
            this._onMessage?.(deckId, { role: 'assistant', content: responseText })
          }, abort.signal)
        } catch (err: any) {
          if (err?.name === 'AbortError') break
          agent.lastError = err?.message || 'LLM error'
          this.emitState(deckId)
          break
        }

        // Add assistant response to history
        agent.messages.push({ role: 'assistant', content: responseText })

        // Check for tool calls
        const toolCalls = this.extractToolCalls(responseText)

        if (toolCalls.length === 0) {
          // No tool calls — agent is done
          agent.streaming = false
          agent.lastAction = 'completed'
          this.emitState(deckId)
          break
        }

        // Execute each tool call
        for (const tc of toolCalls) {
          if (!agent.active || !this.callbacks) break

          agent.lastAction = tc.name
          this.emitState(deckId)
          this.log(`Agent ${deckId}: ${tc.name}(${JSON.stringify(tc.arguments).slice(0, 80)})`)

          // Execute tool
          const result = await executeFrontendTool(tc, this.callbacks)

          // Add tool result to history
          const toolMsg = `[${result.name}] ${result.result}`
          agent.messages.push({ role: 'tool', content: toolMsg })

          this._onMessage?.(deckId, { role: 'tool', content: toolMsg })

          if (!result.success) {
            agent.lastError = result.result
            break
          }
        }
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        agent.lastError = err?.message || 'Agent loop error'
      }
    } finally {
      agent.streaming = false
      this.abortControllers.delete(deckId)
      this.emitState(deckId)
    }
  }

  // -----------------------------------------------------------------------
  // Build system prompt for a deck agent
  // -----------------------------------------------------------------------

  private buildSystemPrompt(deckId: string): string {
    const session = (this.ws as any).lastSession || null
    const deckInfo = session?.decks?.[deckId]

    return [
      `You are DARAVE AI Deck ${deckId} Agent. You control Deck ${deckId} directly.`,
      '',
      'Available tools:',
      ...FRONTEND_TOOLS.map((t) => `  ${t.name}: ${t.description} | args: ${JSON.stringify(Object.keys(t.parameters))}`),
      '',
      'CURRENT DECK STATE:',
      deckInfo ? `  Track: ${deckInfo.track_name || 'empty'}, BPM: ${deckInfo.bpm}, Playing: ${deckInfo.play_state}` : `  Deck ${deckId} is empty`,
      '',
      'RULES:',
      '1. When given a task, IMMEDIATELY call the appropriate tool.',
      '2. Do NOT explain what you will do — just DO it by outputting the tool call.',
      '3. Output EXACTLY one JSON tool call per response:',
      '   {"tool_call": {"id": "call_N", "name": "TOOL_NAME", "arguments": {}}}',
      '4. After seeing tool result, continue with next action if needed.',
      '5. Be concise. No explanations unless asked.',
      '6. You can load tracks, play, pause, set effects, adjust EQ, crossfade.',
      '7. Always load a track first if the deck is empty.',
    ].join('\n')
  }

  // -----------------------------------------------------------------------
  // Stream LLM response from Ollama
  // -----------------------------------------------------------------------

  private async streamLLM(
    messages: Array<{ role: string; content: string }>,
    onChunk: (text: string) => void,
    abortSignal?: AbortSignal,
  ): Promise<string> {
    let fullText = ''

    const res = await fetch(`${OLLAMA_BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: OLLAMA_MODEL,
        messages,
        stream: true,
      }),
      signal: abortSignal,
    })

    if (!res.ok) throw new Error(`Ollama error: ${res.status}`)

    const reader = res.body?.getReader()
    if (!reader) throw new Error('No response body')

    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.trim()) continue
        try {
          const parsed = JSON.parse(line)
          if (parsed.message?.content) {
            onChunk(parsed.message.content)
            fullText += parsed.message.content
          }
        } catch { /* skip */ }
      }
    }

    return fullText
  }

  // -----------------------------------------------------------------------
  // Extract tool calls from LLM response text
  // -----------------------------------------------------------------------

  private extractToolCalls(text: string): FrontendToolCall[] {
    const calls: FrontendToolCall[] = []
    const pattern = /\{"tool_call"\s*:\s*(\{[^}]+(?:\{[^}]*\}[^}]*)?\})\s*\}/g
    let match
    while ((match = pattern.exec(text)) !== null) {
      try {
        const tc = JSON.parse(match[1])
        if (tc.name && typeof tc.name === 'string') {
          calls.push({
            id: tc.id || `call_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            name: tc.name,
            arguments: tc.arguments || {},
          })
        }
      } catch { /* skip */ }
    }
    return calls
  }

  // -----------------------------------------------------------------------
  // Parallel execution — start multiple agents at once
  // -----------------------------------------------------------------------

  async startParallel(tasks: Array<{ deckId: string; task: string }>) {
    const promises = tasks.map((t) => this.startAgent(t.deckId, t.task))
    await Promise.allSettled(promises)
  }

  // -----------------------------------------------------------------------
  // Quick mix — load two tracks and start mixing
  // -----------------------------------------------------------------------

  async quickMix(trackA: string, trackB: string) {
    await this.startParallel([
      { deckId: 'A', task: `Load "${trackA}" into Deck A and start playing it` },
      { deckId: 'B', task: `Load "${trackB}" into Deck B and start playing it` },
    ])
  }
}

// ---------------------------------------------------------------------------
// Singleton
// ---------------------------------------------------------------------------

let _agent: MultiDeckAgent | null = null

export function getMultiDeckAgent(sessionId?: string): MultiDeckAgent {
  if (!_agent) {
    _agent = new MultiDeckAgent(sessionId)
  }
  return _agent
}
