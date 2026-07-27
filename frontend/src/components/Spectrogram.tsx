/* ============================================================
   DARAVE — Spectrogram Component
   Fetches audio, renders FFT-based spectrogram to canvas.
   ============================================================ */
import { useRef, useEffect, useState } from 'react'

interface SpectrogramProps {
  src: string
  height?: number
}

const COLORS = [
  '#0d1117', '#0e1f28', '#0f2d39', '#103b4a',
  '#11495b', '#12576c', '#13657d', '#14738e',
  '#15819f', '#1690b0', '#179ec1', '#18acd2',
  '#19bae3', '#1ac8f4', '#40d0f7', '#80e0ff',
  '#a0f0ff', '#c0ffff', '#ffe040', '#ffc020',
  '#ffa000', '#ff8000', '#ff6000', '#ff4000',
  '#ff2000', '#ff0000',
]

export function Spectrogram({ src, height = 120 }: SpectrogramProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const canvas = canvasRef.current
    if (!canvas || !src) return

    async function render() {
      try {
        const res = await fetch(src)
        if (!res.ok || cancelled) return
        const arrayBuf = await res.arrayBuffer()
        if (cancelled) return

        const audioCtx = new OfflineAudioContext(1, 1, 44100)
        const audioBuffer = await audioCtx.decodeAudioData(arrayBuf)
        if (cancelled) return

        const data = audioBuffer.getChannelData(0)
        const fftSize = 512
        const hop = 256
        const nBins = fftSize / 2
        const nFrames = Math.floor((data.length - fftSize) / hop)
        if (!canvas) return
        const w = canvas.width
        const h = canvas.height

        const ctx2d = canvas.getContext('2d')
        if (!ctx2d) return

        // Simple DFT magnitude via brute-force (no FFT lib needed)
        const colStep = Math.max(1, Math.floor(nFrames / w))
        for (let col = 0; col < w; col++) {
          const frameIdx = Math.min(col * colStep, nFrames - 1)
          const offset = frameIdx * hop
          const magnitudes = new Float32Array(nBins)

          for (let bin = 0; bin < nBins; bin++) {
            let re = 0, im = 0
            for (let n = 0; n < fftSize; n++) {
              const sample = data[offset + n] || 0
              const angle = (2 * Math.PI * bin * n) / fftSize
              re += sample * Math.cos(angle)
              im -= sample * Math.sin(angle)
            }
            magnitudes[bin] = Math.sqrt(re * re + im * im) / fftSize
          }

          // Draw column
          const binHeight = h / nBins
          for (let bin = 0; bin < nBins; bin++) {
            const v = Math.min(1, magnitudes[bin] * 8)
            const colorIdx = Math.min(COLORS.length - 1, Math.floor(v * (COLORS.length - 1)))
            ctx2d.fillStyle = COLORS[colorIdx]
            ctx2d.fillRect(col, h - (bin + 1) * binHeight, 1, Math.ceil(binHeight) + 1)
          }
        }
        if (!cancelled) setLoading(false)
      } catch {
        if (!cancelled) setLoading(false)
      }
    }

    render()
    return () => { cancelled = true }
  }, [src, height])

  return (
    <div className="spectrogram" style={{ position: 'relative' }}>
      <canvas
        ref={canvasRef}
        width={512}
        height={height}
        style={{ width: '100%', height: `${height}px`, borderRadius: 'var(--radius-md)', background: '#000' }}
      />
      {loading && <span style={{ position: 'absolute', top: 4, right: 8, fontSize: 10, color: 'var(--color-muted)' }}>…</span>}
    </div>
  )
}
