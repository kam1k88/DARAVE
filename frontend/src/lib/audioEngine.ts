/* ============================================================
   Web Audio API engine for real-time stem mixing.

   Provides:
   - Multi-track playback (stems loaded from server)
   - Per-stem volume, mute/solo
   - 3-band EQ (BiquadFilter) per track
   - Stereo panning per track
   - Crossfade between 2 decks (3 curves)
   - Live effects (delay, reverb, filter, distortion)
   - Play / pause / seek / stop
   - Analyser node for waveform / frequency visualization
   - RMS level metering per track
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

  // Per-stem gain nodes
  stemGains: Map<string, GainNode>
  // Mix bus: all stem gains → stemMixGain
  stemMixGain: GainNode
  // EQ chain
  eqLow: BiquadFilterNode
  eqMid: BiquadFilterNode
  eqHigh: BiquadFilterNode
  // Panner
  panner: StereoPannerNode
  // Slot output gain
  slotGain: GainNode
  // Crossfade gain
  crossfadeGain: GainNode

  // Effect nodes
  effectInput?: GainNode
  effectOutput?: GainNode
  effectDelay?: DelayNode
  effectFeedback?: GainNode
  effectFilter?: BiquadFilterNode
  effectDistortion?: WaveShaperNode

  // State
  loaded: boolean
  muted: boolean
  solo: boolean
  isPlaying: boolean
  pauseOffset: number
  startTime: number
  stemVolumes: Map<string, number>  // stem_name → 0-1
  eqValues: { low: number; mid: number; high: number }  // dB
  pan: number  // -1 to 1
  volume: number  // 0-1
  currentEffect: string
  // Level metering
  levelAnalyser: AnalyserNode
}

export interface TransitionConfig {
  crossfadeDuration: number  // seconds
  crossfadeCurve: 'linear' | 'exponential' | 'equal_power'
  crossfadeType: 'linear' | 'power' | 'exponential'
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
  onLevels?: (levels: Map<string, number>) => void
}

// ---------------------------------------------------------------------------
// Crossfade curves
// ---------------------------------------------------------------------------

function crossfadeGain(value: number, curve: 'linear' | 'power' | 'exponential'): number {
  // value: 0 = full A, 1 = full B
  switch (curve) {
    case 'linear':
      return value
    case 'power':
      // Equal power: sqrt gives ~-3dB at center
      return Math.sqrt(value)
    case 'exponential':
      return value * value
  }
}

// ---------------------------------------------------------------------------
// Distortion curve generator
// ---------------------------------------------------------------------------

function makeDistortionCurve(amount: number): Float32Array {
  const n = 44100
  const curve = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / n - 1
    curve[i] = ((3 + amount) * x * 20 * (Math.PI / 180)) /
      (Math.PI + amount * Math.abs(x))
  }
  return curve
}

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
  private _levelRafId = 0
  private _pauseOffset = 0
  private events: EngineEvents = {}
  private _transitionConfig: TransitionConfig = {
    crossfadeDuration: 4,
    crossfadeCurve: 'equal_power',
    crossfadeType: 'power',
    hpStartHz: 400,
    hpEndHz: 80,
    effectType: 'none',
    effectDepth: 0,
  }
  private _crossfadeValue = 0  // -1 (full A) to +1 (full B), or 0 = center

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

      // Graph: tracks → masterGain → compressor → analyser → destination
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
    cancelAnimationFrame(this._levelRafId)
    if (this.ctx && this.ctx.state !== 'closed') {
      this.ctx.close()
    }
    this.ctx = null
  }

  // --- State ---

  get state(): PlayState { return this._state }
  get currentTime(): number {
    // Return the time of the first playing deck, or the first deck's offset
    for (const [, slot] of this.tracks) {
      if (slot.isPlaying) {
        return slot.pauseOffset + (this.ctx?.currentTime ?? 0) - slot.startTime
      }
    }
    // Return first deck's pause offset
    for (const [, slot] of this.tracks) {
      return slot.pauseOffset
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

    const slot = this.createEmptySlot(trackId, songName, ctx)

    // Load full audio (MP3/WAV) — skip stems for faster loading
    try {
      const url = `${BASE}/library/${encodeURIComponent(songName)}/audio`
      const res = await fetch(url)
      if (res.ok) {
        const arrayBuf = await res.arrayBuffer()
        const audioBuf = await ctx.decodeAudioData(arrayBuf)
        slot.buffers.set('mix', audioBuf)
      } else {
        this.events.onError?.(`Failed to load audio for ${songName}`)
      }
    } catch {
      this.events.onError?.(`Failed to load audio for ${songName}`)
    }

    slot.loaded = slot.buffers.size > 0
    this.tracks.set(trackId, slot)

    // Update duration — use the longest track
    let maxLen = 0
    for (const buf of slot.buffers.values()) {
      maxLen = Math.max(maxLen, buf.duration)
    }
    this._duration = Math.max(this._duration, maxLen)

    this.events.onTrackLoaded?.(trackId)
    // Fire time update so React state gets duration immediately
    this.events.onTimeUpdate?.(this.currentTime, this._duration)
    if (this._state === 'loading' && this.tracks.size > 0) {
      this.setState('idle')
    }
  }

  private createEmptySlot(trackId: string, songName: string, ctx: AudioContext): TrackSlot {
    const slot: TrackSlot = {
      id: trackId,
      name: songName,
      buffers: new Map(),
      stemGains: new Map(),
      stemMixGain: ctx.createGain(),
      eqLow: ctx.createBiquadFilter(),
      eqMid: ctx.createBiquadFilter(),
      eqHigh: ctx.createBiquadFilter(),
      panner: ctx.createStereoPanner(),
      slotGain: ctx.createGain(),
      crossfadeGain: ctx.createGain(),
      loaded: false,
      muted: false,
      solo: false,
      isPlaying: false,
      pauseOffset: 0,
      startTime: 0,
      stemVolumes: new Map(),
      eqValues: { low: 0, mid: 0, high: 0 },
      pan: 0,
      volume: 1,
      currentEffect: 'none',
      levelAnalyser: ctx.createAnalyser(),
    }

    // Init EQ
    slot.eqLow.type = 'lowshelf'
    slot.eqLow.frequency.setValueAtTime(320, ctx.currentTime)
    slot.eqLow.gain.setValueAtTime(0, ctx.currentTime)

    slot.eqMid.type = 'peaking'
    slot.eqMid.frequency.setValueAtTime(1000, ctx.currentTime)
    slot.eqMid.Q.setValueAtTime(0.7, ctx.currentTime)
    slot.eqMid.gain.setValueAtTime(0, ctx.currentTime)

    slot.eqHigh.type = 'highshelf'
    slot.eqHigh.frequency.setValueAtTime(3200, ctx.currentTime)
    slot.eqHigh.gain.setValueAtTime(0, ctx.currentTime)

    // Level analyser config
    slot.levelAnalyser.fftSize = 256
    slot.levelAnalyser.smoothingTimeConstant = 0.8

    // Chain: stemGains → stemMixGain → eqLow → eqMid → eqHigh → panner → slotGain → crossfadeGain → master
    slot.stemMixGain
      .connect(slot.eqLow)
      .connect(slot.eqMid)
      .connect(slot.eqHigh)
      .connect(slot.panner)
      .connect(slot.slotGain)
      .connect(slot.crossfadeGain)
      .connect(this.masterGain!)
      .connect(slot.levelAnalyser)

    return slot
  }

  getTrackIds(): string[] {
    return Array.from(this.tracks.keys())
  }

  isTrackLoaded(trackId: string): boolean {
    return this.tracks.get(trackId)?.loaded ?? false
  }

  // --- Transport ---

  play(): void {
    for (const [id, slot] of this.tracks) {
      if (!slot.loaded || slot.isPlaying) continue
      this.playDeck(id)
    }
  }

  playDeck(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !slot.loaded || slot.isPlaying) return
    const ctx = this.ensureCtx()

    slot.startTime = ctx.currentTime
    slot.isPlaying = true
    this.playSlot(ctx, slot, ctx.currentTime)

    if (this._state !== 'playing') {
      this.setState('playing')
      this.startAnalyserLoop()
      this.startLevelLoop()
    }
  }

  private playSlot(ctx: AudioContext, slot: TrackSlot, when: number) {
    this.disconnectSlot(slot)

    // Create source nodes for each stem
    for (const [stemName, buffer] of slot.buffers) {
      const source = ctx.createBufferSource()
      source.buffer = buffer

      // Get or create per-stem gain
      let stemGain = slot.stemGains.get(stemName)
      if (!stemGain) {
        stemGain = ctx.createGain()
        stemGain.gain.setValueAtTime(1, ctx.currentTime)
        slot.stemGains.set(stemName, stemGain)
        slot.stemVolumes.set(stemName, 1)
      }

      // source → stemGain → stemMixGain
      source.connect(stemGain)
      stemGain.connect(slot.stemMixGain)

      source.start(when, slot.pauseOffset)
    }
  }

  pause(): void {
    for (const [id, slot] of this.tracks) {
      if (slot.isPlaying) this.pauseDeck(id)
    }
  }

  pauseDeck(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !slot.isPlaying) return
    const ctx = this.ensureCtx()

    slot.pauseOffset += ctx.currentTime - slot.startTime
    slot.isPlaying = false
    this.disconnectSlot(slot)

    // Check if any deck is still playing
    const anyPlaying = Array.from(this.tracks.values()).some((s) => s.isPlaying)
    if (!anyPlaying) {
      this.setState('paused')
      cancelAnimationFrame(this._rafId)
      cancelAnimationFrame(this._levelRafId)
    }
  }

  stop(): void {
    for (const [id] of this.tracks) {
      this.stopDeck(id)
    }
  }

  stopDeck(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return

    slot.pauseOffset = 0
    slot.isPlaying = false
    this.disconnectSlot(slot)

    const anyPlaying = Array.from(this.tracks.values()).some((s) => s.isPlaying)
    if (!anyPlaying) {
      this.setState('idle')
      cancelAnimationFrame(this._rafId)
      cancelAnimationFrame(this._levelRafId)
      this.events.onTimeUpdate?.(0, this._duration)
    }
  }

  isDeckPlaying(trackId: string): boolean {
    return this.tracks.get(trackId)?.isPlaying ?? false
  }

  getDeckTime(trackId: string): number {
    const slot = this.tracks.get(trackId)
    if (!slot) return 0
    if (slot.isPlaying) {
      return slot.pauseOffset + (this.ctx?.currentTime ?? 0) - slot.startTime
    }
    return slot.pauseOffset
  }

  seek(time: number): void {
    this._pauseOffset = Math.max(0, Math.min(time, this._duration))
    for (const [id, slot] of this.tracks) {
      if (slot.isPlaying) {
        this.pauseDeck(id)
        slot.pauseOffset = this._pauseOffset
        this.playDeck(id)
      } else {
        slot.pauseOffset = this._pauseOffset
      }
    }
  }

  private disconnectSlot(slot: TrackSlot) {
    for (const [, gain] of slot.stemGains) {
      try { gain.disconnect() } catch {}
    }
    try { slot.stemMixGain.disconnect() } catch {}
    try { slot.eqLow.disconnect() } catch {}
    try { slot.eqMid.disconnect() } catch {}
    try { slot.eqHigh.disconnect() } catch {}
    try { slot.panner.disconnect() } catch {}
    try { slot.slotGain.disconnect() } catch {}
    try { slot.crossfadeGain.disconnect() } catch {}
    try { slot.levelAnalyser.disconnect() } catch {}
    // Reconnect chain
    slot.stemMixGain.connect(slot.eqLow)
    slot.eqLow.connect(slot.eqMid)
    slot.eqMid.connect(slot.eqHigh)
    slot.eqHigh.connect(slot.panner)
    slot.panner.connect(slot.slotGain)
    slot.slotGain.connect(slot.crossfadeGain)
    slot.crossfadeGain.connect(this.masterGain!)
    slot.crossfadeGain.connect(slot.levelAnalyser)
  }

  // --- Volume ---

  setMasterVolume(v: number): void {
    if (this.masterGain) {
      this.masterGain.gain.setValueAtTime(Math.max(0, Math.min(1, v)), this.ctx!.currentTime)
    }
  }

  setTrackVolume(trackId: string, v: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return
    slot.volume = v
    slot.slotGain.gain.setValueAtTime(v, this.ctx!.currentTime)
  }

  // --- Per-stem volume ---

  setStemVolume(trackId: string, stemName: string, v: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return
    const gain = slot.stemGains.get(stemName)
    if (gain) {
      gain.gain.setValueAtTime(Math.max(0, Math.min(1, v)), this.ctx!.currentTime)
      slot.stemVolumes.set(stemName, v)
    }
  }

  setStemMuted(trackId: string, stemName: string, muted: boolean): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return
    const gain = slot.stemGains.get(stemName)
    if (gain) {
      gain.gain.setValueAtTime(muted ? 0 : (slot.stemVolumes.get(stemName) ?? 1), this.ctx!.currentTime)
    }
  }

  setTrackMuted(trackId: string, muted: boolean): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return
    slot.muted = muted
    slot.crossfadeGain.gain.setValueAtTime(muted ? 0 : 1, this.ctx!.currentTime)
  }

  setTrackSolo(trackId: string, solo: boolean): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return
    slot.solo = solo
    // If any track is solo'd, mute all non-solo'd tracks
    const hasSolo = Array.from(this.tracks.values()).some((s) => s.solo)
    for (const [, otherSlot] of this.tracks) {
      if (hasSolo) {
        otherSlot.crossfadeGain.gain.setValueAtTime(
          otherSlot.solo && !otherSlot.muted ? 1 : 0,
          this.ctx!.currentTime,
        )
      } else {
        otherSlot.crossfadeGain.gain.setValueAtTime(
          otherSlot.muted ? 0 : 1,
          this.ctx!.currentTime,
        )
      }
    }
  }

  // --- 3-Band EQ ---

  setEQ(trackId: string, band: 'low' | 'mid' | 'high', dB: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !this.ctx) return
    const clamped = Math.max(-12, Math.min(12, dB))
    slot.eqValues[band] = clamped
    const filter = band === 'low' ? slot.eqLow : band === 'mid' ? slot.eqMid : slot.eqHigh
    filter.gain.setValueAtTime(clamped, this.ctx.currentTime)
  }

  setEQBandEnabled(trackId: string, band: 'low' | 'mid' | 'high', enabled: boolean): void {
    this.setEQ(trackId, band, enabled ? 0 : -12)
  }

  // --- Panning ---

  setPan(trackId: string, value: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !this.ctx) return
    slot.pan = Math.max(-1, Math.min(1, value))
    slot.panner.pan.setValueAtTime(slot.pan, this.ctx.currentTime)
  }

  // --- Crossfade ---

  setCrossfade(value: number): void {
    // value: -1 = full A, 0 = center, +1 = full B
    if (!this.ctx) return
    this._crossfadeValue = Math.max(-1, Math.min(1, value))
    const curve = this._transitionConfig.crossfadeType

    const trackIds = this.getTrackIds()
    if (trackIds.length < 2) return

    // Normalize to 0-1 for curve function
    const t = (this._crossfadeValue + 1) / 2  // 0 = full A, 1 = full B

    const slotA = this.tracks.get(trackIds[0])
    const slotB = this.tracks.get(trackIds[1])

    if (slotA) {
      const gainA = 1 - crossfadeGain(t, curve)
      slotA.crossfadeGain.gain.setValueAtTime(gainA, this.ctx.currentTime)
    }
    if (slotB) {
      const gainB = crossfadeGain(t, curve)
      slotB.crossfadeGain.gain.setValueAtTime(gainB, this.ctx.currentTime)
    }
  }

  setCrossfadeType(type: 'linear' | 'power' | 'exponential'): void {
    this._transitionConfig.crossfadeType = type
    this.setCrossfade(this._crossfadeValue)
  }

  get crossfadeValue(): number { return this._crossfadeValue }

  // --- Effects ---

  setEffect(trackId: string, effectType: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !this.ctx) return

    // Remove existing effect
    this.removeEffect(trackId)

    if (effectType === 'none') {
      slot.currentEffect = 'none'
      return
    }

    const ctx = this.ctx

    // Create effect nodes
    slot.effectInput = ctx.createGain()
    slot.effectOutput = ctx.createGain()

    switch (effectType) {
      case 'echo': {
        slot.effectDelay = ctx.createDelay(2)
        slot.effectDelay.delayTime.setValueAtTime(0.3, ctx.currentTime)
        slot.effectFeedback = ctx.createGain()
        slot.effectFeedback.gain.setValueAtTime(0.4, ctx.currentTime)

        // Input → delay → feedback → delay (loop)
        slot.effectInput.connect(slot.effectDelay)
        slot.effectDelay.connect(slot.effectFeedback)
        slot.effectFeedback.connect(slot.effectDelay)
        // Input → output (dry) + delay → output (wet)
        slot.effectInput.connect(slot.effectOutput)
        slot.effectDelay.connect(slot.effectOutput)
        break
      }
      case 'filter': {
        slot.effectFilter = ctx.createBiquadFilter()
        slot.effectFilter.type = 'lowpass'
        slot.effectFilter.frequency.setValueAtTime(2000, ctx.currentTime)
        slot.effectFilter.Q.setValueAtTime(5, ctx.currentTime)

        slot.effectInput.connect(slot.effectFilter)
        slot.effectFilter.connect(slot.effectOutput)
        break
      }
      case 'reverb': {
        // Simple reverb using convolver with synthetic impulse
        const sampleRate = ctx.sampleRate
        const length = sampleRate * 1.5
        const impulse = ctx.createBuffer(2, length, sampleRate)
        for (let ch = 0; ch < 2; ch++) {
          const data = impulse.getChannelData(ch)
          for (let i = 0; i < length; i++) {
            data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, 2.5)
          }
        }
        const convolver = ctx.createConvolver()
        convolver.buffer = impulse

        slot.effectInput.connect(convolver)
        convolver.connect(slot.effectOutput)
        // Also pass dry
        slot.effectInput.connect(slot.effectOutput)
        break
      }
      case 'distortion': {
        slot.effectDistortion = ctx.createWaveShaper()
        slot.effectDistortion.curve = makeDistortionCurve(200) as Float32Array<ArrayBuffer>
        slot.effectDistortion.oversample = '4x'

        slot.effectInput.connect(slot.effectDistortion)
        slot.effectDistortion.connect(slot.effectOutput)
        break
      }
      default:
        this.removeEffect(trackId)
        return
    }

    slot.currentEffect = effectType

    // Re-route audio through effect if track is playing
    if (this._state === 'playing') {
      this.rerouteEffect(trackId, slot)
    }
  }

  private rerouteEffect(_trackId: string, slot: TrackSlot): void {
    if (!slot.effectInput || !slot.effectOutput) return

    // Disconnect slotGain from crossfadeGain
    slot.slotGain.disconnect()
    slot.slotGain.connect(slot.effectInput)
    slot.effectOutput.connect(slot.crossfadeGain)
  }

  removeEffect(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot) return

    // Reconnect direct path
    slot.slotGain.disconnect()
    slot.slotGain.connect(slot.crossfadeGain)

    // Cleanup
    try { slot.effectInput?.disconnect() } catch {}
    try { slot.effectOutput?.disconnect() } catch {}
    slot.effectInput = undefined
    slot.effectOutput = undefined
    slot.effectDelay = undefined
    slot.effectFeedback = undefined
    slot.effectFilter = undefined
    slot.effectDistortion = undefined
    slot.currentEffect = 'none'
  }

  // --- Filter sweep (legacy compat) ---

  applyFilterSweep(trackId: string, progress: number): void {
    const slot = this.tracks.get(trackId)
    if (!slot?.eqLow || !this.ctx) return

    const { hpStartHz, hpEndHz } = this._transitionConfig
    const cutoff = hpStartHz + (hpEndHz - hpStartHz) * progress
    slot.eqLow.frequency.setValueAtTime(cutoff, this.ctx.currentTime)
  }

  removeFilter(trackId: string): void {
    const slot = this.tracks.get(trackId)
    if (!slot || !this.ctx) return
    slot.eqLow.frequency.setValueAtTime(320, this.ctx.currentTime)
  }

  // --- Transition config ---

  setTransition(config: Partial<TransitionConfig>): void {
    Object.assign(this._transitionConfig, config)
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
      const anyPlaying = Array.from(this.tracks.values()).some((s) => s.isPlaying)
      if (!anyPlaying) return
      const data = this.getAnalyserData()
      if (data) this.events.onAnalyser?.(data)
      this.events.onTimeUpdate?.(this.currentTime, this._duration)
      this._rafId = requestAnimationFrame(loop)
    }
    this._rafId = requestAnimationFrame(loop)
  }

  // --- Level metering ---

  getTrackLevel(trackId: string): number {
    const slot = this.tracks.get(trackId)
    if (!slot || !slot.levelAnalyser) return 0

    const data = new Float32Array(slot.levelAnalyser.frequencyBinCount)
    slot.levelAnalyser.getFloatTimeDomainData(data)

    // RMS
    let sum = 0
    for (let i = 0; i < data.length; i++) {
      sum += data[i] * data[i]
    }
    return Math.sqrt(sum / data.length)
  }

  private startLevelLoop() {
    const loop = () => {
      const anyPlaying = Array.from(this.tracks.values()).some((s) => s.isPlaying)
      if (!anyPlaying) return
      const levels = new Map<string, number>()
      for (const [id] of this.tracks) {
        levels.set(id, this.getTrackLevel(id))
      }
      this.events.onLevels?.(levels)
      this._levelRafId = requestAnimationFrame(loop)
    }
    this._levelRafId = requestAnimationFrame(loop)
  }

  // --- Multi-track scheduling ---

  async loadSet(tracks: Array<{ id: string; name: string }>): Promise<void> {
    const loadPromises = tracks.map((t) => this.loadTrack(t.id, t.name))
    await Promise.all(loadPromises)

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
