/* ============================================================
   DARAVE — Import
   Import tracks from local folder or URL (YouTube, SoundCloud, etc.)
   ============================================================ */
import { useState } from 'react'
import { Download, Music2, Link, Loader2, CheckCircle2, AlertTriangle } from 'lucide-react'
import { libraryApi, downloadApi } from '@/lib/api'
import './PageBase.css'
import './Operations.css'

// --- Import from local folder panel ---

function LocalImportPanel() {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ imported: number; skipped: number; files: string[] } | null>(null)
  const [error, setError] = useState<string | null>(null)

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
          Import from Music folder
        </h2>
      </div>
      <div className="ops-storage__actions">
        <button className="ops-job__btn" disabled={busy} onClick={handleImport}>
          <Download size={12} />
          {busy ? 'Importing...' : 'Import from Music folder'}
        </button>
      </div>
      {result && (
        <div className="ops-storage__notice" style={{ marginTop: 8 }}>
          {result.imported > 0 && <>Imported: {result.imported} track(s)</>}
          {result.skipped > 0 && <> · Skipped (already in library): {result.skipped}</>}
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

// --- Import from URL panel ---

function UrlImportPanel() {
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ jobId: string } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleUrlImport() {
    if (!url.trim()) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const res = await downloadApi.single(url.trim())
      setResult({ jobId: res.job_id })
      setUrl('')
    } catch (e: any) {
      setError(e?.message || 'Download failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="ops-processing">
      <div className="ops-queue__header">
        <h2 className="ops-queue__label">
          <Link size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          Import from URL
        </h2>
      </div>
      <p className="text-muted" style={{ fontSize: 'var(--text-xs)', marginBottom: 8 }}>
        Paste a YouTube, SoundCloud, or direct audio URL
      </p>
      <div className="ops-url-row">
        <input
          type="url"
          className="ops-url-input"
          placeholder="https://youtube.com/watch?v=..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') handleUrlImport() }}
          disabled={busy}
        />
        <button
          className="ops-job__btn"
          disabled={busy || !url.trim()}
          onClick={handleUrlImport}
        >
          {busy ? <Loader2 size={12} className="ops-spin" /> : <Download size={12} />}
          {busy ? 'Downloading...' : 'Download'}
        </button>
      </div>
      {result && (
        <div className="ops-storage__notice" style={{ marginTop: 8, color: 'var(--color-green-500)' }}>
          <CheckCircle2 size={12} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          Download started! Check Jobs panel for progress.
        </div>
      )}
      {error && (
        <div className="ops-storage__notice" style={{ marginTop: 8, color: 'var(--color-crimson-500)' }}>
          <AlertTriangle size={12} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          {error}
        </div>
      )}
    </section>
  )
}

// --- Upload files panel ---

function UploadPanel() {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ imported: number; errors: string[] } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files
    if (!files || files.length === 0) return
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const res = await libraryApi.upload(Array.from(files))
      setResult(res)
      if (res.imported > 0) {
        window.dispatchEvent(new Event('library-changed'))
      }
    } catch (e: any) {
      setError(e?.message || 'Upload failed')
    } finally {
      setBusy(false)
      e.target.value = ''
    }
  }

  return (
    <section className="ops-processing">
      <div className="ops-queue__header">
        <h2 className="ops-queue__label">
          <Music2 size={11} style={{ marginRight: 4, verticalAlign: '-1px' }} />
          Upload audio files
        </h2>
      </div>
      <p className="text-muted" style={{ fontSize: 'var(--text-xs)', marginBottom: 8 }}>
        Select MP3, WAV, FLAC, OGG, M4A files from your computer
      </p>
      <div className="ops-storage__actions">
        <label className="ops-job__btn" style={{ cursor: busy ? 'not-allowed' : 'pointer' }}>
          {busy ? <Loader2 size={12} className="ops-spin" /> : <Download size={12} />}
          {busy ? 'Uploading...' : 'Choose files...'}
          <input
            type="file"
            accept="audio/*"
            multiple
            style={{ display: 'none' }}
            onChange={handleUpload}
            disabled={busy}
          />
        </label>
      </div>
      {result && (
        <div className="ops-storage__notice" style={{ marginTop: 8 }}>
          {result.imported > 0 && <>Uploaded: {result.imported} track(s)</>}
          {result.errors.length > 0 && (
            <ul style={{ margin: '4px 0 0', paddingLeft: 16 }}>
              {result.errors.map((e, i) => <li key={i} style={{ fontSize: 'var(--text-xs)', color: 'var(--color-crimson-500)' }}>{e}</li>)}
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
  return (
    <div className="page-base">
      <header className="page-base__header">
        <Music2 size={20} strokeWidth={1.5} className="page-base__header-icon" />
        <div>
          <h1 className="page-base__title font-display">Import</h1>
          <p className="page-base__sub text-muted">
            Import tracks from folder, URL, or upload files
          </p>
        </div>
      </header>

      <div className="page-base__body ops-body">
        <LocalImportPanel />
        <UrlImportPanel />
        <UploadPanel />
      </div>
    </div>
  )
}
