# Ghost Studio for Windows - started by "Start Ghost Studio.cmd" (or the Desktop icon).
# Every start it checks for a newer version and updates itself (no account needed).
# Your ads (projects\), settings (config.local.yaml) and installed parts (.venv\) are never touched.
$ErrorActionPreference = "Continue"
$REPO = "vishnusunilwork-lgtm/ghost-studio"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$G = Join-Path $Here ".ghost"; $Bin = Join-Path $G "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$env:Path = "$Bin;$env:Path"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Clear-Host
Write-Host ""
Write-Host "  O  Ghost Studio" -ForegroundColor White
Write-Host "  ---------------------------------------------"
Write-Host "  Keep this window open while you use the app. To stop: close this window."
Write-Host ""
function Fail($m) { Write-Host "  $m" -ForegroundColor Yellow; Read-Host "  Press Enter to close"; exit 1 }

# ---------- 1) Updates (public project, no sign-in needed) ----------
if ($REPO -like "*/*" -and $args -notcontains "--no-update") {
  Set-Content -Path (Join-Path $G "repo") -Value $REPO -NoNewline
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $remote = ""
  try { $remote = (Invoke-WebRequest "https://raw.githubusercontent.com/$REPO/main/VERSION?$(Get-Random)" -UseBasicParsing -TimeoutSec 15).Content.Trim() } catch {}
  $local = if (Test-Path VERSION) { (Get-Content VERSION -Raw).Trim() } else { "" }
  if ($remote -and (($remote -ne $local) -or -not (Test-Path "app\server.py"))) {
    Write-Host "  Getting Ghost Studio $remote..."
    try {
      $tgz = Join-Path $G "new.tgz"; $new = Join-Path $G "new"
      if (Test-Path $new) { Remove-Item $new -Recurse -Force }
      New-Item -ItemType Directory -Force -Path $new | Out-Null
      Invoke-WebRequest "https://github.com/$REPO/archive/refs/heads/main.tar.gz" -OutFile $tgz -UseBasicParsing
      tar -xzf $tgz -C $new
      $src = Get-ChildItem $new -Directory | Select-Object -First 1
      if (Test-Path (Join-Path $src.FullName "app\server.py")) {
        Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -notin @(".venv", "projects", "config.local.yaml", ".ghost", ".git") } |
          ForEach-Object { Copy-Item $_.FullName -Destination $Here -Recurse -Force }
        Remove-Item $new, $tgz -Recurse -Force
        Write-Host "  Updated."
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Here "ghost.ps1") --no-update
        exit
      }
    } catch { Write-Host "  Could not update right now - starting the version you have." }
  } elseif (-not $remote) { Write-Host "  Could not check for updates (offline?) - starting the version you have." }
}
if (-not (Test-Path "app\server.py")) { Fail "The app files are missing. Check your internet connection, then try again." }

# ---------- 2) Python (own copy, nothing installed system-wide) ----------
$py = Join-Path $Here ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "  Getting Python (one time)..."
  $env:UV_INSTALL_DIR = $Bin; $env:INSTALLER_NO_MODIFY_PATH = "1"
  try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression *> $null } catch {}
  & (Join-Path $Bin "uv.exe") venv --python 3.11 .venv *> $null
  & (Join-Path $Bin "uv.exe") pip install --python $py pip *> $null
  if (-not (Test-Path $py)) { Fail "Could not get Python. Send a screenshot of this window to Vishnu." }
}

# ---------- 3) The app's parts ----------
& $py -c "import fastapi, uvicorn, multipart, yaml, PIL" *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "  Installing the app (one time, ~2 min)..."
  & $py -m pip install -q --upgrade pip
  & $py -m pip install -q fastapi "uvicorn[standard]" python-multipart pyyaml pillow
  if ($LASTEXITCODE -ne 0) { Fail "Install failed. Send a screenshot of this window to Vishnu." }
}

# ---------- 4) ffmpeg (the video editor engine) ----------
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
  & $py -c "import static_ffmpeg" *> $null
  if ($LASTEXITCODE -ne 0) { Write-Host "  Getting the video editor (one time)..."; & $py -m pip install -q static-ffmpeg }
  $ff = (& $py -c "import static_ffmpeg,shutil,os; static_ffmpeg.add_paths(); print(os.path.dirname(shutil.which('ffprobe') or ''))" 2>$null | Select-Object -Last 1)
  if ($ff) { $env:Path = "$ff;$env:Path" }
}

# ---------- 5) Script AI (optional, Ollama) ----------
if (Get-Command ollama -ErrorAction SilentlyContinue) { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden }

# ---------- 6) Start ----------
$ver = if (Test-Path VERSION) { (Get-Content VERSION -Raw).Trim() } else { "" }
Write-Host "  Starting Ghost Studio $ver... your browser opens in a moment."
Start-Job { Start-Sleep 4; Start-Process "http://localhost:7860" } | Out-Null
& $py -m uvicorn app.server:app --host 127.0.0.1 --port 7860
Write-Host ""
Fail "Ghost Studio stopped. If that was unexpected, send a screenshot of this window to Vishnu."
