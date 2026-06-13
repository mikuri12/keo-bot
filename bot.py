"""
KeoBot - Discord assistant for Keo's Minecraft community.
Uses Gemini API for AI responses with a curated system prompt.
"""

import asyncio
import json
import os
import logging
import subprocess
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

from system_prompt import get_system_prompt

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("keobot")

# ── Environment ──────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALLOWED_CHANNELS_RAW = os.getenv("ALLOWED_CHANNELS", "")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Check your .env file.")

# Parse allowed channels (empty = respond everywhere when mentioned)
ALLOWED_CHANNELS: set[int] = set()
if ALLOWED_CHANNELS_RAW.strip():
    for ch_id in ALLOWED_CHANNELS_RAW.split(","):
        ch_id = ch_id.strip()
        if ch_id.isdigit():
            ALLOWED_CHANNELS.add(int(ch_id))

# ── Gemini Client ────────────────────────────────────────────
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Model to use (free tier compatible)
GEMINI_MODEL = "gemini-2.5-flash"

# ── Conversation Memory ─────────────────────────────────────
# Stores recent messages per channel for context (max N turns)
MAX_HISTORY = 10

# Dict[channel_id, list of Content objects]
channel_history: dict[int, list] = defaultdict(list)


def build_contents(channel_id: int, user_message: str) -> list:
    """Build the conversation contents list for Gemini, including history."""
    history = channel_history[channel_id]

    # Add user message to history
    user_content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=user_message)],
    )
    history.append(user_content)

    # Trim history if too long
    if len(history) > MAX_HISTORY * 2:  # *2 because user+model pairs
        channel_history[channel_id] = history[-(MAX_HISTORY * 2) :]
        history = channel_history[channel_id]

    return list(history)


def save_assistant_response(channel_id: int, response_text: str):
    """Save assistant response to history."""
    model_content = types.Content(
        role="model",
        parts=[types.Part.from_text(text=response_text)],
    )
    channel_history[channel_id].append(model_content)


async def ask_gemini(channel_id: int, user_message: str) -> str:
    """Send a message to Gemini and return the response."""
    try:
        contents = build_contents(channel_id, user_message)

        # Run sync Gemini call in a thread to not block the event loop
        def _call_gemini():
            return gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=get_system_prompt(),
                    temperature=0.7,
                    max_output_tokens=1024,
                    top_p=0.9,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )

        response = await asyncio.to_thread(_call_gemini)

        # Log if search grounding was used
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            chunks = getattr(gm, 'grounding_chunks', None)
            if chunks:
                log.info("Search grounding used: %d sources", len(chunks))
            else:
                log.info("No search grounding in response")
        else:
            log.info("No grounding metadata in response")

        reply = response.text or "Hmm, no pude generar una respuesta. Intenta de nuevo bro 🤔"
        save_assistant_response(channel_id, reply)
        return reply

    except Exception as e:
        log.error("Gemini API error: %s", e, exc_info=True)
        return f"Ups, tuve un problema conectándome. Intenta de nuevo en un momento ⚠️"


# ── Discord Bot ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!keo ",
    intents=intents,
    help_command=None,
)


@bot.event
async def on_ready():
    log.info("Bot connected as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Serving %d guild(s)", len(bot.guilds))
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing,
            name="Minecraft ⛏️ | @me para preguntar",
        )
    )


@bot.event
async def on_message(message: discord.Message):
    # Ignore self and other bots
    if message.author.bot:
        return

    # Process commands first (e.g., !keo ping, !keo help)
    if message.content.startswith("!keo "):
        await bot.process_commands(message)
        return

    # Check if the bot should respond to AI queries
    should_respond = False

    # Respond when mentioned
    if bot.user in message.mentions:
        should_respond = True

    # Respond to DMs
    if isinstance(message.channel, discord.DMChannel):
        should_respond = True

    if not should_respond:
        return

    # Check allowed channels (skip check for DMs)
    if ALLOWED_CHANNELS and not isinstance(message.channel, discord.DMChannel):
        if message.channel.id not in ALLOWED_CHANNELS:
            return

    # Clean the message (remove bot mention)
    clean_content = message.content
    if bot.user:
        clean_content = clean_content.replace(f"<@{bot.user.id}>", "").strip()
    clean_content = clean_content.removeprefix("!keo ").strip()

    if not clean_content:
        await message.reply(
            "¡Hola! Soy **KeoBot** 🤖⛏️\n"
            "Pregúntame lo que quieras sobre Minecraft o el server de Keo.\n"
            "Ejemplo: `@KeoBot cómo instalo mods en PrismLauncher?`"
        )
        return

    # Add context about the user
    user_context = f"[Mensaje de {message.author.display_name}]: {clean_content}"

    # Show typing indicator while processing
    async with message.channel.typing():
        response = await ask_gemini(message.channel.id, user_context)

    # Split long responses (Discord limit is 2000 chars)
    if len(response) <= 2000:
        await message.reply(response, mention_author=False)
    else:
        # Split into chunks at line breaks
        chunks = split_response(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)


def split_response(text: str, max_len: int = 1900) -> list[str]:
    """Split a long response into Discord-friendly chunks."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current:
                chunks.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks


# ── Slash Commands / Prefix Commands ────────────────────────


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Show help information."""
    embed = discord.Embed(
        title="🤖 KeoBot - Ayuda",
        description="Soy el asistente de la comunidad de Keo. ¡Pregúntame lo que quieras sobre Minecraft!",
        color=0x55FF55,
    )
    embed.add_field(
        name="💬 Cómo usarme",
        value=(
            "• **Mencióname**: @KeoBot tu pregunta\n"
            "• **Comando**: `!keo help`\n"
            "• **DM**: Envíame un mensaje directo"
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 Cosas que puedo hacer",
        value=(
            "• Responder dudas sobre Minecraft\n"
            "• Ayudarte con mods y launchers\n"
            "• Info sobre el server de Keo\n"
            "• Tips y trucos de Minecraft\n"
            "• Resolver problemas técnicos"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Comandos",
        value=(
            "`!keo help` - Este mensaje\n"
            "`!keo clear` - Limpiar historial de conversación\n"
            "`!keo ping` - Verificar que estoy vivo\n"
            "`!keo videos` - Ver videos analizados de Keo"
        ),
        inline=False,
    )
    embed.set_footer(text="Hecho con ❤️ para la comunidad de Keo")
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping_command(ctx: commands.Context):
    """Check bot latency."""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latencia: **{latency}ms**")


@bot.command(name="clear")
async def clear_command(ctx: commands.Context):
    """Clear conversation history for this channel."""
    channel_history[ctx.channel.id] = []
    await ctx.send("🧹 Historial de conversación limpiado para este canal.")


@bot.command(name="videos")
async def videos_command(ctx: commands.Context):
    """Show analyzed video count."""
    knowledge_file = Path(__file__).parent / "video_knowledge.json"
    if knowledge_file.exists():
        data = json.loads(knowledge_file.read_text(encoding="utf-8"))
        count = len(data.get("videos", {}))
        last = data.get("last_updated", "nunca")
        await ctx.send(
            f"📺 Tengo **{count}** videos de Keo analizados.\n"
            f"Última actualización: `{last}`"
        )
    else:
        await ctx.send("📺 Aún no se han analizado videos de Keo.")


@bot.command(name="analyze")
@commands.has_role(ADMIN_ROLE)
async def analyze_command(ctx: commands.Context, max_videos: int = 5):
    """Trigger video analysis (admin only). Usage: !keo analyze [max_videos]"""
    await ctx.send(
        f"📺 Analizando los últimos **{max_videos}** videos de Keo...\n"
        "Esto puede tardar unos minutos. Te aviso cuando termine."
    )

    try:
        # Run the analyzer script as a subprocess
        process = await asyncio.create_subprocess_exec(
            "python", "video_analyzer.py", "--max", str(max_videos),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)

        if process.returncode == 0:
            # Count analyzed videos
            knowledge_file = Path(__file__).parent / "video_knowledge.json"
            count = 0
            if knowledge_file.exists():
                data = json.loads(knowledge_file.read_text(encoding="utf-8"))
                count = len(data.get("videos", {}))

            await ctx.send(
                f"✅ ¡Análisis completado! Ahora tengo **{count}** videos en mi base de conocimiento.\n"
                "Ya puedo responder preguntas sobre el contenido de Keo 🧠"
            )
        else:
            error_msg = stderr.decode()[:500] if stderr else "Error desconocido"
            await ctx.send(f"❌ Error durante el análisis:\n```\n{error_msg}\n```")

    except asyncio.TimeoutError:
        await ctx.send("⏰ El análisis tardó demasiado y fue cancelado.")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")


@bot.command(name="status")
@commands.has_role(ADMIN_ROLE)
async def status_command(ctx: commands.Context):
    """Show bot status (admin only)."""
    total_history = sum(len(h) for h in channel_history.values())

    # Count analyzed videos
    knowledge_file = Path(__file__).parent / "video_knowledge.json"
    video_count = 0
    if knowledge_file.exists():
        try:
            data = json.loads(knowledge_file.read_text(encoding="utf-8"))
            video_count = len(data.get("videos", {}))
        except Exception:
            pass

    embed = discord.Embed(
        title="📊 Estado de KeoBot",
        color=0x5555FF,
    )
    embed.add_field(name="Servidores", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latencia", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Mensajes en memoria", value=str(total_history), inline=True)
    embed.add_field(name="Canales activos", value=str(len(channel_history)), inline=True)
    embed.add_field(name="Modelo IA", value=GEMINI_MODEL, inline=True)
    embed.add_field(name="Videos analizados", value=str(video_count), inline=True)
    await ctx.send(embed=embed)


# ── Entry Point ──────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting KeoBot...")
    bot.run(DISCORD_TOKEN, log_handler=None)
