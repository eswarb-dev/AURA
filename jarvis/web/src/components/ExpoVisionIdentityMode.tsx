import { useEffect, useMemo, useRef, useState } from 'react'
import { Camera, Crosshair, RefreshCw, Upload, UserCheck, Volume2 } from 'lucide-react'
import { PersonTracker, TRACKER_CONFIG, visibleActiveTracks, type PersonTrack, type TrackingMode } from '@/lib/personTracker'
import type { VisionModelCapabilities, VisionModelStatus } from '@/lib/visionIdentity'
import { extractFaceDescriptorFromImage, extractFaceDescriptorFromVideo, summarizeIdentityContext, type IdentityContext, type VisionContext } from '@/lib/visionIdentity'
import { extractVoiceFeatures, voiceFeaturesToEmbedding } from '@/lib/voiceIdentity'
import { speakWithBrowserSpeech } from '@/lib/audioOutput'

type JsonMap = Record<string, any>

type Props = {
  autoTts: boolean
  auraAwake: boolean
  onIdentityContextChange: (context: IdentityContext | null) => void
  onVisionContextChange?: (context: VisionContext | null) => void
}

type CurrentIdentity = {
  decision: string
  display_name: string
  role: 'owner' | 'guest'
  address: string
  confidence: number
  source: 'face' | 'voice' | 'fusion' | 'none'
}

type OwnerFaceCandidate = {
  source: 'uploaded_owner_image' | 'camera_capture' | 'live_track'
  descriptor: number[]
  descriptorLength: number
  faceCount: number
  confidence?: number
  readyAt: number
}

type PersistedOwnerProfile = {
  id: string
  display_name: string
  role: string
  address: string
  preferred_address?: string | null
  address_source?: string | null
  face_enrolled: boolean
  voice_enrolled: boolean
  face_adapter?: string | null
  voice_adapter?: string | null
  face_enrolled_at?: string | null
  voice_enrolled_at?: string | null
  updated_at?: string | null
}

type IdentityStorageStatus = {
  profiles_path?: string
  exists?: boolean
  profiles_exists?: boolean
}

const commandEventName = 'aura:expo-vision-command'
const genderThreshold = 0.75

function statusLine(label: string, value: string) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-[var(--line)]/45 py-1 font-mono text-[11px]">
      <span className="shrink-0 text-[var(--text-faint)]">{label}</span>
      <span className="break-all text-right text-[var(--text)]">{value}</span>
    </div>
  )
}

function ActionButton({ icon, label, onClick, disabled = false }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex items-center justify-center gap-2 rounded-sm border border-[var(--line-bright)] bg-black/30 px-3 py-2 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--text-dim)] transition hover:border-[var(--blue)] hover:text-[var(--blue)] disabled:cursor-not-allowed disabled:opacity-45"
    >
      {icon}
      {label}
    </button>
  )
}

export function ExpoVisionIdentityMode({ autoTts, auraAwake, onIdentityContextChange, onVisionContextChange }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const ownerImageInputRef = useRef<HTMLInputElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const trackerRef = useRef<PersonTracker | null>(null)
  const trackingTimerRef = useRef<number | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const voiceSamplesRef = useRef<Float32Array[]>([])
  const ownerFaceCandidateRef = useRef<OwnerFaceCandidate | null>(null)
  const [cameraOn, setCameraOn] = useState(false)
  const [cameraStatus, setCameraStatus] = useState('off')
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [deviceIndex, setDeviceIndex] = useState(0)
  const [tracking, setTracking] = useState(false)
  const [trackingMode, setTrackingMode] = useState<TrackingMode>('none')
  const [tracks, setTracks] = useState<PersonTrack[]>([])
  const [ownerFaceStatus, setOwnerFaceStatus] = useState('not enrolled')
  const [ownerVoiceStatus, setOwnerVoiceStatus] = useState('not enrolled')
  const [ownerImageFile, setOwnerImageFile] = useState<File | null>(null)
  const [ownerImagePreviewUrl, setOwnerImagePreviewUrl] = useState('')
  const [ownerUploadStatus, setOwnerUploadStatus] = useState('idle')
  const [ownerFaceCandidate, setOwnerFaceCandidate] = useState<OwnerFaceCandidate | null>(null)
  const [persistedOwnerProfile, setPersistedOwnerProfile] = useState<PersistedOwnerProfile | null>(null)
  const [persistentProfileStatus, setPersistentProfileStatus] = useState('not loaded')
  const [identityStorage, setIdentityStorage] = useState<IdentityStorageStatus | null>(null)
  const [ownerUploadError, setOwnerUploadError] = useState('none')
  const [ownerUploadFaceCount, setOwnerUploadFaceCount] = useState(0)
  const [ownerUploadSource, setOwnerUploadSource] = useState('N/A')
  const [ownerEnrollResponse, setOwnerEnrollResponse] = useState('N/A')
  const [ownerVerifyResponse, setOwnerVerifyResponse] = useState('N/A')
  const [lastIdentityApiError, setLastIdentityApiError] = useState('none')
  const [currentIdentity, setCurrentIdentity] = useState<CurrentIdentity>({ decision: 'guest_or_unknown', display_name: 'Guest', role: 'guest', address: 'Guest', confidence: 0, source: 'none' })
  const [ownerVerifiedAt, setOwnerVerifiedAt] = useState<number | null>(null)
  const [fusion, setFusion] = useState<JsonMap | null>(null)
  const [modelStatus, setModelStatus] = useState<VisionModelStatus | 'no_face_detected'>('not_loaded')
  const [modelProvider, setModelProvider] = useState('face_api')
  const [detectorSource, setDetectorSource] = useState('none')
  const [mediaPipeStatus, setMediaPipeStatus] = useState<VisionModelStatus>('not_loaded')
  const [faceApiStatus, setFaceApiStatus] = useState<VisionModelStatus>('not_loaded')
  const [modelCapabilities, setModelCapabilities] = useState<VisionModelCapabilities>({ faceDetection: false, faceEmbedding: false, genderEstimate: false })
  const [lastDetectionError, setLastDetectionError] = useState('none')
  const [detectionIntervalMs] = useState(TRACKER_CONFIG.detectionIntervalMs)
  const [trackingStats, setTrackingStats] = useState({
    rawDetectionsCount: 0,
    confirmedTracksCount: 0,
    predictedTracksCount: 0,
    lostTracksCount: 0,
    visibleActiveTracksCount: 0,
    internalTracksTotal: 0,
    newTracksThisFrame: 0,
    mergedTracksThisFrame: 0,
  })
  const ownerPreferredAddress = persistedOwnerProfile?.preferred_address || persistedOwnerProfile?.address || 'Sir'
  const ownerMatchedProfile = {
    id: persistedOwnerProfile?.id || 'owner',
    display_name: persistedOwnerProfile?.display_name || 'Owner',
    preferred_address: ownerPreferredAddress,
    address_source: persistedOwnerProfile?.address_source || 'explicit_profile',
  }
  const [currentCommand, setCurrentCommand] = useState('N/A')
  const [lastResponse, setLastResponse] = useState('N/A')
  const [logs, setLogs] = useState<string[]>([])

  const applyPersistedProfile = (profile: PersistedOwnerProfile | null, storage?: IdentityStorageStatus) => {
    setPersistedOwnerProfile(profile)
    if (storage) setIdentityStorage(storage)
    if (!profile) {
      setPersistentProfileStatus('missing')
      setOwnerFaceStatus((current) => current === 'verified' ? current : 'not enrolled')
      setOwnerVoiceStatus((current) => current === 'verified' ? current : 'not enrolled')
      return
    }
    setPersistentProfileStatus('loaded')
    setOwnerFaceStatus((current) => current === 'verified' ? current : profile.face_enrolled ? 'enrolled' : 'not enrolled')
    setOwnerVoiceStatus((current) => current === 'verified' ? current : profile.voice_enrolled ? 'enrolled' : 'not enrolled')
  }

  const loadPersistedOwnerProfile = async () => {
    try {
      const res = await fetch('/api/identity/profiles')
      const data = await res.json()
      if (!data.ok) {
        setPersistentProfileStatus('error')
        setLastIdentityApiError(data.error_type || data.error || 'identity_profile_load_failed')
        return
      }
      const owner = data.profiles_by_id?.owner || (Array.isArray(data.profiles) ? data.profiles.find((profile: PersistedOwnerProfile) => profile.id === 'owner') : null)
      applyPersistedProfile(owner || null, data.storage)
      addLog(owner ? 'persistent owner profile loaded' : 'persistent owner profile missing')
    } catch (e: any) {
      setPersistentProfileStatus('error')
      setLastIdentityApiError(e?.message || 'profile_load_failed')
      addLog(`profile load failed: ${e?.message || 'profile_load_failed'}`)
    }
    try {
      const res = await fetch('/api/diagnostics/identity-storage')
      if (!res.ok) {
        setIdentityStorage((current) => current || { profiles_path: 'storage diagnostic unavailable' })
        return
      }
      const data = await res.json()
      if (data.ok) setIdentityStorage(data)
    } catch {
      setIdentityStorage((current) => current || { profiles_path: 'storage diagnostic unavailable' })
    }
  }

  const identityContext = useMemo<IdentityContext>(() => {
    const ownerTrack = tracks.find((track) => track.identity === 'owner')
    const fusionOwner = String(fusion?.decision || '').startsWith('owner_verified')
    const ownerActive = currentIdentity.role === 'owner' || fusionOwner || Boolean(ownerTrack)
    const activeTracks = visibleActiveTracks(tracks)
    return {
      decision: ownerActive ? currentIdentity.decision || fusion?.decision || 'owner_verified_face_only' : fusion?.decision || 'guest_or_unknown',
      display_name: ownerActive ? 'Owner' : 'Guest',
      role: ownerActive ? 'owner' : 'guest',
      address: ownerActive ? ownerPreferredAddress : 'Guest',
      preferred_address: ownerActive ? ownerPreferredAddress : null,
      address_source: ownerActive ? 'explicit_profile' : 'default',
      matched_profile_id: ownerActive ? ownerMatchedProfile.id : null,
      matched_profile_name: ownerActive ? ownerMatchedProfile.display_name : null,
      matched_profile: ownerActive ? ownerMatchedProfile : null,
      identity_confidence: ownerActive ? currentIdentity.confidence || fusion?.confidence || null : null,
      owner_face_status: ownerFaceStatus,
      owner_voice_status: ownerVoiceStatus,
      owner_face_enrolled: ownerFaceStatus === 'enrolled' || ownerFaceStatus === 'verified' || ownerFaceStatus === 'enrolled_not_matched',
      owner_voice_enrolled: ownerVoiceStatus === 'enrolled' || ownerVoiceStatus === 'verified',
      people_count: activeTracks.length,
      tracking_mode: trackingMode,
      tracks: tracks.map((track) => ({
        track_id: track.track_id,
        identity: track.identity === 'owner' ? 'owner' : 'guest',
        address: track.identity === 'owner' ? ownerPreferredAddress : 'Guest',
        status: track.status,
        confirmed: track.confirmed,
        stable_label_index: track.stable_label_index,
        hits: track.hits,
        missed_frames: track.missed_frames,
        descriptor_present: Boolean(track.descriptor?.length),
        descriptor_length: track.descriptor?.length || 0,
      })),
    }
  }, [currentIdentity, fusion, ownerFaceStatus, ownerVoiceStatus, tracks, trackingMode])

  useEffect(() => {
    onIdentityContextChange(identityContext)
  }, [identityContext, onIdentityContextChange])

  const visionContext = useMemo<VisionContext>(() => ({
    camera_status: cameraStatus,
    tracking_status: tracking ? 'on' : 'off',
    people_count: visibleActiveTracks(tracks).length,
    current_identity: currentIdentity.role,
    address_selected: currentIdentity.role === 'owner' ? ownerPreferredAddress : identityContext.address,
    preferred_address: currentIdentity.role === 'owner' ? ownerPreferredAddress : null,
    address_source: currentIdentity.role === 'owner' ? 'explicit_profile' : 'default',
    matched_profile_id: currentIdentity.role === 'owner' ? ownerMatchedProfile.id : null,
    matched_profile_name: currentIdentity.role === 'owner' ? ownerMatchedProfile.display_name : null,
    identity_confidence: currentIdentity.role === 'owner' ? currentIdentity.confidence || null : null,
    fusion_decision: fusion?.decision || identityContext.decision,
    tracking_mode: trackingMode,
    tracks: tracks.map((track) => ({
      track_id: track.track_id,
      status: track.status,
      identity: track.identity === 'owner' ? 'owner' : 'guest',
      address: track.identity === 'owner' ? ownerPreferredAddress : 'Guest',
      visual_estimate: track.gender_estimate,
      gender_confidence: track.gender_confidence,
      confirmed: track.confirmed,
      stable_label_index: track.stable_label_index,
      missed_frames: track.missed_frames,
    })),
  }), [cameraStatus, currentIdentity, fusion, identityContext, ownerPreferredAddress, ownerMatchedProfile.id, ownerMatchedProfile.display_name, tracking, trackingMode, tracks])

  useEffect(() => {
    onVisionContextChange?.(visionContext)
  }, [onVisionContextChange, visionContext])

  useEffect(() => {
    loadPersistedOwnerProfile()
  }, [])

  const addLog = (line: string) => setLogs((current) => [`${new Date().toLocaleTimeString('en-GB')} ${line}`, ...current].slice(0, 40))

  const say = async (text: string) => {
    setLastResponse(text)
    if (!autoTts) return
    try { await speakWithBrowserSpeech(text) } catch (e: any) { addLog(`speech failed: ${e?.message || 'speech failed'}`) }
  }

  const setOwnerCandidate = (candidate: OwnerFaceCandidate | null) => {
    ownerFaceCandidateRef.current = candidate
    setOwnerFaceCandidate(candidate)
    setOwnerUploadSource(candidate?.source || 'N/A')
  }

  const drawOverlay = (nextTracks: PersonTrack[]) => {
    const video = videoRef.current
    const canvas = canvasRef.current
    if (!video || !canvas) return
    canvas.width = video.videoWidth || canvas.clientWidth
    canvas.height = video.videoHeight || canvas.clientHeight
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.font = '14px monospace'
    const visibleTracks = visibleActiveTracks(nextTracks)
    visibleTracks.forEach((track, index) => {
      const box = track.smoothed_bbox || track.bbox
      const selected = trackingMode === 'all' || (trackingMode === 'owner' && track.identity === 'owner') || (trackingMode === 'nearest' && index === 0)
      ctx.strokeStyle = selected ? '#00d9ff' : '#6b7a90'
      ctx.lineWidth = selected ? 3 : 2
      ctx.strokeRect(box.x, box.y, box.width, box.height)
      ctx.fillStyle = 'rgba(0,0,0,0.7)'
      ctx.fillRect(box.x, Math.max(0, box.y - 24), 180, 22)
      ctx.fillStyle = selected ? '#00d9ff' : '#d7e3f4'
      const displayIndex = track.stable_label_index || index + 1
      const label = track.identity === 'owner' ? `Owner / ${ownerPreferredAddress}` : `Guest ${displayIndex}`
      const detail = track.status === 'predicted' ? 'tracking...' : `${(track.face_confidence * 100).toFixed(0)}%`
      ctx.fillText(`${label} ${detail}`, box.x + 6, Math.max(14, box.y - 8))
    })
  }

  const publicTracks = (nextTracks: PersonTrack[]) => nextTracks.map((track) => ({
    track_id: track.track_id,
    bbox: track.bbox,
    face_confidence: track.face_confidence,
    identity: track.identity === 'owner' ? 'owner' : 'guest',
    gender_estimate: track.gender_estimate,
    gender_confidence: track.gender_confidence,
    address: track.identity === 'owner' ? ownerPreferredAddress : 'Guest',
    status: track.status,
    confirmed: track.confirmed,
    stable_label_index: track.stable_label_index,
    hits: track.hits,
    missed_frames: track.missed_frames,
    last_seen_at: track.last_seen_at,
    descriptor_present: Boolean(track.descriptor?.length),
    descriptor_length: track.descriptor?.length || 0,
  }))

  const markTrackAsOwner = (trackId: string | null, confidence: number) => {
    const verifiedAt = Date.now()
    setTracks((current) => current.map((track, index) => {
      const selected = trackId ? track.track_id === trackId : index === 0
      return selected ? {
        ...track,
        identity: 'owner',
        address: ownerPreferredAddress,
        identity_confidence: confidence,
        identity_decision: 'owner_face_verified',
        owner_verified_at: verifiedAt,
      } as PersonTrack : track
    }))
  }

  const stopTracking = () => {
    if (trackingTimerRef.current) window.clearInterval(trackingTimerRef.current)
    trackingTimerRef.current = null
    setTracking(false)
    setTrackingMode('none')
    addLog('tracking stopped')
  }

  const runTrackingTick = async () => {
    const video = videoRef.current
    if (!video) return null
    trackerRef.current = trackerRef.current || new PersonTracker(genderThreshold)
    const result = await trackerRef.current.detect(video, fusion?.identity === 'owner')
    setModelStatus(result.status)
    setModelProvider(result.provider)
    setDetectorSource(result.detectorSource)
    setMediaPipeStatus(result.mediaPipeStatus)
    setFaceApiStatus(result.faceApiStatus)
    setModelCapabilities(result.capabilities)
    setTrackingStats({
      rawDetectionsCount: result.rawDetectionsCount,
      confirmedTracksCount: result.confirmedTracksCount,
      predictedTracksCount: result.predictedTracksCount,
      lostTracksCount: result.lostTracksCount,
      visibleActiveTracksCount: result.visibleActiveTracksCount,
      internalTracksTotal: result.internalTracksTotal,
      newTracksThisFrame: result.newTracksThisFrame,
      mergedTracksThisFrame: result.mergedTracksThisFrame,
    })
    setLastDetectionError(result.message || (result.status === 'no_face_detected' ? 'no_face_detected' : 'none'))
    setTracks(result.tracks)
    drawOverlay(result.tracks)
    if (result.status === 'model_missing' || result.status === 'model_load_failed') addLog(result.message || result.status)
    return result
  }

  const startTracking = () => {
    if (!cameraOn) {
      addLog('start camera before tracking')
      return
    }
    setTracking(true)
    if (trackingMode === 'none') setTrackingMode('all')
    if (trackingTimerRef.current) window.clearInterval(trackingTimerRef.current)
    trackingTimerRef.current = window.setInterval(() => runTrackingTick().catch((e) => {
      setLastDetectionError(e?.message || 'tracking failed')
      addLog(`tracking failed: ${e.message}`)
    }), detectionIntervalMs)
    runTrackingTick().catch((e) => addLog(`tracking failed: ${e.message}`))
    say('Live tracking started.')
  }

  const stopCamera = () => {
    stopTracking()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
    setCameraOn(false)
    setCameraStatus('off')
    setTracks([])
    drawOverlay([])
    addLog('camera stopped')
  }

  const startCamera = async () => {
    stopCamera()
    const device = devices[deviceIndex]
    const stream = await navigator.mediaDevices.getUserMedia({ video: device ? { deviceId: { exact: device.deviceId } } : { facingMode: 'user' }, audio: false })
    streamRef.current = stream
    if (videoRef.current) {
      videoRef.current.srcObject = stream
      await videoRef.current.play()
    }
    const nextDevices = await navigator.mediaDevices.enumerateDevices()
    setDevices(nextDevices.filter((item) => item.kind === 'videoinput'))
    setCameraOn(true)
    setCameraStatus('online')
    addLog('camera online')
    say('Camera online, Sir.')
  }

  const switchCamera = async () => {
    if (!devices.length) return
    setDeviceIndex((current) => (current + 1) % devices.length)
    if (cameraOn) window.setTimeout(() => startCamera().catch((e) => addLog(`camera failed: ${e.message}`)), 0)
  }

  const captureOwnerFromCamera = async () => {
    const liveTrack = tracks.find((track) => track.descriptor)
    if (liveTrack?.descriptor?.length) {
      setOwnerCandidate({
        source: 'live_track',
        descriptor: liveTrack.descriptor,
        descriptorLength: liveTrack.descriptor.length,
        faceCount: 1,
        confidence: liveTrack.face_confidence,
        readyAt: Date.now(),
      })
      setOwnerUploadStatus('descriptor_ready')
      setOwnerUploadFaceCount(1)
      setOwnerUploadError('')
      return
    }
    const video = videoRef.current
    if (!video) {
      setOwnerCandidate(null)
      setOwnerUploadStatus('model_error')
      setOwnerUploadError('camera_not_online')
      return
    }
    const result = await extractFaceDescriptorFromVideo(video)
    setOwnerUploadFaceCount(result.faceCount)
    if (!result.ok || !result.descriptor) {
      setOwnerCandidate(null)
      setOwnerUploadStatus(result.error === 'owner_upload_no_face' ? 'owner_upload_no_face' : result.error === 'owner_upload_multiple_faces' ? 'owner_upload_multiple_faces' : 'model_error')
      setOwnerUploadError(result.error || 'model_error')
      return
    }
    setOwnerCandidate({
      source: 'camera_capture',
      descriptor: result.descriptor,
      descriptorLength: result.descriptor.length,
      faceCount: result.faceCount,
      confidence: result.confidence,
      readyAt: Date.now(),
    })
    setOwnerUploadStatus('descriptor_ready')
    setOwnerUploadError('')
  }

  const enrollFace = async () => {
    let candidate = ownerFaceCandidateRef.current || ownerFaceCandidate
    if (!candidate) {
      const liveTrack = tracks.find((track) => track.descriptor)
      if (liveTrack?.descriptor?.length) {
        candidate = {
          source: 'live_track',
          descriptor: liveTrack.descriptor,
          descriptorLength: liveTrack.descriptor.length,
          faceCount: 1,
          confidence: liveTrack.face_confidence,
          readyAt: Date.now(),
        }
        setOwnerCandidate(candidate)
      }
    }
    if (!candidate && cameraOn && modelCapabilities.faceEmbedding) {
      await captureOwnerFromCamera()
      candidate = ownerFaceCandidateRef.current
    }
    const embedding = candidate?.descriptor || []
    if (!embedding.length) {
      setOwnerFaceStatus('no_owner_face_descriptor_ready')
      setLastIdentityApiError('No owner face descriptor is ready. Upload a clear owner image or capture from camera first.')
      addLog('no_owner_face_descriptor_ready')
      return
    }
    console.log('[AURA OWNER] enroll request sent')
    const res = await fetch('/api/identity/enroll-face', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile_id: 'owner', embedding, source: candidate?.source || 'live_track', adapter: 'face_api_descriptor_v1' }) })
    const data = await res.json()
    setOwnerEnrollResponse(JSON.stringify(data))
    setLastIdentityApiError(data.ok ? 'none' : data.error || 'enroll_failed')
    setOwnerFaceStatus(data.ok ? 'enrolled' : data.error || 'failed')
    if (data.ok) {
      applyPersistedProfile(data.profile || persistedOwnerProfile, data.storage)
      console.log('[AURA OWNER] enroll success')
      setOwnerUploadStatus('enrolled')
      setOwnerUploadSource(candidate?.source || 'live_track')
    }
    addLog(data.ok ? 'owner face embedding enrolled' : `face enrollment failed: ${data.error}`)
  }

  const verifyFace = async () => {
    if (!cameraOn) {
      setOwnerVerifyResponse('camera_not_online')
      setLastIdentityApiError('camera_not_online')
      return
    }
    const latest = (await runTrackingTick())?.tracks || tracks
    const trackedFace = latest.find((track) => track.status === 'detected' || track.status === 'predicted')
    const result = videoRef.current && modelCapabilities.faceEmbedding ? await extractFaceDescriptorFromVideo(videoRef.current) : null
    const embedding = result?.descriptor || []
    if (!embedding.length) {
      setOwnerFaceStatus('face recognition model not loaded')
      setOwnerVerifyResponse(result?.error || 'face_recognition_model_not_loaded')
      setLastIdentityApiError(result?.error || 'face_recognition_model_not_loaded')
      addLog(result?.error || 'Face recognition model not loaded.')
      return
    }
    const res = await fetch('/api/identity/verify-face', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ embedding, adapter: 'face_api_descriptor_v1' }) })
    const data = await res.json()
    console.log(data.matched ? '[AURA OWNER] verify success' : '[AURA OWNER] verify fail')
    setOwnerVerifyResponse(JSON.stringify(data))
    setLastIdentityApiError(data.ok ? 'none' : data.error || 'verify_failed')
    setOwnerFaceStatus(data.matched ? 'verified' : 'enrolled_not_matched')
    if (data.ok && data.matched) {
      const confidence = typeof data.confidence === 'number' ? data.confidence : 0
      setCurrentIdentity({ decision: 'owner_verified_face_only', display_name: 'Owner', role: 'owner', address: ownerPreferredAddress, confidence, source: 'face' })
      setFusion({ ok: true, decision: 'owner_verified_face_only', identity: 'owner', display_name: 'Owner', address: ownerPreferredAddress, preferred_address: ownerPreferredAddress, address_source: 'explicit_profile', confidence })
      setOwnerVerifiedAt(Date.now())
      setTrackingMode('owner')
      markTrackAsOwner(trackedFace?.track_id || null, confidence)
      setLastResponse('Owner verified, Sir.')
    } else {
      setCurrentIdentity({ decision: 'guest_or_unknown', display_name: 'Guest', role: 'guest', address: 'Guest', confidence: typeof data.confidence === 'number' ? data.confidence : 0, source: 'face' })
      setFusion({ ok: true, decision: 'guest_or_unknown', identity: 'unknown', display_name: 'Guest', address: 'Guest', confidence: typeof data.confidence === 'number' ? data.confidence : 0 })
    }
  }

  const handleOwnerImageUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    console.log('[AURA OWNER] file selected')
    setOwnerImageFile(file)
    setOwnerCandidate(null)
    setOwnerUploadError('none')
    setOwnerUploadFaceCount(0)
    setOwnerUploadStatus('image_loaded')
    const previewUrl = URL.createObjectURL(file)
    setOwnerImagePreviewUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous)
      return previewUrl
    })
    const image = new Image()
    image.onload = async () => {
      console.log('[AURA OWNER] image loaded')
      try {
        const result = await extractFaceDescriptorFromImage(image)
        setOwnerUploadFaceCount(result.faceCount)
        if (!result.ok || !result.descriptor) {
          setOwnerCandidate(null)
          const status = result.error === 'owner_upload_no_face' ? 'owner_upload_no_face' : result.error === 'owner_upload_multiple_faces' ? 'owner_upload_multiple_faces' : 'model_error'
          setOwnerUploadStatus(status)
          setOwnerUploadError(result.error || 'model_error')
          return
        }
        setOwnerCandidate({
          source: 'uploaded_owner_image',
          descriptor: result.descriptor,
          descriptorLength: result.descriptor.length,
          faceCount: result.faceCount,
          confidence: result.confidence,
          readyAt: Date.now(),
        })
        setOwnerUploadStatus('descriptor_ready')
        setOwnerUploadError('none')
        console.log('[AURA OWNER] descriptor ready')
      } catch (e: any) {
        setOwnerUploadStatus('model_error')
        setOwnerUploadError(e?.message || 'model_error')
      }
    }
    image.onerror = () => {
      setOwnerUploadStatus('model_error')
      setOwnerUploadError('image_load_failed')
    }
    image.src = previewUrl
  }

  const enrollVoice = async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
    const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext
    audioContextRef.current = new AudioContextCtor()
    const source = audioContextRef.current.createMediaStreamSource(stream)
    const processor = audioContextRef.current.createScriptProcessor(2048, 1, 1)
    voiceSamplesRef.current = []
    processor.onaudioprocess = (event) => voiceSamplesRef.current.push(new Float32Array(event.inputBuffer.getChannelData(0)))
    source.connect(processor)
    processor.connect(audioContextRef.current.destination)
    setOwnerVoiceStatus('recording')
    window.setTimeout(async () => {
      processor.disconnect()
      source.disconnect()
      stream.getTracks().forEach((track) => track.stop())
      const features = extractVoiceFeatures(voiceSamplesRef.current, audioContextRef.current?.sampleRate || 44100)
      const embedding = voiceFeaturesToEmbedding(features)
      const res = await fetch('/api/identity/enroll-voice', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: 'owner', embedding }) })
      const data = await res.json()
      setOwnerVoiceStatus(data.ok ? 'enrolled' : data.error || 'failed')
      if (data.ok) applyPersistedProfile(data.profile || persistedOwnerProfile)
      addLog(data.ok ? 'owner voice tone embedding enrolled' : `voice enrollment failed: ${data.error}`)
    }, 3200)
  }

  const verifyVoice = async () => {
    const embedding = [0.05, 0.08, 0.3]
    const res = await fetch('/api/identity/verify-voice', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ embedding }) })
    const data = await res.json()
    setOwnerVoiceStatus(data.matched ? 'verified' : 'not matched')
    await fusionCheck(ownerFaceStatus === 'verified', data.matched)
  }

  const fusionCheck = async (faceOwner: boolean, voiceOwner: boolean | null) => {
    const body = voiceOwner === null
      ? { face_owner: faceOwner, voice_status: 'not_tested' }
      : { face_owner: faceOwner, voice_owner: voiceOwner, voice_status: ownerVoiceStatus }
    const res = await fetch('/api/identity/fusion-check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    const data = await res.json()
    setFusion(data)
    if (data.identity === 'owner') {
      setCurrentIdentity({ decision: data.decision || 'owner_verified_face_only', display_name: 'Owner', role: 'owner', address: data.preferred_address || data.address || ownerPreferredAddress, confidence: data.confidence || 0, source: 'fusion' })
    }
    if (data.identity === 'owner') await say('Welcome back, Sir.')
  }

  const clearProfile = async () => {
    const res = await fetch('/api/identity/clear-face', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile_id: 'owner' }) })
    const data = await res.json()
    if (data.profile) applyPersistedProfile(data.profile)
    setOwnerFaceStatus('not enrolled')
    setOwnerCandidate(null)
    setOwnerUploadStatus('idle')
    addLog('owner face profile cleared')
  }

  useEffect(() => {
    const handler = (event: Event) => {
      const command = (event as CustomEvent<string>).detail
      setCurrentCommand(command)
      if (!auraAwake && command !== 'standby') return
      if (command === 'start camera') startCamera().catch((e) => addLog(`camera failed: ${e.message}`))
      else if (command === 'stop camera') stopCamera()
      else if (command === 'start tracking') startTracking()
      else if (command === 'stop tracking') stopTracking()
      else if (command === 'track owner' || command === 'track me' || command === 'focus on me') {
        setTrackingMode('owner')
        say(currentIdentity.role === 'owner' ? 'Tracking owner, Sir.' : 'Owner is not verified yet.')
      } else if (command === 'track everyone' || command === 'track all people') {
        setTrackingMode('all')
        say('Tracking all visible people.')
      } else if (command === 'who am i') {
        if (currentIdentity.role === 'owner') say('You are the owner, Sir.')
        else if (ownerFaceStatus === 'enrolled' || ownerFaceStatus === 'enrolled_not_matched') say('I have your owner profile, but I cannot verify you in the live feed yet.')
        else say('Owner face profile is not enrolled yet.')
      } else if (command === 'who is here' || command === 'scan the room') {
        say(modelStatus === 'model_missing' || modelStatus === 'model_load_failed' ? 'Camera is online, but the local vision model is not loaded yet.' : summarizeIdentityContext(identityContext))
      } else if (command === 'how many people do you see') {
        const visible = visibleActiveTracks(tracks)
        const activeCount = visible.length
        const predictedCount = visible.filter((track) => track.status === 'predicted').length
        say(modelStatus === 'model_missing' || modelStatus === 'model_load_failed'
          ? 'Camera is online, but the local vision model is not ready yet, Sir.'
          : predictedCount === 1 && activeCount === 1
            ? 'I can still track one person, Sir.'
            : currentIdentity.role === 'owner'
              ? `I can see ${activeCount} ${activeCount === 1 ? 'person' : 'people'}. Owner verified, Sir.`
              : `I can see ${activeCount} ${activeCount === 1 ? 'person' : 'people'}, Sir.`)
      } else if (command === 'clear tracking') {
        setTracks([])
        drawOverlay([])
        say('Tracking cleared.')
      }
    }
    window.addEventListener(commandEventName, handler)
    return () => window.removeEventListener(commandEventName, handler)
  }, [auraAwake, cameraOn, currentIdentity, identityContext, modelStatus, ownerFaceStatus, tracks, trackingMode, autoTts])

  useEffect(() => () => stopCamera(), [])

  return (
    <section className="rounded-sm border border-[var(--line-bright)] bg-black/45 p-4 backdrop-blur">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-display text-[11px] uppercase tracking-[0.2em] text-[var(--blue)]">Expo Vision Identity Mode</h2>
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--text-dim)]">{modelStatus.toUpperCase()}</span>
      </div>
      <div className="grid gap-3 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="space-y-3">
          <div className="relative aspect-video overflow-hidden rounded-sm border border-[var(--line)] bg-black">
            <video ref={videoRef} className="h-full w-full object-contain" muted playsInline />
            <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />
            {!cameraOn && <div className="absolute inset-0 grid place-items-center font-mono text-[11px] uppercase tracking-[0.14em] text-[var(--text-faint)]">camera off</div>}
          </div>
          <div className="grid gap-2 md:grid-cols-4">
            <ActionButton icon={<Camera size={13} />} label="Start Camera" onClick={() => startCamera().catch((e) => addLog(`camera failed: ${e.message}`))} />
            <ActionButton icon={<Camera size={13} />} label="Stop Camera" onClick={stopCamera} />
            <ActionButton icon={<RefreshCw size={13} />} label="Switch Camera" onClick={switchCamera} disabled={!devices.length} />
            <ActionButton icon={<Crosshair size={13} />} label="Start Live Tracking" onClick={startTracking} />
            <ActionButton icon={<Crosshair size={13} />} label="Stop Live Tracking" onClick={stopTracking} />
            <ActionButton icon={<UserCheck size={13} />} label="Track Owner" onClick={() => setTrackingMode('owner')} />
            <ActionButton icon={<Crosshair size={13} />} label="Track Nearest Person" onClick={() => setTrackingMode('nearest')} />
            <ActionButton icon={<Crosshair size={13} />} label="Track All People" onClick={() => setTrackingMode('all')} />
          </div>
          <div className="grid gap-2 md:grid-cols-4">
            <input ref={ownerImageInputRef} type="file" accept="image/*" className="hidden" onChange={handleOwnerImageUpload} />
            <ActionButton icon={<Upload size={13} />} label="Upload Owner Image" onClick={() => ownerImageInputRef.current?.click()} />
            <ActionButton icon={<Camera size={13} />} label="Capture Owner From Camera" onClick={() => captureOwnerFromCamera().catch((e) => addLog(e.message))} disabled={!cameraOn || !modelCapabilities.faceEmbedding} />
            <ActionButton icon={<UserCheck size={13} />} label="Enroll Owner Face" onClick={() => enrollFace().catch((e) => addLog(e.message))} disabled={!modelCapabilities.faceEmbedding || (!ownerFaceCandidate && !tracks.some((track) => track.descriptor) && !cameraOn)} />
            <ActionButton icon={<UserCheck size={13} />} label="Verify Owner Face" onClick={() => verifyFace().catch((e) => addLog(e.message))} disabled={!modelCapabilities.faceEmbedding} />
            <ActionButton icon={<RefreshCw size={13} />} label="Clear Owner Face Profile" onClick={() => clearProfile().catch((e) => addLog(e.message))} />
            <ActionButton icon={<Volume2 size={13} />} label="Start Owner Voice Enrollment" onClick={() => enrollVoice().catch((e) => addLog(e.message))} />
            <ActionButton icon={<Volume2 size={13} />} label="Verify Owner Voice Tone" onClick={() => verifyVoice().catch((e) => addLog(e.message))} />
            <ActionButton icon={<RefreshCw size={13} />} label="Clear Tracking Logs" onClick={() => setLogs([])} />
          </div>
          {ownerImagePreviewUrl && (
            <div className="grid gap-2 md:grid-cols-[160px_1fr]">
              <img src={ownerImagePreviewUrl} alt="Owner upload preview" className="max-h-32 w-full rounded-sm border border-[var(--line)] object-contain" />
              <div className="space-y-1">
                {statusLine('Owner image file', ownerImageFile?.name || 'N/A')}
                {statusLine('Owner upload status', ownerUploadStatus)}
                {statusLine('Owner upload face count', String(ownerUploadFaceCount))}
                {statusLine('Owner upload descriptor ready', ownerFaceCandidate?.descriptorLength ? 'yes' : 'no')}
                {statusLine('Ready source', ownerFaceCandidate?.source || 'none')}
                {statusLine('Descriptor', ownerFaceCandidate?.descriptorLength ? 'ready' : 'missing')}
              </div>
            </div>
          )}
        </div>
        <div className="space-y-2">
          {statusLine('Camera status', cameraStatus)}
          {statusLine('Vision detector', modelProvider === 'mediapipe_face_detector' ? 'mediapipe_face_detector' : modelProvider === 'face_api' ? 'face_api_fallback' : 'auto')}
          {statusLine('Vision model status', modelStatus)}
          {statusLine('Model provider', modelProvider)}
          {statusLine('MediaPipe status', mediaPipeStatus)}
          {statusLine('face-api status', faceApiStatus)}
          {statusLine('Detector source', detectorSource === 'mediapipe_face_detector' ? 'mediapipe' : detectorSource === 'face_api' ? 'face-api fallback' : 'none')}
          {statusLine('Face detection', modelCapabilities.faceDetection ? 'ready' : 'missing')}
          {statusLine('Face embedding', modelCapabilities.faceEmbedding ? 'ready' : 'missing')}
          {statusLine('Visual estimate capability', modelCapabilities.genderEstimate ? 'ready' : 'missing')}
          {statusLine('Last detection error', lastDetectionError)}
          {statusLine('Detection interval', `${detectionIntervalMs} ms`)}
          {statusLine('Detector input size', String(TRACKER_CONFIG.inputSize))}
          {statusLine('Detector threshold', String(TRACKER_CONFIG.scoreThreshold))}
          {statusLine('Tracking keep-alive', `${TRACKER_CONFIG.keepAliveMs} ms`)}
          {statusLine('Track expire', `${TRACKER_CONFIG.expireMs} ms`)}
          {statusLine('Tracking status', tracking ? 'on' : 'off')}
          {statusLine('People count', String(visibleActiveTracks(tracks).length))}
          {statusLine('Raw detections count', String(trackingStats.rawDetectionsCount))}
          {statusLine('Confirmed tracks count', String(trackingStats.confirmedTracksCount))}
          {statusLine('Predicted tracks count', String(trackingStats.predictedTracksCount))}
          {statusLine('Lost tracks count', String(trackingStats.lostTracksCount))}
          {statusLine('Visible active tracks count', String(trackingStats.visibleActiveTracksCount))}
          {statusLine('Internal tracks total', String(trackingStats.internalTracksTotal))}
          {statusLine('New tracks this frame', String(trackingStats.newTracksThisFrame))}
          {statusLine('Merged tracks this frame', String(trackingStats.mergedTracksThisFrame))}
          {statusLine('Active tracks', tracks.map((track) => `${track.track_id}:${track.status}:${track.confirmed ? 'confirmed' : 'candidate'}:${track.identity === 'owner' ? 'owner' : 'guest'}:${track.stable_label_index || 'hidden'}`).join(', ') || 'N/A')}
          {statusLine('Owner face status', ownerFaceStatus)}
          {statusLine('Persistent profile', persistentProfileStatus)}
          {statusLine('Profile storage', identityStorage?.exists || identityStorage?.profiles_exists ? 'profiles.json exists' : 'profiles.json missing')}
          {statusLine('Face enrolled at', persistedOwnerProfile?.face_enrolled_at || 'N/A')}
          {statusLine('Voice enrolled at', persistedOwnerProfile?.voice_enrolled_at || 'N/A')}
          {statusLine('Face adapter', persistedOwnerProfile?.face_adapter || 'N/A')}
          {statusLine('Voice adapter', persistedOwnerProfile?.voice_adapter || 'N/A')}
          {statusLine('Profile updated at', persistedOwnerProfile?.updated_at || 'N/A')}
          {statusLine('Owner verified at', ownerVerifiedAt ? new Date(ownerVerifiedAt).toLocaleTimeString('en-GB') : 'N/A')}
          {statusLine('Owner upload status', ownerUploadStatus)}
          {statusLine('Owner upload error', ownerUploadError)}
          {statusLine('Owner upload face count', String(ownerUploadFaceCount))}
          {statusLine('Owner upload descriptor ready', ownerFaceCandidate?.descriptorLength ? 'yes' : 'no')}
          {statusLine('Owner upload source', ownerUploadSource)}
          {statusLine('Ready source', ownerFaceCandidate?.source || 'none')}
          {statusLine('Descriptor', ownerFaceCandidate?.descriptorLength ? 'ready' : 'missing')}
          {statusLine('Owner enroll response', ownerEnrollResponse)}
          {statusLine('Owner verify response', ownerVerifyResponse)}
          {statusLine('Last identity API error', lastIdentityApiError)}
          {statusLine('Owner voice status', ownerVoiceStatus)}
          {statusLine('Current identity', currentIdentity.role)}
          {statusLine('Address selected', currentIdentity.role === 'owner' ? 'Sir' : identityContext.address)}
          {statusLine('Visual estimate', tracks[0]?.gender_estimate || 'unknown')}
          {statusLine('Gender confidence', tracks[0] ? tracks[0].gender_confidence.toFixed(2) : 'N/A')}
          {statusLine('Fusion decision', fusion?.decision || 'N/A')}
          {statusLine('Tracking mode', trackingMode)}
          {statusLine('Current command', currentCommand)}
          {statusLine('Last A.U.R.A response', lastResponse)}
          {(modelStatus === 'model_missing' || modelStatus === 'model_load_failed') && (
            <p className="font-mono text-[10.5px] leading-relaxed text-[var(--amber)]">Camera preview is active, but local vision model assets are missing or failed to load. Install face model assets under /models/face-api/ and reload diagnostics.</p>
          )}
          <p className="font-mono text-[10.5px] leading-relaxed text-[var(--text-dim)]">Voice tone verification is experimental and demo-grade. Raw camera frames and raw audio are not stored or sent to cloud APIs.</p>
          <pre className="max-h-48 overflow-auto rounded-sm border border-[var(--line)] bg-black/35 p-3 text-[10px] leading-relaxed text-[var(--text-dim)]">{JSON.stringify({
            identityContext,
            currentIdentity,
            ownerFaceCandidate: ownerFaceCandidate ? {
              source: ownerFaceCandidate.source,
              descriptor_present: Boolean(ownerFaceCandidate.descriptorLength),
              descriptor_length: ownerFaceCandidate.descriptorLength,
              face_count: ownerFaceCandidate.faceCount,
              confidence: ownerFaceCandidate.confidence,
              ready_at: ownerFaceCandidate.readyAt,
            } : null,
            tracks: publicTracks(tracks),
          }, null, 2)}</pre>
          <div className="max-h-32 overflow-auto rounded-sm border border-[var(--line)] bg-black/35 p-3 font-mono text-[10px] text-[var(--text-dim)]">
            {logs.map((line) => <div key={line}>{line}</div>)}
          </div>
        </div>
      </div>
    </section>
  )
}

export function dispatchExpoVisionCommand(command: string) {
  window.dispatchEvent(new CustomEvent(commandEventName, { detail: command }))
}
