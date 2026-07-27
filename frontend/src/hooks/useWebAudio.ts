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

  loadTrack: (trackId: string, songName: string) => Promise<void>
  loadSet: (tracks: Array<{ id: string; name: string }>) => Promise<void>
  play: () => void
  pause: () => void
  stop: () => void
  seek: (time: number) => void
  setMasterVolume: (v: number) => void
  setTransition: (config: Partial<TransitionConfig>) => void
  applyFilterSweep: (trackId: string, progress: number) => void
  removeFilter: (trackId: string) => void
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

  // Create engine on mount
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
  const setTransition = useCallback((c: Partial<TransitionConfig>) => engineRef.current?.setTransition(c), [])
  const applyFilterSweep = useCallback((trackId: string, progress: number) => engineRef.current?.applyFilterSweep(trackId, progress), [])
  const removeFilter = useCallback((trackId: string) => engineRef.current?.removeFilter(trackId), [])
  const isTrackLoaded = useCallback((trackId: string) => engineRef.current?.isTrackLoaded(trackId) ?? false, [])
  const getWaveformData = useCallback((trackId: string, resolution?: number) => engineRef.current?.getWaveformData(trackId, resolution) ?? null, [])

  return {
    state,
    currentTime,
    duration,
    waveform,
    frequency,
    loadTrack,
    loadSet,
    play,
    pause,
    stop,
    seek,
    setMasterVolume,
    setTransition,
    applyFilterSweep,
    removeFilter,
    isTrackLoaded,
    getWaveformData,
  }
}
