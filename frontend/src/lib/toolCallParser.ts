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

export function stripToolCallJson(text: string): string {
  const knownToolNames = new Set(FRONTEND_TOOLS.map((t) => t.name))
  const removeRanges: Array<[number, number]> = []

  // Find all JSON objects and check if they're tool calls
  let pos = 0
  while (pos < text.length) {
    const braceIdx = text.indexOf('{', pos)
    if (braceIdx === -1) break

    const jsonStr = extractBalancedJson(text, braceIdx)
    if (!jsonStr) { pos = braceIdx + 1; continue }

    try {
      const parsed = JSON.parse(jsonStr)
      // Match standalone tool calls: {"name": "TOOL_NAME", ...} or {"tool_call": {...}}
      if (parsed.name && typeof parsed.name === 'string' && knownToolNames.has(parsed.name)) {
        removeRanges.push([braceIdx, braceIdx + jsonStr.length])
      } else if (parsed.tool_call && parsed.tool_call.name && knownToolNames.has(parsed.tool_call.name)) {
        removeRanges.push([braceIdx, braceIdx + jsonStr.length])
      }
    } catch { /* skip */ }

    pos = braceIdx + jsonStr.length
  }

  // Remove ranges in reverse order to preserve indices
  let result = text
  for (let i = removeRanges.length - 1; i >= 0; i--) {
    const [start, end] = removeRanges[i]
    result = result.slice(0, start) + result.slice(end)
  }

  return result.trim()
}
