/* ============================================================
   StemMixer — vertical 4-channel stem mixer with faders,
   solo and mute per stem. Used inside MixDeck.
   ============================================================ */
import { useState, useCallback } from 'react'
import './StemMixer.css'

const STEM_LABELS: Record<string, string> = {
  drums:  'DRUMS',
  bass:   'BASS',
  other:  'OTHER',
  vocals: 'VOCALS',
}

interface StemChannelProps {
  stem: string
  deckColor: string
  volume: number
  muted: boolean
  solo: boolean
  onVolume: (stem: string, value: number) => void
  onMute: (stem: string, muted: boolean) => void
  onSolo: (stem: string, solo: boolean) => void
}

function StemChannel({
  stem,
  deckColor,
  volume,
  muted,
  solo,
  onVolume,
  onMute,
  onSolo,
}: StemChannelProps) {
  const handleVolumeChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onVolume(stem, parseFloat(e.target.value))
    },
    [stem, onVolume],
  )

  return (
    <div className="stem-mixer__channel">
      <button
        className={`stem-mixer__btn stem-mixer__btn--solo ${solo ? 'stem-mixer__btn--solo-active' : ''}`}
        onClick={() => onSolo(stem, !solo)}
        title={`Solo ${stem}`}
      >
        S
      </button>

      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={volume}
        onChange={handleVolumeChange}
        className="stem-mixer__fader"
        style={{ accentColor: deckColor } as React.CSSProperties}
        data-orient="vertical"
        data-writing-mode="bt-lr"
        title={`${stem}: ${Math.round(volume * 100)}%`}
      />

      <span className="stem-mixer__label">{STEM_LABELS[stem] ?? stem}</span>

      <button
        className={`stem-mixer__btn stem-mixer__btn--mute ${muted ? 'stem-mixer__btn--mute-active' : ''}`}
        onClick={() => onMute(stem, !muted)}
        title={`Mute ${stem}`}
      >
        M
      </button>
    </div>
  )
}

export interface StemMixerProps {
  trackId: string
  trackLabel: string
  deckColor: string
  stems: string[]
  onStemVolume: (stem: string, value: number) => void
  onStemMute: (stem: string, muted: boolean) => void
  onStemSolo: (stem: string, solo: boolean) => void
}

export function StemMixer({
  trackLabel,
  deckColor,
  stems,
  onStemVolume,
  onStemMute,
  onStemSolo,
}: StemMixerProps) {
  const [volumes, setVolumes] = useState<Record<string, number>>(
    Object.fromEntries(stems.map((s) => [s, 1])),
  )
  const [muted, setMuted] = useState<Record<string, boolean>>(
    Object.fromEntries(stems.map((s) => [s, false])),
  )
  const [solo, setSolo] = useState<Record<string, boolean>>(
    Object.fromEntries(stems.map((s) => [s, false])),
  )

  const handleVolume = useCallback(
    (stem: string, value: number) => {
      setVolumes((prev) => ({ ...prev, [stem]: value }))
      onStemVolume(stem, value)
    },
    [onStemVolume],
  )

  const handleMute = useCallback(
    (stem: string, muted: boolean) => {
      setMuted((prev) => ({ ...prev, [stem]: muted }))
      onStemMute(stem, muted)
    },
    [onStemMute],
  )

  const handleSolo = useCallback(
    (stem: string, solo: boolean) => {
      setSolo((prev) => ({ ...prev, [stem]: solo }))
      onStemSolo(stem, solo)
    },
    [onStemSolo],
  )

  return (
    <div className="stem-mixer">
      <div className="stem-mixer__header">
        STEMS · {trackLabel}
      </div>

      {stems.map((stem) => (
        <StemChannel
          key={stem}
          stem={stem}
          deckColor={deckColor}
          volume={volumes[stem] ?? 1}
          muted={muted[stem] ?? false}
          solo={solo[stem] ?? false}
          onVolume={handleVolume}
          onMute={handleMute}
          onSolo={handleSolo}
        />
      ))}
    </div>
  )
}
