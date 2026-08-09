# start_nao_sensei.ps1
#
# Full-system startup for NAO Sensei: brings up Ollama, redeploys and
# restarts the NAO bridge, then launches the main app in its own window.
# Run via start_nao_sensei.bat (double-click it), or directly:
#   powershell -ExecutionPolicy Bypass -File start_nao_sensei.ps1
#
# Machine-specific values live in a git-ignored ".env" file, NOT in this
# script - so nobody's personal paths end up in version control. Copy
# ".env.example" to ".env" and fill in your own values before first use:
#   - NAO_SENSEI_PYTHON: the project env's python.exe (conda env or venv).
#   - NAO_IP: NAO's link-local address over the direct Ethernet cable. It can
#     change if the cable is unplugged/replugged - re-verify with "ping" if
#     this script starts failing at the "NAO bridge" step.
#   - NAO_SSH_KEY: the RSA private key authorized on the robot.

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

# Minimal .env reader: KEY=VALUE lines, "#" comments, %ENVVARS% expanded.
function Get-DotEnv {
    param([string]$Key, [string]$Default)
    $envFile = "$RepoRoot\.env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*#') { continue }
            if ($line -match "^\s*$Key\s*=\s*(.+?)\s*$") {
                return [Environment]::ExpandEnvironmentVariables($Matches[1].Trim('"'))
            }
        }
    }
    return $Default
}

if (-not (Test-Path "$RepoRoot\.env")) {
    Write-Host "  ERROR: no .env file found. Copy .env.example to .env and set your" -ForegroundColor Red
    Write-Host "         machine paths first (see the README 'Configuration' section)." -ForegroundColor Red
    exit 1
}

$PythonExe       = Get-DotEnv -Key "NAO_SENSEI_PYTHON" -Default ""
$NaoIp           = Get-DotEnv -Key "NAO_IP"            -Default "169.254.242.9"
$SshKey          = Get-DotEnv -Key "NAO_SSH_KEY"       -Default "$env:USERPROFILE\.ssh\id_rsa_nao"
$NaoUser         = "nao"
$BridgeRemoteDir = "/home/nao/bridge"
$BridgePort      = 8765

# NAO's OpenSSH 5.9 (2011) predates both ed25519 and the modern default
# ssh-rsa signature negotiation - every call needs these two legacy flags
# or key auth silently fails. See CLAUDE.md's "SSH needed an RSA key" note.
$SshOpts = @(
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-i", $SshKey
)

function Write-Step {
    param([string]$Msg)
    Write-Host ""
    Write-Host "== $Msg ==" -ForegroundColor Cyan
}

function Test-Url {
    param([string]$Url, [int]$TimeoutSec)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Fail {
    param([string]$Msg)
    Write-Host "  ERROR: $Msg" -ForegroundColor Red
    exit 1
}

# Tiny config.yaml reader - just enough to pull "section: / key: value" pairs
# out without adding a YAML module dependency. Never hardcode what's already
# in config.yaml (project rule) - llm.model/llm.host live there, not here.
function Get-ConfigValue {
    param([string]$Section, [string]$Key)
    $lines = Get-Content "$RepoRoot\config.yaml"
    $inSection = $false
    foreach ($line in $lines) {
        if ($line -match "^${Section}:") { $inSection = $true; continue }
        if ($inSection) {
            if ($line -match '^\S') { break }
            if ($line -match "^\s+${Key}:\s*(\S+)") { return $Matches[1] }
        }
    }
    return $null
}

Write-Host "=== NAO Sensei - full system startup ===" -ForegroundColor Yellow

if (-not $PythonExe) {
    Fail "NAO_SENSEI_PYTHON is not set in .env - see .env.example."
}
if (-not (Test-Path $PythonExe)) {
    Fail "python.exe not found at $PythonExe - fix NAO_SENSEI_PYTHON in .env."
}
if (-not (Test-Path $SshKey)) {
    Fail "SSH key not found at $SshKey - see CLAUDE.md's SSH note."
}

$LlmModel = Get-ConfigValue -Section "llm" -Key "model"
$LlmHost  = Get-ConfigValue -Section "llm" -Key "host"
if (-not $LlmModel) { Fail "could not read llm.model out of config.yaml." }
if (-not $LlmHost)  { $LlmHost = "http://localhost:11434" }
# Windows PowerShell 5.1's Invoke-WebRequest does a slow sequential
# IPv6-then-IPv4 fallback for the "localhost" hostname on this machine -
# confirmed live 2026-08-06: 127.0.0.1 answers in ~0.1s, "localhost" takes
# ~2.3s (tries [::1] first, times out, falls back). That's what made this
# step "take way too long" - every single poll attempt below was landing
# right under the old 2s per-attempt timeout and silently failing forever
# even though Ollama was healthy the whole time. Only affects this
# script's own PowerShell checks; config.yaml's llm.host stays "localhost"
# since the Python app's httpx client doesn't have this slowdown.
$LlmHostChk = $LlmHost -replace "localhost", "127.0.0.1"

# --- 1. Ollama --------------------------------------------------------
Write-Step -Msg "1/3 Ollama"
if (Test-Url -Url "$LlmHostChk/api/tags" -TimeoutSec 2) {
    Write-Host "  daemon already running"
} else {
    Write-Host "  not running - starting it (no window)..."
    $ollamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollamaExe) { Fail "ollama.exe not found on PATH." }
    $logsDir = "$RepoRoot\logs"
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }
    Start-Process -WindowStyle Hidden -FilePath $ollamaExe -ArgumentList "serve" `
        -RedirectStandardOutput "$logsDir\ollama_serve.out.log" `
        -RedirectStandardError  "$logsDir\ollama_serve.err.log"
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Url -Url "$LlmHostChk/api/tags" -TimeoutSec 2) { $ok = $true; break }
    }
    if (-not $ok) { Fail "Ollama daemon did not come up within 30s - check logs\ollama_serve.err.log." }
    Write-Host "  daemon up"
}

Write-Host "  making sure the model is loaded ($LlmModel)..."
try {
    $warmBody = @{ model = $LlmModel; prompt = ""; stream = $false } | ConvertTo-Json
    $null = Invoke-RestMethod -Uri "$LlmHostChk/api/generate" -Method Post -Body $warmBody -ContentType "application/json" -TimeoutSec 90
    Write-Host "  model ready"
} catch {
    Fail "model warm-up failed: $($_.Exception.Message)"
}

# --- 2. NAO bridge ------------------------------------------------------
Write-Step -Msg "2/3 NAO bridge"
if (-not (Test-Connection -ComputerName $NaoIp -Count 1 -Quiet)) {
    Fail "NAO not reachable at $NaoIp - check the Ethernet cable, and re-verify the link-local address hasn't changed (CLAUDE.md, Phase 6 live bring-up section)."
}
Write-Host "  NAO reachable, deploying latest bridge code..."
$scpDest = $NaoUser + "@" + $NaoIp + ":" + $BridgeRemoteDir + "/"
& scp @SshOpts "$RepoRoot\nao_bridge\bridge.py" "$RepoRoot\nao_bridge\whitelist.py" "$RepoRoot\nao_bridge\restart_bridge.sh" $scpDest | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "bridge deploy (scp) failed - check the SSH connection." }

Write-Host "  restarting the bridge process on the robot..."
$sshTarget = $NaoUser + "@" + $NaoIp
$remoteCmd = "bash " + $BridgeRemoteDir + "/restart_bridge.sh"
& ssh @SshOpts $sshTarget $remoteCmd | Out-Null

Write-Host "  verifying..."
$ok = $false
$healthUrl = "http://" + $NaoIp + ":" + $BridgePort + "/health"
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Url -Url $healthUrl -TimeoutSec 2) { $ok = $true; break }
}
if (-not $ok) { Fail "bridge did not come up after restart - check it manually over SSH ($sshTarget)." }
Write-Host "  up (stiffness resets to 0 on every bridge restart - the app resends it automatically at lecture Start, per CLAUDE.md's own gotcha on this)"

# --- 3. Main app ----------------------------------------------------------
Write-Step -Msg "3/3 Launching NAO Sensei"
Write-Host "  opening a new console window - the operator console link prints there"
Start-Process -FilePath $PythonExe -ArgumentList "-u", "-m", "app.main" -WorkingDirectory $RepoRoot

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Watch the new console window for the operator console URL and student join info."
