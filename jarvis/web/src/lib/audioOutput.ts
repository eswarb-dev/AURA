export type AuraAudioProvider = 'cartesia' | 'cartesia_unavailable' | 'fish_audio' | 'elevenlabs' | 'browser_speech' | 'piper' | 'cloud_tts' | 'none'

export type ExtractedCloudAudio = {
  provider: AuraAudioProvider
  mimeType: string
  audioBytes: number | null
  audioBase64: string | null
  sourcePath: string | null
}

export type AuraAudioState = {
  unlocked: boolean
  provider: AuraAudioProvider
  requested: boolean
  playing: boolean
  lastText: string
  lastAudioBytes: number | null
  lastMimeType: string | null
  lastLatencyMs: number | null
  lastError: string | null
  lastStage: string
}

type AudioTrace = (stage: string, detail?: Record<string, unknown>) => void

export type AudioPlaybackCallbacks = {
  onPlaybackStart?: () => void
  onPlaybackMetadata?: (duration: number) => void
  onPlaybackTimeUpdate?: (currentTime: number, duration: number) => void
  onPlaybackEnd?: () => void
  onPlaybackError?: (error: Error) => void
}

let audioContext: AudioContext | null = null
let currentAudio: HTMLAudioElement | null = null
let currentObjectUrl: string | null = null
let audioUnlockPromise: Promise<AuraAudioState> | null = null
let speechSessionId = 0
let speechController = new AbortController()
let rejectPlayback: ((error: Error) => void) | null = null

export const getSpeechSessionId = () => speechSessionId
export const isSpeechSessionCurrent = (id: number) => id === speechSessionId && !speechController.signal.aborted
export function assertSpeechSession(id: number): void {
  if (!isSpeechSessionCurrent(id)) throw new DOMException('Speech stopped', 'AbortError')
}
export function stopSpeakingNow(): void {
  speechController.abort()
  speechSessionId++
  stopAuraAudio()
}
export function beginSpeechSession(): number {
  stopSpeakingNow()
  speechController = new AbortController()
  return speechSessionId
}

const state: AuraAudioState = {
  unlocked: false,
  provider: 'none',
  requested: false,
  playing: false,
  lastText: '',
  lastAudioBytes: null,
  lastMimeType: null,
  lastLatencyMs: null,
  lastError: null,
  lastStage: 'idle',
}

function update(partial: Partial<AuraAudioState>, trace?: AudioTrace) {
  Object.assign(state, partial)
  if (partial.lastStage) trace?.(partial.lastStage, { ...state })
}

export function sanitizeTextForTts(text: string): string {
  return text
    .replace(/\bA\s*\.\s*U\s*\.\s*R\s*\.\s*A\b/gi, 'Aura')
    .replace(/\bA\s+U\s+R\s+A\b/gi, 'Aura')
    .replace(/\bAURA\b/g, 'Aura')
}

export function getAuraAudioState(): AuraAudioState {
  return { ...state }
}

export function audioBlobFromBase64(audioBase64?: string | null, mimeType = 'audio/mpeg'): Blob | null {
  if (!audioBase64) return null
  const bytes = Uint8Array.from(atob(audioBase64), (char) => char.charCodeAt(0))
  return new Blob([bytes], { type: mimeType })
}

function normalizeAudioMimeType(value: unknown): string {
  const mime = String(value || 'audio/mpeg').toLowerCase().split(';')[0].trim()
  return ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav'].includes(mime) ? mime : 'audio/mpeg'
}

function normalizeAudioProvider(value: unknown): AuraAudioProvider {
  const provider = String(value || '').toLowerCase().replace(/-/g, '_')
  if (provider === 'cartesia') return 'cartesia'
  if (provider === 'cartesia_unavailable') return 'cartesia_unavailable'
  if (provider === 'fish_audio') return 'fish_audio'
  if (provider === 'elevenlabs') return 'elevenlabs'
  if (provider === 'browser_speech') return 'browser_speech'
  if (provider === 'piper') return 'piper'
  if (provider === 'cloud_tts') return 'cloud_tts'
  return 'cloud_tts'
}

export function extractCloudAudio(response: any): ExtractedCloudAudio {
  const candidates: Array<{ path: string; node: any }> = [
    { path: 'audio', node: response?.audio },
    { path: 'audioBase64', node: response },
    { path: 'root', node: response },
    { path: 'tts', node: response?.tts },
    { path: 'result.audio', node: response?.result?.audio },
  ]
  for (const candidate of candidates) {
    const node = candidate.node
    const rawAudioBase64 = candidate.path === 'audioBase64' ? node?.audioBase64 : node?.audio_base64
    const audioBase64 = typeof rawAudioBase64 === 'string' && rawAudioBase64.length > 0 ? rawAudioBase64 : null
    if (!audioBase64) continue
    const provider = normalizeAudioProvider(
      node.provider ||
      node.tts_provider ||
      response?.providers?.tts ||
      response?.provider ||
      response?.tts_debug?.final_provider ||
      'cloud_tts',
    )
    return {
      provider,
      mimeType: normalizeAudioMimeType(node.mime_type || response?.mime_type || 'audio/mpeg'),
      audioBytes: typeof node.audio_bytes === 'number' ? node.audio_bytes : (typeof response?.audio_bytes === 'number' ? response.audio_bytes : null),
      audioBase64,
      sourcePath: candidate.path === 'audioBase64' ? 'response.audioBase64' : `${candidate.path}.audio_base64`,
    }
  }
  return {
    provider: normalizeAudioProvider(response?.providers?.tts || response?.provider || response?.tts_debug?.final_provider || 'browser_speech'),
    mimeType: normalizeAudioMimeType(response?.audio?.mime_type || response?.mime_type || response?.tts?.mime_type || 'audio/mpeg'),
    audioBytes: response?.audio?.audio_bytes ?? response?.audio_bytes ?? response?.tts?.audio_bytes ?? null,
    audioBase64: null,
    sourcePath: 'response.audio.audio_base64, response.audioBase64, response.tts.audio_base64, response.result.audio.audio_base64',
  }
}

export async function playAuraAudioFromBase64({
  audioBase64,
  mimeType = 'audio/mpeg',
  provider = 'cloud_tts',
  trace,
  callbacks,
}: {
  audioBase64?: string | null
  mimeType?: string | null
  provider?: AuraAudioProvider
  trace?: AudioTrace
  callbacks?: AudioPlaybackCallbacks
}): Promise<AuraAudioState> {
  const safeMimeType = normalizeAudioMimeType(mimeType)
  const blob = audioBlobFromBase64(audioBase64, safeMimeType)
  if (!blob) throw new Error('missing cloud TTS audio_base64')
  return playAuraAudioBlob(blob, safeMimeType, trace, provider, callbacks)
}

export async function unlockAuraAudio(trace?: AudioTrace): Promise<AuraAudioState> {
  if (state.unlocked && audioContext?.state !== 'suspended') {
    update({ lastStage: 'audio.unlock.deduped' }, trace)
    return getAuraAudioState()
  }
  if (audioUnlockPromise) {
    update({ requested: true, lastStage: 'audio.unlock.joined', lastError: null }, trace)
    return audioUnlockPromise
  }
  audioUnlockPromise = (async () => {
    update({ requested: true, lastStage: 'audio.unlock.requested', lastError: null }, trace)
    try {
      const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext
      if (AudioContextCtor) {
        audioContext = audioContext || new AudioContextCtor()
        if (audioContext.state === 'suspended') await audioContext.resume()
        const buffer = audioContext.createBuffer(1, 1, audioContext.sampleRate)
        const source = audioContext.createBufferSource()
        const gain = audioContext.createGain()
        gain.gain.value = 0
        source.buffer = buffer
        source.connect(gain)
        gain.connect(audioContext.destination)
        source.start()
      }
      update({ unlocked: true, lastStage: 'audio.unlock.ok', lastError: null }, trace)
    } catch (e: any) {
      update({ unlocked: false, lastStage: 'audio.unlock.failed', lastError: `${e?.name || 'Error'}: ${e?.message || 'audio unlock failed'}` }, trace)
    } finally {
      audioUnlockPromise = null
    }
    return getAuraAudioState()
  })()
  return audioUnlockPromise
}

export async function playBrowserBeep(trace?: AudioTrace): Promise<AuraAudioState> {
  await unlockAuraAudio(trace)
  try {
    if (!audioContext) throw new Error('Web Audio unavailable')
    const osc = audioContext.createOscillator()
    const gain = audioContext.createGain()
    osc.frequency.value = 660
    gain.gain.setValueAtTime(0.0001, audioContext.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.035, audioContext.currentTime + 0.015)
    gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.18)
    osc.connect(gain)
    gain.connect(audioContext.destination)
    osc.start()
    osc.stop(audioContext.currentTime + 0.2)
    update({ provider: 'browser_speech', playing: true, lastStage: 'tts.audio.play.started', lastError: null }, trace)
    window.setTimeout(() => update({ playing: false, lastStage: 'tts.audio.play.ended' }, trace), 230)
  } catch (e: any) {
    update({ playing: false, lastStage: 'tts.audio.play.failed', lastError: `${e?.name || 'Error'}: ${e?.message || 'beep failed'}` }, trace)
  }
  return getAuraAudioState()
}

export async function playAuraAudioBlob(blob: Blob, mimeType = 'audio/mpeg', trace?: AudioTrace, provider: AuraAudioProvider = 'cloud_tts', callbacks?: AudioPlaybackCallbacks): Promise<AuraAudioState> {
  const sessionId = speechSessionId
  assertSpeechSession(sessionId)
  await unlockAuraAudio(trace)
  assertSpeechSession(sessionId)
  stopAuraAudio()
  const url = URL.createObjectURL(blob)
  const audio = new Audio(url)
  currentObjectUrl = url
  currentAudio = audio
  audio.preload = 'auto'
  update({ provider, requested: true, playing: false, lastAudioBytes: blob.size, lastMimeType: mimeType, lastStage: 'tts.audio.received', lastError: null }, trace)
  try {
    await new Promise<void>((resolve, reject) => {
      rejectPlayback = reject
      audio.onloadedmetadata = () => { if (isSpeechSessionCurrent(sessionId)) callbacks?.onPlaybackMetadata?.(audio.duration) }
      audio.ontimeupdate = () => { if (isSpeechSessionCurrent(sessionId)) callbacks?.onPlaybackTimeUpdate?.(audio.currentTime, audio.duration) }
      audio.onplaying = () => {
        if (!isSpeechSessionCurrent(sessionId)) { audio.pause(); return }
        update({ playing: true, lastStage: 'tts.audio.play.started' }, trace)
        callbacks?.onPlaybackStart?.()
      }
      audio.onended = () => {
        if (isSpeechSessionCurrent(sessionId)) {
          update({ playing: false, lastStage: 'tts.audio.play.ended' }, trace)
          callbacks?.onPlaybackEnd?.()
        }
        resolve()
      }
      audio.onerror = () => reject(new Error('audio element playback failed'))
      audio.play().catch(reject)
    })
  } catch (error) {
    if (isSpeechSessionCurrent(sessionId)) {
      callbacks?.onPlaybackError?.(error as Error)
      update({ playing: false, lastStage: 'tts.audio.play.failed', lastError: 'Audio playback failed' }, trace)
    }
    throw error
  } finally {
    audio.onplaying = audio.onended = audio.onerror = null
    audio.ontimeupdate = audio.onloadedmetadata = null
    URL.revokeObjectURL(url)
    if (currentAudio === audio) {
      currentAudio = null
      currentObjectUrl = null
      rejectPlayback = null
    }
  }
  return getAuraAudioState()
}

export async function speakWithBrowserSpeech(text: string, trace?: AudioTrace, callbacks?: AudioPlaybackCallbacks): Promise<AuraAudioState> {
  stopAuraAudio()
  const clean = sanitizeTextForTts(text).trim()
  update({ requested: true, provider: 'browser_speech', lastText: clean, lastStage: 'tts.provider.browser_speech', lastError: null }, trace)
  if (!clean) {
    const message = 'empty speech text'
    update({ playing: false, lastStage: 'tts.audio.play.failed', lastError: message }, trace)
    throw new Error(message)
  }
  if (!('speechSynthesis' in window)) {
    const message = 'speechSynthesis unavailable'
    update({ playing: false, lastStage: 'tts.audio.play.failed', lastError: message }, trace)
    throw new Error(message)
  }
  await unlockAuraAudio(trace)
  try {
    await new Promise<void>((resolve, reject) => {
      const utterance = new SpeechSynthesisUtterance(clean)
      const voices = window.speechSynthesis.getVoices()
      const preferredNames = ['male', 'george', 'daniel', 'arthur', 'microsoft ryan', 'google uk english male', 'ryan']
      const preferredVoice =
        voices.find((voice) => voice.lang?.toLowerCase().startsWith('en-gb') && preferredNames.some((name) => voice.name.toLowerCase().includes(name))) ||
        voices.find((voice) => voice.lang?.toLowerCase().startsWith('en-gb')) ||
        voices.find((voice) => voice.lang?.toLowerCase().startsWith('en-in')) ||
        voices.find((voice) => voice.lang?.toLowerCase().startsWith('en-us')) ||
        voices.find((voice) => voice.lang?.toLowerCase().startsWith('en'))
      utterance.voice = preferredVoice || null
      utterance.lang = preferredVoice?.lang || 'en-GB'
      utterance.rate = 0.95
      utterance.pitch = 0.92
      utterance.volume = 1
      utterance.onstart = () => {
        console.log('[AURA TTS] browser speechSynthesis started')
        update({ playing: true, lastStage: 'tts.audio.play.started' }, trace)
        callbacks?.onPlaybackStart?.()
      }
      utterance.onend = () => {
        console.log('[AURA TTS] browser speechSynthesis ended')
        update({ playing: false, lastStage: 'tts.audio.play.ended' }, trace)
        callbacks?.onPlaybackEnd?.()
        resolve()
      }
      utterance.onerror = (event) => {
        const err = new Error(event.error || 'speech synthesis failed')
        callbacks?.onPlaybackError?.(err)
        reject(err)
      }
      window.speechSynthesis.cancel()
      window.speechSynthesis.speak(utterance)
      window.setTimeout(() => {
        try { window.speechSynthesis.resume() } catch { /* noop */ }
      }, 100)
    })
  } catch (e: any) {
    const message = `${e?.name || 'Error'}: ${e?.message || 'speech failed'}`
    callbacks?.onPlaybackError?.(new Error(message))
    update({ playing: false, lastStage: 'tts.audio.play.failed', lastError: message }, trace)
    throw new Error(message)
  }
  return getAuraAudioState()
}

export function stopAuraAudio(): void {
  const audio = currentAudio
  const reject = rejectPlayback
  rejectPlayback = null
  currentAudio = null
  if (audio) {
    audio.onplaying = audio.onended = audio.onerror = null
    audio.ontimeupdate = audio.onloadedmetadata = audio.onloadeddata = null
    try {
      audio.pause()
      audio.currentTime = 0
      audio.removeAttribute('src')
      audio.load()
    } catch { /* Already detached or unloaded. */ }
  }
  reject?.(new DOMException('Speech stopped', 'AbortError'))
  try {
    window.speechSynthesis?.cancel()
  } catch {
    /* noop */
  }
  if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl)
  currentObjectUrl = null
  currentAudio = null
  state.playing = false
}

export async function fetchCloudTtsSpeech(text: string, provider = 'auto'): Promise<{
  ok: boolean
  provider: AuraAudioProvider
  audioBlob: Blob | null
  audioBytes: number | null
  mimeType: string | null
  latencyMs: number | null
  error: string | null
  message?: string
  selectedClient?: string | null
  attempts?: Record<string, unknown>[]
}> {
  const sessionId = speechSessionId
  assertSpeechSession(sessionId)
  const started = performance.now()
  const res = await fetch('/api/tts/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: sanitizeTextForTts(text), provider, include_audio: true }),
    signal: speechController.signal,
  })
  const data = await res.json()
  assertSpeechSession(sessionId)
  const latencyMs = typeof data.latency_ms === 'number' ? data.latency_ms : Math.round(performance.now() - started)
  const cloud = extractCloudAudio(data)
  if (!data.ok || !cloud.audioBase64) {
    return {
      ok: false,
      provider: cloud.provider || data.provider || 'browser_speech',
      audioBlob: null,
      audioBytes: cloud.audioBytes ?? data.audio_bytes ?? null,
      mimeType: cloud.mimeType ?? data.mime_type ?? null,
      latencyMs,
      error: data.error_type || data.error || 'tts_failed',
      message: data.message,
      selectedClient: data.selected_client ?? null,
      attempts: data.attempts || [],
    }
  }
  const mimeType = cloud.mimeType || 'audio/mpeg'
  const audioBlob = audioBlobFromBase64(cloud.audioBase64, mimeType)
  return {
    ok: true,
    provider: cloud.provider || data.provider || 'cloud_tts',
    audioBlob,
    audioBytes: cloud.audioBytes ?? audioBlob?.size ?? null,
    mimeType,
    latencyMs,
    error: null,
    selectedClient: data.selected_client ?? null,
    attempts: data.attempts || [],
  }
}

export async function fetchCartesiaTtsChunk(text: string, timeoutSeconds = 8, signal?: AbortSignal): Promise<{
  ok: boolean
  provider: AuraAudioProvider
  audioBlob: Blob | null
  audioBytes: number | null
  mimeType: string | null
  latencyMs: number | null
  error: string | null
  message?: string
  attempts?: Record<string, unknown>[]
}> {
  const sessionId = speechSessionId
  assertSpeechSession(sessionId)
  const started = performance.now()
  const res = await fetch('/api/tts/speak', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: sanitizeTextForTts(text), provider: 'cartesia', include_audio: true, timeout_seconds: timeoutSeconds }),
    signal: signal ? AbortSignal.any([signal, speechController.signal]) : speechController.signal,
  })
  const data = await res.json()
  assertSpeechSession(sessionId)
  const latencyMs = typeof data.latency_ms === 'number' ? data.latency_ms : Math.round(performance.now() - started)
  const cloud = extractCloudAudio(data)
  if (!data.ok || cloud.provider !== 'cartesia' || !cloud.audioBase64) {
    return {
      ok: false,
      provider: 'cartesia',
      audioBlob: null,
      audioBytes: cloud.audioBytes ?? data.audio_bytes ?? null,
      mimeType: cloud.mimeType ?? data.mime_type ?? null,
      latencyMs,
      error: data.error_type || data.error || 'cartesia_unavailable',
      message: data.message,
      attempts: data.attempts || [],
    }
  }
  const mimeType = cloud.mimeType || 'audio/mpeg'
  const audioBlob = audioBlobFromBase64(cloud.audioBase64, mimeType)
  return {
    ok: true,
    provider: 'cartesia',
    audioBlob,
    audioBytes: cloud.audioBytes ?? audioBlob?.size ?? null,
    mimeType,
    latencyMs,
    error: null,
    attempts: data.attempts || [],
  }
}
