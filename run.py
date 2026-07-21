"""AI Club Assistant "Steve" - single-process launcher.

Runs:
  - Discord bot (DM chat with club officers, community chat, slash commands,
    voice channel listen/reply)
  - Widget bridge (WebSocket + HTTP for an optional live display / control panel)

Usage:  python run.py
"""

import asyncio
import logging
import sys

from ai import Brain
from discord_bot import create_bot
from state import State, load_config
from widget_server import Broadcaster, ControlContext, start_http_server


async def main():
    # discord.py internals -> discord.log (voice close codes, reconnects, ...)
    handler = logging.FileHandler("discord.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in ("discord", "discord.ext.voice_recv"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)

    cfg = load_config()
    state = State(cfg)
    broadcaster = Broadcaster()
    brain = Brain(state)

    # ---- Discord bot ----
    token = cfg.get("discord", {}).get("botToken", "").strip()
    if not token:
        print("[discord] no botToken in config.json - Discord disabled")

    bot = create_bot(state, brain, broadcaster)

    # ---- Widget + control servers ----
    wcfg = cfg.get("widget", {})
    ctx = ControlContext(state, brain, broadcaster, asyncio.get_running_loop())

    def voice_status():
        for vc in bot.voice_clients:
            if vc.is_connected():
                return vc.channel.name
        return None

    ctx.voice_status = voice_status
    start_http_server(int(wcfg.get("httpPort", 8789)), ctx)

    async def run_discord():
        """Never let a bad/missing/revoked token take down the whole
        process - the admin UI and widget must stay reachable so it can be
        fixed from the browser instead of requiring an SSH session."""
        if not token:
            print("[discord] no botToken in config.json - Discord disabled")
            print("[discord] set one in the admin UI, then restart to go live")
            return
        try:
            await bot.start(token)
        except Exception as e:
            print(f"[discord] failed to start: {e}")
            print("[discord] check discord.botToken in the admin UI - it may be "
                  "invalid, revoked, or regenerated on the Developer Portal")

    tasks = [broadcaster.serve(int(wcfg.get("wsPort", 8788))), run_discord()]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)
