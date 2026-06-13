# 🤖 KeoBot - Asistente de Discord para la comunidad de Keo

Bot de Discord que funciona como asistente para el servidor de Minecraft de **Keo** (@elkeomc). Usa la API de Gemini (gratis) para responder preguntas sobre Minecraft, mods, PrismLauncher, y el contenido de Keo.

## ✨ Características

- 🎮 Responde preguntas sobre Minecraft (mods, launchers, optimización, etc.)
- 📺 Base de conocimiento editable con info de los videos de Keo
- 🧠 Memoria de conversación por canal
- 🛡️ Filtro de contenido (solo responde temas de Minecraft/server)
- ⚡ Respuestas rápidas con Gemini 2.0 Flash
- 🚀 Listo para deploy en Railway

## 🛠️ Setup Local

### 1. Requisitos previos
- Python 3.11+
- Una cuenta de Discord con acceso al [Developer Portal](https://discord.com/developers/applications)
- Una API key de Gemini (gratis) desde [AI Studio](https://aistudio.google.com/apikey)

### 2. Crear el Bot en Discord
1. Ve a https://discord.com/developers/applications
2. Click "New Application" → nombra tu bot "KeoBot"
3. Ve a la pestaña "Bot"
4. Click "Reset Token" y copia el token (lo necesitarás)
5. Activa estos **Privileged Gateway Intents**:
   - ✅ MESSAGE CONTENT INTENT
   - ✅ SERVER MEMBERS INTENT
6. Ve a "OAuth2" → "URL Generator"
7. Selecciona scopes: `bot`, `applications.commands`
8. Selecciona permisos: `Send Messages`, `Read Message History`, `Embed Links`, `Use Slash Commands`
9. Copia la URL generada y ábrela para invitar el bot a tu server

### 3. Instalar dependencias
```bash
cd keo-discord-bot
python -m venv venv
source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 4. Configurar variables de entorno
```bash
cp .env.example .env
# Edita .env con tu token y API key
```

### 5. Personalizar el bot
Edita `system_prompt.py` para agregar:
- Información de los videos de Keo en `KEO_KNOWLEDGE`
- Tips y trucos en `EXTRA_KNOWLEDGE`
- Personalizar la personalidad del bot

### 6. Ejecutar
```bash
python bot.py
```

## 🚀 Deploy en Railway

### 1. Crear el repositorio
```bash
cd keo-discord-bot
git init
git add .
git commit -m "Initial commit: KeoBot"
```

### 2. Subir a GitHub
```bash
# Crea un repo en GitHub (puede ser privado)
gh repo create keo-discord-bot --private --source=. --push
# O manualmente:
git remote add origin https://github.com/TU_USUARIO/keo-discord-bot.git
git push -u origin main
```

### 3. Deploy en Railway
1. Ve a [railway.app](https://railway.app) e inicia sesión con GitHub
2. Click "New Project" → "Deploy from GitHub repo"
3. Selecciona tu repo `keo-discord-bot`
4. En el dashboard del proyecto, ve a "Variables"
5. Agrega las variables de entorno:
   - `DISCORD_TOKEN` = tu token del bot
   - `GEMINI_API_KEY` = tu API key de Gemini
   - `ALLOWED_CHANNELS` = (opcional) IDs de canales separados por coma
   - `ADMIN_ROLE` = (opcional) nombre del rol de admin
6. Railway detectará automáticamente que es Python y lo desplegará
7. ¡Listo! El bot debería estar online 🎉

## 📝 Uso

| Acción | Ejemplo |
|--------|---------|
| Mencionar | `@KeoBot cómo pongo mods en PrismLauncher?` |
| Comando | `!keo help` |
| DM | Enviar mensaje directo al bot |
| Limpiar historial | `!keo clear` |
| Ver estado (admin) | `!keo status` |

## 📺 Agregar info de videos de Keo

Edita la variable `KEO_KNOWLEDGE` en `system_prompt.py`:

```python
KEO_KNOWLEDGE = """
- El server de Keo es survival vanilla 1.21.x
- La IP del server es: play.keomc.com
- En su video del 10 de junio, Keo hizo una granja de hierro automática
- Keo recomienda usar Sodium + Fabric para mejor rendimiento
- Las reglas del server: no grief, no hack, ser respetuoso
"""
```

## 📄 Licencia

Proyecto personal para la comunidad de Keo.
