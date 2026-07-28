/* ============================================================
   AI RemixMate — API client
   Thin wrapper around fetch; base URL resolved via Vite proxy.
   All requests go to /api/* which Vite forwards to FastAPI.
   ============================================================ */

import type {
  Job,
  SongInfo,
  LibraryStats,
  ProcessingStatus,
  StorageStatus,
  StoragePruneResult,
  StorageEvictResult,
  CompatibilityResult,
  Recommendation,
  SimilarTrack,
  DJRemixRequest,
  DJPreviewRequest,
  MixPlanResult,
  TrackStructure,
  Crate,
  HealthStatus,
  DJTechnique,
  PatternSearchResult,
  ChatMessage,
  ChatStatus,
} from '@/types'

// In dev the Vite proxy rewrites /api/* → http://localhost:8000/*.
// In static builds (GitHub Pages) VITE_API_BASE points straight at the
// locally running backend, e.g. http://localhost:8000.
const BASE = import.meta.env.VITE_API_BASE || '/api'

export const EVENTS_URL = import.meta.env.VITE_EVENTS_URL || '/events/stream'

const DEFAULT_TIMEOUT_MS = 30_000

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    message: string,
  ) {
    super(`[${status}] ${path}: ${message}`)
    this.name = 'ApiError'
  }
}

// Generic fetcher — throws ApiError on non-2xx or timeout
async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch(`${BASE}${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    })

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText)
      throw new ApiError(res.status, path, text)
    }

    return (await res.json()) as T
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(0, path, `request timed out after ${timeoutMs}ms`)
    }
    throw err
  } finally {
    clearTimeout(timer)
  }
}

const get  = <T>(path: string) => request<T>('GET', path)
const post = <T>(path: string, body?: unknown) => request<T>('POST', path, body)
const del  = <T>(path: string) => request<T>('DELETE', path)
const patch = <T>(path: string, body?: unknown) => request<T>('PATCH', path, body)

// --- Job normalization ---
// The REST endpoints (/jobs, /download, …) return the raw Pydantic shape
// (lowercase status, `job_type`, progress 0–1, epoch timestamps) while SSE
// frames use the frontend shape (uppercase status, `type`, progress 0–100).
// Normalize everything to the canonical frontend Job here.

type RawJob = Record<string, unknown>

export function normalizeJob(raw: RawJob): Job {
  let status = String(raw.status ?? 'pending')
  if (status.includes('.')) status = status.split('.').pop() as string
  status = status.toUpperCase()
  if (status === 'DONE') status = 'COMPLETED'

  let type = String(raw.type ?? raw.job_type ?? '')
  if (type.includes('.')) type = type.split('.').pop() as string
  type = type.toLowerCase()

  // REST shape (has job_type) reports progress as a 0–1 fraction
  let progress = Number(raw.progress ?? 0)
  if ('job_type' in raw && progress <= 1) progress = progress * 100
  progress = Math.max(0, Math.min(100, Math.round(progress)))

  const toIso = (v: unknown): string =>
    typeof v === 'number'
      ? new Date(v * 1000).toISOString()
      : (v as string) ?? new Date().toISOString()

  return {
    job_id: String(raw.job_id),
    status: status as Job['status'],
    type,
    progress,
    message: (raw.message as string) ?? '',
    created_at: toIso(raw.created_at),
    updated_at: toIso(raw.finished_at ?? raw.started_at ?? raw.updated_at ?? raw.created_at),
    result: (raw.result as Job['result']) ?? undefined,
    error: (raw.error as string) ?? undefined,
    meta: (raw.meta as Job['meta']) ?? undefined,
  }
}

// --- Health ---

export const healthApi = {
  live:  () => get<HealthStatus>('/health/live'),
  ready: () => get<HealthStatus>('/health/ready'),
}

// --- Library ---

export const libraryApi = {
  // GET /library returns { stats, songs: [...] } — paginated, default 50/page
  list:    ()           =>
    get<{ songs: SongInfo[] }>('/library?per_page=5000').then((r) => r.songs ?? []),
  get:     (name: string) => get<SongInfo>(`/library/${encodeURIComponent(name)}`),
  delete:  (name: string) => del<void>(`/library/${encodeURIComponent(name)}`),
  stats:   ()           => get<{ stats: LibraryStats }>('/library').then((r) => r.stats),
  structure: (name: string) =>
    get<TrackStructure>(`/library/${encodeURIComponent(name)}/structure`),
  initRun: (opts: Record<string, unknown>) => post<{ job_id: string }>('/library/initialize', opts),
  // Segregates the library into fully-processed / stems-only / analysis-only /
  // unprocessed buckets. Cheap file-existence scan — safe to poll every ~1s.
  processingStatus: () => get<ProcessingStatus>('/library/processing-status'),
  importLocal: (folder?: string) =>
    post<{ imported: number; skipped: number; files: string[] }>(
      `/library/import-local${folder ? `?folder=${encodeURIComponent(folder)}` : ''}`,
      {},
    ),
  upload: (files: File[], autoAnalyze = true) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    fd.append('auto_analyze', String(autoAnalyze))
    return fetch(`${BASE}/library/upload`, { method: 'POST', body: fd })
      .then((r) => { if (!r.ok) throw new Error(`Upload failed: ${r.status}`); return r.json() })
  },
  cleanupStale: () => post<{ removed: string[]; kept: number }>('/library/cleanup-stale', {}),
}

// --- Storage (size cap, pruning, eviction, library location) ---

export const storageApi = {
  status: () => get<StorageStatus>('/library/storage'),
  scanFolder: (folder?: string) =>
    post<{ folder: string; total_files: number; already_in_library: number; new_files: number; total_size_mb: number; files: Array<{ name: string; filename: string; ext: string; size_mb: number; already_in_library: boolean }> }>(
      `/library/storage/scan${folder ? `?folder=${encodeURIComponent(folder)}` : ''}`,
      {},
    ),
  prune:  () => post<StoragePruneResult>('/library/storage/prune', {}),
  // dry_run defaults true server-side; pass false explicitly to actually delete.
  evict:  (targetGb?: number, dryRun = true) =>
    post<StorageEvictResult>(
      `/library/storage/evict?dry_run=${dryRun}${targetGb ? `&target_gb=${targetGb}` : ''}`,
      {},
    ),
}

// --- Downloads ---

export const downloadApi = {
  // API expects { query, name, auto_analyze } — query is a search string or URL.
  // auto_analyze defaults true: stems + BPM/key/structure analysis both run
  // automatically so the song is fully remix-ready the moment it lands.
  single:     (query: string, name?: string, autoAnalyze = true) =>
    post<RawJob>('/download', { query, name: name || undefined, auto_analyze: autoAnalyze }).then(normalizeJob),
  batch:      (queries: string[], autoAnalyze = true) =>
    post<RawJob[]>('/download-batch', { queries, auto_analyze: autoAnalyze }).then((js) => js.map(normalizeJob)),
  fromSpotify: (url: string) =>
    post<{ job_id: string }>('/spotify/import', { url }),
  playlist:   (url: string, limit?: number, autoAnalyze = true) =>
    post<RawJob>('/download-playlist', { url, limit: limit || undefined, auto_analyze: autoAnalyze }).then(normalizeJob),
}

// --- Audio streaming URLs (no fetch — just URL builders) ---

export const audioApi = {
  streamUrl: (name: string) =>
    `${BASE}/library/${encodeURIComponent(name)}/audio`,
}

// --- Stems ---

export const stemsApi = {
  split:         (song: string, opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/stems/split', { song, ...opts }),
  splitBatch:    (songs: string[]) =>
    post<{ job_id: string }>('/stems/split-batch', { songs }),
  compress:      (song: string) =>
    post<{ job_id: string }>('/stems/compress', { song }),
  compressBatch: (songs: string[]) =>
    post<{ job_id: string }>('/stems/compress-batch', { songs }),
  stemUrl: (name: string, stem: string) =>
    `${BASE}/library/${encodeURIComponent(name)}/stems/${encodeURIComponent(stem)}`,
}

// --- Analysis ---

export const analysisApi = {
  analyze:       (song: string) =>
    post<{ job_id: string }>('/analyze', { song }),
  compatibility: (song_a: string, song_b: string) =>
    post<CompatibilityResult | { job_id: string }>('/compatibility', { song_a, song_b }),
  recommend:     (name: string, limit = 8) =>
    get<{ song: string; recommendations: Recommendation[] }>(
      `/recommend/${encodeURIComponent(name)}?limit=${limit}`,
    ).then((r) => r.recommendations),
  similar:       (name: string, k = 8) =>
    get<{ source: string; similar: SimilarTrack[] }>(
      `/library/similar/${encodeURIComponent(name)}?k=${k}`,
    ).then((r) => r.similar),
  rebuildIndex:  () =>
    post<{ job_id: string }>('/index/rebuild', {}),
  // Batch-analyzes every library song that's missing BPM/key/energy data —
  // backs the "Analyze missing" button on Library Atlas. No payload needed;
  // the backend scans has_analysis() across the whole library itself.
  analyzeMissing: () =>
    post<{ job_id: string }>('/library/analyze-missing', {}),
}

// --- Remix ---

export const remixApi = {
  create:  (req: DJRemixRequest) =>
    post<{ job_id: string }>('/dj-remix', req),
  preview: (req: DJPreviewRequest) =>
    post<{ job_id: string }>('/dj-remix/preview', req),
  chain:   (songs: string[], opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/dj-chain', { songs, ...opts }),
  plan:    (songs: string[], transitionBars = 16) =>
    post<MixPlanResult>('/mix/plan', { songs, transition_bars: transitionBars, smart_transitions: true }),
  effects: () =>
    get<{ effects: { name: string; description: string }[] }>('/effects'),
}

// --- Jobs ---

export const jobsApi = {
  list:   () => get<RawJob[]>('/jobs').then((js) => js.map(normalizeJob)),
  get:    (id: string) => get<RawJob>(`/jobs/${id}`).then(normalizeJob),
  cancel: (id: string) => del<void>(`/jobs/${id}`),
}

// --- Crates ---

export const cratesApi = {
  // Backend GET /crates returns { crates: [...] } — an object, not a bare
  // array (same shape mismatch class as the favoritesApi.list bug noted in
  // CLAUDE.md). Unwrapping here is what actually crashed Library Atlas:
  // allCrates.map() inside CratesSection on an object with no .map().
  list:    ()                           => get<{ crates: Crate[] }>('/crates').then((r) => r.crates ?? []),
  create:  (name: string)               => post<Crate>('/crates', { name }),
  rename:  (id: number, name: string)   => patch<Crate>(`/crates/${id}`, { name }),
  delete:  (id: number)                 => del<void>(`/crates/${id}`),
  songs:   (id: number)                 => get<string[]>(`/crates/${id}/songs`),
  addSong: (id: number, name: string)   => post<void>(`/crates/${id}/songs`, { name }),
  removeSong: (id: number, name: string) =>
    del<void>(`/crates/${id}/songs/${encodeURIComponent(name)}`),
}

// --- Tags ---

export const tagsApi = {
  list:      ()                             => get<string[]>('/tags'),
  songTags:  (name: string)                 => get<string[]>(`/library/${encodeURIComponent(name)}/tags`),
  addTag:    (name: string, tag: string)    =>
    post<void>(`/library/${encodeURIComponent(name)}/tags`, { tag }),
  removeTag: (name: string, tag: string)    =>
    del<void>(`/library/${encodeURIComponent(name)}/tags/${encodeURIComponent(tag)}`),
}

// --- Favorites ---

export const favoritesApi = {
  list:   () => get<{ songs: string[]; count: number }>('/favorites').then((r) => r.songs ?? []),
  add:    (name: string) => post<void>(`/favorites/${encodeURIComponent(name)}`),
  remove: (name: string) => del<void>(`/favorites/${encodeURIComponent(name)}`),
}

// --- Setlist ---

export const setlistApi = {
  optimize: (
    songs: Array<{ name: string; bpm?: number; energy?: number; camelot?: string }>,
  ) =>
    post<{ setlist: Array<{ name: string }> }>('/setlist/optimize', {
      tracks: songs.map((s) => ({
        title: s.name,
        artist: '',
        bpm: s.bpm ?? null,
        energy: s.energy ?? 0.5,
        camelot: s.camelot ?? null,
      })),
    }).then((r) => r.setlist.map((t) => t.name)),
}

// --- AI / Generative ---

export const aiApi = {
  models: () => get<string[]>('/ai/models'),
  // Backend StyleTransferRequest requires song_name + description (free-text
  // MusicGen style prompt) — NOT a second track name. See schemas.py.
  styleTransfer: (songName: string, description: string, opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/ai/style-transfer', { song_name: songName, description, ...opts }),
  // Backend InpaintRequest requires song_a (outgoing) + song_b (incoming).
  inpaint: (songA: string, songB: string, opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/ai/inpaint', { song_a: songA, song_b: songB, ...opts }),
  // Separate endpoint — was previously (incorrectly) faked via inpaint(tokenize:true).
  tokenize: (songName: string, opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/ai/tokenize', { song_name: songName, ...opts }),
}

// --- DJ Techniques (Quick Mix) ---

export const techniquesApi = {
  list:   () =>
    get<{ techniques: DJTechnique[]; total: number }>('/techniques').then((r) => r.techniques),
  get:    (id: string) =>
    get<DJTechnique>(`/techniques/${encodeURIComponent(id)}`),
}

// --- Pattern Search ---

export const patternSearchApi = {
  search: (techniqueId: string, maxResults = 20) =>
    post<PatternSearchResult>('/library/pattern-search', {
      technique_id: techniqueId,
      max_results: maxResults,
    }),
}

// --- Quick Mix ---

export const quickMixApi = {
  preview: (songA: string, songB: string, techniqueId = 'DNB-04', effect = 'auto') =>
    post<{ job_id: string }>('/mix/quick-preview', {
      song_a: songA,
      song_b: songB,
      technique_id: techniqueId,
      transition_bars: 4,
      effect,
    }),
  mix:     (tracks: string[], techniqueIds?: (string | null)[], opts?: Record<string, unknown>) =>
    post<{ job_id: string }>('/mix/quick-mix', {
      tracks,
      technique_ids: techniqueIds || null,
      transition_bars: 16,
      bridge_beat: false,
      master: true,
      ...opts,
    }),
}

// --- Strategy ---

export const strategyApi = {
  planSmart: (arcMode = 'dynamic') =>
    post<{ songs: string[]; structures: any[]; transitions: any[]; energy_arc: any[]; total_duration_sec: number; avg_confidence: number; ordering: string }>(`/mix/plan/smart?arc_mode=${encodeURIComponent(arcMode)}`),

  planAll: () =>
    post<{ songs: string[]; structures: any[]; transitions: any[]; energy_arc: any[]; total_duration_sec: number; avg_confidence: number }>('/mix/plan/all'),

  alternatives: (songA: string, songB: string) =>
    post<{ alternatives: any[] }>(`/mix/plan/alternatives?song_a=${encodeURIComponent(songA)}&song_b=${encodeURIComponent(songB)}`),
}

// --- AI Chat ---

async function postStream(path: string, body?: unknown): Promise<ReadableStream<Uint8Array>> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, path, text)
  }
  if (!res.body) {
    throw new ApiError(0, path, 'No response body')
  }
  return res.body
}

export const chatApi = {
  stream: (messages: ChatMessage[], model?: string) =>
    postStream('/chat', { messages, model }),

  history: (limit = 50) =>
    get<ChatMessage[]>(`/chat/history?limit=${limit}`),

  clearHistory: () =>
    del<void>('/chat/history'),

  status: () =>
    get<ChatStatus>('/chat/status'),
}
