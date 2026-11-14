# Ghost Studio installer for Windows (no account needed).
# Run in PowerShell:  irm https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main/install/windows.ps1 | iex
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$R = "https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main"
$D = Join-Path $env:USERPROFILE "Ghost Studio"
New-Item -ItemType Directory -Force -Path $D | Out-Null
Write-Host "Installing Ghost Studio..."
Invoke-WebRequest "$R/ghost.ps1" -OutFile (Join-Path $D "ghost.ps1") -UseBasicParsing
Invoke-WebRequest "$R/Start%20Ghost%20Studio.cmd" -OutFile (Join-Path $D "Start Ghost Studio.cmd") -UseBasicParsing
$ws = New-Object -ComObject WScript.Shell
$l = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Ghost Studio.lnk"))
$l.TargetPath = Join-Path $D "Start Ghost Studio.cmd"; $l.WorkingDirectory = $D; $l.Save()
Write-Host "Done. A 'Ghost Studio' icon is on your Desktop. Opening it now..."
Start-Process (Join-Path $D "Start Ghost Studio.cmd")
