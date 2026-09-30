param(
  [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)

$source = Join-Path $ProjectRoot 'jarvis\web\node_modules\@vladmandic\face-api\model'
$target = Join-Path $ProjectRoot 'jarvis\web\public\models\face-api'

if (-not (Test-Path -LiteralPath $source)) {
  throw "Missing @vladmandic/face-api model source. Run: cd jarvis\web; npm install @vladmandic/face-api"
}

New-Item -ItemType Directory -Force -Path $target | Out-Null

$files = @(
  'tiny_face_detector_model-weights_manifest.json',
  'tiny_face_detector_model.bin',
  'face_landmark_68_model-weights_manifest.json',
  'face_landmark_68_model.bin',
  'face_recognition_model-weights_manifest.json',
  'face_recognition_model.bin',
  'age_gender_model-weights_manifest.json',
  'age_gender_model.bin'
)

foreach ($file in $files) {
  Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $target $file) -Force
}

Write-Host "Installed local face-api model assets to $target"
