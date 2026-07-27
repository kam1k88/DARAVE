import { useCallback, useRef, useState } from 'react'
import './EQKnobs.css'

interface Band {
  id: 'low' | 'mid' | 'high'
  label: string
  freq: string
}

const BANDS: Band[] = [
  { id: 'low', label: 'LOW', freq: '~320 Hz' },
  { id: 'mid', label: 'MID', freq: '~1 kHz' },
  { id: 'high', label: 'HIGH', freq: '~3.2 kHz' },
]

const MIN_DB = -12
const MAX_DB = 12
const MIN_DEG = -135
const MAX_DEG = 135

function dbToDeg(db: number): number {
  const t = (db - MIN_DB) / (MAX_DB - MIN_DB)
  return MIN_DEG + t * (MAX_DEG - MIN_DEG)
}

function formatDB(db: number): string {
  if (db === 0) return '0 dB'
  return `${db > 0 ? '+' : ''}${db} dB`
}

interface EQKnobsProps {
  trackId: string
  deckColor: string
  onEQChange: (band: 'low' | 'mid' | 'high', dB: number) => void
  onKill: (band: 'low' | 'mid' | 'high', enabled: boolean) => void
}

export default function EQKnobs({ trackId, deckColor, onEQChange, onKill }: EQKnobsProps) {
  const [values, setValues] = useState<Record<string, number>>({
    low: 0,
    mid: 0,
    high: 0,
  })
  const [killed, setKilled] = useState<Record<string, boolean>>({
    low: false,
    mid: false,
    high: false,
  })

  const dragRef = useRef<{
    bandId: string
    startY: number
    startDb: number
  } | null>(null)

  const updateBand = useCallback(
    (bandId: 'low' | 'mid' | 'high', db: number) => {
      const clamped = Math.round(Math.max(MIN_DB, Math.min(MAX_DB, db)))
      setValues((prev) => ({ ...prev, [bandId]: clamped }))
      onEQChange(bandId, clamped)
    },
    [onEQChange],
  )

  const handleKill = useCallback(
    (bandId: 'low' | 'mid' | 'high') => {
      setKilled((prev) => {
        const next = !prev[bandId]
        if (next) {
          setValues((v) => ({ ...v, [bandId]: MIN_DB }))
          onEQChange(bandId, MIN_DB)
        } else {
          setValues((v) => ({ ...v, [bandId]: 0 }))
          onEQChange(bandId, 0)
        }
        onKill(bandId, next)
        return { ...prev, [bandId]: next }
      })
    },
    [onEQChange, onKill],
  )

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>, bandId: string) => {
      e.preventDefault()
      const current = killed[bandId] ? MIN_DB : values[bandId]
      dragRef.current = { bandId, startY: e.clientY, startDb: current }
      ;(e.target as HTMLElement).setPointerCapture(e.pointerId)

      const onMove = (ev: PointerEvent) => {
        if (!dragRef.current) return
        const dy = dragRef.current.startY - ev.clientY
        const dbDelta = Math.round(dy / 2)
        const newDb = dragRef.current.startDb + dbDelta
        updateBand(dragRef.current.bandId as 'low' | 'mid' | 'high', newDb)
      }

      const onUp = () => {
        dragRef.current = null
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
      }

      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
    },
    [killed, values, updateBand],
  )

  return (
    <div className="eq-knobs">
      {BANDS.map((band) => {
        const db = killed[band.id] ? MIN_DB : values[band.id]
        const deg = dbToDeg(db)
        const isActive = killed[band.id]

        return (
          <div key={`${trackId}-${band.id}`} className="eq-knobs__band">
            <div
              className="eq-knobs__knob"
              style={
                {
                  transform: `rotate(${deg}deg)`,
                  borderColor: isActive ? 'var(--color-crimson-500)' : undefined,
                  '--knob-accent': deckColor,
                } as React.CSSProperties
              }
              onPointerDown={(e) => handlePointerDown(e, band.id)}
              role="slider"
              aria-label={`${band.label} EQ`}
              aria-valuemin={MIN_DB}
              aria-valuemax={MAX_DB}
              aria-valuenow={db}
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
                  e.preventDefault()
                  updateBand(band.id, (killed[band.id] ? MIN_DB : values[band.id]) + 1)
                } else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
                  e.preventDefault()
                  updateBand(band.id, (killed[band.id] ? MIN_DB : values[band.id]) - 1)
                }
              }}
            >
              <input
                type="range"
                min={MIN_DB}
                max={MAX_DB}
                step={1}
                value={db}
                aria-label={`${band.label} dB`}
                style={{
                  position: 'absolute',
                  width: '100%',
                  height: '100%',
                  opacity: 0,
                  cursor: 'inherit',
                  margin: 0,
                }}
                onChange={(e) => updateBand(band.id, Number(e.target.value))}
              />
            </div>
            <span className="eq-knobs__value">{formatDB(db)}</span>
            <span className="eq-knobs__label">{band.label}</span>
            <button
              className={`eq-knobs__kill${isActive ? ' eq-knobs__kill--active' : ''}`}
              onClick={() => handleKill(band.id)}
              aria-label={`Kill ${band.label}`}
              aria-pressed={isActive}
              title={`${isActive ? 'Restore' : 'Kill'} ${band.label}`}
            >
              ✕
            </button>
          </div>
        )
      })}
    </div>
  )
}
