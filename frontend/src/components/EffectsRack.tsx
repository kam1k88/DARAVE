import './EffectsRack.css'

interface EffectsRackProps {
  trackId: string
  deckColor: string
  currentEffect: string
  onEffectChange: (effectType: string) => void
}

const EFFECTS = [
  { id: 'none', label: 'OFF', description: 'No effect' },
  { id: 'echo', label: 'ECHO', description: 'Delay' },
  { id: 'filter', label: 'FILTER', description: 'Lowpass' },
  { id: 'reverb', label: 'REVERB', description: 'Room' },
  { id: 'distortion', label: 'DIST', description: 'Drive' },
] as const

export default function EffectsRack({
  trackId,
  deckColor,
  currentEffect,
  onEffectChange,
}: EffectsRackProps) {
  return (
    <div className="effects-rack" style={{ '--deck-color': deckColor } as React.CSSProperties}>
      <div className="effects-rack__header">EFFECTS</div>
      <div className="effects-rack__grid">
        {EFFECTS.map((fx) => {
          const isActive = currentEffect === fx.id
          return (
            <button
              key={`${trackId}-${fx.id}`}
              className={`effects-rack__btn${isActive ? ' effects-rack__btn--active' : ''}`}
              onClick={() => onEffectChange(isActive ? 'none' : fx.id)}
              title={fx.description}
            >
              <span>{fx.label}</span>
            </button>
          )
        })}
      </div>
      <div className="effects-rack__label">
        {EFFECTS.find((f) => f.id === currentEffect)?.description ?? 'No effect'}
      </div>
    </div>
  )
}
