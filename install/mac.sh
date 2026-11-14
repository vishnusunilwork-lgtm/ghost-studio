#!/bin/bash
# Ghost Studio installer for Mac (no account needed).
# Run in Terminal:  curl -fsSL https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main/install/mac.sh | bash
set -e
R="https://raw.githubusercontent.com/vishnusunilwork-lgtm/ghost-studio/main"
D="$HOME/Ghost Studio"
mkdir -p "$D"
echo "Installing Ghost Studio…"
curl -fsSL "$R/Start%20Ghost%20Studio.command?$(date +%s)" -o "$D/Start Ghost Studio.command"
chmod +x "$D/Start Ghost Studio.command"
printf '#!/bin/bash\nexec "$HOME/Ghost Studio/Start Ghost Studio.command"\n' > "$HOME/Desktop/Ghost Studio.command"
chmod +x "$HOME/Desktop/Ghost Studio.command"
echo "Done. A 'Ghost Studio' icon is on your Desktop. Opening it now…"
open -a Terminal "$D/Start Ghost Studio.command"
