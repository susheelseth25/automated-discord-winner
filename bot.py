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
REWARDS_CHANNEL_ID = 1480554579467698176
APOLLO_BOT_ID = 475744554910351370

VOICE_CHANNELS = [
    1160675378814271668,
    1141760803704410222,
    1141330327017631825,
    1141331270727651388,
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
        if end_str:
            event_end_time = datetime.fromisoformat(end_str)
            if event_end_time.tzinfo is None:
                event_end_time = event_end_time.replace(tzinfo=MSK)
        else:
            event_end_time = None
        draw_done = data.get("draw_done", False)
        current_draw_number = data.get("current_draw_number", 0)
        draw_history = data.get("draw_history", [])
        print(f"Loaded state: Draw #{current_draw_number}")
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

def get_times():
    if not event_end_time:
        return "Not set", "Not set"
    start = (event_end_time - timedelta(minutes=60)).strftime("%H:%M MSK")
    end = event_end_time.strftime("%H:%M MSK")
    return start, end

def start_new_draw():
    global current_draw_number, registered_players, present_during_event, draw_done
    current_draw_number += 1
    registered_players = set()
    present_during_event = set()
    draw_done = False
    save_state()
    print(f"Started new draw #{current_draw_number}")

async def finish_pending_draw_if_needed():
    """On startup, finish any previous draw whose end time has already passed."""
    if event_end_time and not draw_done and now_msk() >= event_end_time:
        print("Finishing pending draw from previous run")
        await run_draw()

async def scan_recent_events():
    global event_end_time
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if not channel:
        return
    try:
        async for message in channel.history(limit=30):
            if message.author.id == APOLLO_BOT_ID:
                end_time = parse_end_time(message.content)
                if end_time:
                    event_end_time = end_time
                    save_state()
                    break
        threads = await channel.guild.active_threads()
        apollo_threads = [t for t in threads if t.parent_id == APOLLO_CHANNEL_ID]
        if apollo_threads:
            newest = max(apollo_threads, key=lambda t: t.created_at or datetime.min)
            await newest.join()
            await asyncio.sleep(3)
            await scan_thread(newest)
    except Exception as e:
        print("Startup scan error:", e)

async def announce_capture(count):
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if channel:
        start, end = get_times()
        await channel.send(
            f"✅ **Draw #{current_draw_number}** — Captured {count} registered players.\n"
            f"Start: {start} | End: {end}\n"
            f"Lucky draw result will be posted in <#{REWARDS_CHANNEL_ID}>."
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
        print("Update error:", e)

async def scan_thread(thread):
    global registered_players, event_end_time, draw_done
    try:
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
            await process_thread_updates(thread)
            save_state()
            await announce_capture(len(registered_players))
    except Exception as e:
        print("Scan error:", e)

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
    await finish_pending_draw_if_needed()
    if not check_event_end.is_running():
        check_event_end.start()
    await scan_recent_events()
    print("Ready")

@bot.event
async def on_thread_create(thread):
    if thread.parent_id != APOLLO_CHANNEL_ID:
        return
    try:
        await thread.join()
        await asyncio.sleep(10)
        await scan_thread(thread)
    except Exception as e:
        print("Thread error:", e)

@bot.event
async def on_message(message):
    if message.author.bot and message.author.id != APOLLO_BOT_ID:
        return
    if not message.author.bot:
        await bot.process_commands(message)
    if message.channel.id == APOLLO_CHANNEL_ID and message.author.id == APOLLO_BOT_ID:
        end_time = parse_end_time(message.content)
        if end_time:
            start_new_draw()
            global event_end_time
            event_end_time = end_time
            save_state()
            channel = bot.get_channel(APOLLO_CHANNEL_ID)
            if channel:
                start, end = get_times()
                await channel.send(
                    f"⏰ **Draw #{current_draw_number}** started.\n"
                    f"Start: {start} | End: {end}\n"
                    f"Results will be posted in <#{REWARDS_CHANNEL_ID}>."
                )
    if message.author.id == APOLLO_BOT_ID and "started a thread" in message.content.lower():
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
                print("Thread detect error:", e)

@tasks.loop(minutes=1)
async def check_event_end():
    await track_voice_presence()
    save_state()
    now = now_msk()
    print(f"CHECK | now={now.strftime('%H:%M')} | end={event_end_time} | done={draw_done}")
    if not registered_players or draw_done or not event_end_time:
        return
    if now >= event_end_time:
        await run_draw()

async def run_draw():
    global draw_done, present_during_event, draw_history
    rewards_channel = bot.get_channel(REWARDS_CHANNEL_ID)
    if not rewards_channel:
        print("Rewards channel not found")
        return

    present = []
    for uid in present_during_event:
        member = rewards_channel.guild.get_member(uid)
        if member:
            present.append(member)

    start, end = get_times()

    if len(present) < MIN_PLAYERS:
        result = (
            f"**Draw #{current_draw_number}** ended.\n"
            f"Start: {start} | End: {end}\n"
            f"Registered: {len(registered_players)} | Present in voice: {len(present)}\n"
            f"Only {len(present)} registered players were in voice (minimum required: {MIN_PLAYERS}). No winners posted."
        )
        winners_list = []
    else:
        winners_count = max(1, len(present) // 3)
        winners = random.sample(present, min(winners_count, len(present)))
        mentions = " ".join(w.mention for w in winners)
        winners_list = [w.display_name for w in winners]
        result = (
            f"🎉 **Draw #{current_draw_number} Winners** 🎉\n"
            f"Start: {start} | End: {end}\n"
            f"Registered: {len(registered_players)} | Present in voice: {len(present)}\n"
            f"Winners ({winners_count}): {mentions}"
        )

    await rewards_channel.send(result)

    main_channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if main_channel:
        await main_channel.send(
            f"✅ **Draw #{current_draw_number}** result posted in <#{REWARDS_CHANNEL_ID}>.\n"
            f"Start: {start} | End: {end}"
        )

    draw_history.append({
        "draw_number": current_draw_number,
        "registered": len(registered_players),
        "present": len(present),
        "winners": winners_list
    })
    draw_done = True
    present_during_event.clear()
    save_state()

@bot.command()
async def connect(ctx):
    await ctx.send(f"Connected! Current Draw: #{current_draw_number}")

@bot.command()
async def status(ctx):
    start, end = get_times()
    await ctx.send(
        f"**Draw #{current_draw_number}**\n"
        f"Start: {start} | End: {end}\n"
        f"Registered: {len(registered_players)} | Present: {len(present_during_event)} | Done: {draw_done}"
    )

@bot.command()
async def details(ctx):
    start, end = get_times()
    names = []
    for uid in registered_players:
        m = ctx.guild.get_member(uid)
        names.append(m.display_name if m else str(uid))
    await ctx.send(
        f"**Draw #{current_draw_number}**\n"
        f"Start: {start} | End: {end}\n"
        f"Registered ({len(names)}): {', '.join(names) or 'None'}"
    )

@bot.command()
async def history(ctx, number: int = None):
    if not draw_history:
        await ctx.send("No history yet.")
        return
    if number:
        for d in draw_history:
            if d["draw_number"] == number:
                await ctx.send(f"Draw #{number}: {d}")
                return
        await ctx.send("Not found.")
        return
    await ctx.send("Last draws:\n" + "\n".join([str(d) for d in draw_history[-5:]]))

@bot.command()
async def forcedraw(ctx):
    await run_draw()

@bot.command()
async def cancel(ctx):
    global draw_done, registered_players, present_during_event, event_end_time, draw_history
    if current_draw_number == 0:
        await ctx.send("No active draw to cancel.")
        return
    start, end = get_times()
    await ctx.send(
        f"❌ **Draw #{current_draw_number}** has been cancelled.\n"
        f"Start: {start} | End: {end}"
    )
    draw_history.append({
        "draw_number": current_draw_number,
        "registered": len(registered_players),
        "present": len(present_during_event),
        "winners": ["CANCELLED"]
    })
    draw_done = True
    registered_players = set()
    present_during_event = set()
    event_end_time = None
    save_state()
    start_new_draw()

@bot.command()
async def reset(ctx):
    global registered_players, present_during_event, event_end_time, draw_done
    registered_players = set()
    present_during_event = set()
    event_end_time = None
    draw_done = False
    save_state()
    await ctx.send("Reset.")

if __name__ == "__main__":
    bot.run(TOKEN)