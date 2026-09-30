import { type GenderEstimate } from './identityAddressing'
import { detectMediaPipeFacesForVideo, faceapi, getVisionModelLoadState, loadVisionIdentityModel, type VisionDetectorProvider, type VisionModelCapabilities, type VisionModelStatus } from './visionIdentity'

export type BBox = { x: number; y: number; width: number; height: number }
export type RawDetection = { bbox: BBox; confidence: number; descriptor?: number[]; gender_estimate?: GenderEstimate; gender_confidence?: number }

export type PersonTrack = {
  track_id: string
  stable_label_index: number
  bbox: BBox
  smoothed_bbox: BBox
  face_confidence: number
  confidence: number
  identity: 'owner' | 'known_user' | 'guest'
  gender_estimate: GenderEstimate
  gender_confidence: number
  address: 'Sir' | 'Mam' | 'Guest' | string
  descriptor?: number[]
  status: 'candidate' | 'detected' | 'predicted' | 'lost_recently' | 'expired'
  confirmed: boolean
  hits: number
  missed_frames: number
  first_seen_at: number
  last_seen_at: number
  last_detected_at: number
}

export type TrackingMode = 'owner' | 'nearest' | 'all' | 'selected' | 'none'
export type TrackerResult = {
  ok: boolean
  status: VisionModelStatus | 'no_face_detected'
  modelStatus: VisionModelStatus
  provider: VisionDetectorProvider
  detectorSource: VisionDetectorProvider
  mediaPipeStatus: VisionModelStatus
  faceApiStatus: VisionModelStatus
  capabilities: VisionModelCapabilities
  peopleCount: number
  tracks: PersonTrack[]
  rawDetectionsCount: number
  confirmedTracksCount: number
  predictedTracksCount: number
  lostTracksCount: number
  visibleActiveTracksCount: number
  internalTracksTotal: number
  newTracksThisFrame: number
  mergedTracksThisFrame: number
  message?: string
}

export const TRACK_CONFIRM_HITS = 2
export const TRACK_KEEP_ALIVE_MS = 1800
export const TRACK_EXPIRE_MS = 3200
export const MAX_PREDICTED_VISIBLE_MS = 1800
export const MAX_MISSED_FRAMES_VISIBLE = 5
export const MIN_FACE_BOX_AREA = 1600
export const NMS_IOU_THRESHOLD = 0.45
export const IOU_MATCH_THRESHOLD = 0.18
export const CENTER_DISTANCE_THRESHOLD_RATIO = 0.22
export const DESCRIPTOR_MATCH_THRESHOLD = 0.45
export const OWNER_IDENTITY_KEEP_ALIVE_MS = 6000
export const BOX_SMOOTHING_ALPHA = 0.3
export const DETECTION_INTERVAL_MS = 300
export const TRACKER_CONFIG = {
  inputSize: 416,
  scoreThreshold: 0.25,
  confirmHits: TRACK_CONFIRM_HITS,
  keepAliveMs: TRACK_KEEP_ALIVE_MS,
  expireMs: TRACK_EXPIRE_MS,
  maxPredictedVisibleMs: MAX_PREDICTED_VISIBLE_MS,
  maxMissedFramesVisible: MAX_MISSED_FRAMES_VISIBLE,
  minFaceBoxArea: MIN_FACE_BOX_AREA,
  nmsIouThreshold: NMS_IOU_THRESHOLD,
  smoothingAlpha: BOX_SMOOTHING_ALPHA,
  detectionIntervalMs: DETECTION_INTERVAL_MS,
}

const boxArea = (box: BBox) => Math.max(0, box.width) * Math.max(0, box.height)

function iou(a: BBox, b: BBox) {
  const ax2 = a.x + a.width
  const ay2 = a.y + a.height
  const bx2 = b.x + b.width
  const by2 = b.y + b.height
  const ix = Math.max(0, Math.min(ax2, bx2) - Math.max(a.x, b.x))
  const iy = Math.max(0, Math.min(ay2, by2) - Math.max(a.y, b.y))
  const intersection = ix * iy
  const union = boxArea(a) + boxArea(b) - intersection
  return union > 0 ? intersection / union : 0
}

function centerDistance(a: BBox, b: BBox) {
  const ax = a.x + a.width / 2
  const ay = a.y + a.height / 2
  const bx = b.x + b.width / 2
  const by = b.y + b.height / 2
  return Math.hypot(ax - bx, ay - by)
}

function centerSimilarity(a: BBox, b: BBox) {
  const scale = Math.max(a.width, a.height, b.width, b.height, 1)
  return Math.max(0, 1 - centerDistance(a, b) / scale)
}

function descriptorSimilarity(a?: number[], b?: number[]) {
  if (!a?.length || !b?.length || a.length !== b.length) return 0
  let dot = 0
  let left = 0
  let right = 0
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i]
    left += a[i] * a[i]
    right += b[i] * b[i]
  }
  return left && right ? Math.max(0, Math.min(1, dot / Math.sqrt(left * right))) : 0
}

function smoothBox(previous: BBox, next: BBox): BBox {
  const alpha = BOX_SMOOTHING_ALPHA
  return {
    x: previous.x + (next.x - previous.x) * alpha,
    y: previous.y + (next.y - previous.y) * alpha,
    width: previous.width + (next.width - previous.width) * alpha,
    height: previous.height + (next.height - previous.height) * alpha,
  }
}

export function nonMaxSuppressDetections(detections: RawDetection[], iouThreshold = NMS_IOU_THRESHOLD): RawDetection[] {
  const sorted = detections.filter((detection) => boxArea(detection.bbox) >= MIN_FACE_BOX_AREA).sort((a, b) => b.confidence - a.confidence)
  const kept: RawDetection[] = []
  for (const detection of sorted) {
    if (kept.every((existing) => iou(existing.bbox, detection.bbox) <= iouThreshold)) kept.push(detection)
  }
  return kept.sort((a, b) => a.bbox.x - b.bbox.x)
}

export class PersonTracker {
  private tracks: PersonTrack[] = []
  private nextTrackNumber = 1

  constructor(_genderThreshold = 0.75) {}

  get modelStatus() {
    return getVisionModelLoadState().status
  }

  get capabilities() {
    return getVisionModelLoadState().capabilities
  }

  async detect(video: HTMLVideoElement, ownerVerified = false): Promise<TrackerResult> {
    const model = await loadVisionIdentityModel()
    if (!model.ok) {
      this.tracks = []
      return this.result({ ok: false, status: model.status, model, rawDetectionsCount: 0, newTracksThisFrame: 0, mergedTracksThisFrame: 0, message: model.message })
    }

    const rawDetections = model.detectorSource === 'mediapipe_face_detector'
      ? this.detectWithMediaPipe(video)
      : await this.detectWithFaceApi(video, model.capabilities)
    const detections = nonMaxSuppressDetections(rawDetections)
    const now = Date.now()
    const matches = this.matchDetectionsToTracks(detections)
    const matchedDetectionIndexes = new Set(matches.map((match) => match.detectionIndex))
    const matchedTrackIds = new Set(matches.map((match) => match.track.track_id))
    let newTracksThisFrame = 0

    const updatedTracks = matches.map(({ detection, track }) => this.updateTrack(track, detection, now, ownerVerified))
    for (const [index, detection] of detections.entries()) {
      if (matchedDetectionIndexes.has(index)) continue
      if (detection.confidence < TRACKER_CONFIG.scoreThreshold || boxArea(detection.bbox) < MIN_FACE_BOX_AREA) continue
      updatedTracks.push(this.createCandidateTrack(detection, now, ownerVerified && updatedTracks.length === 0 && index === 0))
      newTracksThisFrame += 1
    }

    const missedTracks = this.tracks
      .filter((track) => !matchedTrackIds.has(track.track_id))
      .map((track) => this.markMissed(track, now))
      .filter((track) => track.status !== 'expired')
    const merged = mergeOverlappingTracks([...updatedTracks, ...missedTracks], now)
    this.tracks = assignVisibleLabelIndexes(merged.tracks)

    return this.result({
      ok: true,
      status: visibleActiveTracks(this.tracks, now).length ? 'ready' : 'no_face_detected',
      model,
      rawDetectionsCount: rawDetections.length,
      newTracksThisFrame,
      mergedTracksThisFrame: merged.mergedCount,
      message: model.message,
    })
  }

  private matchDetectionsToTracks(detections: RawDetection[]) {
    const candidates: Array<{ detectionIndex: number; detection: RawDetection; track: PersonTrack; score: number }> = []
    detections.forEach((detection, detectionIndex) => {
      for (const track of this.tracks.filter((item) => item.status !== 'expired')) {
        const box = track.smoothed_bbox || track.bbox
        const overlap = iou(box, detection.bbox)
        const center = centerSimilarity(box, detection.bbox)
        const descriptor = descriptorSimilarity(track.descriptor, detection.descriptor)
        const centerRatio = centerDistance(box, detection.bbox) / Math.max(detection.bbox.width, detection.bbox.height, track.bbox.width, track.bbox.height, 1)
        const descriptorOk = !track.descriptor || !detection.descriptor || descriptor >= DESCRIPTOR_MATCH_THRESHOLD
        if ((overlap >= IOU_MATCH_THRESHOLD || centerRatio <= CENTER_DISTANCE_THRESHOLD_RATIO || descriptor >= DESCRIPTOR_MATCH_THRESHOLD) && descriptorOk) {
          candidates.push({ detectionIndex, detection, track, score: overlap * 0.65 + center * 0.25 + descriptor * 0.10 })
        }
      }
    })
    candidates.sort((a, b) => b.score - a.score)
    const usedDetections = new Set<number>()
    const usedTracks = new Set<string>()
    const matches: Array<{ detectionIndex: number; detection: RawDetection; track: PersonTrack }> = []
    for (const candidate of candidates) {
      if (usedDetections.has(candidate.detectionIndex) || usedTracks.has(candidate.track.track_id)) continue
      usedDetections.add(candidate.detectionIndex)
      usedTracks.add(candidate.track.track_id)
      matches.push(candidate)
    }
    return matches
  }

  private createCandidateTrack(detection: RawDetection, now: number, owner = false): PersonTrack {
    const identity: PersonTrack['identity'] = owner ? 'owner' : 'guest'
    return {
      track_id: `track_${this.nextTrackNumber++}`,
      stable_label_index: 0,
      bbox: detection.bbox,
      smoothed_bbox: detection.bbox,
      face_confidence: detection.confidence,
      confidence: detection.confidence,
      identity,
      gender_estimate: detection.gender_estimate || 'unknown',
      gender_confidence: detection.gender_confidence || 0,
      address: this.resolveAddress(identity, detection),
      descriptor: detection.descriptor,
      status: 'candidate',
      confirmed: false,
      hits: 1,
      missed_frames: 0,
      first_seen_at: now,
      last_seen_at: now,
      last_detected_at: now,
    }
  }

  private updateTrack(track: PersonTrack, detection: RawDetection, now: number, ownerVerified: boolean): PersonTrack {
    const hits = track.hits + 1
    const recentlyOwner = track.identity === 'owner' && now - track.last_detected_at <= OWNER_IDENTITY_KEEP_ALIVE_MS
    const identity: PersonTrack['identity'] = ownerVerified || recentlyOwner ? 'owner' : track.identity
    const confirmed = track.confirmed || hits >= TRACK_CONFIRM_HITS
    return {
      ...track,
      bbox: detection.bbox,
      smoothed_bbox: smoothBox(track.smoothed_bbox || track.bbox, detection.bbox),
      face_confidence: detection.confidence,
      confidence: detection.confidence,
      identity,
      gender_estimate: detection.gender_estimate || track.gender_estimate,
      gender_confidence: detection.gender_confidence ?? track.gender_confidence,
      address: this.resolveAddress(identity, detection, track.address),
      descriptor: detection.descriptor || track.descriptor,
      status: confirmed ? 'detected' : 'candidate',
      confirmed,
      hits,
      missed_frames: 0,
      last_seen_at: now,
      last_detected_at: now,
    }
  }

  private markMissed(track: PersonTrack, now: number): PersonTrack {
    const missed_frames = track.missed_frames + 1
    const age = now - track.last_detected_at
    const keepOwner = track.identity === 'owner' && age <= OWNER_IDENTITY_KEEP_ALIVE_MS
    if (!track.confirmed) return { ...track, status: age > TRACK_KEEP_ALIVE_MS ? 'expired' : 'candidate', missed_frames }
    if ((age <= TRACK_KEEP_ALIVE_MS || keepOwner) && missed_frames <= MAX_MISSED_FRAMES_VISIBLE) {
      return { ...track, status: 'predicted', missed_frames, last_seen_at: now }
    }
    if (age <= TRACK_EXPIRE_MS) return { ...track, status: 'lost_recently', missed_frames, last_seen_at: now }
    return { ...track, status: 'expired', missed_frames }
  }

  private resolveAddress(identity: PersonTrack['identity'], _detection: RawDetection, fallback = 'Guest') {
    if (identity === 'owner') return fallback
    return fallback
  }

  private detectWithMediaPipe(video: HTMLVideoElement): RawDetection[] {
    const result = detectMediaPipeFacesForVideo(video)
    return (result?.detections || []).map((detection: any) => {
      const box = detection.boundingBox || {}
      const category = detection.categories?.[0] || {}
      return {
        bbox: {
          x: box.originX ?? box.xMin ?? 0,
          y: box.originY ?? box.yMin ?? 0,
          width: box.width ?? Math.max(0, (box.xMax ?? 0) - (box.xMin ?? 0)),
          height: box.height ?? Math.max(0, (box.yMax ?? 0) - (box.yMin ?? 0)),
        },
        confidence: category.score ?? detection.score ?? 0,
        gender_estimate: 'unknown',
        gender_confidence: 0,
      }
    })
  }

  private async detectWithFaceApi(video: HTMLVideoElement, capabilities: VisionModelCapabilities): Promise<RawDetection[]> {
    const options = new faceapi.TinyFaceDetectorOptions({ inputSize: TRACKER_CONFIG.inputSize, scoreThreshold: TRACKER_CONFIG.scoreThreshold })
    const base = faceapi.detectAllFaces(video, options)
    const detections = capabilities.genderEstimate ? await base.withAgeAndGender() : await base
    return detections.map((detection: any) => ({
      bbox: detection.detection?.box || detection.box,
      confidence: detection.detection?.score || detection.score || 0,
      gender_estimate: capabilities.genderEstimate ? String(detection.gender || 'unknown') as GenderEstimate : 'unknown',
      gender_confidence: capabilities.genderEstimate && typeof detection.genderProbability === 'number' ? detection.genderProbability : 0,
    }))
  }

  private result(input: {
    ok: boolean
    status: VisionModelStatus | 'no_face_detected'
    model: Awaited<ReturnType<typeof loadVisionIdentityModel>>
    rawDetectionsCount: number
    newTracksThisFrame: number
    mergedTracksThisFrame: number
    message?: string
  }): TrackerResult {
    const now = Date.now()
    const visible = visibleActiveTracks(this.tracks, now)
    return {
      ok: input.ok,
      status: input.status,
      modelStatus: input.model.status,
      provider: input.model.provider,
      detectorSource: input.model.detectorSource,
      mediaPipeStatus: input.model.mediaPipeStatus,
      faceApiStatus: input.model.faceApiStatus,
      capabilities: input.model.capabilities,
      peopleCount: visible.length,
      tracks: this.tracks,
      rawDetectionsCount: input.rawDetectionsCount,
      confirmedTracksCount: this.tracks.filter((track) => track.confirmed).length,
      predictedTracksCount: this.tracks.filter((track) => track.status === 'predicted').length,
      lostTracksCount: this.tracks.filter((track) => track.status === 'lost_recently').length,
      visibleActiveTracksCount: visible.length,
      internalTracksTotal: this.tracks.length,
      newTracksThisFrame: input.newTracksThisFrame,
      mergedTracksThisFrame: input.mergedTracksThisFrame,
      message: input.message,
    }
  }
}

export function visibleActiveTracks(tracks: PersonTrack[], now = Date.now()): PersonTrack[] {
  return tracks
    .filter((track) => track.confirmed && (
      track.status === 'detected' ||
      (track.status === 'predicted' && now - track.last_detected_at <= MAX_PREDICTED_VISIBLE_MS && track.missed_frames <= MAX_MISSED_FRAMES_VISIBLE)
    ))
    .sort((a, b) => a.smoothed_bbox.x - b.smoothed_bbox.x)
}

function assignVisibleLabelIndexes(tracks: PersonTrack[]) {
  const labelByTrackId = new Map(visibleActiveTracks(tracks).map((track, index) => [track.track_id, index + 1]))
  return tracks.map((track) => ({ ...track, stable_label_index: labelByTrackId.get(track.track_id) || 0 }))
}

export function mergeOverlappingTracks(tracks: PersonTrack[], now = Date.now()) {
  const sorted = [...tracks].sort((a, b) => {
    if (a.identity === 'owner' && b.identity !== 'owner') return -1
    if (b.identity === 'owner' && a.identity !== 'owner') return 1
    return b.last_detected_at - a.last_detected_at || b.confidence - a.confidence
  })
  const kept: PersonTrack[] = []
  let mergedCount = 0
  for (const track of sorted) {
    const duplicate = kept.find((existing) => iou(existing.smoothed_bbox, track.smoothed_bbox) > 0.35)
    if (!duplicate) {
      kept.push(track)
      continue
    }
    mergedCount += 1
    const latestDetectedAt = Math.max(duplicate.last_detected_at, track.last_detected_at)
    const owner = duplicate.identity === 'owner' || track.identity === 'owner'
    Object.assign(duplicate, {
      identity: owner ? 'owner' : duplicate.identity,
      address: owner ? 'Sir' : duplicate.address,
      confirmed: duplicate.confirmed || track.confirmed,
      hits: Math.max(duplicate.hits, track.hits),
      last_seen_at: Math.max(duplicate.last_seen_at, track.last_seen_at),
      last_detected_at: latestDetectedAt,
      missed_frames: Math.min(duplicate.missed_frames, track.missed_frames),
      status: duplicate.status === 'detected' || track.status === 'detected'
        ? 'detected'
        : now - latestDetectedAt <= MAX_PREDICTED_VISIBLE_MS
          ? 'predicted'
          : duplicate.status,
    })
  }
  return { tracks: kept.filter((track) => track.status !== 'expired'), mergedCount }
}

export async function detectPeopleFromVideo(video: HTMLVideoElement): Promise<TrackerResult> {
  const tracker = new PersonTracker()
  return tracker.detect(video)
}
