/* ============================================================
   DARAVE — Tool Call Parser
   Extracts tool calls from LLM response text.
   Used by ChatPanel and multiDeckAgent.
   ============================================================ */

import { FRONTEND_TOOLS, type FrontendToolCall } from './agentTools'

export function extractBalancedJson(text: string, startIdx: number): string | null {
  let start = startIdx
  while (start < text.length && text[start] !== '{') start++
  if (start >= text.length) return null

  let depth = 0
  let end = start
  let inString = false
  let escape = false

  for (let i = start; i < text.length; i++) {
    const ch = text[i]
    if (escape) { escape = false; continue }
    if (ch === '\\' && inString) { escape = true; continue }
    if (ch === '"') { inString = !inString; continue }
    if (inString) continue
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }

  if (depth !== 0) return null
  return text.slice(start, end + 1)
}

function findJsonStartBefore(text: string, markerIdx: number): string | null {
  // Search backwards from markerIdx to find the opening { of the JSON object
  for (let i = markerIdx - 1; i >= 0 && i >= markerIdx - 5; i--) {
    if (text[i] === '{') {
      return extractBalancedJson(text, i)
    }
  }
  return null
}

export function extractToolCalls(text: string): FrontendToolCall[] {
  const calls: FrontendToolCall[] = []
  const knownToolNames = new Set(FRONTEND_TOOLS.map((t) => t.name))

  // Strategy 1: find {"tool_call": {...}} format
  const toolCallMarker = '"tool_call"'
  let searchFrom = 0
  while (true) {
    const idx = text.indexOf(toolCallMarker, searchFrom)
    if (idx === -1) break

    const jsonStr = findJsonStartBefore(text, idx)
    if (!jsonStr) { searchFrom = idx + toolCallMarker.length; continue }

    try {
      const parsed = JSON.parse(jsonStr)
      const tc = parsed.tool_call
      if (tc && tc.name && typeof tc.name === 'string') {
        calls.push({
          id: tc.id || `call_test`,
          name: tc.name,
          arguments: tc.arguments || tc.parameters || {},
        })
      }
    } catch { /* skip */ }

    searchFrom = idx + jsonStr.length
  }

  if (calls.length > 0) return calls

  // Strategy 2: standalone JSON objects with "name" matching a known tool
  const SCAN_LIMIT = 1000
  const MAX_JSON_ATTEMPTS = 10
  const scanStart = Math.max(0, text.length - SCAN_LIMIT)
  let pos = scanStart
  let attempts = 0

  while (pos < text.length && attempts < MAX_JSON_ATTEMPTS) {
    const braceIdx = text.indexOf('{', pos)
    if (braceIdx === -1) break
    attempts++

    const jsonStr = extractBalancedJson(text, braceIdx)
    if (!jsonStr) { pos = braceIdx + 1; continue }

    try {
      const parsed = JSON.parse(jsonStr)
      if (parsed.name && typeof parsed.name === 'string' && knownToolNames.has(parsed.name)) {
        calls.push({
          id: parsed.id || `call_test`,
          name: parsed.name,
          arguments: parsed.arguments || parsed.parameters || {},
        })
      }
    } catch { /* skip */ }

    pos = braceIdx + jsonStr.length
  }

  return calls
}

const KNOWN_TOOL_NAMES_RE = '(?:load_track_to_deck|play_deck|pause_deck|stop_deck|set_crossfader|set_volume|set_effect|check_compatibility|start_remix|get_deck_info|list_library)'

export function stripToolCallJson(text: string): string {
  let result = text
  // Remove {"tool_call": {...}} blocks (supports nested braces)
  result = result.replace(new RegExp(`\\{"tool_call"\\s*:\\s*\\{(?:[^{}]|\\{[^{}]*\\})*\\}\\s*\\}`, 'g'), '')
  // Remove standalone {"name": "TOOL_NAME", ...} blocks (supports one level of nesting)
  result = result.replace(new RegExp(`\\{"name"\\s*:\\s*"${KNOWN_TOOL_NAMES_RE}"\\s*,\\s*(?:\\{(?:[^{}]|\\{[^{}]*\\})*\\}|[^}])*\\}`, 'g'), '')
  return result.trim()
}
