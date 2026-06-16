"""
Video Analyzer for Keo's TikTok content.
Downloads videos from @elkeomc and uses Gemini to extract
summaries, tips, modpack info, and any useful Minecraft knowledge.

Usage:
    python video_analyzer.py                  # Analyze new videos
    python video_analyzer.py --max 5          # Analyze only 5 latest
    python video_analyzer.py --force          # Re-analyze all videos
    python video_analyzer.py --list           # List analyzed videos
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ── Setup ────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
log = logging.getLogger("analyzer")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    log.error("GEMINI_API_KEY not set in .env")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# Paths
VIDEOS_DIR = Path("videos_tmp")
KNOWLEDGE_FILE = Path("video_knowledge.json")

# TikTok user to analyze
TIKTOK_USER = "elkeomc"

# Prompt for Gemini to analyze each video
ANALYSIS_PROMPT = """
Analiza este video de TikTok del creador de contenido de Minecraft "Keo" (@elkeomc).

Extrae la siguiente información en formato JSON (responde SOLO con el JSON, sin markdown ni explicaciones):

{
    "titulo": "Título o tema principal del video",
    "resumen": "Resumen breve de lo que pasa/se explica en el video (2-3 oraciones)",
    "tipo_contenido": "tutorial | gameplay | review | modpack | tip | humor | otro",
    "tips_minecraft": ["Lista de tips o consejos de Minecraft mencionados"],
    "mods_mencionados": ["Lista de mods o modpacks mencionados con detalles"],
    "versiones_minecraft": ["Versiones de Minecraft mencionadas"],
    "herramientas_mencionadas": ["Launchers, programas o herramientas mencionadas"],
    "info_relevante": "Cualquier otra información útil que alguien de la comunidad podría preguntar"
}

Si algún campo no aplica, usa una lista vacía [] o "N/A".
Sé preciso y no inventes información que no esté en el video.
"""


def download_videos(max_videos: int = 10) -> list[dict]:
    """Download recent TikTok videos from Keo using yt-dlp."""
    VIDEOS_DIR.mkdir(exist_ok=True)

    log.info("Fetching video list from @%s...", TIKTOK_USER)

    # First get video info without downloading
    info_cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", str(max_videos),
        f"https://www.tiktok.com/@{TIKTOK_USER}",
    ]

    try:
        result = subprocess.run(
            info_cmd, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        log.error(
            "yt-dlp not found! Install it: pip install yt-dlp\n"
            "Also make sure ffmpeg is installed on your system."
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        log.error("Timed out fetching video list. TikTok might be blocking requests.")
        return []

    if result.returncode != 0:
        log.error("yt-dlp error: %s", result.stderr[:500])
        return []

    # Parse video info
    videos = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            info = json.loads(line)
            videos.append({
                "id": info.get("id", ""),
                "url": info.get("url") or info.get("webpage_url", ""),
                "title": info.get("title", "Sin título"),
                "timestamp": info.get("timestamp"),
            })
        except json.JSONDecodeError:
            continue

    log.info("Found %d videos", len(videos))
    return videos


def download_single_video(video_url: str, video_id: str) -> Path | None:
    """Download a single video file."""
    output_path = VIDEOS_DIR / f"{video_id}.mp4"

    if output_path.exists():
        log.info("Video %s already downloaded", video_id)
        return output_path

    log.info("Downloading video %s...", video_id)
    cmd = [
        "yt-dlp",
        "-o", str(output_path),
        "--format", "best[ext=mp4]/best",
        "--no-playlist",
        video_url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and output_path.exists():
            log.info("Downloaded: %s (%.1f MB)", output_path.name, output_path.stat().st_size / 1e6)
            return output_path
        else:
            log.warning("Failed to download %s: %s", video_id, result.stderr[:200])
            return None
    except subprocess.TimeoutExpired:
        log.warning("Download timed out for %s", video_id)
        return None


def analyze_video_with_gemini(video_path: Path) -> dict | None:
    """Upload video to Gemini and get analysis."""
    log.info("Uploading %s to Gemini for analysis...", video_path.name)

    try:
        # Upload video file
        uploaded_file = client.files.upload(file=video_path)

        # Wait for file to be processed
        log.info("Waiting for Gemini to process the video...")
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(3)
            uploaded_file = client.files.get(name=uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            log.error("Gemini failed to process video: %s", video_path.name)
            return None

        # Analyze the video
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_uri(
                            file_uri=uploaded_file.uri,
                            mime_type="video/mp4",
                        ),
                        types.Part.from_text(text=ANALYSIS_PROMPT),
                    ],
                ),
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,  # Low temp for factual extraction
                max_output_tokens=2048,
            ),
        )

        # Parse the JSON response
        response_text = response.text.strip()

        # Clean up markdown code fences if present
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[1]
        if response_text.endswith("```"):
            response_text = response_text.rsplit("```", 1)[0]
        response_text = response_text.strip()

        analysis = json.loads(response_text)

        # Clean up the uploaded file
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass  # Not critical

        log.info("Analysis complete for %s: %s", video_path.name, analysis.get("titulo", "?"))
        return analysis

    except json.JSONDecodeError as e:
        log.error("Failed to parse Gemini response as JSON: %s", e)
        log.debug("Raw response: %s", response_text[:500] if 'response_text' in dir() else "N/A")
        return None
    except Exception as e:
        log.error("Gemini analysis failed for %s: %s", video_path.name, e)
        return None


def load_existing_knowledge() -> dict:
    """Load previously analyzed video data."""
    if KNOWLEDGE_FILE.exists():
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "videos": {}}


def save_knowledge(knowledge: dict):
    """Save video knowledge to file."""
    knowledge["last_updated"] = datetime.now().isoformat()
    with open(KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
        json.dump(knowledge, f, ensure_ascii=False, indent=2)
    log.info("Knowledge saved to %s (%d videos)", KNOWLEDGE_FILE, len(knowledge["videos"]))


def cleanup_videos():
    """Remove downloaded video files to save space."""
    if VIDEOS_DIR.exists():
        for f in VIDEOS_DIR.iterdir():
            f.unlink()
        log.info("Cleaned up temporary video files")


def get_knowledge_summary() -> str:
    """Generate a human-readable summary of all analyzed videos for the bot."""
    knowledge = load_existing_knowledge()

    if not knowledge["videos"]:
        return "No se han analizado videos todavía."

    lines = []
    for vid_id, data in knowledge["videos"].items():
        analysis = data.get("analysis", {})
        lines.append(f"\n### Video: {analysis.get('titulo', 'Sin título')}")
        lines.append(f"- **Tipo**: {analysis.get('tipo_contenido', 'N/A')}")
        lines.append(f"- **Resumen**: {analysis.get('resumen', 'N/A')}")

        tips = analysis.get("tips_minecraft", [])
        if tips:
            lines.append("- **Tips de Minecraft**:")
            for tip in tips:
                lines.append(f"  - {tip}")

        mods = analysis.get("mods_mencionados", [])
        if mods:
            lines.append("- **Mods/Modpacks mencionados**:")
            for mod in mods:
                lines.append(f"  - {mod}")

        tools = analysis.get("herramientas_mencionadas", [])
        if tools:
            lines.append("- **Herramientas mencionadas**:")
            for tool in tools:
                lines.append(f"  - {tool}")

        info = analysis.get("info_relevante", "N/A")
        if info and info != "N/A":
            lines.append(f"- **Info extra**: {info}")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Analyze Keo's TikTok videos")
    parser.add_argument("--max", type=int, default=10, help="Max videos to analyze (default: 10)")
    parser.add_argument("--force", action="store_true", help="Re-analyze all videos")
    parser.add_argument("--list", action="store_true", help="List analyzed videos")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep downloaded videos")
    parser.add_argument("--url", type=str, help="Analyze a specific video URL")
    args = parser.parse_args()

    # List mode
    if args.list:
        knowledge = load_existing_knowledge()
        if not knowledge["videos"]:
            print("No videos analyzed yet.")
            return
        print(f"\nAnalyzed {len(knowledge['videos'])} videos (last updated: {knowledge['last_updated']})\n")
        for vid_id, data in knowledge["videos"].items():
            analysis = data.get("analysis", {})
            print(f"  [{vid_id}] {analysis.get('titulo', '?')} ({analysis.get('tipo_contenido', '?')})")
        return

    # URL mode
    if args.url:
        knowledge = load_existing_knowledge()
        url = args.url.strip()
        
        log.info("Fetching video info for URL: %s...", url)
        info_cmd = ["yt-dlp", "--dump-json", "--no-playlist", url]
        try:
            result = subprocess.run(info_cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                info = json.loads(result.stdout)
                vid_id = info.get("id", "")
                title = info.get("title", "Sin título")
            else:
                log.error("Failed to fetch video info: %s", result.stderr[:200])
                return
        except Exception as e:
            log.error("Failed to run yt-dlp: %s", e)
            return

        if not vid_id:
            import hashlib
            vid_id = hashlib.md5(url.encode()).hexdigest()[:12]
            title = "Video analizado por URL"

        if vid_id in knowledge["videos"] and not args.force:
            log.info("Skipping %s (already analyzed)", vid_id)
            return

        # Download
        video_path = download_single_video(url, vid_id)
        if not video_path:
            log.error("Could not download video.")
            return

        # Analyze with Gemini
        analysis = analyze_video_with_gemini(video_path)
        if not analysis:
            log.error("Analysis failed.")
            return

        # Store
        knowledge["videos"][vid_id] = {
            "url": url,
            "title": title,
            "analyzed_at": datetime.now().isoformat(),
            "analysis": analysis,
        }
        
        save_knowledge(knowledge)
        
        if not args.no_cleanup:
            cleanup_videos()
            
        print("\n" + "=" * 60)
        print("KNOWLEDGE SUMMARY FOR SINGLE VIDEO")
        print("=" * 60)
        print(f"Video: {title}")
        print(f"Tipo: {analysis.get('tipo_contenido', 'N/A')}")
        print(f"Resumen: {analysis.get('resumen', 'N/A')}")
        return

    # Analyze mode
    knowledge = load_existing_knowledge()
    videos = download_videos(max_videos=args.max)

    if not videos:
        log.warning("No videos found to analyze.")
        return

    new_count = 0
    for video_info in videos:
        vid_id = video_info["id"]

        # Skip already analyzed (unless --force)
        if vid_id in knowledge["videos"] and not args.force:
            log.info("Skipping %s (already analyzed)", vid_id)
            continue

        # Download
        video_path = download_single_video(video_info["url"], vid_id)
        if not video_path:
            continue

        # Analyze with Gemini
        analysis = analyze_video_with_gemini(video_path)
        if not analysis:
            continue

        # Store
        knowledge["videos"][vid_id] = {
            "url": video_info["url"],
            "title": video_info["title"],
            "analyzed_at": datetime.now().isoformat(),
            "analysis": analysis,
        }
        new_count += 1

        # Save after each video in case we crash
        save_knowledge(knowledge)

        # Brief pause to be nice to APIs
        time.sleep(2)

    log.info("Done! Analyzed %d new videos. Total: %d", new_count, len(knowledge["videos"]))

    # Cleanup
    if not args.no_cleanup:
        cleanup_videos()

    # Show summary
    print("\n" + "=" * 60)
    print("KNOWLEDGE SUMMARY")
    print("=" * 60)
    print(get_knowledge_summary())


if __name__ == "__main__":
    main()
