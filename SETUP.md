# Steve — Setup Instructions

Step-by-step guide to get the IT Club AI assistant running from scratch.

---

## 1. Requirements

| Thing | Why | Check with |
| --- | --- | --- |
| Python 3.10+ | runs everything | `python --version` |
| ffmpeg on PATH | speaking in Discord voice calls | `ffmpeg -version` |
| Ollama (or a Claude API key) | the AI brain | `curl http://localhost:11434/api/tags` |
| A Windows SAPI voice | text-to-speech (built into Windows) | — |

ffmpeg install if missing: `winget install Gyan.FFmpeg` (then reopen the terminal).

## 2. Install Python packages

From the project folder:

```bash
python -m pip install -r requirements.txt
```

> **Heads-up:** plain `pip install` may target a *different* Python than
> `python` runs. Always use `python -m pip install ...` so packages land in
> the right one.

## 3. The Vosk speech model

Voice recognition uses a local Vosk model, shipped in this folder
(`vosk-model-en-us-0.22-lgraph`). `config.json` already points at it:

```json
"voice": { "voskModelPath": "./vosk-model-en-us-0.22-lgraph" }
```

If you move it, download the model from
https://alphacephei.com/vosk/models, unzip it anywhere, and update
`voskModelPath` (absolute paths are fine).

## 4. Configure `config.json`

You can hand-edit this file, or start Steve once and use the web admin UI
at `http://127.0.0.1:8789/admin` for everything except the bot token/API
key setup below (do that first).

### Discord
| Field | What |
| --- | --- |
| `discord.botToken` | bot account token (from the [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot). Needs **Message Content** + **Voice States** intents enabled there. |
| `discord.adminUserId` | the officer's Discord user ID — right-click yourself → Copy User ID (needs Developer Mode on). Gets access to `/say`, `/meeting`, `/casual`, `/reset`. |
| `discord.chatChannelIds` | `[]` = Steve may talk in any channel; or list specific channel IDs |
| `discord.alwaysRespondChannelId` | one channel where Steve answers *every* message — no @mention, no "steve", no `/ask` needed. Good for a dedicated help channel. |

### AI brain
| Field | What |
| --- | --- |
| `ai.provider` | `"ollama"` (local, default) or `"claude"` |
| `ai.model` | Ollama model, e.g. `llama3.2:latest` (`ollama pull llama3.2` first) |
| `ai.ollamaUrl` | `http://localhost:11434` on this PC |
| `ai.claudeApiKey` | only if provider is `claude` — from console.anthropic.com |
| `ai.systemPrompt` | base personality — already tuned for hardware/software/code/networking help |

### Voice / wake word
| Field | What |
| --- | --- |
| `voice.requireWakeWord` | `true` (default) = Steve only replies in voice calls when someone says "steve" or "hey steve"; otherwise he just listens |
| `voice.energyThreshold` | mic sensitivity — raise for noisy rooms |
| `voice.pauseThreshold` | seconds of silence that end an utterance |

### Home channels (optional)
| Field | What |
| --- | --- |
| `discord.homeTextChannelId` | Steve's home text channel (right-click channel → Copy Channel ID) |
| `discord.homeVoiceChannelId` | his home voice channel |
| `discord.onlyHomeChannels` | `true` = he ONLY talks in his home text channel and refuses `/join` anywhere but his home voice channel |
| `discord.autoJoinVoice` | `true` = he joins his home voice channel automatically on startup |

DMs with the admin always work regardless of the lockdown.

### Web access
| Field | What |
| --- | --- |
| `web.enabled` | `true`/`false` — master switch for Steve's internet access |
| `web.maxChars` | how much page/search text gets fed to the AI (default 2000) |

When on: any URL someone sends him gets fetched and summarized, and
"steve look up X" / "search for X" / "google X" triggers a DuckDuckGo search
he answers from. Private/LAN addresses are always refused, so chat can't
make him poke around your network.

### Personalities
`profiles.casual` / `profiles.meeting` hold the system prompts. Toggle
manually with `/casual`, `/meeting` in Discord or the mode buttons in the
GUI / admin UI — there's no automatic switching since there's no stream
status to key off anymore.

## 5. Run him

**With the control panel (recommended):**
```bash
python gui.py
```
Hit **▶ Start Steve**. The log streams in the window; Stop / mode / "say" /
chat are all right there.

**Headless:**
```bash
python run.py
```

Startup is healthy when the log shows:
```
[voice] DAVE E2EE receive decryption enabled
[widget] overlay at http://127.0.0.1:8789/widget (OBS Browser Source)
[discord] logged in as Steve#1605 in 1 server(s)
```

## 6. Admin UI

Open `http://127.0.0.1:8789/admin` in a browser. Edit AI settings, prompts,
channels, wake-word behavior, and voice tuning; set/rotate the bot token or
Claude key (write-only, never echoed back); and see the last 50
conversations. Most changes are live immediately — a new bot token needs a
restart.

## 7. Using Steve

| Where | How |
| --- | --- |
| DM him on Discord | just message the bot — private chat with memory |
| Server channels | @mention him or say "steve" in a message |
| The always-respond channel (if set) | just talk — no name or command needed |
| Voice calls | `/join` while you're in a voice channel, say "steve" or "hey steve" before your question, `/leave` when done |

Other commands: `/help` (lists everything), `/status`, `/reset [scope]`
(wipe his memory — everything, or just DMs / Discord channels / voice
calls, e.g. when chat prompt-injects him into PotatoGPT), `/hallucination
[phrase]` (teach him a phrase is mic noise, e.g. breathing that transcribes
as words), `/meeting` `/casual` (officer only).

Steve also chimes into channel chatter on his own every ~6 messages
(tune `discord.autoReactEveryMessages`; `discord.autoReactEnabled: false`
turns it off).

## 8. Running on a VM

The zip is fully self-contained — code + Vosk model + installer.

```bash
wget http://<your-pc-ip>:8080/steve.zip
sudo apt install -y unzip && unzip steve.zip -d steve && cd steve
bash install.sh
venv/bin/python run.py
```

The installer sets `widget.bindHost` to `0.0.0.0` and points the Vosk model
at the bundled copy. Two things to check in the VM's `config.json`:
- `ai.ollamaUrl` → point it at whichever machine runs Ollama (e.g.
  `http://192.168.1.10:11434`). On that machine, Ollama must listen on the
  LAN: set `OLLAMA_HOST=0.0.0.0` and restart Ollama.
- The admin UI and live display become `http://<VM-IP>:8789/admin` and
  `.../widget`; the control panel on your PC connects with
  `python gui.py <VM-IP>`.

TTS on Linux uses espeak-ng (robotic but works). The install script prints a
ready-made systemd unit so Steve starts on VM boot. The bot binds to the
LAN on a VM — fine at home, don't port-forward it to the internet.

## 9. Troubleshooting

| Symptom | Fix |
| --- | --- |
| No AI replies ("brain unreachable") | Ollama isn't running / wrong `ollamaUrl` — `curl http://localhost:11434/api/tags` |
| Mishears you in voice | listen to `debug_last.wav` (exactly what he heard); raise `voice.energyThreshold` for noisy mics, or `/hallucination` the junk phrase |
| Doesn't respond in voice at all | you need to say "steve" or "hey steve" — check `voice.requireWakeWord` in the admin UI |
| Slow replies | check the `[timing]` lines in the log — they name the slow stage (finalize / AI / TTS) |
| Kicked from voice / garbled hearing | don't remove the DAVE patches in `voice.py` — Discord voice is E2EE and requires them |
| No sound from Steve in voice | ffmpeg missing from PATH, or no Windows SAPI voice installed |
| Live display shows no dot | Steve isn't running, or the browser loaded the page before him — refresh |
| Ports 8788/8789 busy | another Steve instance is running — close it (or the GUI is attached to it, which is fine) |

Voice tuning knobs (`config.json` → `voice`, or the admin UI): `energyThreshold`
(default 200, higher = ignores quieter sounds), `pauseThreshold` (silence
seconds that end an utterance, default 1.5), `ttsRate` (speaking speed).
