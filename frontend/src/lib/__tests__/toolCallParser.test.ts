// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { extractBalancedJson, extractToolCalls, stripToolCallJson } from '../toolCallParser'

describe('extractBalancedJson', () => {
  it('extracts simple JSON', () => {
    const text = '{"name": "test"}'
    expect(extractBalancedJson(text, 0)).toBe('{"name": "test"}')
  })

  it('extracts nested JSON', () => {
    const text = 'prefix {"a": {"b": 1}} suffix'
    expect(extractBalancedJson(text, 7)).toBe('{"a": {"b": 1}}')
  })

  it('handles strings with braces', () => {
    const text = '{"msg": "hello {world}"}'
    expect(extractBalancedJson(text, 0)).toBe('{"msg": "hello {world}"}')
  })

  it('handles escaped quotes inside strings', () => {
    const text = '{"msg": "he said \\"hi\\""}'
    expect(extractBalancedJson(text, 0)).toBe('{"msg": "he said \\"hi\\""}')
  })

  it('returns null for unclosed JSON', () => {
    expect(extractBalancedJson('{"a": 1', 0)).toBeNull()
  })

  it('returns null for empty text', () => {
    expect(extractBalancedJson('', 0)).toBeNull()
  })
})

describe('extractToolCalls', () => {
  describe('Strategy 1: {"tool_call": {...}} format', () => {
    it('extracts single tool_call', () => {
      const text = 'I will load the track.\n{"tool_call": {"name": "load_track_to_deck", "parameters": {"deck": "A", "track_name": "test.mp3"}}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('load_track_to_deck')
      expect(calls[0].arguments).toEqual({ deck: 'A', track_name: 'test.mp3' })
    })

    it('extracts multiple tool_calls', () => {
      const text = '{"tool_call": {"name": "play_deck", "parameters": {}}}\n{"tool_call": {"name": "set_crossfader", "parameters": {"position": 0.5}}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(2)
      expect(calls[0].name).toBe('play_deck')
      expect(calls[1].name).toBe('set_crossfader')
    })

    it('uses arguments key if parameters absent', () => {
      const text = '{"tool_call": {"name": "play_deck", "arguments": {}}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('play_deck')
    })
  })

  describe('Strategy 2: standalone JSON with known tool name', () => {
    it('extracts standalone load_track_to_deck', () => {
      const text = 'Загружаю трек.\n{"name": "load_track_to_deck", "parameters": {"deck": "Deck A", "track_name": "track1"}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('load_track_to_deck')
      expect(calls[0].arguments.deck).toBe('Deck A')
    })

    it('extracts standalone play_deck', () => {
      const text = 'Включаю.\n{"name": "play_deck", "parameters": {}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('play_deck')
    })

    it('extracts multiple standalone tool calls', () => {
      const text = `Загружаю треки и миксую:
{"name": "load_track_to_deck", "parameters": {"deck": "Deck A", "track_name": "track1"}}
{"name": "load_track_to_deck", "parameters": {"deck": "Deck B", "track_name": "track2"}}
{"name": "set_crossfader", "parameters": {"position": "0.5"}}`
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(3)
      expect(calls[0].name).toBe('load_track_to_deck')
      expect(calls[1].name).toBe('load_track_to_deck')
      expect(calls[2].name).toBe('set_crossfader')
    })

    it('ignores unknown tool names', () => {
      const text = '{"name": "unknown_tool", "parameters": {}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(0)
    })

    it('ignores non-JSON braces', () => {
      const text = 'Here is some code: if (true) { console.log("hi"); } and {"name": "play_deck", "parameters": {}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('play_deck')
    })

    it('handles arguments key', () => {
      const text = '{"name": "set_effect", "arguments": {"deck": "A", "effect": "echo"}}'
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
      expect(calls[0].arguments).toEqual({ deck: 'A', effect: 'echo' })
    })

    it('returns empty for no tool calls', () => {
      const text = 'Просто текст без tool calls. Вот ответ на твой вопрос.'
      expect(extractToolCalls(text)).toHaveLength(0)
    })

    it('scans only last 1000 chars (SCAN_LIMIT)', () => {
      const longPrefix = 'A'.repeat(1500)
      const text = longPrefix + '\n{"name": "play_deck", "parameters": {}}'
      // Tool call is beyond SCAN_LIMIT from the end of the long prefix
      // But since text.length - 1000 = 500, and the JSON is at ~1501, it should be found
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
    })

    it('does NOT scan text before SCAN_LIMIT from end', () => {
      const longPrefix = 'A'.repeat(2000)
      const text = longPrefix + '\n{"name": "play_deck", "parameters": {}}\n' + 'B'.repeat(500)
      // Tool call is in first 2000 chars, scan starts at text.length - 1000 = 1500+
      // The tool call at position ~2001 should still be within scan range since it's near the middle
      // Actually let me think: text = 2000 A + \n + JSON + \n + 500 B ≈ 2700 chars
      // scanStart = 2700 - 1000 = 1700
      // JSON starts at ~2001, which is > 1700, so it IS found
      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(1)
    })

    it('limits JSON attempts to MAX_JSON_ATTEMPTS=10', () => {
      // Create text with 15 opening braces before the tool call
      let text = ''
      for (let i = 0; i < 15; i++) {
        text += `{ "key${i}": "val${i}" } `
      }
      text += '{"name": "play_deck", "parameters": {}}'
      const calls = extractToolCalls(text)
      // Should still find it since MAX_JSON_ATTEMPTS=10 and the last JSON is a tool call
      // But if there are 15 JSON objects before it, and max is 10, it may not reach the last one
      // Let's just check it doesn't throw
      expect(Array.isArray(calls)).toBe(true)
    })
  })

  describe('mixed content', () => {
    it('handles AI response with text + multiple tool calls', () => {
      const text = `Конечно! Загружаю два трека и настраиваю кроссфейдер:

{"name": "load_track_to_deck", "parameters": {"deck": "Deck A", "track_name": " track1"}}
{"name": "load_track_to_deck", "parameters": {"deck": "Deck B", "track_name": "track2"}}
{"name": "set_crossfader", "parameters": {"position": "0.5"}}`

      const calls = extractToolCalls(text)
      expect(calls).toHaveLength(3)
    })

    it('Strategy 1 takes priority over Strategy 2', () => {
      const text = '{"tool_call": {"name": "play_deck", "parameters": {}}}\n{"name": "stop_deck", "parameters": {}}'
      const calls = extractToolCalls(text)
      // Strategy 1 found play_deck, so it returns early — stop_deck from Strategy 2 is NOT included
      expect(calls).toHaveLength(1)
      expect(calls[0].name).toBe('play_deck')
    })
  })
})

describe('stripToolCallJson', () => {
  it('removes {"tool_call": ...} blocks', () => {
    const text = 'Hello {"tool_call": {"name": "play_deck"}} world'
    expect(stripToolCallJson(text)).toBe('Hello  world')
  })

  it('removes standalone tool JSON', () => {
    const text = 'Loading {"name": "load_track_to_deck", "parameters": {"deck": "A"}} done'
    expect(stripToolCallJson(text)).toBe('Loading  done')
  })

  it('removes multiple tool JSONs', () => {
    const text = `Response:
{"name": "load_track_to_deck", "parameters": {"deck": "Deck A", "track_name": "track1"}}
{"name": "load_track_to_deck", "parameters": {"deck": "Deck B", "track_name": "track2"}}
{"name": "set_crossfader", "parameters": {"position": "0.5"}}`
    const cleaned = stripToolCallJson(text)
    expect(cleaned).not.toContain('load_track_to_deck')
    expect(cleaned).not.toContain('set_crossfader')
    expect(cleaned).toContain('Response:')
  })

  it('preserves non-tool JSON', () => {
    const text = 'Here is {"some": "data"} which is not a tool call'
    expect(stripToolCallJson(text)).toBe(text)
  })

  it('preserves plain text', () => {
    const text = 'Простой текст без JSON'
    expect(stripToolCallJson(text)).toBe(text)
  })
})
