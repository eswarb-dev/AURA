Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$supabaseUrl = [Environment]::GetEnvironmentVariable("SUPABASE_URL")
$serviceRoleKey = [Environment]::GetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY")
$table = [Environment]::GetEnvironmentVariable("SUPABASE_KEEPALIVE_TABLE")

if ([string]::IsNullOrWhiteSpace($table)) {
  $table = "aura_keepalive"
}

if ([string]::IsNullOrWhiteSpace($supabaseUrl)) {
  Write-Host "Missing required environment variable: SUPABASE_URL"
  exit 1
}

if ([string]::IsNullOrWhiteSpace($serviceRoleKey)) {
  Write-Host "Missing required environment variable: SUPABASE_SERVICE_ROLE_KEY"
  exit 1
}

$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$endpoint = "$supabaseUrl/rest/v1/$table" + "?id=eq.1"
$body = @{
  touched_at = $now
  source = "local_validation"
  note = "A.U.R.A local Supabase keepalive validation"
} | ConvertTo-Json -Compress

Write-Host "Running Supabase keepalive validation at $now"
Write-Host "Target table: $table"

try {
  $response = Invoke-WebRequest `
    -Uri $endpoint `
    -Method Patch `
    -TimeoutSec 30 `
    -Headers @{
      "apikey" = $serviceRoleKey
      "Authorization" = "Bearer $serviceRoleKey"
      "Content-Type" = "application/json"
      "Prefer" = "return=minimal"
    } `
    -Body $body `
    -UseBasicParsing

  $statusCode = [int]$response.StatusCode
  Write-Host "Supabase keepalive HTTP status: $statusCode"
  if ($statusCode -lt 200 -or $statusCode -ge 300) {
    Write-Host "Supabase keepalive failed."
    exit 1
  }
  Write-Host "Supabase keepalive succeeded."
} catch {
  $statusCode = $null
  if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
    $statusCode = [int]$_.Exception.Response.StatusCode
    Write-Host "Supabase keepalive HTTP status: $statusCode"
  }
  if ($statusCode -eq 404) {
    Write-Host "Supabase keepalive table was not found. Run docs/supabase/aura_keepalive_schema.sql in the Supabase SQL Editor."
  } else {
    Write-Host "Supabase keepalive failed."
  }
  Write-Host "Response body redacted by default. Check table, REST API status, and environment variables."
  exit 1
}
