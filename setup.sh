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

if [ ! -f "run.py" ]; then
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

echo
echo "== done =="
echo "start Steve:      venv/bin/python run.py"
echo "or the GUI:       venv/bin/python gui.py"
echo "then set your bot token at:  http://127.0.0.1:8789/admin"
