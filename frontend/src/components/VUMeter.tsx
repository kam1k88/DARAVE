import { useEffect, useRef, useState } from 'react'
import './VUMeter.css'

interface VUMeterProps {
  level: number
  peak?: number
  color?: string
  height?: number
  width?: number
  label?: string
}

const SEGMENTS = 16
const GAP = 2

export default function VUMeter({
  level,
  peak,
  color: _color,
  height = 120,
  width = 12,
  label,
}: VUMeterProps) {
  const [peakLevel, setPeakLevel] = useState(peak ?? 0)
  const decayRef = useRef<number | null>(null)

  useEffect(() => {
    if (peak !== undefined) {
      setPeakLevel(peak)
      return
    }

    if (level > peakLevel) {
      setPeakLevel(level)
    }

    if (decayRef.current !== null) cancelAnimationFrame(decayRef.current)

    let last = performance.now()
    const decay = (now: number) => {
      const dt = (now - last) / 1000
      last = now
      setPeakLevel((prev) => {
        const next = prev - dt * 0.8
        if (next <= 0) return 0
        decayRef.current = requestAnimationFrame(decay)
        return next
      })
    }
    decayRef.current = requestAnimationFrame(decay)

    return () => {
      if (decayRef.current !== null) cancelAnimationFrame(decayRef.current)
    }
  }, [level, peak])

  const segmentHeight = (height - GAP * (SEGMENTS - 1)) / SEGMENTS
  const litCount = Math.round(level * SEGMENTS)
  const peakIndex = Math.round(peakLevel * SEGMENTS)

  const segmentColor = (index: number): string => {
    if (index < 10) return 'green'
    if (index < 14) return 'amber'
    return 'red'
  }

  return (
    <div className="vu-meter" style={{ width }}>
      <div className="vu-meter__bar" style={{ width, height }}>
        {Array.from({ length: SEGMENTS }, (_, i) => {
          const segIdx = SEGMENTS - 1 - i
          const lit = segIdx < litCount
          return (
            <div
              key={i}
              className={`vu-meter__segment vu-meter__segment--${segmentColor(segIdx)}${lit ? ' vu-meter__segment--lit' : ''}`}
              style={{
                top: i * (segmentHeight + GAP),
                height: segmentHeight,
                background: lit
                  ? undefined
                  : 'var(--color-bg-elevated, #18181b)',
              }}
            />
          )
        })}
        {peakLevel > 0 && (
          <div
            className="vu-meter__peak"
            style={{
              top: (SEGMENTS - 1 - peakIndex) * (segmentHeight + GAP),
            }}
          />
        )}
      </div>
      {label && <span className="vu-meter__label">{label}</span>}
    </div>
  )
}
