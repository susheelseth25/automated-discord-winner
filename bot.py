import discord
from discord.ext import commands, tasks
import random
from datetime import datetime, timedelta, timezone
import os
import re
import json
import asyncio
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

TOKEN = os.getenv("TOKEN")
APOLLO_CHANNEL_ID = 1158100032248086619
APOLLO_BOT_ID = 475744554910351370

VOICE_CHANNELS = [
    1160675378814271668,  # A
    1141760803704410222,  # B
    1141330327017631825,  # C
    1141331270727651388,  # D
]

MIN_PLAYERS = 12
STATE_FILE = "bot_state.json"
MSK = timezone(timedelta(hours=3))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

registered_players = set()
present_during_event = set()
event_end_time = None
draw_done = False
current_draw_number = 0
draw_history = []

def now_msk():
    return datetime.now(MSK)

def save_state():
    data = {
        "registered_players": list(registered_players),
        "present_during_event": list(present_during_event),
        "event_end_time": event_end_time.isoformat() if event_end_time else None,
        "draw_done": draw_done,
        "current_draw_number": current_draw_number,
        "draw_history": draw_history
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Failed to save state:", e)

def load_state():
    global registered_players, present_during_event, event_end_time, draw_done, current_draw_number, draw_history
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        registered_players = set(data.get("registered_players", []))
        present_during_event = set(data.get("present_during_event", []))
        end_str = data.get("event_end_time")
        event_end_time = datetime.fromisoformat(end_str) if end_str else None
        draw_done = data.get("draw_done", False)
        current_draw_number = data.get("current_draw_number", 0)
        draw_history = data.get("draw_history", [])
        print(f"Loaded state: Draw #{current_draw_number}, {len(registered_players)} registered")
    except Exception as e:
        print("Failed to load state:", e)

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        return

def run_web():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), DummyHandler).serve_forever()

Thread(target=run_web, daemon=True).start()

def parse_end_time(content: str):
    time_section = re.search(r'Time\s*[:\n]?\s*(.+)', content, re.IGNORECASE | re.DOTALL)
    search_text = time_section.group(1) if time_section else content

    time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', search_text, re.IGNORECASE)
    if not time_match:
        return None

    start_str = time_match.group(1).strip().upper()
    try:
        start_dt = datetime.strptime(start_str, "%I:%M %p")
    except ValueError:
        try:
            start_dt = datetime.strptime(start_str, "%H:%M")
        except ValueError:
            return None

    now = now_msk()
    start_time = now.replace(hour=start_dt.hour, minute=start_dt.minute, second=0, microsecond=0)
    if start_time < now:
        start_time += timedelta(days=1)

    return start_time + timedelta(minutes=60)

def start_new_draw():
    global current_draw_number, registered_players, present_during_event, draw_done
    current_draw_number += 1
    registered_players = set()
    present_during_event = set()
    draw_done = False
    save_state()
    print(f"Started new draw #{current_draw_number}")

async def scan_recent_events():
    global event_end_time, draw_done
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if not channel:
        return
    try:
        async for message in channel.history(limit=30):
            if message.author.id == APOLLO_BOT_ID:
                end_time = parse_end_time(message.content)
                if end_time:
                    if event_end_time != end_time:
                        start_new_draw()
                    event_end_time = end_time
                    save_state()
                    print("Startup scan set end time")
                    break

        try:
            threads = await channel.guild.active_threads()
            apollo_threads = [t for t in threads if t.parent_id == APOLLO_CHANNEL_ID]
            if apollo_threads:
                newest = max(apollo_threads, key=lambda t: t.created_at or datetime.min)
                print(f"Scanning newest thread only: {newest.name}")
                await newest.join()
                await asyncio.sleep(3)
                await scan_thread(newest)
        except Exception as e:
            print(f"Error scanning newest thread: {e}")

    except Exception as e:
        print(f"Startup scan error: {e}")

async def announce_capture(count):
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if not channel:
        return
    await channel.send(
        f"✅ **Draw #{current_draw_number}** — Captured {count} registered players.\n"
        f"Lucky draw result is set to 60 minutes from event start time (MSK)."
    )

async def process_thread_updates(thread):
    global registered_players
    try:
        async for message in thread.history(limit=30):
            if message.author.id != APOLLO_BOT_ID:
                continue
            content = message.content.lower()
            if "added" in content and message.mentions:
                for m in message.mentions:
                    registered_players.add(m.id)
            elif "removed" in content and message.mentions:
                for m in message.mentions:
                    registered_players.discard(m.id)
        save_state()
    except Exception as e:
        print(f"Error processing updates in {thread.name}: {e}")

async def scan_thread(thread):
    global registered_players, event_end_time, draw_done
    try:
        print(f"Scanning thread: {thread.name}")
        best_message = None
        max_mentions = 0

        async for message in thread.history(limit=20):
            if message.author.id == APOLLO_BOT_ID and len(message.mentions) > max_mentions:
                max_mentions = len(message.mentions)
                best_message = message

        if best_message and max_mentions >= 2:
            registered_players = {m.id for m in best_message.mentions}
            draw_done = False
            event_end_time = parse_end_time(best_message.content) or event_end_time
            print(f"Initial capture: {len(registered_players)} players for Draw #{current_draw_number}")

            await process_thread_updates(thread)
            save_state()
            await announce_capture(len(registered_players))
            return

        print(f"No bulk tagged message found in: {thread.name}")
    except Exception as e:
        print(f"Scan error for {thread.name}: {e}")

async def track_voice_presence():
    global present_during_event
    if not registered_players or draw_done or not event_end_time:
        return
    now = now_msk()
    if now > event_end_time:
        return
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            for member in vc.members:
                if member.id in registered_players:
                    present_during_event.add(member.id)
    save_state()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    load_state()
    if not check_event_end.is_running():
        check_event_end.start()
    await scan_recent_events()
    print("Ready. Listening for new threads and events...")

@bot.event
async def on_thread_create(thread):
    if thread.parent_id != APOLLO_CHANNEL_ID:
        return
    try:
        await thread.join()
        print(f"Joined new thread: {thread.name}")
        await asyncio.sleep(10)
        await scan_thread(thread)
    except Exception as e:
        print(f"Thread join/scan failed: {e}")

@bot.event
async def on_message(message):
    global event_end_time, draw_done
    if message.author.bot and message.author.id != APOLLO_BOT_ID:
        return

    if not message.author.bot:
        await bot.process_commands(message)

    # Every new Apollo event message starts a fresh draw
    if (message.channel.id == APOLLO_CHANNEL_ID and 
        message.author.id == APOLLO_BOT_ID):
        end_time = parse_end_time(message.content)
        if end_time:
            start_new_draw()
            event_end_time = end_time
            save_state()
            print(f"New event detected — Draw #{current_draw_number}")

    if message.author.id == APOLLO_BOT_ID and "started a thread" in message.content.lower():
        print(f"Apollo started a thread: {message.content}")
        channel = bot.get_channel(APOLLO_CHANNEL_ID)
        if channel:
            try:
                threads = await channel.guild.active_threads()
                apollo_threads = [t for t in threads if t.parent_id == APOLLO_CHANNEL_ID]
                if apollo_threads:
                    newest = max(apollo_threads, key=lambda t: t.created_at or datetime.min)
                    await newest.join()
                    await asyncio.sleep(8)
                    await scan_thread(newest)
            except Exception as e:
                print(f"Error on new thread: {e}")

@tasks.loop(minutes=1)
async def check_event_end():
    global draw_done
    await track_voice_presence()
    if not registered_players or draw_done or not event_end_time:
        return
    now = now_msk()
    if now >= event_end_time:
        await run_draw()

async def run_draw():
    global registered_players, draw_done, present_during_event, draw_history
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if not channel:
        return

    present = []
    for uid in present_during_event:
        member = channel.guild.get_member(uid)
        if member:
            present.append(member)

    start_time = (event_end_time - timedelta(minutes=60)).strftime("%H:%M") if event_end_time else "Unknown"
    end_time_str = event_end_time.strftime("%H:%M") if event_end_time else "Unknown"

    if len(present) < MIN_PLAYERS:
        result = f"**Draw #{current_draw_number}** ended.\nOnly {len(present)} registered players were in voice during the event (minimum required: {MIN_PLAYERS}). No winners posted."
        winners_list = []
    else:
        winners_count = max(1, len(present) // 3)
        winners = random.sample(present, min(winners_count, len(present)))
        mentions = " ".join(w.mention for w in winners)
        winners_list = [w.display_name for w in winners]
        result = (
            f"🎉 **Draw #{current_draw_number} Winners** 🎉\n"
            f"Players who joined voice: {len(present)}\n"
            f"Winners ({winners_count}): {mentions}"
        )

    await channel.send(result)

    draw_history.append({
        "draw_number": current_draw_number,
        "start_time": start_time,
        "end_time": end_time_str,
        "registered": len(registered_players),
        "present": len(present),
        "winners": winners_list,
        "timestamp": now_msk().isoformat()
    })
    if len(draw_history) > 50:
        draw_history = draw_history[-50:]

    draw_done = True
    present_during_event.clear()
    save_state()

@bot.command()
async def connect(ctx):
    await ctx.send(f"Connected! Bot is online. Current Draw: #{current_draw_number}")

@bot.command()
async def stop(ctx):
    await ctx.send(f"🛑 Draw #{current_draw_number} — Bot is stopping...")
    await bot.close()

@bot.command()
async def start(ctx):
    await ctx.send("ℹ️ Ask bot admin to restart webserver.")

@bot.command()
async def restart(ctx):
    await ctx.send(f"🔄 Draw #{current_draw_number} — Bot is restarting... Ask bot admin to restart webserver.")
    await bot.close()

@bot.command()
async def status(ctx):
    voice_count = 0
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            voice_count += len(vc.members)
    end_str = event_end_time.strftime("%H:%M MSK") if event_end_time else "Not set"
    start_str = (event_end_time - timedelta(minutes=60)).strftime("%H:%M MSK") if event_end_time else "Not set"
    await ctx.send(
        f"**Draw #{current_draw_number}**\n"
        f"Start time: {start_str}\n"
        f"End time: {end_str}\n"
        f"Registered: {len(registered_players)} | In Voice now: {voice_count} | "
        f"Present during event: {len(present_during_event)} | Draw done: {draw_done}"
    )

@bot.command()
async def details(ctx):
    registered_names = []
    for uid in registered_players:
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        registered_names.append(name)
    reg_text = ", ".join(registered_names) if registered_names else "None"

    voice_names = []
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            for member in vc.members:
                voice_names.append(f"{member.display_name} ({vc.name})")
    voice_text = ", ".join(voice_names) if voice_names else "None"

    present_names = []
    for uid in present_during_event:
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        present_names.append(name)
    present_text = ", ".join(present_names) if present_names else "None"

    await ctx.send(
        f"**Draw #{current_draw_number}**\n"
        f"**Registered ({len(registered_names)}):** {reg_text}\n"
        f"**In Voice now ({len(voice_names)}):** {voice_text}\n"
        f"**Present during event ({len(present_names)}):** {present_text}"
    )

@bot.command()
async def history(ctx, number: int = None):
    if not draw_history:
        await ctx.send("No draw history yet.")
        return

    if number is not None:
        for d in draw_history:
            if d["draw_number"] == number:
                winners = ", ".join(d["winners"]) if d["winners"] else "None"
                await ctx.send(
                    f"**Draw #{d['draw_number']}**\n"
                    f"Time: {d['start_time']} – {d['end_time']} MSK\n"
                    f"Registered: {d['registered']} | Present: {d['present']}\n"
                    f"Winners: {winners}"
                )
                return
        await ctx.send(f"Draw #{number} not found.")
        return

    lines = ["**Last 5 draws:**"]
    for d in reversed(draw_history[-5:]):
        winners = ", ".join(d["winners"]) if d["winners"] else "None"
        lines.append(
            f"Draw #{d['draw_number']} | {d['start_time']}–{d['end_time']} MSK | "
            f"Reg: {d['registered']} | Present: {d['present']} | Winners: {winners}"
        )
    await ctx.send("\n".join(lines))

@bot.command()
async def voices(ctx):
    lines = []
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            lines.append(f"✅ {vc.name} — {len(vc.members)} member(s)")
        else:
            lines.append(f"❌ Cannot see channel ID {vc_id}")
    await ctx.send(f"**Draw #{current_draw_number}** — Voice Channel Access:\n" + "\n".join(lines))

@bot.command()
async def forcedraw(ctx):
    await ctx.send(f"Running Draw #{current_draw_number} manually...")
    await run_draw()

@bot.command()
async def reset(ctx):
    global registered_players, present_during_event, event_end_time, draw_done
    registered_players = set()
    present_during_event = set()
    event_end_time = None
    draw_done = False
    save_state()
    await ctx.send(f"Draw #{current_draw_number} state reset.")

if __name__ == "__main__":
    bot.run(TOKEN)