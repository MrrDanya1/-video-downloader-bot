import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
INTERAPI_KEY = os.environ["INTERAPI_KEY"].strip()
REST = f"{SUPABASE_URL}/rest/v1"
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
INTERAPI = "https://api.interapi.dev"
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


def source_for(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None

    domains = {
        "tiktok": ("tiktok.com",),
        "youtube": ("youtube.com", "youtu.be"),
        "pinterest": ("pinterest.com", "pin.it"),
    }
    for source, suffixes in domains.items():
        if any(host == domain or host.endswith("." + domain) for domain in suffixes):
            return source
    return None


def download_video(source: str, url: str, folder: Path) -> Path:
    # InterAPI's file proxy returns the media itself. The API key is kept
    # server-side in GitHub Actions secrets and is never sent to Telegram.
    if source == "youtube":
        endpoint = f"{INTERAPI}/api/v1/youtube/file"
        params = {"url": url, "kind": "video", "format_id": "best", "api": INTERAPI_KEY}
    else:
        endpoint = f"{INTERAPI}/api/v1/media/file"
        params = {"source": source, "url": url, "api": INTERAPI_KEY}

    try:
        with requests.get(
            endpoint,
            params=params,
            headers={"X-API-Key": INTERAPI_KEY},
            stream=True,
            timeout=(20, 180),
            allow_redirects=True,
        ) as response:
            if not response.ok:
                # Do not include the request URL: it contains the private API key.
                detail = ""
                try:
                    payload = response.json()
                    detail = str(payload.get("message") or payload.get("error") or "")
                except Exception:
                    pass
                if response.status_code == 401:
                    raise RuntimeError("InterAPI: ключ не принят. Проверьте секрет INTERAPI_KEY в GitHub.")
                if response.status_code == 402:
                    raise RuntimeError("InterAPI: закончились кредиты на аккаунте.")
                if response.status_code == 429:
                    raise RuntimeError("InterAPI: превышен лимит запросов. Попробуйте позже.")
                if response.status_code in (400, 403, 404):
                    raise RuntimeError("InterAPI не смог обработать эту ссылку. Проверьте, что видео общедоступно.")
                raise RuntimeError(f"InterAPI вернул ошибку HTTP {response.status_code}. {detail[:160]}")

            content_type = response.headers.get("content-type", "").lower()
            if "json" in content_type or "text/html" in content_type:
                try:
                    payload = response.json()
                    detail = payload.get("message") or payload.get("error") or payload.get("description")
                except Exception:
                    detail = None
                raise RuntimeError(f"InterAPI не вернул видеофайл. {str(detail or '')[:160]}")

            output = folder / "video.mp4"
            total = 0
            with output.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise ValueError("Видео больше 49 МБ — Telegram не позволит отправить его этим способом.")
                    file.write(chunk)

            if total < 1024:
                raise RuntimeError("InterAPI вернул пустой или слишком маленький файл.")
            return output
    except requests.Timeout:
        raise RuntimeError("InterAPI не ответил вовремя. Попробуйте ещё раз.")
    except requests.RequestException:
        raise RuntimeError("Не удалось получить видео от InterAPI. Попробуйте позже.")


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
        source = source_for(url)
        if source not in {"tiktok", "youtube", "pinterest"}:
            raise ValueError("Поддерживаются только общедоступные ссылки TikTok, YouTube и Pinterest.")
        with tempfile.TemporaryDirectory(prefix="video-bot-") as temp:
            video = download_video(source, url, Path(temp))
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
