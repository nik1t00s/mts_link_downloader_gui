from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError


ProgressCallback = Callable[[dict], None]


class DownloaderError(Exception):
    """User-facing download error."""


@dataclass(frozen=True)
class MediaSource:
    url: str
    label: str
    kind: str
    start_time: float | None = None


@dataclass(frozen=True)
class DownloadResult:
    file_paths: list[Path]
    title: str

    @property
    def file_path(self) -> Path:
        return self.file_paths[0]


INVALID_FILENAME_CHARS = '<>:"/\\|?*'
MTS_API_BASE = "https://events.webinar.ru/api"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def validate_record_url(url: str) -> None:
    if not url.strip():
        raise DownloaderError("Ссылка пустая. Вставьте ссылку на запись MTS Link.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DownloaderError("Ссылка выглядит некорректно. Проверьте, что она начинается с http:// или https://.")

    host = parsed.netloc.lower()
    if not (host.endswith("mts-link.ru") or host.endswith("webinar.ru")):
        raise DownloaderError("Это не похоже на ссылку MTS Link / МТС Линк.")


def validate_output_dir(output_dir: str | Path) -> Path:
    if not str(output_dir).strip():
        raise DownloaderError("Папка для сохранения не выбрана.")

    path = Path(output_dir).expanduser()
    if not path.exists() or not path.is_dir():
        raise DownloaderError("Выбранная папка не существует.")

    test_file = path / ".mts_link_downloader_write_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        raise DownloaderError("Нет прав на запись в выбранную папку.") from exc

    return path


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def sanitize_filename(name: str) -> str:
    cleaned = "".join("_" if ch in INVALID_FILENAME_CHARS or ch == "\0" else ch for ch in name)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or fallback_title()


def fallback_title() -> str:
    return "mts_link_record_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def unique_stem(directory: Path, stem: str) -> str:
    if not list(directory.glob(f"{stem}.*")):
        return stem

    counter = 1
    while True:
        candidate = f"{stem}_{counter}"
        if not list(directory.glob(f"{candidate}.*")):
            return candidate
        counter += 1


def _is_probably_auth_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "login",
        "sign in",
        "signin",
        "auth",
        "authorization",
        "authorized",
        "403",
        "401",
        "forbidden",
        "private",
        "cookies",
    )
    return any(marker in lowered for marker in markers)


def _has_streaming_format(info: dict) -> bool:
    protocol = str(info.get("protocol") or "").lower()
    if "m3u8" in protocol:
        return True

    for fmt in info.get("formats") or []:
        fmt_protocol = str(fmt.get("protocol") or "").lower()
        fmt_url = str(fmt.get("url") or "").lower()
        if "m3u8" in fmt_protocol or ".m3u8" in fmt_url:
            return True
    return False


def _build_progress_hook(callback: ProgressCallback | None) -> Callable[[dict], None]:
    def hook(status: dict) -> None:
        if callback is None:
            return

        payload = {"status": status.get("status", "unknown")}
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        downloaded = status.get("downloaded_bytes")
        if total and downloaded is not None:
            payload["percent"] = max(0.0, min(100.0, downloaded * 100 / total))
        if status.get("speed"):
            payload["speed"] = status["speed"]
        if status.get("eta") is not None:
            payload["eta"] = status["eta"]
        if status.get("filename"):
            payload["filename"] = status["filename"]
        callback(payload)

    return hook


def _parse_mts_record_url(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parts and parts[0] in {"new", "j"}:
        parts = parts[1:]

    if "record-new" not in parts:
        return None

    index = parts.index("record-new")
    if len(parts) <= index + 1:
        return None

    result = {"session_id": parts[index + 1]}
    tail = parts[index + 2 :]
    if tail[:1] == ["record-file"] and len(tail) >= 2:
        result["record_file_id"] = tail[1]
        if len(tail) >= 3:
            result["access_token"] = tail[2]
    elif tail:
        result["access_token"] = tail[0]

    query = parse_qs(parsed.query)
    for key in ("recordAccessToken", "accessToken"):
        if query.get(key):
            result["access_token"] = query[key][0]
            break

    return result


def _api_json(endpoint: str, referer: str, params: dict[str, str] | None = None) -> dict:
    url = f"{MTS_API_BASE}{endpoint}"
    if params:
        url = f"{url}?{urlencode(params)}"

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": "https://my.mts-link.ru",
            "Referer": referer,
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise DownloaderError("Нет доступа к записи. Возможно, нужна авторизация в MTS Link.") from exc
        if exc.code == 404:
            raise DownloaderError("Запись не найдена или больше недоступна по этой ссылке.") from exc
        raise DownloaderError(f"API MTS Link вернул ошибку HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise DownloaderError("Не удалось подключиться к MTS Link. Проверьте интернет-соединение.") from exc
    except json.JSONDecodeError as exc:
        raise DownloaderError("MTS Link вернул неожиданный ответ без данных записи.") from exc


def _iter_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _extract_media_sources(record: dict) -> list[MediaSource]:
    sources: list[MediaSource] = []
    seen: set[str] = set()

    for item in _iter_dicts(record):
        candidate = None
        hls_url = item.get("hlsUrl")
        direct_url = item.get("url")

        if isinstance(direct_url, str) and ".mp4" in direct_url.lower():
            candidate = direct_url
        elif isinstance(hls_url, str) and ".m3u8" in hls_url.lower():
            candidate = hls_url
        elif isinstance(direct_url, str) and ".m3u8" in direct_url.lower():
            candidate = direct_url

        if not candidate or candidate in seen:
            continue

        seen.add(candidate)
        stream = item.get("stream") if isinstance(item.get("stream"), dict) else {}
        if "screensharing" in stream:
            kind = "video"
        elif "conference" in stream:
            kind = "audio"
        else:
            kind = "unknown"

        label = str(item.get("type") or item.get("mediaType") or item.get("id") or f"media_{len(sources) + 1}")
        start_time = item.get("time")
        sources.append(
            MediaSource(
                url=candidate,
                label=sanitize_filename(label),
                kind=kind,
                start_time=float(start_time) if isinstance(start_time, (int, float)) else None,
            )
        )

    return sources


def _extract_mts_link_info(url: str) -> tuple[str, list[MediaSource], dict[str, str]] | None:
    parsed = _parse_mts_record_url(url)
    if not parsed:
        return None

    params = {}
    if parsed.get("access_token"):
        params["recordAccessToken"] = parsed["access_token"]

    if parsed.get("record_file_id"):
        endpoint = f"/event-sessions/{parsed['session_id']}/record-files/{parsed['record_file_id']}/flow"
    else:
        endpoint = f"/eventsessions/{parsed['session_id']}/record"

    record = _api_json(endpoint, referer=url, params=params)
    if record.get("isViewAllowed") is False or record.get("isViewable") is False:
        raise DownloaderError("Запись есть, но просмотр запрещен для текущего доступа.")

    title = sanitize_filename(str(record.get("name") or fallback_title()))
    sources = _extract_media_sources(record)
    if not sources:
        raise DownloaderError("Видео в данных записи не найдено. Возможно, запись еще обрабатывается или доступ закрыт.")

    headers = {
        "Referer": url,
        "Origin": "https://my.mts-link.ru",
        "User-Agent": USER_AGENT,
    }
    return title, sources, headers


def _download_with_ytdlp(
    media_url: str,
    directory: Path,
    title: str,
    headers: dict[str, str] | None,
    progress_callback: ProgressCallback | None,
) -> Path:
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "http_headers": headers or {},
    }

    with YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(media_url, download=False)

    if not info:
        raise DownloaderError("Видео на странице не найдено.")

    if _has_streaming_format(info) and not ffmpeg_available():
        raise DownloaderError("Видео доступно как поток .m3u8, но ffmpeg не найден. Установите ffmpeg и добавьте его в PATH.")

    safe_stem = unique_stem(directory, title)
    download_opts = {
        **base_opts,
        "outtmpl": str(directory / f"{safe_stem}.%(ext)s"),
        "windowsfilenames": True,
        "restrictfilenames": False,
        "merge_output_format": "mp4",
        "progress_hooks": [_build_progress_hook(progress_callback)],
        "format": "bv*+ba/best",
        "continuedl": True,
        "overwrites": False,
    }

    with YoutubeDL(download_opts) as download_ydl:
        downloaded_info = download_ydl.extract_info(media_url, download=True)
        final_path = Path(download_ydl.prepare_filename(downloaded_info))

    if not final_path.exists():
        candidates = sorted(directory.glob(f"{safe_stem}.*"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            final_path = candidates[0]
        else:
            raise DownloaderError("Скачивание завершилось, но итоговый файл не найден.")

    return final_path


def _source_sort_key(source: MediaSource) -> tuple[float, str]:
    start_time = source.start_time if source.start_time is not None else float("inf")
    return start_time, source.label


def _merge_video_and_audio(
    video_path: Path,
    audio_paths: list[Path],
    output_path: Path,
    video_start: float | None,
    audio_starts: list[float | None],
    progress_callback: ProgressCallback | None,
) -> Path:
    if not ffmpeg_available():
        raise DownloaderError("Для сборки видео и звука в один файл нужен ffmpeg. Установите ffmpeg и добавьте его в PATH.")

    if not audio_paths:
        raise DownloaderError("Аудиодорожки для сборки не найдены.")

    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path)]

    for audio_path, audio_start in zip(audio_paths, audio_starts):
        offset = video_start - audio_start if video_start is not None and audio_start is not None else 0.0
        if offset > 0.05:
            command.extend(["-ss", f"{offset:.3f}", "-i", str(audio_path)])
        elif offset < -0.05:
            command.extend(["-itsoffset", f"{abs(offset):.3f}", "-i", str(audio_path)])
        else:
            command.extend(["-i", str(audio_path)])

    if len(audio_paths) == 1:
        command.extend(["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", str(output_path)])
        log_message = "Быстро объединяю видео и аудио без перекодирования..."
    else:
        inputs = "".join(f"[{index}:a:0]" for index in range(1, len(audio_paths) + 1))
        command.extend(
            [
                "-filter_complex",
                f"{inputs}amix=inputs={len(audio_paths)}:duration=longest:normalize=0[aout]",
                "-map",
                "0:v:0",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(output_path),
            ]
        )
        log_message = f"Смешиваю {len(audio_paths)} аудиодорожки и собираю один MP4..."

    if progress_callback:
        progress_callback({"status": "log", "message": log_message})

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise DownloaderError(f"ffmpeg не смог собрать итоговый файл: {details}")

    if not output_path.exists():
        raise DownloaderError("ffmpeg завершился, но итоговый файл не найден.")

    return output_path


def _concat_video_segments(
    video_paths: list[Path],
    output_path: Path,
    progress_callback: ProgressCallback | None,
) -> Path:
    if len(video_paths) == 1:
        return video_paths[0]

    if not ffmpeg_available():
        raise DownloaderError("Для склейки нескольких видеофрагментов нужен ffmpeg. Установите ffmpeg и добавьте его в PATH.")

    list_path = output_path.with_suffix(".txt")
    list_content = "\n".join(f"file '{path.resolve().as_posix()}'" for path in video_paths)
    list_path.write_text(list_content, encoding="utf-8")

    if progress_callback:
        progress_callback({"status": "log", "message": f"Склеиваю {len(video_paths)} видеофрагмента в одну видеодорожку..."})

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        list_path.unlink(missing_ok=True)
    except OSError:
        pass

    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise DownloaderError(f"ffmpeg не смог склеить видеофрагменты: {details}")

    if not output_path.exists():
        raise DownloaderError("ffmpeg завершил склейку, но итоговый видеофайл не найден.")

    return output_path


def _download_mts_sources(
    title: str,
    sources: list[MediaSource],
    headers: dict[str, str],
    directory: Path,
    progress_callback: ProgressCallback | None,
) -> list[Path]:
    video_sources = sorted([source for source in sources if source.kind == "video"], key=_source_sort_key)
    audio_sources = sorted([source for source in sources if source.kind == "audio"], key=_source_sort_key)

    if video_sources and audio_sources:
        selected_video_sources = video_sources
        selected_audio_sources = audio_sources
        final_path = directory / f"{unique_stem(directory, title)}.mp4"

        with tempfile.TemporaryDirectory(prefix="mts_link_", dir=directory) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            video_paths = []
            for index, video_source in enumerate(selected_video_sources, start=1):
                if progress_callback:
                    progress_callback(
                        {
                            "status": "log",
                            "message": f"Скачиваю видеофрагмент {index} из {len(selected_video_sources)}...",
                        }
                    )
                video_paths.append(
                    _download_with_ytdlp(video_source.url, temp_dir, f"video_{index}", headers, progress_callback)
                )
            video_path = _concat_video_segments(video_paths, temp_dir / "video_joined.mp4", progress_callback)

            audio_paths = []
            for index, audio_source in enumerate(selected_audio_sources, start=1):
                if progress_callback:
                    progress_callback(
                        {
                            "status": "log",
                            "message": f"Скачиваю аудиодорожку {index} из {len(selected_audio_sources)}...",
                        }
                    )
                audio_paths.append(
                    _download_with_ytdlp(audio_source.url, temp_dir, f"audio_{index}", headers, progress_callback)
                )

            return [
                _merge_video_and_audio(
                    video_path,
                    audio_paths,
                    final_path,
                    selected_video_sources[0].start_time,
                    [source.start_time for source in selected_audio_sources],
                    progress_callback,
                )
            ]

    if video_sources:
        if progress_callback:
            progress_callback({"status": "log", "message": "Аудиодорожка не найдена, скачиваю только видео."})
        return [_download_with_ytdlp(video_sources[0].url, directory, title, headers, progress_callback)]

    if audio_sources:
        if progress_callback:
            progress_callback({"status": "log", "message": "Видеодорожка не найдена, скачиваю только основное аудио."})
        return [_download_with_ytdlp(audio_sources[0].url, directory, title, headers, progress_callback)]

    paths = []
    for index, source in enumerate(sources, start=1):
        file_title = title if len(sources) == 1 else f"{title}_{index}_{source.label}"
        paths.append(_download_with_ytdlp(source.url, directory, file_title, headers, progress_callback))
    return paths


def download_record(url: str, output_dir: str | Path, progress_callback: ProgressCallback | None = None) -> DownloadResult:
    url = url.strip()
    validate_record_url(url)
    directory = validate_output_dir(output_dir)

    if progress_callback:
        progress_callback({"status": "log", "message": "Получаю информацию о записи..."})

    try:
        mts_info = _extract_mts_link_info(url)
        if mts_info:
            title, sources, headers = mts_info
            if progress_callback:
                progress_callback({"status": "log", "message": f"Найдено медиа-потоков: {len(sources)}."})
            paths = _download_mts_sources(title, sources, headers, directory, progress_callback)
            return DownloadResult(file_paths=paths, title=title)

        title = fallback_title()
        path = _download_with_ytdlp(url, directory, title, None, progress_callback)
        return DownloadResult(file_paths=[path], title=title)
    except DownloaderError:
        raise
    except YtDlpDownloadError as exc:
        message = str(exc)
        if _is_probably_auth_error(message):
            raise DownloaderError(
                "Запись недоступна без авторизации. Откройте ссылку в браузере и проверьте доступ; "
                "приложение не обходит авторизацию и не использует cookies автоматически."
            ) from exc
        if "unsupported url" in message.lower():
            raise DownloaderError("Не удалось распознать ссылку как запись MTS Link и yt-dlp тоже не поддержал этот URL.") from exc
        if "unable to download webpage" in message.lower() or "urlopen error" in message.lower():
            raise DownloaderError("Не удалось открыть страницу записи. Проверьте интернет-соединение и ссылку.") from exc
        raise DownloaderError(f"Скачивание не удалось: {message}") from exc
    except FileNotFoundError as exc:
        raise DownloaderError("ffmpeg не найден. Установите ffmpeg и добавьте его в PATH.") from exc
    except Exception as exc:
        raise DownloaderError(f"Скачивание не удалось: {exc}") from exc
