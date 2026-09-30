param(
  [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$webRoot = Join-Path $ProjectRoot "jarvis\web"
$modelDir = Join-Path $webRoot "public\models\mediapipe"
$wasmDir = Join-Path $modelDir "wasm"
$packageWasm = Join-Path $webRoot "node_modules\@mediapipe\tasks-vision\wasm"
$faceDetectorUrl = "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
$faceDetectorPath = Join-Path $modelDir "face_detector_short_range.tflite"

New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
New-Item -ItemType Directory -Force -Path $wasmDir | Out-Null

if (-not (Test-Path -LiteralPath $packageWasm)) {
  throw "MediaPipe package wasm folder not found. Run npm install in jarvis\web first."
}

Copy-Item -Path (Join-Path $packageWasm "*") -Destination $wasmDir -Recurse -Force

if (-not (Test-Path -LiteralPath $faceDetectorPath)) {
  Invoke-WebRequest -Uri $faceDetectorUrl -OutFile $faceDetectorPath -UseBasicParsing
}

Write-Host "MediaPipe model assets installed under $modelDir"
Write-Host "Runtime URL: /models/mediapipe/"
