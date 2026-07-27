import { useRef, useCallback } from 'react'
import './Crossfader.css'

interface CrossfaderProps {
  value: number
  curve: 'linear' | 'power' | 'exponential'
  onChange: (value: number) => void
  onCurveChange: (curve: 'linear' | 'power' | 'exponential') => void
  deckAColor?: string
  deckBColor?: string
}

const CURVES: { key: 'linear' | 'power' | 'exponential'; label: string }[] = [
  { key: 'linear', label: 'LIN' },
  { key: 'power', label: 'POW' },
  { key: 'exponential', label: 'EXP' },
]

export default function Crossfader({
  value,
  curve,
  onChange,
  onCurveChange,
  deckAColor = '#f59e0b',
  deckBColor = '#3b82f6',
}: CrossfaderProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const dragging = useRef(false)

  const clamp = (v: number) => Math.max(-1, Math.min(1, v))

  const positionFromEvent = useCallback(
    (clientX: number) => {
      const track = trackRef.current
      if (!track) return value
      const rect = track.getBoundingClientRect()
      const ratio = (clientX - rect.left) / rect.width
      return clamp(ratio * 2 - 1)
    },
    [value]
  )

  const handlePointerDown = useCallback(
    (e: React.PointerEvent) => {
      dragging.current = true
      ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
      onChange(positionFromEvent(e.clientX))
    },
    [onChange, positionFromEvent]
  )

  const handlePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragging.current) return
      onChange(positionFromEvent(e.clientX))
    },
    [onChange, positionFromEvent]
  )

  const handlePointerUp = useCallback(() => {
    dragging.current = false
  }, [])

  const thumbPercent = ((value + 1) / 2) * 100

  return (
    <div className="crossfader">
      <div className="crossfader__labels">
        <span style={{ color: deckAColor }}>A</span>
        <span style={{ color: deckBColor }}>B</span>
      </div>

      <div
        ref={trackRef}
        className="crossfader__track"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
      >
        <div
          className="crossfader__fill"
          style={{
            background: `linear-gradient(to right, ${deckAColor}, transparent 50%, ${deckBColor})`,
          }}
        />
        <div
          className="crossfader__thumb"
          style={{ left: `${thumbPercent}%` }}
        />
      </div>

      <div className="crossfader__curves">
        {CURVES.map((c) => (
          <button
            key={c.key}
            className={`crossfader__curve-btn${curve === c.key ? ' crossfader__curve-btn--active' : ''}`}
            onClick={() => onCurveChange(c.key)}
          >
            {c.label}
          </button>
        ))}
      </div>
    </div>
  )
}
