import os
import discord
from discord.ext import commands
import requests
import json
from io import BytesIO
import time
from discord.ext.commands import CommandOnCooldown

# Initialize bot with command prefix and intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Google Drive API setup
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = "146vaztLHzvMf5Ng5Er8t0ce_Inzw2TH4"

# Rate limiting to prevent API bans
cooldowns = commands.CooldownMapping.from_cooldown(3, 60.0, commands.BucketType.user)  # 3 commands per minute

# Caching system to reduce API calls
CACHE_EXPIRATION = 300  # Cache for 5 minutes
file_cache = {
    "files": [],
    "timestamp": 0
}

def get_drive_files():
    """Fetches the list of MP3 files from the Google Drive folder and caches it."""
    current_time = time.time()
    if current_time - file_cache["timestamp"] < CACHE_EXPIRATION:
        print("Using cached file list.")
        return file_cache["files"]

    url = f"https://www.googleapis.com/drive/v3/files?q='{GOOGLE_DRIVE_FOLDER_ID}'+in+parents&key={GOOGLE_API_KEY}&fields=files(id,name)"
    response = requests.get(url)

    print("Google Drive API Response:", response.status_code, response.text)

    if response.status_code != 200:
        return None

    file_cache["files"] = response.json().get("files", [])
    file_cache["timestamp"] = current_time
    return file_cache["files"]

@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

@bot.command()
async def join(ctx):
    """Joins the voice channel the user is in."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
    else:
        await ctx.send("You need to be in a voice channel first!")

@bot.command()
async def list(ctx):
    """Lists available MP3 files in the Google Drive folder with rate limit protection."""
    bucket = cooldowns.get_bucket(ctx.message)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await ctx.send(f"You're using commands too fast! Try again in {int(retry_after)} seconds.")
        return

    files = get_drive_files()
    if not files:
        await ctx.send("No MP3 files found or unable to fetch files.")
    else:
        file_list = "\n".join([f["name"] for f in files if f["name"].endswith(".mp3")])
        await ctx.send(f"Available MP3 files:\n```{file_list}```")

@bot.command()
async def play(ctx, *, filename: str):
    """Plays an MP3 file from Google Drive, allowing underscores instead of spaces."""
    if not ctx.voice_client:
        await ctx.invoke(join)

    vc = ctx.voice_client

    if vc.is_playing():
        vc.stop()

    files = get_drive_files()
    normalized_filename = filename.replace("_", " ").strip(' "\'').lower()
    print(f"User requested: {normalized_filename}")
    print("Available files:", [f["name"].lower() for f in files])

    file_data = next((f for f in files if f["name"].lower() == normalized_filename), None)

    if not file_data:
        await ctx.send("File not found! Use `!list` to see available files.")
        return

    file_id = file_data["id"]
    url = f"https://drive.google.com/uc?export=download&id={file_id}"

    try:
        response = requests.get(url, allow_redirects=True)
        response.raise_for_status()
        audio_data = BytesIO(response.content)
    except requests.exceptions.RequestException as e:
        await ctx.send(f"Error downloading file: {e}")
        return

    vc.play(discord.FFmpegPCMAudio(audio_data, pipe=True, executable="ffmpeg"), after=lambda e: print(f"Finished playing {file_data['name']}"))
    await ctx.send(f"Now playing: {file_data['name']}")

@bot.command()
async def leave(ctx):
    """Disconnects bot from voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Bot has left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")

# Load bot token from environment variables
token = os.getenv("TOKEN")
if not token:
    raise Exception("Please add your token to the environment variables.")

bot.run(token)
