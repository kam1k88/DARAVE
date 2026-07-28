/* ============================================================
   DARAVE — Mix Deck
   Dual-deck DJ UI with turntables, waveform, sampler, stem
   mixing, EQ, effects, crossfader, VU meters, library filters.
   ============================================================ */
import { useState, useMemo, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Sliders,
  Music2,
  Zap,
  Play,
  GitMerge,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Loader2,
  Upload,
  Disc3,
  PanelLeftOpen,
  PanelLeftClose,
} from 'lucide-react'
import { libraryApi, analysisApi, remixApi } from '@/lib/api'
import { useAppStore } from '@/stores/appStore'
import { useWebAudio } from '@/hooks/useWebAudio'
import { TransitionTimeline } from '@/components/TransitionTimeline'
import { RemixControls, type RemixOptions, REMIX_DEFAULTS } from '@/components/RemixControls'
import { CamelotWheel } from '@/components/CamelotWheel'
import { StemMixer } from '@/components/StemMixer'
import EQKnobs from '@/components/EQKnobs'
import Crossfader from '@/components/Crossfader'
import VUMeter from '@/components/VUMeter'
import TransportControls from '@/components/TransportControls'
import EffectsRack from '@/components/EffectsRack'
import Turntable from '@/components/Turntable'
import WaveformTimeline from '@/components/WaveformTimeline'
import Sampler from '@/components/Sampler'
import LibraryFilterPanel from '@/components/LibraryFilterPanel'
import type { SongInfo, CompatibilityResult, SimilarTrack } from '@/types'
import './PageBase.css'
import './MixDeck.css'
import '@/components/Turntable.css'
import '@/components/WaveformTimeline.css'
import '@/components/Sampler.css'
import '@/components/LibraryFilterPanel.css'

const DECK_HEX: Record<'A' | 'B', string> = {
  A: '#f59e0b',
  B: '#38bdf8',
}

const STEMS = ['drums', 'bass', 'other', 'vocals']

// ── Sub-components ──────────────────────────────────────────────

function DeckHeader({
  label,
  song,
  songInfo,
  songs,
  suggestedSongs,
  onChange,
  loadingSongInfo,
}: {
  label: 'A' | 'B'
  song: string
  songInfo?: SongInfo
  songs: string[]
  suggestedSongs?: string[]
  onChange: (v: string) => void
  loadingSongInfo?: boolean
}) {
  return (
    <div className="md-deck__header">
      <div className="md-deck__label font-display" style={{ color: DECK_HEX[label] }}>
        Deck {label}
      </div>

      <div className="md-select-wrap">
        <Music2 size={13} className="md-select-icon" />
        <select
          className="md-select"
          value={song}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">— select a track —</option>
          {suggestedSongs && suggestedSongs.length > 0 ? (
            <>
              <optgroup label="✦ Similar to Deck A">
                {suggestedSongs.map((n) => (
                  <option key={`sug-${n}`} value={n}>{n}</option>
                ))}
              </optgroup>
              <optgroup label="All tracks">
                {songs.filter((n) => !suggestedSongs.includes(n)).map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </optgroup>
            </>
          ) : (
            songs.map((n) => <option key={n} value={n}>{n}</option>)
          )}
        </select>
      </div>

      {song ? (
        <div className="md-deck__meta">
          <div className="md-meta-row">
            <span className="md-meta-key text-muted">BPM</span>
            <span className="md-meta-val font-mono">
              {loadingSongInfo && !songInfo?.bpm
                ? <Loader2 size={11} className="md-spin" />
                : songInfo?.bpm?.toFixed(1) ?? '—'}
            </span>
          </div>
          <div className="md-meta-row">
            <span className="md-meta-key text-muted">Key</span>
            <span className="md-meta-val font-mono">
              {loadingSongInfo && !songInfo?.key
                ? <Loader2 size={11} className="md-spin" />
                : (
                  <>
                    {songInfo?.key ?? '—'}
                    {songInfo?.camelot && (
                      <span className="md-camelot">{songInfo.camelot}</span>
                    )}
                  </>
                )}
            </span>
          </div>
          <div className="md-meta-row">
            <span className="md-meta-key text-muted">Energy</span>
            <span className="md-meta-val font-mono">
              {loadingSongInfo && songInfo?.energy == null
                ? <Loader2 size={11} className="md-spin" />
                : songInfo?.energy != null
                  ? (
                    <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      {Math.round(songInfo.energy * 100)}
                      <span className="md-energy-bar">
                        <span
                          className="md-energy-fill"
                          style={{ width: `${Math.round(songInfo.energy * 100)}%` }}
                        />
                      </span>
                    </span>
                  )
                  : '—'}
            </span>
          </div>
          <div className="md-meta-row">
            <span className="md-meta-key text-muted">Stems</span>
            <span className={`md-meta-val font-mono ${songInfo?.has_stems ? 'md-green' : 'md-muted'}`}>
              {songInfo?.has_stems ? 'yes' : 'no'}
            </span>
          </div>
        </div>
      ) : (
        <div className="md-deck__empty text-muted">
          No track loaded
        </div>
      )}
    </div>
  )
}

function CompatPanel({ result }: { result: CompatibilityResult }) {
  const pct        = Math.round(result.overall * 100)
  const keyPct     = Math.round(result.key_score * 100)
  const tempoRatio = result.bpm_a && result.bpm_b ? result.bpm_b / result.bpm_a : 1.0
  const tempoOk    = tempoRatio >= 0.9 && tempoRatio <= 1.1

  const color =
    pct >= 80 ? 'var(--color-green-500)' :
    pct >= 60 ? 'var(--color-amber-500)' :
                'var(--color-crimson-500)'

  const verdict = result.compatible
    ? 'These tracks blend well — good harmonic and tempo match.'
    : 'Significant mismatch — mix with caution or adjust tempo.'

  return (
    <div className="md-compat">
      <div className="md-compat__score" style={{ '--score-color': color } as React.CSSProperties}>
        <span className="md-compat__pct font-display">{pct}</span>
        <span className="md-compat__unit text-muted">/ 100</span>
      </div>

      <p className="md-compat__verdict">{verdict}</p>

      <div className="md-compat__rows">
        <div className="md-compat__row">
          <span className="text-muted">Harmonic (key)</span>
          <div className="md-compat__bar">
            <div
              className="md-compat__fill"
              style={{ width: `${keyPct}%`, background: 'var(--color-ice-400)' }}
            />
          </div>
          <span className="font-mono" style={{ fontSize: 'var(--text-xs)' }}>{keyPct}</span>
        </div>
        <div className="md-compat__row">
          <span className="text-muted">Tempo ratio</span>
          <span
            className="font-mono"
            style={{ fontSize: 'var(--text-xs)', color: tempoOk ? 'var(--color-green-500)' : 'var(--color-amber-500)' }}
          >
            {tempoRatio.toFixed(3)}
          </span>
        </div>
        <div className="md-compat__row">
          <span className="text-muted">Camelot</span>
          <span className="font-mono" style={{ fontSize: 'var(--text-xs)' }}>
            {result.camelot_a ?? '?'} → {result.camelot_b ?? '?'}
          </span>
        </div>
        {result.bpm_a !== undefined && result.bpm_b !== undefined && (
          <div className="md-compat__row">
            <span className="text-muted">BPM</span>
            <span className="font-mono" style={{ fontSize: 'var(--text-xs)' }}>
              {result.bpm_a.toFixed(1)} → {result.bpm_b.toFixed(1)}
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

function JobStatusBadge({ jobId }: { jobId: string }) {
  const job = useAppStore((s) => s.jobs[jobId])
  if (!job) return null

  const { color, icon: Icon, spin } = (() => {
    switch (job.status) {
      case 'RUNNING':   return { color: 'var(--color-amber-500)', icon: Loader2, spin: true }
      case 'COMPLETED': return { color: 'var(--color-green-500)', icon: CheckCircle2, spin: false }
      case 'FAILED':    return { color: 'var(--color-crimson-500)', icon: AlertTriangle, spin: false }
      default:          return { color: 'var(--color-text-muted)', icon: Clock, spin: false }
    }
  })()

  return (
    <div className="md-job-badge">
      <Icon size={13} style={{ color }} className={spin ? 'md-spin' : ''} />
      <span style={{ color, fontSize: 'var(--text-xs)', fontFamily: 'var(--font-mono)' }}>
        {job.status === 'RUNNING' ? `${job.progress}% — ${job.message}` : job.status.toLowerCase()}
      </span>
      {job.status === 'COMPLETED' && job.result && (job.result as { stream_url?: string }).stream_url && (
        <a
          className="md-job-link"
          href={`${import.meta.env.VITE_API_BASE || '/api'}${(job.result as { stream_url?: string }).stream_url}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          <Play size={11} /> Listen
        </a>
      )}
    </div>
  )
}

function RemixResultCard({ job }: { job: import('@/types').Job }) {
  const r = job.result as {
    stream_url?: string
    lufs?: number
    duration?: number
    bpm_a?: number
    bpm_b?: number
    harmonic_score?: number
    tempo_ratio?: number
  } | undefined
  if (!r) return null
  const BASE = import.meta.env.VITE_API_BASE || '/api'
  const audioUrl = r.stream_url ? `${BASE}${r.stream_url}` : null

  return (
    <div className="md-result-card">
      <div className="md-result-card__header">
        <CheckCircle2 size={14} className="md-result-card__icon" />
        <span className="font-display">Mix Ready</span>
        {audioUrl && (
          <a className="md-result-card__vault-link" href="/mix-vault">
            Open in Vault →
          </a>
        )}
      </div>
      <div className="md-result-card__stats">
        {r.lufs !== undefined && (
          <><span className="text-muted">LUFS</span><span className="font-mono">{r.lufs.toFixed(1)}</span></>
        )}
        {r.duration !== undefined && (
          <><span className="text-muted">Duration</span><span className="font-mono">{Math.floor(r.duration / 60)}:{String(Math.floor(r.duration % 60)).padStart(2, '0')}</span></>
        )}
        {r.tempo_ratio !== undefined && (
          <><span className="text-muted">Tempo ratio</span><span className="font-mono">{r.tempo_ratio.toFixed(3)}</span></>
        )}
        {r.harmonic_score !== undefined && (
          <><span className="text-muted">Key match</span><span className="font-mono">{Math.round(r.harmonic_score * 100)}%</span></>
        )}
      </div>
      {audioUrl && (
        <audio controls src={audioUrl} className="md-result-card__player" />
      )}
    </div>
  )
}

// ── Main MixDeck page ──────────────────────────────────────────

export default function MixDeck() {
  const [searchParams, setSearchParams] = useSearchParams()

  const [songA, setSongA]           = useState(searchParams.get('song_a') ?? '')
  const [songB, setSongB]           = useState(searchParams.get('song_b') ?? '')
  const [compat, setCompat]         = useState<CompatibilityResult | null>(null)
  const [compatLoading, setCompatLoading] = useState(false)
  const [compatError, setCompatError]     = useState<string | null>(null)
  const [transitionDuration, setTransitionDuration] = useState(32)
  const [targetBpm, setTargetBpm]   = useState('')
  const [remixOpts, setRemixOpts]       = useState<RemixOptions>(REMIX_DEFAULTS)
  const [previewJobId, setPreviewJobId] = useState<string | null>(null)
  const [remixJobId, setRemixJobId]     = useState<string | null>(null)
  const [submitting, setSubmitting]     = useState(false)
  const [dragOverDeck, setDragOverDeck] = useState<'A' | 'B' | null>(null)
  const [analyzing, setAnalyzing] = useState<string | null>(null)

  // Audio state
  const [volumeA, setVolumeA] = useState(1)
  const [volumeB, setVolumeB] = useState(1)
  const [effectA, setEffectA] = useState('none')
  const [effectB, setEffectB] = useState('none')
  const [loadedTracks, setLoadedTracks] = useState<{ a: boolean; b: boolean }>({ a: false, b: false })

  // New UI state
  const [showLibrary, setShowLibrary] = useState(false)
  const [cuePointsA, setCuePointsA] = useState<{ id: number; time: number; color: string; label?: string }[]>([])
  const [cuePointsB, setCuePointsB] = useState<{ id: number; time: number; color: string; label?: string }[]>([])
  const cueIdRef = useRef(0)

  const upsertJob  = useAppStore((s) => s.upsertJob)
  const remixJob   = useAppStore((s) => remixJobId ? s.jobs[remixJobId] : null)

  // Audio engine
  const audio = useWebAudio()

  // Clear URL params after consuming them
  useEffect(() => {
    if (searchParams.get('song_a') || searchParams.get('song_b')) {
      setSearchParams({}, { replace: true })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const { data: songs = [] } = useQuery<SongInfo[]>({
    queryKey: ['library-atlas'],
    queryFn: libraryApi.list,
    staleTime: 60_000,
  })

  const { data: detailA, isLoading: loadingA, refetch: refetchA } = useQuery<SongInfo>({
    queryKey: ['song-detail', songA],
    queryFn: () => libraryApi.get(songA),
    enabled: !!songA,
    staleTime: 120_000,
  })
  const { data: detailB, isLoading: loadingB, refetch: refetchB } = useQuery<SongInfo>({
    queryKey: ['song-detail', songB],
    queryFn: () => libraryApi.get(songB),
    enabled: !!songB,
    staleTime: 120_000,
  })

  const { data: similarTracksA = [] } = useQuery<SimilarTrack[]>({
    queryKey: ['similar', songA],
    queryFn: () => analysisApi.similar(songA, 8),
    enabled: !!songA,
    staleTime: 300_000,
  })

  const songNames  = useMemo(() => songs.map((s) => s.name).sort(), [songs])
  const infoA      = useMemo(() => songs.find((s) => s.name === songA), [songs, songA])
  const infoB      = useMemo(() => songs.find((s) => s.name === songB), [songs, songB])
  const songInfoA = detailA ?? infoA
  const songInfoB = detailB ?? infoB

  const similarNamesForB = useMemo(
    () => similarTracksA.map((t) => t.name).filter((n) => n !== songA),
    [similarTracksA, songA],
  )
  const canCompare = songA && songB && songA !== songB

  // ── Load tracks into audio engine when selected ──
  useEffect(() => {
    if (!songA) {
      setLoadedTracks((p) => ({ ...p, a: false }))
      return
    }
    let cancelled = false
    audio.loadTrack('deckA', songA).then(() => {
      if (!cancelled) setLoadedTracks((p) => ({ ...p, a: true }))
    })
    return () => { cancelled = true }
  }, [songA]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!songB) {
      setLoadedTracks((p) => ({ ...p, b: false }))
      return
    }
    let cancelled = false
    audio.loadTrack('deckB', songB).then(() => {
      if (!cancelled) setLoadedTracks((p) => ({ ...p, b: true }))
    })
    return () => { cancelled = true }
  }, [songB]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Drag & Drop ──
  const handleDragOver = useCallback((e: React.DragEvent, deck: 'A' | 'B') => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDragOverDeck(deck)
  }, [])

  const handleDragLeave = useCallback(() => {
    setDragOverDeck(null)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent, deck: 'A' | 'B') => {
    e.preventDefault()
    setDragOverDeck(null)
    const songName = e.dataTransfer.getData('text/plain')
    if (songName) {
      if (deck === 'A') setSongA(songName)
      else setSongB(songName)
      setCompat(null)
    }
  }, [])

  // ── File drop from OS ──
  const handleFileDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    if (e.dataTransfer.types.includes('Files')) {
      e.dataTransfer.dropEffect = 'copy'
    }
  }, [])

  const addCuePoint = useCallback((deck: 'A' | 'B', time: number) => {
    const id = ++cueIdRef.current
    const colors = ['#ef4444', '#22c55e', '#3b82f6', '#f59e0b', '#a855f7']
    const cp = { id, time, color: colors[id % colors.length], label: `C${id}` }
    if (deck === 'A') setCuePointsA((prev) => [...prev, cp])
    else setCuePointsB((prev) => [...prev, cp])
  }, [])

  // ── Audio context for sampler ──
  const audioCtxRef = useRef<AudioContext | null>(null)
  const getAudioContext = useCallback(() => {
    if (!audioCtxRef.current) {
      audioCtxRef.current = new AudioContext()
    }
    return audioCtxRef.current
  }, [])

  // ── Inline analyze ──
  async function analyzeSong(name: string) {
    if (!name || analyzing) return
    setAnalyzing(name)
    try {
      await analysisApi.analyze(name)
      let attempts = 0
      const poll = async () => {
        attempts++
        if (attempts > 30) { setAnalyzing(null); return }
        try {
          const info = await libraryApi.get(name)
          if (info.has_analysis) {
            setAnalyzing(null)
            if (name === songA) refetchA()
            else if (name === songB) refetchB()
            return
          }
        } catch {}
        setTimeout(poll, 2000)
      }
      setTimeout(poll, 2000)
    } catch {
      setAnalyzing(null)
    }
  }

  async function checkCompat() {
    if (!canCompare) return
    setCompatError(null)
    setCompatLoading(true)
    try {
      const res = await analysisApi.compatibility(songA, songB)
      if ('overall' in res) setCompat(res as CompatibilityResult)
    } catch (e) {
      setCompatError(e instanceof Error ? e.message : 'Compatibility check failed')
    } finally {
      setCompatLoading(false)
    }
  }

  async function launchPreview() {
    if (!canCompare || submitting) return
    setSubmitting(true)
    try {
      const res = await remixApi.preview({
        song_a: songA,
        song_b: songB,
        transition_duration: transitionDuration,
        transition_bars: remixOpts.transition_bars,
        preset: remixOpts.preset,
      })
      setPreviewJobId(res.job_id)
      upsertJob({ job_id: res.job_id, status: 'PENDING', type: 'dj_remix', progress: 0, message: 'Rendering preview…', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), meta: { preview: true } })
    } catch (e) {
      setCompatError(e instanceof Error ? e.message : 'Preview failed')
    } finally {
      setSubmitting(false)
    }
  }

  async function launchRemix() {
    if (!canCompare || submitting) return
    setSubmitting(true)
    try {
      const res = await remixApi.create({
        song_a: songA,
        song_b: songB,
        transition_duration: transitionDuration,
        target_bpm: targetBpm ? parseFloat(targetBpm) : undefined,
        transition_bars: remixOpts.transition_bars,
        preset: remixOpts.preset,
        transition_effect: remixOpts.transition_effect,
        bridge_beat_mode: remixOpts.bridge_beat_mode,
        bridge_beat_genre: remixOpts.bridge_beat_genre,
        bridge_beat_intensity: remixOpts.bridge_beat_intensity,
        target_lufs: remixOpts.target_lufs,
        eq_strategy: remixOpts.eq_strategy,
        crossfade_type: remixOpts.crossfade_type,
      })
      setRemixJobId(res.job_id)
      upsertJob({ job_id: res.job_id, status: 'PENDING', type: 'dj_remix', progress: 0, message: 'Rendering full mix…', created_at: new Date().toISOString(), updated_at: new Date().toISOString() })
    } catch (e) {
      setCompatError(e instanceof Error ? e.message : 'Remix failed')
    } finally {
      setSubmitting(false)
    }
  }

  // ── Audio transport handlers ──
  const handlePlay = useCallback(() => audio.play(), [audio])
  const handlePause = useCallback(() => audio.pause(), [audio])
  const handleStop = useCallback(() => audio.stop(), [audio])
  const handleSeekA = useCallback((t: number) => audio.seek(t), [audio])
  const handleSeekB = useCallback((t: number) => audio.seek(t), [audio])
  const handleVolumeA = useCallback((v: number) => { setVolumeA(v); audio.setTrackVolume('deckA', v) }, [audio])
  const handleVolumeB = useCallback((v: number) => { setVolumeB(v); audio.setTrackVolume('deckB', v) }, [audio])
  const handleEffectA = useCallback((type: string) => { setEffectA(type); audio.setEffect('deckA', type) }, [audio])
  const handleEffectB = useCallback((type: string) => { setEffectB(type); audio.setEffect('deckB', type) }, [audio])

  const levelA = audio.levels.get('deckA') ?? 0
  const levelB = audio.levels.get('deckB') ?? 0

  return (
    <div className="page-base">
      <header className="page-base__header">
        <Disc3 size={20} strokeWidth={1.5} className="page-base__header-icon" />
        <div>
          <h1 className="page-base__title font-display">Mix Deck</h1>
          <p className="page-base__sub text-muted">
            Live stem mixing · EQ · Effects · Crossfade · Server remix
          </p>
        </div>
        <div className="md-header__status">
          {audio.state === 'playing' && (
            <span className="md-live-badge">
              <span className="md-live-dot" /> LIVE
            </span>
          )}
        </div>
      </header>

      <div className="page-base__body md-body">
        {/* ── Decks row ── */}
        <div className="md-decks-row">
          {/* Deck A */}
          <div
            className={`md-deck-wrap ${dragOverDeck === 'A' ? 'md-deck-wrap--drag-over' : ''}`}
            onDragOver={(e) => { handleDragOver(e, 'A'); handleFileDragOver(e) }}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(e, 'A')}
          >
            <div className="md-deck" style={{ '--deck-color': DECK_HEX.A, '--deck-glow': 'var(--color-amber-glow)' } as React.CSSProperties}>
              <DeckHeader
                label="A"
                song={songA}
                songInfo={songInfoA}
                songs={songNames}
                onChange={(v) => { setSongA(v); setCompat(null) }}
                loadingSongInfo={loadingA}
              />

              {loadedTracks.a && (
                <Turntable
                  bpm={songInfoA?.bpm ?? 0}
                  isPlaying={audio.state === 'playing'}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  trackName={songA}
                  deckColor={DECK_HEX.A}
                  label="A"
                  onSeek={handleSeekA}
                />
              )}

              {loadedTracks.a && (
                <WaveformTimeline
                  waveformData={audio.waveform}
                  frequencyData={null}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  bpm={songInfoA?.bpm ?? 0}
                  deckColor={DECK_HEX.A}
                  cuePoints={cuePointsA}
                  onSeek={handleSeekA}
                  onCuePointAdd={(t) => addCuePoint('A', t)}
                  sections={[]}
                />
              )}

              {loadedTracks.a && (
                <TransportControls
                  state={audio.state}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  deckColor={DECK_HEX.A}
                  onPlay={handlePlay}
                  onPause={handlePause}
                  onStop={handleStop}
                  onSeek={handleSeekA}
                  volume={volumeA}
                  onVolumeChange={handleVolumeA}
                />
              )}

              {loadedTracks.a && (
                <div className="md-deck__vu">
                  <VUMeter level={levelA} color={DECK_HEX.A} height={100} width={10} />
                </div>
              )}

              {songA && songInfoA?.has_stems && loadedTracks.a && (
                <StemMixer
                  trackId="deckA"
                  trackLabel="A"
                  deckColor={DECK_HEX.A}
                  stems={STEMS}
                  onStemVolume={(stem, v) => audio.setStemVolume('deckA', stem, v)}
                  onStemMute={(stem, muted) => audio.setStemMuted('deckA', stem, muted)}
                  onStemSolo={(_, solo) => audio.setTrackSolo('deckA', solo)}
                />
              )}

              {loadedTracks.a && (
                <EQKnobs
                  trackId="deckA"
                  deckColor={DECK_HEX.A}
                  onEQChange={(band: 'low' | 'mid' | 'high', dB: number) => audio.setEQ('deckA', band, dB)}
                  onKill={(band: 'low' | 'mid' | 'high', enabled: boolean) => audio.setEQBandEnabled('deckA', band, !enabled)}
                />
              )}

              {loadedTracks.a && (
                <EffectsRack
                  trackId="deckA"
                  deckColor={DECK_HEX.A}
                  currentEffect={effectA}
                  onEffectChange={handleEffectA}
                />
              )}
            </div>
            {dragOverDeck === 'A' && (
              <div className="md-drop-overlay">
                <Upload size={24} />
                <span>Drop track here</span>
              </div>
            )}
          </div>

          {/* Center: crossfader + actions */}
          <div className="md-center">
            <Crossfader
              value={audio.crossfadeValue}
              curve="power"
              onChange={audio.setCrossfade}
              onCurveChange={audio.setCrossfadeType}
            />

            <button
              className="md-compat-btn"
              disabled={!canCompare || compatLoading}
              onClick={checkCompat}
            >
              {compatLoading ? <RefreshCw size={12} className="md-spin" /> : <Zap size={12} />}
              Check compatibility
            </button>

            {songA && !songInfoA?.has_analysis && (
              <button
                className="md-analyze-btn"
                disabled={!!analyzing}
                onClick={() => analyzeSong(songA)}
              >
                {analyzing === songA ? <Loader2 size={11} className="md-spin" /> : null}
                Analyze A
              </button>
            )}
            {songB && !songInfoB?.has_analysis && (
              <button
                className="md-analyze-btn"
                disabled={!!analyzing}
                onClick={() => analyzeSong(songB)}
              >
                {analyzing === songB ? <Loader2 size={11} className="md-spin" /> : null}
                Analyze B
              </button>
            )}
          </div>

          {/* Deck B */}
          <div
            className={`md-deck-wrap ${dragOverDeck === 'B' ? 'md-deck-wrap--drag-over' : ''}`}
            onDragOver={(e) => { handleDragOver(e, 'B'); handleFileDragOver(e) }}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(e, 'B')}
          >
            <div className="md-deck" style={{ '--deck-color': DECK_HEX.B, '--deck-glow': 'var(--color-ice-glow)' } as React.CSSProperties}>
              <DeckHeader
                label="B"
                song={songB}
                songInfo={songInfoB}
                songs={songNames}
                suggestedSongs={similarNamesForB}
                onChange={(v) => { setSongB(v); setCompat(null) }}
                loadingSongInfo={loadingB}
              />

              {loadedTracks.b && (
                <Turntable
                  bpm={songInfoB?.bpm ?? 0}
                  isPlaying={audio.state === 'playing'}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  trackName={songB}
                  deckColor={DECK_HEX.B}
                  label="B"
                  onSeek={handleSeekB}
                />
              )}

              {loadedTracks.b && (
                <WaveformTimeline
                  waveformData={audio.waveform}
                  frequencyData={null}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  bpm={songInfoB?.bpm ?? 0}
                  deckColor={DECK_HEX.B}
                  cuePoints={cuePointsB}
                  onSeek={handleSeekB}
                  onCuePointAdd={(t) => addCuePoint('B', t)}
                  sections={[]}
                />
              )}

              {loadedTracks.b && (
                <TransportControls
                  state={audio.state}
                  currentTime={audio.currentTime}
                  duration={audio.duration}
                  deckColor={DECK_HEX.B}
                  onPlay={handlePlay}
                  onPause={handlePause}
                  onStop={handleStop}
                  onSeek={handleSeekB}
                  volume={volumeB}
                  onVolumeChange={handleVolumeB}
                />
              )}

              {loadedTracks.b && (
                <div className="md-deck__vu">
                  <VUMeter level={levelB} color={DECK_HEX.B} height={100} width={10} />
                </div>
              )}

              {songB && songInfoB?.has_stems && loadedTracks.b && (
                <StemMixer
                  trackId="deckB"
                  trackLabel="B"
                  deckColor={DECK_HEX.B}
                  stems={STEMS}
                  onStemVolume={(stem, v) => audio.setStemVolume('deckB', stem, v)}
                  onStemMute={(stem, muted) => audio.setStemMuted('deckB', stem, muted)}
                  onStemSolo={(_, solo) => audio.setTrackSolo('deckB', solo)}
                />
              )}

              {loadedTracks.b && (
                <EQKnobs
                  trackId="deckB"
                  deckColor={DECK_HEX.B}
                  onEQChange={(band: 'low' | 'mid' | 'high', dB: number) => audio.setEQ('deckB', band, dB)}
                  onKill={(band: 'low' | 'mid' | 'high', enabled: boolean) => audio.setEQBandEnabled('deckB', band, !enabled)}
                />
              )}

              {loadedTracks.b && (
                <EffectsRack
                  trackId="deckB"
                  deckColor={DECK_HEX.B}
                  currentEffect={effectB}
                  onEffectChange={handleEffectB}
                />
              )}
            </div>
            {dragOverDeck === 'B' && (
              <div className="md-drop-overlay">
                <Upload size={24} />
                <span>Drop track here</span>
              </div>
            )}
          </div>
        </div>

        {/* ── Sampler + Library ── */}
        <div className="md-bottom-row">
          <div className="md-sampler-section">
            <Sampler audioContext={getAudioContext()} />
          </div>

          <button
            className="md-library-toggle"
            onClick={() => setShowLibrary(!showLibrary)}
            title={showLibrary ? 'Hide library' : 'Show library'}
          >
            {showLibrary ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
            <span>Library</span>
          </button>

          {showLibrary && (
            <div className="md-library-panel">
              <LibraryFilterPanel
                tracks={songs as any}
                onFilter={() => {}}
                onSelectTrack={(t) => {
                  if (!songA) setSongA(t.name)
                  else if (!songB) setSongB(t.name)
                  else setSongA(t.name)
                  setCompat(null)
                }}
              />
            </div>
          )}
        </div>

        {/* ── Compat result + timeline ── */}
        {compatError && (
          <div className="md-error">{compatError}</div>
        )}
        {compat && <CompatPanel result={compat} />}
        {songA && songB && (
          <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 'var(--space-2)' }}>
            <CamelotWheel keyA={songInfoA?.key} keyB={songInfoB?.key} size={220} />
          </div>
        )}
        {compat && (
          <TransitionTimeline
            result={compat}
            durationA={songInfoA?.duration}
            durationB={songInfoB?.duration}
          />
        )}

        {/* ── Remix options + server actions ── */}
        {canCompare && (
          <RemixControls value={remixOpts} onChange={setRemixOpts} />
        )}

        {canCompare && (
          <div className="md-controls">
            <h3 className="md-controls__title">Server Remix</h3>
            <div className="md-controls__row">
              <div className="md-control">
                <label className="md-control__label">Crossfade bars</label>
                <div className="md-slider-row">
                  <input
                    type="range"
                    min={8}
                    max={128}
                    step={8}
                    value={transitionDuration}
                    onChange={(e) => setTransitionDuration(parseInt(e.target.value))}
                    className="md-slider"
                  />
                  <span className="md-slider-val font-mono">{transitionDuration}</span>
                </div>
              </div>
              <div className="md-control">
                <label className="md-control__label">Target BPM (optional)</label>
                <input
                  type="number"
                  className="md-input"
                  placeholder={songInfoA?.bpm?.toFixed(1) ?? 'auto'}
                  value={targetBpm}
                  onChange={(e) => setTargetBpm(e.target.value)}
                  min={60}
                  max={200}
                />
              </div>
            </div>

            <div className="md-action-row">
              <button
                className="md-btn md-btn--secondary"
                disabled={submitting}
                onClick={launchPreview}
              >
                {submitting ? <Loader2 size={14} className="md-spin" /> : <Play size={14} />}
                Preview transition
              </button>
              <button
                className="md-btn md-btn--primary"
                disabled={submitting}
                onClick={launchRemix}
              >
                {submitting ? <Loader2 size={14} className="md-spin" /> : <GitMerge size={14} />}
                Full remix (−14 LUFS)
              </button>
            </div>

            {previewJobId && <JobStatusBadge jobId={previewJobId} />}
            {remixJobId   && <JobStatusBadge jobId={remixJobId} />}
            {remixJob?.status === 'COMPLETED' && <RemixResultCard job={remixJob} />}
          </div>
        )}

        {!songA && !songB && (
          <div className="md-splash">
            <Sliders size={36} strokeWidth={0.75} />
            <p className="font-display" style={{ fontSize: 'var(--text-xl)' }}>
              Load two tracks to start mixing
            </p>
            <p className="text-muted" style={{ fontSize: 'var(--text-sm)' }}>
              Drag & drop tracks from the library or your file system.
            </p>
            <p className="text-muted" style={{ fontSize: 'var(--text-xs)' }}>
              Turntables · Waveform · Sampler · Stem mixing · EQ · Effects · Crossfade · Server remix
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
