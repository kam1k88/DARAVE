/* ============================================================
   AI RemixMate — Solo Mode
   Pick a technique, tweak parameters via sliders, preview.
   ============================================================ */
import { useState, useMemo, useCallback } from 'react'
import { Sliders, Loader2, RotateCcw, Info } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { techniquesApi } from '@/lib/api'
import type { DJTechnique, TechniqueParam } from '@/types'
import './PageBase.css'
import './SoloMode.css'

const LEVEL_COLORS: Record<string, string> = {
  beginner:      'var(--color-green-500)',
  intermediate:  'var(--color-amber-500)',
  advanced:      'var(--color-crimson-500)',
  experimental:  'var(--color-violet-400)',
}

const CATEGORY_ICONS: Record<string, string> = {
  cut:       '\u2702',
  eq:        '\uD83C\uDF9B',
  filter:    '\uD83D\uDD0A',
  echo:      '\uD83D\uDCFB',
  combo:     '\uD83D\uDD17',
  loop:      '\uD83D\uDD01',
  effect:    '\u2728',
  stem:      '\uD83C\uDFA4',
  structural:'\uD83D\uDCD0',
  ambient:   '\uD83C\uDF0A',
  pitch:     '\uD83C\uDFB5',
}

function ParamSlider({
  param,
  value,
  onChange,
}: {
  param: TechniqueParam
  value: number
  onChange: (v: number) => void
}) {
  if (param.type === 'select' && param.options?.length) {
    return (
      <div className="param-slider">
        <label className="param-slider__label">{param.label}</label>
        <div className="param-slider__select-row">
          {param.options.map((opt) => (
            <button
              key={opt}
              className={`param-slider__select-btn ${value === Number(opt) || String(value) === opt ? 'param-slider__select-btn--active' : ''}`}
              onClick={() => onChange(Number(opt) || 0)}
            >
              {opt}
            </button>
          ))}
        </div>
        <span className="param-slider__unit">{param.unit}</span>
      </div>
    )
  }

  const step = param.type === 'int' ? 1 : 0.01
  const pct = ((value - param.min_val) / (param.max_val - param.min_val)) * 100

  return (
    <div className="param-slider">
      <div className="param-slider__header">
        <label className="param-slider__label">{param.label}</label>
        <span className="param-slider__value">
          {param.type === 'int' ? Math.round(value) : value.toFixed(2)} {param.unit}
        </span>
      </div>
      <div className="param-slider__track-wrap">
        <input
          type="range"
          className="param-slider__track"
          min={param.min_val}
          max={param.max_val}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{ background: `linear-gradient(to right, var(--color-amber-500) ${pct}%, var(--color-surface-elevated) ${pct}%)` }}
        />
        <div className="param-slider__labels">
          <span>{param.min_val}</span>
          <span>{param.max_val}</span>
        </div>
      </div>
    </div>
  )
}

export default function SoloMode() {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [paramValues, setParamValues] = useState<Record<string, number>>({})

  const { data: techniques = [], isLoading } = useQuery({
    queryKey: ['techniques'],
    queryFn: () => techniquesApi.list(),
  })

  const selected = useMemo(
    () => techniques.find((t) => t.id === selectedId) || null,
    [techniques, selectedId],
  )

  const handleSelect = useCallback((t: DJTechnique) => {
    setSelectedId(t.id)
    const defaults: Record<string, number> = {}
    ;(t.parameters ?? []).forEach((p: TechniqueParam) => {
      defaults[p.name] = p.default
    })
    setParamValues(defaults)
  }, [])

  const handleReset = useCallback(() => {
    if (!selected) return
    const defaults: Record<string, number> = {}
    ;(selected.parameters ?? []).forEach((p: TechniqueParam) => {
      defaults[p.name] = p.default
    })
    setParamValues(defaults)
  }, [selected])

  const handleParamChange = useCallback((name: string, val: number) => {
    setParamValues((prev) => ({ ...prev, [name]: val }))
  }, [])

  if (isLoading) {
    return (
      <div className="page-base">
        <div className="page-base__header">
          <h1 className="page-base__title"><Sliders size={20} /> Solo Mode</h1>
        </div>
        <div className="page-base__body solo-loading">
          <Loader2 className="spin" size={32} />
        </div>
      </div>
    )
  }

  return (
    <div className="page-base">
      <div className="page-base__header">
        <h1 className="page-base__title"><Sliders size={20} /> Solo Mode</h1>
        <span className="page-base__subtitle">
          {selected ? `${selected.name} — настройка параметров` : 'Выбери технику для настройки'}
        </span>
      </div>

      <div className="page-base__body solo-body">
        {/* Left: technique list */}
        <div className="solo-list">
          {techniques.map((t) => (
            <button
              key={t.id}
              className={`solo-list__item ${selectedId === t.id ? 'solo-list__item--active' : ''}`}
              onClick={() => handleSelect(t)}
            >
              <span className="solo-list__icon">{CATEGORY_ICONS[t.category] || '?'}</span>
              <div className="solo-list__info">
                <span className="solo-list__name">{t.name}</span>
                <span className="solo-list__id">{t.id}</span>
              </div>
              <span
                className="solo-list__level"
                style={{ color: LEVEL_COLORS[t.level] || 'var(--color-muted)' }}
              >
                {t.difficulty}/5
              </span>
            </button>
          ))}
        </div>

        {/* Center: parameter sliders */}
        <div className="solo-params">
          {selected ? (
            <>
              <div className="solo-params__header">
                <h2 className="solo-params__title">{selected.name}</h2>
                <div className="solo-params__actions">
                  <button className="btn btn--ghost" onClick={handleReset} title="Сброс">
                    <RotateCcw size={16} /> Сброс
                  </button>
                </div>
              </div>

              <p className="solo-params__desc">{selected.description}</p>

              {selected.parameters.length === 0 ? (
                <div className="solo-params__empty">
                  <Info size={16} />
                  <span>Эта техника не имеет настраиваемых параметров</span>
                </div>
              ) : (
                <div className="solo-params__grid">
                  {selected.parameters.map((p) => (
                    <ParamSlider
                      key={p.name}
                      param={p}
                      value={paramValues[p.name] ?? p.default}
                      onChange={(v) => handleParamChange(p.name, v)}
                    />
                  ))}
                </div>
              )}

              <div className="solo-params__meta">
                <span className="solo-meta-tag">BPM: {selected.bpm_range[0]}–{selected.bpm_range[1]}</span>
                <span className="solo-meta-tag">Ключ: {selected.key_compatibility}</span>
                <span className="solo-meta-tag">Энергия: {selected.energy_delta}</span>
                <span className="solo-meta-tag">Такты: {selected.transition_bars}</span>
              </div>

              <div className="solo-params__steps">
                <h3>Как играть:</h3>
                <ol>
                  {(selected.steps ?? []).map((step: string, i: number) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              </div>
            </>
          ) : (
            <div className="solo-params__placeholder">
              <Sliders size={48} />
              <p>Выбери технику слева, чтобы настроить параметры</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
