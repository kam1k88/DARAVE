/* ============================================================
   DARAVE — Solo
   2-track mixing: select tracks, pick technique, tweak params, preview.
   ============================================================ */
import { useState, useCallback, useMemo } from 'react'
import { Sliders, RotateCcw, Info, Music2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { libraryApi, techniquesApi } from '@/lib/api'
import type { SongInfo, DJTechnique, TechniqueParam } from '@/types'
import './PageBase.css'
import './Solo.css'

const LEVEL_COLORS: Record<string, string> = {
  beginner:      'var(--color-green-500)',
  intermediate:  'var(--color-amber-500)',
  advanced:      'var(--color-crimson-500)',
  experimental:  'var(--color-violet-400)',
}

const CATEGORY_ICONS: Record<string, string> = {
  cut: '\u2702', eq: '\uD83C\uDF9B', filter: '\uD83D\uDD0A', echo: '\uD83D\uDCFB',
  combo: '\uD83D\uDD17', loop: '\uD83D\uDD01', effect: '\u2728', stem: '\uD83C\uDFA4',
  structural: '\uD83D\uDCD0', ambient: '\uD83C\uDF0A', pitch: '\uD83C\uDFB5',
}

function ParamSlider({ param, value, onChange }: { param: TechniqueParam; value: number; onChange: (v: number) => void }) {
  if (param.type === 'select' && param.options?.length) {
    return (
      <div className="param-slider">
        <label className="param-slider__label">{param.label}</label>
        <div className="param-slider__select-row">
          {param.options.map((opt) => (
            <button
              key={opt}
              className={`param-slider__select-btn ${String(value) === opt ? 'param-slider__select-btn--active' : ''}`}
              onClick={() => onChange(Number(opt) || 0)}
            >{opt}</button>
          ))}
        </div>
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
      <input
        type="range" className="param-slider__track"
        min={param.min_val} max={param.max_val} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ background: `linear-gradient(to right, var(--color-amber-500) ${pct}%, var(--color-surface-elevated) ${pct}%)` }}
      />
    </div>
  )
}

export default function Solo() {
  const [deckA, setDeckA] = useState<string | null>(null)
  const [deckB, setDeckB] = useState<string | null>(null)
  const [selectedTechId, setSelectedTechId] = useState<string | null>(null)
  const [paramValues, setParamValues] = useState<Record<string, number>>({})
  const [selecting, setSelecting] = useState<'a' | 'b' | null>(null)

  const { data: library = [] } = useQuery<SongInfo[]>({
    queryKey: ['library'],
    queryFn: () => libraryApi.list(),
  })

  const { data: techniques = [] } = useQuery<DJTechnique[]>({
    queryKey: ['techniques'],
    queryFn: () => techniquesApi.list(),
  })

  const selectedTech = useMemo(
    () => techniques.find((t) => t.id === selectedTechId) || null,
    [techniques, selectedTechId],
  )

  const trackA = useMemo(() => library.find((s) => s.name === deckA) || null, [library, deckA])
  const trackB = useMemo(() => library.find((s) => s.name === deckB) || null, [library, deckB])

  const handleSelectTrack = useCallback((name: string) => {
    if (selecting === 'a') { setDeckA(name); setSelecting(null) }
    else if (selecting === 'b') { setDeckB(name); setSelecting(null) }
  }, [selecting])

  const handleSelectTech = useCallback((t: DJTechnique) => {
    setSelectedTechId(t.id)
    const defaults: Record<string, number> = {}
    ;(t.parameters ?? []).forEach((p: TechniqueParam) => { defaults[p.name] = p.default })
    setParamValues(defaults)
  }, [])

  return (
    <div className="page-base">
      <div className="page-base__header">
        <h1 className="page-base__title"><Sliders size={20} /> Соло</h1>
        <span className="page-base__subtitle">
          {trackA && trackB
            ? `${trackA.name.slice(0, 30)} ↔ ${trackB.name.slice(0, 30)}`
            : 'Выбери два трека для настройки перехода'}
        </span>
      </div>

      <div className="page-base__body solo-body">
        {/* Left: decks */}
        <div className="solo-decks">
          {/* Deck A */}
          <div className={`solo-deck ${selecting === 'a' ? 'solo-deck--selecting' : ''}`}>
            <h3>Deck A</h3>
            {trackA ? (
              <div className="solo-deck__track">
                <span className="solo-deck__name">{trackA.name}</span>
                <div className="solo-deck__meta">
                  {trackA.bpm && <span>{Math.round(trackA.bpm)} BPM</span>}
                  {trackA.camelot && <span>{trackA.camelot}</span>}
                  {trackA.el && <span>EL {trackA.el}</span>}
                </div>
                <button className="btn btn--ghost btn--sm" onClick={() => setDeckA(null)}>Сменить</button>
              </div>
            ) : (
              <button className="solo-deck__select" onClick={() => setSelecting('a')}>
                <Music2 size={24} />
                <span>Выбрать трек</span>
              </button>
            )}
          </div>

          {/* Deck B */}
          <div className={`solo-deck ${selecting === 'b' ? 'solo-deck--selecting' : ''}`}>
            <h3>Deck B</h3>
            {trackB ? (
              <div className="solo-deck__track">
                <span className="solo-deck__name">{trackB.name}</span>
                <div className="solo-deck__meta">
                  {trackB.bpm && <span>{Math.round(trackB.bpm)} BPM</span>}
                  {trackB.camelot && <span>{trackB.camelot}</span>}
                  {trackB.el && <span>EL {trackB.el}</span>}
                </div>
                <button className="btn btn--ghost btn--sm" onClick={() => setDeckB(null)}>Сменить</button>
              </div>
            ) : (
              <button className="solo-deck__select" onClick={() => setSelecting('b')}>
                <Music2 size={24} />
                <span>Выбрать трек</span>
              </button>
            )}
          </div>

          {/* Track picker overlay */}
          {selecting && (
            <div className="solo-picker">
              <h3>Выбери трек для Deck {selecting === 'a' ? 'A' : 'B'}</h3>
              <div className="solo-picker__list">
                {library.filter(s => s.name !== (selecting === 'a' ? deckB : deckA)).map((song) => (
                  <button key={song.name} className="solo-picker__item" onClick={() => handleSelectTrack(song.name)}>
                    <span>{song.name}</span>
                    {song.bpm && <span className="solo-picker__bpm">{Math.round(song.bpm)}</span>}
                  </button>
                ))}
              </div>
              <button className="btn btn--ghost" onClick={() => setSelecting(null)}>Отмена</button>
            </div>
          )}
        </div>

        {/* Center: technique + params */}
        <div className="solo-center">
          {/* Technique list */}
          <div className="solo-tech-list">
            <h3>Техника</h3>
            <div className="solo-tech-list__scroll">
              {techniques.map((t) => (
                <button
                  key={t.id}
                  className={`solo-tech-item ${selectedTechId === t.id ? 'solo-tech-item--active' : ''}`}
                  onClick={() => handleSelectTech(t)}
                >
                  <span className="solo-tech-item__icon">{CATEGORY_ICONS[t.category] || '?'}</span>
                  <div className="solo-tech-item__info">
                    <span className="solo-tech-item__name">{t.name}</span>
                    <span className="solo-tech-item__id">{t.id}</span>
                  </div>
                  <span className="solo-tech-item__level" style={{ color: LEVEL_COLORS[t.level] || 'var(--color-muted)' }}>
                    {t.difficulty}/5
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* Parameters */}
          <div className="solo-params">
            {selectedTech ? (
              <>
                <div className="solo-params__header">
                  <h2>{selectedTech.name}</h2>
                  <button className="btn btn--ghost btn--sm" onClick={() => {
                    const defaults: Record<string, number> = {}
                    ;(selectedTech.parameters ?? []).forEach((p: TechniqueParam) => { defaults[p.name] = p.default })
                    setParamValues(defaults)
                  }}>
                    <RotateCcw size={14} /> Сброс
                  </button>
                </div>
                <p className="solo-params__desc">{selectedTech.description}</p>
                {(selectedTech.parameters ?? []).length === 0 ? (
                  <div className="solo-params__empty">
                    <Info size={16} /> Нет настраиваемых параметров
                  </div>
                ) : (
                  <div className="solo-params__grid">
                    {(selectedTech.parameters ?? []).map((p: TechniqueParam) => (
                      <ParamSlider
                        key={p.name} param={p}
                        value={paramValues[p.name] ?? p.default}
                        onChange={(v) => setParamValues((prev) => ({ ...prev, [p.name]: v }))}
                      />
                    ))}
                  </div>
                )}
                <div className="solo-params__meta">
                  <span className="solo-meta-tag">BPM: {selectedTech.bpm_range[0]}–{selectedTech.bpm_range[1]}</span>
                  <span className="solo-meta-tag">Ключ: {selectedTech.key_compatibility}</span>
                  <span className="solo-meta-tag">Энергия: {selectedTech.energy_delta}</span>
                </div>
                <div className="solo-params__steps">
                  <h4>Как играть:</h4>
                  <ol>
                    {(selectedTech.steps ?? []).map((step: string, i: number) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ol>
                </div>
              </>
            ) : (
              <div className="solo-params__placeholder">
                <Sliders size={48} />
                <p>Выбери технику слева</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
