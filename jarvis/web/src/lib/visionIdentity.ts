import * as faceapi from '@vladmandic/face-api'
import { FaceDetector, FilesetResolver, type FaceDetectorResult } from '@mediapipe/tasks-vision'

export type VisionModelStatus = 'not_loaded' | 'loading' | 'ready' | 'model_missing' | 'model_load_failed'
export type VisionDetectorMode = 'auto' | 'mediapipe_face_detector' | 'face_api'
export type VisionDetectorProvider = 'mediapipe_face_detector' | 'face_api' | 'none'

export type VisionModelCapabilities = {
  faceDetection: boolean
  faceLandmarks?: boolean
  faceEmbedding: boolean
  genderEstimate: boolean
  visualEstimate?: boolean
}

export type VisionModelLoadResult = {
  ok: boolean
  status: VisionModelStatus
  provider: VisionDetectorProvider
  detectorMode: VisionDetectorMode
  detectorSource: VisionDetectorProvider
  mediaPipeStatus: VisionModelStatus
  faceApiStatus: VisionModelStatus
  message?: string
  capabilities: VisionModelCapabilities
  mediaPipeDetector?: FaceDetector
  peopleTrackingRuntime?: PeopleTrackingRuntimeDiagnostics
}

export type PeopleTrackingAssetDiagnostics = {
  model_paths_checked: string[]
  missing_paths: string[]
  model_loaded: boolean
  detector_backend: VisionDetectorProvider
}

export type PeopleTrackingRuntimeDiagnostics = {
  backend: 'mediapipe' | 'face_api' | 'none'
  package_available: boolean
  import_ok: boolean
  wasm_resolver_ok: boolean
  wasm_base_url: string | null
  model_asset_path: string | null
  model_asset_reachable: boolean
  detector_init_ok: boolean
  error_code: string | null
  safe_message: string | null
}

export type IdentityContext = {
  decision: string
  display_name: string
  role: string
  address: string
  preferred_address?: string | null
  address_source?: string | null
  matched_profile_id?: string | null
  matched_profile_name?: string | null
  matched_profile?: {
    id?: string
    display_name?: string
    preferred_address?: string
    address_source?: string
  } | null
  identity_confidence?: number | null
  identity_state?: string
  gesture_tracking_state?: string
  gesture_tracking_error?: {
    code: string
    safe_message: string
    stage: string
    timestamp: string
  } | null
  people_tracking_state?: string
  people_tracking_error?: {
    code: string
    safe_message: string
    stage: string
    timestamp: string
  } | null
  tracking_state?: string
  live_identity_fresh?: boolean
  live_identity_address_allowed?: boolean
  active_profile_is_unambiguous?: boolean
  last_live_match_at?: string | null
  effective_address?: string
  session_address_override?: string
  address_reason?: string
  owner_face_status?: string
  owner_voice_status?: string
  owner_face_enrolled?: boolean
  owner_voice_enrolled?: boolean
  people_count: number
  last_owner_verified_at?: string | null
  last_person_seen_at?: string | null
  last_known_people_count?: number
  tracking_mode: string
  tracks: Array<{
    track_id: string
    identity: string
    address: string
    status?: string
    confirmed?: boolean
    stable_label_index?: number
    hits?: number
    missed_frames?: number
    descriptor_present?: boolean
    descriptor_length?: number
  }>
}

export type VisionContext = {
  camera_state?: string
  camera_status: string
  stream_ready?: boolean
  video_ready?: boolean
  video_width?: number
  video_height?: number
  gesture_tracking_state?: string
  gesture_tracking_error?: {
    code: string
    safe_message: string
    stage: 'gesture_model_load' | 'hand_detector_init' | 'tracking_loop' | 'unknown' | string
    timestamp: string
  } | null
  people_tracking_state?: string
  people_tracking_error?: {
    code: string
    safe_message: string
    stage: 'face_model_load' | 'person_detector_init' | 'camera_stream' | 'identity_match' | 'tracking_loop' | 'unknown' | string
    timestamp: string
  } | null
  people_tracking_runtime?: PeopleTrackingRuntimeDiagnostics
  tracking_state?: string
  tracking_status: string
  tracking_started_at?: string | null
  tracking_error?: {
    code: string
    safe_message: string
    stage: 'face_model_load' | 'person_detector_init' | 'camera_stream' | 'identity_match' | 'tracking_loop' | 'unknown' | string
    timestamp: string
  } | null
  tracking_last_error_code?: string | null
  tracking_last_error_message?: string | null
  tracking_backend?: string
  model_loaded?: boolean
  loop_running?: boolean
  people_count: number
  last_owner_verified_at?: string | null
  last_person_seen_at?: string | null
  last_known_people_count?: number
  current_identity: 'owner' | 'guest'
  address_selected: string
  preferred_address?: string | null
  address_source?: string | null
  matched_profile_id?: string | null
  matched_profile_name?: string | null
  identity_confidence?: number | null
  identity_state?: string
  owner_profile_enrolled?: boolean
  live_identity_fresh?: boolean
  live_identity_address_allowed?: boolean
  active_profile_is_unambiguous?: boolean
  visual_gender_inference?: boolean
  descriptors_exposed?: boolean
  raw_frames_uploaded?: boolean
  fusion_decision: string | null
  tracking_mode: string
  tracks: Array<{
    track_id: string
    status: string
    identity: 'owner' | 'guest'
    address: string
    visual_estimate: string
    gender_confidence: number
    confirmed?: boolean
    stable_label_index?: number
    missed_frames?: number
    bbox?: {
      x: number
      y: number
      width: number
      height: number
    }
  }>
}

let loadPromise: Promise<VisionModelLoadResult> | null = null
let cachedResult: VisionModelLoadResult = {
  ok: false,
  status: 'not_loaded',
  provider: 'none',
  detectorMode: 'auto',
  detectorSource: 'none',
  mediaPipeStatus: 'not_loaded',
  faceApiStatus: 'not_loaded',
  capabilities: { faceDetection: false, faceLandmarks: false, faceEmbedding: false, genderEstimate: false, visualEstimate: false },
  peopleTrackingRuntime: {
    backend: 'none',
    package_available: true,
    import_ok: true,
    wasm_resolver_ok: false,
    wasm_base_url: '/models/mediapipe/wasm',
    model_asset_path: '/models/mediapipe/face_detector_short_range.tflite',
    model_asset_reachable: false,
    detector_init_ok: false,
    error_code: null,
    safe_message: null,
  },
}

async function loadOptionalModel(loader: () => Promise<unknown>) {
  try {
    await loader()
    return true
  } catch {
    return false
  }
}

export async function loadVisionIdentityModel(): Promise<VisionModelLoadResult> {
  if (cachedResult.status === 'ready') return cachedResult
  if (loadPromise) return loadPromise
  cachedResult = { ...cachedResult, status: 'loading', message: undefined }
  loadPromise = (async () => {
    const detectorMode = ((import.meta.env.VITE_AURA_VISION_DETECTOR || 'auto') as VisionDetectorMode)
    const mediaPipePath = '/models/mediapipe/'
    const faceApiPath = '/models/face-api/'
    let mediaPipeDetector: FaceDetector | undefined
    let mediaPipeStatus: VisionModelStatus = 'not_loaded'
    let mediaPipeMessage = ''
    let mediaPipeRuntime: PeopleTrackingRuntimeDiagnostics = {
      backend: 'mediapipe',
      package_available: Boolean(FilesetResolver && FaceDetector),
      import_ok: Boolean(FilesetResolver && FaceDetector),
      wasm_resolver_ok: false,
      wasm_base_url: `${mediaPipePath}wasm`,
      model_asset_path: `${mediaPipePath}face_detector_short_range.tflite`,
      model_asset_reachable: false,
      detector_init_ok: false,
      error_code: null,
      safe_message: null,
    }

    if (detectorMode === 'auto' || detectorMode === 'mediapipe_face_detector') {
      try {
        if (!mediaPipeRuntime.package_available || !mediaPipeRuntime.import_ok) {
          throw new Error('mediapipe_import_failed')
        }
        mediaPipeRuntime.model_asset_reachable = await assetExists(mediaPipeRuntime.model_asset_path || '')
        if (!mediaPipeRuntime.model_asset_reachable) {
          mediaPipeRuntime.error_code = 'mediapipe_model_missing'
          mediaPipeRuntime.safe_message = 'Local MediaPipe face detector model asset is missing.'
          throw new Error(mediaPipeRuntime.error_code)
        }
        const vision = await FilesetResolver.forVisionTasks(`${mediaPipePath}wasm`)
        mediaPipeRuntime.wasm_resolver_ok = true
        mediaPipeDetector = await FaceDetector.createFromOptions(vision, {
          baseOptions: { modelAssetPath: `${mediaPipePath}face_detector_short_range.tflite` },
          runningMode: 'VIDEO',
          minDetectionConfidence: 0.35,
        })
        mediaPipeRuntime.detector_init_ok = true
        mediaPipeStatus = 'ready'
      } catch (e: any) {
        mediaPipeStatus = 'model_missing'
        const message = String(e?.message || '')
        if (!mediaPipeRuntime.error_code) {
          const lower = message.toLowerCase()
          mediaPipeRuntime.error_code = lower.includes('wasm')
            ? 'mediapipe_wasm_unavailable'
            : lower.includes('import')
              ? 'mediapipe_import_failed'
              : lower.includes('unsupported')
                ? 'mediapipe_unsupported_browser'
                : lower.includes('detector') || lower.includes('createfromoptions')
                  ? 'mediapipe_detector_init_failed'
                  : 'mediapipe_detector_init_failed'
          mediaPipeRuntime.safe_message = 'Local MediaPipe detector could not initialize.'
        }
        mediaPipeMessage = mediaPipeRuntime.error_code
      }
    }

    if (detectorMode === 'mediapipe_face_detector' && !mediaPipeDetector) {
      cachedResult = {
        ok: false,
        status: mediaPipeStatus,
        provider: 'none',
        detectorMode,
        detectorSource: 'none',
        mediaPipeStatus,
        faceApiStatus: 'not_loaded',
        message: mediaPipeMessage,
        capabilities: { faceDetection: false, faceLandmarks: false, faceEmbedding: false, genderEstimate: false, visualEstimate: false },
        peopleTrackingRuntime: mediaPipeRuntime,
      }
      return cachedResult
    }

    try {
      await faceapi.nets.tinyFaceDetector.loadFromUri(faceApiPath)
      const landmarks = await loadOptionalModel(() => faceapi.nets.faceLandmark68Net.loadFromUri(faceApiPath))
      const recognition = landmarks ? await loadOptionalModel(() => faceapi.nets.faceRecognitionNet.loadFromUri(faceApiPath)) : false
      const gender = await loadOptionalModel(() => faceapi.nets.ageGenderNet.loadFromUri(faceApiPath))
      const detectorSource = mediaPipeDetector && detectorMode !== 'face_api' ? 'mediapipe_face_detector' : 'face_api'
      cachedResult = {
        ok: true,
        status: 'ready',
        provider: detectorSource,
        detectorMode,
        detectorSource,
        mediaPipeStatus,
        faceApiStatus: 'ready',
        mediaPipeDetector,
        capabilities: {
          faceDetection: true,
          faceLandmarks: landmarks,
          faceEmbedding: recognition,
          genderEstimate: gender,
          visualEstimate: gender,
        },
        peopleTrackingRuntime: {
          ...mediaPipeRuntime,
          backend: detectorSource === 'mediapipe_face_detector' ? 'mediapipe' : 'face_api',
          error_code: detectorSource === 'mediapipe_face_detector' ? null : mediaPipeRuntime.error_code,
        },
        message: mediaPipeDetector ? undefined : mediaPipeMessage || undefined,
      }
      return cachedResult
    } catch (e: any) {
      if (mediaPipeDetector && detectorMode === 'auto') {
        cachedResult = {
          ok: true,
          status: 'ready',
          provider: 'mediapipe_face_detector',
          detectorMode,
          detectorSource: 'mediapipe_face_detector',
          mediaPipeStatus: 'ready',
          faceApiStatus: 'model_missing',
          mediaPipeDetector,
          message: 'MediaPipe tracking is ready. Face-api descriptor assets are missing, so owner recognition is unavailable.',
          capabilities: { faceDetection: true, faceLandmarks: false, faceEmbedding: false, genderEstimate: false, visualEstimate: false },
          peopleTrackingRuntime: {
            ...mediaPipeRuntime,
            backend: 'mediapipe',
            error_code: null,
            safe_message: null,
          },
        }
        return cachedResult
      }
      cachedResult = {
        ok: false,
        status: 'model_load_failed',
        provider: 'none',
        detectorMode,
        detectorSource: 'none',
        mediaPipeStatus,
        faceApiStatus: 'model_load_failed',
        message: e?.message || mediaPipeMessage || 'Failed to load local vision model assets.',
        capabilities: { faceDetection: false, faceLandmarks: false, faceEmbedding: false, genderEstimate: false, visualEstimate: false },
        peopleTrackingRuntime: {
          ...mediaPipeRuntime,
          backend: 'none',
          error_code: mediaPipeRuntime.error_code || 'faceapi_unavailable',
          safe_message: mediaPipeRuntime.safe_message || 'No local people tracking detector is available.',
        },
      }
      return cachedResult
    } finally {
      loadPromise = null
    }
  })()
  return loadPromise
}

export function getVisionModelLoadState(): VisionModelLoadResult {
  return cachedResult
}

async function assetExists(path: string) {
  try {
    const res = await fetch(path, { method: 'HEAD', cache: 'no-store' })
    if (res.ok) return true
    if (res.status === 405) {
      const fallback = await fetch(path, { method: 'GET', cache: 'no-store' })
      return fallback.ok
    }
    return false
  } catch {
    return false
  }
}

export async function getPeopleTrackingAssetDiagnostics(): Promise<PeopleTrackingAssetDiagnostics> {
  const model = getVisionModelLoadState()
  const modelPaths = [
    '/models/mediapipe/face_detector_short_range.tflite',
    '/models/mediapipe/wasm/vision_wasm_internal.wasm',
    '/models/face-api/tiny_face_detector_model-weights_manifest.json',
  ]
  const checked = await Promise.all(modelPaths.map(async (path) => ({ path, exists: await assetExists(path) })))
  return {
    model_paths_checked: modelPaths,
    missing_paths: checked.filter((item) => !item.exists).map((item) => item.path),
    model_loaded: model.ok && model.status === 'ready',
    detector_backend: model.detectorSource,
  }
}

export { faceapi }

export function detectMediaPipeFacesForVideo(video: HTMLVideoElement, timestampMs = performance.now()): FaceDetectorResult | null {
  const detector = cachedResult.mediaPipeDetector
  if (!detector || cachedResult.detectorSource !== 'mediapipe_face_detector') return null
  return detector.detectForVideo(video, timestampMs)
}

async function extractFaceDescriptorFromSource(source: HTMLImageElement | HTMLVideoElement): Promise<{
  ok: boolean
  descriptor?: number[]
  faceCount: number
  bbox?: { x: number; y: number; width: number; height: number }
  confidence?: number
  error?: string
}> {
  const model = await loadVisionIdentityModel()
  if (!model.capabilities.faceEmbedding) {
    return { ok: false, faceCount: 0, error: 'face_recognition_model_not_loaded' }
  }
  const options = new faceapi.TinyFaceDetectorOptions({ inputSize: 416, scoreThreshold: 0.25 })
  const faces = await faceapi.detectAllFaces(source, options).withFaceLandmarks().withFaceDescriptors()
  if (faces.length === 0) return { ok: false, faceCount: 0, error: 'owner_upload_no_face' }
  if (faces.length > 1) return { ok: false, faceCount: faces.length, error: 'owner_upload_multiple_faces' }
  const face = faces[0]
  const box = face.detection.box
  return {
    ok: true,
    descriptor: Array.from(face.descriptor),
    faceCount: 1,
    bbox: { x: box.x, y: box.y, width: box.width, height: box.height },
    confidence: face.detection.score,
  }
}

export async function extractFaceDescriptorFromImage(image: HTMLImageElement) {
  return extractFaceDescriptorFromSource(image)
}

export async function extractFaceDescriptorFromVideo(video: HTMLVideoElement) {
  return extractFaceDescriptorFromSource(video)
}

export function summarizeIdentityContext(context: IdentityContext | null): string {
  if (!context) return 'Identity context unavailable.'
  if (context.people_count === 0) return 'I do not see anyone yet.'
  const owner = context.tracks.some((track) => track.identity === 'owner')
  const guests = context.tracks.filter((track) => track.identity !== 'owner').length
  if (context.people_count > 1) return `I can see ${context.people_count} people. ${owner ? 'Owner verified. ' : ''}${guests} guests detected.`
  return owner ? `Welcome back, ${context.address}.` : `Hello, ${context.tracks[0]?.address || 'Guest'}.`
}
