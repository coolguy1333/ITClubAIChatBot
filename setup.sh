#!/bin/bash
# One-line setup for the IT Club AI assistant "Steve".
#
#   curl -fsSL https://raw.githubusercontent.com/coolguy1333/ITClubAIChatBot/main/setup.sh | bash
#
# or, if you've already cloned the repo:
#
#   bash setup.sh
#
# Installs system + Python deps, grabs the Vosk speech model if missing,
# and creates config.json from the template so first run doesn't crash.
set -e

REPO_URL="${STEVE_REPO_URL:-https://github.com/coolguy1333/ITClubAIChatBot.git}"
DIR="steve"

if [ -f "run.py" ]; then
  # already sitting inside some kind of Steve checkout - figure out what
  # kind and refresh it instead of assuming it's already good to go
  if [ -d ".git" ]; then
    echo "== existing git install detected - pulling latest and reinstalling =="
    git fetch origin
    git reset --hard origin/main
  elif [ -f "twitch.py" ] || [ ! -f "config.example.json" ]; then
    echo "== old-style Steve install detected here - migrating to a fresh clone =="
    HERE="$(pwd)"
    NAME="$(basename "$HERE")"
    cd ..
    BACKUP="${NAME}-old-$(date +%Y%m%d%H%M%S)"
    mv "$HERE" "$BACKUP"
    git clone "$REPO_URL" "$NAME"
    cd "$NAME"
    # reuse the Vosk model instead of re-downloading a gigabyte-plus
    if [ -d "../$BACKUP/vosk-model-en-us-0.22-lgraph" ]; then
      mv "../$BACKUP/vosk-model-en-us-0.22-lgraph" .
    fi
    # carry over the bot token and any other matching settings from the old config
    if [ -f "../$BACKUP/config.json" ]; then
      cp config.example.json config.json
      python3 - "../$BACKUP/config.json" <<'PYEOF'
import json, sys
old = json.load(open(sys.argv[1]))
new = json.load(open("config.json"))
def merge(o, n):
    for k, v in o.items():
        if isinstance(v, dict) and isinstance(n.get(k), dict):
            merge(v, n[k])
        elif k in n:
            n[k] = v
merge(old, new)
json.dump(new, open("config.json", "w"), indent=2)
print("  carried over matching settings (including your bot token) from the old config.json")
PYEOF
    fi
    echo "  old install backed up at: $(pwd)/../$BACKUP (delete it once you've confirmed the new one works)"
  else
    echo "== existing install detected - reinstalling in place =="
  fi
else
  echo "== cloning $REPO_URL =="
  git clone "$REPO_URL" "$DIR"
  cd "$DIR"
fi

echo "== system packages (needs sudo) =="
if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-pip python3-venv ffmpeg espeak-ng libopus0 unzip wget
else
  echo "  (skipping - not a Debian/Ubuntu system; make sure Python 3.10+, ffmpeg, and unzip are installed)"
fi

echo "== python packages =="
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo "== speech model =="
if [ ! -d "vosk-model-en-us-0.22-lgraph" ]; then
  wget -q --show-progress https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip
  unzip -q vosk-model-en-us-0.22-lgraph.zip
  rm vosk-model-en-us-0.22-lgraph.zip
else
  echo "  (already present)"
fi

echo "== config =="
if [ ! -f "config.json" ]; then
  cp config.example.json config.json
  echo "  created config.json from the template - fill in discord.botToken and"
  echo "  discord.adminUserId (via the admin UI once running, or by hand)"
else
  echo "  (config.json already exists - leaving it alone)"
fi

echo "== auto-start on boot (systemd) =="
if command -v systemctl >/dev/null; then
  WORKDIR="$(pwd)"
  RUN_USER="${SUDO_USER:-$USER}"
  sudo tee /etc/systemd/system/steve.service > /dev/null <<UNIT
[Unit]
Description=Steve - IT Club AI Assistant (Discord bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WORKDIR
ExecStart=$WORKDIR/venv/bin/python $WORKDIR/run.py
Restart=on-failure
RestartSec=5
User=$RUN_USER

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable steve
  if [ -s "config.json" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('config.json'))['discord']['botToken'] else 1)" 2>/dev/null; then
    sudo systemctl restart steve
    echo "  installed, enabled on boot, and (re)started as a systemd service: steve"
  else
    echo "  installed and enabled on boot as a systemd service: steve"
    echo "  NOT starting it yet - config.json has no discord.botToken. Set one, then:"
    echo "    sudo systemctl start steve"
  fi
  echo "  check status:  systemctl status steve"
  echo "  view logs:     journalctl -u steve -f"
  echo "  restart after config changes:  sudo systemctl restart steve"
else
  echo "  (no systemd found - start manually with: venv/bin/python run.py, and re-run it after reboots)"
fi

echo
echo "== done =="
echo "admin UI + bot token / channel setup:  http://127.0.0.1:8789/admin"
echo "manual start (if not using systemd):   venv/bin/python run.py"
echo "control panel (desktop, not headless):  venv/bin/python gui.py"
