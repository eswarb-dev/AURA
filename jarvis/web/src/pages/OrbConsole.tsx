import { useEffect, useRef, useState, useMemo } from 'react'
import {
  Mic, Square, Send, Cpu, MapPin, FolderGit2, Cloud,
  Activity, Clock, Radio, Plus, Minus, Maximize2,
} from 'lucide-react'
import { useJarvisStore, modelLabel } from '@/store/jarvisStore'
import { useAuraVoiceRuntime } from '@/hooks/useAuraVoiceRuntime'
import { useAuraRemoteControl } from '@/hooks/useAuraRemoteControl'
import { useAuraMainVisionContext } from '@/hooks/useAuraMainVisionContext'
import { AuraBootScreen, type AuraBootLine, type AuraBootPhase } from '@/components/AuraBootScreen'
import { getCapabilities, getProviders } from '@/lib/auraApi'
import { MiniCameraPreview } from '@/components/MiniCameraPreview'
import { cn } from '@/lib/utils'
import { OrbCanvas } from '@/components/OrbCanvas'
import { TaskOrb } from '@/components/TaskOrb'
import { RadialMenu, type RadialPanel } from '@/components/RadialMenu'
import { ErrorBoundary } from '@/components/ErrorBoundary'

// ── Helpers ───────────────────────────────────────────────────────────────

function cleanCaption(text: string, max = 200): string {
  const t = text
    .replace(/<cosmo:[^>]*>[\s\S]*?<\/cosmo:[^>]*>/g, ' ')
    .replace(/<jarvis:[^>]*>[\s\S]*?<\/jarvis:[^>]*>/g, ' ')
    .replace(/[#*`_>|]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (t.length <= max) return t
  return '…' + t.slice(t.length - max)
}

function stateWord(speaking: boolean, listening: boolean, thinking: boolean): string {
  if (speaking) return 'speaking'
  if (listening) return 'listening'
  if (thinking) return 'thinking'
  return 'online'
}

// ── Runtime Console Modal & Mini Widget ───────────────────────────────────────

const FILTER_CATEGORIES = ['ALL', 'VOICE', 'AURA', 'VISION', 'SYSTEM', 'IDENTITY', 'ACTION', 'ERROR', 'MEMORY', 'LATENCY'] as const
const PEOPLE_TRACKING_BOOT_START_DELAY_MS = Number(import.meta.env.VITE_AURA_PEOPLE_TRACKING_BOOT_START_DELAY_MS || 500)

function RuntimeConsoleModal({
  open,
  onClose,
  logs,
  onClearSession,
  onLog,
}: {
  open: boolean
  onClose: () => void
  logs: string[]
  onClearSession: () => void
  onLog: (line: string) => void
}) {
  const [categoryFilter, setCategoryFilter] = useState<string>('ALL')
  const [searchQuery, setSearchQuery] = useState<string>('')
  const [isNearBottom, setIsNearBottom] = useState(true)
  const [copied, setCopied] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  const fullLogs = useMemo(() => logs.slice(-500), [logs])

  const filteredLogs = useMemo(() => {
    return fullLogs.filter((line) => {
      const norm = line.toLowerCase()
      if (categoryFilter !== 'ALL') {
        const cat = categoryFilter.toLowerCase()
        if (cat === 'action' && !norm.includes('[aura action]') && !norm.includes('action')) return false
        if (cat === 'voice' && !norm.includes('[voice]') && !norm.includes('voice')) return false
        if (cat === 'aura' && !norm.includes('[aura]') && !norm.includes('[aura latency]')) return false
        if (cat === 'vision' && !norm.includes('[vision]') && !norm.includes('vision')) return false
        if (cat === 'system' && !norm.includes('[system]') && !norm.includes('[expo]')) return false
        if (cat === 'identity' && !norm.includes('[identity]') && !norm.includes('identity')) return false
        if (cat === 'error' && !norm.includes('[error]') && !norm.includes('failed') && !norm.includes('error')) return false
        if (cat === 'memory' && !norm.includes('memory') && !norm.includes('turn')) return false
        if (cat === 'latency' && !norm.includes('[aura latency]') && !norm.includes('latency')) return false
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim()
        if (!norm.includes(q)) return false
      }
      return true
    })
  }, [fullLogs, categoryFilter, searchQuery])

  const handleScroll = () => {
    if (!bodyRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = bodyRef.current
    setIsNearBottom(scrollHeight - (scrollTop + clientHeight) < 60)
  }

  useEffect(() => {
    if (open && isNearBottom && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [filteredLogs, open, isNearBottom])

  const scrollToBottom = () => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
      setIsNearBottom(true)
    }
  }

  const handleCopy = async () => {
    try {
      const textToCopy = filteredLogs.join('\n')
      await navigator.clipboard.writeText(textToCopy)
      setCopied(true)
      onLog('[SYSTEM] runtime_logs_copied')
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* noop */
    }
  }

  if (!open) return null

  return (
    <>
      <div className="aura-runtime-modal-backdrop" onClick={onClose} />
      <div
        className="aura-runtime-modal font-mono"
        role="dialog"
        aria-modal="true"
        aria-label="Runtime Console"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="aura-runtime-modal-header">
          <div>
            <div className="text-[10px] uppercase tracking-[0.24em] text-[var(--blue)] font-bold mb-0.5">
              LIVE SYSTEM LOGS
            </div>
            <h2 className="text-[16px] font-bold tracking-[0.14em] text-[var(--text)] uppercase m-0">
              RUNTIME CONSOLE
            </h2>
          </div>
          <button
            type="button"
            className="aura-modal-close"
            aria-label="Close runtime console"
            onClick={onClose}
          >
            ×
          </button>
        </div>

        {/* Toolbar */}
        <div className="aura-runtime-modal-toolbar">
          {/* Category Filter Pills */}
          <div className="flex flex-wrap items-center gap-1.5 flex-1 min-w-[280px]">
            {FILTER_CATEGORIES.map((cat) => (
              <button
                key={cat}
                type="button"
                className={cn('aura-log-filter-pill', categoryFilter === cat && 'active')}
                onClick={() => setCategoryFilter(cat)}
              >
                {cat}
              </button>
            ))}
          </div>

          {/* Search Box & Controls */}
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search logs…"
              className="w-44 rounded-full border border-[var(--line-bright)] bg-black/60 px-3 py-1 text-[11px] text-[var(--text)] outline-none focus:border-[var(--blue)] focus:shadow-[0_0_10px_var(--blue-glow)]"
            />
            <button
              type="button"
              onClick={handleCopy}
              className="rounded-full border border-[var(--blue)]/50 bg-[rgba(0,194,255,0.1)] px-3 py-1 text-[10px] font-bold tracking-widest text-[var(--blue)] transition hover:bg-[rgba(0,194,255,0.25)]"
            >
              {copied ? 'COPIED!' : 'COPY'}
            </button>
            <button
              type="button"
              onClick={() => {
                onClearSession()
                onLog('[SYSTEM] session_logs_cleared')
              }}
              className="rounded-full border border-[var(--line-bright)] bg-black/40 px-3 py-1 text-[10px] font-bold tracking-widest text-[var(--text-dim)] transition hover:border-[var(--red)] hover:text-[var(--red)]"
            >
              CLEAR SESSION
            </button>
          </div>
        </div>

        {/* Log Body */}
        <div ref={bodyRef} onScroll={handleScroll} className="aura-runtime-modal-body space-y-1">
          {filteredLogs.length === 0 ? (
            <div className="text-[12px] text-[var(--text-faint)] py-8 text-center">
              No matching runtime logs found.
            </div>
          ) : (
            filteredLogs.map((line, i) => {
              const lower = line.toLowerCase()
              const isErr = lower.includes('[error]') || lower.includes('failed')
              const isAction = lower.includes('[aura action]') || lower.includes('action')
              const isVision = lower.includes('[vision]')
              const isVoice = lower.includes('[voice]')
              const isAura = lower.includes('[aura]') && !isAction

              return (
                <div
                  key={i}
                  className={cn(
                    'flex items-start gap-2 border-b border-[var(--line)]/20 py-1 transition-colors hover:bg-white/[0.02]',
                    isErr ? 'text-[var(--red)]' : isAction ? 'text-[var(--amber)]' : isVision ? 'text-[var(--blue)]' : isVoice ? 'text-cyan-300' : isAura ? 'text-emerald-300' : 'text-[var(--text-dim)]'
                  )}
                >
                  <span className="shrink-0 text-[11px] opacity-60 font-mono select-none">
                    {line.slice(0, 8)}
                  </span>
                  <span className="flex-1 break-all font-mono text-[12.5px] leading-relaxed">
                    {line.slice(9) || line}
                  </span>
                </div>
              )
            })
          )}
        </div>

        {/* Scroll helper */}
        {!isNearBottom && (
          <button
            type="button"
            onClick={scrollToBottom}
            className="absolute bottom-4 right-6 rounded-full border border-[var(--blue)] bg-black/90 px-3.5 py-1.5 font-mono text-[10.5px] font-bold tracking-wider text-[var(--blue)] shadow-[0_0_15px_rgba(0,194,255,0.4)] transition hover:scale-105 pointer-events-auto z-50"
          >
            ↓ JUMP TO LATEST
          </button>
        )}
      </div>
    </>
  )
}

function RuntimeLogsWidget({ logs, onExpand }: { logs: string[]; onExpand?: () => void }) {
  const bodyRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [logs])

  const visibleLogs = logs.slice(-15)

  return (
    <div className="aura-runtime-card pointer-events-auto rounded-xl border border-[var(--line-bright)] bg-black/35 px-3.5 py-2 font-mono backdrop-blur-md">
      <div className="flex items-center justify-between text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)] mb-1 shrink-0">
        <span>runtime</span>
        {onExpand && (
          <button
            type="button"
            className="aura-log-expand-btn"
            aria-label="Open runtime console"
            title="Open runtime console"
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              onExpand()
            }}
          >
            <Maximize2 size={10} />
          </button>
        )}
      </div>
      <div ref={bodyRef} className="log-body min-h-0 flex-1 overflow-y-auto space-y-0.5 pr-1">
        {visibleLogs.length === 0 ? (
          <div className="aura-log-line text-[10px] text-[var(--text-faint)]">no runtime events</div>
        ) : (
          visibleLogs.map((line, i) => (
            <div key={i} className="aura-log-line text-[10px] text-[var(--text-dim)]" title={line}>
              {line}
            </div>
          ))
        )}
      </div>
    </div>
  )
}

// ── Dashboard widgets ─────────────────────────────────────────────────────

function ClockWidget() {
  const [time, setTime] = useState(new Date())
  useEffect(() => {
    const i = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(i)
  }, [])
  const pad = (n: number) => String(n).padStart(2, '0')
  const date = time.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short' })
  return (
    <div className="aura-right-card select-none font-mono">
      <div className="flex items-baseline gap-2">
        <Clock size={11} className="text-[var(--text-faint)]" />
        <span className="text-[18px] tabular-nums text-[var(--text)]">
          {pad(time.getHours())}:{pad(time.getMinutes())}:{pad(time.getSeconds())}
        </span>
      </div>
      <div className="text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
        {date}
      </div>
    </div>
  )
}

interface HardwareStats {
  cpu_pct: number
  ram_pct: number
  ram_used_gb: number
  ram_total_gb: number
  gpu_pct: number | null
  gpu_name: string | null
}

function SystemStatsWidget({ onClick }: { onClick?: () => void }) {
  const [stats, setStats] = useState<HardwareStats | null>(null)
  useEffect(() => {
    let alive = true
    const fetchStats = async () => {
      try {
        const r = await fetch('/api/hardware')
        const data = await r.json()
        if (!alive) return
        const cpuRaw = data.cpu_pct ?? data.cpu_usage ?? (typeof data.cpu === 'number' ? data.cpu : 0)
        const cpuNum = Number(cpuRaw) || 0
        const ram = data.ram ?? null
        const gpu = data.gpu ?? null
        setStats({
          cpu_pct: cpuNum,
          ram_pct: Number(ram?.percent ?? data.ram_pct ?? 0) || 0,
          ram_used_gb: Number(ram?.used_gb ?? 0) || 0,
          ram_total_gb: Number(ram?.total_gb ?? data.ram_gb ?? 0) || 0,
          gpu_pct: gpu?.utilization_pct ?? null,
          gpu_name: gpu?.name ?? null,
        })
      } catch { /* swallow */ }
    }
    fetchStats()
    const i = setInterval(fetchStats, 4000)
    return () => { alive = false; clearInterval(i) }
  }, [])

  const Bar = ({ pct, color }: { pct: number; color: string }) => (
    <div className="h-1 w-12 overflow-hidden rounded-full bg-white/10">
      <div
        className="h-full rounded-full transition-[width] duration-500"
        style={{ width: `${pct}%`, background: color, boxShadow: `0 0 6px ${color}` }}
      />
    </div>
  )

  return (
    <button
      type="button"
      onClick={onClick}
      className="pointer-events-auto group rounded-xl border border-[var(--line-bright)] bg-black/35 px-3.5 py-2 font-mono text-left backdrop-blur-md transition hover:border-[var(--blue)] hover:bg-black/55"
    >
      <div className="flex items-center gap-2 text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
        <Cpu size={11} />
        sys
      </div>
      {stats ? (
        <div className="mt-1 space-y-1 text-[10px]">
          <div className="flex items-center justify-between gap-2 text-[var(--text-dim)]">
            <span>cpu</span>
            <Bar pct={stats.cpu_pct} color="#00c8ff" />
            <span className="tabular-nums text-[var(--text)]">{Number(stats.cpu_pct ?? 0).toFixed(0)}%</span>
          </div>
          <div className="flex items-center justify-between gap-2 text-[var(--text-dim)]">
            <span>ram</span>
            <Bar pct={stats.ram_pct} color="#5cdcff" />
            <span className="tabular-nums text-[var(--text)]">{stats.ram_pct.toFixed(0)}%</span>
          </div>
          {stats.gpu_pct !== null && (
            <div className="flex items-center justify-between gap-2 text-[var(--text-dim)]">
              <span>gpu</span>
              <Bar pct={stats.gpu_pct} color="#ff8c5c" />
              <span className="tabular-nums text-[var(--text)]">{stats.gpu_pct.toFixed(0)}%</span>
            </div>
          )}
        </div>
      ) : (
        <div className="mt-1 text-[10px] text-[var(--text-faint)]">…</div>
      )}
    </button>
  )
}

function WeatherWidget() {
  const [w, setW] = useState<{ temp_c: number; condition: string; city: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    (async () => {
      try {
        const geo = await fetch('https://ipapi.co/json/').then((r) => r.json())
        const lat = geo.latitude, lon = geo.longitude, city = geo.city || 'local'
        const wx = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code`)
          .then((r) => r.json())
        const code = wx.current?.weather_code ?? 0
        const conditions: Record<number, string> = {
          0: 'clear', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast',
          45: 'fog', 48: 'fog',
          51: 'drizzle', 53: 'drizzle', 55: 'drizzle',
          61: 'rain', 63: 'rain', 65: 'rain',
          71: 'snow', 73: 'snow', 75: 'snow',
          80: 'showers', 81: 'showers', 82: 'showers',
          95: 'storm', 96: 'storm', 99: 'storm',
        }
        setW({ temp_c: wx.current?.temperature_2m ?? 0, condition: conditions[code] ?? 'unknown', city })
      } catch (e) {
        setErr('weather offline')
      }
    })()
  }, [])
  return (
    <div className="aura-right-card select-none font-mono">
      <div className="flex items-center gap-2 text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
        <Cloud size={11} />
        weather
      </div>
      {w ? (
        <div className="mt-1">
          <div className="aura-right-card-row text-[15px] tabular-nums text-[var(--text)]">
            {w.temp_c.toFixed(0)}°c <span className="text-[10px] text-[var(--text-dim)]">{w.condition}</span>
          </div>
          <div className="aura-right-card-row text-[9px] uppercase tracking-[0.14em] text-[var(--text-faint)]">{w.city}</div>
        </div>
      ) : (
        <div className="aura-right-card-row mt-1 text-[10px] text-[var(--text-faint)]">{err ?? '…'}</div>
      )}
    </div>
  )
}

interface ProjectContext {
  mode: string
  cwd: string | null
  git_branch: string | null
  git_last_commit: string | null
}

function ProjectContextWidget({ onClick }: { onClick?: () => void }) {
  const mode = useJarvisStore((s) => s.mode)
  const [ctx, setCtx] = useState<ProjectContext | null>(null)
  useEffect(() => {
    fetch(`/api/mode`).then((r) => r.json()).then((d) => {
      setCtx((c) => ({ ...c, mode: d.mode } as ProjectContext))
    }).catch(() => undefined)
  }, [mode])
  useEffect(() => {
    if (!ctx?.cwd) return
    fetch(`/api/project/context?cwd=${encodeURIComponent(ctx.cwd)}`)
      .then((r) => r.json())
      .then((d) => setCtx((c) => ({ ...c, ...d })))
      .catch(() => undefined)
  }, [ctx?.cwd])
  return (
    <button
      type="button"
      onClick={onClick}
      className="pointer-events-auto group rounded-xl border border-[var(--line-bright)] bg-black/35 px-3.5 py-2 font-mono text-left backdrop-blur-md transition hover:border-[var(--blue)] hover:bg-black/55"
    >
      <div className="flex items-center gap-2 text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">
        <FolderGit2 size={11} />
        project
      </div>
      <div className="mt-1 text-[13px] text-[var(--text)]">
        {ctx?.mode ?? mode}
      </div>
      {ctx?.cwd && (
        <div className="max-w-[180px] truncate text-[9.5px] text-[var(--text-dim)]">
          {ctx.cwd.replace(/^C:\\Users\\[^\\]+\\/, '~/').replace(/^.*?\\Users\\[^\\]+\\/, '~/')}
        </div>
      )}
      {ctx?.git_branch && (
        <div className="flex items-center gap-1 text-[9.5px] text-[var(--amber)]">
          <MapPin size={9} />
          {ctx.git_branch}
        </div>
      )}
    </button>
  )
}

function ActivityWidget() {
  const [items, setItems] = useState<{ time: number; text: string }[]>([])
  useEffect(() => {
    let alive = true
    const fetchHistory = async () => {
      try {
        const r = await fetch('/api/history')
        const data = await r.json()
        if (!alive || !data?.messages) return
        const turns = data.messages
          .filter((m: { role: string }) => m.role === 'user')
          .slice(-6)
          .map((m: { content: string; ts?: number }) => ({
            time: Date.now() - Math.random() * 60000,
            text: (m.content || '').slice(0, 60),
          }))
        setItems(turns)
      } catch { /* noop */ }
    }
    fetchHistory()
    const i = setInterval(fetchHistory, 8000)
    return () => { alive = false; clearInterval(i) }
  }, [])
  return (
    <div className="aura-recent-card pointer-events-auto rounded-xl border border-[var(--line-bright)] bg-black/35 px-3.5 py-2 font-mono backdrop-blur-md">
      <div className="flex items-center gap-2 text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)] shrink-0">
        <Activity size={11} />
        recent
      </div>
      <div className="log-body mt-1 space-y-0.5 text-[10px]">
        {items.length === 0 && (
          <div className="aura-log-line text-[var(--text-faint)]">no turns yet</div>
        )}
        {items.slice(0, 5).map((it, i) => (
          <div key={i} className="aura-log-line text-[var(--text-dim)]" title={it.text}>
            <span className="text-[var(--text-faint)]">·</span> {it.text || '…'}
          </div>
        ))}
      </div>
    </div>
  )
}

function WakeIndicator({
  listening,
  speaking,
  thinking,
  awake,
  activationRequired,
}: {
  listening: boolean
  speaking?: boolean
  thinking?: boolean
  awake?: boolean
  activationRequired?: boolean
}) {
  let label = 'standby'
  let colorClass = 'border-[var(--line-bright)] text-[var(--text-faint)]'

  if (activationRequired) {
    label = 'activation needed'
    colorClass = 'border-[var(--amber)] text-[var(--amber)] shadow-[0_0_10px_var(--amber-glow)]'
  } else if (speaking) {
    label = 'speaking'
    colorClass = 'border-[var(--blue)] text-[var(--blue)] shadow-[0_0_12px_var(--blue-glow)]'
  } else if (thinking) {
    label = 'thinking'
    colorClass = 'border-[var(--blue)] text-[var(--blue)]'
  } else if (awake) {
    label = 'awake'
    colorClass = 'border-[var(--blue)] text-[var(--blue)] shadow-[0_0_10px_var(--blue-glow)]'
  } else if (listening) {
    label = 'listening'
    colorClass = 'border-[var(--amber)] text-[var(--amber)] shadow-[0_0_10px_var(--amber-glow)]'
  }

  return (
    <div className={cn(
      'pointer-events-none rounded-full border px-3 py-1 font-mono text-[10px] tracking-[0.2em] backdrop-blur-md transition',
      colorClass,
    )}>
      <Radio size={10} className="mr-1.5 inline align-[-1px]" />
      {label}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────

export function OrbConsole() {
  const connected = useJarvisStore((s) => s.connected)
  const models = useJarvisStore((s) => s.models)
  const activeModel = useJarvisStore((s) => s.activeModel)
  const mode = useJarvisStore((s) => s.mode)
  const setMode = useJarvisStore((s) => s.setMode)
  const transcript = useJarvisStore((s) => s.transcript)
  const responseLive = useJarvisStore((s) => s.responseLive)
  const responseTurns = useJarvisStore((s) => s.responseTurns)
  const socketSpeaking = useJarvisStore((s) => s.speaking)
  const socketListening = useJarvisStore((s) => s.listening)
  const turnActive = useJarvisStore((s) => s.turnActive)
  const stop = useJarvisStore((s) => s.stop)
  const task = useJarvisStore((s) => s.task)

  const searchParams = new URLSearchParams(window.location.search)
  const expoParam = searchParams.get('expo')
  const controlsParam = searchParams.get('controls')
  const previewParam = searchParams.get('preview')
  const envExpoAuto = import.meta.env.VITE_AURA_EXPO_AUTO_ACTIVE !== 'false'
  const envHideControls = import.meta.env.VITE_AURA_HIDE_RUNTIME_CONTROLS !== 'false'

  const isExpoMode = expoParam !== '0' && envExpoAuto
  const hideControls = isExpoMode && controlsParam !== '1' && envHideControls
  const showMiniPreview = previewParam !== '0'

  const [text, setText] = useState('')
  const [showInput, setShowInput] = useState(false)
  const [orbBrightness, setOrbBrightness] = useState(1.0)
  const [runtimeLogs, setRuntimeLogs] = useState<string[]>([])
  const [runtimeConsoleOpen, setRuntimeConsoleOpen] = useState(false)
  const [runtimeActivated, setRuntimeActivated] = useState(false)
  const [activationRequired, setActivationRequired] = useState(false)
  const [lastActivationError, setLastActivationError] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<any>(null)
  const [providerStatus, setProviderStatus] = useState<any>(null)
  const [miniPreviewCollapsed, setMiniPreviewCollapsed] = useState(false)
  const [gestureTrackingState, setGestureTrackingState] = useState('off')
  const [bootPhase, setBootPhase] = useState<AuraBootPhase>('initializing')
  const [bootLines, setBootLines] = useState<AuraBootLine[]>([
    { key: 'init', text: 'INITIALIZING...', status: 'active' },
    { key: 'load', text: 'LOADING A.U.R.A v1.0', status: 'pending' },
    { key: 'audio', text: 'PROBING audio device...', status: 'pending' },
    { key: 'mic', text: 'PROBING microphone...', status: 'pending' },
    { key: 'camera', text: 'STARTING camera...', status: 'pending' },
    { key: 'tracking', text: 'STARTING people tracking...', status: 'pending' },
    { key: 'identity', text: 'VERIFYING owner identity...', status: 'pending' },
    { key: 'runtime', text: 'LINKING runtime...', status: 'pending' },
    { key: 'online', text: 'ONLINE. what do you actually need?', status: 'pending' },
  ])
  const [bootErrors, setBootErrors] = useState<string[]>([])

  const identityContextRef = useRef<any>(null)
  const visionContextRef = useRef<any>(null)
  const executedActionIdsRef = useRef<Set<string>>(new Set())
   const textInputRef = useRef<HTMLInputElement>(null)
   const activeSegmentRef = useRef<HTMLDivElement>(null)
   const speakingTextContainerRef = useRef<HTMLDivElement>(null)
   const addRuntimeLog = (line: string) => setRuntimeLogs((current) => [...current, `${new Date().toLocaleTimeString('en-GB')} ${line}`].slice(-500))
  const logRuntime = (line: string) => addRuntimeLog(line)
  const bootLogKeysRef = useRef<Set<string>>(new Set())
  const logBoot = (key: string, line: string) => {
    if (bootLogKeysRef.current.has(key)) return
    bootLogKeysRef.current.add(key)
    addRuntimeLog(line)
  }
  const updateBootLine = (key: string, status: AuraBootLine['status'], detail?: string) => {
    setBootLines((current) => current.map((line) => line.key === key ? { ...line, status, detail } : line))
  }
  const resetBootForRetry = () => {
    setBootErrors([])
    setBootLines((current) => current.map((line) => ({
      ...line,
      status: line.key === 'init' ? 'ok' : line.key === 'load' ? 'ok' : 'pending',
      detail: undefined,
    })))
  }
  const vision = useAuraMainVisionContext(addRuntimeLog)
  identityContextRef.current = vision.identityContext
  visionContextRef.current = vision.visionContext

  useEffect(() => {
    const onGestureStatus = (event: Event) => {
      const detail = (event as CustomEvent<any>).detail || {}
      setGestureTrackingState(String(detail.gesture_tracking_state || 'unknown'))
    }
    window.addEventListener('aura-gesture-tracking-status', onGestureStatus)
    return () => window.removeEventListener('aura-gesture-tracking-status', onGestureStatus)
  }, [])
  const [lastActionDebug, setLastActionDebug] = useState<{
    last_action_type: string
    last_action_status: 'success' | 'failed'
    last_action_error: string
    last_action_duration_ms: number
    camera_status_after_action: string
    tracking_status_after_action: string
    address_used_for_action: string
  } | null>(null)

  useEffect(() => {
    let alive = true
    const loadStatus = async () => {
      const [caps, providers] = await Promise.all([
        getCapabilities().catch((e) => ({ ok: false, message: e?.message || 'capabilities_failed' })),
        getProviders().catch((e) => ({ ok: false, message: e?.message || 'providers_failed' })),
      ])
      if (!alive) return
      setCapabilities(caps)
      setProviderStatus(providers)
    }
    loadStatus()
    const timer = window.setInterval(loadStatus, 15000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const handleExecuteAction = async (action: { type: string; label: string; value?: any; url?: string; mode?: string; target?: string; request_id?: string; action_execution_id?: string }) => {
    const startAction = performance.now()
    const idCtxForAction = vision.getIdentityContext()
    const liveAddressAllowed = Boolean(idCtxForAction?.live_identity_address_allowed || vision.getVisionContext()?.live_identity_address_allowed)
    const idAddress = liveAddressAllowed ? idCtxForAction?.address : null
    const actionAddress = (idAddress && idAddress !== 'Guest') ? idAddress : 'Sir'
    const type = action.type
    const stateBefore = vision.getVisionContext()
    const beforeCamera = String(stateBefore?.camera_status || vision.cameraStatus || (vision.cameraOn ? 'active' : 'off'))
    const beforeTracking = String(stateBefore?.people_tracking_state || stateBefore?.tracking_status || vision.trackingStatus || (vision.tracking ? 'active' : 'off'))
    const beforePeople = Number(stateBefore?.people_count ?? vision.peopleCount ?? 0)
    const actionExecutionId = `${action.request_id || action.action_execution_id || 'current'}:${type}:${action.target || action.label || 'default'}`
    addRuntimeLog(`[AURA ACTION] received ${type}`)
    if (executedActionIdsRef.current.has(actionExecutionId)) {
      addRuntimeLog(`[AURA ACTION] duplicate ${type} skipped`)
      return { ok: true }
    }
    executedActionIdsRef.current.add(actionExecutionId)
    window.setTimeout(() => executedActionIdsRef.current.delete(actionExecutionId), 30000)
    addRuntimeLog(`[AURA VISION STATE] before_action camera=${beforeCamera} people_tracking=${beforeTracking} gesture_tracking=${stateBefore?.gesture_tracking_state || 'off'} people=${beforePeople}`)
    addRuntimeLog(`[AURA ACTION] executing ${type}`)

    const waitForGestureState = (wanted: 'active' | 'off', timeoutMs = 5000): Promise<{ ok: boolean; state: string; error?: string }> => {
      return new Promise((resolve) => {
        let done = false
        const finish = (value: { ok: boolean; state: string; error?: string }) => {
          if (done) return
          done = true
          window.removeEventListener('aura-gesture-tracking-status', onStatus as EventListener)
          window.clearTimeout(timer)
          resolve(value)
        }
        const onStatus = (event: CustomEvent<any>) => {
          const detail = event.detail || {}
          const state = String(detail.gesture_tracking_state || 'unknown')
          const error = detail.gesture_tracking_error?.code || detail.gesture_tracking_error?.safe_message
          if (state === wanted) finish({ ok: true, state })
          if (state === 'error') finish({ ok: false, state, error: String(error || 'gesture_tracking_error') })
        }
        const timer = window.setTimeout(() => finish({ ok: false, state: 'timeout', error: 'gesture_tracking_timeout' }), timeoutMs)
        window.addEventListener('aura-gesture-tracking-status', onStatus as EventListener)
      })
    }

    let result: { ok: boolean; spoken?: string; error?: string; post_action?: () => void } = {
      ok: false,
      error: 'unknown_action_type',
      spoken: `Unknown action type ${type}, ${actionAddress}.`,
    }

    if (type === 'camera.stop') {
      vision.stopCamera('voice_action')
      result = { ok: true, spoken: `Camera is off, ${actionAddress}.` }
    } else if (type === 'camera.start') {
      const res = await vision.startCamera('voice_action')
      if (res.ok) {
        result = { ok: true, spoken: `Camera is on, ${actionAddress}.` }
      } else {
        result = {
          ok: false,
          error: res.error || 'camera_failed',
          spoken: `I could not turn on the camera because permission was denied, ${actionAddress}.`,
        }
      }
    } else if (type === 'tracking.stop' || type === 'people_tracking.stop') {
      vision.stopTracking('voice_action')
      result = { ok: true, spoken: `People tracking is off, ${actionAddress}.` }
    } else if (type === 'tracking.start' || type === 'people_tracking.start') {
      addRuntimeLog('[AURA ACTION] people_tracking.start dispatched')
      let camOk = vision.cameraStatus === 'active' || vision.cameraOn
      if (!camOk) {
        const camRes = await vision.startCamera('tracking_required')
        camOk = camRes.ok
        if (!camOk) {
          result = {
            ok: false,
            error: camRes.error || 'camera_failed',
            spoken: `I could not activate people tracking, ${actionAddress}. Camera access is not active.`,
          }
        }
      }
      if (camOk) {
        const trackRes = await vision.startTracking('voice_action')
        const trackingActive = trackRes.ok
        if (trackingActive) {
          addRuntimeLog('[AURA ACTION] people_tracking.start verified active')
          result = { ok: true, spoken: `People tracking is active, ${actionAddress}.` }
        } else {
          addRuntimeLog(`[AURA ACTION] people_tracking.start failed reason=${trackRes.error || 'people_tracking_error'}`)
          result = {
            ok: false,
            error: trackRes.error || 'people_tracking_failed',
            spoken: `Camera is active, ${actionAddress}, but people tracking could not start: ${trackRes.error || 'people_tracking_error'}.`,
          }
        }
      }
    } else if (type === 'gesture_tracking.start') {
      addRuntimeLog('[AURA ACTION] gesture_tracking.start dispatched')
      window.dispatchEvent(new CustomEvent('aura-gesture-tracking-command', { detail: { action: 'start' } }))
      const gesture = await waitForGestureState('active')
      if (gesture.ok) {
        result = { ok: true, spoken: `Gesture control is active, ${actionAddress}.` }
      } else {
        result = { ok: false, error: gesture.error || 'gesture_tracking_failed', spoken: `Gesture control could not start, ${actionAddress}: ${gesture.error || 'gesture_tracking_failed'}.` }
      }
    } else if (type === 'gesture_tracking.stop') {
      addRuntimeLog('[AURA ACTION] gesture_tracking.stop dispatched')
      window.dispatchEvent(new CustomEvent('aura-gesture-tracking-command', { detail: { action: 'stop' } }))
      const gesture = await waitForGestureState('off')
      result = gesture.ok
        ? { ok: true, spoken: `Gesture control is off, ${actionAddress}.` }
        : { ok: false, error: gesture.error || 'gesture_tracking_stop_failed', spoken: `Gesture control could not stop, ${actionAddress}: ${gesture.error || 'gesture_tracking_stop_failed'}.` }
    } else if (type === 'mic.stop') {
      runtime.stopContinuousListening()
      logRuntime('[AURA ACTION] mic.stop success')
      logRuntime('[VOICE] mic_muted voice_action')
      result = {
        ok: true,
        spoken: `Microphone muted, ${actionAddress}.`,
      }
    } else if (type === 'mic.start') {
      logRuntime('[AURA ACTION] mic.start success')
      logRuntime('[VOICE] mic_active voice_action')
      await runtime.unlockAudio()
      const ok = await runtime.startContinuousListening()
      if (ok && runtime.recognitionRunning) {
        result = { ok: true, spoken: `Microphone active, ${actionAddress}.` }
      } else {
        result = {
          ok: false,
          error: runtime.lastMicStartError || 'mic_start_failed',
          spoken: `I could not activate the mic, ${actionAddress}.`,
        }
      }
    } else if (type === 'standby.enter') {
      await runtime.sleepAura('voice_action')
      result = { ok: true, spoken: `Standing by, ${actionAddress}.` }
    } else if (type === 'wake.enter') {
      await runtime.wakeAura('voice_action')
      result = { ok: true, spoken: `Awake, ${actionAddress}.` }
    } else if (type === 'panel.projects.open') {
      window.dispatchEvent(new CustomEvent('jarvis-open-panel', { detail: { panel: 'projects' } }))
      result = { ok: true, spoken: `Opening projects panel, ${actionAddress}.` }
    } else if (type === 'panel.projects.close') {
      window.dispatchEvent(new CustomEvent('jarvis-close-panel'))
      result = { ok: true, spoken: `Closing projects panel, ${actionAddress}.` }
    } else if (type === 'panel.tasks.open') {
      window.dispatchEvent(new CustomEvent('jarvis-open-panel', { detail: { panel: 'tasks' } }))
      result = { ok: true, spoken: `Opening tasks panel, ${actionAddress}.` }
    } else if (type === 'panel.tasks.close') {
      window.dispatchEvent(new CustomEvent('jarvis-close-panel'))
      result = { ok: true, spoken: `Closing tasks panel, ${actionAddress}.` }
    } else if (type === 'diagnostics.open') {
      window.location.href = '/diagnostics'
      result = { ok: true, spoken: `Opening diagnostics, ${actionAddress}.` }
    } else if (type === 'browser.open_url' || type === 'browser.search') {
      const url = typeof action.value === 'string' ? action.value : ''
      if (/^https?:\/\//i.test(url)) {
        window.open(url, '_blank', 'noopener,noreferrer')
        result = { ok: true, spoken: `Opening browser, ${actionAddress}.` }
      } else {
        result = { ok: false, error: 'invalid_browser_url', spoken: `I could not open that URL safely, ${actionAddress}.` }
      }
    } else if (type === 'browser.new_tab') {
      const url = action.url === 'chrome://newtab/' ? action.url : 'chrome://newtab/'
      addRuntimeLog(`[AURA ACTION RESULT] action_type=${type} executor_used=window.open command_mode=new_tab target_url_or_mode=${url} process_start_attempted=true`)
      window.open(url, '_blank', 'noopener,noreferrer')
      addRuntimeLog(`[AURA DEBUG] failure_class=local_action_dispatched_unverified failure_origin=action_executor action_type=${type}`)
      result = { ok: true, spoken: 'I sent the command, Sir, but I could not verify that it completed.' }
    } else if (type === 'browser.new_window') {
      addRuntimeLog(`[AURA ACTION RESULT] action_type=${type} executor_used=window.open command_mode=new_window target_url_or_mode=${action.mode || 'new_window'} process_start_attempted=true`)
      window.open(undefined, '_blank', 'noopener,noreferrer')
      addRuntimeLog(`[AURA DEBUG] failure_class=local_action_dispatched_unverified failure_origin=action_executor action_type=${type}`)
      result = { ok: true, spoken: 'I sent the command, Sir, but I could not verify that it completed.' }
    } else if (type === 'app.open') {
      result = { ok: true, spoken: `Opening ${action.label || 'the app'}, ${actionAddress}.` }
    } else if (type === 'folder.open') {
      result = { ok: true, spoken: `Opening folder, ${actionAddress}.` }
    } else if (type === 'screenshot.capture') {
      result = { ok: true, spoken: `Screenshot capture is ready, ${actionAddress}.` }
    } else if (type === 'volume.up') {
      result = { ok: true, spoken: `Volume up, ${actionAddress}.` }
    } else if (type === 'volume.down') {
      result = { ok: true, spoken: `Volume down, ${actionAddress}.` }
    } else if (type === 'volume.mute') {
      result = { ok: true, spoken: `Volume muted, ${actionAddress}.` }
    } else if (type === 'laptop.lock') {
      result = { ok: true, spoken: `Lock command prepared, ${actionAddress}.` }
    } else if (type === 'orb.brightness.set') {
      const val = action.value ? Number(action.value) / 100 : 1.0
      setOrbBrightness(val)
      result = { ok: true, spoken: `Setting orb brightness to ${action.value || 100} percent, ${actionAddress}.` }
    } else if (type === 'orb.brightness.increase') {
      setOrbBrightness((prev) => Math.min(1.0, prev + 0.25))
      result = { ok: true, spoken: `Increasing orb brightness, ${actionAddress}.` }
    } else if (type === 'orb.brightness.decrease') {
      setOrbBrightness((prev) => Math.max(0.2, prev - 0.3))
      result = { ok: true, spoken: `Dimming the orb, ${actionAddress}.` }
    }

    const duration = Math.round(performance.now() - startAction)
    const stateAfter = vision.getVisionContext()
    const isPeopleTrackingStart = type === 'tracking.start' || type === 'people_tracking.start'
    const isPeopleTrackingStop = type === 'tracking.stop' || type === 'people_tracking.stop'
    const afterCamera = type === 'camera.stop' ? 'off' : String(stateAfter?.camera_status || (type === 'camera.start' || isPeopleTrackingStart ? (result.ok || vision.cameraOn ? 'active' : 'off') : (vision.cameraStatus || (vision.cameraOn ? 'active' : 'off'))))
    const afterTracking = isPeopleTrackingStop || type === 'camera.stop'
      ? 'off'
      : String(stateAfter?.people_tracking_state || stateAfter?.tracking_status || (isPeopleTrackingStart ? (result.ok ? 'active' : 'off') : (vision.trackingStatus || (vision.tracking ? 'active' : 'off'))))
    const afterPeople = Number(stateAfter?.people_count ?? vision.peopleCount ?? 0)
    addRuntimeLog(`[AURA VISION STATE] after_action camera=${afterCamera} people_tracking=${afterTracking} gesture_tracking=${stateAfter?.gesture_tracking_state || 'off'} people=${afterPeople}`)
    if (isPeopleTrackingStart && afterCamera === 'active' && afterTracking !== 'active') {
      addRuntimeLog('[AURA STATE WARNING] response_blocked_false_people_tracking_success camera=active people_tracking=off')
    }
    setLastActionDebug({
      last_action_type: type,
      last_action_status: isPeopleTrackingStart ? (afterTracking === 'active' ? 'success' : 'failed') : (result.ok ? 'success' : 'failed'),
      last_action_error: result.error || 'none',
      last_action_duration_ms: duration,
      camera_status_after_action: afterCamera,
      tracking_status_after_action: afterTracking,
      address_used_for_action: actionAddress,
    })

    return result
  }

  const runtime = useAuraVoiceRuntime({
    mode: 'main_orb',
    wakeRequired: true,
    autoResponse: true,
    autoTts: true,
    getIdentityContext: () => vision.getIdentityContext(),
    getVisionContext: () => ({
      ...vision.getVisionContext(),
      gesture_tracking_state: gestureTrackingState,
    }),
    pauseTracking: vision.pauseTracking,
    resumeTracking: vision.resumeTracking,
    onExecuteAction: handleExecuteAction,
    onLog: addRuntimeLog,
  })
  useAuraRemoteControl({
    sendTextTurn: runtime.sendTextTurn,
    stopSpeaking: runtime.stopSpeaking,
    stopContinuousListening: runtime.stopContinuousListening,
    startContinuousListening: runtime.startContinuousListening,
    onLog: addRuntimeLog,
  })

  const {
    speakingSegments,
    activeSpeakingSegmentIndex,
    isSpeakingTextSynced,
    handleUserScrollPause,
  } = runtime

  // Part 4 — Ordered Runtime Activation
  const runtimeActivationPromiseRef = useRef<Promise<void> | null>(null)
  const activateAuraExpoRuntime = async (reason = 'expo_auto') => {
    if (runtime.voiceTurnInFlight || runtime.isAuraSpeaking) {
      addRuntimeLog(`[VOICE] mic_recovery_ignored request_or_tts_active reason=${reason}`)
      return
    }
    if (runtimeActivationPromiseRef.current) {
      addRuntimeLog(`[EXPO] activation_joined ${reason}`)
      return runtimeActivationPromiseRef.current
    }
    runtimeActivationPromiseRef.current = (async () => {
      if (reason === 'boot_authorize_click') {
        logBoot(`authorize-${Date.now()}`, '[BOOT] authorize_click')
        resetBootForRetry()
      }
      setBootPhase('initializing')
      updateBootLine('init', 'ok')
      updateBootLine('load', 'ok')
      logBoot('boot-init', '[BOOT] initializing')
      addRuntimeLog(`[EXPO] Engaging A.U.R.A runtime (${reason})...`)
      try {
        setBootPhase('audio')
        updateBootLine('audio', 'active')
        logBoot('boot-audio-start', '[BOOT] audio_unlock_started')
        await runtime.unlockAudio()
        updateBootLine('audio', 'ok')
        logBoot('boot-audio-ready', '[BOOT] audio_unlock_ready')

        setBootPhase('mic')
        updateBootLine('mic', 'active')
        logBoot('boot-mic-start', '[BOOT] mic_start_requested')
        const micOk = await runtime.startContinuousListening()
        if (!micOk) {
          updateBootLine('mic', 'blocked', runtime.lastMicStartError || runtime.micPermissionState)
          setBootPhase('permission_needed')
          setActivationRequired(true)
          setLastActivationError(runtime.lastMicStartError || 'Microphone permission required')
          setBootErrors((current) => Array.from(new Set([...current, 'MICROPHONE PERMISSION REQUIRED'])))
          addRuntimeLog('[BOOT] permission_needed microphone')
          addRuntimeLog('[VOICE] mic_recovery_required')
          return
        }
        updateBootLine('mic', 'ok')
        logBoot('boot-mic-active', '[BOOT] mic_active')

        setBootPhase('camera')
        updateBootLine('camera', 'active')
        logBoot('boot-camera-start', '[BOOT] camera_start_requested')
        const camRes = await vision.startCamera('boot_screen')
        if (!camRes.ok) {
          updateBootLine('camera', 'blocked', camRes.error || 'permission_required')
          setBootPhase('permission_needed')
          setActivationRequired(true)
          setLastActivationError(camRes.error || 'Camera permission required')
          setBootErrors((current) => Array.from(new Set([...current, 'CAMERA PERMISSION REQUIRED'])))
          addRuntimeLog('[BOOT] permission_needed camera')
          return
        }
        updateBootLine('camera', 'ok')
        logBoot('boot-camera-active', '[BOOT] camera_active')

        setBootPhase('tracking')
        updateBootLine('tracking', 'active')
        if (PEOPLE_TRACKING_BOOT_START_DELAY_MS > 0) {
          logBoot('boot-tracking-wait-video', `[BOOT] people_tracking_wait_video_ready delay_ms=${PEOPLE_TRACKING_BOOT_START_DELAY_MS}`)
          await new Promise((resolve) => window.setTimeout(resolve, PEOPLE_TRACKING_BOOT_START_DELAY_MS))
        }
        const trackRes = await vision.startTracking('boot_screen')
        if (!trackRes.ok) {
          updateBootLine('tracking', 'failed', trackRes.error || 'tracking_failed')
          setBootPhase('failed')
          setActivationRequired(true)
          setLastActivationError(trackRes.error || 'Tracking failed')
          return
        }
        updateBootLine('tracking', 'ok')
        logBoot('boot-tracking-active', '[BOOT] tracking_active')

        setBootPhase('identity')
        updateBootLine('identity', 'active')
        const identityRes = await vision.verifyOwnerIfAvailable()
        updateBootLine('identity', identityRes.ok ? 'ok' : 'failed', identityRes.skipped ? 'no enrolled face required' : identityRes.error)
        if (identityRes.ok && !identityRes.skipped) logBoot('boot-owner-verified', '[BOOT] owner_verified')

        setBootPhase('ready')
        updateBootLine('runtime', 'ok')
        updateBootLine('online', 'ok')
        setRuntimeActivated(true)
        setActivationRequired(false)
        setLastActivationError(null)
        logBoot('boot-runtime-ready', '[BOOT] runtime_ready')
        addRuntimeLog('[SYSTEM] A.U.R.A runtime active')
      } catch (err: any) {
        setRuntimeActivated(false)
        setActivationRequired(true)
        setBootPhase('failed')
        const msg = String(err?.message || err || 'Permission required')
        setLastActivationError(msg)
        setBootErrors((current) => Array.from(new Set([...current, msg])))
        addRuntimeLog(`[SYSTEM] Activation required: ${msg}`)
      } finally {
        runtimeActivationPromiseRef.current = null
      }
    })()
    return runtimeActivationPromiseRef.current
  }

  useEffect(() => {
    if (runtime.recognitionRunning && runtime.continuousMicActive && bootPhase === 'permission_needed' && lastActivationError?.toLowerCase().includes('mic')) {
      updateBootLine('mic', 'ok')
      setBootErrors((current) => current.filter((entry) => !entry.toLowerCase().includes('microphone')))
      setActivationRequired(false)
      setLastActivationError(null)
      addRuntimeLog('[BOOT] microphone_permission_recovered')
      addRuntimeLog('[BOOT] mic_active')
      activateAuraExpoRuntime('mic_permission_recovered')
    }
  }, [runtime.recognitionRunning, runtime.continuousMicActive, bootPhase, lastActivationError])

  useEffect(() => {
    if (isExpoMode) {
      setOrbBrightness(1.0)
      activateAuraExpoRuntime('boot_screen')
    }
  }, [isExpoMode])

  const speaking = socketSpeaking || runtime.isAuraSpeaking
  const recording = runtime.continuousMicRequested && runtime.continuousMicActive && runtime.recognitionRunning
  const listening = recording && (socketListening || runtime.listening)
  const thinking = runtime.lastStage === 'thinking' || (turnActive && !speaking)
  const [responseVisualHold, setResponseVisualHold] = useState(false)
  useEffect(() => {
    if (!runtime.lastResponseText) return
    setResponseVisualHold(true)
    const timer = window.setTimeout(() => setResponseVisualHold(false), 1200)
    return () => window.clearTimeout(timer)
  }, [runtime.lastResponseText])
  const isAuraRespondingVisual = runtime.voiceTurnInFlight || speaking || responseVisualHold
  const orbReactiveState = speaking ? 'speaking' : (runtime.voiceTurnInFlight || thinking ? 'thinking' : 'idle')
  const visionStatusLabel = (status: string) => status ? status.charAt(0).toUpperCase() + status.slice(1) : 'Unknown'

  const lastTurn = transcript.length ? transcript[transcript.length - 1] : null
  const lastMemoryTurn = runtime.conversationTurns.at(-1)
  const lastUser = runtime.lastNormalizedTranscript || lastTurn?.text || ''
  const rawUser = lastTurn?.rawText && lastTurn.rawText !== lastTurn.text ? lastTurn.rawText : ''
  const userCaption = recording || listening ? 'listening…' : cleanCaption(lastUser, 140)
  const assistantText = runtime.lastResponseText || responseLive || (responseTurns.length ? responseTurns[responseTurns.length - 1].text : '') || lastMemoryTurn?.aura_response || ''
  const assistantCaption = cleanCaption(assistantText, 320)

  const submit = () => {
    const t = text.trim()
    if (!t) return
    runtime.sendTextTurn(t)
    setText('')
  }

  const focusTextInteraction = (source: 'shortcut' | 'button' = 'button') => {
    setShowInput(true)
    window.setTimeout(() => {
      textInputRef.current?.focus()
    }, 0)
    if (source === 'shortcut') {
      addRuntimeLog('[UI] text_interaction_focus shortcut=T')
    }
  }

  const activateMicFromButton = () => {
    addRuntimeLog('[AURA ACTION] mic.start requested ui_activate_button')
    runtime.startContinuousListening().catch(() => undefined)
  }

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.repeat) {
        e.preventDefault()
        addRuntimeLog('[UI] stop_speaking_hotkey shortcut=Escape')
        runtime.stopSpeakingNow()
        return
      }
      const target = e.target as HTMLElement | null
      const tagName = target?.tagName?.toLowerCase()
      const isTypingTarget = tagName === 'input' || tagName === 'textarea' || tagName === 'select' || Boolean(target?.isContentEditable)
      if (isTypingTarget || e.ctrlKey || e.altKey || e.metaKey) return
      const key = e.key.toLowerCase()
      if (key === 'm') {
        if (e.repeat || e.isComposing) return
        e.preventDefault()
        runtime.toggleMic('hotkey')
        return
      }
      if (key === 't') {
        e.preventDefault()
        focusTextInteraction('shortcut')
        return
      }
      if (key === 'g') {
        addRuntimeLog('[UI] gesture_control_shortcut_triggered key=G')
        return
      }
      if (key === 'p') {
        e.preventDefault()
        addRuntimeLog('[UI] expo_guest_greeting_shortcut_triggered key=P')
        runtime.sendTextTurn('start guest greeting', undefined, undefined, {
          frontendKind: 'expo_guest_greeting',
          timeoutMs: 60000,
          expoGuestGreetingShortcut: true,
          shortcut: 'P',
        })
        return
      }
      if (key !== 'i') return
      e.preventDefault()
      addRuntimeLog('[UI] expo_intro_shortcut_triggered key=I')
      runtime.sendTextTurn('start expo intro', undefined, undefined, {
        frontendKind: 'expo_intro',
        timeoutMs: 60000,
        expoShortcut: true,
        shortcut: 'I',
      })
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [runtime.sendTextTurn, runtime.toggleMic, runtime.stopSpeakingNow])

  useEffect(() => {
    const onOpenPanel = (e: Event) => {
      const detail = (e as CustomEvent).detail as { panel?: string }
      if (detail?.panel === 'logs' || detail?.panel === 'runtime' || detail?.panel === 'activity') {
        setRuntimeConsoleOpen(true)
      }
    }
    window.addEventListener('jarvis-open-panel', onOpenPanel)
    return () => window.removeEventListener('jarvis-open-panel', onOpenPanel)
  }, [])

  useEffect(() => {
    if (isSpeakingTextSynced && activeSpeakingSegmentIndex >= 0) {
      activeSegmentRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [activeSpeakingSegmentIndex, isSpeakingTextSynced])

  useEffect(() => {
    if (!isSpeakingTextSynced) return
    const container = speakingTextContainerRef.current
    if (!container) return
    const handleScroll = () => {
      handleUserScrollPause()
    }
    container.addEventListener('scroll', handleScroll, { passive: true })
    return () => container.removeEventListener('scroll', handleScroll)
  }, [isSpeakingTextSynced, handleUserScrollPause])

  const panels: RadialPanel[] = [
    { id: 'projects', label: 'projects', icon: 'projects' },
    { id: 'tasks', label: 'tasks', icon: 'tasks' },
    { id: 'subagents', label: 'subagents', icon: 'brain' },
    { id: 'logs', label: 'logs', icon: 'activity' },
    { id: 'settings', label: 'settings', icon: 'settings' },
  ]

  const showMicRecoveryPill =
    runtimeActivated &&
    (runtime.continuousMicRequested || isExpoMode) &&
    !runtime.voiceTurnInFlight &&
    !runtime.isAuraSpeaking &&
    (!runtime.continuousMicActive || !runtime.recognitionRunning || runtime.micStartupState === 'failed' || runtime.micStartupState === 'permission_denied')

  return (
    <div className="relative h-full w-full overflow-hidden bg-black">
      {isExpoMode && !runtimeActivated && (
        <AuraBootScreen
          phase={bootPhase}
          lines={bootLines}
          errors={bootErrors}
          needsAuthorization={activationRequired || bootPhase === 'permission_needed' || bootPhase === 'failed'}
          onAuthorize={() => activateAuraExpoRuntime('boot_authorize_click')}
        />
      )}
      {/* Orb — centered stage, 100% brightness default */}
      <div className="aura-orb-stage z-5 pointer-events-none">
        <div
          className={cn('aura-orb-interactive pointer-events-auto h-full w-full', isAuraRespondingVisual && 'aura-orb-responding')}
          data-orb-reactive-state={orbReactiveState}
          style={{ filter: `brightness(${orbBrightness})` }}
          onPointerDown={(e) => {
            if (new URLSearchParams(window.location.search).get('debug') === '1') {
              console.log('[AURA UI] orb pointer down', (e.target as HTMLElement).className)
            }
          }}
          onClick={(e) => {
            if (new URLSearchParams(window.location.search).get('debug') === '1') {
              console.log('[AURA UI] orb click', (e.target as HTMLElement).className)
            }
          }}
        >
          <OrbCanvas />
        </div>
      </div>
      <div
        className="pointer-events-none absolute inset-0 z-6"
        style={{ background: 'radial-gradient(circle at 50% 50%, transparent 45%, rgba(0,0,0,0.82) 100%)' }}
      />

      {/* Part 12 Debug Overlay */}
      {searchParams.get('debug') === '1' && (
        <div className="pointer-events-none fixed top-4 right-4 z-50 rounded-lg border border-[var(--blue)]/40 bg-black/85 p-3 font-mono text-[10px] text-[var(--blue)] backdrop-blur-md space-y-1">
          <div className="font-bold uppercase tracking-widest text-[var(--text)] border-b border-[var(--blue)]/30 pb-1">MIC DEBUG INFO</div>
          <div>micStartupState: <span className="text-white">{runtime.micStartupState}</span></div>
          <div>recognitionRunning: <span className="text-white">{String(runtime.recognitionRunning)}</span></div>
          <div>continuousMicRequested: <span className="text-white">{String(runtime.continuousMicRequested)}</span></div>
          <div>continuousMicActive: <span className="text-white">{String(runtime.continuousMicActive)}</span></div>
          <div>micPermissionState: <span className="text-white">{runtime.micPermissionState}</span></div>
          <div>retryCount: <span className="text-white">{runtime.retryCount}</span></div>
          <div>bootPhase: <span className="text-white">{bootPhase}</span></div>
          <div>audioUnlocked: <span className="text-white">{String(runtime.audioUnlocked)}</span></div>
          <div>cameraStatus: <span className="text-white">{vision.cameraStatus}</span></div>
          <div>peopleTrackingStatus: <span className="text-white">{vision.trackingStatus}</span></div>
          <div>restartPending: <span className="text-white">{String(runtime.restartPending)}</span></div>
          <div>OpenRouter: <span className="text-white">{providerStatus?.brain?.health?.openrouter?.status || 'unknown'}</span></div>
          <div>Groq: <span className="text-white">{providerStatus?.brain?.health?.groq?.status || 'unknown'}</span></div>
          <div>Email: <span className="text-white">{capabilities?.email?.configured ? 'connected' : 'not connected'}</span></div>
          <div>Cloud: <span className="text-white">{capabilities?.cloud_storage?.configured ? 'connected' : 'not connected'}</span></div>
          {bootErrors.length > 0 && <div className="text-[var(--amber)]">bootErrors: {bootErrors.join(', ')}</div>}
          {runtime.lastMicStartError && <div className="text-[var(--amber)]">lastError: {runtime.lastMicStartError}</div>}
        </div>
      )}

      {/* Activation Overlay for browser permission gesture */}
      {isExpoMode && !runtimeActivated && activationRequired && (
        <div className="pointer-events-auto absolute inset-0 z-50 flex flex-col items-center justify-center bg-black/85 backdrop-blur-md">
          <div className="flex max-w-md flex-col items-center gap-4 text-center px-8 py-8 rounded-2xl border border-[var(--blue)] bg-[#080d14]/90 shadow-[0_0_50px_rgba(0,200,255,0.25)] font-mono">
            <div className="h-14 w-14 rounded-full border border-[var(--blue)] flex items-center justify-center text-[var(--blue)] shadow-[0_0_20px_var(--blue-glow)] bg-[rgba(0,200,255,0.1)]">
              <Activity size={26} />
            </div>
            <h2 className="text-xl uppercase tracking-[0.3em] text-[var(--text)] font-semibold">Activate A.U.R.A</h2>
            <p className="text-[11px] leading-relaxed text-[var(--text-dim)]">
              Camera, microphone, and voice output need browser permission to engage spatial reasoning and continuous mic.
            </p>
            {lastActivationError && (
              <div className="text-[10px] text-[var(--amber)] bg-[rgba(255,180,0,0.1)] px-3 py-1 rounded border border-[var(--amber)]/30">
                {lastActivationError}
              </div>
            )}
            <button
              type="button"
              onClick={() => activateAuraExpoRuntime('user_click')}
              className="mt-2 rounded-full border border-[var(--blue)] bg-[rgba(0,200,255,0.18)] px-8 py-3 font-mono text-[12px] uppercase tracking-[0.25em] text-[var(--blue)] shadow-[0_0_25px_var(--blue-glow)] transition hover:bg-[rgba(0,200,255,0.35)] hover:scale-105"
            >
              Activate A.U.R.A
            </button>
          </div>
        </div>
      )}

      {/* Top-left: name + status */}
      <div className="pointer-events-none absolute left-5 top-4 z-10 flex items-center gap-2.5">
        <span
          className="h-2 w-2 rounded-full"
          style={{
            background: connected ? 'var(--blue)' : 'var(--red)',
            boxShadow: connected ? '0 0 10px var(--blue-glow)' : '0 0 8px var(--red-glow)',
          }}
        />
        <span className="font-mono text-[11px] uppercase tracking-[0.34em] text-[var(--text-dim)]">A.U.R.A</span>
        <WakeIndicator
          listening={listening || recording || runtime.auraAwake}
          speaking={speaking}
          thinking={thinking}
          awake={runtime.auraAwake}
          activationRequired={isExpoMode && !runtimeActivated && activationRequired}
        />
      </div>

      {/* Left rail: project + system stats + activity + runtime logs */}
      <div className="aura-left-rail">
        <ErrorBoundary compact label="project">
          <ProjectContextWidget onClick={() => window.dispatchEvent(new CustomEvent('jarvis-open-panel', { detail: { panel: 'projects' } }))} />
        </ErrorBoundary>
        <ErrorBoundary compact label="system"><SystemStatsWidget /></ErrorBoundary>
        <ErrorBoundary compact label="activity"><ActivityWidget /></ErrorBoundary>
        <ErrorBoundary compact label="runtime"><RuntimeLogsWidget logs={runtimeLogs} onExpand={() => setRuntimeConsoleOpen(true)} /></ErrorBoundary>
      </div>

      {/* Right rail: clock + weather + voice + vision status cards + radial actions */}
      <div className="aura-right-rail">
        <ErrorBoundary compact label="clock"><ClockWidget /></ErrorBoundary>
        <ErrorBoundary compact label="weather"><WeatherWidget /></ErrorBoundary>
        <div className="aura-right-card font-mono">
          <div className="text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">voice</div>
          <div className="aura-right-card-row mt-1 text-[10px] text-[var(--text-dim)]">
            Continuous Mic: {runtime.continuousMicActive && runtime.recognitionRunning ? 'Active' : runtime.micStartupState === 'permission_denied' ? 'Permission Needed' : runtime.micStartupState === 'failed' ? 'Failed' : runtime.micStartupState === 'starting_recognition' || runtime.micStartupState === 'unlocking_audio' ? 'Starting...' : 'Off'}
          </div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">
            Recognition: {runtime.recognitionRunning ? 'running' : runtime.micStartupState === 'retrying' ? 'restarting' : runtime.micStartupState === 'failed' || runtime.micStartupState === 'permission_denied' ? 'error' : 'stopped'}
          </div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">
            Restart pending: {runtime.restartPending ? 'yes' : 'no'}
          </div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">
            Mic Permission: {runtime.micPermissionState}
          </div>
          {runtime.lastMicStartError && (
            <div className="aura-right-card-row text-[9.5px] text-[var(--red)]" title={runtime.lastMicStartError}>
              Last mic error: {runtime.lastMicStartError}
            </div>
          )}
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">State: {runtime.auraAwake ? 'awake' : 'standby'}</div>
          {runtime.lastRawTranscript && (
            <div className="aura-right-card-row text-[9.5px] text-[var(--amber)] truncate" title={runtime.lastRawTranscript}>
              Last input: {runtime.lastRawTranscript}
            </div>
          )}
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Provider: {runtime.lastProvider}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">TTS provider: {runtime.lastTtsProvider}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Playback: {runtime.lastPlayback}</div>
          {runtime.lastLatency && (
            <div className="aura-right-card-row text-[10px] text-[var(--blue)]" title={`STT->send:${runtime.lastLatency.stt_final_ms}ms, backend:${runtime.lastLatency.backend_ms}ms, tts:${runtime.lastLatency.tts_ms}ms`}>
              Latency: {runtime.lastLatency.total_ms}ms (backend: {runtime.lastLatency.backend_ms}ms)
            </div>
          )}
          {runtime.lastActionStatus && (
            <div className="aura-right-card-row text-[10px] text-[var(--amber)]">
              Action: {runtime.lastActionStatus}
            </div>
          )}
          {lastActionDebug && (
            <div className="aura-right-card-row text-[9.5px] text-[var(--blue)]" title={`Duration: ${lastActionDebug.last_action_duration_ms}ms, Camera: ${lastActionDebug.camera_status_after_action}, People Tracking: ${lastActionDebug.tracking_status_after_action}, Addr: ${lastActionDebug.address_used_for_action}`}>
              Last Action: {lastActionDebug.last_action_type} [{lastActionDebug.last_action_status}] ({lastActionDebug.address_used_for_action})
            </div>
          )}
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Memory: {runtime.conversationTurns.length} turns</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Persistent Memory: {runtime.persistentMemoryActive ? runtime.persistentMemoryBackend : 'off'}</div>
          {searchParams.get('debug') === '1' && (
            <>
              <div className="aura-right-card-row text-[10px] text-[var(--blue)]">OpenRouter: {providerStatus?.brain?.health?.openrouter?.status || 'unknown'}</div>
              <div className="aura-right-card-row text-[10px] text-[var(--blue)]">Groq: {providerStatus?.brain?.health?.groq?.status || 'unknown'}</div>
              <div className="aura-right-card-row text-[10px] text-[var(--amber)]">Email: {capabilities?.email?.configured ? 'connected' : 'not connected'}</div>
              <div className="aura-right-card-row text-[10px] text-[var(--amber)]">Cloud: {capabilities?.cloud_storage?.configured ? 'connected' : 'not connected'}</div>
            </>
          )}
        </div>
        <div className="aura-right-card font-mono">
          <div className="text-[9.5px] uppercase tracking-[0.18em] text-[var(--text-faint)]">vision</div>
          <div className="aura-right-card-row mt-1 text-[10px] text-[var(--text-dim)]">Camera: {activationRequired && vision.cameraStatus === 'off' ? 'Permission Needed' : visionStatusLabel(vision.cameraStatus)}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Gesture Tracking: {visionStatusLabel(gestureTrackingState)}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">People Tracking: {visionStatusLabel(vision.trackingStatus)}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Backend: {vision.peopleTrackingRuntime?.backend || vision.detectorSource || 'none'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">MediaPipe Package: {vision.peopleTrackingRuntime?.package_available ? 'yes' : 'no'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">MediaPipe Import: {vision.peopleTrackingRuntime?.import_ok ? 'ok' : 'pending'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">WASM Resolver: {vision.peopleTrackingRuntime?.wasm_resolver_ok ? 'ok' : 'not ready'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Model Asset: {vision.peopleTrackingRuntime?.model_asset_reachable ? 'reachable' : 'not reachable'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Detector Init: {vision.peopleTrackingRuntime?.detector_init_ok ? 'ok' : 'not ready'}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Video: {vision.visionContext.video_ready ? 'ready' : 'not ready'} {vision.visionContext.video_width || 0}x{vision.visionContext.video_height || 0}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Loop: {vision.visionContext.loop_running ? 'running' : 'stopped'}</div>
          <div className="aura-right-card-row text-[9.5px] text-[var(--amber)]">Last Error: {vision.trackingError?.code || vision.peopleTrackingRuntime?.error_code || 'none'}</div>
          {vision.trackingError?.stage && <div className="aura-right-card-row text-[9.5px] text-[var(--text-faint)]">Stage: {vision.trackingError.stage}</div>}
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">Owner: {vision.ownerStatus}</div>
          <div className="aura-right-card-row text-[10px] text-[var(--text-dim)]">People: {vision.peopleCount}</div>
        </div>
        <div className="aura-right-actions">
          <div className="aura-radial-menu">
            <RadialMenu panels={panels} />
          </div>
        </div>
      </div>

      {showMiniPreview && (
        <MiniCameraPreview
          cameraOn={vision.cameraOn}
          tracking={vision.tracking}
          peopleCount={vision.peopleCount}
          cameraStatus={vision.cameraStatus}
          trackingStatus={vision.trackingStatus}
          ownerStatus={vision.ownerStatus}
          tracks={vision.tracks}
          collapsed={miniPreviewCollapsed}
          onToggleCollapse={() => setMiniPreviewCollapsed((value) => !value)}
          attachPreviewVideoElement={vision.attachPreviewVideoElement}
          onStartCamera={() => { vision.startCamera('mini_preview') }}
          onStopCamera={() => { vision.stopCamera('mini_preview') }}
        />
      )}

      {/* Task plan widget (lower left, when background task active) */}
      {task && (
        <div className="pointer-events-none fixed left-6 bottom-28 z-20">
          <ErrorBoundary compact label="task"><TaskOrb plan={task} /></ErrorBoundary>
        </div>
      )}

      {/* User caption (upper-center, below clock) */}
      <div className="pointer-events-none absolute inset-x-0 top-[18%] z-10 flex justify-center px-8">
        <div
          className={cn(
            'max-w-2xl text-center font-mono text-[13px] leading-relaxed tracking-wide transition-opacity duration-300',
            recording || listening ? 'text-[var(--amber)] opacity-90' : 'text-[var(--text-dim)] opacity-70',
          )}
        >
          {rawUser ? <span className="opacity-60">voice · {rawUser} → </span> : userCaption && <span className="opacity-60">you · </span>}
          {userCaption}
        </div>
      </div>

      {/* Main Orb bottom console: prevents mic/composer/response overlap */}
      <div className="aura-bottom-console">
        <div className="aura-response-line" aria-live="polite">
          <div
            className="aura-response-state"
            style={{
              color: speaking ? 'var(--blue)' : 'var(--text-faint)',
              textShadow: speaking ? '0 0 14px var(--blue-glow)' : 'none',
            }}
          >
            {runtime.auraAwake ? stateWord(speaking, recording || listening, thinking).replace('ready', 'awake') : stateWord(speaking, recording || listening, thinking).replace('ready', 'standby')}
          </div>
          {isSpeakingTextSynced && speakingSegments.length > 0 ? (
            <div
              ref={speakingTextContainerRef}
              className="aura-speaking-text-area"
              style={{ maxHeight: 180, overflowY: 'auto', scrollBehavior: 'smooth' }}
            >
              {speakingSegments.map((segment, index) => (
                <div
                  key={index}
                  ref={index === activeSpeakingSegmentIndex ? activeSegmentRef : null}
                  className={index === activeSpeakingSegmentIndex ? 'aura-speaking-segment-active' : 'aura-speaking-segment'}
                  style={{
                    color: index === activeSpeakingSegmentIndex ? 'var(--text)' : 'var(--text-dim)',
                    textShadow: index === activeSpeakingSegmentIndex ? '0 0 18px rgba(0,200,255,0.25)' : 'none',
                    fontWeight: index === activeSpeakingSegmentIndex ? 600 : 400,
                    opacity: index === activeSpeakingSegmentIndex ? 1 : 0.5,
                    transition: 'all 0.3s ease',
                    lineHeight: 1.45,
                    fontSize: 15,
                    fontFamily: 'var(--mono)',
                    textAlign: 'center' as const,
                  }}
                >
                  {segment}
                </div>
              ))}
              {responseLive && <span className="crt-cursor ml-0.5">▍</span>}
            </div>
          ) : (
            <div
              className={cn('aura-response-text transition-opacity duration-300', assistantCaption ? 'opacity-100' : 'opacity-0')}
              style={{ color: 'var(--text)', textShadow: '0 0 18px rgba(0,200,255,0.18)' }}
            >
              {assistantCaption}
              {responseLive && <span className="crt-cursor ml-0.5">▍</span>}
            </div>
          )}
        </div>

        {showMicRecoveryPill && (
          <div className="aura-mic-recovery-row">
            <button
              type="button"
              disabled={runtime.voiceTurnInFlight || runtime.isAuraSpeaking}
              onClick={() => runtime.recoverMicIfIdle('mic_recovery_click').then((ok) => {
                if (!ok && !runtime.voiceTurnInFlight && !runtime.isAuraSpeaking) activateAuraExpoRuntime('mic_recovery_click')
              })}
              className="flex items-center gap-2 rounded-full border border-[var(--amber)] bg-[#120a02]/90 px-6 py-2.5 text-[12px] font-bold uppercase tracking-[0.2em] text-[var(--amber)] shadow-[0_0_20px_var(--amber-glow)] transition hover:bg-[rgba(255,140,0,0.3)] hover:scale-105"
            >
              <Mic size={15} className="animate-pulse" />
              Activate A.U.R.A Mic
            </button>
            {runtime.micPermissionState === 'denied' ? (
              <span className="aura-mic-recovery-message text-[var(--red)]">
                Microphone permission blocked. Allow mic access and click Activate A.U.R.A Mic.
              </span>
            ) : runtime.lastMicStartError ? (
              <span className="aura-mic-recovery-message text-[var(--amber)]">
                Status: {runtime.micStartupState} ({runtime.lastMicStartError})
              </span>
            ) : null}
          </div>
        )}

        <div className="aura-main-mic-row">
          {speaking && (
            <button type="button" aria-label="Stop speaking" title="Stop speaking (Escape)"
              onClick={() => {
                addRuntimeLog('[UI] stop_speaking_clicked')
                runtime.stopSpeakingNow()
              }}
              className="flex h-10 w-10 items-center justify-center rounded border border-[var(--line-bright)] text-[var(--text)]">
              <Square size={18} />
            </button>
          )}
          <button
            type="button"
            onClick={() => runtime.toggleMic('click')}
            title={runtime.continuousMicRequested ? 'Turn microphone off (M)' : 'Turn microphone on (M)'}
            aria-label={recording ? 'Microphone listening; turn off' : runtime.continuousMicRequested ? 'Microphone enabled, waiting; turn off' : 'Microphone off; turn on'}
            aria-pressed={runtime.continuousMicRequested}
            className={cn(
              'relative flex h-16 w-16 items-center justify-center rounded-full border transition',
              recording
                ? 'border-[var(--amber)] text-[var(--amber)] shadow-[0_0_28px_var(--amber-glow)]'
                : 'border-[var(--blue)] text-[var(--blue)] hover:shadow-[0_0_22px_var(--blue-glow)]',
            )}
            style={{ background: 'rgba(0,0,0,0.35)', backdropFilter: 'blur(4px)' }}
          >
            <Mic size={22} />
            {recording && (
              <span className="absolute inset-0 rounded-full border border-[var(--amber)]" style={{ animation: 'orb-ping 1.1s ease-out infinite' }} />
            )}
          </button>
        </div>

        <div className="aura-bottom-composer">
          {!runtime.continuousMicActive && !showMicRecoveryPill && (
            <div className="aura-mic-activate-row">
              <button
                type="button"
                onClick={activateMicFromButton}
                className="aura-activate-mic-button"
                aria-label="Activate A.U.R.A microphone"
                title="Activate microphone"
              >
                ACTIVATE MIC
              </button>
            </div>
          )}

          {showInput && (
            <div className="aura-text-composer-row">
              <input
                ref={textInputRef}
                value={text}
                autoFocus
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    submit()
                  } else if (e.key === 'Escape' && !text.trim()) {
                    e.preventDefault()
                    setShowInput(false)
                    textInputRef.current?.blur()
                  }
                }}
                placeholder="type a message..."
                className="aura-text-input"
                aria-label="Type a message to A.U.R.A"
              />
              <button
                type="button"
                onClick={submit}
                className="rounded-full border border-[var(--blue)] p-2 text-[var(--blue)] transition hover:bg-[rgba(0,200,255,0.12)]"
                aria-label="Send message"
                title="Send message"
              >
                <Send size={15} />
              </button>
            </div>
          )}
        </div>

        <div className="aura-secondary-controls">
          <button
            type="button"
            onClick={() => showInput ? setShowInput(false) : focusTextInteraction('button')}
            title="Type instead"
            className="font-mono text-[11px] tracking-[0.2em] text-[var(--text-faint)] transition hover:text-[var(--text-dim)]"
          >
            {showInput ? 'hide' : 'type'}
          </button>

          {!hideControls && (
            <>
              <button
                type="button"
                onClick={() => setMode(mode === 'wwf' ? 'default' : 'wwf')}
                title={mode === 'wwf' ? 'Switch to normal mode' : 'Switch to WWF work mode'}
                className={cn(
                  'rounded-full border bg-black/40 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.2em] backdrop-blur transition',
                  mode === 'wwf'
                    ? 'border-[var(--amber)] text-[var(--amber)] shadow-[0_0_12px_var(--amber-glow)]'
                    : 'border-[var(--line-bright)] text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]',
                )}
              >
                {mode === 'wwf' ? 'wwf' : 'normal'}
              </button>

              <button
                type="button"
                onClick={() => runtime.toggleMic('click')}
                className={cn(
                  'rounded-full border bg-black/40 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.2em] backdrop-blur transition',
                  runtime.continuousMicActive
                    ? 'border-[var(--amber)] text-[var(--amber)] shadow-[0_0_12px_var(--amber-glow)]'
                    : 'border-[var(--line-bright)] text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]',
                )}
              >
                Continuous Mic: {runtime.continuousMicActive ? 'On' : 'Off'}
              </button>

              <button
                type="button"
                onClick={() => vision.cameraOn ? vision.stopCamera() : vision.startCamera()}
                className="rounded-full border border-[var(--line-bright)] bg-black/40 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--text-dim)] backdrop-blur transition hover:border-[var(--blue)] hover:text-[var(--blue)]"
              >
                Vision: {vision.cameraOn ? 'On' : 'Off'}
              </button>

              <button
                type="button"
                onClick={() => vision.tracking ? vision.stopTracking() : vision.startTracking()}
                className="rounded-full border border-[var(--line-bright)] bg-black/40 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--text-dim)] backdrop-blur transition hover:border-[var(--blue)] hover:text-[var(--blue)]"
              >
                People Tracking: {vision.tracking ? 'On' : 'Off'}
              </button>

              {/* Model selector */}
              <select
                value={activeModel}
                onChange={(e) => useJarvisStore.getState().send({ type: 'set_model', model: e.target.value })}
                className="rounded-full border border-[var(--line-bright)] bg-black/40 px-2 py-1 font-mono text-[10px] tracking-[0.12em] text-[var(--text-dim)] outline-none backdrop-blur hover:border-[var(--blue)] hover:text-[var(--blue)]"
              >
                {models.map((m) => (
                  <option key={m} value={m} className="bg-[#0d1218] text-[var(--text)]">
                    {modelLabel(m)}
                  </option>
                ))}
              </select>
            </>
          )}

          <button
            type="button"
            onClick={stop}
            title="Interrupt"
            disabled={!turnActive && !speaking}
            className={cn(
              'flex items-center gap-1.5 font-mono text-[11px] tracking-[0.2em] transition',
              turnActive || speaking ? 'text-[var(--red)] opacity-100 hover:opacity-80' : 'text-[var(--text-faint)] opacity-40',
            )}
          >
            <Square size={11} />
            stop
          </button>
        </div>

        {!hideControls && (
          <div className="flex items-center gap-2 font-mono text-[9.5px] uppercase tracking-[0.16em] text-[var(--text-faint)]">
            <Minus size={9} />
            <input
              type="range"
              min="0.25"
              max="1.0"
              step="0.05"
              value={orbBrightness}
              onChange={(e) => setOrbBrightness(Number(e.target.value))}
              className="w-24 accent-[var(--blue)]"
              title="Orb brightness"
            />
            <Plus size={9} />
            <span className="w-8 tabular-nums text-[var(--text-dim)]">
              {(orbBrightness * 100).toFixed(0)}%
            </span>
          </div>
        )}

        <div className="aura-wakeup-hint">
          say "Wakeup" · <a className="pointer-events-auto hover:text-[var(--blue)]" href="/diagnostics">diagnostics</a>
        </div>
      </div>
      <video ref={vision.videoRef} className="pointer-events-none absolute bottom-4 right-4 h-1 w-1 opacity-0" muted playsInline />
      <RuntimeConsoleModal
        open={runtimeConsoleOpen}
        onClose={() => setRuntimeConsoleOpen(false)}
        logs={runtimeLogs}
        onClearSession={() => setRuntimeLogs([])}
        onLog={addRuntimeLog}
      />
    </div>
  )
}
