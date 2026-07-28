/* ============================================================
   DARAVE — React hook for Multi-Deck Agent.

   Provides parallel agent control, voice input, and real-time
   deck monitoring.
   ============================================================ */

import { useState, useEffect, useCallback, useRef } from 'react'
import { MultiDeckAgent, getMultiDeckAgent, type DeckAgentState } from '@/lib/multiDeckAgent'
import { useWebSocket } from './useWebSocket'
import type { AgentCallbacks } from '@/lib/agentTools'

export interface UseMultiDeckAgentReturn {
  // WebSocket
  ws: ReturnType<typeof useWebSocket>

  // Agent states per deck
  agents: Record<string, DeckAgentState>
  allAgents: DeckAgentState[]

  // Actions
  startAgent: (deckId: string, task: string) => Promise<void>
  stopAgent: (deckId: string) => void
  stopAll: () => void
  quickMix: (trackA: string, trackB: string) => Promise<void>
  startParallel: (tasks: Array<{ deckId: string; task: string }>) => Promise<void>

  // Voice
  startVoice: () => void
  stopVoice: () => void
  voiceActive: boolean
  voiceTranscript: string

  // Logs
  logs: string[]
  clearLogs: () => void
}

export function useMultiDeckAgent(sessionId: string = 'default'): UseMultiDeckAgentReturn {
  const ws = useWebSocket(sessionId)
  const agentRef = useRef<MultiDeckAgent>(getMultiDeckAgent(sessionId))

  const [agentStates, setAgentStates] = useState<Record<string, DeckAgentState>>({})
  const [logs, setLogs] = useState<string[]>([])
  const [voiceActive, setVoiceActive] = useState(false)
  const [voiceTranscript, setVoiceTranscript] = useState('')
  const recognitionRef = useRef<any>(null)

  // Initialize agent callbacks from WebSocket
  useEffect(() => {
    const agent = agentRef.current
    const callbacks: AgentCallbacks = {
      loadTrack: async (deck, trackName) => {
        await ws.loadTrack(deck, trackName)
        return `Loaded "${trackName}" into Deck ${deck}`
      },
      play: async () => { await ws.play('A'); return 'Playing Deck A' },
      pause: async () => { await ws.pause('A'); return 'Paused Deck A' },
      stop: async () => { await ws.stop('A'); return 'Stopped Deck A' },
      setCrossfader: async (pos) => { await ws.setCrossfader(pos); return `Crossfader set to ${pos}` },
      setVolume: async (deck, vol) => { await ws.setVolume(deck, vol); return `Volume ${deck} set to ${vol}` },
      setEffect: async (deck, effect) => { await ws.setEffect(deck, effect); return `Effect ${effect} applied to Deck ${deck}` },
      checkCompatibility: async () => {
        const state = ws.session
        if (!state) return 'No session data'
        const deckA = state.decks?.['A']
        const deckB = state.decks?.['B']
        if (!deckA?.track_name || !deckB?.track_name) return 'Load tracks first'
        return `Deck A: ${deckA.track_name} (${deckA.bpm} BPM, ${deckA.camelot}) | Deck B: ${deckB.track_name} (${deckB.bpm} BPM, ${deckB.camelot})`
      },
      startRemix: async () => 'Remix started (server-side)',
      getDeckInfo: async () => {
        const state = ws.session
        if (!state) return 'No session'
        return JSON.stringify(state.decks, null, 2)
      },
      listLibrary: async () => {
        try {
          const res = await fetch('/api/library')
          const data = await res.json()
          return data.songs?.map((s: any) => s.name).join('\n') || 'Library empty'
        } catch { return 'Failed to fetch library' }
      },
    }

    agent.setCallbacks(callbacks)
    agent.onStateChange((deckId, state) => {
      setAgentStates((prev) => ({ ...prev, [deckId]: state }))
    })
    agent.onLog((msg) => {
      setLogs((prev) => [...prev.slice(-50), `[${new Date().toLocaleTimeString()}] ${msg}`])
    })
  }, [ws])

  // Voice recognition
  const startVoice = useCallback(() => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognition) {
      setLogs((prev) => [...prev, 'Speech recognition not supported'])
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

      // On final result, send to agent
      if (event.results[event.resultIndex].isFinal) {
        const text = transcript.trim()
        if (text) {
          setLogs((prev) => [...prev, `[Voice] ${text}`])
          // Route to deck A agent by default
          agentRef.current.startAgent('A', text)
        }
      }
    }

    recognition.onerror = (event: any) => {
      console.error('Speech recognition error:', event.error)
      setVoiceActive(false)
    }

    recognition.onend = () => {
      if (voiceActive) {
        recognition.start() // Restart if still active
      }
    }

    recognition.start()
    recognitionRef.current = recognition
    setVoiceActive(true)
  }, [voiceActive])

  const stopVoice = useCallback(() => {
    recognitionRef.current?.stop()
    recognitionRef.current = null
    setVoiceActive(false)
    setVoiceTranscript('')
  }, [])

  return {
    ws,
    agents: agentStates,
    allAgents: Object.values(agentStates),
    startAgent: (deckId, task) => agentRef.current.startAgent(deckId, task),
    stopAgent: (deckId) => agentRef.current.stopAgent(deckId),
    stopAll: () => agentRef.current.stopAll(),
    quickMix: (trackA, trackB) => agentRef.current.quickMix(trackA, trackB),
    startParallel: (tasks) => agentRef.current.startParallel(tasks),
    startVoice,
    stopVoice,
    voiceActive,
    voiceTranscript,
    logs,
    clearLogs: () => setLogs([]),
  }
}
