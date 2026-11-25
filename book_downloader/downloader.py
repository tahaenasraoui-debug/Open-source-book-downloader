"""File path sanitization, format selection, and atomic streaming downloads."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

from book_downloader.sources import REQUEST_TIMEOUT, SESSION

DOWNLOAD_DIR = "downloads"
CHUNK_SIZE = 65536  # 64 KiB for faster large PDF/EPUB transfers

FORMAT_PRIORITY = ["EPUB", "PDF", "TXT", "MOBI"]
FORMAT_EXTENSIONS = {
    "EPUB": "epub",
    "PDF": "pdf",
    "TXT": "txt",
    "MOBI": "mobi",
}


def default_download_dir() -> str:
    """Return a safe, user-writable default folder regardless of launch cwd."""
    downloads_root = Path.home() / "Downloads"
    base = downloads_root if downloads_root.is_dir() else Path.home()
    return str(base / "FreeBookDownloader")


def open_folder(path: str) -> None:
    """Open *path* in the OS file manager."""
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def sanitize_filename(name: str, max_len: int = 100, fallback: str = "untitled") -> str:
    """Convert arbitrary text into a safe, bounded filename component."""
    cleaned = re.sub(r"[\\/]+", " ", str(name or ""))
    cleaned = re.sub(r"[^\w\s\-.]", "", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"[\s_]+", "_", cleaned).strip("_.")
    if max_len > 0:
        cleaned = cleaned[:max_len].strip("_.")
    return cleaned or fallback


def build_dest_path(result: dict, ext: str, download_dir: str = DOWNLOAD_DIR) -> str:
    """Build a collision-safe output path including author, ID, and dedup suffix."""
    safe_ext = sanitize_filename(ext, max_len=16, fallback="bin").lstrip(".") or "bin"
    parts = [
        sanitize_filename(result.get("source", ""), max_len=25, fallback="source"),
        sanitize_filename(result.get("title", ""), max_len=80, fallback="untitled"),
    ]
    author = str(result.get("author") or "").strip()
    if author:
        parts.append(sanitize_filename(author, max_len=40, fallback="unknown"))
    book_id = str(result.get("id") or "").strip()
    if book_id:
        parts.append(sanitize_filename(book_id, max_len=30, fallback="id"))

    stem = "_".join(parts)
    candidate = os.path.join(download_dir, f"{stem}.{safe_ext}")
    counter = 2
    while os.path.exists(candidate) or os.path.exists(f"{candidate}.part"):
        candidate = os.path.join(download_dir, f"{stem}_{counter}.{safe_ext}")
        counter += 1
    return candidate


def pick_format(formats: dict, preferred: str = "Auto") -> tuple[str, str]:
    """Select the preferred format if available, else fall back to FORMAT_PRIORITY."""
    pref = (preferred or "Auto").upper()
    if pref != "AUTO" and pref in formats:
        return pref, formats[pref]
    for label in FORMAT_PRIORITY:
        if label in formats:
            return label, formats[label]
    label = next(iter(formats))
    return label, formats[label]


def _parse_content_length(raw_value) -> int:
    try:
        total = int(raw_value) if raw_value is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(0, total)


def _remove_if_exists(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def download_file(url, dest_path, progress_cb=None, cancel_event=None):
    """Stream *url* into a `.part` temporary file and rename atomically on completion."""
    part_path = f"{dest_path}.part"
    created_part = False
    try:
        with SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
            resp.raise_for_status()
            total = _parse_content_length(resp.headers.get("content-length"))
            downloaded = 0
            if progress_cb:
                progress_cb(0, total)
            created_part = True
            with open(part_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel_event and cancel_event.is_set():
                        raise InterruptedError("Cancelled by user.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Cancelled by user.")
        os.replace(part_path, dest_path)
        return True, None
    except InterruptedError as exc:
        if created_part:
            _remove_if_exists(part_path)
        return False, str(exc)
    except (requests.exceptions.RequestException, OSError) as exc:
        if created_part:
            _remove_if_exists(part_path)
        return False, str(exc)
    except Exception:
        if created_part:
            _remove_if_exists(part_path)
        raise
