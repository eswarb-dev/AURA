import { useEffect, useMemo, useRef, useState } from 'react'
import { getIdentityProfiles, verifyOwnerFace } from '@/lib/auraApi'
import { extractFaceDescriptorFromVideo, getPeopleTrackingAssetDiagnostics, getVisionModelLoadState, type IdentityContext, type PeopleTrackingRuntimeDiagnostics, type VisionContext } from '@/lib/visionIdentity'
import { PersonTracker, visibleActiveTracks, type PersonTrack } from '@/lib/personTracker'

type OwnerProfile = {
  id?: string
  display_name?: string
  role?: string
  face_enrolled?: boolean
  voice_enrolled?: boolean
  address?: string
  preferred_address?: string
  address_source?: string
}

export type AuraVisionStatus = 'active' | 'off' | 'starting' | 'error' | 'loading' | 'unknown'
type TrackingState = 'off' | 'starting' | 'active' | 'error' | 'unavailable'
type TrackingErrorStage = 'face_model_load' | 'person_detector_init' | 'camera_stream' | 'identity_match' | 'tracking_loop' | 'unknown'
type TrackingErrorInfo = {
  code: string
  safe_message: string
  stage: TrackingErrorStage
  timestamp: string
}

const IDENTITY_LIVE_MATCH_TTL_MS = Number(import.meta.env.VITE_AURA_IDENTITY_LIVE_MATCH_TTL_MS || 5000)
const TRACKING_START_VERIFY_TIMEOUT_MS = Number(import.meta.env.VITE_AURA_TRACKING_START_VERIFY_TIMEOUT_MS || 5000)
const PEOPLE_TRACKING_VIDEO_READY_TIMEOUT_MS = Number(import.meta.env.VITE_AURA_PEOPLE_TRACKING_VIDEO_READY_TIMEOUT_MS || 7000)
const PEOPLE_TRACKING_MODEL_READY_TIMEOUT_MS = Number(import.meta.env.VITE_AURA_PEOPLE_TRACKING_MODEL_READY_TIMEOUT_MS || 10000)

function classifyPeopleTrackingError(raw: string, fallbackCode = 'unknown_only_if_unclassified') {
  const value = String(raw || '').toLowerCase()
  if (!value.trim()) return fallbackCode
  if (value.includes('mediapipe_package_missing')) return 'mediapipe_package_missing'
  if (value.includes('mediapipe_import_failed')) return 'mediapipe_import_failed'
  if (value.includes('mediapipe_wasm_unavailable')) return 'mediapipe_wasm_unavailable'
  if (value.includes('mediapipe_model_missing')) return 'mediapipe_model_missing'
  if (value.includes('mediapipe_model_fetch_failed')) return 'mediapipe_model_fetch_failed'
  if (value.includes('mediapipe_detector_init_failed')) return 'mediapipe_detector_init_failed'
  if (value.includes('mediapipe_unsupported_browser')) return 'mediapipe_unsupported_browser'
  if (value.includes('permission') || value.includes('notallowed')) return 'permission_denied'
  if (value.includes('camera_stream_missing') || value.includes('stream')) return 'camera_stream_missing'
  if (value.includes('video_element_missing')) return 'video_element_missing'
  if (value.includes('video_dimensions_zero') || value.includes('videowidth') || value.includes('videoheight') || value.includes('width=0') || value.includes('height=0')) return 'video_dimensions_zero'
  if (value.includes('camera_video_not_ready') || value.includes('video_not_ready') || value.includes('readystate') || value.includes('metadata')) return 'camera_video_not_ready'
  if (value.includes('model_path_not_found') || value.includes('404') || value.includes('not found')) return 'model_path_not_found'
  if (value.includes('model_missing') || value.includes('model_not_loaded') || value.includes('not_loaded')) return 'model_not_loaded'
  if (value.includes('faceapi') || value.includes('face-api')) return 'faceapi_unavailable'
  if (value.includes('tfjs') || value.includes('tensorflow')) return value.includes('backend') ? 'tfjs_backend_failed' : 'tfjs_unavailable'
  if (value.includes('mediapipe') || value.includes('filesetresolver')) return 'mediapipe_detector_init_failed'
  if (value.includes('wasm')) return 'wasm_backend_failed'
  if (value.includes('canvas')) return 'canvas_context_failed'
  if (value.includes('timeout')) return 'startup_timeout'
  if (value.includes('detector_library_missing')) return 'detector_library_missing'
  if (value.includes('detector') || value.includes('createfromoptions')) return 'detector_init_failed'
  if (value.includes('tracking_loop_exception') || value.includes('loop')) return 'people_tracking_loop_exception'
  return fallbackCode
}

function safeTrackingError(code: string, stage: TrackingErrorStage = 'unknown', fallback = 'Tracking could not start'): TrackingErrorInfo {
  const allowed = new Set([
    'model_not_loaded',
    'model_path_not_found',
    'mediapipe_package_missing',
    'mediapipe_import_failed',
    'mediapipe_wasm_unavailable',
    'mediapipe_model_missing',
    'mediapipe_model_fetch_failed',
    'mediapipe_detector_init_failed',
    'mediapipe_unsupported_browser',
    'camera_stream_missing',
    'video_element_missing',
    'video_not_ready',
    'camera_video_not_ready',
    'video_dimensions_zero',
    'detector_library_missing',
    'detector_init_failed',
    'faceapi_unavailable',
    'tfjs_unavailable',
    'tfjs_backend_failed',
    'mediapipe_unavailable',
    'wasm_backend_failed',
    'canvas_context_failed',
    'permission_denied',
    'people_tracking_loop_exception',
    'startup_timeout',
    'unsupported_browser',
    'unknown_only_if_unclassified',
  ])
  const safeCode = allowed.has(code) ? code : classifyPeopleTrackingError(code)
  return {
    code: safeCode,
    safe_message: fallback,
    stage,
    timestamp: new Date().toISOString(),
  }
}

function normalizeCameraStatus(active: boolean, error?: string): AuraVisionStatus {
  if (error) return 'error'
  return active ? 'active' : 'off'
}

function normalizeTrackingStatus(active: boolean, error?: string, starting?: boolean): AuraVisionStatus {
  if (error) return 'error'
  if (starting) return 'starting'
  return active ? 'active' : 'off'
}

export function useAuraMainVisionContext(onLog?: (line: string) => void) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const trackerRef = useRef<PersonTracker | null>(null)
  const timerRef = useRef<number | null>(null)
  const verifyTimerRef = useRef<number | null>(null)
  const previewVideoRef = useRef<HTMLVideoElement | null>(null)
  const [cameraOn, setCameraOn] = useState(false)
  const [trackingState, setTrackingState] = useState<TrackingState>('off')
  const [trackingStartedAt, setTrackingStartedAt] = useState<string | null>(null)
  const [trackingError, setTrackingError] = useState<TrackingErrorInfo | null>(null)
  const [ownerVerified, setOwnerVerified] = useState(false)
  const [ownerProfile, setOwnerProfile] = useState<OwnerProfile | null>(null)
  const [tracks, setTracks] = useState<PersonTrack[]>([])
  const [detectorSource, setDetectorSource] = useState('none')
  const [peopleTrackingRuntime, setPeopleTrackingRuntime] = useState<PeopleTrackingRuntimeDiagnostics | null>(null)
  const [lastError, setLastError] = useState('')

  const pausedRef = useRef(false)
  const latestIdentityContextRef = useRef<IdentityContext>({
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
  })
  const latestVisionContextRef = useRef<VisionContext>({
    camera_status: 'off',
    tracking_status: 'off',
    people_count: 0,
    current_identity: 'guest',
    address_selected: 'Guest',
    fusion_decision: null,
    tracking_mode: 'none',
    tracks: [],
  })

  const cameraStartPromiseRef = useRef<Promise<{ ok: boolean; error?: string }> | null>(null)
  const trackingStartPromiseRef = useRef<Promise<{ ok: boolean; error?: string }> | null>(null)
  const lastOwnerVerifiedAtRef = useRef<string | null>(null)
  const lastPersonSeenAtRef = useRef<string | null>(null)
  const lastKnownPeopleCountRef = useRef(0)
  const activeTracks = useMemo(() => visibleActiveTracks(tracks), [tracks])
  const ownerAddress = ownerProfile?.preferred_address || ownerProfile?.address || 'Sir'
  const tracking = trackingState === 'active'
  const trackingStarting = trackingState === 'starting'
  const streamIsLive = () => Boolean(streamRef.current?.active && streamRef.current.getVideoTracks().some((track) => track.readyState === 'live'))
  const recentOwnerVerified = () => {
    if (!lastOwnerVerifiedAtRef.current) return false
    return Date.now() - Date.parse(lastOwnerVerifiedAtRef.current) < IDENTITY_LIVE_MATCH_TTL_MS
  }
  const videoIsReady = () => Boolean(videoRef.current && videoRef.current.readyState >= 2 && videoRef.current.videoWidth > 0 && videoRef.current.videoHeight > 0)
  const waitForVideoReady = async (timeoutMs = PEOPLE_TRACKING_VIDEO_READY_TIMEOUT_MS): Promise<{ ok: boolean; error?: TrackingErrorInfo }> => {
    const started = Date.now()
    onLog?.(`[AURA PEOPLE TRACKING] waiting_for_video_ready timeout_ms=${timeoutMs}`)
    while (Date.now() - started < timeoutMs) {
      if (!streamIsLive()) return { ok: false, error: safeTrackingError('camera_stream_missing', 'camera_stream', 'Camera stream is missing') }
      if (!videoRef.current) return { ok: false, error: safeTrackingError('video_element_missing', 'camera_stream', 'Video element is not available') }
      if (videoIsReady()) return { ok: true }
      await new Promise((resolve) => window.setTimeout(resolve, 120))
    }
    if (!videoRef.current) return { ok: false, error: safeTrackingError('video_element_missing', 'camera_stream', 'Video element is not available') }
    if (videoRef.current.videoWidth === 0 || videoRef.current.videoHeight === 0) {
      return { ok: false, error: safeTrackingError('video_dimensions_zero', 'camera_stream', 'Video dimensions are not available') }
    }
    return { ok: false, error: safeTrackingError('camera_video_not_ready', 'camera_stream', 'Video is not ready for tracking') }
  }
  const clearLiveIdentity = (reason = 'tracking_not_active') => {
    setOwnerVerified(false)
    lastOwnerVerifiedAtRef.current = null
    onLog?.(`[AURA IDENTITY] live_identity_address_allowed=false reason=${reason}`)
  }

  // Update cached refs synchronously whenever state changes
  useEffect(() => {
    const trackingActive = trackingState === 'active'
    const ownerRecentlyVerified = trackingActive && (ownerVerified || recentOwnerVerified())
    const recentPeopleCount = Math.max(activeTracks.length, ownerRecentlyVerified ? 1 : 0, lastKnownPeopleCountRef.current || 0)
    const defaultAddress = ownerRecentlyVerified ? ownerAddress : 'Guest'
    const activeProfileIsUnambiguous = ownerRecentlyVerified && activeTracks.length <= 1
    const liveIdentityAddressAllowed = Boolean(trackingActive && ownerRecentlyVerified && activeProfileIsUnambiguous && ownerAddress)
    const matchedProfile = ownerRecentlyVerified ? {
      id: ownerProfile?.id || 'owner',
      display_name: ownerProfile?.display_name || 'Owner',
      preferred_address: ownerAddress,
      address_source: ownerProfile?.address_source || 'explicit_profile',
    } : null
    const idCtx: IdentityContext = {
      decision: ownerRecentlyVerified ? 'owner_verified_face_only' : ownerProfile?.face_enrolled ? 'owner_enrolled_not_verified' : 'guest_or_unknown',
      display_name: ownerRecentlyVerified ? 'Owner' : 'Guest',
      role: ownerRecentlyVerified ? 'owner' : 'guest',
      address: defaultAddress,
      preferred_address: ownerRecentlyVerified ? ownerAddress : null,
      address_source: ownerRecentlyVerified ? 'explicit_profile' : 'default',
      matched_profile_id: matchedProfile?.id || null,
      matched_profile_name: matchedProfile?.display_name || null,
      matched_profile: liveIdentityAddressAllowed ? matchedProfile : null,
      identity_confidence: ownerRecentlyVerified ? 0.8 : null,
      identity_state: liveIdentityAddressAllowed ? 'matched' : trackingState === 'error' ? 'unavailable' : 'guest',
      gesture_tracking_state: 'off',
      people_tracking_state: trackingState,
      tracking_state: trackingState,
      live_identity_fresh: ownerRecentlyVerified,
      live_identity_address_allowed: liveIdentityAddressAllowed,
      active_profile_is_unambiguous: activeProfileIsUnambiguous,
      last_live_match_at: lastOwnerVerifiedAtRef.current,
      owner_face_status: ownerVerified ? 'verified' : ownerProfile?.face_enrolled ? 'enrolled' : 'not enrolled',
      owner_voice_status: ownerProfile?.voice_enrolled ? 'enrolled' : 'not enrolled',
      owner_face_enrolled: Boolean(ownerProfile?.face_enrolled),
      owner_voice_enrolled: Boolean(ownerProfile?.voice_enrolled),
      people_count: recentPeopleCount,
      last_owner_verified_at: lastOwnerVerifiedAtRef.current,
      last_person_seen_at: lastPersonSeenAtRef.current,
      last_known_people_count: lastKnownPeopleCountRef.current,
      tracking_mode: trackingActive ? 'all' : 'none',
      tracks: activeTracks.map((track) => ({
        track_id: track.track_id,
        identity: track.identity === 'owner' || ownerVerified ? 'owner' : 'guest',
        address: track.identity === 'owner' || ownerVerified ? ownerAddress : String(track.address || 'Guest'),
        status: track.status,
        confirmed: track.confirmed,
        stable_label_index: track.stable_label_index,
        descriptor_present: false,
        descriptor_length: 0,
      })),
    }
    const cameraStatus = normalizeCameraStatus(cameraOn, lastError && !cameraOn ? lastError : '')
    const trackingStatus = trackingState === 'error' ? 'error' : normalizeTrackingStatus(tracking, '', trackingStarting)
    const visCtx: VisionContext = {
      camera_state: cameraStatus,
      camera_status: cameraStatus,
      stream_ready: streamIsLive(),
      video_ready: videoIsReady(),
      video_width: videoRef.current?.videoWidth || 0,
      video_height: videoRef.current?.videoHeight || 0,
      gesture_tracking_state: 'off',
      gesture_tracking_error: null,
      people_tracking_state: trackingState,
      people_tracking_error: trackingError,
      people_tracking_runtime: peopleTrackingRuntime || undefined,
      tracking_state: trackingState,
      tracking_status: trackingStatus,
      tracking_started_at: trackingStartedAt,
      tracking_error: trackingError,
      tracking_last_error_code: trackingError?.code || null,
      tracking_last_error_message: trackingError?.safe_message || null,
      tracking_backend: detectorSource,
      model_loaded: detectorSource !== 'none',
      loop_running: Boolean(timerRef.current),
      people_count: recentPeopleCount,
      last_owner_verified_at: lastOwnerVerifiedAtRef.current,
      last_person_seen_at: lastPersonSeenAtRef.current,
      last_known_people_count: lastKnownPeopleCountRef.current,
      current_identity: ownerRecentlyVerified ? 'owner' : 'guest',
      address_selected: defaultAddress,
      preferred_address: ownerRecentlyVerified ? ownerAddress : null,
      address_source: ownerRecentlyVerified ? 'explicit_profile' : 'default',
      matched_profile_id: matchedProfile?.id || null,
      matched_profile_name: matchedProfile?.display_name || null,
      identity_confidence: ownerRecentlyVerified ? 0.8 : null,
      identity_state: liveIdentityAddressAllowed ? 'matched' : trackingState === 'error' ? 'unavailable' : 'guest',
      owner_profile_enrolled: Boolean(ownerProfile?.face_enrolled),
      live_identity_fresh: ownerRecentlyVerified,
      live_identity_address_allowed: liveIdentityAddressAllowed,
      active_profile_is_unambiguous: activeProfileIsUnambiguous,
      visual_gender_inference: false,
      descriptors_exposed: false,
      raw_frames_uploaded: false,
      fusion_decision: idCtx.decision,
      tracking_mode: trackingActive ? 'all' : 'none',
      tracks: activeTracks.map((track) => ({
        track_id: track.track_id,
        status: track.status,
        identity: track.identity === 'owner' || ownerVerified ? 'owner' : 'guest',
        address: track.identity === 'owner' || ownerVerified ? ownerAddress : String(track.address || 'Guest'),
        visual_estimate: track.gender_estimate,
        gender_confidence: track.gender_confidence,
        confirmed: track.confirmed,
        stable_label_index: track.stable_label_index,
        missed_frames: track.missed_frames,
        bbox: track.smoothed_bbox || track.bbox,
      })),
    }
    latestIdentityContextRef.current = idCtx
    latestVisionContextRef.current = visCtx
  }, [cameraOn, tracking, trackingState, trackingStarting, trackingStartedAt, trackingError, peopleTrackingRuntime, ownerVerified, ownerProfile, ownerAddress, activeTracks, lastError, detectorSource])

  const loadOwnerProfile = async () => {
    const data = await getIdentityProfiles()
    if (!data.ok) {
      setLastError(data.error_type || data.message || 'identity_profile_load_failed')
      return
    }
    const owner = data.profiles_by_id?.owner || (Array.isArray(data.profiles) ? data.profiles.find((p: any) => p.id === 'owner') : null)
    setOwnerProfile(owner || null)
  }

  useEffect(() => {
    loadOwnerProfile()
    return () => {
      stopCamera()
    }
  }, [])

  const startCamera = async (reason = 'user_command'): Promise<{ ok: boolean; error?: string }> => {
    if ((cameraOn || streamIsLive()) && streamRef.current?.active) {
      return { ok: true, error: 'already_active' }
    }
    if (cameraStartPromiseRef.current) return cameraStartPromiseRef.current
    cameraStartPromiseRef.current = (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' }, audio: false })
        streamRef.current = stream
        if (videoRef.current) {
          videoRef.current.srcObject = stream
          await videoRef.current.play()
        }
        attachPreviewVideoElement(previewVideoRef.current, stream)
        setCameraOn(true)
        setLastError('')
        onLog?.(`[VISION] camera_on ${reason}`.trim())
        return { ok: true }
      } catch (e: any) {
        const message = e?.name === 'NotAllowedError' ? 'permission_denied' : e?.message || 'camera_permission_denied'
        setLastError(message)
        setTrackingError(safeTrackingError(message === 'permission_denied' ? 'permission_denied' : 'camera_stream_missing', 'camera_stream', 'Camera stream is not available'))
        onLog?.(`[VISION] camera_failed ${message}`.trim())
        return { ok: false, error: message }
      } finally {
        cameraStartPromiseRef.current = null
      }
    })()
    return cameraStartPromiseRef.current
  }

  const stopTracking = (reason = 'user_command'): { ok: boolean } => {
    if (timerRef.current) window.clearInterval(timerRef.current)
    if (verifyTimerRef.current) window.clearInterval(verifyTimerRef.current)
    timerRef.current = null
    verifyTimerRef.current = null
    setTrackingState('off')
    setTrackingStartedAt(null)
    setTrackingError(null)
    setTracks([])
    clearLiveIdentity('tracking_stopped')
    onLog?.(`[VISION] tracking_off ${reason}`.trim())
    return { ok: true }
  }

  const stopCamera = (reason = 'user_command'): { ok: boolean } => {
    stopTracking('camera_stopped')
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => {
        try { track.stop() } catch { /* noop */ }
      })
      streamRef.current = null
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null
    }
    if (previewVideoRef.current) {
      previewVideoRef.current.srcObject = null
    }
    setCameraOn(false)
    setOwnerVerified(false)
    setTracks([])
    onLog?.(`[VISION] camera_off ${reason}`.trim())
    return { ok: true }
  }

  const trackingTick = async (): Promise<{ ok: boolean; error?: TrackingErrorInfo }> => {
    if (pausedRef.current) return { ok: true }
    if (!streamIsLive()) {
      const err = safeTrackingError('camera_stream_missing', 'camera_stream', 'Camera stream is missing')
      setLastError(err.code)
      setTrackingState('error')
      setTrackingError(err)
      clearLiveIdentity('tracking_not_active')
      onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
      return { ok: false, error: err }
    }
    if (!videoRef.current) {
      const err = safeTrackingError('video_element_missing', 'camera_stream', 'Video element is not available')
      setLastError(err.code)
      setTrackingState('error')
      setTrackingError(err)
      clearLiveIdentity('tracking_not_active')
      onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
      return { ok: false, error: err }
    }
    if (!videoIsReady()) {
      const err = safeTrackingError(videoRef.current.videoWidth === 0 || videoRef.current.videoHeight === 0 ? 'video_dimensions_zero' : 'camera_video_not_ready', 'camera_stream', 'Video is not ready for tracking')
      setLastError(err.code)
      setTrackingState('error')
      setTrackingError(err)
      clearLiveIdentity('tracking_not_active')
      onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
      return { ok: false, error: err }
    }
    trackerRef.current = trackerRef.current || new PersonTracker()
    const result = await trackerRef.current.detect(videoRef.current, ownerVerified)
    const runtime = getVisionModelLoadState().peopleTrackingRuntime || null
    setPeopleTrackingRuntime(runtime)
    setTracks(result.tracks)
    const visibleCount = visibleActiveTracks(result.tracks).length
    lastKnownPeopleCountRef.current = visibleCount
    if (visibleCount > 0) lastPersonSeenAtRef.current = new Date().toISOString()
    setDetectorSource(result.detectorSource)
    if (!result.ok) {
      const modelStage: TrackingErrorStage = result.detectorSource === 'none' ? 'person_detector_init' : 'tracking_loop'
      const exactCode = runtime?.error_code || String(result.message || result.status || 'people_tracking_loop_exception')
      const err = safeTrackingError(exactCode, modelStage, 'Tracking detector is unavailable')
      setLastError(err.code)
      setTrackingState('error')
      setTrackingError(err)
      clearLiveIdentity('tracking_not_active')
      onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
      return { ok: false, error: err }
    }
    setLastError('')
    setTrackingError(null)
    return { ok: true }
  }

  const verifyOwnerFromCamera = async () => {
    if (!videoRef.current || !ownerProfile?.face_enrolled || pausedRef.current) return
    const result = await extractFaceDescriptorFromVideo(videoRef.current)
    if (!result.ok || !result.descriptor) return
    const verified = await verifyOwnerFace({ embedding: result.descriptor, adapter: 'face_api_descriptor_v1' })
    if (verified.ok && verified.matched) {
      setOwnerVerified(true)
      lastOwnerVerifiedAtRef.current = new Date().toISOString()
      onLog?.('[IDENTITY] owner_verified')
    }
  }

  const startTracking = async (reason = 'user_command'): Promise<{ ok: boolean; error?: string }> => {
    if (trackingState === 'active' && timerRef.current) {
      onLog?.(`[VISION] tracking_on_deduped ${reason}`.trim())
      return { ok: true }
    }
    if (trackingStartPromiseRef.current) return trackingStartPromiseRef.current
    trackingStartPromiseRef.current = (async () => {
      setTrackingState('starting')
      setTrackingError(null)
      onLog?.(`[VISION] tracking_starting ${reason}`.trim())
      if (!cameraOn || !streamIsLive()) {
        const camRes = await startCamera(reason)
        if (!camRes.ok) {
          const err = safeTrackingError(camRes.error || 'camera_stream_missing', 'camera_stream', 'Camera is active, but tracking could not start')
          setTrackingState('error')
          setTrackingError(err)
          clearLiveIdentity('tracking_not_active')
          onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
          return { ok: false, error: err.code }
        }
      }
      const videoReady = await waitForVideoReady()
      if (!videoReady.ok) {
        const err = videoReady.error || safeTrackingError('camera_video_not_ready', 'camera_stream', 'Video is not ready for tracking')
        setTrackingState('error')
        setTrackingError(err)
        clearLiveIdentity('tracking_not_active')
        onLog?.(`[AURA PEOPLE TRACKING] startup_failed stage=${err.stage} code=${err.code}`)
        return { ok: false, error: err.code }
      }
      const assets = await getPeopleTrackingAssetDiagnostics()
      setPeopleTrackingRuntime(getVisionModelLoadState().peopleTrackingRuntime || null)
      onLog?.(`[AURA PEOPLE TRACKING] asset_check model_paths_checked=${assets.model_paths_checked.join(',')} missing_paths=${assets.missing_paths.join(',') || 'none'}`)
      const firstTick = await Promise.race([
        trackingTick(),
        new Promise<{ ok: boolean; error?: TrackingErrorInfo }>((resolve) => {
          window.setTimeout(() => resolve({ ok: false, error: safeTrackingError('startup_timeout', 'tracking_loop', 'Tracking did not confirm active') }), Math.max(TRACKING_START_VERIFY_TIMEOUT_MS, PEOPLE_TRACKING_MODEL_READY_TIMEOUT_MS))
        }),
      ])
      if (!firstTick.ok) {
        if (timerRef.current) window.clearInterval(timerRef.current)
        timerRef.current = null
        if (verifyTimerRef.current) window.clearInterval(verifyTimerRef.current)
        verifyTimerRef.current = null
        const code = firstTick.error?.code || 'people_tracking_loop_exception'
        setTrackingState('error')
        setTrackingError(firstTick.error || safeTrackingError(code, 'tracking_loop', 'Tracking did not confirm active'))
        clearLiveIdentity('tracking_not_active')
        onLog?.(`[AURA ACTION] tracking.start failed reason=${code}`)
        return { ok: false, error: code }
      }
      setTrackingState('active')
      setTrackingStartedAt(new Date().toISOString())
      if (!timerRef.current) {
        timerRef.current = window.setInterval(() => trackingTick().catch(() => {
          const err = safeTrackingError('people_tracking_loop_exception', 'tracking_loop', 'Tracking loop failed')
          setLastError(err.code)
          setTrackingState('error')
          setTrackingError(err)
          clearLiveIdentity('tracking_not_active')
          onLog?.(`[VISION] tracking_error reason=${err.code}`.trim())
        }), 800)
      }
      if (ownerProfile?.face_enrolled && !verifyTimerRef.current) {
        verifyTimerRef.current = window.setInterval(() => verifyOwnerFromCamera().catch(() => undefined), 2500)
      }
      onLog?.(`[VISION] tracking_active ${reason}`.trim())
      if (visibleActiveTracks(tracks).length === 0) onLog?.('[VISION] detection_pending')
      return { ok: true }
    })().finally(() => {
      trackingStartPromiseRef.current = null
    })
    return trackingStartPromiseRef.current
  }

  const pauseTracking = () => {
    pausedRef.current = true
  }

  const resumeTracking = () => {
    pausedRef.current = false
  }

  const verifyOwnerIfAvailable = async (): Promise<{ ok: boolean; skipped?: boolean; error?: string }> => {
    if (!ownerProfile?.face_enrolled) return { ok: true, skipped: true }
    if (!cameraOn || !streamIsLive()) return { ok: false, error: 'camera_not_active' }
    try {
      await verifyOwnerFromCamera()
      return { ok: true }
    } catch (e: any) {
      return { ok: false, error: e?.message || 'owner_verification_failed' }
    }
  }

  const attachPreviewVideoElement = (video: HTMLVideoElement | null, streamOverride?: MediaStream | null) => {
    previewVideoRef.current = video
    if (!video) return
    const stream = streamOverride || (videoRef.current?.srcObject as MediaStream | null) || streamRef.current
    if (!stream || !stream.active) {
      video.srcObject = null
      return
    }
    if (video.srcObject !== stream) {
      video.srcObject = stream
    }
    video.muted = true
    video.playsInline = true
    video.autoplay = true
    video.play().catch(() => undefined)
  }

  return {
    videoRef,
    cameraOn,
    tracking,
    trackingState,
    trackingStartedAt,
    trackingError,
    ownerVerified,
    ownerProfile,
    tracks: activeTracks,
    cameraStatus: normalizeCameraStatus(cameraOn, lastError && !cameraOn ? lastError : ''),
    trackingStatus: trackingState === 'error' ? 'error' : normalizeTrackingStatus(tracking, '', trackingStarting),
    ownerStatus: ownerVerified ? 'verified' : ownerProfile?.face_enrolled ? 'face enrolled' : 'not enrolled',
    peopleCount: activeTracks.length,
    detectorSource,
    peopleTrackingRuntime,
    lastError,
    identityContext: latestIdentityContextRef.current,
    visionContext: latestVisionContextRef.current,
    getIdentityContext: () => latestIdentityContextRef.current,
    getVisionContext: () => latestVisionContextRef.current,
    pauseTracking,
    resumeTracking,
    startCamera,
    stopCamera,
    startTracking,
    stopTracking,
    loadOwnerProfile,
    verifyOwnerIfAvailable,
    attachPreviewVideoElement,
  }
}
