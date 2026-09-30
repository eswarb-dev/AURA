export type SpeechNormalizationResult = {
  raw: string
  normalized: string
  wakeDetected: boolean
  wakeAliasMatched: string | null
  confidenceHint: 'high' | 'medium' | 'low'
}

const AURA_WAKE_ALIASES = [
  'wakeup',
  'wake up',
  'wake-up',
  'wake up aura',
  'wake up a u r a',
  'aura',
  'a u r a',
  'a.u.r.a',
  'hey aura',
  'hey a u r a',
  'hello aura',
  'ok aura',
  'okay aura',
  'ora',
  'hey ora',
  'aurora',
  'hey aurora',
  'aira',
  'hey aira',
  'ara',
  'hey ara',
  'oura',
  'hey oura',
  'cosmo',
  'hey cosmo',
  'abba',
  'hey abba',
  'eva',
  'hey eva',
  'ada',
  'hey ada',
]

const KNOWN_MISTAKES = new Set(['makeup', 'wake', 'wake-up', 'ora', 'aurora', 'aira', 'ara', 'oura', 'cosmo', 'abba', 'eva', 'ada'])
const NOISE = new Set(['uh', 'um', 'hmm', '...'])

function clean(text: string) {
  return text
    .trim()
    .toLowerCase()
    .replace(/[.,!?;:-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function levenshtein(a: string, b: string): number {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i])
  for (let j = 1; j <= b.length; j++) dp[0][j] = j
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] = Math.min(
        dp[i - 1][j] + 1,
        dp[i][j - 1] + 1,
        dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      )
    }
  }
  return dp[a.length][b.length]
}

export function similarity(a: string, b: string): number {
  const left = clean(a)
  const right = clean(b)
  if (!left && !right) return 1
  if (!left || !right) return 0
  const maxLen = Math.max(left.length, right.length)
  return 1 - levenshtein(left, right) / maxLen
}

function normalizeAliasReplacement(text: string, alias: string) {
  const words = text.split(' ')
  const aliasWords = alias.split(' ')
  const idx = findPhrase(words, aliasWords)
  if (idx < 0) return text
  const before = words.slice(0, idx)
  const after = words.slice(idx + aliasWords.length)
  const prefix = before.filter((w) => !['hey', 'hello', 'ok', 'okay'].includes(w))
  return [...prefix, 'hey', 'aura', ...after].join(' ').replace(/\s+/g, ' ').trim()
}

function findPhrase(words: string[], phrase: string[]) {
  for (let i = 0; i <= words.length - phrase.length; i++) {
    if (phrase.every((w, j) => words[i + j] === w)) return i
  }
  return -1
}

export function normalizeAuraTranscript(rawText: string): SpeechNormalizationResult {
  const raw = rawText.trim()
  const text = clean(raw)
  if (!text || NOISE.has(text)) {
    return { raw, normalized: text, wakeDetected: false, wakeAliasMatched: null, confidenceHint: 'low' }
  }

  const aliases = [...AURA_WAKE_ALIASES].sort((a, b) => b.length - a.length)
  for (const alias of aliases) {
    const aliasClean = clean(alias)
    const words = text.split(' ')
    if (findPhrase(words, aliasClean.split(' ')) >= 0) {
      const matched = aliasClean.replace(/^hey |^hello |^ok |^okay /, '')
      return {
        raw,
        normalized: normalizeAliasReplacement(text, aliasClean),
        wakeDetected: true,
        wakeAliasMatched: matched,
        confidenceHint: KNOWN_MISTAKES.has(matched) ? 'medium' : 'high',
      }
    }
  }

  const words = text.split(' ')
  for (const word of words) {
    if (similarity(word, 'aura') >= 0.65) {
      return {
        raw,
        normalized: normalizeAliasReplacement(text, word),
        wakeDetected: true,
        wakeAliasMatched: word,
        confidenceHint: 'low',
      }
    }
  }

  if (similarity(words.slice(0, 2).join(' '), 'hey aura') >= 0.65) {
    return {
      raw,
      normalized: ['hey', 'aura', ...words.slice(2)].join(' ').trim(),
      wakeDetected: true,
      wakeAliasMatched: words.slice(0, 2).join(' '),
      confidenceHint: 'low',
    }
  }

  return { raw, normalized: text, wakeDetected: false, wakeAliasMatched: null, confidenceHint: 'low' }
}

export function chooseBestAuraTranscript(alternatives: { transcript: string; confidence?: number }[]) {
  if (!alternatives.length) return normalizeAuraTranscript('')
  const normalized = alternatives.map((alt) => ({
    ...normalizeAuraTranscript(alt.transcript),
    recognitionConfidence: alt.confidence,
  }))
  return normalized.find((n) => n.wakeDetected) ?? normalized[0]
}

export function normalizeAuraCommandForRouting(text: string): string {
  let normalized = clean(text)
    .replace(/\bhey ra\b/g, 'hey aura')
    .replace(/\bhey ora\b/g, 'hey aura')
    .replace(/\bhey aurora\b/g, 'hey aura')
    .replace(/\bhey aura\b/g, 'hey aura')
    .replace(/\bwakeapp\b/g, 'wake up')
    .replace(/\bwakeup\b/g, 'wake up')
    .replace(/\bstop cam\b/g, 'stop camera')
    .replace(/\bturn off cam\b/g, 'turn off camera')
    .replace(/\bcan you able to\b/g, 'are you able to')
    .replace(/\bcan we have an general talk\b/g, 'can we have a general talk')
    .replace(/\bare you able to see em\b/g, 'are you able to see me')
    .replace(/\bwhere 2 peoples\b/g, 'were 2 people')
    .replace(/\bthere where\b/g, 'there were')
    .replace(/\bpeoples\b/g, 'people')
    .replace(/\bpersons\b/g, 'people')
    .replace(/\btwo peoples\b/g, 'two people')
    .replace(/\b2 peoples\b/g, '2 people')
    .replace(/\bsee em\b/g, 'see me')
    .replace(/\bcam\b/g, 'camera')
    .replace(/\blife feed\b/g, 'live feed')
    .replace(/\blife video\b/g, 'live video')
    .replace(/\blife videos\b/g, 'live videos')
    .replace(/\bglass\b/g, 'glasses')
    .replace(/\bcrhome\b/g, 'chrome')
    .replace(/\bcrome\b/g, 'chrome')
    .replace(/\bchrom\b/g, 'chrome')
    .replace(/\bchrome tap\b/g, 'chrome tab')
    .replace(/\bsystme\b/g, 'system')
    .replace(/\bvs code\b/g, 'vscode')
    .replace(/\blatest updates\b/g, 'latest news')
    .replace(/\bscreen shot\b/g, 'screenshot')
    .replace(/\bpython compiled\b/g, 'python compile')
    .replace(/\bnewsai\b/g, 'newsapi')
    .replace(/\btamilnadu\b/g, 'tamil nadu')
    .replace(/\btamil puriuma\b/g, 'tamil puriyuma')
    .replace(/\btamil prema\b/g, 'tamil puriyuma')
    .replace(/\btheriuma\b/g, 'theriyuma')
    .replace(/\btamilla\b/g, 'tamil la')
    .replace(/\btamil ah\b/g, 'tamil la')
    .replace(/\btanglish la\b/g, 'tamil la')
    .replace(/\beluththukal\b/g, 'eluthukkal')
    .replace(/\beluthukal\b/g, 'eluthukkal')
    .replace(/\bezhuthukkal\b/g, 'eluthukkal')
    .replace(/\bipo\b/g, 'ippo')
    .replace(/\btoday's\b/g, 'todays')
    .replace(/\s+/g, ' ')
    .trim()

  const words = normalized.split(' ')
  const hasBrowserContext = words.includes('chrome') || words.includes('browser') || normalized.includes('new tab') || normalized.includes('new window')
  if (hasBrowserContext) {
    normalized = words.map((word) => (word === 'tba' || word === 'tap' || word === 'tb' ? 'tab' : word)).join(' ')
  }

  const micCorrected = normalized
    .replace(/\bmeet the mic\b/g, 'mute the mic')
    .replace(/\bmeet mic\b/g, 'mute mic')
    .replace(/\bmeet the microphone\b/g, 'mute the microphone')
    .replace(/\bmeet microphone\b/g, 'mute microphone')
    .replace(/\bmeet my mic\b/g, 'mute mic')
    .replace(/\bmeet my microphone\b/g, 'mute microphone')
    .replace(/\bmute the mike\b/g, 'mute the mic')
    .replace(/\bmute mike\b/g, 'mute mic')
    .replace(/\bmute my mic\b/g, 'mute mic')
    .replace(/\bmute my microphone\b/g, 'mute microphone')
    .replace(/\s+/g, ' ')
    .trim()

  const politeMuteMic = /^(?:can you |could you |please |can you please )?(?:mute|turn off|stop|disable)\s+(?:the |my )?(?:mic|microphone)(?:\s+(?:for a second|for one second|for a moment|now))?$/
  if (politeMuteMic.test(micCorrected)) {
    return 'mute mic'
  }

  return micCorrected
}
