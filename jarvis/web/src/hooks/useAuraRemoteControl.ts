import { useEffect, useRef } from 'react'
import { ackRemoteControlCommand, getRemoteControlStatus, pollRemoteControlCommand, type RemoteCommand } from '@/lib/remoteControlApi'

type SendTextTurn = (
  text: string,
  confidence?: number,
  captureState?: { micMutedAtCapture?: boolean; recognitionActiveAtCapture?: boolean },
  dispatchOptions?: { frontendKind?: string; timeoutMs?: number; expoShortcut?: boolean; inputSource?: string },
) => Promise<any>

type UseAuraRemoteControlOptions = {
  sendTextTurn: SendTextTurn
  stopSpeaking: () => void
  stopContinuousListening: () => void
  startContinuousListening: () => Promise<boolean>
  onLog: (line: string) => void
}

function dispatchOptionsFor(command: RemoteCommand) {
  if (command.frontend_kind === 'expo_intro') {
    return { frontendKind: 'expo_intro', timeoutMs: 60000, expoShortcut: command.shortcut === 'I', inputSource: command.source || 'mobile' }
  }
  return { frontendKind: command.frontend_kind || 'normal_chat', inputSource: command.source || 'mobile' }
}

async function ack(commandId: string, status: 'picked_up' | 'route_completed' | 'speech_played' | 'route_completed_speech_failed' | 'executed' | 'failed', safeMessage?: string) {
  await ackRemoteControlCommand({
    commandId,
    token: '',
    client: 'main_orb',
    status,
    safeMessage,
  })
}

export function useAuraRemoteControl({ sendTextTurn, stopSpeaking, stopContinuousListening, startContinuousListening, onLog }: UseAuraRemoteControlOptions) {
  const processingIdRef = useRef<string | null>(null)
  const enabledRef = useRef(false)
  const sendTextTurnRef = useRef(sendTextTurn)
  const stopSpeakingRef = useRef(stopSpeaking)
  const stopContinuousListeningRef = useRef(stopContinuousListening)
  const startContinuousListeningRef = useRef(startContinuousListening)
  const onLogRef = useRef(onLog)

  useEffect(() => {
    sendTextTurnRef.current = sendTextTurn
    stopSpeakingRef.current = stopSpeaking
    stopContinuousListeningRef.current = stopContinuousListening
    startContinuousListeningRef.current = startContinuousListening
    onLogRef.current = onLog
  }, [sendTextTurn, stopSpeaking, stopContinuousListening, startContinuousListening, onLog])

  useEffect(() => {
    let stopped = false

    const statusLoop = async () => {
      const status = await getRemoteControlStatus()
      if (!stopped && status.ok) {
        enabledRef.current = Boolean(status.enabled)
      }
    }

    const pollLoop = async () => {
      if (stopped || !enabledRef.current || processingIdRef.current) return
      const result = await pollRemoteControlCommand()
      if (!result.ok || !result.command) return
      const command = result.command
      processingIdRef.current = command.command_id
      onLogRef.current(`[AURA REMOTE] command_received source=${command.source || 'mobile'} id=${command.command_id}`)
      try {
        let playbackResult: any = null
        await ack(command.command_id, 'picked_up')
        const normalized = command.text.trim().toLowerCase().replace(/\s+/g, ' ')
        if (command.frontend_kind === 'runtime_control' && normalized === 'stop speaking') {
          stopSpeakingRef.current()
        } else if (command.frontend_kind === 'runtime_control' && normalized === 'mute mic') {
          stopContinuousListeningRef.current()
        } else if (command.frontend_kind === 'runtime_control' && normalized === 'unmute mic') {
          await startContinuousListeningRef.current()
        } else {
          playbackResult = await sendTextTurnRef.current(command.text, undefined, undefined, dispatchOptionsFor(command))
          if (playbackResult?.ttsBlocked) {
            const isExpo = command.frontend_kind === 'expo_intro' || command.frontend_kind === 'expo_demo'
            const failureReason = isExpo ? 'tts_playback_blocked' : 'tts_provider_failed'
            await ack(command.command_id, 'route_completed_speech_failed', failureReason)
            onLogRef.current(`[AURA REMOTE] command_failed id=${command.command_id} reason=${failureReason}`)
            return
          }
          await ack(command.command_id, 'route_completed')
        }
        await ack(command.command_id, 'speech_played')
        const fallback = playbackResult?.playback === 'browser_fallback' ? ' fallback=browser' : ''
        onLogRef.current(`[AURA REMOTE] command_executed id=${command.command_id} speech_played=true${fallback}`)
      } catch (e: any) {
        await ack(command.command_id, 'failed', e?.message || 'remote_command_failed')
        onLogRef.current(`[AURA REMOTE] command_failed id=${command.command_id}`)
      } finally {
        if (processingIdRef.current === command.command_id) {
          processingIdRef.current = null
        }
      }
    }

    statusLoop()
    const statusTimer = window.setInterval(statusLoop, 2500)
    const pollTimer = window.setInterval(pollLoop, 1000)
    return () => {
      stopped = true
      window.clearInterval(statusTimer)
      window.clearInterval(pollTimer)
    }
  }, [])
}
