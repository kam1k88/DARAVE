/* ============================================================
   DARAVE — Job polling fallback hook
   Used when SSE is unavailable; polls /api/jobs every N seconds
   and syncs results into the Zustand job store.
   Only updates jobs that are newer than what's already stored.
   ============================================================ */

import { useEffect, useRef } from 'react'
import { useAppStore } from '@/stores/appStore'
import { jobsApi } from '@/lib/api'

const POLL_INTERVAL_MS = 3_000

export function useJobPoller(enabled = true) {
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!enabled) return

    async function poll() {
      try {
        const { sseConnected, upsertJob } = useAppStore.getState()
        // Don't poll if SSE is handling it
        if (sseConnected) return

        const jobs = await jobsApi.list()
        const store = useAppStore.getState()
        for (const job of jobs) {
          const existing = store.jobs[job.job_id]
          // Only upsert if we don't have it, or if the server version is newer
          if (!existing || new Date(job.updated_at) > new Date(existing.updated_at)) {
            upsertJob(job)
          }
        }
        useAppStore.getState().setApiHealth('ok')
      } catch {
        useAppStore.getState().setApiHealth('degraded')
      }
    }

    poll()
    timerRef.current = setInterval(poll, POLL_INTERVAL_MS)

    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [enabled])
}
