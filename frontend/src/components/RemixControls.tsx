/* ============================================================
   RemixControls — compact card exposing DJ remix parameters.
   Shared by MixDeck and SetBuilder.
   ============================================================ */
import './RemixControls.css'

export interface RemixOptions {
  transition_bars: 8 | 16 | 32
  preset: string
  transition_effect: string
  bridge_beat_mode: 'none' | 'auto'
  bridge_beat_genre: string
  bridge_beat_intensity: number
  target_lufs: number
  eq_strategy: string
  crossfade_type: string
}

export const REMIX_DEFAULTS: RemixOptions = {
  transition_bars: 16,
  preset: 'auto',
  transition_effect: 'auto',
  bridge_beat_mode: 'none',
  bridge_beat_genre: 'auto',
  bridge_beat_intensity: 0.38,
  target_lufs: -8,
  eq_strategy: 'auto',
  crossfade_type: 'auto',
}

const BAR_OPTIONS: Array<8 | 16 | 32> = [8, 16, 32]
const PRESETS       = ['auto', 'techno', 'house', 'hiphop', 'trap', 'dnb', 'ambient']
const EFFECTS       = ['auto', 'echo', 'filter', 'reverb', 'none']
const GENRES        = ['auto', 'techno', 'house', 'hiphop', 'trap', 'dnb', 'ambient']
const EQ_STRATEGIES = ['auto', 'default', 'bass_heavy', 'vocal_priority', 'aggressive']
const CROSSFADE_TYPES = ['auto', 'standard', 'extended', 'sharp']

function label(s: string) {
  if (s === 'dnb') return 'DnB'
  if (s === 'hiphop') return 'Hip-Hop'
  if (s === 'bass_heavy') return 'Bass Heavy'
  if (s === 'vocal_priority') return 'Vocal Priority'
  return s.charAt(0).toUpperCase() + s.slice(1)
}

interface RemixControlsProps {
  value: RemixOptions
  onChange: (v: RemixOptions) => void
}

function patch<K extends keyof RemixOptions>(
  prev: RemixOptions,
  key: K,
  val: RemixOptions[K],
): RemixOptions {
  return { ...prev, [key]: val }
}

export function RemixControls({ value, onChange }: RemixControlsProps) {
  const bridgeOn = value.bridge_beat_mode === 'auto'

  return (
    <div className="rc-card">
      <div className="rc-grid">
        {/* Transition bars — segmented control */}
        <div className="rc-field">
          <label className="rc-label">Transition bars</label>
          <div className="rc-seg">
            {BAR_OPTIONS.map((b) => (
              <button
                key={b}
                className={`rc-seg-btn ${value.transition_bars === b ? 'rc-seg-btn--active' : ''}`}
                onClick={() => onChange(patch(value, 'transition_bars', b))}
              >
                {b}
              </button>
            ))}
          </div>
        </div>

        {/* Preset */}
        <div className="rc-field">
          <label className="rc-label">Preset</label>
          <select
            className="rc-select"
            value={value.preset}
            onChange={(e) => onChange(patch(value, 'preset', e.target.value))}
          >
            {PRESETS.map((p) => <option key={p} value={p}>{label(p)}</option>)}
          </select>
        </div>

        {/* Transition effect */}
        <div className="rc-field">
          <label className="rc-label">Transition effect</label>
          <select
            className="rc-select"
            value={value.transition_effect}
            onChange={(e) => onChange(patch(value, 'transition_effect', e.target.value))}
          >
            {EFFECTS.map((e) => <option key={e} value={e}>{label(e)}</option>)}
          </select>
        </div>

        {/* Bridge beat toggle */}
        <div className="rc-field">
          <label className="rc-label">Bridge beat</label>
          <button
            className={`rc-toggle ${bridgeOn ? 'rc-toggle--on' : ''}`}
            onClick={() => onChange(patch(value, 'bridge_beat_mode', bridgeOn ? 'none' : 'auto'))}
          >
            {bridgeOn ? 'on' : 'off'}
          </button>
        </div>
      </div>

      {/* Bridge beat sub-controls */}
      {bridgeOn && (
        <div className="rc-grid rc-grid--bridge">
          <div className="rc-field">
            <label className="rc-label">Beat genre</label>
            <select
              className="rc-select"
              value={value.bridge_beat_genre}
              onChange={(e) => onChange(patch(value, 'bridge_beat_genre', e.target.value))}
            >
              {GENRES.map((g) => <option key={g} value={g}>{label(g)}</option>)}
            </select>
          </div>
          <div className="rc-field rc-field--wide">
            <label className="rc-label">
              Beat intensity — {Math.round(value.bridge_beat_intensity * 100)}%
            </label>
            <input
              type="range" min={0} max={1} step={0.05}
              value={value.bridge_beat_intensity}
              onChange={(e) =>
                onChange(patch(value, 'bridge_beat_intensity', parseFloat(e.target.value)))
              }
              className="rc-slider"
            />
          </div>
        </div>
      )}

      {/* Advanced mixing controls */}
      <div className="rc-grid rc-grid--advanced">
        {/* Target LUFS */}
        <div className="rc-field rc-field--wide">
          <label className="rc-label">
            Target LUFS — {value.target_lufs}
          </label>
          <input
            type="range" min={-16} max={-6} step={1}
            value={value.target_lufs}
            onChange={(e) =>
              onChange(patch(value, 'target_lufs', parseInt(e.target.value)))
            }
            className="rc-slider"
          />
          <div className="rc-slider-hints">
            <span>quieter</span>
            <span>louder</span>
          </div>
        </div>

        {/* EQ Strategy */}
        <div className="rc-field">
          <label className="rc-label">EQ strategy</label>
          <select
            className="rc-select"
            value={value.eq_strategy}
            onChange={(e) => onChange(patch(value, 'eq_strategy', e.target.value))}
          >
            {EQ_STRATEGIES.map((s) => <option key={s} value={s}>{label(s)}</option>)}
          </select>
        </div>

        {/* Crossfade type */}
        <div className="rc-field">
          <label className="rc-label">Crossfade type</label>
          <select
            className="rc-select"
            value={value.crossfade_type}
            onChange={(e) => onChange(patch(value, 'crossfade_type', e.target.value))}
          >
            {CROSSFADE_TYPES.map((t) => <option key={t} value={t}>{label(t)}</option>)}
          </select>
        </div>
      </div>
    </div>
  )
}
