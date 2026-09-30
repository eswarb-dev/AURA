export type AuraApiResult<T = Record<string, any>> = T & {
  ok: boolean
  error?: string
  error_type?: string
  message?: string
}

async function safeJson(res: Response) {
  try {
    return await res.json()
  } catch {
    return null
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<AuraApiResult<T>> {
  try {
    const res = await fetch(url, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    })
    const data = await safeJson(res)
    if (!res.ok) {
      return {
        ok: false,
        ...(data || {}),
        error_type: data?.error_type || `http_${res.status}`,
        message: data?.message || `Request failed with HTTP ${res.status}`,
      }
    }
    return data || ({ ok: true } as AuraApiResult<T>)
  } catch (e: any) {
    if (e?.name === 'AbortError' || init?.signal?.aborted) {
      const reason = init?.signal?.reason || 'aborted_without_reason'
      return {
        ok: false,
        is_aborted: true,
        error_type: 'aborted',
        abort_reason: String(reason),
        message: `signal is aborted: ${reason}`,
      } as unknown as AuraApiResult<T>
    }
    return { ok: false, error_type: 'network_error', message: e?.message || 'Network request failed' } as AuraApiResult<T>
  }
}

export function sendAuraVoiceTurn(
  payload: Record<string, any>,
  timeoutMs: number = 25000,
  signal?: AbortSignal,
): Promise<AuraApiResult<Record<string, any>>> {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => {
    controller.abort('voice_turn_timeout')
  }, timeoutMs)

  if (signal) {
    const onParentAbort = () => {
      controller.abort(signal.reason || 'superseded_by_new_command')
    }
    if (signal.aborted) {
      onParentAbort()
    } else {
      signal.addEventListener('abort', onParentAbort, { once: true })
    }
  }

  return requestJson<Record<string, any>>('/api/agent/voice-turn', {
    method: 'POST',
    body: JSON.stringify(payload),
    signal: controller.signal,
  }).then((res) => {
    if (!res.ok && (controller.signal.aborted || res.message?.includes('aborted'))) {
      const reason = controller.signal.reason || signal?.reason || 'voice_turn_aborted'
      return {
        ...res,
        ok: false,
        is_aborted: true,
        abort_reason: String(reason),
        error_type: 'aborted',
        message: `voice_turn request aborted: ${reason}`,
      }
    }
    if (!res.ok && res.error_type === 'network_error') {
      const started = Date.now()
      return fetch('/api/tools/status', { method: 'GET' })
        .then((health) => ({
          ...res,
          request_id: payload.request_id,
          endpoint: '/api/agent/voice-turn',
          frontend_kind: payload.frontend_kind,
          elapsed_ms: Date.now() - started,
          navigator_online: typeof navigator !== 'undefined' ? navigator.onLine : null,
          backend_health: health.ok ? 'reachable' : `http_${health.status}`,
        }))
        .catch((e: any) => ({
          ...res,
          request_id: payload.request_id,
          endpoint: '/api/agent/voice-turn',
          frontend_kind: payload.frontend_kind,
          elapsed_ms: Date.now() - started,
          navigator_online: typeof navigator !== 'undefined' ? navigator.onLine : null,
          backend_health: e?.message || 'unreachable',
        }))
    }
    return res
  }).finally(() => clearTimeout(timeoutId))
}

export function getIdentityProfiles(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/identity/profiles')
}

export function getIdentityStorage(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/diagnostics/identity-storage')
}

export function verifyOwnerFace(payload: { embedding: number[]; adapter: string }): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/identity/verify-face', { method: 'POST', body: JSON.stringify(payload) })
}

export function getProviders(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/diagnostics/providers')
}

export function getCapabilities(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/capabilities')
}

export function testTts(text: string): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/tts/speak', { method: 'POST', body: JSON.stringify({ text, provider: 'auto', include_audio: true }) })
}

export function runTtsPreflight(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/diagnostics/tts-preflight', { method: 'POST' })
}

export function getMemoryStatus(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/memory/status')
}

export function getMemoryContextPreview(conversationId?: string): Promise<AuraApiResult<Record<string, any>>> {
  const qs = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''
  return requestJson(`/api/memory/context-preview${qs}`)
}

export function getMemoryFacts(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/memory/facts')
}

export function testMemoryWrite(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/memory/test-write', { method: 'POST' })
}

export function clearLocalFallbackMemory(): Promise<AuraApiResult<Record<string, any>>> {
  return requestJson('/api/memory/local-fallback/clear', { method: 'POST' })
}
