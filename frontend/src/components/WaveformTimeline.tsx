import React, { useRef, useEffect, useCallback, useState } from 'react'
import './WaveformTimeline.css'

interface CuePoint {
  id: number
  time: number
  color: string
  label?: string
}

interface WaveformTimelineProps {
  waveformData: Float32Array | null
  frequencyData: Float32Array | null
  currentTime: number
  duration: number
  bpm: number
  deckColor: string
  cuePoints?: CuePoint[]
  onSeek?: (time: number) => void
  onCuePointAdd?: (time: number) => void
  sections?: { type: string; start: number; end: number }[]
}

const SECTION_COLORS: Record<string, string> = {
  intro: '#4ade80',
  verse: '#60a5fa',
  chorus: '#f59e0b',
  drop: '#ef4444',
  breakdown: '#a78bfa',
  build: '#fb923c',
  outro: '#94a3b8',
}

const WaveformTimeline: React.FC<WaveformTimelineProps> = ({
  waveformData,
  currentTime,
  duration,
  bpm,
  deckColor,
  cuePoints = [],
  onSeek,
  onCuePointAdd,
  sections = [],
}) => {
  const containerRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [zoom, setZoom] = useState(1)
  const [scrollX, setScrollX] = useState(0)
  const isDragging = useRef(false)

  const pixelsPerSecond = 80 * zoom

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    const container = containerRef.current
    if (!canvas || !container) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = container.clientWidth
    const h = container.clientHeight || 70
    canvas.width = w * 2
    canvas.height = h * 2
    canvas.style.width = `${w}px`
    canvas.style.height = `${h}px`
    ctx.scale(2, 2)

    ctx.fillStyle = '#0a0a15'
    ctx.fillRect(0, 0, w, h)

    const totalWidth = duration * pixelsPerSecond
    const startSample = Math.floor((scrollX / totalWidth) * (waveformData?.length || 0))
    const samplesPerPixel = Math.floor((waveformData?.length || 1) / totalWidth)

    // Draw sections background
    sections.forEach((sec) => {
      const x1 = sec.start * pixelsPerSecond - scrollX
      const x2 = sec.end * pixelsPerSecond - scrollX
      if (x2 < 0 || x1 > w) return
      ctx.fillStyle = (SECTION_COLORS[sec.type] || '#666') + '15'
      ctx.fillRect(Math.max(0, x1), 0, Math.min(x2, w) - Math.max(0, x1), h)

      // Section label
      if (x2 - x1 > 30) {
        ctx.fillStyle = (SECTION_COLORS[sec.type] || '#666') + '60'
        ctx.font = '9px system-ui'
        ctx.textAlign = 'center'
        ctx.fillText(sec.type.toUpperCase(), (x1 + x2) / 2, 12)
      }
    })

    // Draw waveform
    if (waveformData && waveformData.length > 0) {
      const mid = h / 2
      ctx.beginPath()
      ctx.moveTo(0, mid)

      for (let x = 0; x < w; x++) {
        const sampleIndex = startSample + x * samplesPerPixel
        if (sampleIndex >= waveformData.length) break

        let min = 0
        let max = 0
        const range = Math.max(1, samplesPerPixel)
        for (let j = 0; j < range; j++) {
          const idx = sampleIndex + j
          if (idx < waveformData.length) {
            const val = waveformData[idx]
            if (val < min) min = val
            if (val > max) max = val
          }
        }

        const yMin = mid + min * mid * 0.9
        const yMax = mid + max * mid * 0.9

        ctx.lineTo(x, yMax)
        ctx.lineTo(x, yMin)
      }

      ctx.lineTo(w, mid)
      ctx.strokeStyle = deckColor + 'cc'
      ctx.lineWidth = 1
      ctx.fill()

      // Mirrored bottom half
      ctx.beginPath()
      ctx.moveTo(0, mid)
      for (let x = 0; x < w; x++) {
        const sampleIndex = startSample + x * samplesPerPixel
        if (sampleIndex >= waveformData.length) break

        let min = 0
        let max = 0
        const range = Math.max(1, samplesPerPixel)
        for (let j = 0; j < range; j++) {
          const idx = sampleIndex + j
          if (idx < waveformData.length) {
            const val = waveformData[idx]
            if (val < min) min = val
            if (val > max) max = val
          }
        }

        const yMin = mid - min * mid * 0.9
        const yMax = mid - max * mid * 0.9

        ctx.lineTo(x, yMax)
        ctx.lineTo(x, yMin)
      }
      ctx.lineTo(w, mid)
      ctx.strokeStyle = deckColor + '88'
      ctx.lineWidth = 1
      ctx.fill()
    }

    // Time grid
    const gridInterval = zoom > 2 ? 1 : zoom > 1 ? 2 : 5
    ctx.strokeStyle = 'rgba(255,255,255,0.06)'
    ctx.lineWidth = 1
    ctx.font = '9px system-ui'
    ctx.fillStyle = 'rgba(255,255,255,0.3)'
    ctx.textAlign = 'center'

    for (let t = 0; t <= duration; t += gridInterval) {
      const x = t * pixelsPerSecond - scrollX
      if (x < -10 || x > w + 10) continue
      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x, h)
      ctx.stroke()
      ctx.fillText(`${Math.floor(t / 60)}:${(t % 60).toString().padStart(2, '0')}`, x, h - 4)
    }

    // Beat grid
    if (bpm > 0) {
      const beatInterval = 60 / bpm
      ctx.strokeStyle = 'rgba(255,255,255,0.04)'
      for (let t = 0; t <= duration; t += beatInterval) {
        const x = t * pixelsPerSecond - scrollX
        if (x < 0 || x > w) continue
        ctx.beginPath()
        ctx.moveTo(x, 0)
        ctx.lineTo(x, h)
        ctx.stroke()
      }
    }

    // Cue points
    cuePoints.forEach((cp) => {
      const x = cp.time * pixelsPerSecond - scrollX
      if (x < -5 || x > w + 5) return

      ctx.beginPath()
      ctx.moveTo(x, 0)
      ctx.lineTo(x, h)
      ctx.strokeStyle = cp.color
      ctx.lineWidth = 2
      ctx.stroke()

      // Triangle marker
      ctx.beginPath()
      ctx.moveTo(x - 5, 0)
      ctx.lineTo(x + 5, 0)
      ctx.lineTo(x, 8)
      ctx.closePath()
      ctx.fillStyle = cp.color
      ctx.fill()

      if (cp.label) {
        ctx.fillStyle = cp.color
        ctx.font = 'bold 8px system-ui'
        ctx.textAlign = 'center'
        ctx.fillText(cp.label, x, 18)
      }
    })

    // Playhead
    const playX = currentTime * pixelsPerSecond - scrollX
    ctx.beginPath()
    ctx.moveTo(playX, 0)
    ctx.lineTo(playX, h)
    ctx.strokeStyle = '#ffffff'
    ctx.lineWidth = 2
    ctx.stroke()

    // Playhead triangle
    ctx.beginPath()
    ctx.moveTo(playX - 6, 0)
    ctx.lineTo(playX + 6, 0)
    ctx.lineTo(playX, 10)
    ctx.closePath()
    ctx.fillStyle = '#ffffff'
    ctx.fill()
  }, [waveformData, currentTime, duration, bpm, deckColor, cuePoints, zoom, scrollX, pixelsPerSecond, sections])

  useEffect(() => {
    draw()
  }, [draw])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault()
      if (e.ctrlKey || e.metaKey) {
        setZoom((z) => Math.max(0.5, Math.min(8, z + (e.deltaY > 0 ? -0.2 : 0.2))))
      } else {
        setScrollX((s) => Math.max(0, Math.min(s + e.deltaY * 2, duration * pixelsPerSecond - container.clientWidth)))
      }
    }
    container.addEventListener('wheel', handleWheel, { passive: false })
    return () => container.removeEventListener('wheel', handleWheel)
  }, [duration, pixelsPerSecond])

  const handleMouseDown = (e: React.MouseEvent) => {
    isDragging.current = true
    seekTo(e)
  }

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isDragging.current) return
    seekTo(e)
  }

  const handleMouseUp = () => {
    isDragging.current = false
  }

  const seekTo = (e: React.MouseEvent) => {
    if (!containerRef.current || !onSeek || duration <= 0) return
    const rect = containerRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left + scrollX
    const time = Math.max(0, Math.min(x / pixelsPerSecond, duration))
    onSeek(time)
  }

  const handleDoubleClick = (e: React.MouseEvent) => {
    if (!onCuePointAdd || !containerRef.current || duration <= 0) return
    const rect = containerRef.current.getBoundingClientRect()
    const x = e.clientX - rect.left + scrollX
    const time = Math.max(0, Math.min(x / pixelsPerSecond, duration))
    onCuePointAdd(time)
  }

  return (
    <div className="wt-container">
      <div className="wt-controls">
        <button
          className="wt-zoom-btn"
          onClick={() => setZoom((z) => Math.max(0.5, z - 0.5))}
          title="Zoom out"
        >
          −
        </button>
        <span className="wt-zoom-label">{Math.round(zoom * 100)}%</span>
        <button
          className="wt-zoom-btn"
          onClick={() => setZoom((z) => Math.min(8, z + 0.5))}
          title="Zoom in"
        >
          +
        </button>
        <button
          className="wt-zoom-btn"
          onClick={() => {
            setZoom(1)
            setScrollX(0)
          }}
          title="Reset"
        >
          Fit
        </button>
      </div>
      <div
        ref={containerRef}
        className="wt-canvas-wrap"
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={handleMouseUp}
        onDoubleClick={handleDoubleClick}
      >
        <canvas ref={canvasRef} />
      </div>
    </div>
  )
}

export default WaveformTimeline
