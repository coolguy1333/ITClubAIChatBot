"""Steve control panel - tkinter (Python builtin), no extra dependencies.

Start it with:  python gui.py

- Start/Stop Steve (runs run.py as a subprocess, live log below)
- If Steve is already running elsewhere, the panel just attaches to it
- Mode / voice indicators
- Mode buttons (casual / meeting)
- "Say" - push a line straight to the widget display/TTS
- Chat tab - talk to Steve directly from the panel
"""

import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import scrolledtext, ttk

BASE = Path(__file__).parent
# point the panel at a remote Steve (e.g. on a VM):  python gui.py 192.168.1.50
_host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
API = f"http://{_host}:8789"

BG = "#12121a"
FG = "#f2f2f7"
ACCENT = "#7c5cff"
DIM = "#8888a0"


def api_get(path, timeout=3):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path, payload, timeout=200):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class SteveGUI:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.log_q = queue.Queue()
        root.title("Steve — Control Panel")
        root.geometry("780x560")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground="#1c1c28")
        style.configure("TNotebook.Tab", background="#1c1c28", foreground=FG, padding=(12, 5))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)])
        style.configure("TButton", background="#2a2a3a", foreground=FG, padding=6)
        style.map("TButton", background=[("active", ACCENT)])

        # ---- top bar: process + status ----
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.start_btn = ttk.Button(top, text="▶ Start Steve", command=self.start_bot)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(top, text="■ Stop", command=self.stop_bot)
        self.stop_btn.pack(side="left", padx=(6, 12))
        self.status_lbl = tk.Label(top, text="checking...", bg=BG, fg=DIM,
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="left")

        # ---- personality row ----
        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=10, pady=4)
        tk.Label(row, text="Mode:", bg=BG, fg=FG).pack(side="left")
        for mode in ("casual", "meeting"):
            ttk.Button(row, text=mode, width=8,
                       command=lambda m=mode: self.set_mode(m)).pack(side="left", padx=3)
        tk.Label(row, text="   Say on stream:", bg=BG, fg=FG).pack(side="left")
        self.say_entry = tk.Entry(row, bg="#1c1c28", fg=FG, insertbackground=FG, width=30)
        self.say_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.say_entry.bind("<Return>", lambda e: self.do_say())
        ttk.Button(row, text="Say", command=self.do_say).pack(side="left")

        # ---- tabs ----
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        log_tab = tk.Frame(nb, bg=BG)
        nb.add(log_tab, text="  Log  ")
        self.log = scrolledtext.ScrolledText(log_tab, bg="#0c0c12", fg="#c8c8dc",
                                             insertbackground=FG, wrap="word",
                                             font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)

        chat_tab = tk.Frame(nb, bg=BG)
        nb.add(chat_tab, text="  Chat with Steve  ")
        self.chat = scrolledtext.ScrolledText(chat_tab, bg="#0c0c12", fg=FG,
                                              insertbackground=FG, wrap="word",
                                              font=("Segoe UI", 10), state="disabled")
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_config("me", foreground="#8ec5ff")
        self.chat.tag_config("steve", foreground="#b9a8ff")
        self.chat.tag_config("dim", foreground=DIM)
        chat_row = tk.Frame(chat_tab, bg=BG)
        chat_row.pack(fill="x", pady=(4, 0))
        self.chat_entry = tk.Entry(chat_row, bg="#1c1c28", fg=FG, insertbackground=FG)
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.chat_entry.bind("<Return>", lambda e: self.do_chat())
        ttk.Button(chat_row, text="Send", command=self.do_chat).pack(side="left")

        self.root.after(200, self.drain_log)
        self.root.after(500, self.poll_status)

    # ---- bot process ----
    def start_bot(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            if api_get("/status", timeout=1):
                self.log_line("[gui] Steve is already running outside this panel — attached.\n")
                return
        except Exception:
            pass
        self.log_line("[gui] starting Steve...\n")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run.py"], cwd=BASE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump_output, daemon=True).start()

    def stop_bot(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log_line("[gui] Steve stopped.\n")
        self.proc = None

    def _pump_output(self):
        proc = self.proc
        for line in proc.stdout:
            self.log_q.put(line)
        self.log_q.put("[gui] Steve process exited.\n")

    def log_line(self, line):
        self.log_q.put(line)

    def drain_log(self):
        try:
            lines = []
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log.configure(state="normal")
            self.log.insert("end", "".join(lines))
            self.log.see("end")
            # keep the log from growing forever
            if float(self.log.index("end-1c").split(".")[0]) > 3000:
                self.log.delete("1.0", "1000.0")
            self.log.configure(state="disabled")
        self.root.after(200, self.drain_log)

    # ---- status polling ----
    def poll_status(self):
        def work():
            try:
                s = api_get("/status")
                voice = f" | voice: {s['voice']}" if s.get("voice") else ""
                text = f"Mode: {s['mode']} | AI: {s['provider']} ({s['model']}){voice}"
                color = "#40c463"
            except Exception:
                running = self.proc and self.proc.poll() is None
                text = "starting..." if running else "Steve is not running — hit Start"
                color = DIM
            self.root.after(0, lambda: self.status_lbl.config(text=text, fg=color))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(3000, self.poll_status)

    # ---- controls ----
    def set_mode(self, mode):
        def work():
            try:
                api_post("/control", {"mode": mode}, timeout=5)
                self.log_line(f"[gui] personality mode -> {mode}\n")
            except Exception as e:
                self.log_line(f"[gui] failed ({e}) — is Steve running?\n")
        threading.Thread(target=work, daemon=True).start()

    def do_say(self):
        text = self.say_entry.get().strip()
        if not text:
            return
        self.say_entry.delete(0, "end")
        def work():
            try:
                api_post("/say", {"text": text}, timeout=5)
                self.log_line(f"[gui] on stream: {text}\n")
            except Exception as e:
                self.log_line(f"[gui] say failed ({e}) — is Steve running?\n")
        threading.Thread(target=work, daemon=True).start()

    def do_chat(self):
        text = self.chat_entry.get().strip()
        if not text:
            return
        self.chat_entry.delete(0, "end")
        self.chat_append("You: ", "me", text + "\n")
        def work():
            try:
                reply = api_post("/chat", {"text": text}).get("reply")
                if reply:
                    self.root.after(0, lambda: self.chat_append("Steve: ", "steve", reply + "\n\n"))
                else:
                    self.root.after(0, lambda: self.chat_append(
                        "", "dim", "(no reply — AI backend unreachable?)\n\n"))
            except Exception as e:
                self.root.after(0, lambda: self.chat_append(
                    "", "dim", f"(chat failed: {e} — is Steve running?)\n\n"))
        threading.Thread(target=work, daemon=True).start()

    def chat_append(self, prefix, tag, text):
        self.chat.configure(state="normal")
        if prefix:
            self.chat.insert("end", prefix, tag)
        self.chat.insert("end", text)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    SteveGUI(root)
    root.mainloop()
