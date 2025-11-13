import os
import discord
from discord.ext import commands
import requests
import json
from io import BytesIO
import time
import asyncio  # ✅ Needed for async timeout
from discord.ext.commands import CommandOnCooldown
import discord.opus

# Load Opus library for voice support
if not discord.opus.is_loaded():
    discord.opus.load_opus("libopus.so")

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Google Drive Setup
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = "146vaztLHzvMf5Ng5Er8t0ce_Inzw2TH4"

# Command rate-limiting
cooldowns = commands.CooldownMapping.from_cooldown(3, 60.0, commands.BucketType.user)

# Cache for Drive file listing
CACHE_EXPIRATION = 300
file_cache = {
    "files": [],
    "timestamp": 0
}

def get_drive_files():
    """Fetch and cache Google Drive MP3 file list."""
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
    """Joins the voice channel of the user."""
    if ctx.author.voice:
        try:
            await ctx.author.voice.channel.connect(timeout=10)
        except asyncio.TimeoutError:
            await ctx.send("Failed to connect to the voice channel in time.")
    else:
        await ctx.send("You need to be in a voice channel first!")

@bot.command()
async def list(ctx):
    """Lists available MP3 files in the Drive folder with cooldown."""
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
    """Plays an MP3 file from Google Drive."""
    # Normalize filename
    normalized_filename = filename.replace("_", " ").strip(' "\'').lower()
    print(f"User requested: {normalized_filename}")

    files = get_drive_files()
    print("Available files:", [f["name"].lower() for f in files])
    file_data = next((f for f in files if f["name"].lower() == normalized_filename), None)

    if not file_data:
        await ctx.send("File not found! Use `!list` to see available files.")
        return

    # Connect to voice if not already
    if not ctx.voice_client:
        if ctx.author.voice:
            try:
                await ctx.author.voice.channel.connect(timeout=10)
            except asyncio.TimeoutError:
                await ctx.send("Failed to connect to the voice channel in time.")
                return
        else:
            await ctx.send("You need to be in a voice channel first!")
            return

    vc = ctx.voice_client
    if vc.is_playing():
        vc.stop()

    # Download audio file from Drive
    file_id = file_data["id"]
    url = f"https://drive.google.com/uc?export=download&id={file_id}"

    try:
        response = requests.get(url, allow_redirects=True)
        response.raise_for_status()
        audio_data = BytesIO(response.content)
    except requests.exceptions.RequestException as e:
        await ctx.send(f"Error downloading file: {e}")
        return

    try:
        vc.play(discord.FFmpegPCMAudio(audio_data, pipe=True, executable="ffmpeg"),
                after=lambda e: print(f"Finished playing {file_data['name']}"))
        await ctx.send(f"Now playing: {file_data['name']}")
    except discord.ClientException as e:
        await ctx.send("Something went wrong playing the audio.")
        print("Audio error:", e)

@bot.command()
async def leave(ctx):
    """Leaves the voice channel."""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Bot has left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")

# Start bot using secret token
token = os.getenv("TOKEN")
if not token:
    raise Exception("Please add your TOKEN to the Replit Secrets pane.")

bot.run(token)
