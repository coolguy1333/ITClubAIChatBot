"""Discord side of the companion.

- DM the bot to talk with Steve privately (rolling memory, works for anyone).
- In server channels it answers @mentions / its name, always attributing the
  reply to whoever actually sent the message.
- /join pulls Steve into a voice channel: he transcribes everyone with
  Vosk (local) and talks back with TTS.
- Slash commands for everyone (/ask, /status, /help, /join, /leave,
  /hallucination) and officer-only controls (/say, /meeting, /casual, /reset).
"""

import datetime
import io
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from state import load_config
from voice import VoiceManager

NAME_RE = re.compile(r"^\s*steve\b", re.IGNORECASE)   # must START with "steve", not just mention it
DISCORD_SAFE_LEN = 1900   # leave headroom under Discord's 2000-char message cap
CODE_BLOCK_RE = re.compile(r"```(\w*)\n?(.*?)```", re.DOTALL)
LANG_EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs", "cs": "cs",
    "html": "html", "css": "css", "json": "json", "bash": "sh", "sh": "sh", "shell": "sh",
    "sql": "sql", "go": "go", "rust": "rs", "rs": "rs", "php": "php", "yaml": "yaml", "yml": "yaml",
    "powershell": "ps1", "ps1": "ps1",
}


def _ext_for(lang):
    return LANG_EXT.get((lang or "").strip().lower(), "txt")


async def send_maybe_file(send_func, text, **kwargs):
    """Send a reply, preferring files over a wall of text:
    - any ```code``` blocks get sent as file attachments (easy to copy/run,
      syntax highlighting in most editors), named with a sensible extension
    - otherwise, if the whole thing is too long for one Discord message, it
      goes as a single .txt attachment instead of getting truncated
    Needs the 'Attach Files' permission; falls back to plain/truncated text
    if that's missing or something else goes wrong."""
    blocks = CODE_BLOCK_RE.findall(text)
    if blocks:
        try:
            remaining = CODE_BLOCK_RE.sub("", text).strip() or "Here's the code:"
            if len(remaining) > DISCORD_SAFE_LEN:
                remaining = remaining[:DISCORD_SAFE_LEN] + " …(truncated)"
            files = []
            for i, (lang, code) in enumerate(blocks):
                code = code.strip("\n")
                name = f"steve-code-{i + 1}.{_ext_for(lang)}" if len(blocks) > 1 \
                    else f"steve-code.{_ext_for(lang)}"
                files.append(discord.File(io.BytesIO(code.encode("utf-8")), filename=name))
            await send_func(remaining, files=files, **kwargs)
            return
        except Exception as e:
            print(f"[discord] couldn't send code as file(s) ({e}) - sending inline instead")

    if len(text) <= DISCORD_SAFE_LEN:
        await send_func(text, **kwargs)
        return
    try:
        buf = io.BytesIO(text.encode("utf-8"))
        await send_func("(reply attached below - too long for one message)",
                        file=discord.File(buf, filename="steve-reply.txt"), **kwargs)
    except Exception as e:
        print(f"[discord] couldn't attach long reply as a file ({e}) - sending truncated")
        await send_func(text[:DISCORD_SAFE_LEN] + " …(truncated)", **kwargs)


def create_bot(state, brain, broadcaster):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    voice_mgr = VoiceManager(state, brain, broadcaster)
    bot.voice_mgr = voice_mgr

    last_reply = {}   # channel_id -> monotonic time of last bot reply

    def is_admin(user_id):
        return str(user_id) == load_config().get("discord", {}).get("adminUserId", "").strip()

    def channel_allowed(dcfg, channel_id):
        # home lockdown: when enabled, Steve only talks in his home text channel
        if dcfg.get("onlyHomeChannels") and dcfg.get("homeTextChannelId"):
            return str(channel_id) == str(dcfg["homeTextChannelId"])
        ids = [str(i) for i in dcfg.get("chatChannelIds", [])]
        return not ids or str(channel_id) in ids

    async def show_on_stream(source, user, question, reply):
        await broadcaster.send({
            "type": "steve", "source": source, "user": user,
            "question": question, "text": reply,
        })

    async def deliver_reply(message, dcfg, cid, reply, name_hint, mention_reply):
        """Send a reply either inline, or into a thread if this channel is
        configured for it (discord.threadReplyChannelIds - needs the
        'Create Public Threads' + 'Send Messages in Threads' permissions)."""
        thread_ids = [str(i) for i in dcfg.get("threadReplyChannelIds", [])]
        if str(cid) in thread_ids and not isinstance(message.channel, discord.Thread):
            try:
                name = (name_hint or reply)[:90].strip() or "steve"
                thread = await message.create_thread(name=name, auto_archive_duration=1440)
                await send_maybe_file(thread.send, reply)
                print(f"[discord] replied in thread '{thread.name}' ({thread.id}) in #{message.channel.name}")
                return
            except discord.Forbidden:
                print(f"[discord] thread reply failed in #{message.channel.name}: missing permission - "
                      "check the bot has 'Create Public Threads' + 'Send Messages in Threads'. "
                      "Falling back to an inline reply.")
            except Exception as e:
                print(f"[discord] thread reply failed, replying inline instead: {e}")
        if mention_reply:
            await send_maybe_file(message.reply, reply, mention_author=False)
        else:
            await send_maybe_file(message.channel.send, reply)

    @bot.event
    async def on_ready():
        print(f"[discord] logged in as {bot.user} in {len(bot.guilds)} server(s)")
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"[discord] slash commands synced to: {guild.name}")
        # auto-join Steve's home voice channel if configured
        dcfg = load_config().get("discord", {})
        home_vc = dcfg.get("homeVoiceChannelId", "")
        if dcfg.get("autoJoinVoice") and home_vc:
            try:
                channel = bot.get_channel(int(home_vc))
                if isinstance(channel, discord.VoiceChannel):
                    await voice_mgr.join(channel, bot)
                    print(f"[voice] auto-joined home channel: {channel.name}")
                else:
                    print(f"[voice] homeVoiceChannelId {home_vc} is not a voice channel")
            except Exception as e:
                print(f"[voice] auto-join failed: {e}")

    @bot.event
    async def on_voice_state_update(member, before, after):
        # leave automatically once everyone else has left the call - no
        # point sitting in an empty voice channel listening to nobody
        guild = member.guild
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        humans = [m for m in vc.channel.members if not m.bot]
        if not humans:
            print(f"[voice] {vc.channel.name} is empty - leaving")
            await voice_mgr.leave(guild)

    @bot.event
    async def on_message(message):
        if message.author == bot.user or message.author.bot:
            return
        cfg = load_config()
        dcfg = cfg.get("discord", {})
        text = message.content.strip()
        if not text:
            return

        # ---- Direct messages: private chat with Steve ----
        if isinstance(message.channel, discord.DMChannel):
            admin = is_admin(message.author.id)
            if not admin and not dcfg.get("replyToAllDMs", True):
                return
            who = message.author.display_name + (" (an officer)" if admin else "")
            prompt = f"[{who} says via Discord DM]: {text}"
            async with message.channel.typing():
                reply = await brain.ask(f"dm:{message.author.id}", prompt,
                                        "discord_dm", message.author.display_name)
            await send_maybe_file(message.channel.send, reply or "(my AI brain is unreachable right now)")
            return  # DMs are private - never mirrored to the widget

        # ---- Server channels ----
        cid = message.channel.id

        mentioned = bot.user in message.mentions
        named = dcfg.get("respondToName", True) and NAME_RE.match(text)
        always_id = str(dcfg.get("alwaysRespondChannelId", "") or "").strip()
        always = bool(always_id) and str(cid) == always_id
        cooldown = int(dcfg.get("replyCooldownSeconds", 8))

        if (mentioned or named or always) and channel_allowed(dcfg, cid):
            # the always-respond channel is a running conversation - never skip a
            # message there due to cooldown, or the chat/memory loses turns
            if not always and time.monotonic() - last_reply.get(cid, 0) < cooldown:
                return
            last_reply[cid] = time.monotonic()
            clean = re.sub(rf"<@!?{bot.user.id}>", "Steve", text).strip()
            who = message.author.display_name + (" (an officer)" if is_admin(message.author.id) else "")
            prompt = f"[{who} says in Discord chat]: {clean}"
            async with message.channel.typing():
                reply = await brain.ask(f"guild:{cid}", prompt,
                                        "discord_chat", message.author.display_name)
            if reply:
                await deliver_reply(message, dcfg, cid, reply, clean, mention_reply=True)
                await show_on_stream("discord", message.author.display_name, clean, reply)
                if dcfg.get("speakTextRepliesInVoice", False):
                    voice_mgr.speak(message.guild.voice_client, reply)
            return

    # ---------------- Slash commands ----------------

    @bot.tree.command(name="ask", description="Ask Steve a question")
    @app_commands.describe(question="What do you want to ask?")
    async def ask_cmd(interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        prompt = f'[{interaction.user.display_name} asks via /ask]: {question}'
        reply = await brain.ask(f"guild:{interaction.channel_id}", prompt,
                                "discord_ask", interaction.user.display_name)
        if not reply:
            await interaction.followup.send("(my AI brain is unreachable right now)")
            return

        dcfg = load_config().get("discord", {})
        thread_ids = [str(i) for i in dcfg.get("threadReplyChannelIds", [])]
        cid = interaction.channel_id
        thread = None
        if str(cid) in thread_ids and isinstance(interaction.channel, discord.TextChannel):
            try:
                thread = await interaction.channel.create_thread(
                    name=question[:90].strip() or "steve",
                    type=discord.ChannelType.public_thread, auto_archive_duration=1440)
                await interaction.followup.send(f"Answered in {thread.mention}", ephemeral=True)
                await send_maybe_file(thread.send, reply)
                print(f"[discord] /ask replied in thread '{thread.name}' ({thread.id})")
            except discord.Forbidden:
                print("[discord] /ask thread reply failed: missing 'Create Public Threads' permission - "
                      "replying inline instead.")
                thread = None
            except Exception as e:
                print(f"[discord] /ask thread reply failed, replying inline instead: {e}")
                thread = None
        if thread is None:
            await send_maybe_file(interaction.followup.send, reply)

        await show_on_stream("discord", interaction.user.display_name, question, reply)
        if interaction.guild and dcfg.get("speakTextRepliesInVoice", False):
            voice_mgr.speak(interaction.guild.voice_client, reply)

    @bot.tree.command(name="status", description="Show Steve's status")
    async def status_cmd(interaction: discord.Interaction):
        cfg = load_config()
        ai_cfg = cfg.get("ai", {})
        provider = ai_cfg.get("provider", "ollama")
        model = ai_cfg.get("claudeModel") if provider == "claude" else ai_cfg.get("model")
        vc = interaction.guild.voice_client if interaction.guild else None
        voice = f"in **{vc.channel.name}**, listening" if vc and vc.is_connected() \
            else "not in a voice channel"
        await interaction.response.send_message(
            f"Mode: **{state.profile_name}** | AI: {provider} ({model}) | Voice: {voice}",
            ephemeral=True)

    @bot.tree.command(name="help", description="What can Steve do?")
    async def help_cmd(interaction: discord.Interaction):
        cfg = load_config()
        dcfg = cfg.get("discord", {})
        always_id = dcfg.get("alwaysRespondChannelId", "")
        always_note = (f"\nIn <#{always_id}> I answer every message, no need to say my name.\n"
                       if always_id else "\n")
        web_note = ('\nPaste a link and I\'ll read it, or say "look up X" / "search for X" and '
                    "I'll check the web.\n" if cfg.get("web", {}).get("enabled", True) else "\n")
        await interaction.response.send_message(
            "**Steve — IT Club assistant**\n"
            "Say my name or @ mention me in a channel and I'll answer. DM me to chat privately. "
            "In voice, say \"steve\" or \"hey steve\" before your question."
            f"{always_note}{web_note}\n"
            "/ask <question> — ask me anything, anywhere\n"
            "/poll <question> <options> — start a quick poll in this channel\n"
            "/status — what mode/model I'm running\n"
            "/join — pull me into your voice channel to listen and talk\n"
            "/leave — I'll head out of voice\n"
            "/hallucination — tell me to ignore a bad transcription\n\n"
            "Officer-only: /say, /meeting, /casual, /reset",
            ephemeral=True)

    @bot.tree.command(name="join", description="Steve joins your voice channel and starts listening")
    async def join_cmd(interaction: discord.Interaction):
        if interaction.user.voice is None:
            await interaction.response.send_message(
                "You need to be in a voice channel first!", ephemeral=True)
            return
        channel = interaction.user.voice.channel
        dcfg = load_config().get("discord", {})
        home_vc = dcfg.get("homeVoiceChannelId", "")
        if (dcfg.get("onlyHomeChannels") and home_vc
                and str(channel.id) != str(home_vc)):
            await interaction.response.send_message(
                f"Steve only hangs out in <#{home_vc}> — join him there!",
                ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await voice_mgr.join(channel, bot)
            await interaction.followup.send(f"Joined **{channel.name}** — listening!")
        except Exception as e:
            print(f"[voice] join error: {e}")
            await interaction.followup.send("Failed to join — check bot permissions.")

    @bot.tree.command(name="leave", description="Steve leaves the voice channel")
    async def leave_cmd(interaction: discord.Interaction):
        if interaction.guild and interaction.guild.voice_client:
            await voice_mgr.leave(interaction.guild)
            await interaction.response.send_message("Left the voice channel.")
        else:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

    @bot.tree.command(name="hallucination",
                      description="Mark the last thing Steve heard from you (or a phrase) as noise to ignore")
    @app_commands.describe(phrase="Phrase to block — leave blank to use your last transcription")
    async def hallucination_cmd(interaction: discord.Interaction, phrase: str = ""):
        name = interaction.user.display_name
        target = phrase.strip() or voice_mgr.last_transcription.get(name, "")
        if not target:
            await interaction.response.send_message(
                "No recent transcription — speak first or give a phrase.", ephemeral=True)
            return
        voice_mgr.add_hallucination(target)
        await interaction.response.send_message(
            f'Got it — I\'ll ignore "{target}" from now on.', ephemeral=True)

    @bot.tree.command(name="poll", description="Create a quick poll in this channel")
    @app_commands.describe(question="The poll question", option1="First answer", option2="Second answer",
                            option3="Third answer (optional)", option4="Fourth answer (optional)",
                            hours="How long the poll runs, in hours (default 24)")
    async def poll_cmd(interaction: discord.Interaction, question: str, option1: str, option2: str,
                        option3: str = "", option4: str = "", hours: int = 24):
        await interaction.response.defer()
        duration = datetime.timedelta(hours=max(1, min(hours, 168)))
        poll = discord.Poll(question=question, duration=duration)
        poll.add_answer(text=option1)
        poll.add_answer(text=option2)
        if option3:
            poll.add_answer(text=option3)
        if option4:
            poll.add_answer(text=option4)
        try:
            await interaction.channel.send(poll=poll)
            await interaction.followup.send("Poll posted!", ephemeral=True)
        except Exception as e:
            print(f"[discord] poll error: {e}")
            await interaction.followup.send(
                "Couldn't post the poll — needs the 'Create Polls' permission and a recent discord.py.",
                ephemeral=True)

    def officer_only(interaction):
        if not is_admin(interaction.user.id):
            return False
        return True

    @bot.tree.command(name="say", description="(officer) Make Steve say something on the widget")
    @app_commands.describe(text="What Steve should say on the overlay")
    async def say_cmd(interaction: discord.Interaction, text: str):
        if not officer_only(interaction):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        await show_on_stream("say", "", "", text)
        await interaction.response.send_message(f"Posted: {text}", ephemeral=True)

    async def _set_mode(interaction, mode):
        if not officer_only(interaction):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        state.mode = mode
        await interaction.response.send_message(
            f"Mode set to **{mode}**", ephemeral=True)

    @bot.tree.command(name="meeting", description="(officer) Switch Steve to meeting mode")
    async def meeting_cmd(interaction: discord.Interaction):
        await _set_mode(interaction, "meeting")

    @bot.tree.command(name="casual", description="(officer) Switch Steve back to casual mode")
    async def casual_cmd(interaction: discord.Interaction):
        await _set_mode(interaction, "casual")

    @bot.tree.command(name="reset", description="(officer) Clear Steve's conversation memory")
    @app_commands.describe(scope="Which memory to clear (default: everything)")
    @app_commands.choices(scope=[
        app_commands.Choice(name="everything", value="all"),
        app_commands.Choice(name="DMs", value="dm"),
        app_commands.Choice(name="Discord channels", value="guild"),
        app_commands.Choice(name="voice calls", value="voice"),
        app_commands.Choice(name="control panel chat", value="gui"),
    ])
    async def reset_cmd(interaction: discord.Interaction,
                        scope: app_commands.Choice[str] = None):
        if not officer_only(interaction):
            await interaction.response.send_message("Officers only.", ephemeral=True)
            return
        value = scope.value if scope else "all"
        n = brain.reset(None if value == "all" else value)
        label = scope.name if scope else "everything"
        await interaction.response.send_message(
            f"Cleared **{label}** — {n} conversation(s) wiped.", ephemeral=True)

    return bot
