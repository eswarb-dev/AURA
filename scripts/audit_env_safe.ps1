param(
    [string]$EnvPath = (Join-Path (Split-Path -Parent $PSScriptRoot) ".env")
)

$ErrorActionPreference = "Stop"

function Read-AuraEnv {
    param([string]$Path)
    $values = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $values[$matches[1]] = $matches[2].Trim()
        }
    }
    return $values
}

function Get-SafeValueStatus {
    param(
        [hashtable]$Values,
        [string]$Name,
        [switch]$ShowValue
    )
    $value = if ($Values.Contains($Name)) { [string]$Values[$Name] } else { "" }
    if ([string]::IsNullOrWhiteSpace($value)) {
        return "missing"
    }
    if ($ShowValue) {
        return $value
    }
    return "present"
}

$values = Read-AuraEnv -Path $EnvPath

$plainValueNames = @(
    "OPENROUTER_MODEL",
    "OPENROUTER_FALLBACK_MODELS",
    "GROQ_MODEL",
    "FISH_AUDIO_MODEL",
    "AURA_TTS_PROVIDER_PRIORITY",
    "AURA_HOST",
    "AURA_PORT",
    "AURA_SAFE_LOCALHOST_ONLY"
)

$names = @(
    "OPENROUTER_API_KEY",
    "AURA_OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "AURA_GROQ_API_KEY",
    "ELEVENLABS_API_KEY",
    "AURA_ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "AURA_ELEVENLABS_VOICE_ID",
    "FISH_AUDIO_API_KEY",
    "AURA_FISH_AUDIO_API_KEY",
    "FISH_AUDIO_REFERENCE_ID",
    "AURA_FISH_AUDIO_REFERENCE_ID"
) + $plainValueNames

foreach ($name in $names) {
    $show = $plainValueNames -contains $name
    "{0}: {1}" -f $name, (Get-SafeValueStatus -Values $values -Name $name -ShowValue:$show)
}
