import React, { useState, useRef, useCallback } from 'react'

interface SamplePad {
  id: number
  label: string
  color: string
  audioBuffer: AudioBuffer | null
  fileName: string | null
  isPlaying: boolean
  volume: number
}

interface SamplerProps {
  audioContext: AudioContext | null
}

const PAD_COLORS = [
  '#ef4444', '#f97316', '#eab308', '#22c55e',
  '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899',
]

const Sampler: React.FC<SamplerProps> = ({ audioContext }) => {
  const [pads, setPads] = useState<SamplePad[]>(
    Array.from({ length: 8 }, (_, i) => ({
      id: i,
      label: `S${i + 1}`,
      color: PAD_COLORS[i],
      audioBuffer: null,
      fileName: null,
      isPlaying: false,
      volume: 0.8,
    }))
  )
  const sourceRefs = useRef<Map<number, AudioBufferSourceNode>>(new Map())
  const gainRefs = useRef<Map<number, GainNode>>(new Map())

  const handleFileDrop = useCallback(
    (padId: number, file: File) => {
      if (!audioContext) return
      const reader = new FileReader()
      reader.onload = async (e) => {
        try {
          const buffer = await audioContext.decodeAudioData(e.target?.result as ArrayBuffer)
          setPads((prev) =>
            prev.map((p) =>
              p.id === padId ? { ...p, audioBuffer: buffer, fileName: file.name } : p
            )
          )
        } catch {
          console.error('Failed to decode audio')
        }
      }
      reader.readAsArrayBuffer(file)
    },
    [audioContext]
  )

  const triggerPad = useCallback(
    (padId: number) => {
      const pad = pads[padId]
      if (!pad.audioBuffer || !audioContext) return

      // Stop existing playback
      const existing = sourceRefs.current.get(padId)
      if (existing) {
        try { existing.stop() } catch {}
      }

      const source = audioContext.createBufferSource()
      const gain = audioContext.createGain()
      source.buffer = pad.audioBuffer
      gain.gain.value = pad.volume
      source.connect(gain)
      gain.connect(audioContext.destination)
      source.start()

      sourceRefs.current.set(padId, source)
      gainRefs.current.set(padId, gain)

      setPads((prev) => prev.map((p) => (p.id === padId ? { ...p, isPlaying: true } : p)))

      source.onended = () => {
        setPads((prev) => prev.map((p) => (p.id === padId ? { ...p, isPlaying: false } : p)))
        sourceRefs.current.delete(padId)
        gainRefs.current.delete(padId)
      }
    },
    [pads, audioContext]
  )

  const stopPad = useCallback((padId: number) => {
    const source = sourceRefs.current.get(padId)
    if (source) {
      try { source.stop() } catch {}
      sourceRefs.current.delete(padId)
    }
    setPads((prev) => prev.map((p) => (p.id === padId ? { ...p, isPlaying: false } : p)))
  }, [])

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
  }

  const handleDrop = (e: React.DragEvent, padId: number) => {
    e.preventDefault()
    const file = e.dataTransfer.files[0]
    if (file && file.type.startsWith('audio/')) {
      handleFileDrop(padId, file)
    }
  }

  return (
    <div className="sampler-container">
      <div className="sampler-header">
        <span className="sampler-title">SAMPLER</span>
      </div>
      <div className="sampler-pads">
        {pads.map((pad) => (
          <div
            key={pad.id}
            className={`sampler-pad ${pad.isPlaying ? 'active' : ''} ${pad.audioBuffer ? 'loaded' : ''}`}
            style={{
              '--pad-color': pad.color,
              borderColor: pad.isPlaying ? pad.color : undefined,
            } as React.CSSProperties}
            onClick={() => (pad.isPlaying ? stopPad(pad.id) : triggerPad(pad.id))}
            onDragOver={handleDragOver}
            onDrop={(e) => handleDrop(e, pad.id)}
            title={pad.fileName || 'Drop audio file here'}
          >
            <span className="sampler-pad-label">{pad.label}</span>
            {pad.fileName && (
              <span className="sampler-pad-file">
                {pad.fileName.slice(0, 8)}
              </span>
            )}
            {pad.isPlaying && <div className="sampler-pad-playing" />}
          </div>
        ))}
      </div>
    </div>
  )
}

export default Sampler
