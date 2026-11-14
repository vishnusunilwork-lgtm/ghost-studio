#!/bin/bash
# Ghost Studio installer for Mac (no account needed). Run in Terminal once:
#   curl -fsSL https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main/install/mac.sh | bash
set -e
R="https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main"
D="$HOME/Ghost Studio"
mkdir -p "$D/.ghost" "$D/launcher"
echo "Installing Ghost Studio…"
curl -fsSL "$R/Start%20Ghost%20Studio.command?$(date +%s)" -o "$D/Start Ghost Studio.command"
chmod +x "$D/Start Ghost Studio.command"
curl -fsSL "$R/launcher/starting.html" -o "$D/launcher/starting.html" || true
curl -fsSL "$R/install/ghost.icns" -o "$D/.ghost/ghost.icns" || true

# The Ghost Studio app: click it and the browser opens (no Terminal)
make_app() {
  A="$1/Ghost Studio.app"
  rm -rf "$A"; mkdir -p "$A/Contents/MacOS" "$A/Contents/Resources"
  printf '#!/bin/bash\nexec /bin/bash "$HOME/Ghost Studio/Start Ghost Studio.command"\n' > "$A/Contents/MacOS/ghost"
  chmod +x "$A/Contents/MacOS/ghost"
  [ -f "$D/.ghost/ghost.icns" ] && cp "$D/.ghost/ghost.icns" "$A/Contents/Resources/ghost.icns"
  cat > "$A/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Ghost Studio</string>
<key>CFBundleDisplayName</key><string>Ghost Studio</string>
<key>CFBundleIdentifier</key><string>local.ghoststudio.launcher</string>
<key>CFBundleExecutable</key><string>ghost</string>
<key>CFBundleIconFile</key><string>ghost</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>2.0</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
PL
  touch "$A"
}
mkdir -p "$HOME/Applications"
make_app "$HOME/Applications"
make_app "$HOME/Desktop" || true
echo "Done. From now on just double-click 'Ghost Studio' on your Desktop. Opening it now…"
open "$HOME/Applications/Ghost Studio.app"
