# IT Club AI Assistant — "Steve"

A single-process Discord bot that acts as an AI assistant for the IT Club.

> **New here? See [SETUP.md](SETUP.md) for full step-by-step setup instructions.**

## What it does

- **Talk to Steve on Discord** — DM the bot and it chats back with rolling
  conversation memory. (This uses the official bot token, *not* a self-bot —
  self-bots are against Discord ToS and can get your personal account banned.)
- **Server chat** — in server channels Steve answers @mentions and any
  message containing "steve", and can auto-chime into conversation every
  N messages. Designate one channel (`discord.alwaysRespondChannelId`) as a
  dedicated Q&A channel where he answers every message — no name or /ask
  needed.
- **Live display (optional)** — a small local webpage with a speech bubble
  + browser TTS that mirrors everything Steve says publicly (handy for a
  screen in the club room). Nothing here is Discord-specific; it's just an
  optional viewer.
- **Voice calls** — `/join` while you're in a voice channel and Steve hops in,
  transcribes everyone locally with Vosk (no cloud STT), and only replies
  when someone actually says "steve" or "hey steve" (`voice.requireWakeWord`)
  — otherwise he just listens quietly. Replies through the AI and speaks
  back with TTS (pyttsx3 + ffmpeg). `/leave` to kick him out,
  `/hallucination` to teach him phrases that are actually mic noise.
  Auto-reconnects if the connection drops.
- **Casual / meeting modes** — a manual toggle between a relaxed "casual"
  system prompt and a more focused "meeting" prompt for when club meetings
  are in session. Switch with `/casual` and `/meeting`.

## Quick start (Linux / a VM or LXC)

```bash
curl -fsSL https://raw.githubusercontent.com/coolguy1333/ITClubAIChatBot/main/setup.sh | bash
```

(or, if you've already cloned the repo, just `bash setup.sh` from inside it)

That installs system + Python deps, grabs the Vosk speech model if it's not
already there, creates `config.json` from `config.example.json`, and
installs + enables a `steve` systemd service so it starts on boot and
restarts on crash. Set your bot token and admin user ID at
`http://127.0.0.1:8789/admin` (or edit `config.json` by hand), then:

```bash
sudo systemctl start steve      # if it wasn't auto-started (no token set yet)
systemctl status steve          # check it's running
journalctl -u steve -f          # tail its logs
```

**Re-running `setup.sh` is safe and idempotent** — it detects what's already
there and reinstalls/updates accordingly:
- an existing git checkout → `git fetch` + `git reset --hard` to the latest, then reinstalls deps and the systemd service
- an old/pre-cleanup install (e.g. still has `twitch.py`) → backs it up alongside itself, clones fresh, and carries over your bot token + Vosk model automatically
- nothing there yet → clones and installs from scratch

So updating Steve to the latest version later is just: re-run the same
one-liner.

## Manual setup (any OS, e.g. this Windows PC)

```bash
pip install -r requirements.txt
copy config.example.json config.json   # then fill in botToken / adminUserId
python gui.py     # control panel (starts/stops the bot for you)
# or headless:
python run.py
```

## Control panel (gui.py)

Pure tkinter, no extra deps. Start/Stop Steve with a live log, mode/voice
status, mode buttons, a "Say" box that posts straight to the live display,
and a Chat tab to talk to Steve directly. If Steve is already running (e.g.
started from a terminal), the panel attaches to it instead of starting a
second copy.

It talks to the bot over the local control API on `127.0.0.1:8789`
(`/status`, `/control`, `/say`, `/chat`) — loopback only.

Config lives in `config.json` (hot-reloaded on most paths):
- `discord.botToken` — your bot's token
- `discord.adminUserId` — the Discord user ID of the club officer who gets
  access to `/say`, `/meeting`, `/casual`, and `/reset`
- `ai.provider` — `"ollama"` (default, points at a local/LAN Ollama box) or
  `"claude"` (set `ai.claudeApiKey`)
- `discord.chatChannelIds` — list of channel IDs Steve may talk in;
  empty = all channels he can see

### Live display

Open `http://127.0.0.1:8789/widget` in a browser (or add it as an OBS
Browser Source if you stream meetings). Everything binds to 127.0.0.1 only,
so nothing (including the bot token) is exposed to the network unless you
change `widget.bindHost`.

### Admin UI

Open `http://127.0.0.1:8789/admin` for a web page to edit config.json without
touching the file directly: AI provider/model, system prompts for both
modes, Discord channels and the officer's user ID, the wake-word/always-
respond behavior, voice tuning, and the bot token / Claude key (write-only —
they're never sent back to the browser). It also shows the last 50
conversations (every exchange is logged to `chat_history.jsonl`, tagged with
which channel/DM it came from) and live CPU/RAM/GPU/VRAM usage of the
machine or LXC container Steve is running on (GPU needs `nvidia-smi`
available in the container). Changes save immediately except the bot token,
which needs a restart.

## Discord commands

| Command | Who | Effect |
| --- | --- | --- |
| DM the bot | anyone (if `replyToAllDMs`) or the admin | Private chat with memory |
| `@Steve ...` / "steve ..." | everyone | Reply in channel |
| `/ask <q>` | everyone | Ask Steve a question (spoken too if he's in voice) |
| `/help` | everyone | List what Steve can do |
| `/status` | everyone | Mode, AI provider, voice status |
| `/join` / `/leave` | everyone | Steve joins/leaves your voice channel |
| `/hallucination [phrase]` | everyone | Teach Steve to ignore a noise phrase |
| `/say <text>` | officer | Steve posts it on the live display |
| `/meeting` / `/casual` | officer | Switch system prompt mode |
| `/reset` | officer | Clear conversation memory |

## Files

```
gui.py           # tkinter control panel (start/stop, log, chat, say)
run.py           # launcher — starts everything in one process
discord_bot.py   # Discord bot (DMs, channel chat, slash commands)
voice.py         # voice-call capture + Vosk transcription + TTS replies
ai.py            # Ollama/Claude brain with per-conversation history
widget_server.py # WebSocket + HTTP bridge for the live display + admin UI
widget.html       # optional browser display (speech bubble + browser TTS)
admin.html       # web admin UI (config editing, recent conversations)
hwstats.py       # CPU/RAM/GPU/VRAM stats for the admin UI
state.py         # shared state + config loader
setup.sh         # one-line install (deps, Vosk model, config.json from template)
config.example.json # config template with secrets blanked - safe to commit
config.json      # your real configuration (token, prompts, ports) - gitignored, never commit this
chat_history.jsonl  # append-only log of every exchange (created on first reply)
```

## Ports (all 127.0.0.1 only)

| Port | What |
| --- | --- |
| 8788 | WebSocket → live display (Steve's lines) |
| 8789 | HTTP → serves `/widget` and `/widget-config` |
