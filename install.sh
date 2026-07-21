#!/bin/bash
# Steve installer for a Debian/Ubuntu VM (Proxmox).
# Usage:  unzip steve.zip -d steve && cd steve && bash install.sh
set -e

echo "== installing system packages =="
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv ffmpeg espeak-ng libopus0

echo "== creating venv + python packages =="
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "== VM config tweaks =="
# bind the widget/control API to all interfaces so gui.py (or a browser)
# on another machine can reach the VM (only do this on a trusted LAN)
python3 - <<'EOF'
import json
cfg = json.load(open('config.json'))
cfg['widget']['bindHost'] = '0.0.0.0'
# the model ships inside this zip
cfg['voice']['voskModelPath'] = 'vosk-model-en-us-0.42-gigaspeech'
json.dump(cfg, open('config.json', 'w'), indent=2)
print('config.json updated (bindHost=0.0.0.0, local vosk model)')
EOF

echo
echo "== done =="
echo "IMPORTANT: edit config.json -> ai.ollamaUrl must point at the machine"
echo "running Ollama (e.g. http://192.168.1.10:11434), and on that machine"
echo "Ollama must listen on the LAN:  OLLAMA_HOST=0.0.0.0 ollama serve"
echo
echo "start Steve:            venv/bin/python run.py"
echo "start on boot (optional):"
echo "  sudo tee /etc/systemd/system/steve.service <<UNIT"
echo "  [Unit]"
echo "  Description=Steve AI Stream Companion"
echo "  After=network-online.target"
echo "  [Service]"
echo "  WorkingDirectory=$(pwd)"
echo "  ExecStart=$(pwd)/venv/bin/python run.py"
echo "  Restart=on-failure"
echo "  User=$USER"
echo "  [Install]"
echo "  WantedBy=multi-user.target"
echo "  UNIT"
echo "  sudo systemctl enable --now steve"
