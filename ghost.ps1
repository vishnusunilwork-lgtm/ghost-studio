# Ghost Studio for Windows - started by "Start Ghost Studio.cmd" (or the Desktop icon).
# Every start it checks the team's GitHub project for a newer version and updates itself.
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

# ---------- 1) Updates from the team project ----------
if ($REPO -like "*/*" -and $args -notcontains "--no-update") {
  Set-Content -Path (Join-Path $G "repo") -Value $REPO -NoNewline
  $gh = Join-Path $Bin "gh.exe"
  if (-not (Test-Path $gh)) {
    Write-Host "  Getting the GitHub helper (one time)..."
    try {
      $rel = Invoke-RestMethod "https://api.github.com/repos/cli/cli/releases/latest" -UseBasicParsing
      $a = $rel.assets | Where-Object { $_.name -like "*_windows_amd64.zip" } | Select-Object -First 1
      $z = Join-Path $G "gh.zip"; Invoke-WebRequest $a.browser_download_url -OutFile $z -UseBasicParsing
      Expand-Archive $z -DestinationPath (Join-Path $G "ghx") -Force
      Copy-Item (Get-ChildItem (Join-Path $G "ghx") -Recurse -Filter gh.exe | Select-Object -First 1).FullName $gh
      Remove-Item $z, (Join-Path $G "ghx") -Recurse -Force
    } catch { Write-Host "  Could not get the GitHub helper - starting without updates." }
  }
  if (Test-Path $gh) {
    & $gh auth status -h github.com *> $null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "  First time only: sign in to GitHub so the app can get updates."
      Write-Host "  1. A code appears below.  2. Press Enter - your browser opens."
      Write-Host "  3. Paste the code there and click Authorize."
      Write-Host ""
      & $gh auth login -h github.com -p https --web
    }
    $remote = (& $gh api "repos/$REPO/commits/main" --jq .sha 2>$null)
    $local = if (Test-Path (Join-Path $G "version")) { (Get-Content (Join-Path $G "version") -Raw).Trim() } else { "" }
    if ($remote -and $remote -ne $local) {
      Write-Host "  Getting the newest version..."
      try {
        $tok = (& $gh auth token).Trim()
        $tgz = Join-Path $G "new.tgz"; $new = Join-Path $G "new"
        if (Test-Path $new) { Remove-Item $new -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $new | Out-Null
        Invoke-WebRequest "https://api.github.com/repos/$REPO/tarball/$remote" -Headers @{ Authorization = "Bearer $tok" } -OutFile $tgz -UseBasicParsing
        tar -xzf $tgz -C $new
        $src = Get-ChildItem $new -Directory | Select-Object -First 1
        if (Test-Path (Join-Path $src.FullName "app\server.py")) {
          Get-ChildItem $src.FullName -Force | Where-Object { $_.Name -notin @(".venv", "projects", "config.local.yaml", ".ghost", ".git") } |
            ForEach-Object { Copy-Item $_.FullName -Destination $Here -Recurse -Force }
          Set-Content -Path (Join-Path $G "version") -Value $remote -NoNewline
          Remove-Item $new, $tgz -Recurse -Force
          Write-Host "  Updated."
          & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Here "ghost.ps1") --no-update
          exit
        }
      } catch { Write-Host "  Could not update right now - starting the version you have." }
    }
  }
}
if (-not (Test-Path "app\server.py")) { Fail "The app files are missing. Check your internet and that Vishnu invited your GitHub account, then try again." }

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
