export type RemoteControlStatus = {
  ok: boolean
  enabled: boolean
  laptop_client_connected: boolean
  queue_size: number
  last_command_status: string
  error_type?: string
  message?: string
}

export type RemoteCommand = {
  command_id: string
  text: string
  source: string
  label?: string | null
  shortcut?: string | null
  frontend_kind: string
  created_at: number
  expires_at: number
}

type ApiResult<T> = T & {
  ok: boolean
  error_type?: string
  message?: string
  detail?: any
}

async function safeJson(res: Response) {
  try {
    return await res.json()
  } catch {
    return null
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<ApiResult<T>> {
  try {
    const res = await fetch(url, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    })
    const data = await safeJson(res)
    if (!res.ok) {
      const detail = data?.detail || data || {}
      return {
        ok: false,
        ...(detail || {}),
        error_type: detail?.error_type || `http_${res.status}`,
        message: detail?.message || `Request failed with HTTP ${res.status}`,
      } as ApiResult<T>
    }
    return (data || { ok: true }) as ApiResult<T>
  } catch (e: any) {
    return { ok: false, error_type: 'network_error', message: e?.message || 'Network request failed' } as ApiResult<T>
  }
}

export function getRemoteControlStatus(token?: string): Promise<ApiResult<RemoteControlStatus>> {
  const query = token ? `?token=${encodeURIComponent(token)}` : ''
  return requestJson<RemoteControlStatus>(`/api/remote-control/status${query}`)
}

export function sendRemoteControlCommand(payload: {
  text: string
  token: string
  source?: string
  label?: string
  shortcut?: string
  frontendKind?: string
}): Promise<ApiResult<{ command_id: string; queue_size: number; status: string }>> {
  return requestJson('/api/remote-control/send', {
    method: 'POST',
    body: JSON.stringify({
      text: payload.text,
      token: payload.token,
      source: payload.source || 'mobile',
      label: payload.label,
      shortcut: payload.shortcut,
      frontend_kind: payload.frontendKind,
    }),
  })
}

export function pollRemoteControlCommand(): Promise<ApiResult<{ command: RemoteCommand | null; queue_size: number }>> {
  return requestJson('/api/remote-control/poll?client=main_orb')
}

export function ackRemoteControlCommand(payload: {
  commandId: string
  token: string
  status: 'picked_up' | 'route_completed' | 'speech_played' | 'route_completed_speech_failed' | 'executed' | 'failed'
  safeMessage?: string
  client?: string
}): Promise<ApiResult<{ command_id: string; status: string }>> {
  return requestJson('/api/remote-control/ack', {
    method: 'POST',
    body: JSON.stringify({
      command_id: payload.commandId,
      token: payload.token,
      client: payload.client,
      status: payload.status,
      safe_message: payload.safeMessage,
    }),
  })
}
