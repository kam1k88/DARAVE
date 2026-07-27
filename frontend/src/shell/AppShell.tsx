/* ============================================================
   DARAVE — AppShell
   3-zone CSS Grid: [LeftRail | Canvas | RightInspector]
   The canvas is router-controlled; the two side zones are persistent.
   ============================================================ */

import { Suspense, lazy, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { LeftRail } from './LeftRail'
import { RightInspector } from './RightInspector'
import { useAppStore } from '@/stores/appStore'
import { useSSE } from '@/hooks/useSSE'
import { useJobPoller } from '@/hooks/useJobPoller'
import { ToastProvider } from '@/components/Toast'
import { useJobToasts } from '@/hooks/useJobToasts'
import { ShortcutsModal } from '@/components/ShortcutsModal'
import { useKeyboardShortcuts } from '@/hooks/useKeyboardShortcuts'
import { PageErrorBoundary } from '@/components/PageErrorBoundary'
import './AppShell.css'

function JobToastWatcher() {
  useJobToasts()
  return null
}

// Lazy-load pages
const Strategy     = lazy(() => import('@/pages/Strategy'))
const LibraryAtlas = lazy(() => import('@/pages/LibraryAtlas'))
const Solo         = lazy(() => import('@/pages/Solo'))
const Operations   = lazy(() => import('@/pages/Operations'))
const Outputs      = lazy(() => import('@/pages/Outputs'))

function PageFallback() {
  return (
    <div className="page-fallback">
      <div className="page-fallback__spinner" />
    </div>
  )
}

export function AppShell() {
  useSSE()
  const sseConnected = useAppStore((s) => s.sseConnected)
  useJobPoller(!sseConnected)

  const inspectorOpen = useAppStore((s) => s.inspectorOpen)
  const [showShortcuts, setShowShortcuts] = useState(false)
  useKeyboardShortcuts(
    () => setShowShortcuts(true),
    () => setShowShortcuts(false),
  )

  return (
    <ToastProvider>
      <JobToastWatcher />
      <div className={`app-shell ${inspectorOpen ? 'app-shell--inspector-open' : ''}`}>
        <LeftRail />

        <main className="app-shell__canvas">
          <Routes>
            <Route index element={<Navigate to="/strategy" replace />} />
            <Route path="strategy" element={
              <PageErrorBoundary pageName="Strategy">
                <Suspense fallback={<PageFallback />}><Strategy /></Suspense>
              </PageErrorBoundary>
            } />
            <Route path="library" element={
              <PageErrorBoundary pageName="Library">
                <Suspense fallback={<PageFallback />}><LibraryAtlas /></Suspense>
              </PageErrorBoundary>
            } />
            <Route path="solo" element={
              <PageErrorBoundary pageName="Solo">
                <Suspense fallback={<PageFallback />}><Solo /></Suspense>
              </PageErrorBoundary>
            } />
            <Route path="downloads" element={
              <PageErrorBoundary pageName="Downloads">
                <Suspense fallback={<PageFallback />}><Operations /></Suspense>
              </PageErrorBoundary>
            } />
            <Route path="outputs" element={
              <PageErrorBoundary pageName="Outputs">
                <Suspense fallback={<PageFallback />}><Outputs /></Suspense>
              </PageErrorBoundary>
            } />
            <Route path="*" element={<Navigate to="/strategy" replace />} />
          </Routes>
        </main>

        {inspectorOpen && <RightInspector />}
      </div>
      {showShortcuts && <ShortcutsModal onClose={() => setShowShortcuts(false)} />}
    </ToastProvider>
  )
}
