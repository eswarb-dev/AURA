import { useEffect, useMemo, useRef, useState } from 'react'
import { getMemoryStatus } from '@/lib/auraApi'

export type AuraConversationTurn = {
  id: string
  timestamp: string
  user_text: string
  normalized_text: string
  aura_response: string
  spoken_text: string
  provider: string
  model?: string
  identity_summary?: string
  vision_summary?: string
}

const newId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`

export function useAuraConversationMemory(limit = 12) {
  const [conversationId, setConversationId] = useState(() => newId())
  const [turns, setTurns] = useState<AuraConversationTurn[]>([])
  const [persistentMemoryActive, setPersistentMemoryActive] = useState(false)
  const [persistentMemoryBackend, setPersistentMemoryBackend] = useState('unknown')
  const turnsRef = useRef<AuraConversationTurn[]>([])

  useEffect(() => {
    let alive = true
    getMemoryStatus().then((status) => {
      if (!alive) return
      setPersistentMemoryActive(Boolean(status.enabled))
      setPersistentMemoryBackend(String(status.backend || 'unknown'))
    }).catch(() => {
      if (!alive) return
      setPersistentMemoryActive(false)
      setPersistentMemoryBackend('unknown')
    })
    return () => { alive = false }
  }, [])

  const appendTurn = (turn: Omit<AuraConversationTurn, 'id' | 'timestamp'> & Partial<Pick<AuraConversationTurn, 'id' | 'timestamp'>>) => {
    const next = {
      id: turn.id || newId(),
      timestamp: turn.timestamp || new Date().toISOString(),
      ...turn,
    }
    setTurns((current) => {
      const updated = [...current, next].slice(-limit)
      turnsRef.current = updated
      return updated
    })
  }

  const clearMemory = () => {
    turnsRef.current = []
    setTurns([])
    setConversationId(newId())
  }

  const recentTurns = (count = 8) => turnsRef.current.slice(-count)
  const stats = useMemo(() => ({ memoryUsed: turns.length > 0, historyTurns: turns.length }), [turns.length])

  return { conversationId, turns, turnsRef, appendTurn, clearMemory, recentTurns, persistentMemoryActive, persistentMemoryBackend, ...stats }
}
