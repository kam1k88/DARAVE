"""
recording_uploader.py — после остановки записи ищет самый свежий аудиофайл
в директории записей Mixxx и грузит его backend'у по HTTP (см.
server.py: POST /api/rooms/{session_id}/recording), чтобы диджей мог
скачать микс прямо из браузерного чата, не имея прямого доступа к дискам
companion'а (тот обычно и есть его собственный компьютер, но backend в
модели аренды может обслуживать десятки таких companion'ов одновременно).

Путь к директории записей Mixxx смотреть в Mixxx: Preferences -> Recordings.
Обычно:
  Windows: C:\\Users\\<user>\\Documents\\Mixxx\\Recordings
  Linux/macOS: смотреть в Preferences, обычно ~/Music/Mixxx/Recordings
"""
from __future__ import annotations

from pathlib import Path

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif"}


def find_latest_recording(recordings_dir: str, newer_than: float = 0.0) -> Path | None:
    """Самый свежий аудиофайл в директории записей, изменённый не раньше
    newer_than (time.time() на момент старта записи — чтобы не подхватить
    старый файл, если новый почему-то не появился)."""
    directory = Path(recordings_dir)
    if not directory.is_dir():
        return None
    candidates = [
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and p.stat().st_mtime >= newer_than
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


async def upload_recording(http_base_url: str, session_id: str, file_path: Path) -> bool:
    import httpx  # локальный импорт: не требуем httpx, если запись не используется

    url = f"{http_base_url.rstrip('/')}/api/rooms/{session_id}/recording"
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            with open(file_path, "rb") as f:
                response = await client.post(
                    url, files={"file": (file_path.name, f, "application/octet-stream")}
                )
        response.raise_for_status()
        print(f"[companion] запись '{file_path.name}' загружена backend'у ({file_path.stat().st_size} байт)")
        return True
    except Exception as exc:
        print(f"[companion] не удалось загрузить запись '{file_path.name}': {exc!r}")
        return False


def ws_url_to_http(ws_url: str) -> str:
    """ws://host:port -> http://host:port, wss://... -> https://..."""
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    return ws_url
