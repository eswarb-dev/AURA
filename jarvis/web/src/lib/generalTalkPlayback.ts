export function generalCartesiaReason(error: unknown): string {
  const value = String(error).toLowerCase()
  if (/402|payment|credits/.test(value)) return 'cartesia_payment_required_or_credits_low'
  if (/401|403|auth|invalid_key|forbidden/.test(value)) return 'cartesia_auth_failed'
  if (/429|rate|quota/.test(value)) return 'cartesia_rate_limited_or_quota'
  if (/timeout|timed out/.test(value)) return 'cartesia_timeout'
  if (/empty|missing.*audio|audio_missing/.test(value)) return 'cartesia_empty_audio'
  if (/voice|model|payload|404|422/.test(value)) return 'cartesia_voice_or_model_invalid'
  return 'cartesia_network_error'
}

export const sanitizeGeneralChunk = (text: string) => text
  .replace(/[\u2014\u2013|]/g, ', ').replace(/\u2011/g, '-')
  .replace(/[\u2018\u2019]/g, "'").replace(/[\u201c\u201d]/g, '"')
  .replace(/[*_`#]/g, '').replace(/\s+/g, ' ').trim()

type ChunkAudio = { ok: boolean; provider: string; audioBlob: Blob | null; error: string | null }

export function splitCartesiaText(text: string, maxChars = 220): string[] {
  const chunks: string[] = []
  for (const sentence of sanitizeGeneralChunk(text).split(/(?<=[.!?])\s+/)) {
    let chunk = ''
    for (const word of sentence.split(/\s+/).filter(Boolean)) {
      if (word.length > maxChars) throw new Error('cartesia_invalid_payload')
      if (chunk && chunk.length + word.length + 1 > maxChars) {
        chunks.push(chunk)
        chunk = ''
      }
      chunk = `${chunk} ${word}`.trim()
    }
    if (chunk) chunks.push(chunk)
  }
  return chunks
}

export async function runGeneralTalkQueue(chunks: string[], deps: {
  fetch: (text: string) => Promise<ChunkAudio>
  play: (blob: Blob, onStart: () => void) => Promise<unknown>
  active: (index: number) => void
  cancelled: () => boolean
  log: (message: string) => void
  prefetch?: number
}): Promise<number> {
  let played = 0
  let stopped = false
  let endedAt: number | null = null
  const prefetch = Math.max(1, Math.min(2, deps.prefetch ?? 2))
  const pending = new Map<number, Promise<{ audio?: ChunkAudio; error?: unknown }>>()
  deps.log(`[AURA TTS PIPELINE] enabled=true provider=cartesia chunks=${chunks.length} prefetch=${prefetch}`)
  deps.log(`[AURA GENERAL TTS] segmented=true chunks=${chunks.length}`)
  if (!chunks.length) throw new Error('cartesia_empty_audio')
  const request = async (index: number): Promise<ChunkAudio> => {
    if (deps.cancelled()) throw new Error('cancelled')
    const text = sanitizeGeneralChunk(chunks[index])
    deps.log(`[AURA TTS PIPELINE] chunk_request_started index=${index + 1} total=${chunks.length}`)
    deps.log(`[AURA GENERAL TTS] chunk_started index=${index + 1} total=${chunks.length} chars=${text.length}`)
    let audio: ChunkAudio | undefined
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        audio = await deps.fetch(attempt ? sanitizeGeneralChunk(text).replace(/:/g, ',') : text)
        if (audio.ok && audio.provider === 'cartesia' && audio.audioBlob?.size) break
        throw new Error(audio.error || 'cartesia_empty_audio')
      } catch (error) {
        if (stopped || deps.cancelled()) throw new Error('cancelled')
        deps.log(`[AURA GENERAL TTS] chunk_failed index=${index + 1} reason=${generalCartesiaReason(error)}`)
        if (attempt) throw error
        deps.log('[AURA GENERAL TTS] retry_sanitized_chunk=true')
      }
    }
    if (deps.cancelled()) throw new Error('cancelled')
    if (!audio?.audioBlob) throw new Error('cartesia_empty_audio')
    deps.log(`[AURA TTS PIPELINE] chunk_ready index=${index + 1} audio_bytes=${audio.audioBlob.size}`)
    return audio
  }
  const schedule = (index: number) => {
    if (index >= chunks.length || pending.has(index) || stopped || deps.cancelled()) return
    // Observe speculative failures immediately; propagate them in playback order.
    pending.set(index, request(index).then(audio => ({ audio }), error => ({ error })))
  }
  try {
  schedule(0)
  for (let index = 0; index < chunks.length; index++) {
    if (deps.cancelled()) throw new Error('cancelled')
    schedule(index)
    const result = await pending.get(index)!
    pending.delete(index)
    if (result.error) throw result.error
    const audio = result.audio
    if (!audio?.audioBlob) throw new Error('cartesia_empty_audio')
    if (deps.cancelled()) throw new Error('cancelled')
    deps.log(`[TTS] provider=cartesia audio_base64=true audio_bytes=${audio.audioBlob.size} playback=cloud_audio`)
    await deps.play(audio.audioBlob, () => {
      deps.log(`[AURA TTS PIPELINE] chunk_playback_started index=${index + 1} total=${chunks.length} gap_ms=${endedAt === null ? 0 : Math.round(performance.now() - endedAt)}`)
      deps.active(index)
      for (let next = index + 1; next <= index + prefetch; next++) {
        if (next < chunks.length && !pending.has(next)) {
          deps.log(`[AURA TTS PIPELINE] chunk_prefetch_started index=${next + 1} total=${chunks.length}`)
          schedule(next)
        }
      }
    })
    endedAt = performance.now()
    if (deps.cancelled()) throw new Error('cancelled')
    played++
    deps.log(`[AURA GENERAL TTS] chunk_played index=${index + 1} total=${chunks.length}`)
  }
  deps.log(`[AURA TTS PIPELINE] completed chunks=${chunks.length}`)
  deps.log('[AURA GENERAL TTS] read_aloud_completed speech_played=true')
  return played
  } finally {
    stopped = true
    pending.clear()
  }
}
