/* ============================================================
   TransitionCard — visual card for a single mix transition
   ============================================================ */

import {
  ArrowRight,
  Zap,
  Music,
  Waves,
  BarChart3,
} from 'lucide-react'
import type { MixPlanTransition } from '@/types'
import './TransitionCard.css'

const TECHNIQUE_COLORS: Record<string, string> = {
  'Standard Crossfade': 'var(--color-ice-400)',
  'Extended Blend': 'var(--color-green-500)',
  'Sharp Handoff': 'var(--color-crimson-500)',
  'Dynamic EQ': 'var(--color-violet-400)',
  'Filter Sweep': 'var(--color-amber-500)',
  'Bass Swap': 'var(--color-ice-400)',
  'Echo Out': 'var(--color-green-500)',
  'Reverb Tail': 'var(--color-violet-400)',
}

const EFFECT_ICONS: Record<string, typeof ArrowRight> = {
  echo: Zap,
  filter: Waves,
  reverb: Music,
  none: ArrowRight,
}

interface Props {
  transition: MixPlanTransition
  isLast?: boolean
}

export function TransitionCard({ transition, isLast }: Props) {
  const t = transition
  const color = TECHNIQUE_COLORS[t.technique] || 'var(--color-text-secondary)'
  const EffectIcon = EFFECT_ICONS[t.effect] || ArrowRight
  const confPct = Math.round(t.confidence * 100)
  const confColor =
    confPct >= 80 ? 'var(--color-green-500)' :
    confPct >= 50 ? 'var(--color-amber-500)' :
    'var(--color-crimson-500)'

  return (
    <div className="tc-card">
      {/* Song labels */}
      <div className="tc-card__header">
        <div className="tc-card__song tc-card__song--from">
          <span className="tc-card__song-idx">{t.pair_index + 1}</span>
          <span className="tc-card__song-name" title={t.from_song}>
            {t.from_song}
          </span>
        </div>
        <ArrowRight size={14} className="tc-card__arrow" />
        <div className="tc-card__song tc-card__song--to">
          <span className="tc-card__song-idx">{t.pair_index + 2}</span>
          <span className="tc-card__song-name" title={t.to_song}>
            {t.to_song}
          </span>
        </div>
      </div>

      {/* Technique badge */}
      <div className="tc-card__badge" style={{ borderColor: color, color }}>
        <EffectIcon size={12} />
        {t.technique}
      </div>

      {/* Stats row */}
      <div className="tc-card__stats">
        <div className="tc-card__stat">
          <span className="tc-card__stat-label">Bars</span>
          <span className="tc-card__stat-value">{t.transition_bars}</span>
        </div>
        <div className="tc-card__stat">
          <span className="tc-card__stat-label">BPM</span>
          <span className="tc-card__stat-value">{t.bpm_from} → {t.bpm_to}</span>
        </div>
        <div className="tc-card__stat">
          <span className="tc-card__stat-label">Key</span>
          <span className="tc-card__stat-value">{t.camelot_from} → {t.camelot_to}</span>
        </div>
        <div className="tc-card__stat">
          <span className="tc-card__stat-label">X-fade</span>
          <span className="tc-card__stat-value">{t.crossfade_type}</span>
        </div>
      </div>

      {/* Energy delta bar */}
      <div className="tc-card__energy">
        <BarChart3 size={12} />
        <div className="tc-card__energy-bar">
          <div
            className="tc-card__energy-fill"
            style={{
              width: `${Math.min(100, Math.abs(t.energy_delta) * 200)}%`,
              background: t.energy_delta > 0 ? 'var(--color-green-500)' : 'var(--color-crimson-500)',
            }}
          />
        </div>
        <span className="tc-card__energy-delta" style={{ color: t.energy_delta > 0 ? 'var(--color-green-500)' : 'var(--color-crimson-500)' }}>
          {t.energy_delta > 0 ? '+' : ''}{(t.energy_delta * 100).toFixed(0)}
        </span>
      </div>

      {/* Confidence + reason */}
      <div className="tc-card__footer">
        <div className="tc-card__conf">
          <div className="tc-card__conf-dot" style={{ background: confColor }} />
          <span>{confPct}%</span>
        </div>
        <span className="tc-card__reason">{t.reason}</span>
      </div>

      {/* Bridge beat indicator */}
      {t.bridge_beat && (
        <div className="tc-card__bridge">
          <Zap size={10} /> Bridge beat
        </div>
      )}

      {/* Connector line to next card */}
      {!isLast && <div className="tc-card__connector" />}
    </div>
  )
}
