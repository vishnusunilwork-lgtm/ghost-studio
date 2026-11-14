# AI Film Studio Local - one-time setup for Windows (NVIDIA GPU PC)
# Right-click this file > "Run with PowerShell". Takes 20-60 min (big downloads).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "== 1/5 Installing Python 3.11, ffmpeg and Ollama ==" -ForegroundColor Cyan
winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
Write-Host "== 2/5 Creating the app's own Python ==" -ForegroundColor Cyan
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
Write-Host "== 3/5 Installing PyTorch for NVIDIA GPUs ==" -ForegroundColor Cyan
.\.venv\Scripts\pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
Write-Host "== 4/5 Installing the app ==" -ForegroundColor Cyan
.\.venv\Scripts\pip install -r requirements.txt
Write-Host "== 5/5 Downloading the script-writing models (about 15 GB) ==" -ForegroundColor Cyan
Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep 5
ollama pull qwen2.5:14b
ollama pull qwen2.5vl:7b
.\.venv\Scripts\python -c "import torch;print('GPU found:',torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
Write-Host "Done! Double-click run.bat to start." -ForegroundColor Green
Read-Host "Press Enter to close"
