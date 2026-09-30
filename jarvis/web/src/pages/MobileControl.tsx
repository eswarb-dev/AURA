import { useEffect, useMemo, useState } from 'react'
import {
  Brain, ChevronDown, CloudSun, Eye, Laptop, Mic, Search, Send, ShieldCheck,
  Sparkles, Square, Trash2, Volume2, Zap,
} from 'lucide-react'
import { getRemoteControlStatus, sendRemoteControlCommand, type RemoteControlStatus } from '@/lib/remoteControlApi'

const TOKEN_STORAGE_KEY = 'aura_remote_control_token'

type DemoCommand = {
  label: string
  text: string
  frontendKind: string
  shortcut?: string
}

type DemoSection = {
  title: string
  icon: typeof Zap
  commands: DemoCommand[]
}

const sections: DemoSection[] = [
  { title: 'Start / Greeting', icon: Zap, commands: [
    { label: 'Wake up', text: 'wakeup', frontendKind: 'wake' },
    { label: 'Hello A.U.R.A', text: 'hello aura', frontendKind: 'wake' },
  ] },
  { title: 'About A.U.R.A', icon: Sparkles, commands: [
    { label: 'Introduce yourself', text: 'introduce yourself', frontendKind: 'expo_demo' },
    { label: 'Tell me about yourself', text: 'tell me about yourself', frontendKind: 'expo_demo' },
    { label: 'Who are you?', text: 'who are you', frontendKind: 'expo_demo' },
  ] },
  { title: 'Capabilities', icon: Brain, commands: [
    { label: 'What can you do?', text: 'what can you do', frontendKind: 'expo_demo' },
    { label: 'Explain your features', text: 'explain your features', frontendKind: 'expo_demo' },
    { label: 'Show your capabilities', text: 'show your capabilities', frontendKind: 'expo_demo' },
  ] },
  { title: 'Project Explanation', icon: Laptop, commands: [
    { label: 'Tell me about this project', text: 'tell me about this project', frontendKind: 'expo_demo' },
    { label: 'Explain this project', text: 'explain this project', frontendKind: 'expo_demo' },
    { label: 'Why was A.U.R.A built?', text: 'why was aura built', frontendKind: 'expo_demo' },
  ] },
  { title: 'Difference from Chatbot', icon: ShieldCheck, commands: [
    { label: 'What makes A.U.R.A different?', text: 'what makes aura different from a normal chatbot', frontendKind: 'expo_demo' },
    { label: 'Different from ChatGPT?', text: 'how are you different from chatgpt', frontendKind: 'expo_demo' },
    { label: 'Not just a chatbot?', text: 'why is this not just a chatbot', frontendKind: 'expo_demo' },
  ] },
  { title: 'Student Usefulness', icon: Brain, commands: [
    { label: 'How does A.U.R.A help students?', text: 'how does aura help students', frontendKind: 'expo_demo' },
    { label: 'How can students use this?', text: 'how can students use this', frontendKind: 'expo_demo' },
    { label: 'How is this useful?', text: 'how is this useful', frontendKind: 'expo_demo' },
  ] },
  { title: 'Expo / Engineers Week', icon: Sparkles, commands: [
    { label: 'Start Expo Intro', text: 'start expo intro', frontendKind: 'expo_intro', shortcut: 'I' },
    { label: 'Principal / Trustee Greeting', text: 'start guest greeting', frontendKind: 'expo_guest_greeting', shortcut: 'P' },
    { label: 'Engineers Week Intro', text: 'engineers week intro', frontendKind: 'expo_intro' },
    { label: "Tell us about Engineers' Week", text: 'tell us about engineers week', frontendKind: 'expo_demo' },
    { label: 'What is this expo about?', text: 'what is this expo about', frontendKind: 'expo_demo' },
  ] },
  { title: 'Department Projects', icon: Laptop, commands: [
    { label: 'AI&DS Projects', text: 'tell the guests about AI&DS dept projects', frontendKind: 'normal_chat' },
    { label: 'What projects are shown here?', text: 'what projects are shown here', frontendKind: 'expo_demo' },
    { label: 'Tell me about department projects', text: 'tell me about your department projects', frontendKind: 'expo_demo' },
    { label: 'Explain projects in this expo', text: 'explain the projects in this expo', frontendKind: 'expo_demo' },
  ] },
  { title: 'Individual Projects', icon: Sparkles, commands: [
    { label: 'Tell me about A.U.R.A', text: 'tell me about aura', frontendKind: 'expo_demo' },
    { label: 'Explain Edge Storm', text: 'explain edge storm', frontendKind: 'expo_demo' },
    { label: 'Explain Game Centre', text: 'explain game centre', frontendKind: 'expo_demo' },
    { label: 'Explain Tech Hub', text: 'explain tech hub', frontendKind: 'expo_demo' },
  ] },
  { title: 'Safety / Tools', icon: ShieldCheck, commands: [
    { label: 'Explain safe tools', text: 'explain your safe tools', frontendKind: 'expo_demo' },
    { label: 'Use local tools safely?', text: 'how do you use local tools safely', frontendKind: 'expo_demo' },
    { label: 'Control the laptop?', text: 'can you control the laptop', frontendKind: 'expo_demo' },
  ] },
  { title: 'Memory', icon: Brain, commands: [
    { label: 'Check persistent memory', text: 'check your persistent memory', frontendKind: 'memory' },
    { label: 'Storing this conversation?', text: 'are you storing this conversation', frontendKind: 'memory' },
    { label: 'What do you remember?', text: 'what do you remember', frontendKind: 'memory' },
    { label: 'What did Eswar tell you?', text: 'what did Eswar tell you about the expo', frontendKind: 'memory' },
  ] },
  { title: 'General Talk', icon: Sparkles, commands: [
    { label: 'General talk', text: 'can we have a general talk', frontendKind: 'expo_demo' },
    { label: 'Can we chat?', text: 'can we chat', frontendKind: 'expo_demo' },
    { label: 'Talk with me', text: 'talk with me', frontendKind: 'expo_demo' },
  ] },
  { title: 'Live Tool Demos', icon: CloudSun, commands: [
    { label: 'Weather in Coimbatore', text: 'what is the weather in Coimbatore', frontendKind: 'tool_command' },
    { label: 'Weather in Mumbai', text: 'what is the weather in Mumbai', frontendKind: 'tool_command' },
    { label: 'Will it rain today?', text: 'will it rain today', frontendKind: 'tool_command' },
    { label: 'Any interesting news today', text: 'any interesting news today', frontendKind: 'tool_command' },
    { label: 'Top headlines today', text: "tell me today's top headlines", frontendKind: 'tool_command' },
    { label: 'Technology news', text: 'latest technology news today', frontendKind: 'tool_command' },
    { label: 'Tamil Nadu news', text: 'latest Tamil Nadu news today', frontendKind: 'tool_command' },
    { label: 'Sports news', text: 'latest sports news today', frontendKind: 'tool_command' },
    { label: 'Check CPU and RAM', text: 'check CPU and RAM usage', frontendKind: 'tool_command' },
    { label: 'System status', text: 'what is my system status', frontendKind: 'tool_command' },
  ] },
  { title: 'Browser / Runtime Controls', icon: Mic, commands: [
    { label: 'Open Chrome', text: 'open Chrome', frontendKind: 'tool_command' },
    { label: 'Open new Chrome tab', text: 'open new Chrome tab', frontendKind: 'tool_command' },
    { label: 'Mute Mic', text: 'mute mic', frontendKind: 'runtime_control' },
    { label: 'Unmute Mic', text: 'unmute mic', frontendKind: 'runtime_control' },
    { label: 'Stop Speaking', text: 'stop speaking', frontendKind: 'runtime_control' },
  ] },
  { title: 'Vision Demo', icon: Eye, commands: [
    { label: 'Can you see me?', text: 'can you see me', frontendKind: 'vision' },
    { label: 'How many people are in camera?', text: 'how many people are in camera', frontendKind: 'vision' },
    { label: 'Check vision status', text: 'check vision status', frontendKind: 'vision' },
  ] },
  { title: 'Closing', icon: Square, commands: [
    { label: 'Summarize this demo', text: 'summarize this demo', frontendKind: 'expo_demo' },
    { label: 'Conclude the demo', text: 'conclude the demo', frontendKind: 'expo_demo' },
    { label: 'End the intro', text: 'end the intro', frontendKind: 'expo_demo' },
  ] },
]

const favorites: DemoCommand[] = [
  { label: 'Wake up', text: 'wakeup', frontendKind: 'wake' },
  { label: 'Start Expo Intro', text: 'start expo intro', frontendKind: 'expo_intro', shortcut: 'I' },
  { label: 'Guest Greeting', text: 'start guest greeting', frontendKind: 'expo_guest_greeting', shortcut: 'P' },
  { label: 'What can you do?', text: 'what can you do', frontendKind: 'expo_demo' },
  { label: 'This project', text: 'tell me about this project', frontendKind: 'expo_demo' },
  { label: 'Any news today', text: 'any interesting news today', frontendKind: 'tool_command' },
  { label: 'Coimbatore weather', text: 'what is the weather in Coimbatore', frontendKind: 'tool_command' },
  { label: 'Check Memory', text: 'check your persistent memory', frontendKind: 'memory' },
  { label: 'Stop Speaking', text: 'stop speaking', frontendKind: 'runtime_control' },
]

function matches(command: DemoCommand, query: string) {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return `${command.label} ${command.text} ${command.frontendKind}`.toLowerCase().includes(q)
}

export function MobileControl() {
  const [token, setToken] = useState('')
  const [text, setText] = useState('')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<RemoteControlStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [lastMessage, setLastMessage] = useState<string | null>(null)
  const [openSections, setOpenSections] = useState<Set<string>>(new Set(['Start / Greeting', 'About A.U.R.A', 'Expo / Engineers Week', 'Live Tool Demos']))

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const urlToken = params.get('token') || ''
    const stored = window.localStorage.getItem(TOKEN_STORAGE_KEY) || ''
    const nextToken = urlToken || stored
    if (nextToken) {
      setToken(nextToken)
      window.localStorage.setItem(TOKEN_STORAGE_KEY, nextToken)
    }
    if (urlToken) {
      params.delete('token')
      const query = params.toString()
      window.history.replaceState({}, document.title, `${window.location.pathname}${query ? `?${query}` : ''}`)
    }
  }, [])

  useEffect(() => {
    let alive = true
    const refresh = async () => {
      const data = await getRemoteControlStatus(token || undefined)
      if (alive) setStatus(data as RemoteControlStatus)
    }
    refresh()
    const timer = window.setInterval(refresh, 1200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [token])

  const disabled = status?.ok && !status.enabled
  const canSend = Boolean(status?.enabled && token.trim() && text.trim() && !busy)
  const commandDisabled = !status?.enabled || !token.trim() || busy

  const filteredSections = useMemo(() => {
    return sections
      .map((section) => ({ ...section, commands: section.commands.filter((command) => matches(command, search)) }))
      .filter((section) => section.commands.length > 0)
  }, [search])

  const statusLabel = useMemo(() => {
    if (!status) return 'connecting'
    if (!status.ok) return status.message || 'offline'
    if (!status.enabled) return 'disabled'
    return status.laptop_client_connected ? 'laptop connected' : 'waiting for laptop'
  }, [status])

  const persistToken = (value: string) => {
    setToken(value)
    if (value.trim()) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, value.trim())
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY)
    }
  }

  const send = async (command: DemoCommand | string, frontendKind?: string, shortcut?: string) => {
    const payload = typeof command === 'string'
      ? { label: 'Custom command', text: command, frontendKind: frontendKind || 'normal_chat', shortcut }
      : command
    const cleanText = payload.text.trim()
    if (!cleanText || !token.trim()) return
    setBusy(true)
    setLastMessage(`queued: ${payload.label}`)
    const result = await sendRemoteControlCommand({
      text: cleanText,
      token: token.trim(),
      source: 'mobile',
      label: payload.label,
      frontendKind: payload.frontendKind,
      shortcut: payload.shortcut,
    })
    setBusy(false)
    if (!result.ok) {
      setLastMessage(result.message || result.error_type || 'send failed')
      return
    }
    setLastMessage(`queued -> ${status?.last_command_status || result.status}: ${payload.label}`)
    if (typeof command === 'string') setText('')
  }

  const toggleSection = (title: string) => {
    setOpenSections((current) => {
      const next = new Set(current)
      if (next.has(title)) next.delete(title)
      else next.add(title)
      return next
    })
  }

  return (
    <main className="h-dvh overflow-y-auto overscroll-contain bg-[#05080d] px-4 py-5 pb-[max(2rem,env(safe-area-inset-bottom))] text-slate-100 [-webkit-overflow-scrolling:touch]">
      <div className="mx-auto flex min-h-[calc(100dvh-40px)] max-w-md flex-col gap-4">
        <header className="flex items-start justify-between gap-3">
          <div>
            <div className="font-mono text-[11px] uppercase tracking-[0.22em] text-cyan-300">A.U.R.A</div>
            <h1 className="mt-1 text-2xl font-semibold tracking-normal">Mobile Control</h1>
          </div>
          <div className="rounded-full border border-cyan-300/30 px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-cyan-100">
            {statusLabel}
          </div>
        </header>

        <section className="rounded-lg border border-white/10 bg-white/[0.04] p-3 text-sm text-slate-300">
          <div className="flex items-center gap-2 text-cyan-100">
            <Volume2 size={16} />
            <span>Responses play on the laptop A.U.R.A console.</span>
          </div>
        </section>

        {disabled ? (
          <section className="rounded-lg border border-amber-300/30 bg-amber-300/10 p-4 text-amber-100">
            Remote control disabled.
          </section>
        ) : (
          <>
            <section className="space-y-2">
              <label className="font-mono text-[11px] uppercase tracking-[0.18em] text-slate-400" htmlFor="remote-token">
                Access token
              </label>
              <input
                id="remote-token"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => persistToken(e.target.value)}
                className="w-full rounded-lg border border-white/10 bg-black/40 px-3 py-3 text-base outline-none transition focus:border-cyan-300"
                placeholder="Enter remote token"
              />
            </section>

            <section className="space-y-2">
              <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-slate-400">Quick Demo</div>
              <div className="flex gap-2 overflow-x-auto pb-1">
                {favorites.map((command) => (
                  <button
                    key={`${command.label}-${command.text}`}
                    type="button"
                    disabled={commandDisabled}
                    onClick={() => send(command)}
                    className="min-h-11 shrink-0 rounded-lg border border-cyan-300/20 bg-cyan-300/10 px-3 py-2 text-sm font-medium text-cyan-50 transition active:scale-[0.99] disabled:opacity-40"
                  >
                    {command.label}
                  </button>
                ))}
              </div>
            </section>

            <section className="space-y-3">
              <div>
                <h2 className="text-lg font-semibold tracking-normal">Try A.U.R.A Demo Commands</h2>
                <div className="mt-2 flex items-center gap-2 rounded-lg border border-white/10 bg-black/40 px-3">
                  <Search size={16} className="text-slate-500" />
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="min-h-11 flex-1 bg-transparent text-sm outline-none placeholder:text-slate-600"
                    placeholder="Search demo commands..."
                  />
                </div>
              </div>

              <div className="space-y-2">
                {filteredSections.map((section) => {
                  const Icon = section.icon
                  const isOpen = search.trim() ? true : openSections.has(section.title)
                  return (
                    <section key={section.title} className="rounded-lg border border-white/10 bg-white/[0.035]">
                      <button
                        type="button"
                        onClick={() => toggleSection(section.title)}
                        className="flex w-full items-center justify-between gap-3 px-3 py-3 text-left"
                      >
                        <span className="flex items-center gap-2 text-sm font-semibold text-slate-100">
                          <Icon size={16} className="text-cyan-300" />
                          {section.title}
                          <span className="font-mono text-[10px] text-slate-500">{section.commands.length}</span>
                        </span>
                        <ChevronDown size={16} className={`text-slate-500 transition ${isOpen ? 'rotate-180' : ''}`} />
                      </button>
                      {isOpen && (
                        <div className="grid grid-cols-1 gap-2 border-t border-white/10 p-2">
                          {section.commands.map((command) => (
                            <button
                              key={`${section.title}-${command.label}`}
                              type="button"
                              disabled={commandDisabled}
                              onClick={() => send(command)}
                              className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-white/10 bg-slate-950/70 px-3 py-2 text-left text-sm text-slate-100 transition active:scale-[0.99] disabled:opacity-40"
                            >
                              <span>{command.label}</span>
                              <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-slate-500">{command.frontendKind}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </section>
                  )
                })}
              </div>
            </section>

            <section className="mt-auto space-y-3">
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value.slice(0, 1000))}
                className="min-h-28 w-full resize-none rounded-lg border border-white/10 bg-black/45 px-3 py-3 text-base outline-none transition focus:border-cyan-300"
                placeholder="Type a custom command for the laptop orb"
              />
              <div className="grid grid-cols-[1fr_auto] gap-2">
                <button
                  type="button"
                  disabled={!canSend}
                  onClick={() => send(text)}
                  className="flex min-h-12 items-center justify-center gap-2 rounded-lg bg-cyan-300 px-4 py-3 font-semibold text-slate-950 transition active:scale-[0.99] disabled:opacity-40"
                >
                  <Send size={17} />
                  Send
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setText('')
                    setLastMessage(null)
                  }}
                  className="flex aspect-square min-h-12 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04] text-slate-200"
                  aria-label="Clear command"
                  title="Clear command"
                >
                  <Trash2 size={18} />
                </button>
              </div>
            </section>

            <footer className="min-h-6 font-mono text-[11px] uppercase tracking-[0.12em] text-slate-500">
              {lastMessage || `queue ${status?.queue_size ?? 0} | last ${status?.last_command_status || 'none'}`}
            </footer>
          </>
        )}
      </div>
    </main>
  )
}
