import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

import discord
from discord.ext import commands
from discord import opus
import psutil
import requests
from dotenv import load_dotenv

load_dotenv()

# --- Logging setup (console + rotating file) ---
LOG_DIR = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

logger = logging.getLogger("varnell_bot")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

# Playback mode switch for A/B testing on Raspberry Pi.
# Use "pcm" for FFmpegPCMAudio, or "opus" for FFmpegOpusAudio.
PLAYBACK_MODE = os.getenv("PLAYBACK_MODE", "pcm").strip().lower()
if PLAYBACK_MODE not in {"pcm", "opus"}:
    logger.warning("playback.mode invalid=%s fallback=pcm", PLAYBACK_MODE)
    PLAYBACK_MODE = "pcm"

PROCESS = psutil.Process(os.getpid()) if psutil else None
psutil.cpu_percent(interval=None)
if PROCESS:
    PROCESS.cpu_percent(interval=None)

# Initialize bot with command prefix and intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Google Drive API setup
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = "146vaztLHzvMf5Ng5Er8t0ce_Inzw2TH4"

# Local audio temp storage
AUDIO_TEMP_DIR = Path("/tmp/varnell_audio")
AUDIO_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Rate limiting to prevent API bans
cooldowns = commands.CooldownMapping.from_cooldown(3, 60.0, commands.BucketType.user)

# Caching system to reduce API calls
CACHE_EXPIRATION = 300
file_cache = {"files": [], "timestamp": 0}

# Playback diagnostics state
playback_state = {
    "local_path": None,
    "display_name": None,
    "mode": PLAYBACK_MODE,
    "ffmpeg_pid": None,
    "ffmpeg_args": None,
    "ffmpeg_stderr_path": None,
    "ffmpeg_stderr_handle": None,
    "download_seconds": None,
    "command_start_monotonic": None,
    "cleanup_started": False,
    "diag_task": None,
}


def get_drive_files():
    """Fetch list of MP3 files from Google Drive and cache it."""
    current_time = time.time()
    if current_time - file_cache["timestamp"] < CACHE_EXPIRATION:
        logger.info("file_list.cache_hit count=%s", len(file_cache["files"]))
        return file_cache["files"]

    url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q='{GOOGLE_DRIVE_FOLDER_ID}'+in+parents&key={GOOGLE_API_KEY}&fields=files(id,name)"
    )
    response = requests.get(url, timeout=30)
    logger.info("drive.list.status_code=%s", response.status_code)

    if response.status_code != 200:
        return None

    file_cache["files"] = response.json().get("files", [])
    file_cache["timestamp"] = current_time
    logger.info("file_list.refreshed count=%s", len(file_cache["files"]))
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
                logger.info("temp.cleanup_stale path=%s", file_path)
        except Exception:
            logger.exception("temp.cleanup_stale_failed path=%s", file_path)


def build_temp_audio_path(original_name: str) -> Path:
    """Build a safe, unique temp path for downloaded audio."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
    stem = Path(safe_name).stem or "audio"
    suffix = Path(safe_name).suffix or ".mp3"
    unique_name = f"{stem}_{uuid4().hex}{suffix}"
    return AUDIO_TEMP_DIR / unique_name


def download_file_to_path(url: str, destination: Path) -> int:
    """Download audio file to local disk. Returns total bytes written."""
    total_bytes = 0
    logger.info("download.start url=%s destination=%s", url, destination)
    with requests.get(url, stream=True, allow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with open(destination, "wb") as output_file:
            for chunk in response.iter_content(chunk_size=1024 * 64):
                if not chunk:
                    continue
                output_file.write(chunk)
                total_bytes += len(chunk)
    logger.info("download.complete destination=%s bytes=%s", destination, total_bytes)
    return total_bytes


async def voice_diagnostics_loop(ctx: commands.Context):
    """Periodic voice + host metrics to help isolate latency/CPU/memory issues."""
    try:
        vc = ctx.voice_client
        if not vc:
            logger.info("voice.diag stopped reason=no_voice_client")
            return

        while vc.is_connected() and (vc.is_playing() or vc.is_paused()):

            py_cpu = PROCESS.cpu_percent(interval=None) if PROCESS else None
            sys_cpu = psutil.cpu_percent(interval=None) if psutil else None
            mem_pct = psutil.virtual_memory().percent if psutil else None

            logger.info(
                "voice.diag mode=%s connected=%s playing=%s paused=%s latency_ms=%.1f avg_latency_ms=%.1f channel=%s ffmpeg_pid=%s py_cpu_pct=%s sys_cpu_pct=%s mem_pct=%s",
                playback_state.get("mode"),
                vc.is_connected(),
                vc.is_playing(),
                vc.is_paused(),
                (vc.latency or 0.0) * 1000,
                (vc.average_latency or 0.0) * 1000,
                getattr(vc.channel, "name", None),
                playback_state.get("ffmpeg_pid"),
                f"{py_cpu:.1f}" if py_cpu is not None else "n/a",
                f"{sys_cpu:.1f}" if sys_cpu is not None else "n/a",
                f"{mem_pct:.1f}" if mem_pct is not None else "n/a",
            )

            await asyncio.sleep(5)

            # Watchdog: playback expected but no longer active.
            if playback_state.get("local_path") and not vc.is_playing() and not vc.is_paused():
                logger.warning("voice.watchdog playback_not_active path=%s", playback_state.get("local_path"))

        logger.info("voice.diag stopped reason=playback_inactive")
    except asyncio.CancelledError:
        logger.info("voice.diag cancelled")
        raise
    except Exception:
        logger.exception("voice.diag crashed")


def _capture_ffmpeg_process_info(source: discord.AudioSource):
    """Inspect discord.py FFmpeg source internals for diagnostics."""
    process = getattr(source, "_process", None)
    if process:
        playback_state["ffmpeg_pid"] = process.pid
        playback_state["ffmpeg_args"] = process.args
    logger.info(
        "ffmpeg.process pid=%s args=%s",
        playback_state.get("ffmpeg_pid"),
        playback_state.get("ffmpeg_args"),
    )


class MinimalFFmpegPCMAudio(discord.AudioSource):
    """Minimal PCM source that avoids custom blocksize tuning for conservative playback."""

    def __init__(
        self,
        source: str,
        *,
        executable: str = "ffmpeg",
        stderr=None,
        before_options: Optional[str] = None,
        options: Optional[str] = None,
    ):
        args = [executable, "-nostdin"]

        if before_options:
            args.extend(shlex.split(before_options))

        args.extend(["-i", source, "-f", "s16le", "-ar", "48000", "-ac", "2"])

        if options:
            option_tokens = shlex.split(options)

            # Guardrail: keep PCM path free of blocksize tuning to avoid stutter regressions.
            cleaned_tokens = []
            skip_next = False
            for token in option_tokens:
                if skip_next:
                    skip_next = False
                    continue
                if token == "-blocksize":
                    skip_next = True
                    continue
                cleaned_tokens.append(token)

            args.extend(cleaned_tokens)

        args.append("pipe:1")

        self._process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr)
        self._stdout = self._process.stdout

    def read(self) -> bytes:
        if not self._stdout:
            return b""

        ret = self._stdout.read(opus.Encoder.FRAME_SIZE)
        if len(ret) != opus.Encoder.FRAME_SIZE:
            return b""
        return ret

    def cleanup(self):
        proc = self._process
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()


async def _build_audio_source(local_path: Path, stderr_log_path: Path) -> discord.AudioSource:
    """Create FFmpeg source based on PLAYBACK_MODE for easy A/B testing."""
    stderr_handle = open(stderr_log_path, "ab")
    playback_state["ffmpeg_stderr_handle"] = stderr_handle
    playback_state["ffmpeg_stderr_path"] = str(stderr_log_path)

    before_options = "-hide_banner"
    base_options = "-vn -loglevel warning"

    # Keep Opus settings conservative on Pi to avoid over-aggressive encode parameters.
    if PLAYBACK_MODE == "opus":
        options = f"{base_options} -c:a libopus -b:a 96k"
        playback_state["ffmpeg_args"] = f"before_options={before_options} options={options}"
        source = discord.FFmpegOpusAudio(
            str(local_path),
            stderr=stderr_handle,
            before_options=before_options,
            options=options,
        )
        logger.info("ffmpeg.source created=FFmpegOpusAudio mode=opus path=%s options=%s", local_path, options)
        return source

    options = base_options
    playback_state["ffmpeg_args"] = f"before_options={before_options} options={options}"
    source = MinimalFFmpegPCMAudio(
        str(local_path),
        executable="ffmpeg",
        stderr=stderr_handle,
        before_options=before_options,
        options=options,
    )
    logger.info("ffmpeg.source created=FFmpegPCMAudio mode=pcm path=%s options=%s", local_path, options)
    return source


async def safe_cleanup(path: Optional[Path]):
    """Single-entry cleanup for file and stderr handle."""
    if playback_state.get("cleanup_started"):
        return
    playback_state["cleanup_started"] = True

    try:
        if path and path.exists():
            path.unlink(missing_ok=True)
            logger.info("temp.cleanup_complete path=%s", path)
    except Exception:
        logger.exception("temp.cleanup_failed path=%s", path)

    try:
        stderr_handle = playback_state.get("ffmpeg_stderr_handle")
        if stderr_handle:
            stderr_handle.close()
    except Exception:
        logger.exception("ffmpeg.stderr_close_failed path=%s", playback_state.get("ffmpeg_stderr_path"))

    playback_state.update(
        {
            "local_path": None,
            "display_name": None,
            "mode": PLAYBACK_MODE,
            "ffmpeg_pid": None,
            "ffmpeg_args": None,
            "ffmpeg_stderr_handle": None,
            "cleanup_started": False,
        }
    )


@bot.event
async def on_ready():
    logger.info("bot.startup user=%s playback_mode=%s", bot.user, PLAYBACK_MODE)


@bot.event
async def on_command(ctx: commands.Context):
    logger.info("command.received name=%s author=%s channel=%s", ctx.command, ctx.author, ctx.channel)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    logger.exception("command.error name=%s", getattr(ctx.command, "name", None), exc_info=error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"You're using commands too fast! Try again in {int(error.retry_after)} seconds.")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return
    logger.info(
        "voice.state_update before=%s after=%s",
        getattr(before.channel, "name", None),
        getattr(after.channel, "name", None),
    )


@bot.command()
async def join(ctx):
    """Joins the voice channel the user is in."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client and ctx.voice_client.channel != channel:
            logger.info("voice.move from=%s to=%s", ctx.voice_client.channel, channel)
            await ctx.voice_client.move_to(channel)
        elif not ctx.voice_client:
            logger.info("voice.join channel=%s", channel)
            await channel.connect(reconnect=True)
        else:
            logger.info("voice.join already_in_channel channel=%s", channel)
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
async def voiceinfo(ctx):
    """Shows live voice connection diagnostics for troubleshooting."""
    vc = ctx.voice_client
    if not vc:
        await ctx.send("Not connected to a voice channel.")
        return

    message = (
        f"Channel: {getattr(vc.channel, 'name', 'unknown')}\n"
        f"Connected: {vc.is_connected()}\n"
        f"Playing: {vc.is_playing()}\n"
        f"Paused: {vc.is_paused()}\n"
        f"Latency: {(vc.latency or 0.0) * 1000:.1f} ms\n"
        f"Average latency: {(vc.average_latency or 0.0) * 1000:.1f} ms\n"
        f"Playback mode: {playback_state.get('mode')}\n"
        f"Local file: {playback_state.get('local_path')}\n"
        f"FFmpeg PID: {playback_state.get('ffmpeg_pid')}"
    )
    logger.info("voiceinfo.requested %s", message.replace("\n", " | "))
    await ctx.send(f"```{message}```")


@bot.command()
async def play(ctx, *, filename: str):
    """Plays an MP3 file from Google Drive, allowing underscores instead of spaces."""
    playback_state["command_start_monotonic"] = time.monotonic()

    if not ctx.voice_client:
        await ctx.invoke(join)

    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        await ctx.send("Could not connect to voice channel.")
        logger.error("voice.connection_failed")
        return

    if vc.is_playing():
        logger.info("voice.stop_previous reason=new_play_command")
        vc.stop()

    diag_task = playback_state.get("diag_task")
    if diag_task and not diag_task.done():
        diag_task.cancel()

    await asyncio.to_thread(cleanup_old_temp_files)

    files = await asyncio.to_thread(get_drive_files)
    if not files:
        await ctx.send("No MP3 files found or unable to fetch files.")
        return

    normalized_filename = filename.replace("_", " ").strip(' "\'').lower()
    logger.info("play.request normalized=%s", normalized_filename)

    file_data = next((f for f in files if f["name"].lower() == normalized_filename), None)
    if not file_data:
        await ctx.send("File not found! Use `!list` to see available files.")
        return

    file_id = file_data["id"]
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    temp_audio_path = build_temp_audio_path(file_data["name"])
    stderr_path = temp_audio_path.with_suffix(temp_audio_path.suffix + ".ffmpeg.log")

    playback_state.update(
        {
            "local_path": str(temp_audio_path),
            "display_name": file_data["name"],
            "mode": PLAYBACK_MODE,
            "ffmpeg_pid": None,
            "ffmpeg_args": None,
            "cleanup_started": False,
        }
    )

    try:
        start_dl = time.monotonic()
        await asyncio.to_thread(download_file_to_path, url, temp_audio_path)
        playback_state["download_seconds"] = time.monotonic() - start_dl
        logger.info("download.timing seconds=%.3f", playback_state["download_seconds"])

        if not temp_audio_path.exists() or temp_audio_path.stat().st_size == 0:
            raise RuntimeError(f"Downloaded file missing/empty: {temp_audio_path}")

        logger.info("playback.local_file path=%s", temp_audio_path)
        audio_source = await _build_audio_source(temp_audio_path, stderr_path)
        _capture_ffmpeg_process_info(audio_source)

        def after_playback(error):
            logger.info("playback.after_callback file=%s mode=%s error=%s", file_data["name"], PLAYBACK_MODE, error)
            if error:
                logger.exception("playback.after_error file=%s", file_data["name"], exc_info=error)

            diag_task_local = playback_state.get("diag_task")
            if diag_task_local and not diag_task_local.done():
                diag_task_local.cancel()

            bot.loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_cleanup(temp_audio_path)))

        vc.play(audio_source, after=after_playback)
        playback_state["diag_task"] = asyncio.create_task(voice_diagnostics_loop(ctx))

        total_to_start = time.monotonic() - playback_state["command_start_monotonic"]
        logger.info(
            "playback.start file=%s mode=%s command_to_start_seconds=%.3f",
            file_data["name"],
            PLAYBACK_MODE,
            total_to_start,
        )
        await ctx.send(f"Now playing: {file_data['name']}")

    except requests.exceptions.RequestException as e:
        logger.exception("download.error file=%s", file_data["name"])
        await ctx.send(f"Error downloading file: {e}")
        await safe_cleanup(temp_audio_path)
    except Exception:
        logger.exception("playback.flow_error file=%s", file_data["name"])
        await ctx.send("Unexpected error while preparing playback.")
        await safe_cleanup(temp_audio_path)


@bot.command()
async def leave(ctx):
    """Disconnects bot from voice channel."""
    if ctx.voice_client:
        logger.info("voice.disconnect channel=%s", ctx.voice_client.channel)
        await ctx.voice_client.disconnect(force=False)
        diag_task = playback_state.get("diag_task")
        if diag_task and not diag_task.done():
            diag_task.cancel()
        await ctx.send("Bot has left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel.")


# Load bot token from environment variables
token = os.getenv("TOKEN")
if not token:
    raise Exception("Please add your token to the environment variables.")

bot.run(token, log_handler=None)
