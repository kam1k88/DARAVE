/* ============================================================
   DARAVE — Frontend Agent Tools
   Tools the AI can invoke to control the DJ interface.
   These run client-side and manipulate the MixDeck state.
   ============================================================ */

import type { SongInfo } from '@/types'

export interface FrontendTool {
  name: string
  description: string
  parameters: Record<string, { type: string; description: string; required?: boolean }>
}

export interface FrontendToolCall {
  id: string
  name: string
  arguments: Record<string, unknown>
}

export interface FrontendToolResult {
  id: string
  name: string
  result: string
  success: boolean
}

// All available frontend tools
export const FRONTEND_TOOLS: FrontendTool[] = [
  {
    name: 'load_track_to_deck',
    description: 'Load a track from the library into Deck A or Deck B',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B"', required: true },
      track_name: { type: 'string', description: 'Name of the track from the library', required: true },
    },
  },
  {
    name: 'play_deck',
    description: 'Start playback on a deck',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B", or "all" for both', required: false },
    },
  },
  {
    name: 'pause_deck',
    description: 'Pause playback on a deck',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B", or "all" for both', required: false },
    },
  },
  {
    name: 'stop_deck',
    description: 'Stop playback and return to beginning',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B", or "all" for both', required: false },
    },
  },
  {
    name: 'set_crossfader',
    description: 'Set the crossfader position. 0.0 = full A, 1.0 = full B, 0.5 = center',
    parameters: {
      position: { type: 'number', description: 'Crossfader position from 0.0 to 1.0', required: true },
    },
  },
  {
    name: 'set_volume',
    description: 'Set volume for a deck',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B"', required: true },
      volume: { type: 'number', description: 'Volume level from 0.0 to 1.0', required: true },
    },
  },
  {
    name: 'set_effect',
    description: 'Apply an effect to a deck',
    parameters: {
      deck: { type: 'string', description: 'Which deck: "A" or "B"', required: true },
      effect: { type: 'string', description: 'Effect name (e.g., "echo", "reverb", "filter", "none")', required: true },
    },
  },
  {
    name: 'check_compatibility',
    description: 'Check harmonic and tempo compatibility between the two loaded tracks',
    parameters: {},
  },
  {
    name: 'start_remix',
    description: 'Start a server-side remix/mix of the two loaded tracks',
    parameters: {
      transition_bars: { type: 'number', description: 'Number of bars for the transition (8-128)', required: false },
    },
  },
  {
    name: 'get_deck_info',
    description: 'Get current state of both decks (loaded tracks, BPM, playing status)',
    parameters: {},
  },
  {
    name: 'list_library',
    description: 'List all available tracks in the library',
    parameters: {},
  },
]

// Agent state that the ChatPanel can use
export interface AgentState {
  songA: string
  songB: string
  isPlaying: boolean
  crossfader: number
  volumeA: number
  volumeB: number
  effectA: string
  effectB: string
  library: SongInfo[]
}

// Callbacks the ChatPanel will provide to execute tools
export interface AgentCallbacks {
  loadTrack: (deck: 'A' | 'B', trackName: string) => Promise<string>
  play: (deck?: 'A' | 'B') => Promise<string>
  pause: (deck?: 'A' | 'B') => Promise<string>
  stop: (deck?: 'A' | 'B') => Promise<string>
  setCrossfader: (position: number) => Promise<string>
  setVolume: (deck: 'A' | 'B', volume: number) => Promise<string>
  setEffect: (deck: 'A' | 'B', effect: string) => Promise<string>
  checkCompatibility: () => Promise<string>
  startRemix: (transitionBars?: number) => Promise<string>
  getDeckInfo: () => Promise<string>
  listLibrary: () => Promise<string>
}

export async function executeFrontendTool(
  toolCall: FrontendToolCall,
  callbacks: AgentCallbacks,
): Promise<FrontendToolResult> {
  const { id, name, arguments: args } = toolCall

  try {
    let result: string

    switch (name) {
      case 'load_track_to_deck': {
        const deck = (args.deck as string)?.toUpperCase() as 'A' | 'B'
        const trackName = args.track_name as string
        if (!deck || !trackName) throw new Error('deck and track_name required')
        result = await callbacks.loadTrack(deck, trackName)
        break
      }
      case 'play_deck': {
        const deck = (args.deck as string)?.toUpperCase()
        result = await callbacks.play(deck === 'A' || deck === 'B' ? deck : undefined)
        break
      }
      case 'pause_deck': {
        const deck = (args.deck as string)?.toUpperCase()
        result = await callbacks.pause(deck === 'A' || deck === 'B' ? deck : undefined)
        break
      }
      case 'stop_deck': {
        const deck = (args.deck as string)?.toUpperCase()
        result = await callbacks.stop(deck === 'A' || deck === 'B' ? deck : undefined)
        break
      }
      case 'set_crossfader': {
        const pos = Number(args.position)
        if (isNaN(pos) || pos < 0 || pos > 1) throw new Error('position must be 0.0-1.0')
        result = await callbacks.setCrossfader(pos)
        break
      }
      case 'set_volume': {
        const deck = (args.deck as string)?.toUpperCase() as 'A' | 'B'
        const vol = Number(args.volume)
        if (!deck || isNaN(vol)) throw new Error('deck and volume required')
        result = await callbacks.setVolume(deck, vol)
        break
      }
      case 'set_effect': {
        const deck = (args.deck as string)?.toUpperCase() as 'A' | 'B'
        const effect = args.effect as string
        if (!deck || !effect) throw new Error('deck and effect required')
        result = await callbacks.setEffect(deck, effect)
        break
      }
      case 'check_compatibility':
        result = await callbacks.checkCompatibility()
        break
      case 'start_remix': {
        const bars = args.transition_bars ? Number(args.transition_bars) : undefined
        result = await callbacks.startRemix(bars)
        break
      }
      case 'get_deck_info':
        result = await callbacks.getDeckInfo()
        break
      case 'list_library':
        result = await callbacks.listLibrary()
        break
      default:
        throw new Error(`Unknown tool: ${name}`)
    }

    return { id, name, result, success: true }
  } catch (err) {
    return {
      id,
      name,
      result: `Error: ${err instanceof Error ? err.message : String(err)}`,
      success: false,
    }
  }
}
