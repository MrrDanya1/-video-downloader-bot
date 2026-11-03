import os
import sys
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REST = f"{SUPABASE_URL}/rest/v1"
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
MAX_VIDEO_BYTES = 49 * 1024 * 1024
BATCH_SIZE = 3
TIMEOUT = 30

session = requests.Session()
session.headers.update({
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
})


def telegram_message(chat_id: int, text: str) -> None:
    response = requests.post(
        f"{BOT_API}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram sendMessage failed"))


def update_request(request_id: int, status: str, error: str | None = None) -> None:
    response = session.patch(
        f"{REST}/video_bot_requests",
        params={"id": f"eq.{request_id}"},
        json={"status": status, "error": error},
        headers={"Prefer": "return=minimal"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def allowed_video_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    allowed = ("youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "pin.it")
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def download_video(url: str, folder: Path) -> Path:
    output = folder / "%(id)s.%(ext)s"
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--max-filesize", "49M",
        "--format", "best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--recode-video", "mp4",
        "--output", str(output),
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Download failed").strip()
        if "max-filesize" in details.lower() or "file is larger" in details.lower():
            raise ValueError("Видео слишком большое для отправки через Telegram (лимит этого бота — 49 МБ).")
        raise RuntimeError("Не удалось получить видео. Возможно, ссылка недоступна или источник временно ограничил загрузку.")
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".m4v", ".mov"}]
    if not files:
        raise RuntimeError("Загрузчик не создал MP4-файл.")
    video = max(files, key=lambda p: p.stat().st_size)
    if video.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError("Видео больше 49 МБ — Telegram не позволит отправить его этим способом.")
    return video


def send_video(chat_id: int, path: Path) -> None:
    with path.open("rb") as video:
        response = requests.post(
            f"{BOT_API}/sendVideo",
            data={"chat_id": chat_id, "supports_streaming": "true"},
            files={"video": (path.name, video, "video/mp4")},
            timeout=(TIMEOUT, 180),
        )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram sendVideo failed"))


def claim_request(row: dict) -> bool:
    response = session.patch(
        f"{REST}/video_bot_requests",
        params={"id": f"eq.{row['id']}", "status": "eq.queued"},
        json={"status": "processing", "error": None},
        headers={"Prefer": "return=representation"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json())


def process(row: dict) -> None:
    request_id = row["id"]
    chat_id = row["telegram_id"]
    url = row["source_url"]

    try:
        if not allowed_video_url(url):
            raise ValueError("Поддерживаются только общедоступные ссылки TikTok, YouTube и Pinterest.")
        with tempfile.TemporaryDirectory(prefix="video-bot-") as temp:
            video = download_video(url, Path(temp))
            send_video(chat_id, video)
        update_request(request_id, "sent")
        print(f"Request {request_id}: sent")
    except Exception as exc:
        message = str(exc)[:500]
        try:
            update_request(request_id, "failed", message)
        except Exception as db_exc:
            print(f"Could not mark request {request_id} failed: {db_exc}", file=sys.stderr)
        try:
            telegram_message(chat_id, message)
        except Exception as tg_exc:
            print(f"Could not notify chat for request {request_id}: {tg_exc}", file=sys.stderr)
        print(f"Request {request_id}: failed: {message}", file=sys.stderr)


def main() -> None:
    response = session.get(
        f"{REST}/video_bot_requests",
        params={
            "select": "id,telegram_id,source,source_url",
            "status": "eq.queued",
            "order": "created_at.asc",
            "limit": str(BATCH_SIZE),
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    rows = response.json()
    print(f"Found {len(rows)} queued request(s)")
    for row in rows:
        if claim_request(row):
            process(row)


if __name__ == "__main__":
    main()
