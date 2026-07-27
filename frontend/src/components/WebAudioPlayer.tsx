/* ============================================================
   WebAudioPlayer — Real-time stem mixer component.

   Loads stems from the server and mixes them in the browser
   using the Web Audio API. No server-side rendering needed.

   Props:
     tracks — array of { id, name } to load and mix
     autoPlay — start playback immediately after loading
   ============================================================ */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Play, Pause, Square, Volume2, Loader2 } from 'lucide-react'
import { useWebAudio } from '@/hooks/useWebAudio'
import './WebAudioPlayer.css'

interface WebAudioPlayerProps {
  tracks: Array<{ id: string; name: string }>
  autoPlay?: boolean
}

function formatTime(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

function Visualizer({
  frequency,
  waveform,
}: {
  frequency: Uint8Array | null
  waveform: Float32Array | null
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    canvas.width = rect.width * dpr
    canvas.height = rect.height * dpr
    ctx.scale(dpr, dpr)

    const w = rect.width
    const h = rect.height

    ctx.clearRect(0, 0, w, h)

    if (frequency && frequency.length > 0) {
      // Draw frequency bars
      const barCount = 64
      const barWidth = w / barCount
      const step = Math.floor(frequency.length / barCount)

      for (let i = 0; i < barCount; i++) {
        const val = frequency[i * step] / 255
        const barH = val * h * 0.9

        // Gradient from amber to red based on intensity
        const r = Math.floor(245 - val * 50)
        const g = Math.floor(158 - val * 100)
        const b = Math.floor(11 + val * 50)
        ctx.fillStyle = `rgb(${r},${g},${b})`

        ctx.fillRect(
          i * barWidth + 1,
          h - barH,
          barWidth - 2,
          barH,
        )
      }
    } else if (waveform && waveform.length > 0) {
      // Draw waveform
      ctx.strokeStyle = 'rgba(245, 158, 11, 0.6)'
      ctx.lineWidth = 1.5
      ctx.beginPath()
      const sliceWidth = w / waveform.length
      for (let i = 0; i < waveform.length; i++) {
        const x = i * sliceWidth
        const y = (1 + waveform[i]) * h / 2
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
    }
  }, [frequency, waveform])

  return <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
}

export function WebAudioPlayer({ tracks, autoPlay = false }: WebAudioPlayerProps) {
  const audio = useWebAudio()
  const [volume, setVolumeState] = useState(0.8)
  const [loading, setLoading] = useState(false)
  const [loadProgress, setLoadProgress] = useState('')
  const progressRef = useRef<HTMLDivElement>(null)

  // Load tracks on mount
  useEffect(() => {
    if (tracks.length === 0) return

    let cancelled = false

    async function loadAll() {
      setLoading(true)
      setLoadProgress(`Loading 0/${tracks.length}...`)

      await audio.loadSet(tracks)

      if (!cancelled) {
        setLoading(false)
        setLoadProgress('')
        if (autoPlay) {
          // Small delay to let buffers settle
          setTimeout(() => audio.play(), 100)
        }
      }
    }

    loadAll()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracks.map(t => t.id).join(',')])

  const handlePlayPause = useCallback(() => {
    if (audio.state === 'playing') {
      audio.pause()
    } else {
      audio.play()
    }
  }, [audio])

  const handleStop = useCallback(() => {
    audio.stop()
  }, [audio])

  const handleProgressClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (!progressRef.current || audio.duration <= 0) return
      const rect = progressRef.current.getBoundingClientRect()
      const ratio = (e.clientX - rect.left) / rect.width
      audio.seek(ratio * audio.duration)
    },
    [audio],
  )

  const handleVolumeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = parseFloat(e.target.value)
      setVolumeState(v)
      audio.setMasterVolume(v)
    },
    [audio],
  )

  const progress = audio.duration > 0 ? (audio.currentTime / audio.duration) * 100 : 0

  return (
    <div className="web-audio-player">
      {/* Header */}
      <div className="wap-header">
        <span className="wap-header__title">Real-time Mix</span>
        <span className="wap-header__badge">Web Audio</span>
      </div>

      {/* Visualizer */}
      <div className="wap-visualizer">
        <Visualizer frequency={audio.frequency} waveform={audio.waveform} />
      </div>

      {/* Transport */}
      <div className="wap-transport">
        <button
          className="wap-btn"
          onClick={handleStop}
          disabled={audio.state === 'idle'}
          title="Stop"
        >
          <Square size={14} />
        </button>

        <button
          className="wap-btn wap-btn--primary"
          onClick={handlePlayPause}
          disabled={loading || audio.state === 'loading'}
          title={audio.state === 'playing' ? 'Pause' : 'Play'}
        >
          {loading || audio.state === 'loading' ? (
            <Loader2 size={14} className="wap-spinner" />
          ) : audio.state === 'playing' ? (
            <Pause size={14} />
          ) : (
            <Play size={14} />
          )}
        </button>

        {/* Progress bar */}
        <div
          className="wap-progress"
          ref={progressRef}
          onClick={handleProgressClick}
        >
          <div
            className="wap-progress__fill"
            style={{ width: `${progress}%` }}
          />
        </div>

        {/* Time */}
        <span className="wap-time">
          {formatTime(audio.currentTime)} / {formatTime(audio.duration)}
        </span>

        {/* Volume */}
        <div className="wap-volume">
          <Volume2 size={14} style={{ color: 'var(--color-text-muted)' }} />
          <input
            type="range"
            min="0"
            max="1"
            step="0.01"
            value={volume}
            onChange={handleVolumeChange}
          />
        </div>
      </div>

      {/* Loading indicator */}
      {loading && (
        <div className="wap-loading">
          <div className="wap-spinner" />
          <span>{loadProgress || 'Loading stems...'}</span>
        </div>
      )}

      {/* Track list */}
      {tracks.length > 0 && (
        <div className="wap-tracks">
          {tracks.map((t) => (
            <div
              key={t.id}
              className={`wap-track ${audio.isTrackLoaded(t.id) ? 'wap-track--loaded' : ''}`}
            >
              <span className="wap-track__name">{t.name}</span>
              <span
                className={`wap-track__status ${audio.isTrackLoaded(t.id) ? 'wap-track__status--loaded' : ''}`}
              >
                {audio.isTrackLoaded(t.id) ? 'loaded' : 'pending'}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
