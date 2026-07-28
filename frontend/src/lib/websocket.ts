/* ============================================================
   DARAVE — WebSocket client for real-time DJ control.

   Connects to ws://host:port/ws/{sessionId}
   - Sends commands (load, play, crossfade, etc.)
   - Receives state updates (deck state, levels)
   - Auto-reconnect on disconnect
   ============================================================ */

const WS_BASE = import.meta.env.VITE_WS_BASE || `ws://${window.location.hostname}:8000`

export interface DeckState {
  deck_id: string
  track_name: string
  track_id: string
  bpm: number
  key: string
  camelot: string
  duration: number
  position: number
  play_state: 'idle' | 'loading' | 'playing' | 'paused'
  volume: number
  crossfade: number
  eq_low: number
  eq_mid: number
  eq_high: number
  effect: string
  stem_volumes: Record<string, number>
  cue_point: number
  loop_start: number
  loop_end: number
  loop_active: boolean
  pitch: number
}

export interface SessionState {
  session_id: string
  decks: Record<string, DeckState>
  master_bpm: number
  master_key: string
  crossfade_position: number
  master_volume: number
}

export type WSMessageType =
  | 'session_state'
  | 'deck_update'
  | 'deck_state'
  | 'crossfader_update'
  | 'levels'
  | 'error'

export interface WSMessage {
  type: WSMessageType
  data: any
  ts?: string
}

export type WSCommand = {
  action: string
  deck?: string
  [key: string]: any
}

type MessageHandler = (msg: WSMessage) => void
type StateChangeHandler = (state: 'connecting' | 'connected' | 'disconnected') => void

export class DJWebSocket {
  private ws: WebSocket | null = null
  private url: string
  private handlers: Set<MessageHandler> = new Set()
  private stateHandlers: Set<StateChangeHandler> = new Set()
  private _state: 'connecting' | 'connected' | 'disconnected' = 'disconnected'
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectAttempts = 0
  private maxReconnectDelay = 10000
  private pendingCommands: Array<{ cmd: WSCommand; resolve: (v: any) => void; reject: (e: Error) => void }> = []

  constructor(sessionId: string = 'default') {
    this.url = `${WS_BASE}/ws/${sessionId}`
  }

  get state() { return this._state }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return
    }

    this.setState('connecting')
    this.ws = new WebSocket(this.url)

    this.ws.onopen = () => {
      this.setState('connected')
      this.reconnectAttempts = 0
      // Flush pending commands
      while (this.pendingCommands.length > 0) {
        const pending = this.pendingCommands.shift()!
        this.send(pending.cmd).then(pending.resolve).catch(pending.reject)
      }
    }

    this.ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data)
        this.handlers.forEach((h) => h(msg))
      } catch { /* skip */ }
    }

    this.ws.onclose = () => {
      this.setState('disconnected')
      this.scheduleReconnect()
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.reconnectAttempts = 0
    this.ws?.close()
    this.ws = null
    this.setState('disconnected')
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), this.maxReconnectDelay)
    this.reconnectAttempts++
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private setState(s: 'connecting' | 'connected' | 'disconnected') {
    this._state = s
    this.stateHandlers.forEach((h) => h(s))
  }

  onMessage(handler: MessageHandler): () => void {
    this.handlers.add(handler)
    return () => this.handlers.delete(handler)
  }

  onStateChange(handler: StateChangeHandler): () => void {
    this.stateHandlers.add(handler)
    return () => this.stateHandlers.delete(handler)
  }

  send(cmd: WSCommand): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        this.pendingCommands.push({ cmd, resolve, reject })
        this.connect()
        return
      }
      const id = Math.random().toString(36).slice(2)
      const msg = JSON.stringify({ ...cmd, _id: id })

      const handler = (event: MessageEvent) => {
        try {
          const resp = JSON.parse(event.data)
          if (resp._id === id) {
            this.ws?.removeEventListener('message', handler)
            resolve(resp)
          }
        } catch { /* skip */ }
      }
      this.ws.addEventListener('message', handler)
      this.ws.send(msg)

      // Timeout after 10s
      setTimeout(() => {
        this.ws?.removeEventListener('message', handler)
        reject(new Error('Command timeout'))
      }, 10000)
    })
  }

  // Convenience methods
  loadTrack(deck: string, trackName: string, trackId?: string) {
    return this.send({ action: 'load_track', deck, track_name: trackName, track_id: trackId })
  }

  play(deck: string) {
    return this.send({ action: 'play', deck })
  }

  pause(deck: string) {
    return this.send({ action: 'pause', deck })
  }

  stop(deck: string) {
    return this.send({ action: 'stop', deck })
  }

  setCrossfader(position: number) {
    return this.send({ action: 'set_crossfader', position })
  }

  setVolume(deck: string, volume: number) {
    return this.send({ action: 'set_volume', deck, volume })
  }

  setEQ(deck: string, band: 'low' | 'mid' | 'high', value: number) {
    return this.send({ action: 'set_eq', deck, band, value })
  }

  setEffect(deck: string, effect: string) {
    return this.send({ action: 'set_effect', deck, effect })
  }

  setStemVolume(deck: string, stem: string, volume: number) {
    return this.send({ action: 'set_stem_volume', deck, stem, volume })
  }

  seek(deck: string, position: number) {
    return this.send({ action: 'seek', deck, position })
  }

  setPitch(deck: string, pitch: number) {
    return this.send({ action: 'set_pitch', deck, pitch })
  }

  getState() {
    return this.send({ action: 'get_state' })
  }

  batch(commands: WSCommand[]) {
    return this.send({ action: 'batch', commands })
  }
}

// Singleton factory
let _instance: DJWebSocket | null = null

export function getDJWebSocket(sessionId?: string): DJWebSocket {
  if (!_instance) {
    _instance = new DJWebSocket(sessionId || 'default')
  }
  return _instance
}
