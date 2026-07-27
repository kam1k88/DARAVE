/* ============================================================
   EnergyArcChart — SVG energy arc visualization
   Bezier-curve energy arc with track labels.
   ============================================================ */

import type { MixPlanEnergyArc } from '@/types'
import './EnergyArcChart.css'

interface Props {
  data: MixPlanEnergyArc[]
  height?: number
}

export function EnergyArcChart({ data, height = 180 }: Props) {
  if (!data.length) return null

  const padding = { top: 20, right: 60, bottom: 40, left: 16 }
  const totalDuration = data[data.length - 1].end_sec
  const width = Math.max(600, data.length * 120)

  const innerW = width - padding.left - padding.right
  const innerH = height - padding.top - padding.bottom

  // Map data to points
  const points = data.map((d) => ({
    x: padding.left + (d.start_sec / totalDuration) * innerW,
    y: padding.top + (1 - d.energy_mean) * innerH,
    endX: padding.left + (d.end_sec / totalDuration) * innerW,
    energy: d.energy_mean,
    std: d.energy_std,
    name: d.song_name,
    startLabel: d.start_label,
    endLabel: d.end_label,
  }))

  // Build smooth bezier path through midpoints
  const midpoints = points.map((p) => ({
    x: (p.x + p.endX) / 2,
    y: p.y,
  }))

  let pathD = `M ${midpoints[0].x} ${midpoints[0].y}`
  for (let i = 1; i < midpoints.length; i++) {
    const prev = midpoints[i - 1]
    const curr = midpoints[i]
    const cpx1 = prev.x + (curr.x - prev.x) * 0.4
    const cpx2 = prev.x + (curr.x - prev.x) * 0.6
    pathD += ` C ${cpx1} ${prev.y}, ${cpx2} ${curr.y}, ${curr.x} ${curr.y}`
  }

  // Fill area under curve
  const fillD = pathD
    + ` L ${midpoints[midpoints.length - 1].x} ${padding.top + innerH}`
    + ` L ${midpoints[0].x} ${padding.top + innerH} Z`

  const energyColor = (e: number) =>
    e > 0.7 ? 'var(--color-crimson-500)' :
    e > 0.4 ? 'var(--color-amber-500)' :
    'var(--color-green-500)'

  return (
    <div className="energy-arc" style={{ height }}>
      <svg viewBox={`0 0 ${width} ${height}`} className="energy-arc__svg">
        <defs>
          <linearGradient id="energyArcGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-amber-500)" stopOpacity="0.25" />
            <stop offset="100%" stopColor="var(--color-amber-500)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Fill under curve */}
        <path d={fillD} fill="url(#energyArcGrad)" />

        {/* Main curve */}
        <path d={pathD} fill="none" stroke="var(--color-amber-500)" strokeWidth="2" />

        {/* Data points + labels */}
        {points.map((p, i) => (
          <g key={i}>
            <circle cx={p.x} cy={p.y} r={4} fill={energyColor(p.energy)} />
            <text
              x={p.x}
              y={p.y - 8}
              textAnchor="middle"
              className="energy-arc__val"
            >
              {Math.round(p.energy * 100)}
            </text>
            <text
              x={p.x}
              y={padding.top + innerH + 14}
              textAnchor="middle"
              className="energy-arc__label"
            >
              {p.name.length > 12 ? p.name.slice(0, 12) + '…' : p.name}
            </text>
            <text
              x={p.x}
              y={padding.top + innerH + 26}
              textAnchor="middle"
              className="energy-arc__time"
            >
              {p.startLabel}
            </text>
          </g>
        ))}

        {/* End time of last track */}
        {points.length > 0 && (
          <text
            x={points[points.length - 1].endX}
            y={padding.top + innerH + 26}
            textAnchor="middle"
            className="energy-arc__time"
          >
            {data[data.length - 1].end_label}
          </text>
        )}
      </svg>
    </div>
  )
}
