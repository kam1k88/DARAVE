import { useEffect, useRef } from 'react'
import { useAppStore } from '@/stores/appStore'
import { useToast } from '@/components/Toast'
import type { Job } from '@/types'

export function useJobToasts() {
  const { push } = useToast()
  const jobs = useAppStore((s) => s.jobs)
  const prevRef = useRef<Record<string, Job['status']>>({})

  useEffect(() => {
    Object.values(jobs).forEach((job) => {
      const prev = prevRef.current[job.job_id]
      if (prev === job.status) return
      prevRef.current[job.job_id] = job.status
      if (job.status === 'COMPLETED') {
        push({ level: 'success', message: `✓ ${job.type} complete`, jobId: job.job_id })
      } else if (job.status === 'FAILED') {
        const reason = job.error || job.message || 'unknown error'
        push({ level: 'error', message: `✗ ${job.type} failed: ${reason}`, jobId: job.job_id })
      }
    })
  }, [jobs, push])
}
