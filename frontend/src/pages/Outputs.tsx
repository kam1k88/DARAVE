/* ============================================================
   DARAVE — Outputs
   Browse all generated audio: previews, transitions, chains, beats.
   ============================================================ */
import { useState, useRef } from 'react'
import { Folder, Play, Pause, Music2, Download } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import './PageBase.css'
import './Outputs.css'

interface OutputFile {
  name: string
  size_mb: number
  ext: string
  modified: number
}

interface OutputDir {
  session_id: string
  type: string
  modified: number
  files: OutputFile[]
  audio_file: string | null
  total_size_mb: number
}

const TYPE_LABELS: Record<string, string> = {
  preview: 'Превью',
  transition: 'Переход',
  chain: 'Сет',
  beat: 'Бит',
  lab: 'Лаб',
}

const TYPE_COLORS: Record<string, string> = {
  preview: '#22c55e',
  transition: '#f59e0b',
  chain: '#8b5cf6',
  beat: '#06b6d4',
  lab: '#ec4899',
}

const BASE = import.meta.env.VITE_API_BASE || '/api'

export default function Outputs() {
  const [playingId, setPlayingId] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const { data: outputs = [], isLoading } = useQuery<OutputDir[]>({
    queryKey: ['outputs'],
    queryFn: async () => {
      const res = await fetch(`${BASE}/outputs`)
      if (!res.ok) return []
      return res.json()
    },
  })

  const play = (sessionId: string, filename: string) => {
    const key = `${sessionId}/${filename}`
    if (audioRef.current && playingId === key) {
      audioRef.current.pause()
      audioRef.current = null
      setPlayingId(null)
      return
    }
    if (audioRef.current) {
      audioRef.current.pause()
    }
    const audio = new Audio(`${BASE}/outputs/${encodeURIComponent(sessionId)}/${encodeURIComponent(filename)}`)
    audioRef.current = audio
    audio.play().catch(() => {})
    audio.onended = () => setPlayingId(null)
    setPlayingId(key)
  }

  const formatSize = (mb: number) => {
    if (mb >= 1000) return `${(mb / 1000).toFixed(1)} GB`
    return `${mb.toFixed(1)} MB`
  }

  const formatTime = (ts: number) => {
    const d = new Date(ts * 1000)
    return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
  }

  return (
    <div className="page-base">
      <div className="page-base__header">
        <h1 className="page-base__title"><Folder size={20} /> Выходные файлы</h1>
        <span className="page-base__subtitle">{outputs.length} сессий</span>
      </div>
      <div className="page-base__body">
        {isLoading && <p className="outputs-empty">Загрузка…</p>}
        {!isLoading && outputs.length === 0 && (
          <div className="outputs-empty">
            <Folder size={48} />
            <p>Пока нет сгенерированных файлов</p>
          </div>
        )}
        <div className="outputs-list">
          {outputs.map((out) => {
            const color = TYPE_COLORS[out.type] || '#888'
            return (
              <div key={out.session_id} className="outputs-card">
                <div className="outputs-card__header">
                  <span className="outputs-card__type" style={{ background: color }}>
                    {TYPE_LABELS[out.type] || out.type}
                  </span>
                  <span className="outputs-card__time">{formatTime(out.modified)}</span>
                  <span className="outputs-card__size">{formatSize(out.total_size_mb)}</span>
                </div>
                <div className="outputs-card__files">
                  {out.files.map((f) => {
                    const isAudio = ['.wav', '.mp3', '.flac'].includes(f.ext)
                    const key = `${out.session_id}/${f.name}`
                    const isPlaying = playingId === key
                    return (
                      <div key={f.name} className="outputs-file">
                        <Music2 size={12} className="outputs-file__icon" />
                        <span className="outputs-file__name">{f.name}</span>
                        <span className="outputs-file__size">{f.size_mb.toFixed(1)} MB</span>
                        {isAudio && (
                          <button
                            className={`outputs-play ${isPlaying ? 'outputs-play--active' : ''}`}
                            onClick={() => play(out.session_id, f.name)}
                            title={isPlaying ? 'Пауза' : 'Воспроизвести'}
                          >
                            {isPlaying ? <Pause size={12} /> : <Play size={12} />}
                          </button>
                        )}
                        <a
                          href={`${BASE}/outputs/${encodeURIComponent(out.session_id)}/${encodeURIComponent(f.name)}`}
                          download={f.name}
                          className="outputs-download"
                          title="Скачать"
                        >
                          <Download size={12} />
                        </a>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
