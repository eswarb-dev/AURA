import { Activity } from 'lucide-react'

export type AuraBootPhase =
  | 'initializing'
  | 'audio'
  | 'mic'
  | 'camera'
  | 'tracking'
  | 'identity'
  | 'ready'
  | 'permission_needed'
  | 'failed'

export type AuraBootLine = {
  key: string
  text: string
  status?: 'pending' | 'active' | 'ok' | 'failed' | 'blocked'
  detail?: string
}

function statusLabel(status?: AuraBootLine['status']) {
  if (status === 'ok') return 'ok'
  if (status === 'failed') return 'failed'
  if (status === 'blocked') return 'permission required'
  if (status === 'active') return '...'
  return ''
}

export function AuraBootScreen({
  phase,
  lines,
  errors,
  needsAuthorization,
  onAuthorize,
}: {
  phase: AuraBootPhase
  lines: AuraBootLine[]
  errors: string[]
  needsAuthorization: boolean
  onAuthorize: () => void
}) {
  return (
    <div className="aura-boot-screen">
      <div className="aura-boot-terminal">
        <div className="aura-boot-header">
          <Activity size={14} />
          <span>A.U.R.A BOOT</span>
          <span className="aura-boot-phase">{phase}</span>
        </div>
        <div className="aura-boot-lines">
          {lines.map((line) => (
            <div key={line.key} className={`aura-boot-line ${line.status || 'pending'}`}>
              <span className="aura-boot-prompt">&gt;</span>
              <span className="aura-boot-text">{line.text}</span>
              {statusLabel(line.status) && <span className="aura-boot-status">{statusLabel(line.status)}</span>}
              {line.detail && <span className="aura-boot-detail">{line.detail}</span>}
            </div>
          ))}
          {errors.map((error, index) => (
            <div key={`${error}-${index}`} className="aura-boot-line blocked">
              <span className="aura-boot-prompt">&gt;</span>
              <span className="aura-boot-text">{error}</span>
            </div>
          ))}
          <div className="aura-boot-cursor">█</div>
        </div>
        {needsAuthorization && (
          <button type="button" className="aura-boot-authorize" onClick={onAuthorize}>
            AUTHORIZE A.U.R.A RUNTIME
          </button>
        )}
      </div>
    </div>
  )
}
