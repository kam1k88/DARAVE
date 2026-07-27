/* ============================================================
   DARAVE — Strategy v2
   Smart set plan: EL-arc ordering, alternatives, reorder, energy arc.
   Chain preview, technique selection, final render.
   ============================================================ */
import { useState, useMemo, useCallback, useRef, useEffect } from 'react'
import { Map, Loader2, ChevronDown, ChevronUp, Zap, RotateCcw, GripVertical, Info, ChevronRight, Play, Pause, SkipForward, Download } from 'lucide-react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { libraryApi, strategyApi, quickMixApi, remixApi } from '@/lib/api'
import { useAppStore } from '@/stores/appStore'
import { WebAudioPlayer } from '@/components/WebAudioPlayer'
import type { SongInfo } from '@/types'
import './PageBase.css'
import './Strategy.css'

interface TransitionPlan {
  pair_index: number
  from_song: string
  to_song: string
  technique: string
  technique_id: string
  effect: string
  transition_bars: number
  confidence: number
  reason: string
  el_from: number
  el_to: number
  bpm_from: number
  bpm_to: number
  tempo_type_from: string
  tempo_type_to: string
  camelot_from: string
  camelot_to: string
  priority_rule: number
}

interface TrackInfo {
  song_name: string
  bpm: number
  camelot: string
  key: string
  energy_mean: number
  energy_std: number
  total_bars: number
  tempo_type: string
  el: number
}

interface AltTechnique {
  technique_id: string
  name: string
  confidence: number
  rule: string
  selected: boolean
}

const EL_COLORS: Record<number, string> = {
  1: '#22c55e', 2: '#84cc16', 3: '#eab308', 4: '#f97316', 5: '#ef4444',
}

const RULE_NAMES: Record<number, string> = {
  0: 'Fallback',
  1: 'Tritone (Камелот ±6)',
  2: 'BPM + Key + Energy match',
  3: 'Несовместимые BPM типы',
  4: 'Half→Full: Double Drop',
  5: 'Full→Half: Delay Out',
  6: 'Energy jump ±20/EL±2',
  7: 'Снижение энергии',
  8: 'Experimental',
}

const ARC_MODES = [
  { value: 'dynamic', label: 'Динамическая (DnB)', desc: 'EL 1→5→1→2, кульминация в середине' },
  { value: 'fade_out', label: 'Затухание', desc: 'Старт с высокой энергии, плавное снижение' },
  { value: 'fade_in', label: 'Нарастание', desc: 'Старт с низкой энергии, плавный рост' },
]

const ARC_DESCS: Record<string, string> = {
  dynamic: 'Умный алгоритм: старт с EL 1-2, плавный рост энергии, кульминация EL 4-5 в середине, финал с затуханием. BPM и Camelot подбираются по цепочке.',
  fade_out: 'Высокая энергия в начале сета, постепенное снижение к финалу. Идеально для закрытия танцпола.',
  fade_in: 'Спокойное начало с постепенным нарастанием энергии к кульминации в конце сета.',
}

export default function Strategy() {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null)
  const [plan, setPlan] = useState<any>(null)
  const [trackOrder, setTrackOrder] = useState<string[]>([])
  const [altsForPair, setAltsForPair] = useState<number | null>(null)
  const [alternatives, setAlternatives] = useState<AltTechnique[]>([])
  const [loadingAlts, setLoadingAlts] = useState(false)
  const [arcMode, setArcMode] = useState('dynamic')
  const [previewingPair, setPreviewingPair] = useState<number | null>(null)
  const [previewJobId, setPreviewJobId] = useState<string | null>(null)
  const [chainPreviewing, setChainPreviewing] = useState(false)
  const [chainPreviewQueue, setChainPreviewQueue] = useState<number[]>([])
  const [rendering, setRendering] = useState(false)
  const [renderJobId, setRenderJobId] = useState<string | null>(null)
  const [selectedTechOverrides, setSelectedTechOverrides] = useState<Record<number, string>>({})
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const dragItem = useRef<number | null>(null)
  const dragOver = useRef<number | null>(null)

  const jobs = useAppStore((s) => s.jobs)
  const upsertJob = useAppStore((s) => s.upsertJob)

  const { data: library = [] } = useQuery<SongInfo[]>({
    queryKey: ['library'],
    queryFn: () => libraryApi.list(),
  })

  const analyzedTracks = useMemo(
    () => library.filter((s) => s.bpm && s.has_analysis).sort((a, b) => (a.bpm ?? 0) - (b.bpm ?? 0)),
    [library],
  )

  const smartMutation = useMutation({
    mutationFn: () => strategyApi.planSmart(arcMode),
    onSuccess: (data) => {
      setPlan(data)
      setTrackOrder(data.songs || [])
      setSelectedTechOverrides({})
    },
  })

  const loadAlts = useCallback(async (pairIdx: number) => {
    if (!plan) return
    const tr = plan.transitions[pairIdx]
    if (!tr) return
    setLoadingAlts(true)
    setAltsForPair(pairIdx)
    try {
      const data = await strategyApi.alternatives(tr.from_song, tr.to_song)
      setAlternatives(data.alternatives || [])
    } catch {
      setAlternatives([])
    } finally {
      setLoadingAlts(false)
    }
  }, [plan])

  // Select an alternative technique for a transition
  const selectAlternative = useCallback((pairIdx: number, techniqueId: string) => {
    setSelectedTechOverrides((prev) => ({ ...prev, [pairIdx]: techniqueId }))
    // Update the plan's transition in-place for display
    setPlan((prev: any) => {
      if (!prev) return prev
      const newTransitions = [...prev.transitions]
      const tr = { ...newTransitions[pairIdx] }
      tr.technique_id = techniqueId
      newTransitions[pairIdx] = tr
      return { ...prev, transitions: newTransitions }
    })
  }, [])

  // Single transition preview
  const previewTransition = useCallback(async (tr: TransitionPlan) => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setPreviewingPair(tr.pair_index)
    try {
      const techId = selectedTechOverrides[tr.pair_index] || tr.technique_id
      const res = await quickMixApi.preview(tr.from_song, tr.to_song, techId)
      setPreviewJobId(res.job_id)
    } catch {
      setPreviewingPair(null)
    }
  }, [selectedTechOverrides])

  // Chain preview: play all transitions sequentially
  const previewChain = useCallback(async () => {
    if (!plan || chainPreviewing) return
    setChainPreviewing(true)
    setChainPreviewQueue(transitions.map((_: any, i: number) => i))
  }, [plan, chainPreviewing])

  const stopChainPreview = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setChainPreviewing(false)
    setChainPreviewQueue([])
    setPreviewingPair(null)
    setPreviewJobId(null)
  }, [])

  // Watch for chain preview queue: play next when current finishes
  useEffect(() => {
    if (!chainPreviewing || chainPreviewQueue.length === 0) return
    const nextIdx = chainPreviewQueue[0]
    const tr = transitions[nextIdx]
    if (!tr) { setChainPreviewing(false); setChainPreviewQueue([]); return }
    previewTransition(tr)
    setChainPreviewQueue((prev) => prev.slice(1))
  }, [chainPreviewing, chainPreviewQueue, previewTransition])

  // Watch for preview job completion → play audio
  const BASE = import.meta.env.VITE_API_BASE || '/api'
  useEffect(() => {
    if (!previewJobId) return
    const job = jobs[previewJobId]
    if (!job) return
    if (job.status === 'COMPLETED' && job.result) {
      const r = job.result as { stream_url?: string }
      if (r.stream_url) {
        const url = `${BASE}${r.stream_url}`
        const audio = new Audio(url)
        audioRef.current = audio
        audio.play().catch(() => {})
        audio.onended = () => {
          if (!chainPreviewing) {
            setPreviewingPair(null)
            setPreviewJobId(null)
          }
          // If chain previewing, the queue effect will pick up the next one
        }
      } else {
        setPreviewingPair(null)
        setPreviewJobId(null)
      }
    } else if (job.status === 'FAILED') {
      setPreviewingPair(null)
      setPreviewJobId(null)
    }
  }, [jobs, previewJobId, BASE, chainPreviewing])

  // Render full chain mix
  const renderChain = useCallback(async () => {
    if (!plan || rendering) return
    setRendering(true)
    try {
      const res = await remixApi.chain(trackOrder, {
        transition_bars: plan.transitions?.[0]?.transition_bars || 16,
        smart_transitions: false,
        transition_effect: 'auto',
      })
      setRenderJobId(res.job_id)
      upsertJob({
        job_id: res.job_id, status: 'PENDING', type: 'dj_chain',
        progress: 0, message: `Rendering ${trackOrder.length}-track mix…`,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      })
    } catch {
      setRendering(false)
    }
  }, [plan, rendering, trackOrder, upsertJob])

  // Track render job
  useEffect(() => {
    if (!renderJobId) return
    const job = jobs[renderJobId]
    if (!job) return
    if (job.status === 'COMPLETED') {
      setRendering(false)
      setRenderJobId(null)
    } else if (job.status === 'FAILED') {
      setRendering(false)
      setRenderJobId(null)
    }
  }, [jobs, renderJobId])

  const transitions: TransitionPlan[] = plan?.transitions ?? []
  const structures: TrackInfo[] = plan?.structures ?? []
  const avgConfidence = plan?.avg_confidence ?? 0
  const totalDuration = plan?.total_duration_sec ?? 0

  // Drag reorder handlers
  const handleDragStart = (idx: number) => { dragItem.current = idx }
  const handleDragEnter = (idx: number) => { dragOver.current = idx }
  const handleDragEnd = () => {
    if (dragItem.current === null || dragOver.current === null) return
    const newOrder = [...trackOrder]
    const dragged = newOrder.splice(dragItem.current, 1)[0]
    newOrder.splice(dragOver.current, 0, dragged)
    setTrackOrder(newOrder)
    dragItem.current = null
    dragOver.current = null
  }

  const moveTrack = (idx: number, dir: -1 | 1) => {
    const newIdx = idx + dir
    if (newIdx < 0 || newIdx >= trackOrder.length) return
    const newOrder = [...trackOrder]
    ;[newOrder[idx], newOrder[newIdx]] = [newOrder[newIdx], newOrder[idx]]
    setTrackOrder(newOrder)
  }

  const renderJob = renderJobId ? jobs[renderJobId] : null

  return (
    <div className="page-base">
      <div className="page-base__header">
        <h1 className="page-base__title"><Map size={20} /> Стратегия</h1>
        <span className="page-base__subtitle">
          {plan
            ? `${transitions.length} переходов · уверенность ${Math.round(avgConfidence * 100)}% · ${Math.round(totalDuration / 60)} мин`
            : `${analyzedTracks.length} проанализированных треков`}
        </span>
      </div>

      <div className="page-base__body strategy-body-full">
        {!plan && (
          <div className="strategy-build">
            <div className="strategy-build__icon"><Map size={64} /></div>
            <h2>Построить стратегию для всех треков</h2>

            <div className="strategy-arc-selector">
              <label className="strategy-arc-selector__label">Энергетическая дуга:</label>
              <div className="strategy-arc-selector__options">
                {ARC_MODES.map((m) => (
                  <button
                    key={m.value}
                    className={`strategy-arc-option ${arcMode === m.value ? 'strategy-arc-option--active' : ''}`}
                    onClick={() => setArcMode(m.value)}
                  >
                    <span className="strategy-arc-option__label">{m.label}</span>
                    <span className="strategy-arc-option__desc">{m.desc}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="strategy-build__info">
              <Info size={14} />
              <span>{ARC_DESCS[arcMode]}</span>
            </div>
            <button
              className="btn btn--primary btn--lg"
              onClick={() => smartMutation.mutate()}
              disabled={smartMutation.isPending || analyzedTracks.length < 2}
            >
              {smartMutation.isPending ? (
                <><Loader2 className="spin" size={18} /> Анализ {analyzedTracks.length} треков…</>
              ) : (
                <><Zap size={18} /> Построить стратегию ({analyzedTracks.length} треков)</>
              )}
            </button>
            {smartMutation.isError && (
              <p className="strategy-build__error">Ошибка: {smartMutation.error.message}</p>
            )}
          </div>
        )}

        {plan && (
          <div className="strategy-results">
            <div className="strategy-results__header">
              <h2>План переходов · {ARC_MODES.find(m => m.value === arcMode)?.label || arcMode}</h2>
              <div className="strategy-results__actions">
                {/* Chain preview button */}
                <button
                  className={`btn btn--ghost ${chainPreviewing ? 'btn--active' : ''}`}
                  onClick={chainPreviewing ? stopChainPreview : previewChain}
                  title={chainPreviewing ? 'Остановить прослушку' : 'Прослушать все переходы по очереди'}
                >
                  {chainPreviewing ? <Pause size={14} /> : <SkipForward size={14} />}
                  {chainPreviewing ? 'Стоп' : 'Прослушать цепочку'}
                </button>
                {/* Render button */}
                <button
                  className="btn btn--primary"
                  onClick={renderChain}
                  disabled={rendering || renderJob?.status === 'RUNNING'}
                  title="Собрать полный микс из всех треков"
                >
                  {rendering || renderJob?.status === 'RUNNING' ? (
                    <><Loader2 className="spin" size={14} /> Рендер…</>
                  ) : (
                    <><Download size={14} /> Собрать микс</>
                  )}
                </button>
                <button className="btn btn--ghost" onClick={() => { setPlan(null); setTrackOrder([]); setExpandedIdx(null); setSelectedTechOverrides({}) }}>
                  <RotateCcw size={14} /> Начать заново
                </button>
              </div>
            </div>

            {/* Render progress */}
            {renderJob && renderJob.status === 'RUNNING' && (
              <div className="strategy-render-progress">
                <Loader2 className="spin" size={14} />
                <span>{renderJob.message || 'Рендеринг…'}</span>
                <div className="strategy-render-progress__bar">
                  <div className="strategy-render-progress__fill" style={{ width: `${(renderJob.progress || 0)}%` }} />
                </div>
                <span className="strategy-render-progress__pct">{Math.round((renderJob.progress || 0))}%</span>
              </div>
            )}

            {/* Real-time Web Audio player */}
            {trackOrder.length >= 2 && (
              <WebAudioPlayer
                tracks={trackOrder.map((name, i) => ({
                  id: `track-${i}`,
                  name,
                }))}
              />
            )}
            {renderJob && renderJob.status === 'COMPLETED' && (
              <div className="strategy-render-done">
                <span>Готово!</span>
                {renderJob.result && (renderJob.result as any).output_path && (
                  <a
                    className="btn btn--primary btn--sm"
                    href={`${BASE}/outputs/${encodeURIComponent((renderJob.result as any).output_path)}`}
                    download
                  >
                    <Download size={12} /> Скачать
                  </a>
                )}
              </div>
            )}

            {/* Energy Arc */}
            <div className="strategy-energy-arc">
              <h3>Энергетическая дуга</h3>
              <div className="strategy-energy-arc__bar">
                {structures.map((s, i) => {
                  const height = Math.max(8, (s.el / 5) * 100)
                  const color = EL_COLORS[s.el] || '#888'
                  return (
                    <div key={i} className="strategy-energy-arc__segment" style={{ height: `${height}%`, backgroundColor: color }}
                      title={`${s.song_name.slice(0, 30)} — EL ${s.el}`}
                    />
                  )
                })}
              </div>
              <div className="strategy-energy-arc__labels">
                <span>EL 1</span><span>EL 3</span><span>EL 5</span>
              </div>
            </div>

            {/* Track list (reorderable) */}
            <div className="strategy-track-list">
              <h3>Треки ({trackOrder.length})</h3>
              <div className="strategy-track-list__scroll">
                {trackOrder.map((name, idx) => {
                  const s = structures.find(st => st.song_name === name)
                  if (!s) return null
                  return (
                    <div key={name} className="strategy-track-row"
                      draggable
                      onDragStart={() => handleDragStart(idx)}
                      onDragEnter={() => handleDragEnter(idx)}
                      onDragEnd={handleDragEnd}
                      onDragOver={(e) => e.preventDefault()}
                    >
                      <span className="strategy-track-row__grip"><GripVertical size={14} /></span>
                      <span className="strategy-track-row__num">{idx + 1}</span>
                      <div className="strategy-track-row__info">
                        <span className="strategy-track-row__name">{name}</span>
                        <div className="strategy-track-row__meta">
                          <span>{s.bpm} BPM</span>
                          <span>{s.camelot}</span>
                          <span style={{ color: EL_COLORS[s.el] }}>EL {s.el}</span>
                          <span>{s.tempo_type}</span>
                        </div>
                      </div>
                      <div className="strategy-track-row__actions">
                        <button onClick={() => moveTrack(idx, -1)} disabled={idx === 0} title="Вверх">↑</button>
                        <button onClick={() => moveTrack(idx, 1)} disabled={idx === trackOrder.length - 1} title="Вниз">↓</button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Transitions */}
            <div className="strategy-transitions">
              <h3>Переходы ({transitions.length})</h3>
              {transitions.map((tr) => {
                const overridden = selectedTechOverrides[tr.pair_index]
                return (
                  <div key={tr.pair_index} className={`strategy-transition ${overridden ? 'strategy-transition--overridden' : ''}`}>
                    <div className="strategy-transition__header-row">
                      <div
                        className="strategy-transition__header"
                        onClick={() => {
                          const next = expandedIdx === tr.pair_index ? null : tr.pair_index
                          setExpandedIdx(next)
                          if (next !== null) loadAlts(next)
                        }}
                      >
                        <span className="strategy-transition__pair">
                          {tr.from_song.slice(0, 35)} <ChevronRight size={12} /> {tr.to_song.slice(0, 35)}
                        </span>
                        <div className="strategy-transition__badges">
                          <span className="badge badge--amber">{overridden || tr.technique_id}</span>
                          {overridden && <span className="badge badge--green">override</span>}
                          <span className="badge">{Math.round(tr.confidence * 100)}%</span>
                          <span className="badge">{tr.bpm_from}→{tr.bpm_to}</span>
                          <span className="badge">{tr.camelot_from}→{tr.camelot_to}</span>
                          <span className="badge" style={{ color: EL_COLORS[tr.el_from] }}>EL{tr.el_from}→{tr.el_to}</span>
                        </div>
                        {expandedIdx === tr.pair_index ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                      </div>
                      <button
                        className={`strategy-transition__preview ${previewingPair === tr.pair_index ? 'strategy-transition__preview--active' : ''}`}
                        onClick={(e) => {
                          e.stopPropagation()
                          if (previewingPair === tr.pair_index && audioRef.current) {
                            audioRef.current.pause()
                            audioRef.current = null
                            setPreviewingPair(null)
                            setPreviewJobId(null)
                          } else {
                            previewTransition(tr)
                          }
                        }}
                        title={previewingPair === tr.pair_index ? 'Остановить' : 'Прослушать переход'}
                      >
                        {previewingPair === tr.pair_index ? <Pause size={14} /> : <Play size={14} />}
                      </button>
                    </div>
                    {expandedIdx === tr.pair_index && (
                      <div className="strategy-transition__detail">
                        <div className="strategy-transition__detail-grid">
                          <div><strong>Техника:</strong> {tr.technique} ({overridden || tr.technique_id})</div>
                          <div><strong>Эффект:</strong> {tr.effect || 'нет'}</div>
                          <div><strong>Такты:</strong> {tr.transition_bars}</div>
                          <div><strong>BPM тип:</strong> {tr.tempo_type_from} → {tr.tempo_type_to}</div>
                          <div><strong>Правило:</strong> #{tr.priority_rule} — {RULE_NAMES[tr.priority_rule] || 'авто'}</div>
                        </div>
                        <p className="strategy-transition__reason">{tr.reason}</p>

                        {/* Alternatives — now selectable */}
                        <div className="strategy-transition__alts">
                          <h4>Альтернативные техники (нажмите чтобы выбрать):</h4>
                          {loadingAlts && altsForPair === tr.pair_index ? (
                            <Loader2 size={14} className="spin" />
                          ) : altsForPair === tr.pair_index && alternatives.length > 0 ? (
                            <div className="strategy-alts-list">
                              {alternatives.map((alt) => {
                                const isActive = (overridden || tr.technique_id) === alt.technique_id
                                return (
                                  <button
                                    key={alt.technique_id}
                                    className={`strategy-alt ${isActive ? 'strategy-alt--selected' : ''}`}
                                    onClick={() => selectAlternative(tr.pair_index, alt.technique_id)}
                                    title={`Выбрать ${alt.name}`}
                                  >
                                    <span className="strategy-alt__id">{alt.technique_id}</span>
                                    <span className="strategy-alt__name">{alt.name}</span>
                                    <span className="strategy-alt__conf">{Math.round(alt.confidence * 100)}%</span>
                                    <span className="strategy-alt__rule">{alt.rule}</span>
                                    {isActive && <span className="strategy-alt__check">✓</span>}
                                  </button>
                                )
                              })}
                            </div>
                          ) : null}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
