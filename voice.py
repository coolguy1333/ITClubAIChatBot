"""Discord voice: join a channel, transcribe each user with Vosk (local, offline),
reply through the AI, and speak the reply back via pyttsx3 + ffmpeg.

Ported from Steve-pt2/discord_bot.py, but self-contained - no voice_server,
transcription runs in-process.
"""

import asyncio
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time

# Wake word: only reply to utterances that actually address Steve -
# "steve", "hey steve", "ok steve", etc. Also catches the most common
# Vosk mishearings of the name.
WAKE_RE = re.compile(r"\b(hey |ok |okay )?(steve|steven|stevie)\b", re.IGNORECASE)

import numpy as np
import discord
import discord.opus as _opus

from state import BASE, CONFIG_PATH, load_config

# Swallow corrupted Opus packets instead of crashing the receive thread
_orig_decode = _opus.Decoder.decode
_decode_stats = {"ok": 0, "fail": 0}

def _safe_decode(self, data, *, fec=False):
    try:
        pcm = _orig_decode(self, data, fec=fec)
        _decode_stats["ok"] += 1
        return pcm
    except _opus.OpusError as e:
        _decode_stats["fail"] += 1
        n = _decode_stats["fail"]
        if n <= 10 or n % 100 == 0:
            head = data[:8].hex() if data else None
            print(f"[voice-dbg] opus decode FAIL #{n} (ok={_decode_stats['ok']}): "
                  f"{e} | len={len(data) if data else None} head={head}")
        return b"\x00" * (960 * 2 * 2)

_opus.Decoder.decode = _safe_decode

from discord.ext import voice_recv  # noqa: E402  (import after opus patch)

# Discord voice is end-to-end encrypted (DAVE) and the server refuses
# non-E2EE clients (close code 4017), but voice_recv predates E2EE: it hands
# the opus decoder frames that are still DAVE-encrypted. discord.py already
# maintains a davey session for *sending* - reuse it to decrypt what we
# receive, right before opus decode.
try:
    import davey as _davey
    from discord.ext.voice_recv import opus as _vr_opus

    _orig_decode_packet = _vr_opus.PacketDecoder._decode_packet
    _dave_dbg = {"ok": 0, "fail": 0}

    def _dave_decode_packet(self, packet):
        try:
            data = getattr(packet, "decrypted_data", None)
            if packet and data and not packet.is_silence():
                vc = self.sink.voice_client
                st = vc._connection
                sess = getattr(st, "dave_session", None)
                if sess is not None and getattr(st, "dave_protocol_version", 0) > 0:
                    uid = vc._get_id_from_ssrc(self.ssrc)
                    if uid:
                        packet.decrypted_data = bytes(
                            sess.decrypt(uid, _davey.MediaType.audio, bytes(data)))
                        _dave_dbg["ok"] += 1
        except Exception as e:
            _dave_dbg["fail"] += 1
            n = _dave_dbg["fail"]
            if n <= 5 or n % 200 == 0:
                print(f"[voice-dbg] DAVE decrypt fail #{n} (ok={_dave_dbg['ok']}): {e}")
        return _orig_decode_packet(self, packet)

    _vr_opus.PacketDecoder._decode_packet = _dave_decode_packet
    print("[voice] DAVE E2EE receive decryption enabled")
except Exception as _e:
    print(f"[voice] DAVE receive patch failed: {_e}")

# Phrases STT engines produce on silence/noise - never worth replying to
_BASE_HALLUCINATIONS = {
    "subtitles by the amara.org community", "subtitles by amara.org",
    "transcribed by the amara.org community", "captions by the amara.org community",
    "i'm not sure", "i don't know", "i'm going to go", "i'm going to take a look",
    "for more information, visit www.fema.org", "www.movieweb.com",
    "i love you", "the", "a", "uh", "um", "huh", "hmm", "but",
}


class UserAudioBuffer:
    """Streams one user's 48kHz stereo PCM into a per-utterance Vosk
    recognizer WHILE they talk, so the transcript is ready almost the moment
    they stop. A silence timer closes the utterance (Discord stops sending
    packets when a user goes quiet)."""

    SOURCE_RATE = 48000
    TARGET_RATE = 16000
    CHANNELS = 2
    FEED_SAMPLES = 24000     # feed vosk every 0.5s of audio
    MAX_UTTERANCE = 48000 * 60

    def __init__(self, user, manager, vc):
        self.user = user
        self.manager = manager
        self.vc = vc
        self.speaking = False
        self._timer = None
        self._chunk = []         # 48k mono frames awaiting the next feed
        self._chunk_len = 0
        self._utt_len = 0
        self._q = queue.Queue()
        threading.Thread(target=self._feed_loop, daemon=True).start()

    @property
    def name(self):
        return getattr(self.user, "display_name", None) or str(self.user)

    def push(self, pcm_bytes):
        if self.manager.speaking:      # drop audio while Steve is talking
            if self.speaking:
                self._abort()
            return
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
        if len(audio) % self.CHANNELS == 0:
            audio = audio.reshape(-1, self.CHANNELS).mean(axis=1)
        energy = np.sqrt(np.mean(audio ** 2)) if len(audio) else 0
        loud = energy > self.manager.energy_threshold

        if self.speaking:
            # mid-utterance: keep EVERY frame (quiet ones too) so the audio
            # stays continuous - dropping them garbles transcription
            self._append(audio)
            if loud:                    # only loud frames push the deadline back
                self._reset_timer()
        elif loud:
            print(f"[voice] speech from {self.name} (energy={energy:.0f})")
            self.speaking = True
            self._append(audio)
            self._reset_timer()

    def _append(self, audio):
        self._chunk.append(audio)
        self._chunk_len += len(audio)
        self._utt_len += len(audio)
        if self._chunk_len >= self.FEED_SAMPLES:
            self._q.put(("audio", np.concatenate(self._chunk)))
            self._chunk, self._chunk_len = [], 0
        if self._utt_len > self.MAX_UTTERANCE:
            self._on_silence()          # cap - flush runaway utterances

    def _reset_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.manager.pause_threshold, self._on_silence)
        self._timer.daemon = True
        self._timer.start()

    def _abort(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._chunk, self._chunk_len, self._utt_len = [], 0, 0
        self.speaking = False
        self._q.put(("abort", None))

    def _on_silence(self):
        self._timer = None
        self.speaking = False
        if self._chunk:
            self._q.put(("audio", np.concatenate(self._chunk)))
            self._chunk, self._chunk_len = [], 0
        self._utt_len = 0
        self._q.put(("final", time.monotonic()))

    def _prep(self, audio48):
        """48k mono float -> 16k int16 bytes for vosk."""
        try:
            from scipy.signal import resample_poly
            audio = resample_poly(audio48, 1, 3)
        except ImportError:
            n = int(len(audio48) * self.TARGET_RATE / self.SOURCE_RATE)
            audio = np.interp(np.linspace(0, len(audio48) - 1, n),
                              np.arange(len(audio48)), audio48)
        audio = audio - np.mean(audio)
        return np.clip(audio, -32768, 32767).astype(np.int16)

    def _feed_loop(self):
        rec, fed, debug = None, 0, []
        while True:
            kind, data = self._q.get()
            try:
                if kind == "audio":
                    x = self._prep(data)
                    if rec is None:
                        rec = self.manager.new_recognizer()
                    rec.AcceptWaveform(x.tobytes())
                    fed += len(x)
                    debug.append(x)
                elif kind == "abort":
                    rec, fed, debug = None, 0, []
                elif kind == "final":
                    if rec is not None and fed >= self.TARGET_RATE // 2:
                        t0 = time.monotonic()
                        text = json.loads(rec.FinalResult()).get("text", "").strip()
                        self.manager.save_debug(np.concatenate(debug))
                        print(f"[timing] finalize {time.monotonic()-t0:.2f}s "
                              f"(utterance {fed/self.TARGET_RATE:.1f}s)")
                        self.manager.on_transcript(self.name, text, self.vc, data)
                    rec, fed, debug = None, 0, []
            except Exception as e:
                print(f"[voice] feed error ({self.name}): {e}")
                rec, fed, debug = None, 0, []


class TranscriptionSink(voice_recv.AudioSink):
    def __init__(self, manager, vc, bot_user_id):
        super().__init__()
        self.manager = manager
        self.vc = vc
        self.bot_user_id = bot_user_id
        self.buffers = {}
        self._last_seq = {}

    def wants_opus(self):
        return False

    def write(self, user, data):
        if user is None or user.id == self.bot_user_id:
            return
        try:
            pkt = data.packet
            kind = type(pkt).__name__
            seq = getattr(pkt, "sequence", -1)
            last = self._last_seq.get(user.id)
            if kind != "RTPPacket" or (last is not None and seq != last + 1):
                print(f"[voice-dbg] {user.display_name}: {kind} seq {last}->{seq}"
                      + (" SILENCE" if pkt.is_silence() else ""))
            self._last_seq[user.id] = seq

            buf = self.buffers.get(user.id)
            if buf is None:
                print(f"[voice] now listening to: {user.display_name}")
                buf = self.buffers[user.id] = UserAudioBuffer(user, self.manager, self.vc)
            buf.push(data.pcm)
        except Exception as e:
            print(f"[voice] sink error: {e}")

    def cleanup(self):
        self.buffers.clear()


class VoiceManager:
    """One per bot: vosk model, TTS queue, hallucination list, join/leave."""

    def __init__(self, state, brain, broadcaster):
        self.state = state
        self.brain = brain
        self.broadcaster = broadcaster
        self.loop = None                 # bot's event loop, set on join
        self.speaking = False
        self.reconnect_flags = {}        # guild_id -> bool
        self.last_transcription = {}     # display name -> last raw text
        self._model = None
        self._model_lock = threading.Lock()
        self._tts_queue = queue.Queue()
        self.hallucinations = set(_BASE_HALLUCINATIONS)
        for p in load_config().get("discord", {}).get("customHallucinations", []):
            self.hallucinations.add(p.lower().strip())
        threading.Thread(target=self._tts_worker, daemon=True).start()

    # ---- config shortcuts ----
    @property
    def _vcfg(self):
        return load_config().get("voice", {})

    @property
    def energy_threshold(self):
        return float(self._vcfg.get("energyThreshold", 200))

    @property
    def pause_threshold(self):
        return float(self._vcfg.get("pauseThreshold", 1.5))

    # ---- join / leave ----
    async def join(self, channel, bot):
        self.loop = asyncio.get_running_loop()
        for vc in list(bot.voice_clients):
            await vc.disconnect(force=True)
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        vc.listen(TranscriptionSink(self, vc, bot.user.id))
        self.reconnect_flags[channel.guild.id] = True
        bot.loop.create_task(self._reconnect_loop(bot, channel))
        print(f"[voice] joined {channel.name} ({channel.id})")
        return vc

    async def leave(self, guild):
        self.reconnect_flags[guild.id] = False
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)

    async def _reconnect_loop(self, bot, channel):
        await asyncio.sleep(5)
        while self.reconnect_flags.get(channel.guild.id, False):
            vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
            if vc is None or not vc.is_connected():
                print(f"[voice] dropped - reconnecting to {channel.name}...")
                try:
                    await self.join(channel, bot)
                except Exception as e:
                    print(f"[voice] reconnect failed: {e}")
                    await asyncio.sleep(10)
                    continue
            await asyncio.sleep(5)

    # ---- transcription ----
    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                from vosk import Model, SetLogLevel
                SetLogLevel(-1)
                path = self._vcfg.get("voskModelPath", "")
                p = (BASE / path).resolve() if path and not os.path.isabs(path) else path
                if not p or not os.path.isdir(str(p)):
                    # fallback: model folder shipped alongside the code (VM installs)
                    local = BASE / "vosk-model-en-us-0.22-lgraph"
                    if local.is_dir():
                        p = local
                print(f"[voice] loading vosk model: {p}")
                self._model = Model(str(p))
                print("[voice] vosk model ready")
            return self._model

    def new_recognizer(self):
        from vosk import KaldiRecognizer
        return KaldiRecognizer(self._get_model(), 16000)

    def is_hallucination(self, text):
        return text.lower().rstrip(".,!? ") in self.hallucinations

    def add_hallucination(self, phrase):
        phrase = phrase.lower().strip(".,!? ")
        self.hallucinations.add(phrase)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            existing = cfg.setdefault("discord", {}).setdefault("customHallucinations", [])
            if phrase not in existing:
                existing.append(phrase)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"[voice] failed to save hallucination: {e}")

    def save_debug(self, audio_int16):
        """Overwrite debug_last.wav with the most recent utterance so it can
        be listened to when Steve mishears something."""
        try:
            import wave
            with wave.open(str(BASE / "debug_last.wav"), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_int16.tobytes())
        except Exception:
            pass

    def on_transcript(self, name, text, vc, started):
        """Called from a buffer's feed thread when an utterance is final."""
        print(f"[voice] {name}: '{text or '(empty)'}'")
        if not text or len(text) < 2:
            return
        self.last_transcription[name] = text
        if self.is_hallucination(text):
            print(f"[voice] hallucination from {name} - ignored")
            return
        if load_config().get("voice", {}).get("requireWakeWord", True):
            m = WAKE_RE.search(text)
            if not m:
                return   # nobody said "steve" / "hey steve" - stay quiet
            # strip the wake phrase so the AI just sees the actual question
            text = (text[:m.start()] + text[m.end():]).strip(" ,.!?") or text
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._reply(name, text, vc, started), self.loop)

    async def _reply(self, name, text, vc, started):
        prompt = f'[{name} says in the voice call]: {text}'
        t0 = time.monotonic()
        reply = await self.brain.ask(f"voice:{vc.guild.id}", prompt, "discord_voice", name)
        print(f"[timing] AI {time.monotonic()-t0:.1f}s | "
              f"total since silence {time.monotonic()-started:.1f}s")
        if not reply:
            return
        print(f"[voice] Steve: {reply}")
        await self.broadcaster.send({"type": "steve", "source": "discord",
                                     "user": name, "question": text, "text": reply})
        self.speak(vc, reply)

    # ---- TTS out ----
    def speak(self, vc, text):
        """Queue text to be spoken into the voice channel."""
        if vc and vc.is_connected():
            self._tts_queue.put((text, vc))

    def _tts_worker(self):
        while True:
            text, vc = self._tts_queue.get()
            try:
                self._render_and_play(text, vc)
            except Exception as e:
                print(f"[voice] TTS error: {e}")
            finally:
                if self._tts_queue.empty():
                    self.speaking = False

    def _render_and_play(self, text, vc):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            print("[voice] ffmpeg not found - cannot speak")
            return
        import pyttsx3
        t0 = time.monotonic()
        engine = pyttsx3.init()
        rate = float(self._vcfg.get("ttsRate", 1.0))
        engine.setProperty("rate", int(175 * rate))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        engine.save_to_file(text, tmp)
        engine.runAndWait()
        print(f"[timing] TTS render {time.monotonic()-t0:.1f}s")
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            print("[voice] TTS produced no audio (no SAPI voice?)")
            return
        if not vc.is_connected():
            os.unlink(tmp)
            return
        self.speaking = True
        done = threading.Event()
        vc.play(discord.FFmpegPCMAudio(tmp, executable=ffmpeg),
                after=lambda err: done.set())
        done.wait(timeout=120)
        try:
            os.unlink(tmp)
        except OSError:
            pass
