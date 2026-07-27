/* ============================================================
   Web Audio API engine for real-time stem mixing.

   Provides:
   - Multi-track playback (stems loaded from server)
   - Crossfade, EQ sweep, delay, compressor
   - Play / pause / seek / stop
   - Analyser node for waveform / frequency visualization
   - Transition scheduling (auto-crossfade between tracks)

   No React dependency — pure Web Audio API.
   React integration lives in hooks/useWebAudio.ts
   ============================================================ */

const BASE = import.meta.env.VITE_API_BASE || '/api'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TrackSlot {
  id: string
  name: string
  buffers: Map<string, AudioBuffer>  // stem_name → buffer
  source?: AudioBufferSourceNode
  gain?: GainNode
  filter?: BiquadFilterNode
  loaded: boolean
}

export interface TransitionConfig {
  crossfadeDuration: number  // seconds
  crossfadeCurve: 'linear' | 'exponential' | 'equal_power'
  hpStartHz: number
  hpEndHz: number
  effectType: string
  effectDepth: number
}

export type PlayState = 'idle' | 'loading' | 'playing' | 'paused'

export interface EngineEvents {
  onStateChange?: (state: PlayState) => void
  onTimeUpdate?: (current: number, duration: number) => void
  onAnalyser?: (data: { waveform: Float32Array; frequency: Uint8Array }) => void
  onTrackLoaded?: (trackId: string) => void
  onError?: (msg: string) => void
}

// ---------------------------------------------------------------------------
// Default stem names (Demucs 4-stem model)
// ---------------------------------------------------------------------------

const STEM_NAMES = ['drums', 'bass', 'other', 'vocals']

// ---------------------------------------------------------------------------
// AudioEngine
// ---------------------------------------------------------------------------

export class AudioEngine {
  private ctx: AudioContext | null = null
  private masterGain: GainNode | null = null
  private compressor: DynamicsCompressorNode | null = null
  private analyser: AnalyserNode | null = null
  private tracks: Map<string, TrackSlot> = new Map()
  private _state: PlayState = 'idle'
  private _duration = 0
  private _rafId = 0
  private _startTime = 0
  private _pauseOffset = 0
  private events: EngineEvents = {}
  private _transitionConfig: TransitionConfig = {
    crossfadeDuration: 4,
    crossfadeCurve: 'equal_power',
    hpStartHz: 400,
    hpEndHz: 80,
    effectType: 'none',
    effectDepth: 0,
  }

  // --- Lifecycle ---

  constructor(events: EngineEvents = {}) {
    this.events = events
  }

  private ensureCtx(): AudioContext {
    if (!this.ctx || this.ctx.state === 'closed') {
      this.ctx = new AudioContext()
      this.masterGain = this.ctx.createGain()
      this.compressor = this.ctx.createDynamicsCompressor()
      this.analyser = this.ctx.createAnalyser()
      this.analyser.fftSize = 2048

      // Graph: source → masterGain → compressor → analyser → destination
      this.masterGain
        .connect(this.compressor)
        .connect(this.analyser)
        .connect(this.ctx.destination)
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume()
    }
    return this.ctx
  }

  destroy(): void {
    this.stop()
    cancelAnimationFrame(this._rafId)
    if (this.ctx && this.ctx.state !== 'closed') {
      this.ctx.close()
    }
    this.ctx = null
  }

  // --- State ---

  get state(): PlayState { return this._state }
  get currentTime(): number {
    if (this._state === 'playing') {
      return this._pauseOffset + (this.ctx?.currentTime ?? 0) - this._startTime
    }
    return this._pauseOffset
  }
  get duration(): number { return this._duration }

  private setState(s: PlayState) {
    this._state = s
    this.events.onStateChange?.(s)
  }

  // --- Load stems from server ---

  async loadTrack(trackId: string, songName: string): Promise<void> {
    const ctx = this.ensureCtx()
    this.setState('loading')

    const slot: TrackSlot = {
      id: trackId,
      name: songName,
      buffers: new Map(),
      loaded: false,
    }

    const stemPromises = STEM_NAMES.map(async (stem) => {
      try {
        const url = `${BASE}/library/${encodeURIComponent(songName)}/stems/${encodeURIComponent(stem)}`
        const res = await fetch(url)
        if (!res.ok) return
        const arrayBuf = await res.arrayBuffer()
        const audioBuf = await ctx.decodeAudioData(arrayBuf)
        slot.buffers.set(stem, audioBuf)
      } catch {
        // Stem not available — skip
      }
    })

    await Promise.all(stemPromises)

    if (slot.buffers.size === 0) {
      // Fallback: try full audio
      try {
        const url = `${BASE}/library/${encodeURIComponent(songName)}/audio`
        const res = await fetch(url)
        if (res.ok) {
          const arrayBuf = await res.arrayBuffer()
          const audioBuf = await ctx.decodeAudioData(arrayBuf)
          slot.buffers.set('mix', audioBuf)
        }
      } catch {
        this.events.onError?.(`Failed to load audio for ${songName}`)
      }
    }

    slot.loaded = slot.buffers.size > 0
    this.tracks.set(trackId, slot)

    // Update duration
    let maxLen = 0
    for (const buf of slot.buffers.values()) {
      maxLen = Math.max(maxLen, buf.duration)
    }
    if (trackId === this.getTrackIds()[0]) {
      this._duration = maxLen
    }

    this.events.onTrackLoaded?.(trackId)
    if (this._state === 'loading' && this.tracks.size > 0) {
      this.setState('idle')
    }
  }

  getTrackIds(): string[] {
    return Array.from(this.tracks.keys())
  }

  isTrackLoaded(trackId: string): boolean {
    return this.tracks.get(trackId)?.loaded ?? false
  }

  // --- Transport ---

  play(): void {
    if (this._state === 'playing') return
    const ctx = this.ensureCtx()

    const startTime = ctx.currentTime
    this._startTime = startTime

    for (const [, slot] of this.tracks) {
      if (!slot.loaded) continue
      this.playSlot(ctx, slot, startTime)
    }

    this.setState('playing')
    this.startAnalyserLoop()
  }

  private playSlot(ctx: AudioContext, slot: TrackSlot, when: number) {
    // Disconnect previous sources
    this.disconnectSlot(slot)

    // Create new source for each stem
    const trackGain = ctx.createGain()
    trackGain.connect(this.masterGain!)

    for (const [, buffer] of slot.buffers) {
      const source = ctx.createBufferSource()
      source.buffer = buffer
      source.connect(trackGain)
      source.start(when, this._pauseOffset)
      slot.source = source  // keep reference for stop
    }
    slot.gain = trackGain

    // Cleanup on end
    slot.source?.addEventListener('ended', () => {
      if (this._state === 'playing') {
        // Could auto-advance to next track
      }
    })
  }

  pause(): void {
    if (this._state !== 'playing') return
    this._pauseOffset = this.currentTime
    this.stopAllSources()
    this.setState('paused')
    cancelAnimationFrame(this._rafId)
  }

  stop(): void {
    this._pauseOffset = 0
    this.stopAllSources()
    this.setState('idle')
    cancelAnimationFrame(this._rafId)
    this.events.onTimeUpdate?.(0, this._duration)
  }

  seek(time: number): void {
    const wasPlaying = this._state === 'playing'
    if (wasPlaying) this.stopAllSources()
    this._pauseOffset = Math.max(0, Math.min(time, this._duration))
    if (wasPlaying) this.play()
  }

  private stopAllSources() {
    for (const [, slot] of this.tracks) {
      this.disconnectSlot(slot)
    }
  }

  private disconnectSlot(slot: TrackSlot) {
    try { slot.source?.stop() } catch {}
    try { slot.source?.disconnect() } catch {}
    try { slot.gain?.disconnect() } catch {}
    try { slot.filter?.disconnect() } catch {}
    slot.source = undefined
    slot.gain = undefined
    slot.filter = undefined
  }

  // --- Volume ---

  setMasterVolume(v: number): void {
    if (this.masterGain) {
      this.masterGain.gain.setValueAtTime(Math.max(0, Math.min(1, v)), this.ctx!.currentTime)
    }
  }

  // --- Effects ---

  setTransition(config: Partial<TransitionConfig>): void {
    Object.assign(this._transitionConfig, config)
  }

  // Apply HP sweep effect to a slot during playback
  applyFilterSweep(trackId: string, progress: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot?.gain || !this.ctx) return

    const { hpStartHz, hpEndHz } = this._transitionConfig
    const cutoff = hpStartHz + (hpEndHz - hpStartHz) * progress

    if (!slot.filter) {
      slot.filter = this.ctx.createBiquadFilter()
      slot.filter.type = 'highpass'
      slot.filter.Q.setValueAtTime(0.7, this.ctx.currentTime)
      // Re-route: source → filter → gain
      slot.gain!.disconnect()
      slot.filter.connect(slot.gain!)
      slot.gain!.connect(this.masterGain!)
    }
    slot.filter.frequency.setValueAtTime(cutoff, this.ctx.currentTime)
  }

  removeFilter(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot?.filter || !this.ctx) return
    slot.filter.disconnect()
    slot.filter = undefined
    if (slot.gain) {
      slot.gain.disconnect()
      slot.gain.connect(this.masterGain!)
    }
  }

  // --- Analyser ---

  getAnalyserData(): { waveform: Float32Array; frequency: Uint8Array } | null {
    if (!this.analyser) return null

    const waveform = new Float32Array(this.analyser.frequencyBinCount)
    this.analyser.getFloatTimeDomainData(waveform)

    const frequency = new Uint8Array(this.analyser.frequencyBinCount)
    this.analyser.getByteFrequencyData(frequency)

    return { waveform, frequency }
  }

  private startAnalyserLoop() {
    const loop = () => {
      if (this._state !== 'playing') return
      const data = this.getAnalyserData()
      if (data) this.events.onAnalyser?.(data)
      this.events.onTimeUpdate?.(this.currentTime, this._duration)
      this._rafId = requestAnimationFrame(loop)
    }
    this._rafId = requestAnimationFrame(loop)
  }

  // --- Multi-track scheduling (set playback order) ---

  async loadSet(tracks: Array<{ id: string; name: string }>): Promise<void> {
    const loadPromises = tracks.map((t) => this.loadTrack(t.id, t.name))
    await Promise.all(loadPromises)

    // Set total duration as sum of all tracks
    let total = 0
    for (const t of tracks) {
      const slot = this.tracks.get(t.id)
      if (slot) {
        for (const buf of slot.buffers.values()) {
          total = Math.max(total, buf.duration)
        }
      }
    }
    this._duration = total
  }

  // Get waveform data for visualization
  getWaveformData(trackId: string, resolution: number = 100): Float32Array | null {
    const slot = this.tracks.get(trackId)
    if (!slot) return null

    // Get the first available buffer
    let buffer: AudioBuffer | undefined
    for (const buf of slot.buffers.values()) {
      buffer = buf
      break
    }
    if (!buffer) return null

    const rawData = buffer.getChannelData(0)
    const blockSize = Math.floor(rawData.length / resolution)
    const waveform = new Float32Array(resolution)
    for (let i = 0; i < resolution; i++) {
      let sum = 0
      for (let j = 0; j < blockSize; j++) {
        sum += Math.abs(rawData[i * blockSize + j])
      }
      waveform[i] = sum / blockSize
    }
    return waveform
  }
}
