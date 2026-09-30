import { useEffect, useRef, useState } from 'react'
import { sendAuraVoiceTurn, testTts } from '@/lib/auraApi'
import { getEffectiveAddress, type AuraAddress } from '@/lib/identityAddressing'
import { extractCloudAudio, fetchCartesiaTtsChunk, fetchCloudTtsSpeech, playAuraAudioBlob, playAuraAudioFromBase64, speakWithBrowserSpeech, unlockAuraAudio, getAuraAudioState, stopSpeakingNow as stopAudioNow, beginSpeechSession, getSpeechSessionId, isSpeechSessionCurrent, assertSpeechSession, type AudioPlaybackCallbacks } from '@/lib/audioOutput'
import { chooseBestAuraTranscript, normalizeAuraCommandForRouting, normalizeAuraTranscript } from '@/lib/speechNormalize'
import { splitSpeakingText } from '@/lib/speakingText'
import { runGeneralTalkQueue, generalCartesiaReason, splitCartesiaText } from '@/lib/generalTalkPlayback'
import { useAuraConversationMemory } from './useAuraConversationMemory'
import type { IdentityContext, VisionContext } from '@/lib/visionIdentity'

export type LatencyBreakdown = {
  stt_final_ms: number
  command_classified_ms: number
  context_snapshot_ms: number
  backend_ms: number
  tts_ms: number
  playback_ms: number
  total_ms: number
}

type RuntimeOptions = {
  mode: 'diagnostics' | 'main_orb'
  wakeRequired?: boolean
  autoResponse?: boolean
  autoTts?: boolean
  getIdentityContext?: () => IdentityContext | null
  getVisionContext?: () => VisionContext | null
  pauseTracking?: () => void
  resumeTracking?: () => void
  onExecuteAction?: (action: { type: string; label: string; value?: any; url?: string; mode?: string; target?: string; request_id?: string; action_execution_id?: string }) => Promise<{ ok: boolean; spoken?: string; error?: string; post_action?: () => void }>
  onLog?: (line: string) => void
}

type VoiceTurnDispatchOptions = {
  frontendKind?: string
  timeoutMs?: number
  expoShortcut?: boolean
  expoGuestGreetingShortcut?: boolean
  shortcut?: string
  inputSource?: string
}

const SpeechRecognitionCtor = () => (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
const sleepCommands = new Set(['standby', 'go to sleep', 'sleep mode', 'stop conversation', 'stop listening to commands'])

const defaultIdentityContext: IdentityContext = {
  decision: 'guest_or_unknown',
  display_name: 'Guest',
  role: 'guest',
  address: 'Guest',
  preferred_address: null,
  address_source: 'default',
  matched_profile_id: null,
  matched_profile_name: null,
  matched_profile: null,
  identity_confidence: null,
  owner_face_status: 'unknown',
  owner_voice_status: 'unknown',
  owner_face_enrolled: false,
  owner_voice_enrolled: false,
  people_count: 0,
  tracking_mode: 'none',
  tracks: [],
}

const defaultVisionContext: VisionContext = {
  camera_status: 'off',
  tracking_status: 'off',
  people_count: 0,
  current_identity: 'guest',
  address_selected: 'Guest',
  preferred_address: null,
  address_source: 'default',
  matched_profile_id: null,
  matched_profile_name: null,
  identity_confidence: null,
  fusion_decision: null,
  tracking_mode: 'all',
  tracks: [],
}

type AuraCommandKind = 'wake' | 'runtime_control' | 'capability' | 'vision' | 'vision_followup' | 'identity' | 'self_capability' | 'smalltalk' | 'tool_command' | 'expo_intro' | 'expo_guest_greeting' | 'expo_demo' | 'junk_stt' | 'normal_chat'

const WAKE_BACKEND_TIMEOUT_MS = 6000
const WAKE_TTS_TIMEOUT_MS = 15000
const WAKE_TOTAL_TIMEOUT_MS = 18000

export function getActiveSegmentIndexFromAudioProgress(
  segments: string[],
  currentTime: number,
  duration: number,
  currentIndex = 0,
): number {
  if (segments.length <= 1) return 0
  if (!Number.isFinite(currentTime) || !Number.isFinite(duration) || duration <= 0) {
    return Math.max(0, Math.min(segments.length - 1, currentIndex))
  }
  const totalWeight = segments.reduce((sum, segment) => sum + Math.max(1, segment.trim().length), 0)
  const progressWeight = Math.max(0, Math.min(1, currentTime / duration)) * totalWeight
  let running = 0
  for (let index = 0; index < segments.length; index += 1) {
    running += Math.max(1, segments[index].trim().length)
    if (progressWeight <= running) return index
  }
  return segments.length - 1
}

export function parseWakeAndCommand(rawTranscript: string): {
  rawTranscript: string
  normalizedTranscript: string
  wakeDetected: boolean
  wakePhrase: string | null
  commandText: string
  isWakeOnly: boolean
} {
  const raw = (rawTranscript || '').trim()
  const rawLower = raw.toLowerCase().replace(/\s+/g, ' ')
  const base = normalizeAuraCommandForRouting(rawLower)

  const wakeAliases = [
    { pattern: /^(hey\s+aura|hey\s+ra|hey\s+ora|hey\s+aurora|hello\s+aura|ok\s+aura|okay\s+aura)\b/, normalized: 'hey aura' },
    { pattern: /^(wake\s+up|wakeup)\b/, normalized: 'wake up' },
    { pattern: /^(aura|ora|aurora)\b/, normalized: 'hey aura' },
  ]

  for (const alias of wakeAliases) {
    const match = rawLower.match(alias.pattern)
    if (!match) continue
    const wakePhrase = alias.normalized
    const commandText = rawLower.slice(match[0].length).trim()
    const normalizedTranscript = commandText || (wakePhrase === 'wake up' ? 'wake up' : 'hey aura')
    return {
      rawTranscript: raw,
      normalizedTranscript,
      wakeDetected: true,
      wakePhrase,
      commandText,
      isWakeOnly: !commandText,
    }
  }

  const normalizedTranscript = base || rawLower
  return {
    rawTranscript: raw,
    normalizedTranscript,
    wakeDetected: false,
    wakePhrase: null,
    commandText: normalizedTranscript,
    isWakeOnly: false,
  }
}

const SMALLTALK_PATTERNS = [
  'how was your day', 'how are you', 'are you okay', 'are you working', 'are you online',
  'nice', 'good', 'thank you', 'thanks', 'okay good', 'good to know',
]

export function isJunkTranscript(text: string): boolean {
  const cleaned = (text || '').trim().toLowerCase().replace(/\s+/g, ' ')
  if (!cleaned) return true
  const junkPatterns = [
    'voice command voice input', 'voice input', 'command voice command', 'microphone microphone',
    'listening listening', 'test test test', 'mic mic', 'listening', 'voice command', 'voice input command',
  ]
  if (junkPatterns.some((p) => cleaned === p || cleaned.includes(p))) return true
  if (['um', 'uh', 'hmm', 'ah'].includes(cleaned)) return true
  return false
}

export function classifyAuraCommandKind(text: string, recentTurns?: { aura_response?: string; spoken_text?: string }[]): AuraCommandKind {
  const norm = normalizeAuraCommandForRouting(text)
  if (norm.includes('wake up') || norm.includes('wakeup') || norm.includes('hey aura') || norm.includes('hello aura') || norm === 'aura' || norm === 'awake') {
    return 'wake'
  }
  const runtimeControlPatterns = [
    'turn off the camera', 'turn off camera', 'stop the camera', 'stop camera', 'camera off',
    'turn on the camera', 'turn on camera', 'start camera', 'camera on',
    'enable gesture control', 'start gesture tracking', 'start orb gesture tracking',
    'control orb with hand', 'control orb with gestures', 'stop gesture control',
    'turn on live tracking', 'stop live tracking', 'tracking on', 'tracking off',
    'activate live tracking', 'enable live tracking', 'track me', 'follow me',
    'turn on person tracking', 'activate camera tracking', 'light tracking on', 'enable light tracking',
    'enable people tracking', 'start people tracking', 'track people', 'start camera tracking',
    'start identity tracking', 'detect people', 'stop people tracking',
    'stop following me', 'turn off live tracking',
    'mute mic', 'mute microphone', 'mic mute', 'microphone mute', 'turn off mic', 'turn off microphone',
    'turn off the mic', 'turn off the microphone', 'turn mic off', 'turn microphone off',
    'stop mic', 'stop microphone', 'stop the mic', 'stop the microphone',
    'disable mic', 'disable microphone', 'stop listening', 'pause listening',
    'stop hearing', 'unmute mic', 'unmute microphone', 'mic on', 'microphone on', 'turn on mic',
    'turn on microphone', 'turn on the mic', 'turn on the microphone',
    'start mic', 'start microphone', 'start the mic', 'start the microphone', 'enable mic', 'enable microphone',
    'start listening', 'listen again', 'resume listening',
  ]
  if (runtimeControlPatterns.some((p) => norm.includes(p))) {
    return 'runtime_control'
  }
  const expoGuestGreetingPatterns = [
    'guest greeting', 'trustee greeting', 'principal greeting', 'welcome trustee',
    'welcome principal', 'welcome trustee and principal', 'start guest greeting',
    'start principal greeting',
  ]
  if (expoGuestGreetingPatterns.some((p) => norm.includes(p))) {
    return 'expo_guest_greeting'
  }
  const expoIntroPatterns = [
    'start expo intro', 'begin expo intro', 'engineers week intro', 'project expo intro',
    'welcome staff', 'welcome faculty',
  ]
  if (expoIntroPatterns.some((p) => norm.includes(p))) {
    return 'expo_intro'
  }
  const expoDemoPatterns = [
    'hello aura introduce yourself', 'introduce yourself', 'tell me about yourself', 'who are you',
    'what can you do', 'explain your features', 'what are your features', 'what all can you do', 'show your capabilities',
    'tell me about this project', 'explain this project', 'what is this project', 'why was aura built',
    'what makes aura different from a normal chatbot', 'how are you different from chatgpt', 'what makes this different',
    'why is this not just a chatbot', 'how does aura help students', 'what makes you useful for students',
    'how can students use this', 'how is this useful', 'tell us about engineers week', 'tell us about engineers day',
    'what is engineers week', 'what is this expo about', 'explain engineers week expo', 'what projects are shown here',
    'tell me about your department projects', 'explain the projects in this expo', 'what are the projects in your stall',
    'department projects', 'tell me about aura', 'explain aura', 'what is artificial unified response assistant',
    'aura project', 'tell me about edge storm', 'explain edge storm', 'what is edge storm', 'edge storm project',
    'tell me about game centre', 'explain game centre', 'what games are here', 'game centre project',
    'tell me about tech hub', 'explain tech hub', 'what is tech hub', 'tech hub project',
    'how do you use local tools safely', 'explain your safe tools', 'what is your tool safety',
    'can you control the laptop', 'are you storing this conversation', 'what do you remember',
    'what did eswar tell you about the expo', 'can we have a general talk', 'can we have an general talk',
    'can we talk', 'can we chat', 'talk with me', "give a short summary of today's demonstration",
    'summarize this demo', 'conclude the demo', 'end the intro',
  ]
  if (expoDemoPatterns.some((p) => norm.includes(p))) {
    return 'expo_demo'
  }
  const selfCapabilityPatterns = [
    'tell me about yourself', 'about yourself', 'introduce yourself', 'who are you', 'what are you',
    'what can you do', 'mention what you can do', 'explain your capabilities', 'what are your abilities',
    'what can aura do', 'what can a u r a do', 'your features', 'your functions', 'your purpose',
    'tell me your name', 'first your name', 'your name only', 'just your name', 'name nothing else', 'who made you',
  ]
  if (selfCapabilityPatterns.some((p) => norm.includes(p))) {
    return 'self_capability'
  }
  const toolCommandPatterns = [
    'weather', 'rain', 'forecast', 'temperature',
    'news', 'headlines', 'latest updates', 'interesting news', 'current news',
    'today news', 'international news', 'technology news', 'tech news', 'ai news',
    'political news', 'politics news', 'election news', 'tamil nadu news',
    'tamil nadu headlines', 'chennai news', 'coimbatore news', 'india political news',
    'what is happening today', "what's happening today", 'happening today',
    'tavily', 'newsapi', 'news api', 'newsai',
    'battery', 'cpu', 'ram', 'memory usage', 'disk', 'storage', 'network status',
    'open calculator', 'open chrome', 'open vscode', 'open vs code', 'open powershell', 'open notepad',
    'open new tab', 'open chrome tab', 'new chrome tab', 'open browser tab',
    'open new chrome tab in current window', 'open new chrome tab in existing window',
    'open new chrome tab in same window', 'open a tab in the current chrome window',
    'new tab in current chrome window', 'open new browser tab in current window',
    'open new window', 'open chrome window', 'new chrome window', 'open browser window',
    'volume', 'brightness', 'lock laptop', 'shutdown', 'restart', 'close app',
    'find file', 'search downloads', 'latest ppt', 'project report',
    'clipboard', 'screenshot', 'capture screen',
    'open youtube', 'open github', 'search google', 'search web',
    'git status', 'branch', 'npm build', 'python compile', 'open project', 'list project files',
  ]
  if (toolCommandPatterns.some((p) => norm.includes(p))) {
    return 'tool_command'
  }
  const capabilityPatterns = [
    'can you hear', 'are you listening', 'are able to listen', 'can you listen',
    'hear me', 'listen me', 'to listen me', 'mic working', 'microphone working',
    'can you draft an email', 'do you have permission to draft email', 'permission to draft email',
    'can you send email', 'do you have cloud access',
    'tamil puriyuma', 'tamil puriuma', 'tamil theriyuma', 'tamil pesuviya',
    'tamil pesuva', 'unaku tamil theriyuma', 'unakku tamil theriyuma',
    'tamil understand', 'do you understand tamil', 'can you speak tamil',
    'can you understand tamil',
    'tamil la sollu', 'tamil la pesu', 'tamil la badhil sollu',
    'tamil la answer pannu', 'tamil la unna intro kudu', 'tamil la un intro sollu',
    'tamil la introduce yourself', 'tamil la explain pannu', 'tamil la sollunga',
    'uyir eluthukkal ethanai', 'mei eluthukkal ethanai', 'tamil letters ethanai',
    'tamil vowels how many', 'tamil consonants how many',
  ]
  if (capabilityPatterns.some((p) => norm.includes(p))) {
    return 'capability'
  }
  const identityPatterns = [
    'are you able to see your owner', 'is your owner visible', 'is the person your owner',
    'am i your owner', 'do you recognize me', 'who am i', 'owner or not',
  ]
  if (identityPatterns.some((p) => norm.includes(p))) {
    return 'identity'
  }
  const visionPatterns = [
    'are you able to see me', 'can you see me', 'able to see me', 'do you see me',
    'can you see', 'see me', 'detect me', 'who is here', 'what do you see',
    'through the camera', 'through your camera', 'camera feed', 'live feed',
    'how many people', 'how many person', 'how many user', 'person visible',
    'address the person you see on camera', 'address the person in camera',
    'address the person in the camera', 'address whoever is in front of you',
    'greet the person in camera', 'greet the person you see',
    'say hello to the person in camera', 'say hello to the person you see',
    'how should you address the person', 'call the person sir or ma\'am',
    'call the person sir or mam', 'address visible person', 'address current person',
    'address detected person',
    'am i visible', 'can you see my hand', 'do you see my hand', 'hand visible',
    'spectacles', 'glasses', 'am i wearing glasses', 'am i wearing spectacles', 'border',
  ]
  if (visionPatterns.some((p) => norm.includes(p))) {
    return 'vision'
  }

  // Check recent conversation turns for vision context
  const lastTurn = recentTurns && recentTurns.length > 0 ? recentTurns[recentTurns.length - 1] : null
  const lastAssistantText = ((lastTurn?.aura_response || lastTurn?.spoken_text || '') as string).toLowerCase()
  const visionContextKeywords = ['camera feed', 'i can see', 'person', 'people', 'tracking', 'owner', 'visible']
  const isPreviousVisionAnswer = visionContextKeywords.some((kw) => lastAssistantText.includes(kw))

  const correctionPatterns = [
    'but there where', 'but there were', 'there are', 'there were', 'actually there are',
    'actually there were', 'no there are', 'no there were', 'it is 2 people', 'you missed one',
    'there are more people', 'there are two persons', 'not one, two', 'count is 2', 'people count is 2',
    'but', 'actually', 'no', 'wrong', 'missed', 'count', 'people', 'person', 'two', 'three', '2', '3'
  ]

  const hasCorrectionWord = correctionPatterns.some((p) => norm.includes(p))

  if (isPreviousVisionAnswer && hasCorrectionWord) {
    return 'vision_followup'
  }

  // Direct follow-up phrases even if recent turns not checked
  const directFollowupPhrases = [
    'there were 2 people', 'there were 2 peoples', 'there were two people', 'there are two people',
    'actually there are 2', 'no there are 2', 'it is 2 people', 'you missed one', 'there are more people',
    'there are two persons', 'not one, two', 'count is 2', 'people count is 2', 'where 2 peoples'
  ]
  if (directFollowupPhrases.some((p) => norm.includes(p))) {
    return 'vision_followup'
  }

  if (SMALLTALK_PATTERNS.some((p) => norm.includes(p))) {
    return 'smalltalk'
  }

  return 'normal_chat'
}

export function formatFallbackReason(raw: string): string {
  if (!raw) return ''
  return raw
    .replace(/openrouter_/g, 'OpenRouter: ')
    .replace(/groq_/g, ' | Groq: ')
    .replace(/_/g, ' ')
}

function addressCommand(text: string): { address: AuraAddress; response: string } | null {
  const value = text.toLowerCase()
  if (value.includes('use neutral address') || value.includes("don't use sir") || value.includes("dont use sir") || value.includes("don't use mam") || value.includes("dont use mam")) {
    return { address: '', response: 'Understood. I will use neutral address for this session.' }
  }
  if (value.includes('address me as mam') || value.includes('call me mam') || value.includes('refer to me as mam')) {
    return { address: 'Mam', response: 'Understood, Mam. I will address you as Mam for this session.' }
  }
  if (value.includes('address me as sir') || value.includes('call me sir') || value.includes('refer to me as sir') || value.includes("don't call me guest") || value.includes('dont call me guest')) {
    return { address: 'Sir', response: 'Understood, Sir. I will address you as Sir for this session.' }
  }
  return null
}

export type MicStartupState =
  | 'idle'
  | 'requesting_permission'
  | 'unlocking_audio'
  | 'starting_recognition'
  | 'active'
  | 'retrying'
  | 'permission_denied'
  | 'failed'

export type MicPermissionState = 'granted' | 'prompt' | 'denied' | 'unknown'

export function useAuraVoiceRuntime(options: RuntimeOptions) {
  const [auraAwake, setAuraAwake] = useState(false)
  const [listening, setListening] = useState(false)
  const [continuousMicActive, setContinuousMicActive] = useState(false)
  const [pushToTalkActive, setPushToTalkActive] = useState(false)
  const [isAuraSpeaking, setIsAuraSpeaking] = useState(false)
  const [lastRawTranscript, setLastRawTranscript] = useState('')
  const [lastNormalizedTranscript, setLastNormalizedTranscript] = useState('')
  const [lastResponseText, setLastResponseText] = useState('')
  const [lastSpokenText, setLastSpokenText] = useState('')
  const [lastProvider, setLastProvider] = useState('N/A')
  const [lastTtsProvider, setLastTtsProvider] = useState('N/A')
  const [lastPlayback, setLastPlayback] = useState('N/A')
  const [lastAudioBytes, setLastAudioBytes] = useState<number | null>(null)
  const [lastTtsError, setLastTtsError] = useState('')
  const [lastError, setLastError] = useState('')
  const [lastStage, setLastStage] = useState('idle')
  const [lastLatency, setLastLatency] = useState<LatencyBreakdown | null>(null)
  const [lastActionStatus, setLastActionStatus] = useState<string>('')
  const [voiceTurnInFlight, setVoiceTurnInFlight] = useState(false)
  const [audioUnlocked, setAudioUnlocked] = useState(getAuraAudioState().unlocked)
  const [sessionAddressOverride, setSessionAddressOverride] = useState<AuraAddress>('')

  // Speaking text segment sync state
  const [speakingSegments, setSpeakingSegments] = useState<string[]>([])
  const [activeSpeakingSegmentIndex, setActiveSpeakingSegmentIndex] = useState(-1)
  const [isSpeakingTextSynced, setIsSpeakingTextSynced] = useState(false)
  const [speakingTextAutoScrollEnabled, setSpeakingTextAutoScrollEnabled] = useState(true)
  const segmentTimerRef = useRef<number | null>(null)
  const segmentIndexRef = useRef(-1)
  const speakingSegmentsRef = useRef<string[]>([])
  const userScrolledRef = useRef(false)
  const autoScrollResumeTimerRef = useRef<number | null>(null)

  // Mic state machine
  const [micStartupState, setMicStartupState] = useState<MicStartupState>('idle')
  const [micPermissionState, setMicPermissionState] = useState<MicPermissionState>('unknown')
  const [continuousMicRequested, setContinuousMicRequested] = useState(false)
  const [recognitionRunning, setRecognitionRunning] = useState(false)
  const [lastMicStartError, setLastMicStartError] = useState<string | null>(null)
  const [lastMicStartAttemptAt, setLastMicStartAttemptAt] = useState<number | null>(null)
  const [lastRecognitionStartAt, setLastRecognitionStartAt] = useState<number | null>(null)
  const [lastRecognitionEndAt, setLastRecognitionEndAt] = useState<number | null>(null)
  const [lastSuccessfulTranscriptAt, setLastSuccessfulTranscriptAt] = useState<number | null>(null)
  const [retryCount, setRetryCount] = useState(0)
  const [restartPending, setRestartPending] = useState(false)

  const recognitionRef = useRef<any>(null)
  const speakingRef = useRef(false)
  const awakeRef = useRef(false)
  const lastWakeTimeRef = useRef<number>(0)
  const lastFinalTimeRef = useRef<number>(0)
  const lastFinalTextRef = useRef<string>('')
  const processingRef = useRef<boolean>(false)
  const voiceTurnInFlightRef = useRef<boolean>(false)

  const micStartupStateRef = useRef<MicStartupState>('idle')
  const micPermissionStateRef = useRef<MicPermissionState>('unknown')
  const continuousMicRequestedRef = useRef(false)
  const recognitionRunningRef = useRef(false)
  const lastRecognitionStartAtRef = useRef<number | null>(null)
  const retryCountRef = useRef(0)
  const pendingStartPromiseRef = useRef<Promise<boolean> | null>(null)
  const restartTimerRef = useRef<number | null>(null)
  const restartPendingRef = useRef(false)
  const lastRecognitionErrorRef = useRef<string | null>(null)
  const micStoppedByUserRef = useRef(false)
  const intentionalMicStopLoggedRef = useRef(false)

  const memory = useAuraConversationMemory()

  useEffect(() => {
    awakeRef.current = auraAwake
  }, [auraAwake])

  useEffect(() => {
    if (continuousMicActive || pushToTalkActive) {
      testTts('preflight').catch(() => undefined)
    }
  }, [continuousMicActive, pushToTalkActive])

  // Monitor microphone permission status
  useEffect(() => {
    if (navigator.permissions && navigator.permissions.query) {
      navigator.permissions.query({ name: 'microphone' as PermissionName }).then((status) => {
        const pState: MicPermissionState = status.state === 'granted' ? 'granted' : status.state === 'denied' ? 'denied' : 'prompt'
        setMicPermissionState(pState)
        micPermissionStateRef.current = pState
        status.onchange = () => {
          const newP: MicPermissionState = status.state === 'granted' ? 'granted' : status.state === 'denied' ? 'denied' : 'prompt'
          setMicPermissionState(newP)
          micPermissionStateRef.current = newP
        }
      }).catch(() => undefined)
    }
  }, [])

  // Mic Health Watchdog (3 second interval)
  useEffect(() => {
    const watchdogInterval = window.setInterval(() => {
      if (restartPendingRef.current) {
        options.onLog?.('[VOICE] watchdog_skip restart_pending')
        return
      }
      if (micStoppedByUserRef.current) {
        if (!intentionalMicStopLoggedRef.current) {
          options.onLog?.('[VOICE] watchdog_skip intentional_mic_stop')
          intentionalMicStopLoggedRef.current = true
        }
        return
      }
      if (
        continuousMicRequestedRef.current &&
        !speakingRef.current &&
        !processingRef.current &&
        !voiceTurnInFlightRef.current &&
        document.visibilityState === 'visible' &&
        !recognitionRunningRef.current &&
        micPermissionStateRef.current !== 'denied'
      ) {
        const now = Date.now()
        const lastStart = lastRecognitionStartAtRef.current || 0
        if (now - lastStart >= 3000) {
          if (retryCountRef.current < 3) {
            retryCountRef.current += 1
            setRetryCount(retryCountRef.current)
            setMicStartupState('retrying')
            micStartupStateRef.current = 'retrying'
            options.onLog?.('[VOICE] mic_watchdog_restart')
            startContinuousListening().catch(() => undefined)
          } else {
            setMicStartupState('failed')
            micStartupStateRef.current = 'failed'
            setLastMicStartError('max_retries_exceeded')
            options.onLog?.('[VOICE] mic_recovery_required')
          }
        }
      }
    }, 3000)

    return () => {
      window.clearInterval(watchdogInterval)
      if (restartTimerRef.current) window.clearTimeout(restartTimerRef.current)
    }
  }, [])

  const log = (line: string) => options.onLog?.(line)
  const identityContext = () => options.getIdentityContext?.() || defaultIdentityContext
  const visionContext = () => options.getVisionContext?.() || defaultVisionContext
  const effectiveAddress = () => {
    const identity = identityContext()
    const vision = visionContext()
    return getEffectiveAddress({
      ownerVerified: identity?.role === 'owner' || vision?.current_identity === 'owner',
      ownerAddress: identity?.address || 'Sir',
      sessionAddressOverride,
      currentIdentityAddress: identity?.address,
      currentIdentity: identity?.role,
      matchedProfile: (identity as any)?.matched_profile || null,
      preferredAddress: (identity as any)?.preferred_address || (vision as any)?.preferred_address || null,
      liveIdentityAddressAllowed: Boolean((identity as any)?.live_identity_address_allowed || (vision as any)?.live_identity_address_allowed),
      ownerProfileEnrolled: Boolean(identity?.owner_face_enrolled || identity?.owner_voice_enrolled),
    })
  }

  const setActiveSpeakingSegment = (index: number, total: number) => {
    if (index === segmentIndexRef.current) return
    segmentIndexRef.current = index
    setActiveSpeakingSegmentIndex(index)
    log(`[AURA SPEAKING TEXT] segment_active index=${index} total=${total}`)
  }

  const clearSegmentTimer = () => {
    if (segmentTimerRef.current) {
      window.clearInterval(segmentTimerRef.current)
      segmentTimerRef.current = null
    }
  }

  const startEstimatedSegmentTimer = (segments: string[]) => {
    clearSegmentTimer()
    if (segments.length <= 1) return
    const words = segments.join(' ').trim().split(/\s+/).filter(Boolean).length
    const estimatedDurationMs = Math.max(1400, Math.ceil((words / 2.45) * 1000))
    const startedAt = performance.now()
    segmentTimerRef.current = window.setInterval(() => {
      const currentTime = (performance.now() - startedAt) / 1000
      const duration = estimatedDurationMs / 1000
      const nextIndex = getActiveSegmentIndexFromAudioProgress(segments, currentTime, duration, segmentIndexRef.current)
      setActiveSpeakingSegment(nextIndex, segments.length)
    }, 250)
  }

  const prepareSegmentSync = (text: string) => {
    const segments = splitSpeakingText(text)
    if (segments.length === 0) return
    clearSegmentTimer()
    speakingSegmentsRef.current = segments
    setSpeakingSegments(segments)
    setActiveSpeakingSegmentIndex(-1)
    segmentIndexRef.current = -1
    setIsSpeakingTextSynced(false)
    setSpeakingTextAutoScrollEnabled(true)
    userScrolledRef.current = false
    log(`[AURA SPEAKING TEXT] sync_ready segments=${segments.length}`)
  }

  const splitExpoIntroForCartesia = (text: string) => {
    const paragraphs = text.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean)
    if (paragraphs.length <= 1) return splitSpeakingText(text)
    const chunks: string[] = []
    const push = (parts: string[]) => {
      const chunk = parts.map((part) => part.trim()).filter(Boolean).join('\n\n').trim()
      if (chunk) chunks.push(chunk)
    }
    push(paragraphs.slice(0, 3))
    push(paragraphs.slice(3, 6))
    push(paragraphs.slice(6, 15))
    push(paragraphs.slice(15, 17))
    for (const paragraph of paragraphs.slice(17, 21)) push([paragraph])
    push(paragraphs.slice(21))
    return chunks.filter(Boolean)
  }

  const prepareSegmentSyncWithSegments = (segments: string[], mode = 'cartesia_segmented') => {
    clearSegmentTimer()
    speakingSegmentsRef.current = segments
    setSpeakingSegments(segments)
    setActiveSpeakingSegmentIndex(-1)
    segmentIndexRef.current = -1
    setIsSpeakingTextSynced(true)
    setSpeakingTextAutoScrollEnabled(true)
    userScrolledRef.current = false
    log(`[AURA SPEAKING TEXT] sync_started segments=${segments.length} mode=${mode}`)
  }

  const createPlaybackSyncCallbacks = (mode: 'audio_progress' | 'estimated'): AudioPlaybackCallbacks => {
    let started = false
    let metadataSeen = false
    return {
      onPlaybackStart: () => {
        const segments = speakingSegmentsRef.current
        if (!segments.length || started) return
        started = true
        setIsSpeakingTextSynced(true)
        log(`[AURA SPEAKING TEXT] sync_started segments=${segments.length} mode=${mode}`)
        setActiveSpeakingSegment(0, segments.length)
        if (mode === 'estimated') startEstimatedSegmentTimer(segments)
      },
      onPlaybackMetadata: (duration) => {
        if (Number.isFinite(duration) && duration > 0) {
          metadataSeen = true
          clearSegmentTimer()
        }
      },
      onPlaybackTimeUpdate: (currentTime, duration) => {
        const segments = speakingSegmentsRef.current
        if (!segments.length || !metadataSeen) return
        const nextIndex = getActiveSegmentIndexFromAudioProgress(segments, currentTime, duration, segmentIndexRef.current)
        setActiveSpeakingSegment(nextIndex, segments.length)
      },
      onPlaybackEnd: () => {
        clearSegmentTimer()
      },
      onPlaybackError: () => {
        clearSegmentTimer()
      },
    }
  }

  const stopSegmentSync = (reason = 'playback_end') => {
    clearSegmentTimer()
    if (autoScrollResumeTimerRef.current) {
      window.clearTimeout(autoScrollResumeTimerRef.current)
      autoScrollResumeTimerRef.current = null
    }
    setIsSpeakingTextSynced(false)
    setSpeakingTextAutoScrollEnabled(false)
    setActiveSpeakingSegmentIndex(-1)
    segmentIndexRef.current = -1
    speakingSegmentsRef.current = []
    log(`[AURA SPEAKING TEXT] sync_completed reason=${reason}`)
  }

  const handleUserScrollPause = () => {
    userScrolledRef.current = true
    if (autoScrollResumeTimerRef.current) {
      window.clearTimeout(autoScrollResumeTimerRef.current)
    }
    autoScrollResumeTimerRef.current = window.setTimeout(() => {
      userScrolledRef.current = false
      setSpeakingTextAutoScrollEnabled(true)
    }, 2000)
  }

  const cartesiaIntroSafeReason = (reason: string | null) => {
    const value = String(reason || '').toLowerCase()
    if (value.includes('payment') || value.includes('quota') || value.includes('credit')) return 'cartesia_payment_required_or_credits_low'
    if (value.includes('timeout')) return 'cartesia_timeout'
    if (value.includes('audio_missing') || value.includes('empty')) return 'cartesia_empty_audio'
    if (value.includes('voice') || value.includes('model') || value.includes('payload') || value.includes('404') || value.includes('400')) return 'cartesia_voice_or_model_invalid'
    return 'cartesia_network_error'
  }

  const playSegmentedCartesiaIntro = async (text: string, pipeline?: { prefetch?: number; timeout_seconds?: number }) => {
    log('[AURA EXPO] intro_started')
    const result = await playGeneralTalk(splitCartesiaText(splitExpoIntroForCartesia(text).join(' ')), pipeline)
    log(`[AURA EXPO] intro_completed speech_played=${String(result.speechPlayed && !result.ttsBlocked)}`)
    return result
  }

  const sanitizeNewsCartesiaChunk = (text: string) => {
    return (text || '')
      .replace(/[“”]/g, '"')
      .replace(/[‘’]/g, "'")
      .replace(/[|]/g, ', ')
      .replace(/[—–]+/g, ', ')
      .replace(/:{2,}/g, ':')
      .replace(/\s*:\s*/g, ': ')
      .replace(/\s+/g, ' ')
      .trim()
  }

  const splitNewsResponseForCartesia = (text: string) => {
    const normalized = (text || '').replace(/\s+/g, ' ').trim()
    const intro = "Here are today's top headlines, Sir."
    const withoutIntro = normalized.replace(/^Here are today's top headlines, Sir[:.]?\s*/i, '').trim()
    const numbered = withoutIntro
      .split(/\s+(?=\d+\.\s+)/)
      .map((part) => part.replace(/^\d+\.\s*/, '').trim())
      .filter(Boolean)
      .slice(0, 5)
    if (!numbered.length) {
      const fallback = splitSpeakingText(normalized).slice(0, 5)
      return { chunks: [intro, ...fallback], segments: fallback }
    }
    const segments = numbered.map((headline, index) => `${index + 1}. ${headline}`)
    return { chunks: [intro, ...segments], segments }
  }

  const isNewsReadAloudResponse = (data: any, text: string) => {
    const debug = data?.debug || {}
    const news = debug?.news || {}
    return (
      debug?.tool_name === 'news' ||
      news?.result_count > 0 ||
      (debug?.provider_route === 'deterministic_tool_command' && /^Here are today's top headlines, Sir[:.]/i.test(text || ''))
    )
  }

  const fetchCartesiaNewsChunkWithRetry = async (chunk: string, index: number, total: number) => {
    const sessionId = getSpeechSessionId()
    log(`[AURA NEWS TTS] chunk_started index=${index + 1} total=${total}`)
    log(`[TTS] provider_attempt provider=cartesia status=started chunk=${index + 1}/${total}`)
    const first = await fetchCartesiaTtsChunk(chunk, 8)
    assertSpeechSession(sessionId)
    if (first.ok && first.audioBlob) return first
    const firstReason = cartesiaIntroSafeReason(first.error)
    log(`[AURA NEWS TTS] chunk_failed index=${index + 1} reason=${firstReason}`)
    log('[AURA NEWS TTS] retry_sanitized_chunk=true')
    const retried = await fetchCartesiaTtsChunk(sanitizeNewsCartesiaChunk(chunk), 8)
    if (retried.ok && retried.audioBlob) return retried
    const retryReason = cartesiaIntroSafeReason(retried.error || first.error)
    log(`[AURA NEWS TTS] chunk_failed index=${index + 1} reason=${retryReason}`)
    return { ...retried, error: retryReason || retried.error || first.error || 'cartesia_chunk_failed' }
  }

  const playSegmentedCartesiaNews = async (text: string) => {
    const sessionId = getSpeechSessionId()
    const { chunks, segments } = splitNewsResponseForCartesia(text)
    if (!chunks.length || !segments.length) return { playback: 'blocked' as const, speechPlayed: false, ttsBlocked: true, blockReason: 'cartesia_empty_audio' }
    log(`[AURA NEWS TTS] segmented_read_aloud=true chunks=${chunks.length}`)
    setLastTtsError('')
    setLastTtsProvider('cartesia')
    let syncStarted = false
    let totalBytes = 0
    try {
      for (let index = 0; index < chunks.length; index += 1) {
        assertSpeechSession(sessionId)
        const fetched = await fetchCartesiaNewsChunkWithRetry(chunks[index], index, chunks.length)
        assertSpeechSession(sessionId)
        if (!fetched.ok || !fetched.audioBlob) {
          const reason = cartesiaIntroSafeReason(fetched.error)
          setLastTtsError('Cartesia could not read the news aloud.')
          setLastPlayback('cartesia_news_failed')
          setLastAudioBytes(totalBytes || null)
          log(`[AURA NEWS TTS] read_aloud_failed reason=cartesia_chunk_failed safe_reason=${reason}`)
          return { playback: 'blocked' as const, speechPlayed: totalBytes > 0, ttsBlocked: true, blockReason: reason }
        }
        if (!syncStarted) {
          speakingRef.current = true
          setIsAuraSpeaking(true)
          setLastStage('speaking')
          prepareSegmentSyncWithSegments(segments, 'cartesia_news_segmented')
          syncStarted = true
        }
        const activeIndex = Math.max(0, Math.min(segments.length - 1, index - 1))
        setActiveSpeakingSegment(activeIndex, segments.length)
        totalBytes += fetched.audioBytes || fetched.audioBlob.size || 0
        log(`[TTS] provider=cartesia audio_base64=true audio_bytes=${fetched.audioBytes ?? fetched.audioBlob.size ?? 'N/A'} playback=cloud_audio`)
        await playAuraAudioBlob(fetched.audioBlob, fetched.mimeType || 'audio/wav', undefined, 'cartesia')
        log(`[AURA NEWS TTS] chunk_played index=${index + 1} total=${chunks.length}`)
      }
      setLastPlayback('cartesia_news_segmented_played')
      setLastAudioBytes(totalBytes)
      log('[AURA NEWS TTS] read_aloud_completed speech_played=true')
      return { playback: 'cloud_audio' as const, speechPlayed: true, ttsBlocked: false, blockReason: null }
    } catch (e: any) {
      if (!isSpeechSessionCurrent(sessionId)) return { playback: 'blocked' as const, speechPlayed: false, ttsBlocked: false, blockReason: 'cancelled' }
      const reason = cartesiaIntroSafeReason(e?.message)
      setLastTtsError('Cartesia could not read the news aloud.')
      setLastPlayback('cartesia_news_failed')
      setLastAudioBytes(totalBytes || null)
      log(`[AURA NEWS TTS] read_aloud_failed reason=cartesia_chunk_failed safe_reason=${reason}`)
      return { playback: 'blocked' as const, speechPlayed: totalBytes > 0, ttsBlocked: true, blockReason: reason }
    } finally {
      if (isSpeechSessionCurrent(sessionId)) {
      stopSegmentSync(totalBytes > 0 ? 'playback_end' : 'playback_blocked')
      log('[AURA TTS DEBUG] backend_provider=cartesia')
      log(`[AURA TTS DEBUG] audio_base64=${String(totalBytes > 0)}`)
      log(`[AURA TTS DEBUG] audio_bytes=${totalBytes}`)
      log(`[AURA TTS DEBUG] playback=${totalBytes > 0 ? 'cloud_audio' : 'blocked'}`)
      log('[AURA TTS DEBUG] fallback_attempted=false')
      speakingRef.current = false
      setIsAuraSpeaking(false)
      setLastStage('ready')
      options.resumeTracking?.()
      restartIfContinuous()
      }
    }
  }

  const generalSpeechAbortRef = useRef<AbortController | null>(null)
  useEffect(() => () => {
    const controller = generalSpeechAbortRef.current
    generalSpeechAbortRef.current = null
    controller?.abort()
    stopAudioNow()
  }, [])

  const playGeneralTalk = async (chunks: string[], pipeline?: { prefetch?: number; timeout_seconds?: number }) => {
    const sessionId = getSpeechSessionId()
    generalSpeechAbortRef.current?.abort()
    const controller = new AbortController()
    generalSpeechAbortRef.current = controller
    speakingRef.current = true
    setIsAuraSpeaking(true)
    setLastStage('speaking')
    setLastTtsProvider('cartesia')
    setLastTtsError('')
    let completed = false
    let started = false
    let played = 0
    try {
      await runGeneralTalkQueue(chunks, {
        prefetch: pipeline?.prefetch ?? 2,
        fetch: text => fetchCartesiaTtsChunk(text, pipeline?.timeout_seconds ?? 8, AbortSignal.any([controller.signal, AbortSignal.timeout(((pipeline?.timeout_seconds ?? 8) + 1) * 1000)])),
        play: (blob, onStart) => new Promise((resolve, reject) => {
          const onAbort = () => reject(new Error('cancelled'))
          controller.signal.addEventListener('abort', onAbort, { once: true })
          playAuraAudioBlob(blob, blob.type || 'audio/wav', undefined, 'cartesia', { onPlaybackStart: onStart })
            .then(value => { played++; resolve(value) }, reject)
            .finally(() => controller.signal.removeEventListener('abort', onAbort))
        }),
        active: index => {
          if (controller.signal.aborted) return
          if (!started) {
            prepareSegmentSyncWithSegments(chunks, 'cartesia_general_segmented')
            started = true
          }
          setActiveSpeakingSegment(index, chunks.length)
        },
        cancelled: () => controller.signal.aborted || !isSpeechSessionCurrent(sessionId),
        log,
      })
      completed = true
      setLastPlayback('cartesia_general_segmented_played')
      return { playback: 'cloud_audio' as const, speechPlayed: true, ttsBlocked: false, blockReason: null }
    } catch (error) {
      if (!controller.signal.aborted && isSpeechSessionCurrent(sessionId)) {
        setLastTtsError('Cartesia could not continue reading this response aloud.')
        setLastPlayback('cartesia_general_failed')
        log(`[AURA GENERAL TTS] read_aloud_failed reason=cartesia_chunk_failed safe_reason=${generalCartesiaReason(error)}`)
      }
      return { playback: 'blocked' as const, speechPlayed: played > 0, ttsBlocked: true, blockReason: controller.signal.aborted ? 'cancelled' : generalCartesiaReason(error) }
    } finally {
      controller.abort()
      if (generalSpeechAbortRef.current === controller && isSpeechSessionCurrent(sessionId)) {
        generalSpeechAbortRef.current = null
        if (started) stopSegmentSync(completed ? 'playback_end' : 'playback_interrupted')
        speakingRef.current = false
        setIsAuraSpeaking(false)
        setLastStage('ready')
        options.resumeTracking?.()
        restartIfContinuous()
      }
    }
  }

  const playResponse = async (data: any, text: string) => {
    if (!options.autoTts) return { playback: 'disabled', speechPlayed: false, ttsBlocked: false, blockReason: null as string | null }
    const sessionId = beginSpeechSession()
    if (data?.debug?.expo?.intent === 'expo_intro.start' || data?.debug?.expo?.intent === 'expo_guest_greeting.start') {
      return await playSegmentedCartesiaIntro(text, data?.debug?.tts_pipeline)
    }
    if (isNewsReadAloudResponse(data, text)) {
      return await playSegmentedCartesiaNews(text)
    }
    if (data?.debug?.general_tts?.segmented === true) {
      return await playGeneralTalk(data.debug.general_tts.chunks || [], data?.debug?.tts_pipeline)
    }
    prepareSegmentSync(text)
    speakingRef.current = true
    setIsAuraSpeaking(true)
    setLastStage('speaking')
    setLastTtsError('')
    let playback: 'cloud_audio' | 'browser_speech' | 'browser_fallback' | 'blocked' = 'browser_speech'
    let fallbackReason: string | null = data?.tts_debug?.fallback_reason || data?.fallback_reason || data?.tts_error_type || data?.tts_debug?.fish_audio_error_type || data?.error || data?.error_type || data?.audio?.error_type || null
    if (fallbackReason === 'unknown') fallbackReason = 'tts_chain_audio_missing'
    let backendProvider = data?.audio?.provider || data?.providers?.tts || data?.provider || data?.tts_debug?.final_provider || 'browser_speech'
    let audioBase64Present = false
    let audioBytesForLog: number | null = null
    const expoRoute = data?.debug?.expo?.intent === 'expo_intro.start' || data?.debug?.expo?.intent === 'expo_guest_greeting.start' || data?.debug?.expo_demo?.provider_route === 'deterministic_expo_demo'
    const expoBrowserFallbackAllowed = expoRoute && Boolean(data?.debug?.expo?.browser_fallback_allowed || data?.debug?.expo_demo?.browser_fallback_allowed || data?.debug?.expo_browser_fallback_allowed)
    const deferredTts = data?.audio_status?.deferred === true || data?.debug?.deterministic_tts_deferred === true
    const strictFallbackBlocked = !deferredTts && Boolean(data?.audio?.strict_mode_blocked_browser_fallback || data?.tts?.strict_mode_blocked_browser_fallback || data?.tts_debug?.strict_mode_blocked_browser_fallback) && !expoBrowserFallbackAllowed
    const strictPrimary = !deferredTts && Boolean(data?.tts_debug?.strict_primary) && !expoBrowserFallbackAllowed
    const audioPathSearched = 'response.audio.audio_base64, response.audioBase64, response.tts.audio_base64, response.result.audio.audio_base64'
    try {
      const cloud = extractCloudAudio(data)
      audioBase64Present = Boolean(cloud.audioBase64)
      backendProvider = cloud.provider
      audioBytesForLog = cloud.audioBytes
      if (cloud.audioBase64) {
        fallbackReason = null
      }
      if (cloud.audioBase64) {
        log(`[TTS] provider=${cloud.provider} cache_hit=${String(Boolean(data?.tts_debug?.cache_hit))} audio_base64=true audio_bytes=${cloud.audioBytes ?? 'N/A'} playback=cloud_audio`)
        await playAuraAudioFromBase64({ audioBase64: cloud.audioBase64, mimeType: cloud.mimeType, provider: cloud.provider, callbacks: createPlaybackSyncCallbacks('audio_progress') })
        playback = 'cloud_audio'
        setLastTtsProvider(cloud.provider)
        setLastPlayback(`${cloud.provider}_played`)
        setLastAudioBytes(cloud.audioBytes)
      } else {
        fallbackReason = data?.tts_debug?.fallback_reason || data?.fallback_reason || data?.tts_error_type || data?.tts_debug?.fish_audio_error_type || data?.error || data?.error_type || data?.audio?.error_type || 'cloud_audio_missing'
        if (fallbackReason === 'unknown') fallbackReason = 'cloud_audio_missing'
        if (strictFallbackBlocked) {
          backendProvider = data?.audio?.provider || data?.providers?.tts || data?.tts_debug?.final_provider || 'cartesia'
          playback = 'blocked'
          setLastTtsProvider('cartesia')
          setLastPlayback('strict_fallback_blocked')
          setLastAudioBytes(null)
          setLastTtsError('Cartesia voice is unavailable.')
          log(`[TTS] provider_attempt provider=cartesia status=failed reason=cartesia_unavailable`)
          return { playback, speechPlayed: false, ttsBlocked: true, blockReason: fallbackReason }
        }
        if (strictPrimary && backendProvider === 'browser_speech') {
          backendProvider = 'browser_speech'
          playback = 'blocked'
          setLastTtsProvider('cartesia')
          setLastPlayback('strict_mode_violation_blocked')
          setLastAudioBytes(null)
          setLastTtsError('Cartesia voice is unavailable.')
          log('[TTS ERROR] strict_mode_violation backend_provider=browser_speech')
          return { playback, speechPlayed: false, ttsBlocked: true, blockReason: 'strict_mode_violation' }
        }
        const fetched = await fetchCloudTtsSpeech(text, 'auto')
        assertSpeechSession(sessionId)
        if (fetched.ok && fetched.audioBlob) {
          backendProvider = fetched.provider
          audioBytesForLog = fetched.audioBytes
          log(`[TTS] provider=${fetched.provider} audio_base64=true audio_bytes=${fetched.audioBytes ?? 'N/A'} playback=cloud_audio`)
          await playAuraAudioBlob(fetched.audioBlob, fetched.mimeType || 'audio/mpeg', undefined, fetched.provider, createPlaybackSyncCallbacks('audio_progress'))
          playback = 'cloud_audio'
          fallbackReason = null
          setLastTtsProvider(fetched.provider)
          setLastPlayback(`${fetched.provider}_played`)
          setLastAudioBytes(fetched.audioBytes)
        } else {
          backendProvider = expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech'
          fallbackReason = fetched.error || fallbackReason || 'cloud_audio_missing'
          if (fallbackReason === 'unknown') fallbackReason = 'cloud_audio_missing'
          if (deferredTts && !expoBrowserFallbackAllowed) {
            playback = 'blocked'
            fallbackReason = 'tts_provider_failed'
          setLastTtsProvider('tts_provider_failed')
          setLastPlayback('tts_provider_failed')
          setLastTtsError('Voice playback is unavailable.')
          return { playback, speechPlayed: false, ttsBlocked: true, blockReason: fallbackReason }
          }
          setLastTtsError(`Cloud TTS unavailable: ${fallbackReason}`)
          if (expoBrowserFallbackAllowed) {
            log('[AURA EXPO] browser_fallback_allowed_by_env=true')
          }
          log(`[TTS] provider=${backendProvider} fallback=true reason=${fallbackReason || 'cloud_audio_missing'}`)
          await speakWithBrowserSpeech(text, undefined, createPlaybackSyncCallbacks('estimated'))
          playback = expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech'
          setLastTtsProvider(backendProvider)
          setLastPlayback(expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech_played')
          setLastAudioBytes(null)
        }
      }
    } catch (e: any) {
      if (!isSpeechSessionCurrent(sessionId)) return { playback: 'blocked' as const, speechPlayed: false, ttsBlocked: false, blockReason: 'cancelled' }
      const message = e?.message || 'tts playback failed'
      if (strictFallbackBlocked || strictPrimary) {
        backendProvider = data?.audio?.provider || data?.providers?.tts || data?.tts_debug?.final_provider || 'cartesia'
        playback = 'blocked'
        fallbackReason = fallbackReason || 'audio_playback_failed'
        setLastTtsError('Cartesia voice is unavailable.')
        log(`[TTS] provider_attempt provider=cartesia status=failed reason=cartesia_unavailable error=${message}`.trim())
        setLastTtsProvider('cartesia')
        setLastPlayback('strict_fallback_blocked')
        setLastAudioBytes(null)
        return { playback, speechPlayed: false, ttsBlocked: true, blockReason: fallbackReason }
      }
      backendProvider = expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech'
      playback = 'browser_speech'
      fallbackReason = 'audio_playback_failed'
      setLastTtsError(`Cloud TTS unavailable: ${message}`)
      if (expoBrowserFallbackAllowed) {
        log('[AURA EXPO] browser_fallback_allowed_by_env=true')
      }
      log(`[TTS] provider=${backendProvider} fallback=true reason=audio_playback_failed error=${message}`.trim())
      await speakWithBrowserSpeech(text, undefined, createPlaybackSyncCallbacks('estimated'))
      playback = expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech'
      setLastTtsProvider(backendProvider)
      setLastPlayback(expoBrowserFallbackAllowed ? 'browser_fallback' : 'browser_speech_played')
      setLastAudioBytes(null)
    } finally {
      if (isSpeechSessionCurrent(sessionId)) {
      stopSegmentSync(playback === 'blocked' ? 'playback_blocked' : (fallbackReason === 'audio_playback_failed' ? 'playback_error' : 'playback_end'))
      log(`[AURA TTS DEBUG] backend_provider=${backendProvider}`)
      log(`[AURA TTS DEBUG] audio_path=${audioPathSearched}`)
      log(`[AURA TTS DEBUG] audio_base64=${String(audioBase64Present || playback === 'cloud_audio')}`)
      log(`[AURA TTS DEBUG] audio_bytes=${audioBytesForLog ?? 0}`)
      log(`[AURA TTS DEBUG] playback=${playback}`)
      log(`[AURA TTS DEBUG] fallback_reason=${fallbackReason || 'null'}`)
      speakingRef.current = false
      setIsAuraSpeaking(false)
      setLastStage('ready')
      options.resumeTracking?.()
      restartIfContinuous()
      }
    }
    const finalPlayback = String(playback)
    return { playback, speechPlayed: finalPlayback === 'cloud_audio' || finalPlayback === 'browser_speech' || finalPlayback === 'browser_fallback', ttsBlocked: finalPlayback === 'blocked', blockReason: fallbackReason }
  }

  const appendAndSpeak = async (raw: string, normalized: string, response: string, provider: string, data: any = {}) => {
    setLastResponseText(response)
    setLastSpokenText(data.spoken_text || response)
    setLastProvider(provider)
    memory.appendTurn({
      user_text: raw,
      normalized_text: normalized,
      aura_response: response,
      spoken_text: data.spoken_text || response,
      provider,
      model: data.providers?.brain_model,
    })
    log(`[AURA] ${response}`)
    if (data?.debug?.fast_text_response && typeof window !== 'undefined') {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
      log(`[AURA LATENCY DETAIL] route_ms=${data.debug?.latency?.route_ms ?? data.debug?.route_ms ?? 'N/A'} memory_mode=${data.debug?.memory_write_mode || 'background'} tts_mode=${data.debug?.tts_mode || 'post_response'} total_ms=${data.timings?.total_backend_ms ?? 'N/A'}`)
    }
    return await playResponse(data, data.spoken_text || response)
  }

  const activeAbortControllerRef = useRef<AbortController | null>(null)
  const activeRequestIdRef = useRef<string | null>(null)
  const speechStopEpochRef = useRef(0)
  const lastCommandTextRef = useRef<string>('')
  const lastCommandTimeRef = useRef<number>(0)

  const setRequestInFlight = (value: boolean) => {
    voiceTurnInFlightRef.current = value
    setVoiceTurnInFlight(value)
  }

  useEffect(() => {
    return () => {
      if (activeAbortControllerRef.current) {
        activeAbortControllerRef.current.abort('component_unmount')
      }
    }
  }, [])

  const wakeAura = async (reason = 'manual', raw = 'wake up', normalized = 'wake up') => {
    const now = Date.now()
    if (now - lastWakeTimeRef.current < 1500) {
      log('[VOICE] duplicate wake ignored')
      return
    }
    lastWakeTimeRef.current = now
    setAuraAwake(true)
    log(`[VOICE] awake ${reason}`)
    const address = effectiveAddress()
    if (options.autoResponse ?? true) {
      options.pauseTracking?.()
      const reqId = 'req_' + Math.random().toString(36).slice(2, 9)
    const reqController = new AbortController()
    activeAbortControllerRef.current = reqController
    activeRequestIdRef.current = reqId
      setRequestInFlight(true)
      const wakeTimeoutMs = (options.autoTts ?? true) ? WAKE_TOTAL_TIMEOUT_MS : WAKE_BACKEND_TIMEOUT_MS
      log(`[AURA VOICE] request started id=${reqId} kind=wake timeout_ms=${wakeTimeoutMs} backend_timeout_ms=${WAKE_BACKEND_TIMEOUT_MS} tts_timeout_ms=${WAKE_TTS_TIMEOUT_MS}`)

      const data = await sendAuraVoiceTurn({
        request_id: reqId,
        conversation_id: memory.conversationId,
        recent_turns: memory.recentTurns(8),
        raw_transcript: raw,
        normalized_transcript: normalized,
        wake_detected: true,
        alias_matched: reason,
        aura_awake: true,
        conversation_mode: options.mode,
        wake_required: options.wakeRequired ?? true,
        identity_context: identityContext(),
        vision_context: visionContext(),
        effective_address: address.address || 'Sir',
        address_reason: address.reason,
        auto_tts: options.autoTts ?? true,
      }, wakeTimeoutMs, reqController.signal)
      setRequestInFlight(false)
      activeAbortControllerRef.current = null
      activeRequestIdRef.current = null

      if (data.ok) {
        log(`[AURA VOICE] request completed id=${reqId}`)
        return appendAndSpeak(raw, normalized, String(data.response_text || `Awake, ${address.address || 'Sir'}.`), data.providers?.brain || 'deterministic_wake', data)
      }
      if (data.is_aborted || data.error_type === 'aborted') {
        log(`[AURA VOICE] request aborted id=${reqId} reason=${data.abort_reason || 'unknown'}`)
      }
      options.resumeTracking?.()
      setLastError(data.message || data.error_type || 'wake_turn_failed')
    }
    await appendAndSpeak(raw, normalized, `Awake, ${address.address || 'Sir'}.`, 'deterministic_wake')
  }

  const sleepAura = async (reason = 'manual') => {
    setAuraAwake(false)
    log(`[VOICE] standby ${reason}`)
    await appendAndSpeak('standby', 'standby', `Standing by, ${effectiveAddress().address || 'Sir'}.`, 'deterministic_identity')
  }

  const sendTextTurn = async (
    text: string,
    confidence?: number,
    captureState?: { micMutedAtCapture?: boolean; recognitionActiveAtCapture?: boolean },
    dispatchOptions?: VoiceTurnDispatchOptions,
  ) => {
    if (processingRef.current || speakingRef.current) return
    const raw = text.trim()
    if (!raw) return

    const parsedWake = parseWakeAndCommand(raw)
    const normalizedResult = normalizeAuraTranscript(raw)
    const normalized = parsedWake.wakeDetected && parsedWake.commandText ? parsedWake.commandText : (normalizedResult.normalized || raw)
    const normalizedForRouting = normalizeAuraCommandForRouting(normalized)
    const wakeAwareKind = parsedWake.wakeDetected && parsedWake.commandText
      ? (classifyAuraCommandKind(normalizedForRouting, memory.recentTurns(8)) === 'normal_chat' && SMALLTALK_PATTERNS.some((p) => normalizedForRouting.includes(p)) ? 'smalltalk' : classifyAuraCommandKind(normalizedForRouting, memory.recentTurns(8)))
      : classifyAuraCommandKind(normalizedForRouting, memory.recentTurns(8))

    if (isJunkTranscript(raw) || isJunkTranscript(normalizedForRouting)) {
      log('[VOICE] ignored_low_confidence_transcript reason=junk_stt')
      setLastStage('ignored_junk_stt')
      processingRef.current = false
      restartIfContinuous()
      return
    }

    if (typeof confidence === 'number' && confidence < 0.55) {
      const strongIntent = /(hey aura|wake up|stop camera|turn off camera|can you see me|who are you|what can you do|how was your day|how are you|are you online)/i.test(normalizedForRouting)
      if (!strongIntent) {
        log(`[VOICE] transcript_confidence=${confidence.toFixed(2)} low_confidence=true`)
        setLastStage('ignored_low_confidence')
        processingRef.current = false
        restartIfContinuous()
        return
      }
      log(`[VOICE] transcript_confidence=${confidence.toFixed(2)} low_confidence=true strong_intent_allowed=true`)
    }

    processingRef.current = true
    const startTime = performance.now()
    setLastRawTranscript(raw)
    setLastNormalizedTranscript(normalized)
    const inputSource = dispatchOptions?.inputSource || (typeof confidence === 'number' ? 'voice' : 'text')
    const micMutedAtCapture = inputSource === 'voice'
      ? Boolean(captureState?.micMutedAtCapture)
      : !recognitionRunningRef.current
    const recognitionActiveAtCapture = inputSource === 'voice'
      ? Boolean(captureState?.recognitionActiveAtCapture)
      : recognitionRunningRef.current
    const micMutedAfterCapture = !recognitionRunning
    const recognitionActiveAfterCapture = recognitionRunning
    const inputMode = inputSource === 'mobile'
      ? (micMutedAtCapture ? 'remote_text_allowed_while_mic_muted' : 'remote_text')
      : (inputSource === 'text' && micMutedAtCapture ? 'text_allowed_while_mic_muted' : inputSource)
    if (inputSource === 'voice' && micMutedAtCapture && !parsedWake.wakeDetected) {
      log(`[AURA INPUT DEBUG] source=${inputSource} raw_transcript="${raw}"`)
      log(`[AURA INPUT DEBUG] mic_muted_at_capture=true recognition_active_at_capture=${String(recognitionActiveAtCapture)}`)
      log('[AURA INPUT DEBUG] ignored=true reason=mic_muted_at_capture')
      processingRef.current = false
      restartIfContinuous()
      return
    }
    log(`[AURA INPUT DEBUG] source=${inputSource} raw_transcript="${raw}"`)
    log(`[AURA INPUT DEBUG] source=${inputSource} selected_transcript="${normalized}"`)
    log(`[AURA INPUT DEBUG] source=${inputSource} normalized_for_routing="${normalizedForRouting}"`)
    log(`[AURA INPUT DEBUG] source=${inputSource} confidence=${typeof confidence === 'number' ? confidence.toFixed(2) : 'N/A'}`)
    log(`[AURA INPUT DEBUG] source=${inputSource} wake_detected=${String(parsedWake.wakeDetected)}`)
    log(`[AURA INPUT DEBUG] source=${inputSource} command_text="${parsedWake.commandText || ''}"`)
    const intent = dispatchOptions?.frontendKind || (parsedWake.wakeDetected && parsedWake.commandText ? wakeAwareKind : wakeAwareKind)
    log(`[AURA INPUT DEBUG] source=${inputSource} frontend_kind=${intent}`)
    log(`[AURA INPUT DEBUG] input_mode=${inputMode}`)
    log(`[AURA INPUT DEBUG] mic_muted_at_capture=${String(micMutedAtCapture)} recognition_active_at_capture=${String(recognitionActiveAtCapture)}`)
    log(`[AURA INPUT DEBUG] mic_muted_after_capture=${String(micMutedAfterCapture)} recognition_active_after_capture=${String(recognitionActiveAfterCapture)} ignored=false reason=null`)

    if (sleepCommands.has(normalized)) {
      processingRef.current = false
      return sleepAura('command')
    }

    const isExpoIntro = intent === 'expo_intro'
    const isExpoSpeech = isExpoIntro || intent === 'expo_guest_greeting'
    if (dispatchOptions?.expoShortcut) {
      log('[AURA EXPO] intro_shortcut_dispatched key=I')
    }
    if (dispatchOptions?.expoGuestGreetingShortcut) {
      log('[AURA EXPO] guest_greeting_shortcut_dispatched key=P')
    }
    log(`[AURA DEBUG] raw_transcript="${raw}" normalized_for_routing="${normalizedForRouting}" frontend_kind=${intent}`)
    const now = Date.now()

    const isDuplicate = (now - lastCommandTimeRef.current < 1500) && (
      normalized === lastCommandTextRef.current ||
      (intent === 'wake' && (lastCommandTextRef.current === 'wake up' || lastCommandTextRef.current === 'awake' || lastCommandTextRef.current.includes('wake')))
    )

    if (isDuplicate) {
      log('[VOICE] duplicate transcript ignored')
      processingRef.current = false
      restartIfContinuous()
      return
    }

    lastCommandTextRef.current = normalized
    lastCommandTimeRef.current = now

    if (intent === 'wake') {
      if (now - lastWakeTimeRef.current < 1500) {
        log('[VOICE] duplicate wake ignored')
        processingRef.current = false
        restartIfContinuous()
        return
      }
    }

    if ((options.wakeRequired ?? true) && !awakeRef.current && parsedWake.wakeDetected) {
      processingRef.current = false
      return wakeAura(parsedWake.wakePhrase || 'wake', raw, normalized)
    }

    const command = addressCommand(normalized)
    if (command) {
      setSessionAddressOverride(command.address)
      processingRef.current = false
      return appendAndSpeak(raw, normalized, command.response, 'deterministic_identity')
    }

    const inputAllowed = !(options.wakeRequired ?? true) || normalizedResult.wakeDetected || awakeRef.current
    if (!inputAllowed) {
      setLastStage('ignored_non_wake')
      processingRef.current = false
      restartIfContinuous()
      return
    }

    if (!(options.autoResponse ?? true)) {
      processingRef.current = false
      restartIfContinuous()
      return
    }

    setLastStage('thinking')
    const tClassified = performance.now()

    options.pauseTracking?.()
    const idCtx = identityContext()
    const visCtx = visionContext()
    const tContext = performance.now()

    const isFastPath = isExpoSpeech || intent === 'wake' || intent === 'capability' || intent === 'vision' || intent === 'identity' || intent === 'self_capability' || intent === 'runtime_control' || intent === 'tool_command'
    const isNewsFastPath = intent === 'tool_command' && /\b(news|headlines|tavily|newsapi)\b/i.test(normalizedForRouting)
    const timeoutMs = isExpoSpeech ? (dispatchOptions?.timeoutMs ?? 60000) : (intent === 'wake' && (options.autoTts ?? true) ? WAKE_TOTAL_TIMEOUT_MS : (isNewsFastPath ? 10000 : (isFastPath ? 6000 : 18000)))

    const address = effectiveAddress()
    const tBackendStart = performance.now()

    const reqId = 'req_' + Math.random().toString(36).slice(2, 9)
    const reqController = new AbortController()
    activeAbortControllerRef.current = reqController
    activeRequestIdRef.current = reqId

    setRequestInFlight(true)
    log(`[AURA VOICE] request started id=${reqId} kind=${intent} timeout_ms=${timeoutMs}`)

    const data = await sendAuraVoiceTurn({
      request_id: reqId,
      conversation_id: memory.conversationId,
      recent_turns: memory.recentTurns(8),
      raw_transcript: raw,
      normalized_transcript: parsedWake.wakeDetected && parsedWake.commandText ? parsedWake.commandText : normalizedForRouting,
      display_transcript: normalized,
      command_text: parsedWake.commandText || normalizedForRouting,
      frontend_kind: intent,
      kind: intent,
      wake_detected: parsedWake.wakeDetected,
      command_after_wake: parsedWake.wakeDetected && Boolean(parsedWake.commandText),
      wake_phrase: parsedWake.wakePhrase || null,
      alias_matched: normalizedResult.wakeAliasMatched || parsedWake.wakePhrase,
      aura_awake: awakeRef.current,
      conversation_mode: options.mode,
      wake_required: options.wakeRequired ?? true,
      identity_context: {
        decision: idCtx?.decision || 'guest_or_unknown',
        display_name: idCtx?.display_name || 'Guest',
        role: idCtx?.role || 'guest',
        address: idCtx?.address || 'Guest',
        preferred_address: (idCtx as any)?.preferred_address || null,
        address_source: (idCtx as any)?.address_source || null,
        matched_profile_id: (idCtx as any)?.matched_profile_id || null,
        matched_profile_name: (idCtx as any)?.matched_profile_name || null,
        matched_profile: (idCtx as any)?.matched_profile || null,
        identity_confidence: (idCtx as any)?.identity_confidence ?? null,
        current_identity: idCtx?.role || 'guest',
        owner_verified: Boolean(idCtx?.role === 'owner' || visCtx?.current_identity === 'owner'),
        owner_live_verified: Boolean(idCtx?.role === 'owner'),
        owner_face_enrolled: Boolean(idCtx?.owner_face_enrolled),
        owner_voice_enrolled: Boolean(idCtx?.owner_voice_enrolled),
        gesture_tracking_state: (idCtx as any)?.gesture_tracking_state || (visCtx as any)?.gesture_tracking_state || 'off',
        people_tracking_state: (idCtx as any)?.people_tracking_state || (visCtx as any)?.people_tracking_state || (idCtx as any)?.tracking_state || (visCtx as any)?.tracking_state || visCtx?.tracking_status || 'off',
        tracking_state: (idCtx as any)?.people_tracking_state || (visCtx as any)?.people_tracking_state || (idCtx as any)?.tracking_state || (visCtx as any)?.tracking_state || visCtx?.tracking_status || 'off',
        identity_state: (idCtx as any)?.identity_state || (visCtx as any)?.identity_state || 'guest',
        live_identity_fresh: Boolean((idCtx as any)?.live_identity_fresh || (visCtx as any)?.live_identity_fresh),
        live_identity_address_allowed: Boolean((idCtx as any)?.live_identity_address_allowed || (visCtx as any)?.live_identity_address_allowed),
        active_profile_is_unambiguous: Boolean((idCtx as any)?.active_profile_is_unambiguous || (visCtx as any)?.active_profile_is_unambiguous),
        visual_gender_inference: false,
        address_used: address.address || 'Sir',
        address_reason: address.reason || 'local_owner_session',
        last_owner_verified_at: (idCtx as any)?.last_owner_verified_at || null,
        people_count: visCtx?.people_count || 0,
      },
      vision_context: {
        camera_state: (visCtx as any)?.camera_state || visCtx?.camera_status || 'off',
        camera_status: visCtx?.camera_status || 'off',
        stream_ready: Boolean((visCtx as any)?.stream_ready),
        video_ready: Boolean((visCtx as any)?.video_ready),
        video_width: Number((visCtx as any)?.video_width || 0),
        video_height: Number((visCtx as any)?.video_height || 0),
        gesture_tracking_state: (visCtx as any)?.gesture_tracking_state || 'off',
        gesture_tracking_error: (visCtx as any)?.gesture_tracking_error || null,
        people_tracking_state: (visCtx as any)?.people_tracking_state || (visCtx as any)?.tracking_state || visCtx?.tracking_status || 'off',
        people_tracking_error: (visCtx as any)?.people_tracking_error || (visCtx as any)?.tracking_error || null,
        tracking_state: (visCtx as any)?.people_tracking_state || (visCtx as any)?.tracking_state || visCtx?.tracking_status || 'off',
        tracking_status: visCtx?.tracking_status || 'off',
        tracking_started_at: (visCtx as any)?.tracking_started_at || null,
        tracking_error: (visCtx as any)?.tracking_error || null,
        tracking_last_error_code: (visCtx as any)?.tracking_last_error_code || null,
        tracking_last_error_message: (visCtx as any)?.tracking_last_error_message || null,
        tracking_backend: (visCtx as any)?.tracking_backend || 'none',
        model_loaded: Boolean((visCtx as any)?.model_loaded),
        loop_running: Boolean((visCtx as any)?.loop_running),
        people_count: visCtx?.people_count || 0,
        current_identity: visCtx?.current_identity || 'guest',
        owner_verified: Boolean((visCtx as any)?.owner_verified || visCtx?.current_identity === 'owner'),
        last_owner_verified_at: (visCtx as any)?.last_owner_verified_at || (idCtx as any)?.last_owner_verified_at || null,
        last_person_seen_at: (visCtx as any)?.last_person_seen_at || (idCtx as any)?.last_person_seen_at || null,
        last_known_people_count: (visCtx as any)?.last_known_people_count || (idCtx as any)?.last_known_people_count || 0,
        address_selected: visCtx?.address_selected || 'Guest',
        preferred_address: (visCtx as any)?.preferred_address || null,
        address_source: (visCtx as any)?.address_source || null,
        matched_profile_id: (visCtx as any)?.matched_profile_id || null,
        matched_profile_name: (visCtx as any)?.matched_profile_name || null,
        identity_confidence: (visCtx as any)?.identity_confidence ?? null,
        identity_state: (visCtx as any)?.identity_state || 'guest',
        owner_profile_enrolled: Boolean((visCtx as any)?.owner_profile_enrolled || idCtx?.owner_face_enrolled),
        live_identity_fresh: Boolean((visCtx as any)?.live_identity_fresh),
        live_identity_address_allowed: Boolean((visCtx as any)?.live_identity_address_allowed),
        active_profile_is_unambiguous: Boolean((visCtx as any)?.active_profile_is_unambiguous),
        visual_gender_inference: false,
        descriptors_exposed: false,
        raw_frames_uploaded: false,
        fusion_decision: visCtx?.fusion_decision || null,
        tracking_mode: visCtx?.tracking_mode || 'all',
        tracks: Array.isArray(visCtx?.tracks) ? visCtx.tracks.map((t: any) => ({
          track_id: t.track_id,
          status: t.status,
          identity: t.identity,
          address: t.address,
          gender_confidence: t.gender_confidence,
        })) : [],
      },
      effective_address: address.address || 'Sir',
      address_reason: address.reason,
      auto_tts: isExpoSpeech ? true : (options.autoTts ?? true),
      shortcut: dispatchOptions?.shortcut,
    }, timeoutMs, reqController.signal).finally(() => {
      setRequestInFlight(false)
      if (activeRequestIdRef.current === reqId) {
        activeAbortControllerRef.current = null
        activeRequestIdRef.current = null
      }
    })
    const tBackendEnd = performance.now()

    if (!data.ok) {
      if (data.is_aborted || data.error_type === 'aborted') {
        const reason = data.abort_reason || reqController.signal.reason || 'unknown'
        log(`[AURA VOICE] request aborted id=${reqId} reason=${reason} elapsed_ms=${Math.round(tBackendEnd - tBackendStart)}`)
        if (isExpoIntro) log(`[AURA EXPO] intro_failed reason=${reason}`)
        if (isNewsFastPath && String(reason) === 'voice_turn_timeout') {
          const timeoutMessage = 'News request timed out, Sir.'
          setLastError(timeoutMessage)
          const playbackResult = await appendAndSpeak(raw, normalized, timeoutMessage, 'deterministic_news_timeout')
          processingRef.current = false
          options.resumeTracking?.()
          restartIfContinuous()
          return playbackResult
        }
        processingRef.current = false
        options.resumeTracking?.()
        restartIfContinuous()
        return
      }
      const rawError = data.fallback_reason || data.message || data.error_type || 'voice_turn_failed'
      if (data.error_type === 'network_error') {
        log(`[AURA FETCH DEBUG] request_id=${reqId} endpoint=/api/agent/voice-turn frontend_kind=${intent} elapsed_ms=${Math.round(tBackendEnd - tBackendStart)} navigator_online=${String(navigator.onLine)} backend_health=${(data as any).backend_health || 'not_checked'}`)
      }
      const message = formatFallbackReason(rawError)
      if (isExpoIntro) log(`[AURA EXPO] intro_failed reason=${data.error_type || rawError}`)
      setLastError(message)
      setLastStage(data.error_type === 'timeout' ? 'timeout' : 'error')
      log(`[ERROR] voice_turn ${rawError}`)
      processingRef.current = false
      options.resumeTracking?.()
      restartIfContinuous()
      return
    }

    log(`[AURA VOICE] request completed id=${reqId}`)
    if (isExpoIntro && data.debug?.intent_detected === 'expo_intro.start' && !data.debug?.expo?.tts_blocked) {
      log('[AURA EXPO] intro_started')
    }
    log(`[AURA DEBUG] provider_route=${data.debug?.provider_route || data.providers?.brain || 'N/A'} failure_class=${data.debug?.failure_class || 'null'} failure_origin=${data.debug?.failure_origin || 'null'} tool=${data.debug?.tool_name || 'none'} actions_returned=${Array.isArray(data.actions) ? data.actions.length : 0}`)
    log(`[AURA DEBUG] backend_intent_detected=${data.debug?.intent_detected || 'N/A'} frontend_kind=${data.debug?.frontend_kind || intent} used_recent_owner_context=${String(Boolean(data.debug?.used_recent_owner_context))} lastOwnerVerifiedAgeMs=${data.debug?.lastOwnerVerifiedAgeMs ?? 'N/A'} lastKnownPeopleCount=${data.debug?.lastKnownPeopleCount ?? 'N/A'}`)
    if (data.debug?.memory?.write_attempted) {
      if (data.debug.memory.write_success) {
        log(`[AURA MEMORY] persistent_write_success backend=${data.debug.memory.backend || 'unknown'} messages=${data.debug.memory.messages_saved ?? 0} facts=${data.debug.memory.facts_saved ?? 0}`)
      } else {
        log(`[AURA MEMORY] write_failed backend=${data.debug.memory.backend || 'unknown'} failure_class=memory_write_failed safe_reason=${data.debug.memory.last_error_safe || 'unknown'} local_fallback_write_success=${String(Boolean(data.debug.memory.local_fallback_write_success))}`)
      }
    }
    if (data.debug?.memory?.read_attempted && data.debug.memory.read_success) {
      log(`[AURA MEMORY] persistent_read_success backend=${data.debug.memory.backend || 'unknown'} facts=${data.debug.memory.facts_loaded ?? 0}`)
    }
    log(`[AURA LATENCY] backend_ms=${data.timings?.backend_ms ?? data.timings?.llm_ms ?? 'N/A'}`)
    log(`[AURA TTS DEBUG] provider=${data.tts_debug?.final_provider || data.providers?.tts || 'N/A'}`)
    log(`[AURA TTS DEBUG] tts_total_ms=${data.tts_debug?.tts_total_ms ?? data.timings?.tts_ms ?? 'N/A'}`)
    log(`[AURA TTS DEBUG] selected_client=${data.tts_debug?.selected_client || 'N/A'}`)
    log(`[AURA TTS DEBUG] provider_attempt_count=${data.tts_debug?.provider_attempt_count ?? 'N/A'}`)

    const tTtsStart = performance.now()
    const postActions: (() => void)[] = []
    if (Array.isArray(data.actions) && data.actions.length > 0) {
      for (const action of data.actions) {
        if (options.onExecuteAction) {
          const res = await options.onExecuteAction(action)
          if (res.ok) {
            const statusWord = action.type === 'browser.new_tab' || action.type === 'browser.new_window' ? 'dispatched' : 'success'
            log(`[AURA ACTION] ${action.type} ${statusWord}`)
            setLastActionStatus(`${action.type}: ${statusWord}`)
            if (res.spoken) {
              data.response_text = res.spoken
              data.spoken_text = res.spoken
            }
            if (res.post_action) {
              postActions.push(res.post_action)
            }
          } else {
            const errorMsg = res.error || 'action_execution_failed'
            log(`[AURA ACTION] ${action.type} failed: ${errorMsg}`)
            setLastActionStatus(`${action.type}: failed (${errorMsg})`)
            data.response_text = res.spoken || `I could not execute ${action.label || action.type}, ${address.address || 'Sir'}.`
            data.spoken_text = data.response_text
          }
        }
      }
    }
    log(`[AURA DEBUG] actions_executed=${Array.isArray(data.actions) ? data.actions.map((a: any) => a.type).join(',') || 'none' : 'none'}`)

    const playbackEpoch = speechStopEpochRef.current
    const playbackResult = await appendAndSpeak(raw, normalized, String(data.response_text || ''), data.providers?.brain || 'N/A', data)
    if (reqController.signal.aborted || playbackEpoch !== speechStopEpochRef.current) return
    const tPlaybackEnd = performance.now()

    for (const postFn of postActions) {
      try { postFn() } catch { /* noop */ }
    }

    processingRef.current = false

    const latency: LatencyBreakdown = {
      stt_final_ms: Math.round(tClassified - startTime),
      command_classified_ms: Math.round(tClassified - startTime),
      context_snapshot_ms: Math.round(tContext - tClassified),
      backend_ms: Math.round(tBackendEnd - tBackendStart),
      tts_ms: Math.round(data.latency_ms || (tPlaybackEnd - tBackendEnd)),
      playback_ms: Math.round(tPlaybackEnd - tTtsStart),
      total_ms: Math.round(tPlaybackEnd - startTime),
    }
    setLastLatency(latency)
    log(`[AURA LATENCY] stt_final_ms=${latency.stt_final_ms} backend_ms=${latency.backend_ms} tts_ms=${latency.tts_ms} total_ms=${latency.total_ms}`)
    log(`[MEMORY] ${memory.recentTurns(12).length} turns`)
    return {
      ...(playbackResult || {}),
      backendIntent: data.debug?.intent_detected,
      frontendKind: data.debug?.frontend_kind || intent,
      ttsBlocked: Boolean((playbackResult as any)?.ttsBlocked || data.debug?.expo?.tts_blocked),
      blockReason: (playbackResult as any)?.blockReason || data.debug?.expo?.block_reason || null,
    }
  }

  const handleSpeechResult = async (event: any) => {
    if (processingRef.current || speakingRef.current) return
    const resultIndex = event.results.length - 1
    const result = event.results?.[resultIndex]
    if (!result) return

    const alternatives = Array.from(result).map((alt: any) => ({
      transcript: String(alt.transcript || '').trim(),
      confidence: typeof alt.confidence === 'number' ? alt.confidence : undefined,
    })).filter((alt) => alt.transcript)

    if (!result.isFinal) {
      const interimText = alternatives[0]?.transcript || ''
      if (interimText) {
        const parsed = parseWakeAndCommand(interimText)
        if (parsed.wakeDetected && parsed.commandText) {
          const commandText = parsed.commandText
          log(`[AURA MIC DEBUG] interim_transcript="${interimText}" command_text="${commandText}"`)
        }
      }
      return
    }

    const rawText = String(result[0]?.transcript || '').trim()
    if (!rawText) return

    const now = Date.now()
    setLastSuccessfulTranscriptAt(now)
    const fallbackPick = chooseBestAuraTranscript(alternatives)
    const finalText = fallbackPick.raw || fallbackPick.normalized || rawText
    if (now - lastFinalTimeRef.current < 900 && finalText === lastFinalTextRef.current) {
      return
    }
    lastFinalTimeRef.current = now
    lastFinalTextRef.current = finalText
    const captureState = {
      micMutedAtCapture: Boolean(micStoppedByUserRef.current || micPermissionStateRef.current === 'denied'),
      recognitionActiveAtCapture: Boolean(recognitionRunningRef.current),
    }

    try { recognitionRef.current?.stop() } catch { /* noop */ }
    recognitionRef.current = null

    const confidenceValue = typeof (fallbackPick as any).recognitionConfidence === 'number' ? (fallbackPick as any).recognitionConfidence : undefined
    if (typeof confidenceValue === 'number' && confidenceValue < 0.55) {
      const strongIntent = /(hey aura|wake up|stop camera|turn off camera|can you see me|who are you|what can you do|how was your day|how are you|are you online)/i.test(normalizeAuraCommandForRouting(finalText))
      if (!strongIntent) {
        log(`[VOICE] transcript_confidence=${confidenceValue.toFixed(2)} low_confidence=true`)
        log('[VOICE] ignored_low_confidence_transcript reason=low_confidence')
        return
      }
    }

    await sendTextTurn(finalText, confidenceValue, captureState)
  }

  const startRecognition = async (continuous: boolean): Promise<boolean> => {
    if (continuous) {
      return startContinuousListening()
    }
    if (speakingRef.current || processingRef.current) return false
    const Ctor = SpeechRecognitionCtor()
    if (!Ctor) {
      setLastError('SpeechRecognition unavailable')
      return false
    }
    await unlockAudio()
    const recognition = new Ctor()
    recognition.lang = 'en-IN'
    recognition.continuous = false
    recognition.interimResults = false
    recognition.maxAlternatives = 3
    recognition.onresult = (event: any) => handleSpeechResult(event)
    recognition.onerror = (event: any) => {
      const errName = String(event?.error || 'speech_recognition_error')
      setLastError(errName)
      log(errName === 'network' ? '[VOICE] recognition_error network recoverable=true restart_scheduled=true' : `[VOICE] recognition_error ${errName}`)
      setListening(false)
      restartIfContinuous()
    }
    recognition.onend = () => {
      setListening(false)
      setPushToTalkActive(false)
      restartIfContinuous()
    }
    recognitionRef.current = recognition
    setListening(true)
    try {
      recognition.start()
      return true
    } catch {
      return false
    }
  }

  function restartIfContinuous(delayMs = 350) {
    if (micStoppedByUserRef.current) {
      log('[VOICE] restart_skip intentional_mic_stop')
      return
    }
    if (!continuousMicRequestedRef.current) return
    if (speakingRef.current || processingRef.current || voiceTurnInFlightRef.current || document.visibilityState !== 'visible') {
      log('[VOICE] restart_skip busy')
      return
    }
    if (restartTimerRef.current) clearTimeout(restartTimerRef.current)
    restartPendingRef.current = true
    setRestartPending(true)
    restartTimerRef.current = window.setTimeout(() => {
      restartPendingRef.current = false
      setRestartPending(false)
      if (
        continuousMicRequestedRef.current &&
        !speakingRef.current &&
        !processingRef.current &&
        !voiceTurnInFlightRef.current &&
        document.visibilityState === 'visible' &&
        !recognitionRunningRef.current
      ) {
        startContinuousListening().catch((e) => setLastError(e?.message || 'speech_restart_failed'))
      }
    }, delayMs)
  }

  const stopRecognition = () => {
    try { recognitionRef.current?.stop() } catch { /* noop */ }
    recognitionRef.current = null
    setListening(false)
    setRecognitionRunning(false)
    recognitionRunningRef.current = false
  }

  const unlockAudio = async () => {
    const alreadyUnlocked = getAuraAudioState().unlocked
    if (!alreadyUnlocked) log('[VOICE] audio_unlock_started')
    await unlockAuraAudio()
    setAudioUnlocked(getAuraAudioState().unlocked)
    if (!alreadyUnlocked) log('[VOICE] audio_unlock_ready')
  }

  const startContinuousListening = async (): Promise<boolean> => {
    if (speakingRef.current || processingRef.current || voiceTurnInFlightRef.current) {
      log('[VOICE] mic_start_deferred busy')
      return false
    }
    if (recognitionRunningRef.current && micStartupStateRef.current === 'active') {
      return true
    }
    if (pendingStartPromiseRef.current) {
      return pendingStartPromiseRef.current
    }

    const promise = (async () => {
      micStoppedByUserRef.current = false
      intentionalMicStopLoggedRef.current = false
      continuousMicRequestedRef.current = true
      setContinuousMicRequested(true)
      setLastMicStartAttemptAt(Date.now())
      log('[VOICE] mic_start_requested')

      setMicStartupState('unlocking_audio')
      micStartupStateRef.current = 'unlocking_audio'
      const alreadyUnlocked = getAuraAudioState().unlocked
      if (!alreadyUnlocked) log('[VOICE] audio_unlock_started')
      await unlockAuraAudio()
      setAudioUnlocked(getAuraAudioState().unlocked)
      if (micStoppedByUserRef.current || !continuousMicRequestedRef.current) return false
      if (!alreadyUnlocked) log('[VOICE] audio_unlock_ready')

      const Ctor = SpeechRecognitionCtor()
      if (!Ctor) {
        setMicStartupState('failed')
        micStartupStateRef.current = 'failed'
        setLastMicStartError('speech_recognition_unsupported')
        log('[VOICE] recognition_error unsupported')
        log('[VOICE] mic_recovery_required')
        return false
      }

      setMicStartupState('starting_recognition')
      micStartupStateRef.current = 'starting_recognition'
      log('[VOICE] recognition_start_requested')

      if (recognitionRef.current) {
        try { recognitionRef.current.stop() } catch {}
        recognitionRef.current = null
      }

      const recognition = new Ctor()
      recognition.lang = 'en-IN'
      recognition.continuous = true
      recognition.interimResults = true
      recognition.maxAlternatives = 3

      let startFulfilled = false

      return new Promise<boolean>((resolve) => {
        const timeoutId = window.setTimeout(() => {
          if (!startFulfilled) {
            startFulfilled = true
            try { recognition.abort() } catch {}
            recognitionRef.current = null
            setContinuousMicActive(false)
            setRecognitionRunning(false)
            recognitionRunningRef.current = false
            setMicStartupState('failed')
            micStartupStateRef.current = 'failed'
            setLastMicStartError('recognition_start_timeout')
            log('[VOICE] recognition_start_timeout')
            log('[VOICE] mic_recovery_required')
            resolve(false)
          }
        }, 2500)

        recognition.onstart = () => {
          if (micStoppedByUserRef.current || !continuousMicRequestedRef.current) {
            clearTimeout(timeoutId)
            startFulfilled = true
            try { recognition.abort() } catch { /* already stopped */ }
            resolve(false)
            return
          }
          if (!startFulfilled) {
            startFulfilled = true
            clearTimeout(timeoutId)
            recognitionRunningRef.current = true
            setRecognitionRunning(true)
            setContinuousMicActive(true)
            setListening(true)
            setMicStartupState('active')
            micStartupStateRef.current = 'active'
            setMicPermissionState('granted')
            micPermissionStateRef.current = 'granted'
            setLastMicStartError(null)
            lastRecognitionStartAtRef.current = Date.now()
            setLastRecognitionStartAt(Date.now())
            retryCountRef.current = 0
            setRetryCount(0)
            log('[VOICE] recognition_started')
            log('[VOICE] mic_active')
            resolve(true)
          }
        }

        recognition.onerror = (event: any) => {
          const errName = String(event?.error || 'unknown')
          lastRecognitionErrorRef.current = errName
          log(errName === 'network' ? '[VOICE] recognition_error network recoverable=true restart_scheduled=true' : `[VOICE] recognition_error ${errName}`)
          if (errName === 'no-speech') {
            setLastMicStartError('no-speech')
            setListening(false)
            setContinuousMicActive(false)
            setRecognitionRunning(false)
            recognitionRunningRef.current = false
            setMicStartupState(continuousMicRequestedRef.current ? 'retrying' : 'idle')
            micStartupStateRef.current = continuousMicRequestedRef.current ? 'retrying' : 'idle'
            
            // Check if a final transcript was received recently (within last 1200ms)
            const recentTranscript = Date.now() - lastFinalTimeRef.current < 1200
            if (recentTranscript) {
              log('[VOICE] final_transcript_received_after_no_speech')
              log('[VOICE] no_speech_restart_cancelled transcript_received')
              if (!startFulfilled) {
                startFulfilled = true
                clearTimeout(timeoutId)
                resolve(false)
              }
              return
            }

            if (continuousMicRequestedRef.current && !speakingRef.current && !processingRef.current && !voiceTurnInFlightRef.current && document.visibilityState === 'visible') {
              if (restartPendingRef.current) {
                log('[VOICE] watchdog_skip restart_pending')
              } else {
                log('[VOICE] no_speech_restart_scheduled')
                restartIfContinuous(700)
              }
            }
            if (!startFulfilled) {
              startFulfilled = true
              clearTimeout(timeoutId)
              resolve(false)
            }
            return
          }
          if (!startFulfilled) {
            startFulfilled = true
            clearTimeout(timeoutId)
            if (errName === 'not-allowed' || errName === 'service-not-allowed') {
              setMicPermissionState('denied')
              micPermissionStateRef.current = 'denied'
              setMicStartupState('permission_denied')
              micStartupStateRef.current = 'permission_denied'
              setContinuousMicActive(false)
              setRecognitionRunning(false)
              recognitionRunningRef.current = false
              setLastMicStartError('permission_denied')
              log('[VOICE] mic_recovery_required')
              resolve(false)
            } else {
              setLastMicStartError(errName)
              setContinuousMicActive(false)
              setRecognitionRunning(false)
              recognitionRunningRef.current = false
              setMicStartupState('failed')
              micStartupStateRef.current = 'failed'
              log('[VOICE] mic_recovery_required')
              resolve(false)
            }
          } else {
            if (errName === 'not-allowed' || errName === 'service-not-allowed') {
              setMicPermissionState('denied')
              micPermissionStateRef.current = 'denied'
              setMicStartupState('permission_denied')
              micStartupStateRef.current = 'permission_denied'
              setContinuousMicActive(false)
              setRecognitionRunning(false)
              recognitionRunningRef.current = false
              setLastMicStartError('permission_denied')
              log('[VOICE] mic_recovery_required')
            } else {
              setLastMicStartError(errName)
              setListening(false)
              setContinuousMicActive(false)
              setRecognitionRunning(false)
              recognitionRunningRef.current = false
            }
          }
        }

        recognition.onend = () => {
          const now = Date.now()
          setLastRecognitionEndAt(now)
          log('[VOICE] recognition_ended')
          setListening(false)
          setContinuousMicActive(false)
          setRecognitionRunning(false)
          recognitionRunningRef.current = false

          if (
            continuousMicRequestedRef.current &&
            !speakingRef.current &&
            !processingRef.current &&
            !voiceTurnInFlightRef.current &&
            document.visibilityState === 'visible' &&
            micPermissionStateRef.current !== 'denied'
          ) {
            if (restartPendingRef.current) {
              log('[VOICE] watchdog_skip restart_pending')
            } else {
              log(lastRecognitionErrorRef.current === 'no-speech' ? '[VOICE] no_speech_restart_scheduled' : '[VOICE] recognition_restart_scheduled')
              if (restartTimerRef.current) clearTimeout(restartTimerRef.current)
              restartPendingRef.current = true
              setRestartPending(true)
              restartTimerRef.current = window.setTimeout(() => {
                restartPendingRef.current = false
                setRestartPending(false)
                if (
                  continuousMicRequestedRef.current &&
                  !speakingRef.current &&
                  !processingRef.current &&
                  !voiceTurnInFlightRef.current &&
                  !recognitionRunningRef.current
                ) {
                  startContinuousListening().catch(() => undefined)
                }
              }, 400)
            }
          }
        }

        recognition.onresult = (event: any) => handleSpeechResult(event)

        recognitionRef.current = recognition
        try {
          recognition.start()
        } catch (e: any) {
          if (!startFulfilled) {
            startFulfilled = true
            clearTimeout(timeoutId)
            setMicStartupState('failed')
            micStartupStateRef.current = 'failed'
            setLastMicStartError(e?.message || 'start_failed')
            log(`[VOICE] recognition_error ${e?.message || 'start_failed'}`)
            log('[VOICE] mic_recovery_required')
            resolve(false)
          }
        }
      })
    })()

    pendingStartPromiseRef.current = promise
    try {
      const res = await promise
      return res
    } finally {
      pendingStartPromiseRef.current = null
    }
  }

  const stopContinuousListening = (source = 'voice_action') => {
    micStoppedByUserRef.current = true
    if (restartTimerRef.current) {
      clearTimeout(restartTimerRef.current)
      restartTimerRef.current = null
    }
    continuousMicRequestedRef.current = false
    setContinuousMicRequested(false)
    setContinuousMicActive(false)
    setRecognitionRunning(false)
    recognitionRunningRef.current = false
    setMicStartupState('idle')
    micStartupStateRef.current = 'idle'
    retryCountRef.current = 0
    setRetryCount(0)
    restartPendingRef.current = false
    setRestartPending(false)
    stopRecognition()
    log(`[VOICE] mic_muted ${source}`)
  }

  const lastMicToggleRef = useRef(-Infinity)
  const toggleMic = (source: 'click' | 'hotkey' = 'click') => {
    const now = performance.now()
    if (now - lastMicToggleRef.current < 300) return
    lastMicToggleRef.current = now
    const targetOn = !continuousMicRequestedRef.current
    log(source === 'hotkey'
      ? `[UI] mic_toggle_hotkey shortcut=M target=${targetOn ? 'on' : 'off'}`
      : `[UI] mic_toggle_clicked target=${targetOn ? 'on' : 'off'}`)
    if (!targetOn) {
      stopContinuousListening('ui_toggle')
      return
    }
    micStoppedByUserRef.current = false
    intentionalMicStopLoggedRef.current = false
    continuousMicRequestedRef.current = true
    setContinuousMicRequested(true)
    if (speakingRef.current || processingRef.current || voiceTurnInFlightRef.current) {
      log('[VOICE] mic_start_requested')
      log('[VOICE] mic_start_deferred busy')
      return
    }
    startContinuousListening().catch(() => setLastMicStartError('mic_start_failed'))
  }

  const stopSpeakingNow = () => {
    speechStopEpochRef.current++
    const controller = generalSpeechAbortRef.current
    generalSpeechAbortRef.current = null
    controller?.abort()
    activeAbortControllerRef.current?.abort('stop_speaking')
    stopAudioNow()
    log('[AURA TTS STOP] current_audio_stopped=true')
    log('[AURA TTS STOP] queued_chunks_cleared=true')
    log('[AURA TTS STOP] inflight_chunks_cancelled=true')
    stopSegmentSync('user_stop')
    log('[AURA SPEAKING TEXT] sync_cancelled reason=user_stop')
    speakingRef.current = false
    setIsAuraSpeaking(false)
    processingRef.current = false
    setRequestInFlight(false)
    setLastStage('ready')
    log('[VOICE] speaking_stopped voice_action')
    restartIfContinuous()
  }
  const stopSpeaking = stopSpeakingNow

  const startPushToTalk = async () => {
    setPushToTalkActive(true)
    await startRecognition(false)
  }

  const stopPushToTalk = () => stopRecognition()

  const recoverMicIfIdle = async (reason = 'manual_recovery'): Promise<boolean> => {
    if (speakingRef.current || processingRef.current || voiceTurnInFlightRef.current) {
      log(`[VOICE] mic_recovery_ignored busy reason=${reason}`)
      return false
    }
    log(`[VOICE] mic_recovery_started reason=${reason}`)
    return startContinuousListening()
  }

  return {
    auraAwake,
    listening,
    continuousMicActive,
    pushToTalkActive,
    isAuraSpeaking,
    lastRawTranscript,
    lastNormalizedTranscript,
    lastResponseText,
    lastSpokenText,
    lastProvider,
    lastTtsProvider,
    lastPlayback,
    lastAudioBytes,
    lastTtsError,
    lastError,
    lastStage,
    lastLatency,
    lastActionStatus,
    voiceTurnInFlight,
    audioUnlocked,
    sessionAddressOverride,
    effectiveAddress: effectiveAddress(),
    conversationId: memory.conversationId,
    conversationTurns: memory.turns,
    persistentMemoryActive: memory.persistentMemoryActive,
    persistentMemoryBackend: memory.persistentMemoryBackend,
    // Part 1 state machine exposures
    micStartupState,
    micPermissionState,
    continuousMicRequested,
    recognitionRunning,
    speechRecognitionSupported: Boolean(SpeechRecognitionCtor()),
    lastMicStartError,
    lastMicStartAttemptAt,
    lastRecognitionStartAt,
    lastRecognitionEndAt,
    lastSuccessfulTranscriptAt,
    retryCount,
    restartPending,
    // Speaking text segment sync
    speakingSegments,
    activeSpeakingSegmentIndex,
    isSpeakingTextSynced,
    speakingTextAutoScrollEnabled,
    handleUserScrollPause,
    unlockAudio,
    startContinuousListening,
    recoverMicIfIdle,
    stopContinuousListening,
    toggleMic,
    stopSpeaking,
    stopSpeakingNow,
    startPushToTalk,
    stopPushToTalk,
    wakeAura,
    sleepAura,
    sendTextTurn,
    clearConversationMemory: memory.clearMemory,
  }
}
