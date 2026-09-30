import { useCallback, useRef } from 'react'
import { Camera, ChevronDown, ChevronUp, Square, VideoOff } from 'lucide-react'

type MiniTrack = {
  track_id?: string
  identity?: string
  address?: string
  confirmed?: boolean
  stable_label_index?: number
  bbox?: {
    x: number
    y: number
    width: number
    height: number
  }
}

export function MiniCameraPreview({
  cameraOn,
  tracking,
  peopleCount,
  cameraStatus,
  trackingStatus,
  ownerStatus,
  tracks,
  collapsed,
  onToggleCollapse,
  attachPreviewVideoElement,
  onStartCamera,
  onStopCamera,
}: {
  cameraOn: boolean
  tracking: boolean
  peopleCount: number
  cameraStatus?: string
  trackingStatus?: string
  ownerStatus: string
  tracks: MiniTrack[]
  collapsed: boolean
  onToggleCollapse: () => void
  attachPreviewVideoElement: (video: HTMLVideoElement | null) => void
  onStartCamera: () => void
  onStopCamera: () => void
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const bindVideo = useCallback((node: HTMLVideoElement | null) => {
    videoRef.current = node
    attachPreviewVideoElement(node)
  }, [attachPreviewVideoElement])

  const identityLabel = ownerStatus.toLowerCase().includes('verified')
    ? 'Owner'
    : tracks.some((track) => track.identity === 'guest')
      ? 'Guest'
      : 'Unknown'
  const statusLabel = (status: string | undefined, fallback: string) => {
    const value = status || fallback
    return value.charAt(0).toUpperCase() + value.slice(1)
  }
  const cameraLabel = statusLabel(cameraStatus, cameraOn ? 'active' : 'off')
  const trackingLabel = statusLabel(trackingStatus, tracking ? 'active' : 'off')

  if (collapsed) {
    return (
      <button type="button" className="aura-mini-camera-preview collapsed" onClick={onToggleCollapse}>
        <span className={`aura-mini-camera-dot ${cameraOn ? 'active' : 'off'}`} />
        <span>CAM</span>
        <span>{cameraLabel}</span>
        <span>People: {peopleCount}</span>
        <ChevronUp size={13} />
      </button>
    )
  }

  return (
    <section className="aura-mini-camera-preview" aria-label="Mini camera feed preview">
      <header className="aura-mini-camera-header">
        <div className="aura-mini-camera-title">
          <span className={`aura-mini-camera-dot ${cameraOn ? 'active' : 'off'}`} />
          <Camera size={13} />
          <span>CAMERA FEED</span>
        </div>
        <button type="button" className="aura-mini-camera-icon-btn" onClick={onToggleCollapse} aria-label="Collapse camera preview">
          <ChevronDown size={14} />
        </button>
      </header>

      <div className="aura-mini-camera-body">
        {cameraOn ? (
          <>
            <video
              ref={bindVideo}
              className="aura-mini-camera-video"
              muted
              playsInline
              autoPlay
            />
          </>
        ) : (
          <div className="aura-mini-camera-placeholder">
            <VideoOff size={22} />
            <span>CAMERA OFF</span>
            <small>Say "turn on camera" or use Start Camera.</small>
          </div>
        )}
      </div>

      <footer className="aura-mini-camera-footer">
        <span>Camera: {cameraLabel}</span>
        <span>People Tracking: {trackingLabel}</span>
        <span>People: {peopleCount}</span>
        <span>Identity: {identityLabel}</span>
        <button type="button" className="aura-mini-camera-action" onClick={cameraOn ? onStopCamera : onStartCamera}>
          {cameraOn ? <Square size={10} /> : <Camera size={10} />}
          {cameraOn ? 'Stop' : 'Start Camera'}
        </button>
      </footer>
    </section>
  )
}
