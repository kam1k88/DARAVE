/* ============================================================
   AI RemixMate — Import
   Import tracks from local Music folder.
   ============================================================ */
import { useState } from 'react'
import { Download, Music2 } from 'lucide-react'
import { libraryApi } from '@/lib/api'
import { useTranslation } from '@/i18n'
import './PageBase.css'
import './Operations.css'

// --- Import from local folder panel ---

function LocalImportPanel() {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ imported: number; skipped: number; files: string[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { t } = useTranslation()

  async function handleImport() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const r = await libraryApi.importLocal()
      setResult(r)
      if (r.imported > 0) {
        window.dispatchEvent(new Event('library-changed'))
      }
    } catch (e: any) {
      setError(e?.message || 'Import failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="ops-processing">
      <div className="ops-queue__header">
        <h2 className="ops-queue__label">
          <Music2 size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          {t('ops.importLocal') || 'Импорт из папки Music'}
        </h2>
      </div>
      <div className="ops-storage__actions">
        <button className="ops-job__btn" disabled={busy} onClick={handleImport}>
          <Download size={12} />
          {busy ? 'Импорт…' : 'Импортировать из C:\\Users\\kam1k88\\Music'}
        </button>
      </div>
      {result && (
        <div className="ops-storage__notice" style={{ marginTop: 8 }}>
          {result.imported > 0 && <>Импортировано: {result.imported} трек(ов)</>}
          {result.skipped > 0 && <> · Пропущено (уже в библиотеке): {result.skipped}</>}
          {result.files.length > 0 && result.files.length <= 10 && (
            <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
              {result.files.map((f) => <li key={f} className="text-muted" style={{ fontSize: 'var(--text-xs)' }}>{f}</li>)}
            </ul>
          )}
        </div>
      )}
      {error && <div className="ops-storage__notice" style={{ marginTop: 8, color: 'var(--color-crimson-500)' }}>{error}</div>}
    </section>
  )
}

// --- Main page ---

export default function Operations() {
  const { t } = useTranslation()

  return (
    <div className="page-base">
      <header className="page-base__header">
        <Music2 size={20} strokeWidth={1.5} className="page-base__header-icon" />
        <div>
          <h1 className="page-base__title font-display">{t('ops.title') || 'Import'}</h1>
          <p className="page-base__sub text-muted">
            {t('ops.subtitle') || 'Import tracks from your local Music folder'}
          </p>
        </div>
      </header>

      <div className="page-base__body ops-body">
        <LocalImportPanel />
      </div>
    </div>
  )
}
