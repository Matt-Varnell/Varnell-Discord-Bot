import os
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import discord
from discord.ext import commands
import requests
from discord.ext.commands import CommandOnCooldown
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("varnell_bot")

# Initialize bot with command prefix and intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Google Drive API setup
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = "146vaztLHzvMf5Ng5Er8t0ce_Inzw2TH4"

# Local audio temp storage
AUDIO_TEMP_DIR = Path("/tmp/varnell_audio")
AUDIO_TEMP_DIR.mkdir(parents=True, exist_ok=True)

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
        logger.info("Using cached file list.")
        return file_cache["files"]

    url = f"https://www.googleapis.com/drive/v3/files?q='{GOOGLE_DRIVE_FOLDER_ID}'+in+parents&key={GOOGLE_API_KEY}&fields=files(id,name)"
    response = requests.get(url)

    logger.info("Google Drive API Response: %s", response.status_code)

    if response.status_code != 200:
        return None

    file_cache["files"] = response.json().get("files", [])
    file_cache["timestamp"] = current_time
    return file_cache["files"]


def cleanup_old_temp_files(max_age: timedelta = timedelta(days=1)):
    """Delete stale temp audio files from disk."""
    now = datetime.now()
    for file_path in AUDIO_TEMP_DIR.glob("*"):
        if not file_path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            if now - mtime > max_age:
                file_path.unlink(missing_ok=True)
                logger.info("Removed stale temp file: %s", file_path)
        except Exception:
            logger.exception("Failed to remove stale temp file: %s", file_path)


def build_temp_audio_path(original_name: str) -> Path:
    """Build a safe, unique temp path for downloaded audio."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
    stem = Path(safe_name).stem or "audio"
    suffix = Path(safe_name).suffix or ".mp3"
    unique_name = f"{stem}_{uuid4().hex}{suffix}"
    return AUDIO_TEMP_DIR / unique_name


def download_file_to_path(url: str, destination: Path) -> int:
    """Download audio file to local disk. Returns total bytes written."""
    logger.info("Download start: %s", url)
    total_bytes = 0
    with requests.get(url, stream=True, allow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with open(destination, "wb") as output_file:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                output_file.write(chunk)
                total_bytes += len(chunk)

    logger.info("Download saved: path=%s bytes=%s", destination, total_bytes)
    return total_bytes


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

    files = await asyncio.to_thread(get_drive_files)
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

    await asyncio.to_thread(cleanup_old_temp_files)

    files = await asyncio.to_thread(get_drive_files)
    normalized_filename = filename.replace("_", " ").strip(' "\'').lower()
    logger.info("User requested: %s", normalized_filename)
    logger.info("Available files: %s", [f["name"].lower() for f in files])

    file_data = next((f for f in files if f["name"].lower() == normalized_filename), None)

    if not file_data:
        await ctx.send("File not found! Use `!list` to see available files.")
        return

    file_id = file_data["id"]
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    temp_audio_path = build_temp_audio_path(file_data["name"])

    try:
        await asyncio.to_thread(download_file_to_path, url, temp_audio_path)
    except requests.exceptions.RequestException as e:
        logger.exception("Error downloading file: %s", file_data["name"])
        await ctx.send(f"Error downloading file: {e}")
        return
    except Exception:
        logger.exception("Unexpected error while downloading: %s", file_data["name"])
        await ctx.send("Unexpected error while downloading file.")
        return

    def after_playback(error):
        if error:
            logger.exception("Playback error for %s", file_data["name"], exc_info=error)

        def cleanup_file():
            try:
                temp_audio_path.unlink(missing_ok=True)
                logger.info("Cleanup success: %s", temp_audio_path)
            except Exception:
                logger.exception("Cleanup failure: %s", temp_audio_path)

        bot.loop.call_soon_threadsafe(cleanup_file)

    audio_source = discord.FFmpegPCMAudio(
        str(temp_audio_path),
        executable="ffmpeg",
        before_options="-nostdin -hide_banner",
        options="-vn -loglevel warning",
    )

    logger.info("Playback start: %s", temp_audio_path)
    vc.play(audio_source, after=after_playback)
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
