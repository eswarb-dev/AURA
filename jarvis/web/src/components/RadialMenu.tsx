import { useEffect, useState } from 'react'
import {
  FolderGit2, ListChecks, Brain, Activity, Settings, X, Plus,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { ErrorBoundary } from './ErrorBoundary'

export interface RadialPanel {
  id: string
  label: string
  icon: string
}

const ICONS: Record<string, typeof FolderGit2> = {
  projects: FolderGit2,
  tasks: ListChecks,
  brain: Brain,
  activity: Activity,
  settings: Settings,
}

// Panels register their React component (not a render function) on import.
// Rendering via a real <Component /> JSX element keeps React's hook
// bookkeeping consistent between renders — calling render() as a plain
// function (the previous implementation) broke hook ordering under React 19.
const PANEL_CONTENT: Record<string, React.ComponentType | null> = {}

export function registerPanelContent(id: string, Component: React.ComponentType) {
  PANEL_CONTENT[id] = Component
}

/** Render the registered panel for an id. Wrapped in an ErrorBoundary so a
 *  single panel crash can't tear down the whole orb UI. */
function PanelContent({ id }: { id: string }) {
  const Component = PANEL_CONTENT[id]
  if (!Component) return <UnknownPanel id={id} />
  return (
    <ErrorBoundary compact label={`panel · ${id}`}>
      <Component />
    </ErrorBoundary>
  )
}

export function RadialMenu({ panels }: { panels: RadialPanel[] }) {
  const [open, setOpen] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)

  const closePanel = () => {
    if (new URLSearchParams(window.location.search).get('debug') === '1') {
      console.log('[AURA UI] close panel:', open)
      console.log('[AURA UI] modal close clicked')
    }
    setOpen(null)
  }

  // Listen to external panel open requests (e.g. from ProjectContextWidget)
  useEffect(() => {
    const onOpenPanel = (e: Event) => {
      const detail = (e as CustomEvent).detail as { panel?: string }
      if (detail?.panel) {
        if (new URLSearchParams(window.location.search).get('debug') === '1') {
          console.log('[AURA UI] open panel:', detail.panel)
        }
        setOpen(detail.panel)
      }
    }
    const onClosePanel = () => {
      if (new URLSearchParams(window.location.search).get('debug') === '1') {
        console.log('[AURA UI] close panel via event')
      }
      setOpen(null)
    }
    window.addEventListener('jarvis-open-panel', onOpenPanel)
    window.addEventListener('jarvis-close-panel', onClosePanel)
    return () => {
      window.removeEventListener('jarvis-open-panel', onOpenPanel)
      window.removeEventListener('jarvis-close-panel', onClosePanel)
    }
  }, [])

  // Close panel on Escape
  useEffect(() => {
    if (!open) return
    if (new URLSearchParams(window.location.search).get('debug') === '1') {
      console.log('[AURA UI] open panel:', open)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (new URLSearchParams(window.location.search).get('debug') === '1') {
          console.log('[AURA UI] escape close')
        }
        setOpen(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const openId = hovered ?? open

  return (
    <>
      {/* The hub button — click to expand into a ring */}
      <div className="relative flex flex-col items-center gap-3">
        <button
          type="button"
          onClick={() => {
            const next = open ? null : '__hub__'
            if (next && new URLSearchParams(window.location.search).get('debug') === '1') {
              console.log('[AURA UI] open panel: __hub__')
            }
            setOpen(next)
          }}
          className={cn(
            'flex h-12 w-12 items-center justify-center rounded-full border-2 backdrop-blur-md transition',
            open
              ? 'border-[var(--blue)] text-[var(--blue)] shadow-[0_0_18px_var(--blue-glow)]'
              : 'border-[var(--line-bright)] text-[var(--text-dim)] hover:border-[var(--blue)] hover:text-[var(--blue)]',
          )}
        >
          <Plus size={20} className={cn('transition', open && 'rotate-45')} />
        </button>

        {/* Panel labels (always-visible shortcuts beside the hub button) */}
        {!open && panels.map((p, i) => {
          const Icon = ICONS[p.icon] ?? Settings
          const x = -(i + 1) * 42
          const y = 0
          return (
            <button
              key={p.id}
              type="button"
              onMouseEnter={() => setHovered(p.id)}
              onMouseLeave={() => setHovered(null)}
              onClick={() => {
                if (new URLSearchParams(window.location.search).get('debug') === '1') {
                  console.log('[AURA UI] open panel:', p.id)
                }
                setOpen(p.id)
              }}
              style={{ transform: `translate(${x}px, ${y}px)` }}
              className="absolute top-1.5 flex h-9 w-9 items-center justify-center rounded-full border border-[var(--line-bright)] bg-black/75 text-[var(--text-dim)] backdrop-blur-md transition hover:scale-110 hover:border-[var(--blue)] hover:text-[var(--blue)]"
              title={p.label}
            >
              <Icon size={14} />
            </button>
          )
        })}
      </div>

      {/* Floating panel & backdrop — renders requested panel content */}
      {openId && (
        <>
          <div
            className="aura-modal-backdrop"
            onClick={() => {
              if (new URLSearchParams(window.location.search).get('debug') === '1') {
                console.log('[AURA UI] backdrop clicked')
              }
              closePanel()
            }}
          />
          <div
            className="aura-modal-panel aura-task-panel pointer-events-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex h-full flex-col">
              <div className="aura-task-panel-header">
                <div className="flex items-center gap-2 font-display text-[12px] uppercase tracking-[0.24em] text-[var(--blue)]">
                  {(() => {
                    const p = panels.find((p) => p.id === openId)
                    if (!p) return <span>menu</span>
                    const Icon = ICONS[p.icon] ?? Settings
                    return <><Icon size={13} /> {p.label}</>
                  })()}
                </div>
                <button
                  type="button"
                  aria-label="Close modal panel"
                  onClick={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                    closePanel()
                  }}
                  className="aura-modal-close"
                  title="Close panel (Esc)"
                >
                  <X size={16} />
                </button>
              </div>
              <div className="aura-task-panel-body">
                {openId === '__hub__' ? <HubPanel onPick={(id) => setOpen(id)} panels={panels} /> : (
                  <PanelContent id={openId} />
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </>
  )
}

function HubPanel({ panels, onPick }: { panels: RadialPanel[]; onPick: (id: string) => void }) {
  return (
    <div className="space-y-1.5 p-3">
      {panels.map((p) => {
        const Icon = ICONS[p.icon] ?? Settings
        return (
          <button
            key={p.id}
            type="button"
            onClick={() => onPick(p.id)}
            className="flex w-full items-center gap-3 rounded-sm border border-[var(--line-bright)] bg-black/30 px-3 py-2.5 text-left transition hover:border-[var(--blue)] hover:bg-[rgba(0,200,255,0.06)]"
          >
            <Icon size={14} className="text-[var(--blue)]" />
            <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--text)]">{p.label}</span>
          </button>
        )
      })}
    </div>
  )
}

function UnknownPanel({ id }: { id: string }) {
  return (
    <div className="p-5 text-[var(--text-dim)]">
      <p>Panel "{id}" has no content registered yet.</p>
      <p className="mt-2 text-[10px] text-[var(--text-faint)]">
        Use <code>registerPanelContent(id, Component)</code> in the panel module to register.
      </p>
    </div>
  )
}
