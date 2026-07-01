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
import time

import re

import discord
import requests
from discord.ext import commands, tasks
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
ALLOWED_BOT_IDS_RAW = os.getenv("ALLOWED_BOT_IDS", "")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Check your .env file.")

# Parse allowed channels (empty = respond everywhere when mentioned)
ALLOWED_CHANNELS: set[int] = set()
if ALLOWED_CHANNELS_RAW.strip():
    for ch_id in ALLOWED_CHANNELS_RAW.split(","):
        ch_id = ch_id.strip()
        if ch_id.isdigit():
            ALLOWED_CHANNELS.add(int(ch_id))

# Parse allowed bot IDs
ALLOWED_BOT_IDS: set[int] = set()
if ALLOWED_BOT_IDS_RAW.strip():
    for bot_id in ALLOWED_BOT_IDS_RAW.split(","):
        bot_id = bot_id.strip()
        if bot_id.isdigit():
            ALLOWED_BOT_IDS.add(int(bot_id))

# ── NVIDIA Client / Models ───────────────────────────────────
NVIDIA_MODELS = [
    "z-ai/glm-5.1",
    "moonshotai/kimi-k2.6",
    "meta/llama-3.3-70b-instruct",
    "deepseek-ai/deepseek-v3",
]
last_used_model = "Ninguno"

# ── Usage Statistics ─────────────────────────────────────────
total_prompt_tokens = 0
total_completion_tokens = 0
total_requests = 0
request_history: list[float] = []      # Timestamps of requests in the last 60s
token_history: list[tuple[float, int]] = []  # (timestamp, tokens) in the last 60s


def record_api_usage(prompt_tok: int, comp_tok: int):
    global total_prompt_tokens, total_completion_tokens, total_requests
    now = time.time()
    total_requests += 1
    total_prompt_tokens += prompt_tok
    total_completion_tokens += comp_tok
    request_history.append(now)
    token_history.append((now, prompt_tok + comp_tok))

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


def search_reddit(query: str) -> str:
    """Search Reddit's public JSON API for threads (no auth needed)."""
    try:
        url = "https://www.reddit.com/search.json"
        params = {"q": query, "limit": 5, "sort": "relevance", "t": "all"}
        headers = {"User-Agent": "KeoBot/1.0 (Minecraft Discord assistant)"}
        log.info("Querying Reddit search: '%s'", query)
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        posts = data.get("data", {}).get("children", [])
        if not posts:
            return "No se encontraron hilos de Reddit para esa búsqueda."

        formatted = ["Hilos de Reddit encontrados:"]
        for idx, post in enumerate(posts, 1):
            p = post.get("data", {})
            title = p.get("title", "Sin título")
            subreddit = p.get("subreddit_name_prefixed", "")
            permalink = p.get("permalink", "")
            selftext = (p.get("selftext", "") or "")[:400]
            score = p.get("score", 0)
            link = f"https://www.reddit.com{permalink}"
            formatted.append(
                f"{idx}. [{subreddit}] {title} (score: {score})\n"
                f"   URL: {link}\n"
                f"   {selftext}".rstrip()
            )
        return "\n".join(formatted)
    except Exception as e:
        log.error("Reddit search failed: %s", e)
        return f"Error al buscar en Reddit: {e}"


def fetch_url(url: str) -> str:
    """Fetch a web page and return cleaned text content (max ~4000 chars)."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        headers = {"User-Agent": "Mozilla/5.0 (KeoBot; Minecraft Discord assistant)"}
        log.info("Fetching URL: %s", url)
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        text = response.text
        # Quitar scripts/estilos y tags para dejar texto legible
        text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        # Decodificar entidades HTML comunes
        import html as _html
        text = _html.unescape(text)
        # Colapsar espacios en blanco
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

        if not text:
            return "La página no devolvió contenido de texto legible."
        return f"Contenido de {url}:\n{text[:4000]}"
    except Exception as e:
        log.error("Fetch URL failed: %s", e)
        return f"Error al leer la página: {e}"


def get_tiktok_info(url: str) -> str:
    """Get metadata (title, description) of a TikTok/video URL via yt-dlp."""
    try:
        log.info("Fetching TikTok/video info: %s", url)
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", "--skip-download", url],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return f"No se pudo obtener info del video: {result.stderr[:200]}"

        info = json.loads(result.stdout)
        title = info.get("title", "Sin título")
        desc = (info.get("description", "") or "")[:800]
        uploader = info.get("uploader", info.get("channel", "?"))
        duration = info.get("duration", 0)
        return (
            f"Info del video:\n"
            f"- Autor: {uploader}\n"
            f"- Título: {title}\n"
            f"- Duración: {duration}s\n"
            f"- Descripción: {desc}"
        )
    except FileNotFoundError:
        return "yt-dlp no está instalado en el servidor."
    except subprocess.TimeoutExpired:
        return "Tiempo agotado obteniendo info del video."
    except Exception as e:
        log.error("TikTok info failed: %s", e)
        return f"Error al obtener info del video: {e}"


# Mapa nombre -> función para el dispatcher de tool calls
TOOL_FUNCTIONS = {
    "search_tavily": lambda a: search_tavily(a.get("query", "")),
    "search_reddit": lambda a: search_reddit(a.get("query", "")),
    "fetch_url": lambda a: fetch_url(a.get("url", "")),
    "get_tiktok_info": lambda a: get_tiktok_info(a.get("url", "")),
}


def _strip_tool_tags(text: str) -> str:
    """Elimina etiquetas de tool call en texto crudo que algunos modelos filtran.

    Red de seguridad final: si el modelo emitió etiquetas <|tool_call...|> o
    bloques 'functions.<nombre>' y no se pudieron procesar, no deben llegar
    nunca al chat del usuario.
    """
    if not text:
        return text
    # Quitar secciones completas de tool call
    text = re.sub(r"<\|tool_calls?_section_begin\|>.*?(?:<\|tool_calls?_section_end\|>|$)", "", text, flags=re.DOTALL)
    text = re.sub(r"<\|tool_call_begin\|>.*?(?:<\|tool_call_end\|>|$)", "", text, flags=re.DOTALL)
    # Quitar cualquier etiqueta <|...|> residual
    text = re.sub(r"<\|[^|]*\|>", "", text)
    # Quitar restos tipo 'functions.search_tavily:6 {"query": ...}'
    text = re.sub(r"functions\.\w+(?::\d+)?\s*(\{.*\})?", "", text, flags=re.DOTALL)
    return text.strip()


async def ask_nvidia(channel_id: int, user_message: str) -> str:
    """Send a message to NVIDIA API with fallback models and optional Tavily search."""
    global last_used_model
    contents = build_contents(channel_id, user_message)

    # Herramientas de function calling disponibles para el modelo
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_tavily",
                "description": "Busca en internet información en tiempo real sobre Minecraft: mods, launchers, shaders, tutoriales, versiones, noticias o dudas técnicas. Úsalo cuando no tengas la información con certeza.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "La consulta de búsqueda detallada en español"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_reddit",
                "description": "Busca hilos en Reddit. Útil para errores técnicos complejos de Minecraft (crashes de Java, conflictos de mods, shaders, optimización) donde la comunidad ya publicó soluciones. Devuelve títulos, enlaces y extractos.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Términos de búsqueda, preferiblemente en inglés para mejores resultados técnicos (ej: 'minecraft fabric sodium crash exit code 1')"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Lee el contenido de texto de una página web concreta (documentación, hilo de Reddit, wiki, página de un mod en Modrinth/CurseForge). Úsalo para leer en detalle un enlace que ya conoces o que apareció en una búsqueda.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "La URL completa a leer"
                        }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_tiktok_info",
                "description": "Obtiene metadatos (autor, título, descripción, duración) de un enlace de video de TikTok/YouTube. Úsalo cuando un usuario comparte un enlace de video y pregunta de qué trata.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "La URL del video de TikTok o YouTube"
                        }
                    },
                    "required": ["url"]
                }
            }
        }
    ]

    def _call_nvidia(model_name, messages, use_tools=False):
        invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": False,
        }
        if use_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        log.info("Sending request to NVIDIA API (model: %s, tools: %s)...", model_name, use_tools)
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=40)
        response.raise_for_status()
        return response.json()

    def _account_usage(res_json):
        if "usage" in res_json:
            usage = res_json["usage"]
            record_api_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    async def _run_tool(name: str, args: dict) -> str:
        func = TOOL_FUNCTIONS.get(name)
        if not func:
            return f"Herramienta '{name}' no disponible."
        return await asyncio.to_thread(func, args)

    MAX_TOOL_ROUNDS = 4  # Evita loops infinitos de tool calls

    last_error = None
    for model_name in NVIDIA_MODELS:
        try:
            system_prompt = get_system_prompt()
            messages = [{"role": "system", "content": system_prompt}] + contents

            reply = ""
            for round_idx in range(MAX_TOOL_ROUNDS):
                use_tools = True
                try:
                    res_json = await asyncio.to_thread(_call_nvidia, model_name, messages, True)
                except Exception as tool_err:
                    log.warning("Tool calling failed for model %s: %s. Trying without tools.", model_name, tool_err)
                    res_json = await asyncio.to_thread(_call_nvidia, model_name, messages, False)
                    use_tools = False

                _account_usage(res_json)
                message = res_json["choices"][0]["message"]

                # Tool calls estándar (OpenAI-style)
                if use_tools and message.get("tool_calls"):
                    messages.append(message)
                    for tool_call in message["tool_calls"]:
                        fname = tool_call["function"]["name"]
                        try:
                            fargs = json.loads(tool_call["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            fargs = {}
                        log.info("Model requested tool '%s' args=%s", fname, fargs)
                        result = await _run_tool(fname, fargs)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": fname,
                            "content": result,
                        })
                    continue  # otra ronda: el modelo procesa los resultados

                reply = message.get("content") or ""

                # Fallback: algunos modelos (GLM) emiten los tool calls como
                # texto crudo con etiquetas <|...|>, y a veces con JSON malformado
                # (comillas sin escapar). Detectamos ampliamente y extraemos el
                # argumento aunque el JSON no parsee.
                if "<|tool_call" in reply or "tool_calls_section" in reply or "functions." in reply:
                    # Nombre de la función: functions.<nombre>[:id]
                    name_match = re.search(r"functions\.(\w+)", reply)
                    fname = name_match.group(1) if name_match else "search_tavily"
                    if fname not in TOOL_FUNCTIONS:
                        fname = "search_tavily"

                    # Bloque de argumentos tras la etiqueta (o todo el texto)
                    arg_match = re.search(
                        r"<\|tool_call_argument_begin\|>\s*(\{.*)", reply, re.DOTALL
                    )
                    raw_args = arg_match.group(1) if arg_match else reply
                    raw_args = re.split(r"<\|tool_call", raw_args)[0].strip()

                    fargs = {}
                    try:
                        fargs = json.loads(raw_args)
                    except Exception:
                        # JSON roto: extraer el valor de query/url a mano.
                        val_match = re.search(
                            r'"(?:query|url)"\s*:\s*"(.+?)"\s*\}?\s*$',
                            raw_args,
                            re.DOTALL,
                        )
                        if val_match:
                            key = "url" if fname in ("fetch_url", "get_tiktok_info") else "query"
                            fargs = {key: val_match.group(1).strip()}

                    if fargs:
                        log.info("Parsed GLM raw tool call '%s' args=%s", fname, fargs)
                        result = await _run_tool(fname, fargs)
                        messages.append({"role": "assistant", "content": reply})
                        messages.append({
                            "role": "user",
                            "content": f"[Resultado de la herramienta '{fname}']:\n{result}\n\nResponde al usuario en lenguaje natural usando esta información. NO incluyas etiquetas de tool call.",
                        })
                        reply = ""
                        continue  # otra ronda con el resultado inyectado
                    else:
                        log.warning("No se pudo extraer args del tool call crudo; se limpiará el texto.")

                break  # respuesta final sin más tool calls

            # Red de seguridad: nunca dejar pasar etiquetas de tool call al chat.
            reply = _strip_tool_tags(reply)

            if reply:
                log.info("Response received successfully from NVIDIA model: %s", model_name)
                last_used_model = model_name
                save_assistant_response(channel_id, reply)
                return reply

        except Exception as e:
            log.error("Model %s failed: %s", model_name, e)
            last_error = e
            continue

    # If all models fail
    log.error("All fallback models failed. Last error: %s", last_error)
    return "Uf, los modelos de IA no están disponibles ahorita. Intenta en un ratito ⚠️"


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
    # Arrancar el reset periódico (solo una vez)
    if not periodic_reset.is_running():
        periodic_reset.start()


@tasks.loop(hours=2)
async def periodic_reset():
    """Cada 2h: limpia el historial de conversación y re-analiza los videos de Keo.

    El historial en memoria acumula contexto viejo mezclado entre temas y
    usuarios, lo que degrada la coherencia con el tiempo. Reiniciarlo mantiene
    las respuestas frescas. El conocimiento de videos se recarga solo del JSON
    en cada mensaje (get_system_prompt), así que aquí solo hay que actualizar
    ese JSON re-corriendo el analizador.
    """
    channel_history.clear()
    log.info("Historial de conversación reseteado (ciclo de 2h)")

    try:
        proc = await asyncio.create_subprocess_exec(
            "python", "video_analyzer.py", "--max", "5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode == 0:
            log.info("Re-análisis de videos completado (ciclo de 2h)")
        else:
            err = stderr.decode("utf-8", errors="replace")[:500] if stderr else "?"
            log.warning("Re-análisis de videos falló (rc=%s): %s", proc.returncode, err)
    except asyncio.TimeoutError:
        log.error("Re-análisis de videos superó el tiempo límite (10 min)")
    except Exception as e:
        log.error("Fallo al re-analizar videos en el ciclo de 2h: %s", e)


@periodic_reset.before_loop
async def _before_periodic_reset():
    await bot.wait_until_ready()


@bot.event
async def on_message(message: discord.Message):
    # Ignore self
    if message.author == bot.user:
        return

    # Log command-like messages from bots or users to help debug integrations
    if message.content.startswith("!keo "):
        log.info(
            "Command detected: author=%s (bot=%s, ID=%s) content=%r",
            message.author,
            message.author.bot,
            message.author.id,
            message.content
        )

    # Ignore other bots unless their ID is specifically allowed
    if message.author.bot and message.author.id not in ALLOWED_BOT_IDS:
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
            "• Buscar soluciones en internet y Reddit\n"
            "• Leer páginas web y enlaces de videos\n"
            "• Info sobre el contenido de Keo (videos analizados)\n"
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
    await ctx.send(
        f"🤖 El modelo de IA principal configurado es: **{NVIDIA_MODELS[0]}**\n"
        f"(Último modelo usado con éxito: **{last_used_model}**)"
    )


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
    """Show bot status including API usage statistics."""
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

    # Calculate current RPM / TPM
    global request_history, token_history
    now = time.time()
    request_history = [t for t in request_history if now - t < 60]
    token_history = [(t, tok) for t, tok in token_history if now - t < 60]

    rpm = len(request_history)
    tpm = sum(tok for _, tok in token_history)
    total_tokens = total_prompt_tokens + total_completion_tokens

    embed = discord.Embed(
        title="📊 Estado de KeoBot",
        color=0x5555FF,
    )
    embed.add_field(name="Servidores", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latencia", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Videos analizados", value=str(video_count), inline=True)
    
    embed.add_field(name="Canales activos", value=str(len(channel_history)), inline=True)
    embed.add_field(name="Modelo IA (Último)", value=last_used_model, inline=True)
    embed.add_field(name="Peticiones Totales", value=str(total_requests), inline=True)

    embed.add_field(name="RPM actual (1m)", value=f"{rpm} req/min", inline=True)
    embed.add_field(name="TPM actual (1m)", value=f"{tpm} tok/min", inline=True)
    embed.add_field(name="Tokens Totales", value=f"{total_tokens:,}", inline=True)

    embed.add_field(
        name="Distribución de Tokens",
        value=f"• Prompt: `{total_prompt_tokens:,}`\n• Completado: `{total_completion_tokens:,}`",
        inline=False
    )
    await ctx.send(embed=embed)


# ── Entry Point ──────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting KeoBot...")
    bot.run(DISCORD_TOKEN, log_handler=None)
