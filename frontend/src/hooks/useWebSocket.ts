/* ============================================================
   DARAVE — React hook for WebSocket DJ control.

   Provides real-time deck state and command methods.
   ============================================================ */

import { useState, useEffect, useCallback, useRef } from 'react'
import { DJWebSocket, getDJWebSocket, type SessionState, type WSMessage } from '@/lib/websocket'

export interface UseWebSocketReturn {
  state: 'connecting' | 'connected' | 'disconnected'
  session: SessionState | null
  send: DJWebSocket['send']
  loadTrack: DJWebSocket['loadTrack']
  play: DJWebSocket['play']
  pause: DJWebSocket['pause']
  stop: DJWebSocket['stop']
  setCrossfader: DJWebSocket['setCrossfader']
  setVolume: DJWebSocket['setVolume']
  setEQ: DJWebSocket['setEQ']
  setEffect: DJWebSocket['setEffect']
  setStemVolume: DJWebSocket['setStemVolume']
  seek: DJWebSocket['seek']
  setPitch: DJWebSocket['setPitch']
  batch: DJWebSocket['batch']
  connected: boolean
}

export function useWebSocket(sessionId: string = 'default'): UseWebSocketReturn {
  const wsRef = useRef<DJWebSocket>(getDJWebSocket(sessionId))
  const [connState, setConnState] = useState<'connecting' | 'connected' | 'disconnected'>('disconnected')
  const [session, setSession] = useState<SessionState | null>(null)

  useEffect(() => {
    const ws = wsRef.current

    const unsubState = ws.onStateChange(setConnState)
    const unsubMsg = ws.onMessage((msg: WSMessage) => {
      if (msg.type === 'session_state' || msg.type === 'deck_state' || msg.type === 'deck_update') {
        setSession(msg.data)
      }
    })

    ws.connect()

    return () => {
      unsubState()
      unsubMsg()
      ws.disconnect()
    }
  }, [sessionId])

  const send = useCallback((cmd: any) => wsRef.current.send(cmd), [])
  const loadTrack = useCallback((deck: string, trackName: string, trackId?: string) => wsRef.current.loadTrack(deck, trackName, trackId), [])
  const play = useCallback((deck: string) => wsRef.current.play(deck), [])
  const pause = useCallback((deck: string) => wsRef.current.pause(deck), [])
  const stop = useCallback((deck: string) => wsRef.current.stop(deck), [])
  const setCrossfader = useCallback((pos: number) => wsRef.current.setCrossfader(pos), [])
  const setVolume = useCallback((deck: string, vol: number) => wsRef.current.setVolume(deck, vol), [])
  const setEQ = useCallback((deck: string, band: 'low' | 'mid' | 'high', val: number) => wsRef.current.setEQ(deck, band, val), [])
  const setEffect = useCallback((deck: string, effect: string) => wsRef.current.setEffect(deck, effect), [])
  const setStemVolume = useCallback((deck: string, stem: string, vol: number) => wsRef.current.setStemVolume(deck, stem, vol), [])
  const seek = useCallback((deck: string, pos: number) => wsRef.current.seek(deck, pos), [])
  const setPitch = useCallback((deck: string, pitch: number) => wsRef.current.setPitch(deck, pitch), [])
  const batch = useCallback((cmds: any[]) => wsRef.current.batch(cmds), [])

  return {
    state: connState,
    session,
    send,
    loadTrack,
    play,
    pause,
    stop,
    setCrossfader,
    setVolume,
    setEQ,
    setEffect,
    setStemVolume,
    seek,
    setPitch,
    batch,
    connected: connState === 'connected',
  }
}
