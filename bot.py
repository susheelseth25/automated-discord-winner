import discord
from discord.ext import commands, tasks
import random
from datetime import datetime, timezone, timedelta
import os
import re
import json
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
IST = timezone(timedelta(hours=5, minutes=30))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

registered_players = set()
event_end_time = None
draw_done = False

def save_state():
    data = {
        "registered_players": list(registered_players),
        "event_end_time": event_end_time.isoformat() if event_end_time else None,
        "draw_done": draw_done
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Failed to save state:", e)

def load_state():
    global registered_players, event_end_time, draw_done
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        registered_players = set(data.get("registered_players", []))
        end_str = data.get("event_end_time")
        event_end_time = datetime.fromisoformat(end_str) if end_str else None
        draw_done = data.get("draw_done", False)
        print(f"Loaded state: {len(registered_players)} players")
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
    match = re.search(
        r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*[-–]\s*(\d{1,2}:\d{2}\s*(?:AM|PM)?)',
        content,
        re.IGNORECASE
    )
    if not match:
        return None
    end_str = match.group(2).strip().upper()
    try:
        end_dt = datetime.strptime(end_str, "%I:%M %p")
    except ValueError:
        try:
            end_dt = datetime.strptime(end_str, "%H:%M")
        except ValueError:
            return None
    now = datetime.now(IST)
    end_time = now.replace(hour=end_dt.hour, minute=end_dt.minute, second=0, microsecond=0)
    if end_time < now:
        end_time += timedelta(days=1)
    return end_time

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    load_state()
    check_event_end.start()
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if channel:
        try:
            threads = channel.threads
            for thread in threads:
                await thread.join()
                print(f"Joined thread: {thread.name}")
        except Exception as e:
            print("Could not join threads:", e)

@bot.event
async def on_thread_create(thread):
    if thread.parent_id == APOLLO_CHANNEL_ID:
        try:
            await thread.join()
            print(f"Joined new thread: {thread.name}")
        except Exception as e:
            print("Failed to join thread:", e)

@bot.event
async def on_message(message):
    global registered_players, event_end_time, draw_done
    if message.author == bot.user:
        return

    # Process commands in the Apollo channel or its threads
    if message.channel.id == APOLLO_CHANNEL_ID or (isinstance(message.channel, discord.Thread) and message.channel.parent_id == APOLLO_CHANNEL_ID):
        await bot.process_commands(message)

    # Capture Apollo reminder
    if message.author.id == APOLLO_BOT_ID and message.mentions:
        if "REMINDER" in message.content.upper() or "starting" in message.content.lower():
            registered_players = {m.id for m in message.mentions}
            draw_done = False
            event_end_time = parse_end_time(message.content)
            save_state()
            print(f"Captured {len(registered_players)} players from reminder")

@tasks.loop(minutes=1)
async def check_event_end():
    global draw_done
    if not registered_players or draw_done or not event_end_time:
        return
    now = datetime.now(IST)
    if now >= event_end_time:
        await run_draw()

async def run_draw():
    global registered_players, draw_done
    channel = bot.get_channel(APOLLO_CHANNEL_ID)
    if not channel:
        return

    present = set()
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            for member in vc.members:
                if member.id in registered_players:
                    present.add(member)

    if len(present) < MIN_PLAYERS:
        await channel.send(
            f"Event ended. Only {len(present)} registered players were in voice "
            f"(minimum required: {MIN_PLAYERS}). No winners posted."
        )
        draw_done = True
        save_state()
        return

    winners_count = max(1, len(present) // 3)
    winners = random.sample(list(present), min(winners_count, len(present)))
    mentions = " ".join(w.mention for w in winners)

    await channel.send(
        f"🎉 **Event Winners** 🎉\n"
        f"Valid players in voice: {len(present)}\n"
        f"Winners ({winners_count}): {mentions}"
    )
    draw_done = True
    save_state()

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot can see messages.")

@bot.command()
async def status(ctx):
    present = 0
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            present += sum(1 for m in vc.members if m.id in registered_players)
    end_str = event_end_time.strftime("%H:%M IST") if event_end_time else "Not set"
    await ctx.send(
        f"Registered: {len(registered_players)} | Present in ABCD: {present} | "
        f"End time: {end_str} | Draw done: {draw_done}"
    )

@bot.command()
async def voices(ctx):
    lines = []
    for vc_id in VOICE_CHANNELS:
        vc = bot.get_channel(vc_id)
        if vc and isinstance(vc, discord.VoiceChannel):
            members = len(vc.members)
            lines.append(f"✅ {vc.name} — {members} member(s)")
        else:
            lines.append(f"❌ Cannot see channel ID {vc_id}")
    await ctx.send("**Voice Channel Access:**\n" + "\n".join(lines))

@bot.command()
async def forcedraw(ctx):
    await run_draw()

@bot.command()
async def reset(ctx):
    global registered_players, event_end_time, draw_done
    registered_players = set()
    event_end_time = None
    draw_done = False
    save_state()
    await ctx.send("State reset.")

if __name__ == "__main__":
    bot.run(TOKEN)