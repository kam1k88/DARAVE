import React, { useState, useMemo } from 'react'
import './LibraryFilterPanel.css'

interface Track {
  name: string
  bpm?: number
  key?: string
  camelot?: string
  genre?: string | { genres?: { name: string; confidence: number }[]; tags?: string[]; description?: string }
  energy?: number
  duration?: number
}

interface LibraryFilterPanelProps {
  tracks: Track[]
  onFilter: (filtered: Track[]) => void
  onSelectTrack?: (track: Track) => void
}

const CAMELOT_KEYS = [
  '1A', '1B', '2A', '2B', '3A', '3B', '4A', '4B',
  '5A', '5B', '6A', '6B', '7A', '7B', '8A', '8B',
  '9A', '9B', '10A', '10B', '11A', '11B', '12A', '12B',
]

const LibraryFilterPanel: React.FC<LibraryFilterPanelProps> = ({
  tracks,
  onFilter,
  onSelectTrack,
}) => {
  const [search, setSearch] = useState('')
  const [bpmMin, setBpmMin] = useState('')
  const [bpmMax, setBpmMax] = useState('')
  const [selectedKey, setSelectedKey] = useState('')
  const [selectedGenre, setSelectedGenre] = useState('')
  const [energyMax, setEnergyMax] = useState(1)
  const [sortBy, setSortBy] = useState<'name' | 'bpm' | 'energy'>('name')

  const genres = useMemo(() => {
    const g = new Set<string>()
    tracks.forEach((t) => {
      if (typeof t.genre === 'string') g.add(t.genre)
      else if (t.genre?.genres) t.genre.genres.forEach((ge) => g.add(ge.name))
    })
    return Array.from(g).sort()
  }, [tracks])

  const filteredTracks = useMemo(() => {
    let result = tracks

    if (search) {
      const q = search.toLowerCase()
      result = result.filter((t) => t.name.toLowerCase().includes(q))
    }
    if (bpmMin) result = result.filter((t) => (t.bpm || 0) >= Number(bpmMin))
    if (bpmMax) result = result.filter((t) => (t.bpm || 0) <= Number(bpmMax))
    if (selectedKey) result = result.filter((t) => t.camelot === selectedKey)
    if (selectedGenre) result = result.filter((t) => {
      if (typeof t.genre === 'string') return t.genre === selectedGenre
      return t.genre?.genres?.some((g) => g.name === selectedGenre) ?? false
    })
    result = result.filter((t) => {
      const e = t.energy ?? 0.5
      return e <= energyMax
    })

    result.sort((a, b) => {
      if (sortBy === 'bpm') return (a.bpm || 0) - (b.bpm || 0)
      if (sortBy === 'energy') return (a.energy || 0) - (b.energy || 0)
      return a.name.localeCompare(b.name)
    })

    return result
  }, [tracks, search, bpmMin, bpmMax, selectedKey, selectedGenre, energyMax, sortBy])

  React.useEffect(() => {
    onFilter(filteredTracks)
  }, [filteredTracks, onFilter])

  return (
    <div className="lfp-container">
      <input
        className="lfp-search"
        type="text"
        placeholder="Search tracks..."
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />

      <div className="lfp-row">
        <label>BPM</label>
        <input
          className="lfp-input-sm"
          type="number"
          placeholder="min"
          value={bpmMin}
          onChange={(e) => setBpmMin(e.target.value)}
        />
        <span className="lfp-dash">-</span>
        <input
          className="lfp-input-sm"
          type="number"
          placeholder="max"
          value={bpmMax}
          onChange={(e) => setBpmMax(e.target.value)}
        />
      </div>

      <div className="lfp-row">
        <label>Key</label>
        <select
          className="lfp-select"
          value={selectedKey}
          onChange={(e) => setSelectedKey(e.target.value)}
        >
          <option value="">All</option>
          {CAMELOT_KEYS.map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
      </div>

      {genres.length > 0 && (
        <div className="lfp-row">
          <label>Genre</label>
          <select
            className="lfp-select"
            value={selectedGenre}
            onChange={(e) => setSelectedGenre(e.target.value)}
          >
            <option value="">All</option>
            {genres.map((g) => (
              <option key={g} value={g}>{g}</option>
            ))}
          </select>
        </div>
      )}

      <div className="lfp-row">
        <label>Energy</label>
        <input
          className="lfp-range"
          type="range"
          min={0}
          max={100}
          value={energyMax * 100}
          onChange={(e) => setEnergyMax(Number(e.target.value) / 100)}
        />
        <span className="lfp-range-label">{Math.round(energyMax * 100)}%</span>
      </div>

      <div className="lfp-row">
        <label>Sort</label>
        <select
          className="lfp-select"
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
        >
          <option value="name">Name</option>
          <option value="bpm">BPM</option>
          <option value="energy">Energy</option>
        </select>
      </div>

      <div className="lfp-results">
        <span className="lfp-count">{filteredTracks.length} tracks</span>
        <div className="lfp-track-list">
          {filteredTracks.slice(0, 50).map((t) => (
            <div
              key={t.name}
              className="lfp-track-item"
              onClick={() => onSelectTrack?.(t)}
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData('text/plain', t.name)
                e.dataTransfer.effectAllowed = 'copy'
              }}
            >
              <span className="lfp-track-name">{t.name}</span>
              <span className="lfp-track-meta">
                {t.bpm && <span>{Math.round(t.bpm)}</span>}
                {t.camelot && <span>{t.camelot}</span>}
                {t.energy != null && (
                  <span className="lfp-energy-dot" style={{ opacity: 0.3 + t.energy * 0.7 }} />
                )}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export default LibraryFilterPanel
