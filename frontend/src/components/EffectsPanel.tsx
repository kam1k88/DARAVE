/* ============================================================
   EffectsPanel — DJ transition effect selector for Mix Deck.
   Shows available effects with visual icons and descriptions.
   ============================================================ */
import { useState } from 'react'
import {
  Repeat,
  AudioLines,
  Waves,
  Scissors,
  Wind,
  RotateCcw,
  Disc3,
  Volume2,
  Radio,
  SlidersHorizontal,
  Ban,
} from 'lucide-react'
import './EffectsPanel.css'

export interface EffectOption {
  name: string
  label: string
  icon: React.ElementType
  color: string
  description: string
}

export const EFFECT_OPTIONS: EffectOption[] = [
  { name: 'loop',       label: 'Loop',       icon: Repeat,       color: '#f59e0b', description: 'Beat-synced stutter loop' },
  { name: 'echo',       label: 'Echo',       icon: AudioLines,   color: '#38bdf8', description: 'Ping-pong delay' },
  { name: 'wobble',     label: 'Wobble',     icon: Waves,        color: '#a78bfa', description: 'Dubstep wobble filter' },
  { name: 'slicer',     label: 'Slicer',     icon: Scissors,     color: '#34d399', description: 'Chop & rearrange beats' },
  { name: 'flanger',    label: 'Flanger',    icon: Wind,         color: '#f87171', description: 'Jet plane comb sweep' },
  { name: 'phaser',     label: 'Phaser',     icon: RotateCcw,    color: '#fb923c', description: 'Swirling all-pass filter' },
  { name: 'vinyl_stop', label: 'Vinyl Stop', icon: Disc3,        color: '#e879f9', description: 'Turntable power-down' },
  { name: 'bitcrush',   label: 'Bitcrush',   icon: Volume2,      color: '#94a3b8', description: 'Lo-fi digital crush' },
  { name: 'reverb',     label: 'Reverb',     icon: Radio,        color: '#60a5fa', description: 'Diffuse reverb tail' },
  { name: 'filter',     label: 'Filter',     icon: SlidersHorizontal, color: '#fbbf24', description: 'Low/high-pass sweep' },
  { name: 'none',       label: 'None',       icon: Ban,          color: '#6b7280', description: 'No effect' },
]

interface EffectsPanelProps {
  /** Currently selected effect name. */
  value: string
  /** Called when user picks a different effect. */
  onChange: (effect: string) => void
}

export function EffectsPanel({ value, onChange }: EffectsPanelProps) {
  const [hovered, setHovered] = useState<string | null>(null)

  return (
    <div className="fx-panel">
      <div className="fx-panel__label">Transition Effect</div>
      <div className="fx-grid">
        {EFFECT_OPTIONS.map((opt) => {
          const active = value === opt.name
          const Icon = opt.icon
          return (
            <button
              key={opt.name}
              className={`fx-btn ${active ? 'fx-btn--active' : ''}`}
              onClick={() => onChange(opt.name)}
              onMouseEnter={() => setHovered(opt.name)}
              onMouseLeave={() => setHovered(null)}
              style={active ? { '--fx-color': opt.color } as React.CSSProperties : undefined}
              title={opt.description}
            >
              <Icon size={14} strokeWidth={active ? 2 : 1.5} />
              <span className="fx-btn__label">{opt.label}</span>
            </button>
          )
        })}
      </div>
      {hovered && (
        <div className="fx-panel__hint text-muted">
          {EFFECT_OPTIONS.find((o) => o.name === hovered)?.description}
        </div>
      )}
    </div>
  )
}
