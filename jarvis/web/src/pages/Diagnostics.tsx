import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Download, Mic, Play, Radio, RefreshCw, Volume2 } from 'lucide-react'
import { ExpoVisionIdentityMode, dispatchExpoVisionCommand } from '@/components/ExpoVisionIdentityMode'
import {
  fetchCloudTtsSpeech,
  getAuraAudioState,
  playAuraAudioBlob,
  playAuraAudioFromBase64,
  playBrowserBeep,
  speakWithBrowserSpeech,
  unlockAuraAudio,
  type AuraAudioProvider,
  type AuraAudioState,
} from '@/lib/audioOutput'
import { chooseBestAuraTranscript, normalizeAuraTranscript, type SpeechNormalizationResult } from '@/lib/speechNormalize'
import type { IdentityContext, VisionContext } from '@/lib/visionIdentity'
import { getEffectiveAddress, type AuraAddress } from '@/lib/identityAddressing'
import { clearLocalFallbackMemory, getMemoryContextPreview, getMemoryFacts, getMemoryStatus, testMemoryWrite } from '@/lib/auraApi'

type JsonMap = Record<string, any>

type VoiceTiming = {
  ui_click_start_ms?: number
  mic_permission_granted_ms?: number
  listening_started_ms?: number
  final_transcript_ms?: number
  backend_request_start_ms?: number
  backend_response_received_ms?: number
  tts_playback_start_ms?: number
  tts_playback_end_ms?: number
}

type BenchEntry = {
  id: string
  time: string
  raw: string
  normalized: string
  wake: boolean
  alias: string
  confidence: string
  action: string
  llmProvider: string
  llmMs: string
  ttsProvider: string
  ttsMs: string
  playback: string
  totalMs: string
  error: string
  responsePreview: string
  spokenPreview: string
  responseText: string
  spokenText: string
  responseTextLength: number
  spokenTextLength: number
  memoryUsed: string
  historyTurns: string
  addressUsed: string
  addressReason: string
  auraState: 'awake' | 'standby'
  startedAtMs: number
  debug: JsonMap | null
  firstTurn: boolean
  audioUnlocked: boolean
  cloudTtsReady: boolean
  ttsClient: string
  audioBase64Present: boolean
  audioBytes: number
  ttsFallbackReason: string | null
}

type AuraConversationTurn = {
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

const fmt = (value: any) => value === undefined || value === null || Number.isNaN(value) ? 'N/A' : `${Math.round(value)} ms`
const nowLabel = () => new Date().toLocaleTimeString('en-GB')
const newId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
const SLEEP_COMMANDS = new Set([
  'sleep',
  'go to sleep',
  'standby',
  'go standby',
  'sleep mode',
  'stop conversation',
  'stop listening to commands',
  "that's enough",
  'thank you aura',
  'thanks aura',
  'go offline',
])
const STOP_MIC_COMMANDS = new Set(['stop continuous mic', 'stop diagnostics mic'])
const EXPO_VISION_COMMANDS = new Set([
  'start camera',
  'stop camera',
  'start tracking',
  'stop tracking',
  'track me',
  'track owner',
  'track everyone',
  'track all people',
  'who am i',
  'who is here',
  'how many people do you see',
  'focus on me',
  'scan the room',
  'clear tracking',
])

function elapsed(t: VoiceTiming, a: keyof VoiceTiming, b: keyof VoiceTiming) {
  const start = t[a]
  const end = t[b]
  return typeof start === 'number' && typeof end === 'number' ? end - start : null
}

function SafeJson({ data }: { data: unknown }) {
  return <pre className="max-h-64 overflow-auto rounded-sm border border-[var(--line)] bg-black/35 p-3 text-[10px] leading-relaxed text-[var(--text-dim)]">{JSON.stringify(data, null, 2)}</pre>
}

export function DiagnosticsPage() {
  const [providers, setProviders] = useState<JsonMap | null>(null)
  const [openrouter, setOpenrouter] = useState<JsonMap | null>(null)
  const [elevenlabsNetwork, setElevenlabsNetwork] = useState<JsonMap | null>(null)
  const [elevenlabs, setElevenlabs] = useState<JsonMap | null>(null)
  const [voiceLoop, setVoiceLoop] = useState<JsonMap | null>(null)
  const [wsResult, setWsResult] = useState<JsonMap | null>(null)
  const [voiceTiming, setVoiceTiming] = useState<VoiceTiming>({})
  const [voiceStatus, setVoiceStatus] = useState('idle')
  const [transcript, setTranscript] = useState('')
  const [rawTranscript, setRawTranscript] = useState('')
  const [normalization, setNormalization] = useState<SpeechNormalizationResult | null>(null)
  const [confidence, setConfidence] = useState<number | null>(null)
  const [browserLang, setBrowserLang] = useState('en-IN')
  const [normalizerInput, setNormalizerInput] = useState('Hey Cosmo')
  const [audioOutput, setAudioOutput] = useState<AuraAudioState>(getAuraAudioState())
  const [fetchedAudio, setFetchedAudio] = useState<{ blob: Blob; mimeType: string; bytes: number; latencyMs: number | null; provider: AuraAudioProvider } | null>(null)
  const [groq, setGroq] = useState<JsonMap | null>(null)
  const [providerChain, setProviderChain] = useState<JsonMap | null>(null)
  const [ttsChain, setTtsChain] = useState<JsonMap | null>(null)
  const [benchActive, setBenchActive] = useState(false)
  const [wakeRequired, setWakeRequired] = useState(true)
  const [auraAwake, setAuraAwake] = useState(false)
  const [awakeSince, setAwakeSince] = useState<number | null>(null)
  const [autoResponse, setAutoResponse] = useState(true)
  const [autoTts, setAutoTts] = useState(true)
  const [cloudTtsReady, setCloudTtsReady] = useState(false)
  const [selectedTtsClient, setSelectedTtsClient] = useState<string | null>(null)
  const [audioUnlocked, setAudioUnlocked] = useState(() => getAuraAudioState().unlocked)
  const [benchEntries, setBenchEntries] = useState<BenchEntry[]>([])
  const [benchMemory, setBenchMemory] = useState<JsonMap | null>(null)
  const [persistentMemoryStatus, setPersistentMemoryStatus] = useState<JsonMap | null>(null)
  const [persistentMemoryPreview, setPersistentMemoryPreview] = useState<JsonMap | null>(null)
  const [persistentMemoryFacts, setPersistentMemoryFacts] = useState<JsonMap | null>(null)
  const [identityAddressProof, setIdentityAddressProof] = useState<JsonMap | null>(null)
  const [benchDebug, setBenchDebug] = useState<JsonMap | null>(null)
  const [identityContext, setIdentityContext] = useState<IdentityContext | null>(null)
  const [visionContext, setVisionContext] = useState<VisionContext | null>(null)
  const [conversationId, setConversationId] = useState(() => newId())
  const [conversationTurns, setConversationTurns] = useState<AuraConversationTurn[]>([])
  const [clearConversationOnStandby, setClearConversationOnStandby] = useState(false)
  const [sessionAddressOverride, setSessionAddressOverride] = useState<AuraAddress>('')
  const [logs, setLogs] = useState<string[]>([])
  const benchActiveRef = useRef(false)
  const benchSpeakingRef = useRef(false)
  const wakeRequiredRef = useRef(true)
  const auraAwakeRef = useRef(false)
  const autoResponseRef = useRef(true)
  const autoTtsRef = useRef(true)
  const cloudTtsReadyRef = useRef(false)
  const selectedTtsClientRef = useRef<string | null>(null)
  const identityContextRef = useRef<IdentityContext | null>(null)
  const visionContextRef = useRef<VisionContext | null>(null)
  const conversationTurnsRef = useRef<AuraConversationTurn[]>([])
  const conversationIdRef = useRef(conversationId)
  const clearConversationOnStandbyRef = useRef(false)
  const sessionAddressOverrideRef = useRef<AuraAddress>('')
  const recognitionRef = useRef<any>(null)
  const restartTimerRef = useRef<number | null>(null)

  const addLog = (msg: string) => setLogs((l) => [`${nowLabel()} ${msg}`, ...l].slice(0, 80))
  const traceAudio = (stage: string, detail?: Record<string, unknown>) => {
    setAudioOutput(getAuraAudioState())
    addLog(detail?.error ? `${stage}: ${detail.error}` : stage)
  }

  useEffect(() => {
    document.body.classList.add('diagnostics-page')
    document.documentElement.classList.add('diagnostics-page')
    return () => {
      document.body.classList.remove('diagnostics-page')
      document.documentElement.classList.remove('diagnostics-page')
    }
  }, [])

  const loadProviders = async () => {
    addLog('provider status requested')
    const r = await fetch('/api/diagnostics/providers')
    const data = await r.json()
    setProviders(data)
    addLog('provider status received')
  }

  useEffect(() => { loadProviders().catch((e) => addLog(`provider status failed: ${e.message}`)) }, [])

  const loadPersistentMemoryStatus = async () => {
    const data = await getMemoryStatus()
    setPersistentMemoryStatus(data)
    addLog(`persistent memory status: ${data.backend || 'unknown'}`)
    return data
  }

  const runPersistentMemoryWrite = async () => {
    const data = await testMemoryWrite()
    setPersistentMemoryStatus(await getMemoryStatus())
    addLog(`persistent memory write: ${data.ok ? 'ok' : data.error || data.message || 'failed'}`)
    return data
  }

  const runPersistentMemoryRead = async () => {
    const facts = await getMemoryFacts()
    setPersistentMemoryFacts(facts)
    addLog(`persistent memory facts: ${Array.isArray(facts.facts) ? facts.facts.length : 0}`)
    return facts
  }

  const previewPersistentMemoryContext = async () => {
    const data = await getMemoryContextPreview(conversationIdRef.current)
    setPersistentMemoryPreview(data)
    addLog(`persistent memory preview: ${data.backend || 'unknown'}`)
    return data
  }

  const clearPersistentLocalFallback = async () => {
    const data = await clearLocalFallbackMemory()
    setPersistentMemoryStatus(await getMemoryStatus())
    setPersistentMemoryFacts(null)
    setPersistentMemoryPreview(null)
    addLog(`local fallback memory clear: ${data.ok ? 'ok' : data.error || data.message || 'failed'}`)
    return data
  }

  useEffect(() => { loadPersistentMemoryStatus().catch((e) => addLog(`persistent memory status failed: ${e.message}`)) }, [])

  const testIdentityAddressResolution = async (withProfile = true) => {
    const body = withProfile ? {
      matched_profile: {
        display_name: 'Test Female Profile',
        preferred_address: "Ma'am",
        address_source: 'explicit_profile',
      },
      people_count: 1,
      owner_verified: false,
    } : {
      matched_profile: null,
      people_count: 1,
      owner_verified: false,
    }
    const r = await fetch('/api/identity/test-effective-address', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await r.json()
    setIdentityAddressProof({ request: body, response: data })
    addLog(`[AURA IDENTITY] effective_address=${data.effective_address || 'N/A'} source=${data.address_source || 'N/A'} visual_gender_inference=${String(Boolean(data.used_visual_gender_inference))}`)
    return data
  }

  useEffect(() => {
    benchActiveRef.current = benchActive
  }, [benchActive])

  useEffect(() => {
    wakeRequiredRef.current = wakeRequired
  }, [wakeRequired])

  useEffect(() => {
    auraAwakeRef.current = auraAwake
  }, [auraAwake])

  useEffect(() => {
    autoResponseRef.current = autoResponse
  }, [autoResponse])

  useEffect(() => {
    autoTtsRef.current = autoTts
  }, [autoTts])

  useEffect(() => {
    identityContextRef.current = identityContext
  }, [identityContext])

  useEffect(() => {
    visionContextRef.current = visionContext
  }, [visionContext])

  useEffect(() => {
    conversationTurnsRef.current = conversationTurns
  }, [conversationTurns])

  useEffect(() => {
    conversationIdRef.current = conversationId
  }, [conversationId])

  useEffect(() => {
    clearConversationOnStandbyRef.current = clearConversationOnStandby
  }, [clearConversationOnStandby])

  useEffect(() => {
    sessionAddressOverrideRef.current = sessionAddressOverride
  }, [sessionAddressOverride])

  const runOpenrouter = async () => {
    addLog('OpenRouter diagnostic started')
    const r = await fetch('/api/diagnostics/openrouter', { method: 'POST' })
    const data = await r.json()
    setOpenrouter(data)
    addLog(`OpenRouter diagnostic ${data.ok ? 'passed' : `failed: ${data.error}`}`)
  }

  const runGroq = async () => {
    addLog('Groq fallback diagnostic started')
    const r = await fetch('/api/diagnostics/groq', { method: 'POST' })
    const data = await r.json()
    setGroq(data)
    addLog(`Groq diagnostic ${data.ok ? 'passed' : `failed: ${data.error_type || data.error}`}`)
  }

  const runProviderChain = async () => {
    addLog('provider chain diagnostic started')
    const r = await fetch('/api/diagnostics/provider-chain', { method: 'POST' })
    const data = await r.json()
    setProviderChain(data)
    addLog(`provider chain: ${data.status || data.provider || 'unknown'}`)
  }

  const runTtsChain = async () => {
    addLog('TTS provider chain diagnostic started')
    const r = await fetch('/api/diagnostics/tts-chain', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ include_audio: false }),
    })
    const data = await r.json()
    setTtsChain(data)
    addLog(`TTS provider chain: ${data.provider || 'unknown'} ${data.ok ? 'ready' : `failed: ${data.error_type || data.error || 'unknown'}`}`)
    return data
  }

  const runElevenlabs = async (includeAudio = false) => {
    addLog('ElevenLabs diagnostic started')
    const r = await fetch('/api/diagnostics/elevenlabs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ include_audio: includeAudio }),
    })
    const data = await r.json()
    setElevenlabs(data)
    addLog(`ElevenLabs diagnostic ${data.ok ? 'passed' : `failed: ${data.error}`}`)
    return data
  }

  const runElevenlabsNetwork = async () => {
    addLog('ElevenLabs network diagnostic started')
    const r = await fetch('/api/diagnostics/elevenlabs-network')
    const data = await r.json()
    setElevenlabsNetwork(data)
    const classification = data.root?.classification || data.error || 'unknown'
    addLog(data.ok ? `ElevenLabs network ${classification}` : `ElevenLabs network failed: ${classification}`)
    return data
  }

  const unlockAudio = async () => {
    const state = await unlockAuraAudio(traceAudio)
    setAudioOutput(state)
  }

  const testBeep = async () => {
    const state = await playBrowserBeep(traceAudio)
    setAudioOutput(state)
  }

  const testBrowserSpeech = async () => {
    try {
      const state = await speakWithBrowserSpeech('A.U.R.A browser speech output is online.', traceAudio)
      setAudioOutput(state)
    } catch (e: any) {
      setAudioOutput(getAuraAudioState())
      addLog(`browser speech failed: ${e?.message || 'speech failed'}`)
    }
  }

  const testCloudTtsFetch = async () => {
    let network = elevenlabsNetwork
    if (!network) {
      network = await runElevenlabsNetwork()
    }
    traceAudio('tts.request.started')
    const testText = 'A.U.R.A cloud TTS audio fetch is online.'
    const result = await fetchCloudTtsSpeech(testText)
    setAudioOutput({
      ...getAuraAudioState(),
      provider: result.provider,
      requested: true,
      lastText: testText,
      lastAudioBytes: result.audioBytes,
      lastMimeType: result.mimeType,
      lastLatencyMs: result.latencyMs,
      lastError: result.error || result.message || null,
      lastStage: result.ok ? 'tts.audio.received' : 'tts.request.failed',
    })
    setElevenlabs({
      ok: result.ok,
      provider: result.provider,
      selected_client: result.selectedClient,
      attempts: result.attempts || [],
      audio_bytes: result.audioBytes,
      mime_type: result.mimeType,
      latency_ms: result.latencyMs,
      error_type: result.error,
      message: result.message,
    })
    if (result.ok && result.audioBlob) {
      setFetchedAudio({ blob: result.audioBlob, mimeType: result.mimeType || 'audio/mpeg', bytes: result.audioBytes || result.audioBlob.size, latencyMs: result.latencyMs, provider: result.provider })
      addLog(`[TTS] provider=${result.provider} audio_bytes=${result.audioBytes || result.audioBlob.size} playback=cloud_audio`)
    } else {
      const rootClass = network?.root?.classification || 'root_not_tested'
      addLog(`Cloud TTS fetch failed: ${result.error || result.message || 'unknown'}; elevenlabs_root=${rootClass}`)
    }
  }

  const playFetchedCloudTts = async () => {
    const audio = fetchedAudio
    if (!audio) {
      addLog('Cloud TTS playback skipped: fetch audio first')
      return
    }
    const state = await playAuraAudioBlob(audio.blob, audio.mimeType, traceAudio, audio.provider || 'cloud_tts')
    setAudioOutput({ ...state, lastAudioBytes: audio.bytes, lastMimeType: audio.mimeType, lastLatencyMs: audio.latencyMs })
  }

  const updateBenchRow = (id: string, patch: Partial<BenchEntry>) => {
    setBenchEntries((rows) => rows.map((row) => row.id === id ? { ...row, ...patch } : row))
  }

  const pushBenchEntry = (entry: Partial<BenchEntry> & Pick<BenchEntry, 'raw' | 'normalized'>) => {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`
    const isFirst = benchEntries.length === 0
    const nextRow: BenchEntry = {
      id,
      time: nowLabel(),
      wake: false,
      alias: 'N/A',
      confidence: 'N/A',
      action: 'captured',
      llmProvider: 'N/A',
      llmMs: 'N/A',
      ttsProvider: 'N/A',
      ttsMs: 'N/A',
      playback: 'N/A',
      totalMs: 'N/A',
      error: '',
      responsePreview: '',
      spokenPreview: '',
      responseText: '',
      spokenText: '',
      responseTextLength: 0,
      spokenTextLength: 0,
      memoryUsed: 'no',
      historyTurns: '0',
      addressUsed: '',
      addressReason: '',
      auraState: auraAwakeRef.current ? 'awake' : 'standby',
      startedAtMs: performance.now(),
      debug: null,
      firstTurn: isFirst,
      audioUnlocked: getAuraAudioState().unlocked,
      cloudTtsReady: cloudTtsReadyRef.current,
      ttsClient: selectedTtsClientRef.current || 'N/A',
      audioBase64Present: false,
      audioBytes: 0,
      ttsFallbackReason: null,
      ...entry,
    }
    setBenchEntries((rows) => [nextRow, ...rows].slice(0, 30))
    return id
  }

  const runTtsPreflight = async () => {
    try {
      addLog('[AURA TTS] running cloud TTS preflight...')
      const r = await fetch('/api/diagnostics/tts-preflight', { method: 'POST' })
      const data = await r.json()
      if (data.ok && data.elevenlabs_ready) {
        setCloudTtsReady(true)
        setSelectedTtsClient(data.selected_client || 'aiohttp')
        addLog(`[AURA TTS] cloud preflight ready (${data.selected_client || 'aiohttp'})`)
      } else {
        setCloudTtsReady(false)
        setSelectedTtsClient(null)
        addLog(`[AURA TTS] cloud preflight offline: ${data.error_type || data.message || 'not configured'}`)
      }
      return data
    } catch (e: any) {
      setCloudTtsReady(false)
      setSelectedTtsClient(null)
      addLog(`[AURA TTS] cloud preflight failed: ${e?.message || e}`)
      return null
    }
  }

  useEffect(() => {
    runTtsPreflight()
  }, [])

  const ensureAudioUnlocked = async (): Promise<boolean> => {
    const state = await unlockAuraAudio(traceAudio)
    setAudioOutput(state)
    setAudioUnlocked(state.unlocked)
    if (!state.unlocked) {
      addLog('audio unlock required before starting voice interaction')
    }
    return state.unlocked
  }

  const preview = (value: string) => value.replace(/\s+/g, ' ').trim().slice(0, 120)
  const summarizeIdentity = (context: IdentityContext | null) => {
    if (!context) return 'identity unavailable'
    return `${context.role || 'guest'} ${context.address || 'Guest'} ${context.decision || 'unknown'}`
  }
  const summarizeVision = (context: VisionContext | null) => {
    if (!context) return 'vision unavailable'
    return `camera=${context.camera_status}; tracking=${context.tracking_status}; people=${context.people_count}; identity=${context.current_identity}; address=${context.address_selected}; mode=${context.tracking_mode}`
  }
  const safeVisionContext = (): VisionContext => visionContextRef.current || {
    camera_status: 'off',
    tracking_status: 'off',
    people_count: 0,
    current_identity: 'guest',
    address_selected: 'Guest',
    fusion_decision: null,
    tracking_mode: 'all',
    tracks: [],
  }
  const safeIdentityContext = (): IdentityContext => identityContextRef.current || {
    decision: 'guest_or_unknown',
    display_name: 'Guest',
    role: 'guest',
    address: 'Guest',
    owner_face_status: 'unknown',
    owner_voice_status: 'unknown',
    owner_face_enrolled: false,
    owner_voice_enrolled: false,
    people_count: 0,
    tracking_mode: 'none',
    tracks: [],
  }
  const appendConversationTurn = (turn: AuraConversationTurn) => {
    setConversationTurns((prev) => [...prev, turn].slice(-12))
  }
  const clearConversationMemory = () => {
    setConversationTurns([])
    setConversationId(newId())
    addLog('memory_cleared')
  }
  const maybeClearConversationOnStandby = () => {
    if (clearConversationOnStandbyRef.current) clearConversationMemory()
  }
  const effectiveAddress = () => getEffectiveAddress({
    ownerVerified: identityContextRef.current?.role === 'owner' || visionContextRef.current?.current_identity === 'owner',
    ownerAddress: identityContextRef.current?.address || 'Sir',
    sessionAddressOverride: sessionAddressOverrideRef.current,
    currentIdentityAddress: identityContextRef.current?.address,
    currentIdentity: identityContextRef.current?.role,
    matchedProfile: (identityContextRef.current as any)?.matched_profile || null,
    preferredAddress: (identityContextRef.current as any)?.preferred_address || (visionContextRef.current as any)?.preferred_address || null,
    liveIdentityAddressAllowed: Boolean((identityContextRef.current as any)?.live_identity_address_allowed || (visionContextRef.current as any)?.live_identity_address_allowed),
    ownerProfileEnrolled: Boolean(identityContextRef.current?.owner_face_enrolled || identityContextRef.current?.owner_voice_enrolled),
  })
  const appendDeterministicTurn = (rawText: string, normalizedText: string, response: string, provider: string) => {
    appendConversationTurn({
      id: newId(),
      timestamp: new Date().toISOString(),
      user_text: rawText,
      normalized_text: normalizedText,
      aura_response: response,
      spoken_text: response,
      provider,
      identity_summary: summarizeIdentity(identityContextRef.current),
      vision_summary: summarizeVision(visionContextRef.current),
    })
  }
  const addressCorrectionResponse = (text: string): { address: AuraAddress; reason: 'session_override' | 'neutral'; response: string } | null => {
    const value = text.toLowerCase().replace(/[.,!?;:]+/g, ' ').replace(/\s+/g, ' ').trim()
    if (/\b(address|call|refer to)\s+me\s+as\s+sir\b/.test(value) || value.includes('address me as sir') || (value.includes("don't address me as guest") && value.includes('sir')) || value.includes("don't call me guest")) {
      return { address: 'Sir', reason: 'session_override', response: 'Understood, Sir. I will address you as Sir for this session.' }
    }
    if (/\b(address|call|refer to)\s+me\s+as\s+mam\b/.test(value) || value.includes('address me as mam')) {
      return { address: 'Mam', reason: 'session_override', response: 'Understood, Mam. I will address you as Mam for this session.' }
    }
    if (value.includes('use neutral address') || value.includes("don't use sir") || value.includes("don't use mam")) {
      return { address: '', reason: 'neutral', response: 'Understood. I will use neutral address for this session.' }
    }
    return null
  }
  const shouldClarify = (text: string, confidenceValue?: number) => {
    const value = text.trim().toLowerCase()
    return (typeof confidenceValue === 'number' && confidenceValue > 0 && confidenceValue < 0.45) || value === 'can you tell me is very nice'
  }

  const isSleepCommand = (text: string) => SLEEP_COMMANDS.has(text.trim().toLowerCase())
  const isStopMicCommand = (text: string) => STOP_MIC_COMMANDS.has(text.trim().toLowerCase())
  const isExpoVisionCommand = (text: string) => EXPO_VISION_COMMANDS.has(text.trim().toLowerCase())
  const isWakeOnlyCommand = (result: SpeechNormalizationResult) => {
    if (!result.wakeDetected) return false
    const text = result.normalized.trim().toLowerCase()
    return text === 'hey aura' || text === 'wake up' || text === 'aura'
  }

  const wakeAura = (reason: string) => {
    const now = Date.now()
    auraAwakeRef.current = true
    setAuraAwake(true)
    setAwakeSince(now)
    addLog(`[AURA BENCH] aura_awake: ${reason}`)
  }

  const sleepAura = (reason: string) => {
    auraAwakeRef.current = false
    setAuraAwake(false)
    setAwakeSince(null)
    addLog(`[AURA BENCH] aura_standby: ${reason}`)
  }

  const pauseContinuousRecognitionForTts = () => {
    try { recognitionRef.current?.stop() } catch { /* noop */ }
  }

  const resumeContinuousRecognitionAfterTtsIfNeeded = () => {
    if (benchActiveRef.current) {
      addLog('[AURA BENCH] continuous mic resumed')
      scheduleBenchRestart()
    }
  }

  const playBenchResponseAudio = async (data: JsonMap, rowId: string, rowStartMs: number) => {
    const responseText = String(data?.spoken_text || data?.response_text || data?.response || '').trim()
    if (!responseText) {
      updateBenchRow(rowId, { action: 'tts_failed', playback: 'no_text', error: 'empty_response_text' })
      return
    }
    if (!autoTtsRef.current) {
      updateBenchRow(rowId, { action: 'response_received', playback: 'auto_tts_off' })
      return
    }
    const ttsStart = performance.now()
    const b64 = data?.audio?.audio_base64 || data?.audio_base64
    const mimeType = data?.audio?.mime_type || data?.mime_type || 'audio/mpeg'
    const bytes = data?.audio?.audio_bytes ?? (b64 ? Math.round(b64.length * 0.75) : 0)
    const providerName = data?.providers?.tts || data?.provider || 'cloud_tts'
    const clientName = data?.debug?.tts_attempts?.[0]?.client || selectedTtsClientRef.current || 'aiohttp'

    try {
      benchSpeakingRef.current = true
      pauseContinuousRecognitionForTts()
      updateBenchRow(rowId, { action: 'tts_started', playback: 'starting' })

      if (b64) {
        addLog(`[TTS] provider=${providerName} audio_bytes=${bytes} playback=cloud_audio`)
        await playAuraAudioFromBase64({ audioBase64: b64, mimeType, provider: providerName, trace: traceAudio })
        const state = getAuraAudioState()
        if (state.lastError) throw new Error(state.lastError)
        const ttsEnd = performance.now()
        updateBenchRow(rowId, {
          action: 'tts_played',
          playback: `${providerName}_played`,
          ttsProvider: providerName,
          ttsClient: clientName,
          audioBase64Present: true,
          audioBytes: bytes,
          ttsMs: fmt(ttsEnd - ttsStart),
          totalMs: fmt(ttsEnd - rowStartMs),
          error: '',
          ttsFallbackReason: null,
        })
        return
      }

      const fallbackReason = data?.tts_error_type || data?.debug?.tts_fallback_reason || (cloudTtsReadyRef.current ? 'cloud_tts_audio_missing' : 'cloud_tts_not_configured')
      addLog(`[TTS] provider=browser_speech fallback=true error=${fallbackReason}`)
      await speakWithBrowserSpeech(responseText, traceAudio)
      const state = getAuraAudioState()
      if (state.lastError) throw new Error(state.lastError)
      const ttsEnd = performance.now()
      updateBenchRow(rowId, {
        action: 'tts_played',
        playback: 'browser_speech_played',
        ttsProvider: 'browser_speech',
        ttsClient: 'browser_speech',
        audioBase64Present: false,
        audioBytes: 0,
        ttsFallbackReason: fallbackReason,
        ttsMs: fmt(ttsEnd - ttsStart),
        totalMs: fmt(ttsEnd - rowStartMs),
        error: `browser_speech used: ${fallbackReason}`,
      })
    } catch (err: any) {
      updateBenchRow(rowId, { action: 'tts_failed', playback: 'failed', error: err?.message || String(err) })
    } finally {
      benchSpeakingRef.current = false
      resumeContinuousRecognitionAfterTtsIfNeeded()
    }
  }

  const speakBenchSystemResponse = async (rowId: string, text: string, rowStartMs: number, playbackLabel: string, provider = 'deterministic_identity', addressUsed?: string, addressReason?: string) => {
    updateBenchRow(rowId, {
      responsePreview: preview(text),
      spokenPreview: preview(text),
      responseText: text,
      spokenText: text,
      responseTextLength: text.length,
      spokenTextLength: text.length,
      llmProvider: provider,
      addressUsed: addressUsed || effectiveAddress().address || '',
      addressReason: addressReason || effectiveAddress().reason,
      ttsProvider: 'cloud_tts',
      playback: playbackLabel,
      totalMs: fmt(performance.now() - rowStartMs),
    })
    if (!autoTtsRef.current) return
    const ttsStart = performance.now()
    try {
      benchSpeakingRef.current = true
      pauseContinuousRecognitionForTts()
      updateBenchRow(rowId, { action: 'tts_started', playback: 'starting' })
      const fetched = await fetchCloudTtsSpeech(text)
      if (fetched.ok && fetched.audioBlob) {
        await playAuraAudioBlob(fetched.audioBlob, fetched.mimeType || 'audio/mpeg', traceAudio, fetched.provider)
        updateBenchRow(rowId, {
          action: 'tts_played',
          playback: `${fetched.provider}_played`,
          ttsProvider: fetched.provider,
          ttsClient: fetched.selectedClient || selectedTtsClientRef.current || 'aiohttp',
          audioBase64Present: true,
          audioBytes: fetched.audioBytes || fetched.audioBlob.size,
          ttsMs: fmt(performance.now() - ttsStart),
          totalMs: fmt(performance.now() - rowStartMs),
          error: '',
          ttsFallbackReason: null,
        })
        return
      }
      const fallbackReason = fetched.error || 'cloud_tts_unavailable'
      await speakWithBrowserSpeech(text, traceAudio)
      updateBenchRow(rowId, {
        action: 'tts_played',
        playback: 'browser_speech_played',
        ttsProvider: 'browser_speech',
        ttsClient: 'browser_speech',
        audioBase64Present: false,
        audioBytes: 0,
        ttsFallbackReason: fallbackReason,
        ttsMs: fmt(performance.now() - ttsStart),
        totalMs: fmt(performance.now() - rowStartMs),
        error: `browser_speech used: ${fallbackReason}`,
      })
    } catch (err: any) {
      updateBenchRow(rowId, { action: 'tts_failed', playback: 'failed', error: err?.message || String(err) })
    } finally {
      benchSpeakingRef.current = false
      resumeContinuousRecognitionAfterTtsIfNeeded()
    }
  }

  const handleBenchTranscript = async (rawText: string, confidenceValue?: number) => {
    const benchStart = performance.now()
    await ensureAudioUnlocked()
    const normalizedResult = normalizeAuraTranscript(rawText)
    const normalizedText = normalizedResult.normalized
    setRawTranscript(rawText)
    setTranscript(normalizedText)
    setNormalization(normalizedResult)
    setConfidence(typeof confidenceValue === 'number' ? confidenceValue : null)
    if (isStopMicCommand(normalizedText)) {
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'aura_standby',
        playback: 'stop_mic',
        startedAtMs: benchStart,
      })
      sleepAura('stop_mic_command')
      updateBenchRow(rowId, { responsePreview: 'Standing by, Sir.', spokenPreview: 'Standing by, Sir.', totalMs: fmt(performance.now() - benchStart) })
      maybeClearConversationOnStandby()
      stopContinuousMic()
      return
    }
    if (isSleepCommand(normalizedText)) {
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'aura_standby',
        playback: 'standby_ack',
        startedAtMs: benchStart,
      })
      sleepAura('user_sleep_command')
      await speakBenchSystemResponse(rowId, 'Standing by, Sir.', benchStart, 'standby_ack')
      maybeClearConversationOnStandby()
      return
    }
    if (normalizedResult.wakeDetected) {
      wakeAura(normalizedResult.wakeAliasMatched || 'wake_detected')
    }
    const inputAllowed = !wakeRequiredRef.current || normalizedResult.wakeDetected || auraAwakeRef.current
    if (!inputAllowed) {
      pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: false,
        auraState: 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'ignored_non_wake',
      })
      return
    }
    if (isWakeOnlyCommand(normalizedResult)) {
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: true,
        auraState: 'awake',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'aura_awake',
        playback: 'wake_ack',
        startedAtMs: benchStart,
      })
      await speakBenchSystemResponse(rowId, 'Awake, Sir.', benchStart, 'wake_ack')
      return
    }
    const addressCommand = addressCorrectionResponse(normalizedText)
    if (addressCommand) {
      setSessionAddressOverride(addressCommand.address)
      sessionAddressOverrideRef.current = addressCommand.address
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: auraAwakeRef.current ? 'awake' : 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'response_received',
        playback: 'address_override',
        startedAtMs: benchStart,
      })
      await speakBenchSystemResponse(rowId, addressCommand.response, benchStart, 'address_override', 'deterministic_identity', addressCommand.address || '', addressCommand.reason)
      appendDeterministicTurn(rawText, normalizedText, addressCommand.response, 'deterministic_identity')
      return
    }
    if (shouldClarify(normalizedText, confidenceValue)) {
      const address = effectiveAddress()
      const label = address.address || 'there'
      const response = `I heard you, ${label}, but that sentence was unclear. Could you repeat it once?`
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: auraAwakeRef.current ? 'awake' : 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'response_received',
        playback: 'clarification',
        startedAtMs: benchStart,
      })
      await speakBenchSystemResponse(rowId, response, benchStart, 'clarification', 'deterministic_clarification', address.address, address.reason)
      appendDeterministicTurn(rawText, normalizedText, response, 'deterministic_clarification')
      return
    }
    if (isExpoVisionCommand(normalizedText)) {
      const rowId = pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: auraAwakeRef.current ? 'awake' : 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: normalizedResult.wakeDetected || !auraAwakeRef.current ? 'sent_to_backend' : 'sent_to_backend_awake',
        playback: 'expo_vision_command',
        startedAtMs: benchStart,
      })
      dispatchExpoVisionCommand(normalizedText)
      updateBenchRow(rowId, {
        responsePreview: 'Expo Vision Identity command routed.',
        spokenPreview: 'Expo Vision Identity command routed.',
        responseText: 'Expo Vision Identity command routed.',
        spokenText: 'Expo Vision Identity command routed.',
        responseTextLength: 'Expo Vision Identity command routed.'.length,
        spokenTextLength: 'Expo Vision Identity command routed.'.length,
        totalMs: fmt(performance.now() - benchStart),
      })
      return
    }
    if (!autoResponseRef.current) {
      pushBenchEntry({
        raw: rawText,
        normalized: normalizedText,
        wake: normalizedResult.wakeDetected,
        auraState: auraAwakeRef.current ? 'awake' : 'standby',
        alias: normalizedResult.wakeAliasMatched || 'N/A',
        confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
        action: 'captured',
      })
      return
    }
    const rowId = pushBenchEntry({
      raw: rawText,
      normalized: normalizedText,
      wake: normalizedResult.wakeDetected,
      auraState: auraAwakeRef.current ? 'awake' : 'standby',
      alias: normalizedResult.wakeAliasMatched || 'N/A',
      confidence: typeof confidenceValue === 'number' ? confidenceValue.toFixed(2) : normalizedResult.confidenceHint,
      action: normalizedResult.wakeDetected || !auraAwakeRef.current ? 'sent_to_backend' : 'sent_to_backend_awake',
      startedAtMs: benchStart,
    })
    const addressForRequest = effectiveAddress()
    const visionContextForRequest = safeVisionContext()
    const identityContextForRequest = {
      ...safeIdentityContext(),
      effective_address: addressForRequest.address,
      session_address_override: sessionAddressOverrideRef.current,
      address_reason: addressForRequest.reason,
      preferred_address: (safeIdentityContext() as any).preferred_address || null,
      address_source: (safeIdentityContext() as any).address_source || null,
      matched_profile_id: (safeIdentityContext() as any).matched_profile_id || null,
      matched_profile_name: (safeIdentityContext() as any).matched_profile_name || null,
      matched_profile: (safeIdentityContext() as any).matched_profile || null,
      identity_confidence: (safeIdentityContext() as any).identity_confidence ?? null,
      owner_face_status: safeIdentityContext().owner_face_status || (safeIdentityContext().role === 'owner' ? 'verified' : 'unknown'),
      owner_voice_status: safeIdentityContext().owner_voice_status || 'unknown',
      owner_face_enrolled: safeIdentityContext().owner_face_enrolled,
      owner_voice_enrolled: safeIdentityContext().owner_voice_enrolled,
      tracking_state: (safeIdentityContext() as any).tracking_state || (visionContextForRequest as any).tracking_state || visionContextForRequest.tracking_status || 'off',
      identity_state: (safeIdentityContext() as any).identity_state || (visionContextForRequest as any).identity_state || 'guest',
      live_identity_fresh: Boolean((safeIdentityContext() as any).live_identity_fresh || (visionContextForRequest as any).live_identity_fresh),
      live_identity_address_allowed: Boolean((safeIdentityContext() as any).live_identity_address_allowed || (visionContextForRequest as any).live_identity_address_allowed),
      active_profile_is_unambiguous: Boolean((safeIdentityContext() as any).active_profile_is_unambiguous || (visionContextForRequest as any).active_profile_is_unambiguous),
      visual_gender_inference: false,
    }
    const safeVisionContextForRequest = {
      ...visionContextForRequest,
      camera_state: (visionContextForRequest as any).camera_state || visionContextForRequest.camera_status || 'off',
      stream_ready: Boolean((visionContextForRequest as any).stream_ready),
      video_ready: Boolean((visionContextForRequest as any).video_ready),
      video_width: Number((visionContextForRequest as any).video_width || 0),
      video_height: Number((visionContextForRequest as any).video_height || 0),
      tracking_state: (visionContextForRequest as any).tracking_state || visionContextForRequest.tracking_status || 'off',
      tracking_error: (visionContextForRequest as any).tracking_error || null,
      tracking_last_error_code: (visionContextForRequest as any).tracking_last_error_code || null,
      tracking_last_error_message: (visionContextForRequest as any).tracking_last_error_message || null,
      tracking_backend: (visionContextForRequest as any).tracking_backend || 'none',
      model_loaded: Boolean((visionContextForRequest as any).model_loaded),
      loop_running: Boolean((visionContextForRequest as any).loop_running),
      preferred_address: (visionContextForRequest as any).preferred_address || null,
      address_source: (visionContextForRequest as any).address_source || null,
      matched_profile_id: (visionContextForRequest as any).matched_profile_id || null,
      matched_profile_name: (visionContextForRequest as any).matched_profile_name || null,
      identity_confidence: (visionContextForRequest as any).identity_confidence ?? null,
      identity_state: (visionContextForRequest as any).identity_state || 'guest',
      owner_profile_enrolled: Boolean((visionContextForRequest as any).owner_profile_enrolled),
      live_identity_fresh: Boolean((visionContextForRequest as any).live_identity_fresh),
      live_identity_address_allowed: Boolean((visionContextForRequest as any).live_identity_address_allowed),
      active_profile_is_unambiguous: Boolean((visionContextForRequest as any).active_profile_is_unambiguous),
      visual_gender_inference: false,
      descriptors_exposed: false,
      raw_frames_uploaded: false,
    }
    const r = await fetch('/api/diagnostics/voice-bench-loop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        raw_transcript: rawText,
        normalized_transcript: normalizedText,
        wake_detected: normalizedResult.wakeDetected,
        alias_matched: normalizedResult.wakeAliasMatched,
        aura_awake: auraAwakeRef.current,
        conversation_id: conversationIdRef.current,
        recent_turns: conversationTurnsRef.current.slice(-8),
        conversation_mode: visionContextRef.current ? 'expo_vision_identity' : 'awake_latched',
        wake_required: wakeRequiredRef.current,
        identity_context: identityContextForRequest,
        vision_context: safeVisionContextForRequest,
        session_address_override: sessionAddressOverrideRef.current,
        effective_address: addressForRequest.address,
        address_reason: addressForRequest.reason,
        started_at: new Date().toISOString(),
        auto_tts: autoTtsRef.current,
      }),
    })
    const data = await r.json()
    const responseText = String(data.response_text || data.response || '')
    const spokenText = String(data.spoken_text || responseText)
    setBenchDebug(data.debug || data)
    addLog('[AURA BENCH] response received')
    addLog(`[AURA DEBUG] provider_route=${data.debug?.provider_route || data.providers?.brain || 'N/A'} failure_class=${data.debug?.failure_class || 'null'} failure_origin=${data.debug?.failure_origin || 'null'} tool=${data.debug?.tool_name || 'none'} actions_returned=${Array.isArray(data.actions) ? data.actions.length : 0} actions_executed=none`)
    updateBenchRow(rowId, {
      action: 'response_received',
      llmProvider: data.providers?.brain || 'N/A',
      llmMs: fmt(data.timings?.llm_ms),
      ttsProvider: data.providers?.tts || 'N/A',
      ttsMs: fmt(data.timings?.tts_ms),
      playback: 'response_received',
      totalMs: fmt(performance.now() - benchStart),
      error: data.error || '',
      responsePreview: preview(responseText),
      spokenPreview: preview(spokenText),
      responseText,
      spokenText,
      responseTextLength: responseText.length,
      spokenTextLength: spokenText.length,
      memoryUsed: data.memory?.memory_used ? 'yes' : 'no',
      historyTurns: String(data.memory?.history_turns ?? conversationTurnsRef.current.length),
      addressUsed: data.address?.address_used || effectiveAddress().address || '',
      addressReason: data.address?.address_reason || effectiveAddress().reason,
      debug: data.debug || null,
    })
    appendConversationTurn({
      id: newId(),
      timestamp: new Date().toISOString(),
      user_text: rawText,
      normalized_text: normalizedText,
      aura_response: responseText,
      spoken_text: spokenText,
      provider: data.providers?.brain || 'N/A',
      model: data.providers?.brain_model,
      identity_summary: summarizeIdentity(identityContextRef.current),
      vision_summary: summarizeVision(visionContextForRequest),
    })
    await playBenchResponseAudio(data, rowId, benchStart)
  }

  const scheduleBenchRestart = () => {
    if (restartTimerRef.current) window.clearTimeout(restartTimerRef.current)
    restartTimerRef.current = window.setTimeout(() => {
      if (!benchActiveRef.current || benchSpeakingRef.current) return scheduleBenchRestart()
      try { recognitionRef.current?.start() } catch { /* already starting */ }
    }, 500)
  }

  const startContinuousMic = async () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognition) {
      addLog('continuous mic failed: browser does not support SpeechRecognition')
      return
    }
    const unlocked = await unlockAuraAudio(traceAudio)
    setAudioOutput(unlocked)
    setAutoTts(unlocked.unlocked)
    autoTtsRef.current = unlocked.unlocked
    const rec = new SpeechRecognition()
    rec.lang = browserLang || 'en-IN'
    rec.interimResults = false
    rec.continuous = false
    rec.maxAlternatives = 3
    rec.onstart = () => {
      setVoiceStatus('continuous listening')
      addLog('continuous mic listening')
    }
    rec.onerror = (event: any) => {
      addLog(`continuous mic error: ${event.error || 'unknown'}`)
    }
    rec.onresult = (event: any) => {
      const result = event.results?.[0]
      const alternatives = Array.from({ length: Math.min(3, result?.length || 0) }, (_, i) => ({
        transcript: String(result?.[i]?.transcript || '').trim(),
        confidence: typeof result?.[i]?.confidence === 'number' ? result[i].confidence : undefined,
      })).filter((alt) => alt.transcript)
      const best = chooseBestAuraTranscript(alternatives)
      const confidenceValue = alternatives[0]?.confidence
      if (best.raw) handleBenchTranscript(best.raw, confidenceValue).catch((e) => addLog(`voice bench failed: ${e.message}`))
    }
    rec.onend = () => {
      if (benchActiveRef.current) scheduleBenchRestart()
      else setVoiceStatus('idle')
    }
    recognitionRef.current = rec
    benchActiveRef.current = true
    setBenchActive(true)
    try { rec.start() } catch { scheduleBenchRestart() }
  }

  const stopContinuousMic = () => {
    benchActiveRef.current = false
    setBenchActive(false)
    if (restartTimerRef.current) window.clearTimeout(restartTimerRef.current)
    restartTimerRef.current = null
    try { recognitionRef.current?.stop() } catch { /* noop */ }
    recognitionRef.current = null
    setVoiceStatus('idle')
  }

  useEffect(() => () => stopContinuousMic(), [])

  const exportBenchMemory = async () => {
    const r = await fetch('/api/diagnostics/voice-bench-memory')
    const data = await r.json()
    setBenchMemory(data)
    addLog(`voice bench memory entries: ${data.entries ?? 0}`)
  }

  const clearBenchMemory = async () => {
    const r = await fetch('/api/diagnostics/voice-bench-memory/clear', { method: 'POST' })
    const data = await r.json()
    setBenchMemory(data)
    setBenchEntries([])
    addLog('voice bench memory cleared')
  }

  const runWebSocket = async () => {
    addLog('WebSocket diagnostic started')
    const start = performance.now()
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`
    let firstEvent: number | null = null
    const result = await new Promise<JsonMap>((resolve) => {
      const ws = new WebSocket(url)
      const timer = window.setTimeout(() => {
        try { ws.close() } catch { /* noop */ }
        resolve({ ok: false, status: 'timeout', error: 'No event received', ws_connect_ms: null, first_event_ms: null })
      }, 5000)
      ws.onopen = () => {
        const connectMs = performance.now() - start
        ws.send(JSON.stringify({ type: 'ping' }))
        setWsResult({ ok: true, status: 'connected', ws_connect_ms: Math.round(connectMs), first_event_ms: null })
      }
      ws.onmessage = () => {
        if (firstEvent === null) firstEvent = performance.now()
        window.clearTimeout(timer)
        const connectMs = performance.now() - start
        const firstEventMs = firstEvent - start
        ws.close()
        resolve({ ok: true, status: 'event_received', ws_connect_ms: Math.round(connectMs), first_event_ms: Math.round(firstEventMs), error: null })
      }
      ws.onerror = () => {
        window.clearTimeout(timer)
        resolve({ ok: false, status: 'error', error: 'WebSocket connection failed', ws_connect_ms: null, first_event_ms: null })
      }
    })
    setWsResult(result)
    addLog(`WebSocket diagnostic ${result.ok ? 'passed' : `failed: ${result.error}`}`)
  }

  const startMicDiagnostic = async () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    const started = performance.now()
    setVoiceTiming({ ui_click_start_ms: started })
    setVoiceStatus('requesting microphone')
    setTranscript('')
    setRawTranscript('')
    setNormalization(null)
    setConfidence(null)
    addLog('mic diagnostic started')
    if (!SpeechRecognition) {
      setVoiceStatus('browser does not support SpeechRecognition')
      addLog('mic diagnostic failed: browser does not support SpeechRecognition')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      stream.getTracks().forEach((track) => track.stop())
      const permission = performance.now()
      setVoiceTiming((t) => ({ ...t, mic_permission_granted_ms: permission, listening_started_ms: permission }))
      const rec = new SpeechRecognition()
      rec.lang = browserLang
      rec.interimResults = false
      rec.continuous = false
      rec.maxAlternatives = 3
      rec.onstart = () => {
        setVoiceStatus('listening')
        addLog('browser STT listening')
      }
      rec.onerror = (event: any) => {
        setVoiceStatus(event.error || 'speech recognition error')
        addLog(`browser STT failed: ${event.error || 'unknown'}`)
      }
      rec.onresult = (event: any) => {
        const result = event.results?.[0]
        const alternatives = Array.from({ length: Math.min(3, result?.length || 0) }, (_, i) => ({
          transcript: String(result?.[i]?.transcript || '').trim(),
          confidence: typeof result?.[i]?.confidence === 'number' ? result[i].confidence : undefined,
        })).filter((alt) => alt.transcript)
        const best = chooseBestAuraTranscript(alternatives)
        const first = alternatives[0]
        const rawText = best.raw || first?.transcript || ''
        const text = best.normalized
        const finalAt = performance.now()
        setRawTranscript(rawText)
        setTranscript(text)
        setNormalization(best)
        setConfidence(typeof first?.confidence === 'number' ? first.confidence : null)
        setVoiceTiming((t) => ({ ...t, final_transcript_ms: finalAt }))
        setVoiceStatus(text ? 'transcript captured' : 'no speech detected')
        addLog(text ? `browser STT: ${rawText} -> ${text}` : 'browser STT returned no transcript')
      }
      rec.start()
    } catch (e: any) {
      setVoiceStatus(e?.name || 'audio-capture')
      addLog(`mic diagnostic failed: ${e?.name || e?.message || 'audio-capture'}`)
    }
  }

  const runVoiceLoop = async () => {
    const text = transcript || 'hello aura diagnostic test'
    const requestStart = performance.now()
    setVoiceTiming((t) => ({ ...t, backend_request_start_ms: requestStart }))
    addLog('full voice loop diagnostic started')
    const r = await fetch('/api/diagnostics/voice-loop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: text, rawTranscript: rawTranscript || text, source: 'browser_stt', startedAt: new Date().toISOString() }),
    })
    const data = await r.json()
    const responseAt = performance.now()
    setVoiceLoop(data)
    setVoiceTiming((t) => ({ ...t, backend_response_received_ms: responseAt }))
    addLog(`full voice loop diagnostic ${data.ok ? 'passed' : `failed: ${data.error}`}`)
  }

  const playAudio = async () => {
    const data = elevenlabs?.audio_base64 ? elevenlabs : await runElevenlabs(true)
    const b64 = data?.audio_base64
    const startAt = performance.now()
    setVoiceTiming((t) => ({ ...t, tts_playback_start_ms: startAt }))
    if (b64) {
      const audio = new Audio(`data:audio/mpeg;base64,${b64}`)
      audio.onended = () => setVoiceTiming((t) => ({ ...t, tts_playback_end_ms: performance.now() }))
      await audio.play()
      addLog('ElevenLabs diagnostic audio playback started')
    } else if ('speechSynthesis' in window) {
      const utterance = new SpeechSynthesisUtterance('A.U.R.A browser speech diagnostic online.')
      utterance.onend = () => setVoiceTiming((t) => ({ ...t, tts_playback_end_ms: performance.now() }))
      window.speechSynthesis.speak(utterance)
      addLog('browser speech fallback playback started')
    }
  }

  const testResponseSpeechPipeline = async () => {
    const text = 'A.U.R.A response-to-speech pipeline is online.'
    traceAudio('tts.request.started')
    const result = await fetchCloudTtsSpeech(text)
    if (result.ok && result.audioBlob) {
      traceAudio('tts.provider.cloud_audio', { provider: result.provider, audio_bytes: result.audioBytes || 0 })
      await playAuraAudioBlob(result.audioBlob, result.mimeType || 'audio/mpeg', traceAudio, result.provider)
    } else {
      traceAudio('tts.fallback.used', { error: result.error || result.message || 'no_audio' })
      try {
        await speakWithBrowserSpeech(text, traceAudio)
      } catch (e: any) {
        addLog(`browser speech fallback failed: ${e?.message || 'speech failed'}`)
      }
    }
    setAudioOutput({ ...getAuraAudioState(), lastText: text, lastLatencyMs: result.latencyMs, lastAudioBytes: result.audioBytes, lastMimeType: result.mimeType, lastError: getAuraAudioState().lastError })
  }

  const exportReport = async () => {
    const payload = { providers, api_tests: { openrouter, elevenlabs_network: elevenlabsNetwork, elevenlabs, websocket: wsResult }, voice_test: { rawTranscript, transcript, normalization, browserLang, confidence, voiceTiming, voiceLoop }, audio_output: audioOutput }
    const r = await fetch('/api/diagnostics/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await r.json()
    addLog(data.ok ? `report exported: ${data.path}` : `report export failed: ${data.error}`)
  }

  const timingRows = useMemo(() => [
    ['STT time', elapsed(voiceTiming, 'listening_started_ms', 'final_transcript_ms')],
    ['Backend roundtrip', elapsed(voiceTiming, 'backend_request_start_ms', 'backend_response_received_ms')],
    ['TTS start delay', elapsed(voiceTiming, 'backend_response_received_ms', 'tts_playback_start_ms')],
    ['Total voice response', elapsed(voiceTiming, 'ui_click_start_ms', 'tts_playback_start_ms')],
  ], [voiceTiming])

  return (
    <main className="min-h-screen bg-black px-5 py-5 text-[var(--text)]">
      <div className="mx-auto max-w-6xl space-y-4">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--line)] pb-4">
          <div>
            <h1 className="font-display text-[18px] uppercase tracking-[0.22em] text-[var(--blue)]">A.U.R.A Voice Diagnostics</h1>
            <p className="mt-1 font-mono text-[11px] text-[var(--text-dim)]">localhost-only provider, voice, WebSocket, and latency checks</p>
          </div>
          <a className="rounded-sm border border-[var(--line-bright)] px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]" href="/">Orb</a>
        </header>

        <section className="grid gap-3 md:grid-cols-3">
          <Panel title="Provider Status" action={<button onClick={loadProviders} className="icon-btn"><RefreshCw size={13} /></button>}>
            <StatusLine label="OpenRouter" value={providers?.openrouter?.configured ? 'configured' : 'missing'} />
            <StatusLine label="OpenRouter model" value={providers?.openrouter?.model || 'unknown'} />
            <StatusLine label="Groq" value={providers?.groq?.configured ? 'configured' : 'missing'} />
            <StatusLine label="Groq model" value={providers?.groq?.model || 'unknown'} />
            <StatusLine label="Fish Audio" value={providers?.fish_audio?.configured ? 'configured' : 'missing'} />
            <StatusLine label="Cartesia Sonic" value={providers?.cartesia?.configured ? 'configured' : 'missing'} />
            <StatusLine label="ElevenLabs" value={providers?.elevenlabs?.configured ? 'configured' : 'missing'} />
            <StatusLine label="ElevenLabs Voice ID" value={providers?.elevenlabs?.voice_id_present ? 'present' : 'missing'} />
            <StatusLine label="Primary TTS Provider" value={providers?.tts?.primary_provider || providers?.tts?.provider || 'unknown'} />
            <StatusLine label="Provider Priority" value={Array.isArray(providers?.tts?.provider_priority) ? providers.tts.provider_priority.join(', ') : 'unknown'} />
            <StatusLine label="STT" value={providers?.stt?.provider || 'unknown'} />
            <StatusLine label="TTS" value={providers?.tts?.provider || 'unknown'} />
            <StatusLine label="Server" value={providers?.server?.safe_localhost_only ? '127.0.0.1:8765' : 'check'} />
            <SafeJson data={providers} />
          </Panel>

          <Panel title="API Tests">
            <div className="grid grid-cols-2 gap-2">
              <ActionButton icon={<Activity size={13} />} label="Test OpenRouter" onClick={runOpenrouter} />
              <ActionButton icon={<Activity size={13} />} label="Test OpenRouter Brain" onClick={runOpenrouter} />
              <ActionButton icon={<Activity size={13} />} label="Test Groq Fallback" onClick={runGroq} />
              <ActionButton icon={<Activity size={13} />} label="Test Provider Chain" onClick={runProviderChain} />
              <ActionButton icon={<Volume2 size={13} />} label="Test TTS Chain" onClick={runTtsChain} />
              <ActionButton icon={<Radio size={13} />} label="Test ElevenLabs Network" onClick={runElevenlabsNetwork} />
              <ActionButton icon={<Volume2 size={13} />} label="Test ElevenLabs" onClick={() => runElevenlabs(false)} />
              <ActionButton icon={<Radio size={13} />} label="Test WebSocket" onClick={runWebSocket} />
              <ActionButton icon={<Play size={13} />} label="Full Voice Loop" onClick={runVoiceLoop} />
            </div>
            <SafeJson data={{ openrouter, groq, provider_chain: providerChain, tts_chain: ttsChain, elevenlabs_network: elevenlabsNetwork, elevenlabs, websocket: wsResult }} />
          </Panel>

          <Panel title="Voice Input">
            <ActionButton icon={<Mic size={13} />} label="Start Mic Diagnostic" onClick={startMicDiagnostic} />
            <div className="flex gap-2 font-mono text-[10.5px] text-[var(--text-dim)]">
              {['en-IN', 'en-US', 'en-GB'].map((lang) => (
                <button
                  key={lang}
                  type="button"
                  onClick={() => setBrowserLang(lang)}
                  className={`rounded-sm border px-2 py-1 ${browserLang === lang ? 'border-[var(--blue)] text-[var(--blue)]' : 'border-[var(--line-bright)]'}`}
                >
                  {lang}
                </button>
              ))}
            </div>
            <StatusLine label="Status" value={voiceStatus} />
            <StatusLine label="Browser language" value={browserLang} />
            <StatusLine label="Raw transcript" value={rawTranscript || 'N/A'} />
            <StatusLine label="Normalized transcript" value={transcript || 'N/A'} />
            <StatusLine label="Wake detected" value={normalization?.wakeDetected ? 'yes' : 'no'} />
            <StatusLine label="Alias matched" value={normalization?.wakeAliasMatched || 'N/A'} />
            <StatusLine label="Confidence hint" value={normalization?.confidenceHint || 'N/A'} />
            <StatusLine label="Confidence" value={confidence === null ? 'N/A' : confidence.toFixed(2)} />
            <div className="mt-2">
              <ActionButton icon={<Volume2 size={13} />} label="Play TTS Test" onClick={playAudio} />
            </div>
          </Panel>
        </section>

        <section>
          <Panel title="Persistent Memory">
            <div className="grid gap-2 md:grid-cols-4">
              <ActionButton icon={<RefreshCw size={13} />} label="Refresh Memory Status" onClick={loadPersistentMemoryStatus} />
              <ActionButton icon={<Activity size={13} />} label="Test Memory Write" onClick={runPersistentMemoryWrite} />
              <ActionButton icon={<Activity size={13} />} label="Test Memory Read" onClick={runPersistentMemoryRead} />
              <ActionButton icon={<RefreshCw size={13} />} label="Preview Memory Context" onClick={previewPersistentMemoryContext} />
              <ActionButton icon={<RefreshCw size={13} />} label="Clear Local Fallback Memory" onClick={clearPersistentLocalFallback} />
            </div>
            <StatusLine label="Backend" value={persistentMemoryStatus?.backend || 'unknown'} />
            <StatusLine label="Enabled" value={persistentMemoryStatus?.enabled ? 'yes' : 'no'} />
            <StatusLine label="Supabase configured" value={persistentMemoryStatus?.supabase_configured ? 'yes' : 'no'} />
            <StatusLine label="Read enabled" value={persistentMemoryStatus?.read_enabled ? 'yes' : 'no'} />
            <StatusLine label="Write enabled" value={persistentMemoryStatus?.write_enabled ? 'yes' : 'no'} />
            <StatusLine label="Local fallback" value={persistentMemoryStatus?.local_fallback_enabled ? 'yes' : 'no'} />
            <StatusLine label="Latest write" value={persistentMemoryStatus?.last_write_ok === undefined || persistentMemoryStatus?.last_write_ok === null ? 'not tested' : persistentMemoryStatus.last_write_ok ? 'ok' : 'failed'} />
            <StatusLine label="Active facts" value={String(persistentMemoryStatus?.active_fact_count ?? (Array.isArray(persistentMemoryFacts?.facts) ? persistentMemoryFacts.facts.length : 'N/A'))} />
            <StatusLine label="Recent conversations" value={String(persistentMemoryStatus?.recent_conversation_count ?? 'N/A')} />
            <StatusLine label="Last error" value={persistentMemoryStatus?.last_error || 'none'} />
            <SafeJson data={{
              status: persistentMemoryStatus,
              facts: persistentMemoryFacts ? { count: Array.isArray(persistentMemoryFacts.facts) ? persistentMemoryFacts.facts.length : 0, facts: persistentMemoryFacts.facts || [] } : null,
              context_preview: persistentMemoryPreview ? {
                enabled: persistentMemoryPreview.enabled,
                backend: persistentMemoryPreview.backend,
                context_text: persistentMemoryPreview.context_text,
                recent_message_count: persistentMemoryPreview.recent_message_count,
              } : null,
            }} />
          </Panel>
        </section>

        <section>
          <Panel title="Identity Address Resolution">
            <div className="grid gap-2 md:grid-cols-2">
              <ActionButton icon={<Activity size={13} />} label="Test Ma'am Profile" onClick={() => testIdentityAddressResolution(true)} />
              <ActionButton icon={<Activity size={13} />} label="Test Unknown Guest" onClick={() => testIdentityAddressResolution(false)} />
            </div>
            <StatusLine label="Matched profile" value={identityAddressProof?.response?.matched_profile_name || 'none'} />
            <StatusLine label="Preferred address" value={identityAddressProof?.request?.matched_profile?.preferred_address || 'none'} />
            <StatusLine label="Effective address" value={identityAddressProof?.response?.effective_address || 'N/A'} />
            <StatusLine label="Address source" value={identityAddressProof?.response?.address_source || 'N/A'} />
            <StatusLine label="Owner verified" value={String(Boolean(identityAddressProof?.request?.owner_verified))} />
            <StatusLine label="People count" value={String(identityAddressProof?.request?.people_count ?? 'N/A')} />
            <StatusLine label="Visual gender inference" value={String(Boolean(identityAddressProof?.response?.used_visual_gender_inference))} />
            <SafeJson data={identityAddressProof} />
          </Panel>
        </section>

        <section>
          <Panel title="Continuous Voice Bench">
            <div className="grid gap-2 md:grid-cols-3">
              <ActionButton icon={<Mic size={13} />} label="Start Continuous Mic" onClick={startContinuousMic} />
              <ActionButton icon={<Radio size={13} />} label="Stop Continuous Mic" onClick={stopContinuousMic} />
              <ActionButton icon={<RefreshCw size={13} />} label="Clear Bench Logs" onClick={() => { setBenchEntries([]); setBenchDebug(null) }} />
              <ActionButton icon={<Activity size={13} />} label="Wake A.U.R.A" onClick={() => wakeAura('manual_button')} />
              <ActionButton icon={<Radio size={13} />} label="Set Standby" onClick={() => sleepAura('manual_button')} />
              <ToggleButton label="Wake Required" enabled={wakeRequired} onClick={() => setWakeRequired((v) => !v)} />
              <ToggleButton label="Auto Response" enabled={autoResponse} onClick={() => setAutoResponse((v) => !v)} />
              <ToggleButton label="Auto TTS Playback" enabled={autoTts} onClick={() => setAutoTts((v) => !v)} />
              <ToggleButton label="Clear Memory On Standby" enabled={clearConversationOnStandby} onClick={() => setClearConversationOnStandby((v) => !v)} />
              <ActionButton icon={<Download size={13} />} label="Export Voice Bench Memory" onClick={exportBenchMemory} />
              <ActionButton icon={<RefreshCw size={13} />} label="Clear Voice Bench Memory" onClick={clearBenchMemory} />
              <ActionButton icon={<RefreshCw size={13} />} label="Clear Conversation Memory" onClick={clearConversationMemory} />
            </div>
            <div className={`inline-flex rounded-sm border px-2 py-1 font-mono text-[10px] uppercase tracking-[0.14em] ${auraAwake ? 'border-[var(--blue)] text-[var(--blue)]' : 'border-[var(--line-bright)] text-[var(--text-dim)]'}`}>
              {auraAwake ? 'A.U.R.A AWAKE' : 'A.U.R.A STANDBY'}
            </div>
            <StatusLine label="Cloud TTS" value={cloudTtsReady ? `Ready (${selectedTtsClient || 'cloud_tts'})` : 'Not Ready / Fallback'} />
            <StatusLine label="Audio Unlocked" value={audioUnlocked ? 'Yes' : 'No'} />
            {!audioUnlocked && (
              <div className="mt-1 mb-2">
                <ActionButton icon={<Volume2 size={13} />} label="Click to activate A.U.R.A audio" onClick={async () => { await ensureAudioUnlocked(); await runTtsPreflight(); }} />
              </div>
            )}
            <StatusLine label="Continuous mic" value={benchActive ? 'on' : 'off'} />
            <StatusLine label="A.U.R.A State" value={auraAwake ? 'Awake' : 'Standby'} />
            <StatusLine label="Wake mode" value={!wakeRequired ? 'Wake not required - all speech accepted' : auraAwake ? 'Awake - general talk accepted' : 'Waiting for wake phrase'} />
            <StatusLine label="Awake since" value={awakeSince ? new Date(awakeSince).toLocaleTimeString('en-GB') : 'N/A'} />
            <StatusLine label="Wake required" value={wakeRequired ? 'on' : 'off'} />
            <StatusLine label="Auto response" value={autoResponse ? 'on' : 'off'} />
            <StatusLine label="Auto TTS" value={autoTts ? 'on' : 'off'} />
            <StatusLine label="Session address override" value={sessionAddressOverride || 'none'} />
            <ActionButton icon={<RefreshCw size={13} />} label="Clear Address Override" onClick={() => setSessionAddressOverride('')} />
            <StatusLine label="Bench memory" value={benchMemory ? `${benchMemory.entries ?? 0} entries` : 'not loaded'} />
            <StatusLine label="Conversation Memory" value={`${conversationId.slice(0, 8)} - ${conversationTurns.length} / 12 turns`} />
            <StatusLine label="Last memory user" value={conversationTurns.at(-1)?.normalized_text || 'N/A'} />
            <StatusLine label="Last memory response" value={conversationTurns.at(-1)?.aura_response || 'N/A'} />
            <div className="overflow-auto rounded-sm border border-[var(--line)]">
              <table className="min-w-[2400px] w-full border-collapse font-mono text-[10px]">
                <thead className="bg-black/50 text-[var(--blue)]">
                  <tr>
                    {['Time', 'First turn', 'Audio unlocked', 'Cloud TTS ready', 'Raw transcript', 'Normalized transcript', 'Wake', 'State', 'Alias', 'Confidence', 'Action', 'Response preview', 'Spoken preview', 'Response len', 'Spoken len', 'Memory used', 'History turns', 'Address used', 'Address reason', 'LLM provider', 'LLM ms', 'TTS provider', 'TTS client', 'b64 present', 'audio_bytes', 'tts_fallback_reason', 'TTS ms', 'Playback status', 'Total ms', 'Error'].map((h) => (
                      <th key={h} className="border-b border-[var(--line)] px-2 py-1 text-left font-normal uppercase tracking-[0.12em]">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {benchEntries.length === 0 && (
                    <tr><td className="px-2 py-3 text-[var(--text-faint)]" colSpan={30}>no voice bench entries yet</td></tr>
                  )}
                  {benchEntries.map((row) => (
                    <tr key={row.id} className="border-b border-[var(--line)]/40 text-[var(--text-dim)]">
                      <td className="px-2 py-1">{row.time}</td>
                      <td className="px-2 py-1">{row.firstTurn ? 'true' : 'false'}</td>
                      <td className="px-2 py-1">{row.audioUnlocked ? 'true' : 'false'}</td>
                      <td className="px-2 py-1">{row.cloudTtsReady ? 'true' : 'false'}</td>
                      <td className="px-2 py-1">{row.raw}</td>
                      <td className="px-2 py-1">{row.normalized}</td>
                      <td className="px-2 py-1">{row.wake ? 'yes' : 'no'}</td>
                      <td className="px-2 py-1">{row.auraState}</td>
                      <td className="px-2 py-1">{row.alias}</td>
                      <td className="px-2 py-1">{row.confidence}</td>
                      <td className="px-2 py-1 text-[var(--text)]">{row.action}</td>
                      <td className="max-w-[220px] px-2 py-1 text-[var(--text)]">{row.responsePreview || 'N/A'}</td>
                      <td className="max-w-[220px] px-2 py-1 text-[var(--text)]">{row.spokenPreview || 'N/A'}</td>
                      <td className="px-2 py-1">{row.responseTextLength || 'N/A'}</td>
                      <td className="px-2 py-1">{row.spokenTextLength || 'N/A'}</td>
                      <td className="px-2 py-1">{row.memoryUsed}</td>
                      <td className="px-2 py-1">{row.historyTurns}</td>
                      <td className="px-2 py-1">{row.addressUsed || 'N/A'}</td>
                      <td className="px-2 py-1">{row.addressReason || 'N/A'}</td>
                      <td className="px-2 py-1">{row.llmProvider}</td>
                      <td className="px-2 py-1">{row.llmMs}</td>
                      <td className="px-2 py-1">{row.ttsProvider}</td>
                      <td className="px-2 py-1">{row.ttsClient}</td>
                      <td className="px-2 py-1">{row.audioBase64Present ? 'true' : 'false'}</td>
                      <td className="px-2 py-1">{row.audioBytes}</td>
                      <td className="px-2 py-1 text-[var(--amber)]">{row.ttsFallbackReason || 'none'}</td>
                      <td className="px-2 py-1">{row.ttsMs}</td>
                      <td className="px-2 py-1">{row.playback}</td>
                      <td className="px-2 py-1">{row.totalMs}</td>
                      <td className="px-2 py-1 text-[var(--red)]">{row.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-3">
              <StatusLine label="Last command text" value={benchDebug?.command_text || 'N/A'} />
              <StatusLine label="Intent detected" value={benchDebug?.intent_detected || 'N/A'} />
              <StatusLine label="Vision match reason" value={benchDebug?.vision_match_reason || 'none'} />
              <StatusLine label="Sensitive vision attribute" value={benchDebug?.sensitive_vision_attribute || 'none'} />
              <StatusLine label="Visual gender inference" value={benchDebug?.visual_gender_inference ? 'true' : 'false'} />
              <StatusLine label="Provider route" value={benchDebug?.provider_route || 'N/A'} />
              <StatusLine label="Failure class" value={benchDebug?.failure_class || 'none'} />
              <StatusLine label="Failure origin" value={benchDebug?.failure_origin || 'none'} />
              <StatusLine label="Tool name" value={benchDebug?.tool_name || 'none'} />
              <StatusLine label="Actions returned" value={String(benchDebug?.actions_returned ?? 'N/A')} />
              <StatusLine label="Actions executed" value="see Main Orb runtime log" />
              <StatusLine label="Vision context present" value={benchDebug?.vision_context_present ? 'yes' : 'no'} />
              <StatusLine label="Vision people count" value={String(benchDebug?.vision_people_count ?? 'N/A')} />
              <StatusLine label="OpenRouter status" value={benchDebug?.openrouter_last_status || 'N/A'} />
              <StatusLine label="Groq status" value={benchDebug?.groq_last_status || 'N/A'} />
              <StatusLine label="Provider available now" value={benchDebug?.provider_available_now ? 'yes' : 'no'} />
              <StatusLine label="TTS fallback reason" value={benchDebug?.tts_fallback_reason || 'none'} />
              <SafeJson data={benchDebug} />
            </div>
            <SafeJson data={benchMemory} />
          </Panel>
        </section>

        <section>
          <ExpoVisionIdentityMode autoTts={autoTts} auraAwake={auraAwake} onIdentityContextChange={setIdentityContext} onVisionContextChange={setVisionContext} />
        </section>

        <section className="grid gap-3 md:grid-cols-2">
          <Panel title="Audio Output Diagnostics">
            <div className="grid gap-2 md:grid-cols-2">
              <ActionButton icon={<Volume2 size={13} />} label="Unlock Browser Audio" onClick={unlockAudio} />
              <ActionButton icon={<Activity size={13} />} label="Test Browser Beep" onClick={testBeep} />
              <ActionButton icon={<Radio size={13} />} label="Test Browser SpeechSynthesis" onClick={testBrowserSpeech} />
              <ActionButton icon={<RefreshCw size={13} />} label="Test Cloud TTS Fetch" onClick={testCloudTtsFetch} />
              <ActionButton icon={<Play size={13} />} label="Play Cloud TTS Audio" onClick={playFetchedCloudTts} />
              <ActionButton icon={<Volume2 size={13} />} label="Test Response-To-Speech Pipeline" onClick={testResponseSpeechPipeline} />
            </div>
            <StatusLine label="Unlocked" value={audioOutput.unlocked ? 'yes' : 'no'} />
            <StatusLine label="Provider" value={audioOutput.provider} />
            <StatusLine label="Root connectivity" value={elevenlabsNetwork?.root?.classification || 'not tested'} />
            <StatusLine label="Root HTTP status" value={elevenlabsNetwork?.root?.http_status ? String(elevenlabsNetwork.root.http_status) : 'N/A'} />
            <StatusLine label="Curl root classification" value={elevenlabsNetwork?.curl_root?.classification || 'not tested'} />
            <StatusLine label="Selected client" value={elevenlabs?.selected_client || 'N/A'} />
            <StatusLine label="TTS HTTP status" value={elevenlabs?.attempts?.find?.((a: JsonMap) => a.ok)?.http_status ? String(elevenlabs.attempts.find((a: JsonMap) => a.ok).http_status) : 'N/A'} />
            <StatusLine label="Error type" value={elevenlabs?.error_type || elevenlabs?.error || 'none'} />
            <StatusLine label="Playing" value={audioOutput.playing ? 'yes' : 'no'} />
            <StatusLine label="Bytes" value={audioOutput.lastAudioBytes === null ? 'N/A' : String(audioOutput.lastAudioBytes)} />
            <StatusLine label="MIME type" value={audioOutput.lastMimeType || 'N/A'} />
            <StatusLine label="Latency" value={fmt(audioOutput.lastLatencyMs)} />
            <StatusLine label="Last stage" value={audioOutput.lastStage} />
            <StatusLine label="Last error" value={audioOutput.lastError || 'none'} />
            {elevenlabs?.selected_client === 'urllib' && (
              <p className="font-mono text-[10.5px] text-[var(--amber)]">Using urllib fallback because aiohttp failed.</p>
            )}
            {elevenlabs?.selected_client === 'curl_exe' && (
              <p className="font-mono text-[10.5px] text-[var(--amber)]">Using curl.exe fallback because Python socket access was blocked.</p>
            )}
            {elevenlabs?.ok === false && (
              <p className="font-mono text-[10.5px] text-[var(--red)]">Cloud TTS unavailable. Browser SpeechSynthesis fallback remains active.</p>
            )}
            <SafeJson data={{ root: elevenlabsNetwork, tts: elevenlabs, attempts: elevenlabs?.attempts || [] }} />
          </Panel>
          <Panel title="Latency Breakdown">
            {timingRows.map(([label, value]) => <StatusLine key={label as string} label={label as string} value={fmt(value)} />)}
            <SafeJson data={{ browser: voiceTiming, backend: voiceLoop?.timings, providers: voiceLoop?.providers }} />
          </Panel>
          <Panel title="Diagnostic Report" action={<button onClick={exportReport} className="icon-btn"><Download size={13} /></button>}>
            <p className="font-mono text-[11px] leading-relaxed text-[var(--text-dim)]">Exports a safe Markdown report under <code>diagnostics/</code>. Secrets and raw audio are not written.</p>
            <div className="mt-3 max-h-64 overflow-auto rounded-sm border border-[var(--line)] bg-black/35 p-3 font-mono text-[10px] text-[var(--text-dim)]">
              {logs.map((line) => <div key={line}>{line}</div>)}
            </div>
          </Panel>
        </section>

        <section>
          <Panel title="Speech Normalizer Test">
            <input
              value={normalizerInput}
              onChange={(e) => setNormalizerInput(e.target.value)}
              className="w-full rounded-sm border border-[var(--line-bright)] bg-black/40 px-3 py-2 font-mono text-[11px] text-[var(--text)] outline-none focus:border-[var(--blue)]"
            />
            <div className="grid gap-2 md:grid-cols-2">
              {['Wakeup', 'wake up', 'wake up what is your status', 'wake up run diagnostic', 'Hey AURA run diagnostic', 'Hey Cosmo'].map((sample) => (
                <button key={sample} type="button" onClick={() => setNormalizerInput(sample)} className="rounded-sm border border-[var(--line-bright)] px-2 py-1 text-left font-mono text-[10px] text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]">
                  {sample}
                </button>
              ))}
            </div>
            <SafeJson data={normalizeAuraTranscript(normalizerInput)} />
          </Panel>
        </section>
      </div>
    </main>
  )
}

function Panel({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-sm border border-[var(--line-bright)] bg-black/45 p-4 backdrop-blur">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-display text-[11px] uppercase tracking-[0.2em] text-[var(--blue)]">{title}</h2>
        {action}
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  )
}

function StatusLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-[var(--line)]/45 py-1 font-mono text-[11px]">
      <span className="shrink-0 text-[var(--text-faint)]">{label}</span>
      <span className="break-all text-right text-[var(--text)]">{value}</span>
    </div>
  )
}

function ActionButton({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center justify-center gap-2 rounded-sm border border-[var(--line-bright)] bg-black/30 px-3 py-2 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--text-dim)] transition hover:border-[var(--blue)] hover:text-[var(--blue)]"
    >
      {icon}
      {label}
    </button>
  )
}

function ToggleButton({ label, enabled, onClick }: { label: string; enabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-sm border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.12em] transition ${enabled ? 'border-[var(--blue)] text-[var(--blue)]' : 'border-[var(--line-bright)] bg-black/30 text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]'}`}
    >
      {label}: {enabled ? 'on' : 'off'}
    </button>
  )
}
