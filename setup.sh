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

migrate_from_non_git() {
  # not a git checkout (old zip/manual install, or a non-git copy of the
  # new code) - back it up, clone fresh from GitHub, and carry over the
  # bot token + Vosk model so nothing has to be re-entered/re-downloaded
  HERE="$(pwd)"
  NAME="$(basename "$HERE")"
  cd ..
  BACKUP="${NAME}-old-$(date +%Y%m%d%H%M%S)"
  mv "$HERE" "$BACKUP"
  git clone "$REPO_URL" "$NAME"
  cd "$NAME"
  if [ -d "../$BACKUP/vosk-model-en-us-0.42-gigaspeech" ]; then
    mv "../$BACKUP/vosk-model-en-us-0.42-gigaspeech" .
  elif [ -d "../$BACKUP/vosk-model-en-us-0.22-lgraph" ]; then
    mv "../$BACKUP/vosk-model-en-us-0.22-lgraph" .   # old smaller model - setup.sh will fetch the better one below
  fi
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
}

if [ -f "run.py" ]; then
  # already sitting inside some kind of Steve checkout - every run pulls
  # the latest from GitHub, one way or another, instead of trusting
  # whatever's already on disk
  if [ -d ".git" ]; then
    echo "== existing git install detected - pulling latest from GitHub =="
    if git fetch origin && git reset --hard origin/main; then
      echo "  now on latest origin/main"
    else
      echo "  git pull failed (no network / repo issue?) - migrating to a fresh clone instead"
      migrate_from_non_git
    fi
  else
    echo "== not a git checkout - migrating to a fresh clone from GitHub =="
    migrate_from_non_git
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
# gigaspeech is Vosk's most accurate English model (~2.3GB) - worth the extra
# download/RAM over the older lgraph model for real transcription quality
if [ ! -d "vosk-model-en-us-0.42-gigaspeech" ]; then
  wget -q --show-progress https://alphacephei.com/vosk/models/vosk-model-en-us-0.42-gigaspeech.zip
  unzip -q vosk-model-en-us-0.42-gigaspeech.zip
  rm vosk-model-en-us-0.42-gigaspeech.zip
else
  echo "  (already present)"
fi
if [ -d "vosk-model-en-us-0.22-lgraph" ]; then
  echo "  (old vosk-model-en-us-0.22-lgraph is no longer used - safe to delete: rm -rf vosk-model-en-us-0.22-lgraph)"
fi

echo "== config =="
if [ ! -f "config.json" ]; then
  cp config.example.json config.json
  echo "  created config.json from the template - fill in discord.botToken and"
  echo "  discord.adminUserId (via the admin UI once running, or by hand)"
else
  echo "  (config.json already exists - leaving it alone)"
fi

echo "== network access =="
# bind the admin UI / live display to this machine's LAN IP (not just
# 127.0.0.1) so you can reach it from another device - fine on a trusted
# home/club LAN, don't expose this port to the open internet
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$LOCAL_IP" ]; then
  LOCAL_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
fi
if [ -z "$LOCAL_IP" ]; then
  LOCAL_IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -n1)"
fi
if [ -z "$LOCAL_IP" ]; then
  LOCAL_IP="$(hostname -i 2>/dev/null | awk '{print $1}')"
fi
if [ -n "$LOCAL_IP" ]; then
  python3 - "$LOCAL_IP" <<'PYEOF'
import json, sys
ip = sys.argv[1]
cfg = json.load(open("config.json"))
cfg.setdefault("widget", {})["bindHost"] = "0.0.0.0"
json.dump(cfg, open("config.json", "w"), indent=2)
print(f"  widget.bindHost set to 0.0.0.0 - reachable at http://{ip}:8789/admin")
PYEOF
else
  echo "  couldn't detect a LAN IP - leave widget.bindHost as-is or set it by hand"
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
ExecStart=$WORKDIR/venv/bin/python -u $WORKDIR/run.py
Restart=on-failure
RestartSec=5
User=$RUN_USER

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable steve
  sudo systemctl restart steve
  echo "  installed, enabled on boot, and (re)started as a systemd service: steve"
  if ! python3 -c "import json,sys; sys.exit(0 if json.load(open('config.json'))['discord']['botToken'] else 1)" 2>/dev/null; then
    echo "  no discord.botToken set yet - the admin UI still runs, set it there, then:"
    echo "    sudo systemctl restart steve"
  fi
  echo "  check status:  systemctl status steve"
  echo "  view logs:     journalctl -u steve -f"
  echo "  restart after config changes:  sudo systemctl restart steve"
else
  echo "  (no systemd found - start manually with: venv/bin/python run.py, and re-run it after reboots)"
fi

ADMIN_HOST="${LOCAL_IP:-127.0.0.1}"
echo
echo "== done =="
echo "admin UI + bot token / channel setup:  http://$ADMIN_HOST:8789/admin"
echo "manual start (if not using systemd):   venv/bin/python run.py"
echo "control panel (desktop only, needs a display - not this headless box):  venv/bin/python gui.py"
