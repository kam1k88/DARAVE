import React, { useRef, useEffect, useCallback } from 'react'

interface TurntableProps {
  bpm: number
  isPlaying: boolean
  currentTime: number
  duration: number
  trackName: string
  deckColor: string
  label: 'A' | 'B'
  onSeek?: (time: number) => void
}

const Turntable: React.FC<TurntableProps> = ({
  bpm,
  isPlaying,
  currentTime,
  duration,
  trackName,
  deckColor,
  label,
  onSeek,
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const animRef = useRef<number>(0)
  const angleRef = useRef(0)
  const lastTimeRef = useRef(0)

  const rpm = bpm || 120
  const degreesPerSecond = (rpm / 60) * 360

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = canvas.width
    const h = canvas.height
    const cx = w / 2
    const cy = h / 2
    const radius = Math.min(cx, cy) - 8

    const now = performance.now() / 1000
    const dt = lastTimeRef.current ? now - lastTimeRef.current : 0
    lastTimeRef.current = now

    if (isPlaying) {
      angleRef.current += degreesPerSecond * dt
    }

    ctx.clearRect(0, 0, w, h)

    // Outer ring glow
    ctx.beginPath()
    ctx.arc(cx, cy, radius + 4, 0, Math.PI * 2)
    ctx.strokeStyle = deckColor + '40'
    ctx.lineWidth = 3
    ctx.stroke()

    // Vinyl background
    const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius)
    grad.addColorStop(0, '#1a1a2e')
    grad.addColorStop(0.3, '#16213e')
    grad.addColorStop(0.7, '#0f0f23')
    grad.addColorStop(1, '#0a0a15')
    ctx.beginPath()
    ctx.arc(cx, cy, radius, 0, Math.PI * 2)
    ctx.fillStyle = grad
    ctx.fill()

    // Grooves
    ctx.save()
    ctx.translate(cx, cy)
    ctx.rotate((angleRef.current * Math.PI) / 180)

    const grooveCount = 18
    for (let i = 0; i < grooveCount; i++) {
      const r = radius * 0.35 + (radius * 0.55 * i) / grooveCount
      ctx.beginPath()
      ctx.arc(0, 0, r, 0, Math.PI * 2)
      ctx.strokeStyle = `rgba(255,255,255,${0.03 + (i % 3 === 0 ? 0.02 : 0)})`
      ctx.lineWidth = 0.5
      ctx.stroke()
    }

    // Label area (center circle)
    const labelRadius = radius * 0.28
    const labelGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, labelRadius)
    labelGrad.addColorStop(0, deckColor)
    labelGrad.addColorStop(1, deckColor + '80')
    ctx.beginPath()
    ctx.arc(0, 0, labelRadius, 0, Math.PI * 2)
    ctx.fillStyle = labelGrad
    ctx.fill()

    // Label text
    ctx.fillStyle = '#fff'
    ctx.font = `bold ${Math.max(10, radius * 0.12)}px system-ui`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(label, 0, -radius * 0.06)

    ctx.font = `${Math.max(7, radius * 0.07)}px system-ui`
    ctx.fillStyle = 'rgba(255,255,255,0.7)'
    ctx.fillText(`${Math.round(bpm)} BPM`, 0, radius * 0.08)

    // Spindle hole
    ctx.beginPath()
    ctx.arc(0, 0, 3, 0, Math.PI * 2)
    ctx.fillStyle = '#0a0a15'
    ctx.fill()

    // Playhead indicator (small dot on outer edge)
    ctx.beginPath()
    ctx.arc(radius * 0.85, 0, 3, 0, Math.PI * 2)
    ctx.fillStyle = deckColor
    ctx.fill()

    ctx.restore()

    // Progress arc
    if (duration > 0) {
      const progress = currentTime / duration
      const startAngle = -Math.PI / 2
      const endAngle = startAngle + progress * Math.PI * 2
      ctx.beginPath()
      ctx.arc(cx, cy, radius + 2, startAngle, endAngle)
      ctx.strokeStyle = deckColor
      ctx.lineWidth = 3
      ctx.lineCap = 'round'
      ctx.stroke()
    }

    // Time display
    const formatTime = (t: number) => {
      const m = Math.floor(t / 60)
      const s = Math.floor(t % 60)
      return `${m}:${s.toString().padStart(2, '0')}`
    }
    ctx.fillStyle = '#fff'
    ctx.font = `bold 11px system-ui`
    ctx.textAlign = 'center'
    ctx.fillText(
      `${formatTime(currentTime)} / ${formatTime(duration)}`,
      cx,
      cy + radius + 16
    )

    animRef.current = requestAnimationFrame(draw)
  }, [bpm, isPlaying, currentTime, duration, deckColor, label, degreesPerSecond])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const resize = () => {
      const parent = canvas.parentElement
      if (!parent) return
      const size = Math.min(parent.clientWidth * 0.4, 140)
      canvas.width = size * 2
      canvas.height = size * 2
      canvas.style.width = `${size}px`
      canvas.style.height = `${size}px`
    }
    resize()
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])

  useEffect(() => {
    lastTimeRef.current = 0
    animRef.current = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(animRef.current)
  }, [draw])

  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (!onSeek || !canvasRef.current || duration <= 0) return
    const rect = canvasRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left - rect.width / 2
    const y = e.clientY - rect.top - rect.height / 2
    const angle = Math.atan2(y, x)
    const normalized = (angle + Math.PI) / (2 * Math.PI)
    onSeek(normalized * duration)
  }

  return (
    <div className="tt-container">
      <canvas
        ref={canvasRef}
        className="tt-canvas"
        onClick={handleClick}
        style={{ cursor: onSeek ? 'pointer' : 'default' }}
      />
      <div className="tt-track-name" style={{ color: deckColor }}>
        {trackName || 'No track loaded'}
      </div>
    </div>
  )
}

export default Turntable
