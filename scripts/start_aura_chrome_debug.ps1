param(
    [string]$Port = "9222",
    [string]$DebugAddress = "127.0.0.1",
    [string]$ProfilePath = "$env:LOCALAPPDATA\AURA-Chrome"
)

$ChromePaths = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)

$chrome = $null
foreach ($path in $ChromePaths) {
    if (Test-Path -LiteralPath $path) {
        $chrome = $path
        break
    }
}

if (-not $chrome) {
    Write-Host "[AURA CHROME DEBUG] ERROR: Chrome not found in safe launcher paths." -ForegroundColor Red
    Write-Host "[AURA CHROME DEBUG] Set AURA_CHROME_PATH environment variable to override." -ForegroundColor Yellow
    exit 1
}

Write-Host "[AURA CHROME DEBUG] Locating Chrome: $chrome" -ForegroundColor Cyan
Write-Host "[AURA CHROME DEBUG] Profile: $ProfilePath" -ForegroundColor Cyan
Write-Host "[AURA CHROME DEBUG] Port: $Port" -ForegroundColor Cyan
Write-Host "[AURA CHROME DEBUG] Debug Address: $DebugAddress" -ForegroundColor Cyan

if (Test-Path -LiteralPath $ProfilePath) {
    Write-Host "[AURA CHROME DEBUG] Profile directory exists. Reusing." -ForegroundColor Yellow
} else {
    New-Item -ItemType Directory -Path $ProfilePath -Force | Out-Null
    Write-Host "[AURA CHROME DEBUG] Created profile directory." -ForegroundColor Green
}

$args = @(
    "--remote-debugging-port=$Port"
    "--remote-debugging-address=$DebugAddress"
    "--user-data-dir=`"$ProfilePath`""
    "--no-first-run"
    "--disable-extensions"
    "--no-sandbox"
)

Write-Host "[AURA CHROME DEBUG] Starting Chrome with args:" -ForegroundColor Cyan
Write-Host "  $chrome $($args -join ' ')" -ForegroundColor Cyan

try {
    $process = Start-Process -FilePath $chrome -ArgumentList $args -PassThru -NoNewWindow
    Write-Host "[AURA CHROME DEBUG] Chrome process started. PID: $($process.Id)" -ForegroundColor Green
} catch {
    Write-Host "[AURA CHROME DEBUG] ERROR: Failed to start Chrome: $_" -ForegroundColor Red
    exit 1
}

$maxWait = 10
$elapsed = 0
$endpointUrl = "http://${DebugAddress}:${Port}/json/version"

while ($elapsed -lt $maxWait) {
    Start-Sleep -Milliseconds 500
    $elapsed += 0.5
    try {
        $response = Invoke-RestMethod -Uri $endpointUrl -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($response.Browser -and $response.'webSocketDebuggerUrl') {
            Write-Host ""
            Write-Host "[AURA CHROME DEBUG] SUCCESS" -ForegroundColor Green
            Write-Host "[AURA CHROME DEBUG] Browser: $($response.Browser)" -ForegroundColor Green
            Write-Host "[AURA CHROME DEBUG] Protocol-Version: $($response.'Protocol-Version')" -ForegroundColor Green
            Write-Host "[AURA CHROME DEBUG] WebSocket: $($response.'webSocketDebuggerUrl')" -ForegroundColor Green
            Write-Host "[AURA CHROME DEBUG] Endpoint: $endpointUrl" -ForegroundColor Green
            Write-Host ""
            Write-Host "Set AURA_BROWSER_MCP_ENABLED=true" -ForegroundColor Cyan
            Write-Host "Set AURA_BROWSER_MCP_MODE=browser-url" -ForegroundColor Cyan
            Write-Host "Set AURA_BROWSER_MCP_BROWSER_URL=$endpointUrl" -ForegroundColor Cyan
            Write-Host "Set AURA_BROWSER_WINDOW_MODE=separate" -ForegroundColor Cyan
            exit 0
        }
    } catch {
        # Not ready yet
    }
}

Write-Host ""
Write-Host "[AURA CHROME DEBUG] TIMEOUT: Chrome debug endpoint not available after ${maxWait}s." -ForegroundColor Red
Write-Host "[AURA CHROME DEBUG] Check if Chrome is running and try again." -ForegroundColor Yellow
Write-Host "[AURA CHROME DEBUG] Process PID: $($process.Id)" -ForegroundColor Yellow
exit 1
