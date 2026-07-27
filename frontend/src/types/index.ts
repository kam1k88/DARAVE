/* ============================================================
   AI RemixMate — Shared TypeScript types
   Mirror of FastAPI schemas; kept in sync manually.
   ============================================================ */

// --- Job types ---

export type JobStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'

export interface Job {
  job_id: string
  status: JobStatus
  type: string
  progress: number          // 0–100
  message: string
  created_at: string        // ISO
  updated_at: string        // ISO
  result?: Record<string, unknown>
  error?: string
  meta?: Record<string, unknown>
}

// --- Library ---

export interface SongInfo {
  name: string
  path: string
  has_stems: boolean
  has_analysis: boolean
  bpm?: number
  key?: string
  camelot?: string
  duration?: number         // seconds
  genre?: string | { genres?: Array<{ name: string; confidence: number }>; tags?: string[]; description?: string }
  energy?: number           // 0–1
  embedding?: number[]
  stems?: string[]          // e.g. ['vocals', 'drums', 'bass', 'other']
  tempo_type?: string       // "half-time" | "full-time" | "downtempo" | "other"
  el?: number               // Energy Level 1–5
  drops?: number            // Number of drops in the track
  breakdowns?: number       // Number of breakdowns in the track
}

export interface LibraryStats {
  total_songs: number
  indexed_songs: number
  stems_split: number
  total_size_mb: number
}

// Matches scripts/api/schemas.py:ProcessingStatusResponse
export interface ProcessingStatus {
  fully_processed: string[]
  stems_only: string[]
  analysis_only: string[]
  unprocessed: string[]
  total: number
  generated_at: number
}

// Matches scripts/api/schemas.py:StorageStatusResponse / StoragePruneResponse / StorageEvictResponse
export interface StorageStatus {
  library_dir: string
  outputs_dir: string
  total_songs: number
  total_size_gb: number
  cap_gb: number
  within_cap: boolean
  songs_with_full_wav: number
  songs_stems_only: number
  prune_on_download: boolean
  keep_raw_after_separation: boolean
  auto_evict_on_download: boolean
}

export interface StoragePruneResult {
  pruned: string[]
  freed_mb: number
}

export interface StorageEvictResult {
  evicted: string[]
  dry_run: boolean
  size_before_gb: number
  size_after_gb: number
}

// --- Analysis ---

export interface TransitionPlan {
  exit_bar_a: number
  entry_bar_b: number
  transition_bars: number
  stretch_ratio: number
  exit_time_a?: number      // seconds in Song A's timeline
  entry_time_b?: number     // seconds in Song B's timeline
  key_compatible?: boolean
}

export interface CompatibilityResult {
  song_a: string
  song_b: string
  compatible: boolean
  overall: number           // 0–1 composite score
  bpm_score: number         // 0–1
  key_score: number         // 0–1
  energy_score: number      // 0–1
  genre_proximity?: number      // 0–1, neutral 0.5 when genre data missing
  timbral_similarity?: number   // 0–1, neutral 0.5 when spectral data missing
  vocal_clash_penalty?: number  // 0–1 subtracted from overall, 0 when vocal data missing
  bpm_a: number
  bpm_b: number
  camelot_a?: string
  camelot_b?: string
  genre_a?: string
  genre_b?: string
  transition_plan?: TransitionPlan
}

export interface Recommendation {
  name: string
  bpm?: number
  bpm_score?: number
  overall?: number
  score?: number
  reason?: string
}

/** Result row from /library/similar — 35-dim RAG vector match. */
export interface SimilarTrack {
  name: string
  score: number
  bpm?: number
  key?: string
  mode?: string
  camelot?: string
  genre?: string
  breakdown?: Record<string, number>
}

// --- Remix ---

export interface DJRemixRequest {
  song_a: string
  song_b: string
  transition_duration?: number
  transition_bars?: number
  effects?: string[]
  target_bpm?: number
  preset?: string
  transition_effect?: string
  bridge_beat_mode?: string
  bridge_beat_genre?: string
  bridge_beat_intensity?: number
  target_lufs?: number
  eq_strategy?: string
  crossfade_type?: string
}

export interface DJPreviewRequest {
  song_a: string
  song_b: string
  transition_duration?: number
  transition_bars?: number
  preset?: string
}

// --- Crates ---

export interface Crate {
  id: number
  name: string
  song_count: number
  created_at: string
}

// --- SSE events ---

export type SSEEventType =
  | 'heartbeat'
  | 'job_created'
  | 'job_updated'
  | 'job_completed'
  | 'job_failed'
  | 'job_cancelled'
  | 'library_changed'
  | 'system_status'

export interface SSEEvent<T = unknown> {
  type: SSEEventType
  data: T
  ts: string                // ISO timestamp
}

export interface HeartbeatData {
  uptime_seconds: number
  active_jobs: number
  api_version: string
  machine_profile?: MachineProfile
}

// --- Machine profile ---

export interface MachineProfile {
  hostname: string
  platform: string
  cpu_model: string
  cpu_cores_physical: number
  cpu_cores_logical: number
  ram_gb: number
  gpu_backend: 'cuda' | 'mps' | 'cpu'
  gpu_name?: string
  gpu_vram_gb?: number
  demucs_device: string
  recommended_batch_size: number
  tier: 'low' | 'mid' | 'high' | 'pro'
}

// --- Health ---

export interface HealthStatus {
  status: 'ok' | 'degraded' | 'down'
  version?: string
  uptime_seconds?: number
}

// --- Track Structure ---

export interface TrackSection {
  type: string       // intro | verse | chorus | drop | break | build | outro
  start_time: number
  end_time: number
  start_bar: number
  end_bar: number
  avg_energy: number
}

export interface TrackStructure {
  name: string
  bpm: number | null
  duration: number | null
  key: string | null
  camelot: string | null
  total_bars: number | null
  sections: TrackSection[]
  energy_curve: number[] | null
  phrase_boundaries: number[]
}

// --- Mix Plan ---

export interface MixPlanTrack {
  song_name: string
  bpm: number
  camelot: string
  key: string
  energy_mean: number
  energy_std: number
  total_bars: number
}

export interface MixPlanTransition {
  pair_index: number
  from_song: string
  to_song: string
  technique: string
  effect: string
  transition_bars: number
  crossfade_type: string
  confidence: number
  reason: string
  energy_from: number
  energy_to: number
  energy_delta: number
  bpm_from: number
  bpm_to: number
  camelot_from: string
  camelot_to: string
  bridge_beat: boolean
  key_compatible: boolean
  tempo_ratio: number
}

export interface MixPlanEnergyArc {
  song_index: number
  song_name: string
  energy_mean: number
  energy_std: number
  start_sec: number
  end_sec: number
  start_label: string
  end_label: string
}

export interface MixPlanResult {
  songs: string[]
  track_order: number[]
  structures: MixPlanTrack[]
  transitions: MixPlanTransition[]
  energy_arc: MixPlanEnergyArc[]
  total_duration_sec: number
  avg_confidence: number
}

// --- Navigation ---

export type NavDestination =
  | 'strategy'
  | 'library'
  | 'solo'
  | 'outputs'
  | 'downloads'


// --- DJ Techniques (Quick Mix) ---

export interface TechniqueParam {
  name: string               // "swap_start_bar"
  label: string              // "Точка начала замены"
  type: string               // "int" | "float" | "select"
  min_val: number
  max_val: number
  default: number
  unit: string               // "тактов" | "dB" | "%" | "x" | ""
  options?: string[]         // for type="select"
}

export interface DJTechnique {
  id: string                     // "DNB-01"
  name: string                   // "Double Drop"
  category: string               // "cut" | "eq" | "filter" | etc.
  difficulty: number             // 1-5
  level: string                  // "beginner" | "intermediate" | "advanced" | "experimental"
  description: string
  description_ru?: string
  description_cn?: string
  best_for: string
  when_to_use: string
  effects_used: string[]
  bpm_range: number[]            // [160, 180]
  key_compatibility: string
  energy_delta: string
  transition_bars: number
  frequency_focus: string
  parameters: TechniqueParam[]
  steps: string[]
}

export interface PatternSearchTrack {
  name: string
  bpm: number
  key?: string
  mode?: string
  camelot?: string
  energy_mean: number
  has_stems: boolean
  score: number
  reasons: string[]
}

export interface PatternSearchPair {
  track_a: PatternSearchTrack
  track_b: PatternSearchTrack
  score: number
  reasons: string[]
}

export interface PatternSearchResult {
  technique_id: string
  technique_name: string
  tracks: PatternSearchTrack[]
  pairs: PatternSearchPair[]
}
