/* ============================================================
   i18n — lightweight translation system
   React context + localStorage persistence.
   ============================================================ */

import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import { en } from './en'
import { ru } from './ru'
import { cn } from './cn'

export type Lang = 'en' | 'ru' | 'cn'

const LANGUAGES: Record<Lang, Record<string, string>> = { en, ru, cn }

interface I18nContextValue {
  lang: Lang
  setLang: (l: Lang) => void
  t: (key: string, params?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nContextValue>({
  lang: 'en',
  setLang: () => {},
  t: (k) => k,
})

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => {
    try {
      return (localStorage.getItem('remixmate-lang') as Lang) || 'en'
    } catch {
      return 'en'
    }
  })

  const setLang = useCallback((l: Lang) => {
    setLangState(l)
    try { localStorage.setItem('remixmate-lang', l) } catch {}
  }, [])

  const t = useCallback((key: string, params?: Record<string, string | number>): string => {
    const dict = LANGUAGES[lang] || LANGUAGES.en
    let val = dict[key] || LANGUAGES.en[key] || key
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        val = val.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v))
      }
    }
    return val
  }, [lang])

  return (
    <I18nContext.Provider value={{ lang, setLang, t }}>
      {children}
    </I18nContext.Provider>
  )
}

export function useTranslation() {
  return useContext(I18nContext)
}

export const LANG_OPTIONS: { value: Lang; label: string; flag: string }[] = [
  { value: 'en', label: 'English', flag: '🇬🇧' },
  { value: 'ru', label: 'Русский', flag: '🇷🇺' },
  { value: 'cn', label: '中文', flag: '🇨🇳' },
]
