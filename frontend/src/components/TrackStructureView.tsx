/* ============================================================
   TrackStructureView — visual track structure
   Shows energy curve + section blocks + drop/break markers.
   ============================================================ */

import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { libraryApi } from '@/lib/api'
import type { TrackSection } from '@/types'
import './TrackStructureView.css'

interface Props {
  songName: string
  height?: number
  transitionZone?: { startSec: number; endSec: number }  // highlight mix window
}

const SECTION_COLORS: Record<string, { bg: string; border: string; label: string }> = {
  intro:  { bg: 'rgba(100,100,100,0.25)',  border: 'rgba(100,100,100,0.5)',  label: 'INTRO' },
  verse:  { bg: 'rgba(56,189,248,0.15)',   border: 'rgba(56,189,248,0.4)',   label: 'VERSE' },
  chorus: { bg: 'rgba(52,211,153,0.2)',    border: 'rgba(52,211,153,0.5)',   label: 'CHORUS' },
  drop:   { bg: 'rgba(248,113,113,0.25)',  border: 'rgba(248,113,113,0.6)',  label: 'DROP' },
  break:  { bg: 'rgba(251,191,36,0.2)',    border: 'rgba(251,191,36,0.5)',   label: 'BREAK' },
  build:  { bg: 'rgba(251,146,60,0.2)',    border: 'rgba(251,146,60,0.5)',   label: 'BUILD' },
  outro:  { bg: 'rgba(100,100,100,0.2)',   border: 'rgba(100,100,100,0.4)',  label: 'OUTRO' },
}

function secToMMSS(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function TrackStructureView({ songName, height = 140, transitionZone }: Props) {
  const { data: structure, isLoading, error } = useQuery({
    queryKey: ['structure', songName],
    queryFn: () => libraryApi.structure(songName),
    enabled: !!songName,
    staleTime: 60_000,
  })

  if (isLoading) {
    return (
      <div className="tsv-loading" style={{ height }}>
        <Loader2 size={16} className="tsv-spin" />
      </div>
    )
  }

  if (error || !structure) {
    return (
      <div className="tsv-empty" style={{ height }}>
        <span>No structure data</span>
      </div>
    )
  }

  const duration = structure.duration || 120
  const sections = structure.sections || []
  const energyCurve = structure.energy_curve || []
  const phraseBoundaries = structure.phrase_boundaries || []

  // SVG dimensions
  const svgW = 800
  const svgH = height
  const padTop = 24
  const padBot = 28
  const chartH = svgH - padTop - padBot
  const chartW = svgW

  // Energy curve points
  const energyPoints = energyCurve.map((v, i) => ({
    x: (i / Math.max(energyCurve.length - 1, 1)) * chartW,
    y: padTop + chartH - v * chartH,
  }))

  // Build energy area path
  let energyPathD = ''
  let energyAreaD = ''
  if (energyPoints.length > 1) {
    energyPathD = `M ${energyPoints[0].x} ${energyPoints[0].y}`
    for (let i = 1; i < energyPoints.length; i++) {
      const prev = energyPoints[i - 1]
      const curr = energyPoints[i]
      const cpx = (prev.x + curr.x) / 2
      energyPathD += ` C ${cpx} ${prev.y}, ${cpx} ${curr.y}, ${curr.x} ${curr.y}`
    }
    energyAreaD = energyPathD
      + ` L ${energyPoints[energyPoints.length - 1].x} ${padTop + chartH}`
      + ` L ${energyPoints[0].x} ${padTop + chartH} Z`
  }

  // Section blocks
  const sectionBlocks = sections.map((sec: TrackSection) => {
    const x = (sec.start_time / duration) * chartW
    const w = Math.max(2, ((sec.end_time - sec.start_time) / duration) * chartW)
    const colors = SECTION_COLORS[sec.type] || SECTION_COLORS.verse
    return { ...sec, x, w, colors }
  })

  // Time labels (every 15s or so)
  const timeStep = duration > 300 ? 60 : duration > 120 ? 30 : 15
  const timeLabels = []
  for (let t = 0; t <= duration; t += timeStep) {
    timeLabels.push({ t, x: (t / duration) * chartW })
  }

  return (
    <div className="tsv-root">
      <svg
        viewBox={`0 0 ${svgW} ${svgH}`}
        className="tsv-svg"
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id="tsvEnergyGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-amber-500)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="var(--color-amber-500)" stopOpacity="0.03" />
          </linearGradient>
        </defs>

        {/* Section background blocks */}
        {sectionBlocks.map((sec, i) => (
          <g key={i}>
            <rect
              x={sec.x}
              y={padTop}
              width={sec.w}
              height={chartH}
              fill={sec.colors.bg}
              rx={2}
            />
            <line
              x1={sec.x}
              y1={padTop}
              x2={sec.x}
              y2={padTop + chartH}
              stroke={sec.colors.border}
              strokeWidth={1}
              strokeDasharray="3,3"
            />
            {/* Section label */}
            {sec.w > 30 && (
              <text
                x={sec.x + sec.w / 2}
                y={padTop + 10}
                textAnchor="middle"
                className="tsv-section-label"
                fill={sec.colors.border}
              >
                {sec.colors.label}
              </text>
            )}
            {/* Drop marker — red diamond */}
            {sec.type === 'drop' && sec.w > 15 && (
              <polygon
                points={`${sec.x + sec.w / 2},${padTop + 16} ${sec.x + sec.w / 2 + 4},${padTop + 22} ${sec.x + sec.w / 2},${padTop + 28} ${sec.x + sec.w / 2 - 4},${padTop + 22}`}
                fill="var(--color-crimson-500)"
                opacity={0.8}
              />
            )}
          </g>
        ))}

        {/* Transition zone highlight */}
        {transitionZone && (
          <rect
            x={(transitionZone.startSec / duration) * chartW}
            y={padTop}
            width={Math.max(4, ((transitionZone.endSec - transitionZone.startSec) / duration) * chartW)}
            height={chartH}
            fill="rgba(167,139,250,0.15)"
            stroke="var(--color-violet-400)"
            strokeWidth={1.5}
            strokeDasharray="4,2"
            rx={3}
          />
        )}

        {/* Phrase boundaries — vertical dashed lines */}
        {phraseBoundaries.map((t: number, i: number) => {
          const x = (t / duration) * chartW
          return (
            <line
              key={`pb-${i}`}
              x1={x}
              y1={padTop}
              x2={x}
              y2={padTop + chartH}
              stroke="var(--color-text-muted)"
              strokeWidth={0.8}
              strokeDasharray="2,4"
              opacity={0.5}
            />
          )
        })}

        {/* Energy area fill */}
        {energyAreaD && (
          <path d={energyAreaD} fill="url(#tsvEnergyGrad)" />
        )}

        {/* Energy curve line */}
        {energyPathD && (
          <path d={energyPathD} fill="none" stroke="var(--color-amber-500)" strokeWidth="1.5" />
        )}

        {/* Time axis */}
        <line x1={0} y1={padTop + chartH} x2={chartW} y2={padTop + chartH} stroke="var(--color-border-subtle)" strokeWidth={1} />
        {timeLabels.map((tl, i) => (
          <g key={i}>
            <line x1={tl.x} y1={padTop + chartH - 3} x2={tl.x} y2={padTop + chartH + 3} stroke="var(--color-border-subtle)" strokeWidth={1} />
            <text x={tl.x} y={padTop + chartH + 14} textAnchor="middle" className="tsv-time-label">
              {secToMMSS(tl.t)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  )
}
