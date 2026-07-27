/* ============================================================
   useWebAudio — React hook wrapping AudioEngine.

   Manages lifecycle (create/destroy), exposes reactive state,
   and provides stable callbacks for UI components.
   ============================================================ */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AudioEngine,
  type PlayState,
  type TransitionConfig,
  type EngineEvents,
} from '@/lib/audioEngine'

interface UseWebAudioReturn {
  state: PlayState
  currentTime: number
  duration: number
  waveform: Float32Array | null
  frequency: Uint8Array | null
  levels: Map<string, number>

  loadTrack: (trackId: string, songName: string) => Promise<void>
  loadSet: (tracks: Array<{ id: string; name: string }>) => Promise<void>
  play: () => void
  pause: () => void
  stop: () => void
  seek: (time: number) => void
  setMasterVolume: (v: number) => void
  setTrackVolume: (trackId: string, v: number) => void
  setTrackMuted: (trackId: string, muted: boolean) => void
  setTrackSolo: (trackId: string, solo: boolean) => void
  setStemVolume: (trackId: string, stem: string, v: number) => void
  setStemMuted: (trackId: string, stem: string, muted: boolean) => void
  setEQ: (trackId: string, band: 'low' | 'mid' | 'high', dB: number) => void
  setEQBandEnabled: (trackId: string, band: 'low' | 'mid' | 'high', enabled: boolean) => void
  setPan: (trackId: string, value: number) => void
  setCrossfade: (value: number) => void
  setCrossfadeType: (type: 'linear' | 'power' | 'exponential') => void
  crossfadeValue: number
  setTransition: (config: Partial<TransitionConfig>) => void
  setEffect: (trackId: string, effectType: string) => void
  removeEffect: (trackId: string) => void
  applyFilterSweep: (trackId: string, progress: number) => void
  removeFilter: (trackId: string) => void
  getTrackLevel: (trackId: string) => number
  isTrackLoaded: (trackId: string) => boolean
  getWaveformData: (trackId: string, resolution?: number) => Float32Array | null
}

export function useWebAudio(events?: EngineEvents): UseWebAudioReturn {
  const engineRef = useRef<AudioEngine | null>(null)

  const [state, setState] = useState<PlayState>('idle')
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(0)
  const [waveform, setWaveform] = useState<Float32Array | null>(null)
  const [frequency, setFrequency] = useState<Uint8Array | null>(null)
  const [levels, setLevels] = useState<Map<string, number>>(new Map())
  const [crossfadeVal, setCrossfadeVal] = useState(0)

  useEffect(() => {
    const engine = new AudioEngine({
      onStateChange: (s) => {
        setState(s)
        events?.onStateChange?.(s)
      },
      onTimeUpdate: (cur, dur) => {
        setCurrentTime(cur)
        setDuration(dur)
        events?.onTimeUpdate?.(cur, dur)
      },
      onAnalyser: (data) => {
        setWaveform(data.waveform)
        setFrequency(data.frequency)
        events?.onAnalyser?.(data)
      },
      onTrackLoaded: (trackId) => {
        events?.onTrackLoaded?.(trackId)
      },
      onError: (msg) => {
        events?.onError?.(msg)
      },
      onLevels: (l) => {
        setLevels(new Map(l))
      },
    })
    engineRef.current = engine

    return () => {
      engine.destroy()
      engineRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadTrack = useCallback(async (trackId: string, songName: string) => {
    await engineRef.current?.loadTrack(trackId, songName)
  }, [])

  const loadSet = useCallback(async (tracks: Array<{ id: string; name: string }>) => {
    await engineRef.current?.loadSet(tracks)
  }, [])

  const play = useCallback(() => engineRef.current?.play(), [])
  const pause = useCallback(() => engineRef.current?.pause(), [])
  const stop = useCallback(() => engineRef.current?.stop(), [])
  const seek = useCallback((t: number) => engineRef.current?.seek(t), [])
  const setMasterVolume = useCallback((v: number) => engineRef.current?.setMasterVolume(v), [])

  const setTrackVolume = useCallback((trackId: string, v: number) => {
    engineRef.current?.setTrackVolume(trackId, v)
  }, [])

  const setTrackMuted = useCallback((trackId: string, muted: boolean) => {
    engineRef.current?.setTrackMuted(trackId, muted)
  }, [])

  const setTrackSolo = useCallback((trackId: string, solo: boolean) => {
    engineRef.current?.setTrackSolo(trackId, solo)
  }, [])

  const setStemVolume = useCallback((trackId: string, stem: string, v: number) => {
    engineRef.current?.setStemVolume(trackId, stem, v)
  }, [])

  const setStemMuted = useCallback((trackId: string, stem: string, muted: boolean) => {
    engineRef.current?.setStemMuted(trackId, stem, muted)
  }, [])

  const setEQ = useCallback((trackId: string, band: 'low' | 'mid' | 'high', dB: number) => {
    engineRef.current?.setEQ(trackId, band, dB)
  }, [])

  const setEQBandEnabled = useCallback((trackId: string, band: 'low' | 'mid' | 'high', enabled: boolean) => {
    engineRef.current?.setEQBandEnabled(trackId, band, enabled)
  }, [])

  const setPan = useCallback((trackId: string, value: number) => {
    engineRef.current?.setPan(trackId, value)
  }, [])

  const setCrossfade = useCallback((value: number) => {
    engineRef.current?.setCrossfade(value)
    setCrossfadeVal(value)
  }, [])

  const setCrossfadeType = useCallback((type: 'linear' | 'power' | 'exponential') => {
    engineRef.current?.setCrossfadeType(type)
  }, [])

  const setTransition = useCallback((c: Partial<TransitionConfig>) => engineRef.current?.setTransition(c), [])
  const setEffect = useCallback((trackId: string, effectType: string) => {
    engineRef.current?.setEffect(trackId, effectType)
  }, [])
  const removeEffect = useCallback((trackId: string) => {
    engineRef.current?.removeEffect(trackId)
  }, [])

  const applyFilterSweep = useCallback((trackId: string, progress: number) => engineRef.current?.applyFilterSweep(trackId, progress), [])
  const removeFilter = useCallback((trackId: string) => engineRef.current?.removeFilter(trackId), [])
  const getTrackLevel = useCallback((trackId: string) => engineRef.current?.getTrackLevel(trackId) ?? 0, [])
  const isTrackLoaded = useCallback((trackId: string) => engineRef.current?.isTrackLoaded(trackId) ?? false, [])
  const getWaveformData = useCallback((trackId: string, resolution?: number) => engineRef.current?.getWaveformData(trackId, resolution) ?? null, [])

  return {
    state,
    currentTime,
    duration,
    waveform,
    frequency,
    levels,
    loadTrack,
    loadSet,
    play,
    pause,
    stop,
    seek,
    setMasterVolume,
    setTrackVolume,
    setTrackMuted,
    setTrackSolo,
    setStemVolume,
    setStemMuted,
    setEQ,
    setEQBandEnabled,
    setPan,
    setCrossfade,
    setCrossfadeType,
    crossfadeValue: crossfadeVal,
    setTransition,
    setEffect,
    removeEffect,
    applyFilterSweep,
    removeFilter,
    getTrackLevel,
    isTrackLoaded,
    getWaveformData,
  }
}
