/* ============================================================
   DARAVE — Left Navigation Rail
   64px fixed-width column; icon + label; 4 destinations.
   ============================================================ */

import { useNavigate, useLocation } from 'react-router-dom'
import {
  Library,
  Headphones,
  Settings,
  Radio,
  Map,
  Bug,
  Globe,
  Folder,
  Disc3,
  type LucideIcon,
} from 'lucide-react'
import { useShallow } from 'zustand/react/shallow'
import { useAppStore } from '@/stores/appStore'
import { useTranslation, LANG_OPTIONS } from '@/i18n'
import type { NavDestination } from '@/types'
import './LeftRail.css'

interface NavItem {
  id: NavDestination
  path: string
  icon: LucideIcon
  labelKey: string
  shortKey: string
}

const NAV_ITEMS: NavItem[] = [
  { id: 'strategy',  path: '/strategy',  icon: Map,       labelKey: 'nav.strategy',  shortKey: 'nav.short.strategy' },
  { id: 'library',   path: '/library',   icon: Library,    labelKey: 'nav.library',   shortKey: 'nav.short.library' },
  { id: 'solo',      path: '/solo',      icon: Headphones,  labelKey: 'nav.solo',      shortKey: 'nav.short.solo' },
  { id: 'mix-deck',  path: '/mix-deck',  icon: Disc3,      labelKey: 'nav.mixdeck',   shortKey: 'nav.short.mixdeck' },
  { id: 'outputs',   path: '/outputs',   icon: Folder,     labelKey: 'nav.outputs',   shortKey: 'nav.short.outputs' },
  { id: 'downloads', path: '/downloads', icon: Settings,   labelKey: 'nav.import', shortKey: 'nav.short.import' },
]

function ConnectionDot() {
  const { apiHealth, sseConnected } = useAppStore(
    useShallow((s) => ({
      apiHealth: s.apiHealth,
      sseConnected: s.sseConnected,
    })),
  )
  const { t } = useTranslation()

  const cls =
    apiHealth === 'ok' && sseConnected
      ? 'pulse-dot pulse-dot--green'
      : apiHealth === 'degraded'
        ? 'pulse-dot pulse-dot--amber'
        : 'pulse-dot pulse-dot--crimson'

  const title =
    apiHealth === 'ok' && sseConnected
      ? t('inspector.connected') + ' — live stream active'
      : apiHealth === 'degraded'
        ? t('inspector.off') + ' — API reachable, no live stream'
        : t('inspector.off')

  return <span className={cls} title={title} />
}

export function LeftRail() {
  const navigate = useNavigate()
  const location = useLocation()
  const setActiveNav = useAppStore((s) => s.setActiveNav)
  const { lang, setLang, t } = useTranslation()

  function handleNav(item: NavItem) {
    setActiveNav(item.id)
    navigate(item.path)
  }

  function cycleLang() {
    const langs = LANG_OPTIONS.map((o) => o.value)
    const idx = langs.indexOf(lang)
    const next = langs[(idx + 1) % langs.length]
    setLang(next)
  }

  const currentPath = location.pathname.replace(/\/$/, '') || '/strategy'

  return (
    <nav className="left-rail" aria-label="Primary navigation">
      {/* Wordmark / logo */}
      <div className="left-rail__logo" title="DARAVE">
        <Radio size={22} strokeWidth={1.5} className="left-rail__logo-icon" />
      </div>

      <div className="left-rail__divider" />

      {/* Nav items */}
      <ul className="left-rail__nav" role="list">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          const isActive = currentPath.startsWith(item.path)
          return (
            <li key={item.id}>
              <button
                className={`left-rail__item ${isActive ? 'left-rail__item--active' : ''}`}
                onClick={() => handleNav(item)}
                title={t(item.labelKey)}
                aria-label={t(item.labelKey)}
                aria-current={isActive ? 'page' : undefined}
              >
                <Icon size={18} strokeWidth={isActive ? 2 : 1.5} />
                <span className="left-rail__item-label">{t(item.shortKey)}</span>
                {isActive && <span className="left-rail__item-indicator" aria-hidden="true" />}
              </button>
            </li>
          )
        })}
      </ul>

      {/* Bottom: debug toggle + language + connection status */}
      <div className="left-rail__footer">
        <button
          className="left-rail__lang-btn"
          onClick={cycleLang}
          title={LANG_OPTIONS.find((o) => o.value === lang)?.label}
          aria-label="Change language"
        >
          <Globe size={14} />
          <span className="left-rail__lang-flag">{LANG_OPTIONS.find((o) => o.value === lang)?.flag}</span>
        </button>
        <button
          className="left-rail__lang-btn"
          onClick={() => useAppStore.getState().toggleInspector()}
          title="Debug Mode"
          aria-label="Toggle debug inspector"
        >
          <Bug size={14} />
        </button>
        <div className="left-rail__status-dot">
          <ConnectionDot />
        </div>
      </div>
    </nav>
  )
}
