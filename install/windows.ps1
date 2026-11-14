# Ghost Studio installer for Windows (no account needed). Run in PowerShell once:
#   irm https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main/install/windows.ps1 | iex
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$R = "https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main"
$D = Join-Path $env:USERPROFILE "Ghost Studio"
New-Item -ItemType Directory -Force -Path (Join-Path $D "launcher"), (Join-Path $D ".ghost") | Out-Null
Write-Host "Installing Ghost Studio..."
Invoke-WebRequest "$R/ghost.ps1" -OutFile (Join-Path $D "ghost.ps1") -UseBasicParsing
Invoke-WebRequest "$R/launcher/launch.vbs" -OutFile (Join-Path $D "launcher\launch.vbs") -UseBasicParsing
Invoke-WebRequest "$R/launcher/starting.html" -OutFile (Join-Path $D "launcher\starting.html") -UseBasicParsing
Invoke-WebRequest "$R/install/ghost.ico" -OutFile (Join-Path $D ".ghost\ghost.ico") -UseBasicParsing
$ws = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
  $l = $ws.CreateShortcut((Join-Path $dir "Ghost Studio.lnk"))
  $l.TargetPath = "$env:WINDIR\System32\wscript.exe"
  $l.Arguments = "`"" + (Join-Path $D "launcher\launch.vbs") + "`""
  $l.WorkingDirectory = $D
  $l.IconLocation = (Join-Path $D ".ghost\ghost.ico")
  $l.Save()
}
Write-Host "Done. From now on just double-click 'Ghost Studio' on your Desktop. Opening it now..."
Start-Process "$env:WINDIR\System32\wscript.exe" -ArgumentList ("`"" + (Join-Path $D "launcher\launch.vbs") + "`"")
