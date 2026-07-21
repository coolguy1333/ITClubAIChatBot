"""Discord side of the companion.

- DM the bot to talk with Steve privately (rolling memory).
- In server channels it answers @mentions / its name, and can auto-chime
  into community chatter every N messages.
- /join pulls Steve into a voice channel: he transcribes everyone with
  Vosk (local) and talks back with TTS.
- Slash commands for everyone (/ask, /status, /help, /join, /leave,
  /hallucination) and officer-only controls (/say, /meeting, /casual, /reset).
"""

import re
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

from state import load_config
from voice import VoiceManager

NAME_RE = re.compile(r"\bsteve\b", re.IGNORECASE)


def create_bot(state, brain, broadcaster):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    voice_mgr = VoiceManager(state, brain, broadcaster)
    bot.voice_mgr = voice_mgr

    recent = {}       # channel_id -> deque of "user: text" for auto-react context
    counters = {}     # channel_id -> messages since last auto-react
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
            if not admin and not dcfg.get("replyToAllDMs", False):
                return
            who = dcfg.get("adminName", "an officer") if admin else message.author.display_name
            prompt = f"[{who} says via Discord DM]: {text}"
            async with message.channel.typing():
                reply = await brain.ask(f"dm:{message.author.id}", prompt,
                                        "discord_dm", message.author.display_name)
            await message.channel.send(reply or "(my AI brain is unreachable right now)")
            return  # DMs are private - never mirrored to the widget

        # ---- Server channels ----
        cid = message.channel.id
        recent.setdefault(cid, deque(maxlen=8)).append(
            f"{message.author.display_name}: {text}")

        mentioned = bot.user in message.mentions
        named = dcfg.get("respondToName", True) and NAME_RE.search(text)
        always_id = str(dcfg.get("alwaysRespondChannelId", "") or "").strip()
        always = bool(always_id) and str(cid) == always_id
        cooldown = int(dcfg.get("replyCooldownSeconds", 8))

        if (mentioned or named or always) and channel_allowed(dcfg, cid):
            if time.monotonic() - last_reply.get(cid, 0) < cooldown:
                return
            last_reply[cid] = time.monotonic()
            clean = re.sub(rf"<@!?{bot.user.id}>", "Steve", text).strip()
            who = dcfg.get("adminName", "") if is_admin(message.author.id) \
                else message.author.display_name
            prompt = f"[{who} says in Discord chat]: {clean}"
            async with message.channel.typing():
                reply = await brain.ask(f"guild:{cid}", prompt,
                                        "discord_chat", message.author.display_name)
            if reply:
                await message.reply(reply, mention_author=False)
                await show_on_stream("discord", message.author.display_name, clean, reply)
                voice_mgr.speak(message.guild.voice_client, reply)
            counters[cid] = 0
            return

        # ---- Community auto-react ----
        if not dcfg.get("autoReactEnabled", True):
            return
        every = int(dcfg.get("autoReactEveryMessages", 6))
        if every <= 0 or not channel_allowed(dcfg, cid):
            return
        counters[cid] = counters.get(cid, 0) + 1
        if counters[cid] < every:
            return
        counters[cid] = 0
        last_reply[cid] = time.monotonic()
        buzz = "\n".join(recent[cid])
        prompt = ("[Recent Discord community chat]\n" + buzz +
                  "\n[React briefly to what the community is talking about, "
                  "like a friend hanging out in the chat. Don't address anyone as 'user'.]")
        reply = await brain.ask(f"guild:{cid}", prompt, "discord_auto", "community")
        if reply:
            await message.channel.send(reply)
            await show_on_stream("discord", "community", "", reply)

    # ---------------- Slash commands ----------------

    @bot.tree.command(name="ask", description="Ask Steve a question")
    @app_commands.describe(question="What do you want to ask?")
    async def ask_cmd(interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        prompt = f'[{interaction.user.display_name} asks via /ask]: {question}'
        reply = await brain.ask(f"guild:{interaction.channel_id}", prompt,
                                "discord_ask", interaction.user.display_name)
        await interaction.followup.send(reply or "(my AI brain is unreachable right now)")
        if reply:
            await show_on_stream("discord", interaction.user.display_name, question, reply)
            if interaction.guild:
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
        dcfg = load_config().get("discord", {})
        always_id = dcfg.get("alwaysRespondChannelId", "")
        always_note = (f"\nIn <#{always_id}> I answer every message, no need to say my name.\n"
                       if always_id else "\n")
        await interaction.response.send_message(
            "**Steve — IT Club assistant**\n"
            "Say my name or @ mention me in a channel and I'll answer. DM me to chat privately. "
            "In voice, say \"steve\" or \"hey steve\" before your question."
            f"{always_note}\n"
            "/ask <question> — ask me anything, anywhere\n"
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
