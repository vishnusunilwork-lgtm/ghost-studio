# Ghost Studio (Windows). The Ghost Studio Desktop icon runs this file with no window.
#   (no option)  open Ghost Studio: start the quiet background engine if needed, then open the browser
#   --service    the background engine: keeps the app running, restarts it after updates
#   --once       one run of the engine: update check, setup, start (used by --service)
# Your ads (projects\), settings (config.local.yaml) and installed parts (.venv\) are never touched.
$ErrorActionPreference = "Continue"
$REPO = "vishnusunilwork-lgtm/ghost-studio"
$PORT = 7860
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$G = Join-Path $Here ".ghost"; $Bin = Join-Path $G "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$env:Path = "$Bin;$env:Path"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Self = Join-Path $Here "ghost.ps1"
$Log = Join-Path $G "server.log"

function Up { try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $PORT); $c.Close(); return $true } catch { return $false } }
function Status($m) { Set-Content -Path (Join-Path $G "status.txt") -Value $m -NoNewline; Add-Content -Path $Log -Value "$(Get-Date -Format HH:mm:ss) $m" }

function Open-App {
  if (-not (Up)) {
    Remove-Item (Join-Path $G "stop") -ErrorAction SilentlyContinue
    if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 5MB)) { Move-Item $Log (Join-Path $G "server.old.log") -Force }
    Start-Process powershell -WindowStyle Hidden -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$Self`"", "--service")
    $page = Join-Path $Here "launcher\starting.html"
    if (Test-Path $page) { Start-Process $page; return }
    for ($i = 0; $i -lt 900 -and -not (Up); $i++) { Start-Sleep 1 }
  }
  Start-Process "http://localhost:$PORT"
}

function Service {
  $fails = 0
  while ($true) {
    $stop = Join-Path $G "stop"
    if (Test-Path $stop) { Remove-Item $stop; Status "Stopped"; return }
    $t0 = Get-Date
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Self --once *>> $Log
    if (((Get-Date) - $t0).TotalSeconds -lt 20) { $fails++ } else { $fails = 0 }
    if ($fails -ge 5) { Status "Could not start. Open Ghost Studio again, or send .ghost\server.log to Vishnu."; return }
    Start-Sleep 2
  }
}

function Once {
  # 1) updates (public project, no sign-in)
  Set-Content -Path (Join-Path $G "repo") -Value $REPO -NoNewline
  $remote = ""
  try { $remote = (Invoke-WebRequest "https://raw.githubusercontent.com/$REPO/main/VERSION?$(Get-Random)" -UseBasicParsing -TimeoutSec 15).Content.Trim() } catch {}
  $local = if (Test-Path VERSION) { (Get-Content VERSION -Raw).Trim() } else { "" }
  if ($remote -and (($remote -ne $local) -or -not (Test-Path "app\server.py"))) {
    Status "Getting Ghost Studio $remote..."
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
        Status "Updated to $remote"
        exit 0
      }
    } catch { Status "Could not update right now - starting the version you have." }
  }
  if (-not (Test-Path "app\server.py")) { Status "Could not download Ghost Studio. Check your internet."; exit 1 }

  # 2) Python (own copy)
  $py = Join-Path $Here ".venv\Scripts\python.exe"
  if (-not (Test-Path $py)) {
    Status "Setting up (one time, about 5 minutes)..."
    $env:UV_INSTALL_DIR = $Bin; $env:INSTALLER_NO_MODIFY_PATH = "1"
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression *> $null } catch {}
    & (Join-Path $Bin "uv.exe") venv --python 3.11 .venv *> $null
    & (Join-Path $Bin "uv.exe") pip install --python $py pip *> $null
    if (-not (Test-Path $py)) { Status "Could not get Python."; exit 1 }
  }

  # 3) the app's parts
  & $py -c "import fastapi, uvicorn, multipart, yaml, PIL" *> $null
  if ($LASTEXITCODE -ne 0) {
    Status "Installing the app (one time, about 2 minutes)..."
    & $py -m pip install -q --upgrade pip
    & $py -m pip install -q fastapi "uvicorn[standard]" python-multipart pyyaml pillow
    if ($LASTEXITCODE -ne 0) { Status "Install failed."; exit 1 }
  }

  # 4) ffmpeg
  if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    & $py -c "import static_ffmpeg" *> $null
    if ($LASTEXITCODE -ne 0) { Status "Getting the video editor (one time)..."; & $py -m pip install -q static-ffmpeg }
    $ff = (& $py -c "import static_ffmpeg,shutil,os; static_ffmpeg.add_paths(); print(os.path.dirname(shutil.which('ffprobe') or ''))" 2>$null | Select-Object -Last 1)
    if ($ff) { $env:Path = "$ff;$env:Path" }
  }

  # 5) optional script AI
  if (Get-Command ollama -ErrorAction SilentlyContinue) { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden }

  # 6) start
  Status "Ready"
  $env:GHOST_SERVICE = "1"; $env:GHOST_PORT = "$PORT"
  & $py -m uvicorn app.server:app --host 127.0.0.1 --port $PORT --log-level warning
  exit $LASTEXITCODE
}

switch ($args[0]) {
  "--service" { Service }
  "--once" { Once }
  default { Open-App }
}
