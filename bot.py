"""
KeoBot - Discord assistant for Keo's Minecraft community.
Uses NVIDIA API with Kimi-K2.6 model for AI responses.
"""

import asyncio
import json
import os
import logging
import subprocess
from collections import defaultdict
from pathlib import Path

import discord
import requests
from discord.ext import commands
from dotenv import load_dotenv

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
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
if not NVIDIA_API_KEY:
    NVIDIA_API_KEY = "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"

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
if not TAVILY_API_KEY:
    TAVILY_API_KEY = "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"

ALLOWED_CHANNELS_RAW = os.getenv("ALLOWED_CHANNELS", "")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")

# Parse allowed channels (empty = respond everywhere when mentioned)
ALLOWED_CHANNELS: set[int] = set()
if ALLOWED_CHANNELS_RAW.strip():
    for ch_id in ALLOWED_CHANNELS_RAW.split(","):
        ch_id = ch_id.strip()
        if ch_id.isdigit():
            ALLOWED_CHANNELS.add(int(ch_id))

# ── NVIDIA Client / Models ───────────────────────────────────
NVIDIA_MODEL = "moonshotai/kimi-k2.6"
last_used_model = "Ninguno"

# ── Conversation Memory ─────────────────────────────────────
# Stores recent messages per channel for context (max N turns)
MAX_HISTORY = 10

# Dict[channel_id, list of dicts]
channel_history: dict[int, list[dict]] = defaultdict(list)


def build_contents(channel_id: int, user_message: str) -> list[dict]:
    """Build the conversation contents list including history."""
    history = channel_history[channel_id]

    # Add user message to history
    user_content = {
        "role": "user",
        "content": user_message,
    }
    history.append(user_content)

    # Trim history if too long
    if len(history) > MAX_HISTORY * 2:  # *2 because user+model pairs
        channel_history[channel_id] = history[-(MAX_HISTORY * 2) :]
        history = channel_history[channel_id]

    return list(history)


def save_assistant_response(channel_id: int, response_text: str):
    """Save assistant response to history."""
    model_content = {
        "role": "assistant",
        "content": response_text,
    }
    channel_history[channel_id].append(model_content)


def search_tavily(query: str) -> str:
    """Search the web using Tavily API and format results for the LLM."""
    try:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "include_answer": True,
            "max_results": 3
        }
        log.info("Querying Tavily search: '%s'", query)
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            return "No se encontraron resultados en internet."
            
        formatted = []
        if data.get("answer"):
            formatted.append(f"Resumen de la búsqueda: {data['answer']}\n")
        
        formatted.append("Resultados web:")
        for idx, item in enumerate(results, 1):
            formatted.append(f"{idx}. {item['title']}\n   URL: {item['url']}\n   Contenido: {item['content']}")
            
        return "\n".join(formatted)
    except Exception as e:
        log.error("Tavily search failed: %s", e)
        return f"Error al buscar en internet: {e}"


async def ask_nvidia(channel_id: int, user_message: str) -> str:
    """Send a message to NVIDIA API (moonshotai/kimi-k2.6) with optional Tavily web search."""
    global last_used_model
    contents = build_contents(channel_id, user_message)

    def _call_nvidia(messages, tools=None):
        invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        payload = {
            "model": NVIDIA_MODEL,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            
        log.info("Sending request to NVIDIA API (model: %s)...", NVIDIA_MODEL)
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()

    # Define tools for function calling (Tavily search)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_tavily",
                "description": "Busca en internet información en tiempo real sobre Minecraft, mods, launchers, tutoriales, videos, noticias o dudas técnicas de los usuarios.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "La consulta de búsqueda detallada a realizar en español"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]

    try:
        system_prompt = get_system_prompt()
        messages = [{"role": "system", "content": system_prompt}] + contents

        # Try requesting with function calling enabled
        try:
            res_json = await asyncio.to_thread(_call_nvidia, messages, tools)
        except Exception as tool_err:
            log.warning("NVIDIA tool calling failed: %s. Falling back to direct text generation.", tool_err)
            res_json = await asyncio.to_thread(_call_nvidia, messages, None)

        choice = res_json["choices"][0]
        message = choice["message"]

        # If model wants to call the tool
        if message.get("tool_calls"):
            tool_calls = message["tool_calls"]
            messages.append(message)  # Append assistant message with tool call request

            for tool_call in tool_calls:
                if tool_call["function"]["name"] == "search_tavily":
                    args = json.loads(tool_call["function"]["arguments"])
                    query = args.get("query", "")
                    
                    # Run the search
                    search_result = await asyncio.to_thread(search_tavily, query)
                    
                    # Append tool response message
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": "search_tavily",
                        "content": search_result
                    }
                    messages.append(tool_message)
            
            # Request final answer from model using search context
            log.info("Sending search results back to NVIDIA API for final response...")
            res_json = await asyncio.to_thread(_call_nvidia, messages, None)
            choice = res_json["choices"][0]
            message = choice["message"]

        log.info("Response received successfully from NVIDIA model: %s", NVIDIA_MODEL)
        last_used_model = NVIDIA_MODEL

        reply = message.get("content") or "Hmm, no pude generar una respuesta bro 🤔"
        save_assistant_response(channel_id, reply)
        return reply

    except Exception as e:
        log.error("NVIDIA API failed: %s", e)
        return "Uf, el modelo de IA no está disponible ahorita. Intenta en un ratito ⚠️"


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
        response = await ask_nvidia(message.channel.id, user_context)

    # Split long responses (Discord limit is 2000 chars)
    if len(response) <= 2000:
        await message.reply(response, mention_author=True)
    else:
        # Split into chunks at line breaks
        chunks = split_response(response)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=True)
            else:
                await message.channel.send(chunk)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    """Handle command errors and notify users."""
    if isinstance(error, commands.MissingRole):
        await ctx.send(f"❌ No tienes permiso para usar este comando. Se requiere el rol: **{error.missing_role}**")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Falta un argumento obligatorio: {error.param.name}")
    else:
        log.error("Command error: %s", error)
        await ctx.send(f"❌ Ocurrió un error al ejecutar el comando: {error}")


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
            "`!keo videos` - Ver videos analizados de Keo\n"
            "`!keo model` - Ver el modelo de IA activo"
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


@bot.command(name="model")
async def model_command(ctx: commands.Context):
    """Show the currently active AI model."""
    await ctx.send(f"🤖 El modelo de IA configurado es: **{NVIDIA_MODEL}**")


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
async def analyze_command(ctx: commands.Context, param: str = "5"):
    """Trigger video analysis. Usage: !keo analyze [max_videos | video_url]"""
    is_url = param.strip().startswith("http")

    if is_url:
        await ctx.send(
            "📺 Analizando el video de TikTok proporcionado...\n"
            "Esto puede tardar un par de minutos. Te aviso cuando termine."
        )
        args = ["video_analyzer.py", "--url", param.strip()]
    else:
        try:
            max_videos = int(param)
        except ValueError:
            max_videos = 5
        await ctx.send(
            f"📺 Analizando los últimos **{max_videos}** videos de Keo...\n"
            "Esto puede tardar unos minutos. Te aviso cuando termine."
        )
        args = ["video_analyzer.py", "--max", str(max_videos)]

    log.info("Starting video analyzer subprocess: python %s", " ".join(args))
    await ctx.send("⚙️ Ejecutando script de análisis en el servidor...")

    try:
        # Run the analyzer script as a subprocess
        process = await asyncio.create_subprocess_exec(
            "python", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)

        log.info("Subprocess finished with return code: %d", process.returncode)
        
        stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

        if stdout_str:
            log.info("Analyzer stdout:\n%s", stdout_str)
        if stderr_str:
            log.warning("Analyzer stderr:\n%s", stderr_str)

        if process.returncode == 0:
            if is_url:
                await ctx.send(
                    "✅ ¡Análisis del video completado con éxito! Se ha agregado a mi base de conocimiento. 🧠"
                )
            else:
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
            error_msg = stderr_str if stderr_str else "Error desconocido"
            await ctx.send(
                f"❌ Error durante el análisis (Código de salida: {process.returncode}):\n"
                f"```\n{error_msg[:1800]}\n```"
            )

    except asyncio.TimeoutError:
        log.error("Video analyzer subprocess timed out.")
        await ctx.send("⏰ El análisis tardó demasiado (límite de 10 minutos) y fue cancelado.")
    except Exception as e:
        log.error("Failed to run video analyzer: %s", e)
        await ctx.send(f"❌ Error al ejecutar el script de análisis: {e}")


@bot.command(name="status")
async def status_command(ctx: commands.Context):
    """Show bot status."""
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
    embed.add_field(name="Modelo IA", value=last_used_model, inline=True)
    embed.add_field(name="Videos analizados", value=str(video_count), inline=True)
    await ctx.send(embed=embed)


# ── Entry Point ──────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting KeoBot...")
    bot.run(DISCORD_TOKEN, log_handler=None)
