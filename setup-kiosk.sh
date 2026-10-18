#!/bin/bash
# Wojtek Stock - kiosk setup (adapted from the Wojtek Locator's) for Linux Mint (XFCE, Cinnamon or MATE).
# Run once as the user who will be logged in at the shop screen (not with sudo):
#     bash setup-kiosk.sh                 (opens http://10.42.10.246:8090/ and asks for the shop password)
# Terminal: bash setup-kiosk.sh 'http://10.42.10.246:8090/terminal/KEY'   (the setup link from the People tab)
#
# It sets up:
#   - a separate Firefox profile just for the kiosk, so it never clashes with normal browsing
#   - a launcher that waits for the Wojtek Stock server, opens it full screen, and reopens it if closed
#   - an autostart entry so it opens at login
#   - screen blanking, sleep and screen lock off, so the screen stays up
# To leave kiosk mode for maintenance: press Ctrl+Alt+T for a terminal and run:  wojtek-stock-kiosk stop
# To start it again without logging out:  wojtek-stock-kiosk start
set -e
if [ "$(id -u)" = 0 ]; then echo "Run this as your normal user, not with sudo." >&2; exit 1; fi

URL="${1:-http://10.42.10.246:8090/}"
PROFILE="$HOME/.wojtek-stock-kiosk-profile"
BIN="$HOME/.local/bin/wojtek-stock-kiosk"
mkdir -p "$PROFILE" "$HOME/.local/bin" "$HOME/.config/autostart"

# Firefox settings for an unattended screen
cat > "$PROFILE/user.js" <<'EOF'
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.translations.automaticallyPopup", false);
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
EOF

# The launcher
cat > "$BIN" <<EOF
#!/bin/bash
URL="$URL"
PROFILE="$PROFILE"
case "\$1" in
  stop)
    pkill -f "wojtek-stock-kiosk run" || true
    pkill -f -- "--profile \$PROFILE" || true
    exit 0 ;;
  start)
    nohup "\$0" run >/dev/null 2>&1 &
    exit 0 ;;
  run) ;;
  *) echo "usage: wojtek-stock-kiosk start|stop"; exit 1 ;;
esac
# Wait up to 3 minutes for the Wojtek Stock server (it starts on its own at boot)
for i in \$(seq 1 90); do curl -s -o /dev/null -m 3 "\$URL" && break; sleep 2; done
# Keep the kiosk up: if the window gets closed, reopen it
while true; do
  firefox --kiosk --no-remote --profile "\$PROFILE" "\$URL"
  sleep 3
done
EOF
chmod +x "$BIN"

# Open at login
cat > "$HOME/.config/autostart/wojtek-stock-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Wojtek Stock (kiosk)
Comment=Opens Wojtek Stock full screen
Exec=$BIN run
X-GNOME-Autostart-enabled=true
Hidden=false
EOF

# Keep the screen on: no blanking, no sleep, no lock (XFCE). Missing settings are created.
if command -v xfconf-query >/dev/null; then
  pm=/xfce4-power-manager
  for p in dpms-enabled:bool:false blank-on-ac:int:0 dpms-on-ac-sleep:int:0 dpms-on-ac-off:int:0 \
           inactivity-on-ac:int:0 lock-screen-suspend-hibernate:bool:false; do
    IFS=: read -r key type val <<< "$p"
    xfconf-query -c xfce4-power-manager -p "$pm/$key" -n -t "$type" -s "$val" 2>/dev/null || true
  done
  xfconf-query -c xfce4-screensaver -p /saver/enabled -n -t bool -s false 2>/dev/null || true
  xfconf-query -c xfce4-screensaver -p /lock/enabled -n -t bool -s false 2>/dev/null || true
  xfconf-query -c xfce4-session -p /shutdown/LockScreen -n -t bool -s false 2>/dev/null || true
fi
# Cinnamon and MATE editions keep these settings in gsettings instead
if command -v gsettings >/dev/null; then
  gs() { gsettings set "$@" 2>/dev/null || true; }
  gs org.cinnamon.desktop.session idle-delay 0
  gs org.cinnamon.desktop.screensaver lock-enabled false
  gs org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0
  gs org.cinnamon.settings-daemon.plugins.power sleep-inactive-ac-timeout 0
  gs org.cinnamon.settings-daemon.plugins.power lid-close-ac-action "'nothing'"
  gs org.mate.screensaver idle-activation-enabled false
  gs org.mate.screensaver lock-enabled false
  gs org.mate.power-manager sleep-display-ac 0
  gs org.mate.power-manager sleep-computer-ac 0
  gs org.mate.power-manager button-lid-ac "'nothing'"
fi
# XFCE: closing the lid on the charger does nothing, so a laptop can run lid-down on a shelf
command -v xfconf-query >/dev/null && xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/lid-action-on-ac \
  -n -t int -s 0 2>/dev/null || true
command -v xset >/dev/null && { xset s off; xset -dpms; } 2>/dev/null || true

echo
echo "Kiosk set up. It opens $URL full screen at every login."
echo "  Start it now:          wojtek-stock-kiosk start"
echo "  Leave kiosk mode:      Ctrl+Alt+T for a terminal, then: wojtek-stock-kiosk stop"
echo "  Turn autostart off:    rm ~/.config/autostart/wojtek-stock-kiosk.desktop"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  (Open a new terminal first so 'wojtek-stock-kiosk' is found, or use $BIN)";; esac
