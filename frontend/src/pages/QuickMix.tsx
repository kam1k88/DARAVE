/* ============================================================
   AI RemixMate — Quick Mix
   Unified page: select tracks → pick techniques → mix.
   ============================================================ */
import { useState, useMemo, useCallback } from 'react'
import {
  Zap,
  Music2,
  Play,
  Loader2,
  ChevronRight,
  Trash2,
  GripVertical,
  Sparkles,
  ArrowDown,
  ArrowUp,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { libraryApi, techniquesApi, patternSearchApi, quickMixApi } from '@/lib/api'
import type { DJTechnique, PatternSearchTrack } from '@/types'
import './PageBase.css'
import './QuickMix.css'

const LEVEL_COLORS: Record<string, string> = {
  beginner:      'var(--color-green-500)',
  intermediate:  'var(--color-amber-500)',
  advanced:      'var(--color-crimson-500)',
  experimental:  'var(--color-violet-400)',
}

const LEVEL_LABELS: Record<string, string> = {
  beginner:      'Новичок',
  intermediate:  'Средний',
  advanced:      'Продвинутый',
  experimental:  'Экспериментальный',
}

const CATEGORY_ICONS: Record<string, string> = {
  cut:       '✂️',
  eq:        '🎛️',
  filter:    '🔊',
  echo:      '📻',
  combo:     '🔗',
  loop:      '🔁',
  effect:    '✨',
  stem:      '🎤',
  structural:'📐',
  ambient:   '🌊',
  pitch:     '🎵',
}

export default function QuickMix() {
  // Track selection state
  const [selectedTracks, setSelectedTracks] = useState<string[]>([])

  // Technique selection per transition
  const [techniquePerTransition, setTechniquePerTransition] = useState<Record<number, string>>({})

  // UI state
  const [expandedTech, setExpandedTech] = useState<string | null>(null)
  const [previewingPair, setPreviewingPair] = useState<{ a: string; b: string } | null>(null)

  // Fetch songs with full info
  const { data: songs = [] } = useQuery({
    queryKey: ['library'],
    queryFn: () => libraryApi.list(),
  })

  // Fetch techniques
  const { data: techniques = [] } = useQuery({
    queryKey: ['techniques'],
    queryFn: () => techniquesApi.list(),
  })

  // Pattern search for selected technique
  const [searchTechId, setSearchTechId] = useState<string | null>(null)
  const { data: searchResults, isLoading: searching } = useQuery({
    queryKey: ['pattern-search', searchTechId],
    queryFn: () => patternSearchApi.search(searchTechId!),
    enabled: !!searchTechId,
  })

  // Track selection
  const addTrack = useCallback((name: string) => {
    if (!selectedTracks.includes(name)) {
      setSelectedTracks((prev) => [...prev, name])
    }
  }, [selectedTracks])

  const removeTrack = useCallback((idx: number) => {
    setSelectedTracks((prev) => prev.filter((_, i) => i !== idx))
    // Clean up technique selections
    setTechniquePerTransition((prev) => {
      const next: Record<number, string> = {}
      for (const [k, v] of Object.entries(prev)) {
        const ki = parseInt(k)
        if (ki < idx) next[ki] = v
        else if (ki > idx) next[ki - 1] = v
      }
      return next
    })
  }, [])

  const moveTrack = useCallback((from: number, to: number) => {
    if (to < 0 || to >= selectedTracks.length) return
    setSelectedTracks((prev) => {
      const next = [...prev]
      const [item] = next.splice(from, 1)
      next.splice(to, 0, item)
      return next
    })
  }, [selectedTracks])

  // Available songs not yet selected
  const availableSongs = useMemo(
    () => songs.filter((s) => !selectedTracks.includes(s.name)),
    [songs, selectedTracks],
  )

  // Transitions
  const transitions = useMemo(() => {
    if (selectedTracks.length < 2) return []
    return selectedTracks.slice(0, -1).map((from, i) => ({
      from,
      to: selectedTracks[i + 1],
      index: i,
      techniqueId: techniquePerTransition[i] || null,
    }))
  }, [selectedTracks, techniquePerTransition])

  // Set technique for a transition
  const setTransitionTechnique = useCallback((idx: number, techId: string) => {
    setTechniquePerTransition((prev) => ({ ...prev, [idx]: techId }))
  }, [])

  // Preview
  const handlePreview = useCallback(async (songA: string, songB: string, techId: string) => {
    setPreviewingPair({ a: songA, b: songB })
    try {
      await quickMixApi.preview(songA, songB, techId || 'DNB-04')
    } catch {
      // Preview job created, poll for completion
    }
    setTimeout(() => setPreviewingPair(null), 3000)
  }, [])

  // Full mix
  const [mixing, setMixing] = useState(false)
  const handleMix = useCallback(async () => {
    if (selectedTracks.length < 2) return
    setMixing(true)
    try {
      const techIds = selectedTracks.slice(0, -1).map((_, i) => techniquePerTransition[i] || null)
      await quickMixApi.mix(selectedTracks, techIds)
    } catch {
      // Job created
    }
    setTimeout(() => setMixing(false), 2000)
  }, [selectedTracks, techniquePerTransition])

  // Find track info
  const findSong = (name: string) => songs.find((s) => s.name === name)

  return (
    <div className="page-base">
      <div className="page-base__header">
        <Zap size={20} className="page-base__header-icon" />
        <div>
          <div className="page-base__title">Quick Mix</div>
          <div className="page-base__sub" style={{ color: 'var(--color-text-secondary)' }}>
            Выбери треки, техники, смешай
          </div>
        </div>
      </div>

      <div className="page-base__body">
        <div className="qm-layout">

          {/* ── Left: Track Selector ── */}
          <div className="qm-panel qm-tracks-panel">
            <div className="qm-panel__header">
              <Music2 size={16} />
              <span>Треки ({selectedTracks.length})</span>
            </div>

            {/* Selected tracks */}
            <div className="qm-selected-tracks">
              {selectedTracks.length === 0 && (
                <div className="qm-empty">
                  <ArrowDown size={20} />
                  <span>Добавь треки из списка ниже</span>
                </div>
              )}
              {selectedTracks.map((name, idx) => {
                const info = findSong(name)
                return (
                  <div key={`${name}-${idx}`} className="qm-track-card">
                    <GripVertical size={14} className="qm-grip" />
                    <span className="qm-track-num">{idx + 1}</span>
                    <div className="qm-track-info">
                      <span className="qm-track-name">{name}</span>
                      {info && (
                        <span className="qm-track-meta">
                          {info.bpm ? `${Math.round(info.bpm)} BPM` : ''}
                          {info.camelot ? ` · ${info.camelot}` : ''}
                          {info.energy != null ? ` · E:${(info.energy * 10).toFixed(0)}` : ''}
                        </span>
                      )}
                    </div>
                    <div className="qm-track-actions">
                      <button
                        onClick={() => moveTrack(idx, idx - 1)}
                        disabled={idx === 0}
                        className="qm-btn-icon"
                        title="Move up"
                      >
                        <ArrowUp size={12} />
                      </button>
                      <button
                        onClick={() => moveTrack(idx, idx + 1)}
                        disabled={idx === selectedTracks.length - 1}
                        className="qm-btn-icon"
                        title="Move down"
                      >
                        <ArrowDown size={12} />
                      </button>
                      <button
                        onClick={() => removeTrack(idx)}
                        className="qm-btn-icon qm-btn-icon--danger"
                        title="Remove"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>

            {/* Available songs */}
            <div className="qm-available">
              <div className="qm-available__header">Библиотека</div>
              <div className="qm-available__list">
              {availableSongs.map((s) => (
                    <button
                      key={s.name}
                      onClick={() => addTrack(s.name)}
                      className="qm-available__item"
                    >
                      <span className="qm-available__name">{s.name}</span>
                      {s.bpm && (
                        <span className="qm-available__bpm">{Math.round(s.bpm)}</span>
                      )}
                    </button>
                  ))}
              </div>
            </div>
          </div>

          {/* ── Center: Transitions + Techniques ── */}
          <div className="qm-panel qm-mix-panel">
            <div className="qm-panel__header">
              <Sparkles size={16} />
              <span>Переходы ({transitions.length})</span>
            </div>

            {transitions.length === 0 && (
              <div className="qm-empty">
                <Sparkles size={20} />
                <span>Добавь минимум 2 трека</span>
              </div>
            )}

            {transitions.map((tr) => (
              <div key={tr.index} className="qm-transition">
                <div className="qm-transition__pair">
                  <span className="qm-transition__song qm-transition__song--from">{tr.from}</span>
                  <ChevronRight size={16} className="qm-transition__arrow" />
                  <span className="qm-transition__song qm-transition__song--to">{tr.to}</span>
                </div>

                {/* Technique selector */}
                <div className="qm-tech-selector">
                  <select
                    value={tr.techniqueId || ''}
                    onChange={(e) => setTransitionTechnique(tr.index, e.target.value)}
                    className="qm-tech-select"
                  >
                    <option value="">Авто (AI)</option>
                    {techniques.map((t: DJTechnique) => (
                      <option key={t.id} value={t.id}>
                        {t.id} — {t.name} ({LEVEL_LABELS[t.level] || t.level})
                      </option>
                    ))}
                  </select>

                  <button
                    onClick={() => handlePreview(tr.from, tr.to, tr.techniqueId || '')}
                    disabled={previewingPair?.a === tr.from && previewingPair?.b === tr.to}
                    className="qm-btn-preview"
                    title="Preview transition"
                  >
                    {previewingPair?.a === tr.from && previewingPair?.b === tr.to ? (
                      <Loader2 size={14} className="spin" />
                    ) : (
                      <Play size={14} />
                    )}
                    Preview
                  </button>
                </div>

                {/* Selected technique details */}
                {tr.techniqueId && (() => {
                  const tech = techniques.find((t: DJTechnique) => t.id === tr.techniqueId)
                  if (!tech) return null
                  return (
                    <div className="qm-tech-detail">
                      <div className="qm-tech-detail__row">
                        <span
                          className="qm-tech-badge"
                          style={{ color: LEVEL_COLORS[tech.level] || 'var(--color-text-secondary)' }}
                        >
                          {tech.level}
                        </span>
                        <span className="qm-tech-category">
                          {CATEGORY_ICONS[tech.category] || '🎵'} {tech.category}
                        </span>
                        <span className="qm-tech-bars">{tech.transition_bars} bars</span>
                      </div>
                      <div className="qm-tech-detail__desc">{tech.description}</div>
                      <div className="qm-tech-detail__effects">
                        {tech.effects_used.map((e) => (
                          <span key={e} className="qm-effect-tag">{e}</span>
                        ))}
                      </div>
                    </div>
                  )
                })()}
              </div>
            ))}

            {/* Mix button */}
            {selectedTracks.length >= 2 && (
              <button
                onClick={handleMix}
                disabled={mixing}
                className="qm-btn-mix"
              >
                {mixing ? (
                  <>
                    <Loader2 size={16} className="spin" />
                    Создаём микс...
                  </>
                ) : (
                  <>
                    <Zap size={16} />
                    Смешать ({selectedTracks.length} треков)
                  </>
                )}
              </button>
            )}
          </div>

          {/* ── Right: Technique Catalog ── */}
          <div className="qm-panel qm-catalog-panel">
            <div className="qm-panel__header">
              <Zap size={16} />
              <span>Каталог техник ({techniques.length})</span>
            </div>

            <div className="qm-catalog-list">
              {techniques.map((tech: DJTechnique) => (
                <div
                  key={tech.id}
                  className={`qm-catalog-card ${expandedTech === tech.id ? 'qm-catalog-card--expanded' : ''}`}
                  onClick={() => setExpandedTech(expandedTech === tech.id ? null : tech.id)}
                >
                  <div className="qm-catalog-card__header">
                    <span className="qm-catalog-card__id">{tech.id}</span>
                    <span className="qm-catalog-card__name">{tech.name}</span>
                    <span
                      className="qm-catalog-card__level"
                      style={{ color: LEVEL_COLORS[tech.level] }}
                    >
                      {tech.level}
                    </span>
                  </div>

                  {expandedTech === tech.id && (
                    <div className="qm-catalog-card__body">
                      <p>{tech.description}</p>
                      <div className="qm-catalog-card__meta">
                        <span>Лучше для: {tech.best_for}</span>
                        <span>BPM: {tech.bpm_range[0]}–{tech.bpm_range[1]}</span>
                        <span>Энергия: {tech.energy_delta}</span>
                        <span>Ключи: {tech.key_compatibility}</span>
                      </div>
                      <div className="qm-catalog-card__effects">
                        Эффекты: {tech.effects_used.join(', ')}
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          setSearchTechId(tech.id)
                        }}
                        className="qm-btn-search"
                      >
                        Найти треки для этой техники
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* Pattern search results */}
            {searchResults && (
              <div className="qm-search-results">
                <div className="qm-search-results__header">
                  Треки для {searchResults.technique_name} ({searchResults.tracks.length})
                </div>
                {searchResults.tracks.map((t: PatternSearchTrack) => (
                  <div key={t.name} className="qm-search-track">
                    <span className="qm-search-track__name">{t.name}</span>
                    <span className="qm-search-track__bpm">{Math.round(t.bpm)}</span>
                    {t.camelot && <span className="qm-search-track__key">{t.camelot}</span>}
                    <span className="qm-search-track__score">
                      {Math.round(t.score * 100)}%
                    </span>
                    <button
                      onClick={() => addTrack(t.name)}
                      className="qm-btn-icon"
                      title="Add to mix"
                    >
                      <Music2 size={12} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            {searching && (
              <div className="qm-search-loading">
                <Loader2 size={16} className="spin" />
                Поиск...
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
