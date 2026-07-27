import { Play, Pause, Square, Headphones, Volume2, Loader2 } from 'lucide-react'
import './TransportControls.css'

interface TransportControlsProps {
  state: 'idle' | 'loading' | 'playing' | 'paused'
  currentTime: number
  duration: number
  deckColor: string
  onPlay: () => void
  onPause: () => void
  onStop: () => void
  onSeek: (time: number) => void
  onCue?: () => void
  volume: number
  onVolumeChange: (v: number) => void
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export default function TransportControls({
  state,
  currentTime,
  duration,
  deckColor,
  onPlay,
  onPause,
  onStop,
  onSeek,
  onCue,
  volume,
  onVolumeChange,
}: TransportControlsProps) {
  const isPlaying = state === 'playing'
  const isLoading = state === 'loading'
  const isIdle = state === 'idle'
  const progress = duration > 0 ? (currentTime / duration) * 100 : 0

  function handleProgressClick(e: React.MouseEvent<HTMLDivElement>) {
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = (e.clientX - rect.left) / rect.width
    onSeek(Math.max(0, Math.min(duration, ratio * duration)))
  }

  return (
    <div className="transport" style={{ '--deck-color': deckColor } as React.CSSProperties}>
      <button
        className="transport__btn"
        onClick={onCue}
        disabled={!onCue || isIdle}
        title="Cue"
      >
        <Headphones size={14} />
      </button>

      <button
        className="transport__btn"
        onClick={onStop}
        disabled={isIdle}
        title="Stop"
      >
        <Square size={14} />
      </button>

      <button
        className="transport__btn transport__btn--play"
        onClick={isPlaying ? onPause : onPlay}
        disabled={isIdle || isLoading}
        title={isPlaying ? 'Pause' : 'Play'}
      >
        {isLoading ? (
          <Loader2 size={16} className="spin" />
        ) : isPlaying ? (
          <Pause size={16} />
        ) : (
          <Play size={16} />
        )}
      </button>

      <span className="transport__time">
        {formatTime(currentTime)} / {formatTime(duration)}
      </span>

      <div className="transport__progress" onClick={handleProgressClick}>
        <div
          className="transport__progress-fill"
          style={{ width: `${Math.min(100, progress)}%` }}
        />
      </div>

      <div className="transport__volume">
        <Volume2 size={14} className="transport__volume-icon" />
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={volume}
          onChange={(e) => onVolumeChange(parseFloat(e.target.value))}
        />
      </div>
    </div>
  )
}
