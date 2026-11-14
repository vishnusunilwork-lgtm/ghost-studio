#!/bin/bash
# Ghost Studio installer for Mac (no account needed).
# Run in Terminal:  curl -fsSL https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main/install/mac.sh | bash
set -e
R="https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main"
D="$HOME/Ghost Studio"
mkdir -p "$D/.ghost"
echo "Installing Ghost Studio…"
curl -fsSL "$R/Start%20Ghost%20Studio.command?$(date +%s)" -o "$D/Start Ghost Studio.command"
chmod +x "$D/Start Ghost Studio.command"
curl -fsSL "$R/install/ghost.icns" -o "$D/.ghost/ghost.icns" || true

# A real app icon (Desktop + Applications), opens Ghost Studio with a double-click
make_app() {
  A="$1/Ghost Studio.app"
  rm -rf "$A"; mkdir -p "$A/Contents/MacOS" "$A/Contents/Resources"
  printf '#!/bin/bash\nopen -a Terminal "$HOME/Ghost Studio/Start Ghost Studio.command"\n' > "$A/Contents/MacOS/ghost"
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
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PL
  touch "$A"
}
mkdir -p "$HOME/Applications"
make_app "$HOME/Applications"
make_app "$HOME/Desktop" || true
echo "Done. Open 'Ghost Studio' from your Desktop (or Launchpad). Opening it now…"
open -a Terminal "$D/Start Ghost Studio.command"
