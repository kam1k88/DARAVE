/* ============================================================
   AI RemixMate — Mix Plan Page
   Visual plan for a DJ mix with per-transition recommendations.
   ============================================================ */

import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Map,
  X,
  Loader2,
  RefreshCw,
  GitBranch,
  BarChart3,
  Clock,
  Zap,
} from 'lucide-react'
import { libraryApi, remixApi } from '@/lib/api'
import { EnergyArcChart } from '@/components/EnergyArcChart'
import { TransitionCard } from '@/components/TransitionCard'
import type { SongInfo } from '@/types'
import './PageBase.css'
import './MixPlanPage.css'

export default function MixPlanPage() {
  const [selectedSongs, setSelectedSongs] = useState<string[]>([])
  const [transitionBars, setTransitionBars] = useState(16)

  const { data: songs = [] } = useQuery({
    queryKey: ['library-list'],
    queryFn: () => libraryApi.list(),
  })

  const {
    data: plan,
    isLoading: loadingPlan,
    refetch: refetchPlan,
    isFetching,
  } = useQuery({
    queryKey: ['mix-plan', selectedSongs, transitionBars],
    queryFn: () => remixApi.plan(selectedSongs, transitionBars),
    enabled: selectedSongs.length >= 2,
    staleTime: 30_000,
  })

  const availableSongs = useMemo(() => {
    const selectedSet = new Set(selectedSongs)
    return songs.filter((s: SongInfo) => !selectedSet.has(s.name))
  }, [songs, selectedSongs])

  function addSong(name: string) {
    setSelectedSongs((prev) => [...prev, name])
  }

  function removeSong(name: string) {
    setSelectedSongs((prev) => prev.filter((s) => s !== name))
  }

  function moveSong(idx: number, dir: -1 | 1) {
    setSelectedSongs((prev) => {
      const next = [...prev]
      const newIdx = idx + dir
      if (newIdx < 0 || newIdx >= next.length) return prev
      ;[next[idx], next[newIdx]] = [next[newIdx], next[idx]]
      return next
    })
  }

  const totalDuration = plan ? `${Math.round(plan.total_duration_sec / 60)}m ${Math.round(plan.total_duration_sec % 60)}s` : '—'
  const avgConf = plan ? `${Math.round(plan.avg_confidence * 100)}%` : '—'

  return (
    <div className="page-base">
      <header className="page-base__header">
        <Map size={20} className="page-base__header-icon" />
        <div>
          <h1 className="page-base__title">Mix Plan</h1>
          <p className="page-base__sub" style={{ color: 'var(--color-text-faint)' }}>
            Visual plan with per-transition technique recommendations
          </p>
        </div>
      </header>

      <div className="page-base__body mp-body">
        {/* Song selector */}
        <section className="mp-section">
          <h2 className="mp-section__title">
            <BarChart3 size={14} /> Track List
          </h2>

          <div className="mp-tracks">
            {selectedSongs.length === 0 && (
              <div className="mp-empty">
                Add songs from the library to start planning your mix
              </div>
            )}
            {selectedSongs.map((name, idx) => {
              const song = songs.find((s: SongInfo) => s.name === name)
              return (
                <div key={name} className="mp-track">
                  <span className="mp-track__idx">{idx + 1}</span>
                  <div className="mp-track__info">
                    <span className="mp-track__name">{name}</span>
                    <span className="mp-track__meta">
                      {song?.bpm ? `${Math.round(song.bpm)} BPM` : '—'}
                      {song?.camelot ? ` · ${song.camelot}` : ''}
                    </span>
                  </div>
                  <div className="mp-track__actions">
                    <button
                      className="mp-track__btn"
                      onClick={() => moveSong(idx, -1)}
                      disabled={idx === 0}
                      title="Move up"
                    >↑</button>
                    <button
                      className="mp-track__btn"
                      onClick={() => moveSong(idx, 1)}
                      disabled={idx === selectedSongs.length - 1}
                      title="Move down"
                    >↓</button>
                    <button
                      className="mp-track__btn mp-track__btn--remove"
                      onClick={() => removeSong(name)}
                      title="Remove"
                    ><X size={12} /></button>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Add song dropdown */}
          {availableSongs.length > 0 && (
            <div className="mp-add-row">
              <select
                className="mp-add-select"
                value=""
                onChange={(e) => {
                  if (e.target.value) addSong(e.target.value)
                }}
              >
                <option value="">+ Add song…</option>
                {availableSongs.map((s: SongInfo) => (
                  <option key={s.name} value={s.name}>
                    {s.name} {s.bpm ? `(${Math.round(s.bpm)} BPM)` : ''}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Settings */}
          <div className="mp-settings">
            <label className="mp-settings__label">
              Transition bars:
              <select
                className="mp-settings__select"
                value={transitionBars}
                onChange={(e) => setTransitionBars(Number(e.target.value))}
              >
                <option value={8}>8</option>
                <option value={16}>16</option>
                <option value={32}>32</option>
                <option value={64}>64</option>
              </select>
            </label>
          </div>
        </section>

        {/* Plan results */}
        {selectedSongs.length >= 2 && (
          <>
            {loadingPlan || isFetching ? (
              <div className="mp-loading">
                <Loader2 size={24} className="mp-spin" />
                <span>Analyzing tracks and planning transitions…</span>
              </div>
            ) : plan ? (
              <>
                {/* Summary bar */}
                <section className="mp-summary">
                  <div className="mp-summary__item">
                    <Clock size={12} />
                    <span>{totalDuration}</span>
                  </div>
                  <div className="mp-summary__item">
                    <Zap size={12} />
                    <span>{plan.transitions.length} transitions</span>
                  </div>
                  <div className="mp-summary__item">
                    <BarChart3 size={12} />
                    <span>Avg confidence: {avgConf}</span>
                  </div>
                  <button
                    className="mp-summary__refresh"
                    onClick={() => refetchPlan()}
                    disabled={isFetching}
                  >
                    <RefreshCw size={12} />
                  </button>
                </section>

                {/* Energy arc */}
                <section className="mp-section">
                  <h2 className="mp-section__title">
                    <BarChart3 size={14} /> Energy Arc
                  </h2>
                  <EnergyArcChart data={plan.energy_arc} height={180} />
                </section>

                {/* Track order + structures */}
                <section className="mp-section">
                  <h2 className="mp-section__title">
                    <GitBranch size={14} /> Track Analysis
                  </h2>
                  <div className="mp-struct-grid">
                    {plan.structures.map((s, i) => (
                      <div key={s.song_name} className="mp-struct-card">
                        <div className="mp-struct-card__idx">{i + 1}</div>
                        <div className="mp-struct-card__info">
                          <span className="mp-struct-card__name">{s.song_name}</span>
                          <span className="mp-struct-card__meta">
                            {s.bpm} BPM · {s.camelot} · {s.key}
                          </span>
                          <span className="mp-struct-card__energy">
                            Energy: {Math.round(s.energy_mean * 100)}%
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>

                {/* Transition cards */}
                <section className="mp-section">
                  <h2 className="mp-section__title">
                    <Zap size={14} /> Transition Plan
                  </h2>
                  <div className="mp-transitions">
                    {plan.transitions.map((t, i) => (
                      <TransitionCard
                        key={t.pair_index}
                        transition={t}
                        isLast={i === plan.transitions.length - 1}
                      />
                    ))}
                  </div>
                </section>
              </>
            ) : null}
          </>
        )}
      </div>
    </div>
  )
}
