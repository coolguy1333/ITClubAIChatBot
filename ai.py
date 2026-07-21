"""AI brain: Ollama or Claude, with per-conversation rolling history."""

import asyncio
import json
import re
import threading
import urllib.request
from collections import deque
from datetime import datetime

from state import BASE, load_config

HISTORY_PATH = BASE / "chat_history.jsonl"
_log_lock = threading.Lock()


def log_chat(key, source, user, msg, reply):
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "key": key, "source": source, "user": user, "msg": msg, "reply": reply,
    }
    try:
        with _log_lock:
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[ai] chat log error: {e}")


class Brain:
    """Holds one rolling message history per conversation key
    (e.g. "dm:1234", "guild:5678", "voice:9012")."""

    def __init__(self, state):
        self.state = state
        self.histories = {}
        self._pulled_models = set()   # ollama model names confirmed present this run

    def _history(self, key, maxlen):
        h = self.histories.get(key)
        if h is None or h.maxlen != maxlen:
            h = deque(h or [], maxlen=maxlen)
            self.histories[key] = h
        return h

    def reset(self, scope=None):
        """Clear conversation memory. scope=None wipes everything; otherwise
        clears keys matching the scope prefix (e.g. "dm" -> all "dm:*" chats,
        "voice" -> all voice-call contexts). Returns how many were cleared."""
        if scope is None:
            n = len(self.histories)
            self.histories.clear()
            return n
        keys = [k for k in self.histories
                if k == scope or k.startswith(scope + ":")]
        for k in keys:
            del self.histories[k]
        return len(keys)

    async def ask(self, key, prompt, source="", user=""):
        """Query the AI. Returns the reply text, or None on failure. Every
        attempt is logged (even failures) so chat_history.jsonl is a
        complete record, tagged with the conversation key (e.g. "guild:123",
        "dm:456") so the admin UI can show which channel/DM it came from."""
        reply = await asyncio.to_thread(self._ask_sync, key, prompt)
        log_chat(key, source, user, prompt, reply or "(no reply - AI unreachable)")
        return reply

    def _ask_sync(self, key, prompt):
        cfg = load_config()
        ai_cfg = cfg.get("ai", {})
        system_prompt = self.state.system_prompt(cfg)
        history = self._history(key, int(ai_cfg.get("historyLength", 20)))

        # optional web access (config "web") - augmented prompt goes to the
        # model, but only the original is kept in history to avoid bloat
        full_prompt = prompt
        try:
            from web import augment
            extra = augment(prompt)
            if extra:
                full_prompt = (f"{prompt}\n\n{extra}\n"
                               "[Answer naturally using this - keep it short.]")
        except Exception as e:
            print(f"[web] augment error: {e}")

        messages = list(history) + [{"role": "user", "content": full_prompt}]
        provider = ai_cfg.get("provider", "ollama")
        try:
            if provider == "claude":
                reply = self._claude(ai_cfg, system_prompt, messages)
            else:
                reply = self._ollama(ai_cfg, system_prompt, messages)
        except Exception as e:
            print(f"[ai] {provider} error: {e}")
            return None
        if not reply:
            return None
        # strip "[... says]:" prefixes the model sometimes echoes back
        reply = re.sub(r"^\[.*?\]:\s*", "", reply.strip()).strip()
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply})
        return reply

    def _claude(self, ai_cfg, system_prompt, messages):
        api_key = ai_cfg.get("claudeApiKey", "").strip()
        if not api_key:
            print("[ai] Claude selected but claudeApiKey is empty")
            return None
        body = json.dumps({
            "model": ai_cfg.get("claudeModel", "claude-haiku-4-5-20251001"),
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        timeout = int(ai_cfg.get("ollamaTimeout", 120))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["content"][0]["text"]

    def _ensure_ollama_model(self, base_url, model):
        """Make sure `model` is pulled locally before we try to chat with it -
        switching ai.model (e.g. via the admin UI) just works without a
        manual `ollama pull` first. Checked once per model per run."""
        if model in self._pulled_models:
            return
        try:
            req = urllib.request.Request(base_url + "/api/tags")
            with urllib.request.urlopen(req, timeout=10) as resp:
                installed = {m.get("name") for m in json.loads(resp.read()).get("models", [])}
            if model in installed:
                self._pulled_models.add(model)
                return
        except Exception as e:
            print(f"[ai] couldn't list installed ollama models: {e}")
            return   # don't block the chat on a failed check

        print(f"[ai] model '{model}' isn't pulled yet - downloading via Ollama "
              "(this can take a while the first time)...")
        try:
            body = json.dumps({"name": model, "stream": True}).encode()
            req = urllib.request.Request(base_url + "/api/pull", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            last_pct = -1
            with urllib.request.urlopen(req, timeout=1800) as resp:
                for line in resp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    total, done = evt.get("total"), evt.get("completed")
                    if total and done:
                        pct = int(done / total * 100)
                        if pct >= last_pct + 10:
                            print(f"[ai] pulling {model}: {pct}%")
                            last_pct = pct
                    if evt.get("error"):
                        print(f"[ai] pull error for '{model}': {evt['error']}")
                        return
            print(f"[ai] model '{model}' pulled and ready")
            self._pulled_models.add(model)
        except Exception as e:
            print(f"[ai] failed to pull model '{model}': {e}")

    def _ollama(self, ai_cfg, system_prompt, messages):
        base_url = ai_cfg.get("ollamaUrl", "http://localhost:11434").rstrip("/")
        model = ai_cfg.get("model", "llama3.2:latest")
        self._ensure_ollama_model(base_url, model)
        url = base_url + "/api/chat"
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "stream": False,
            "keep_alive": "60m",                  # keep the model loaded between replies
            "options": {"num_predict": 200},      # replies are short; cap generation
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        timeout = int(ai_cfg.get("ollamaTimeout", 120))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())["message"]["content"]
