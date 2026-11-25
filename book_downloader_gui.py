#!/usr/bin/env python3
"""
book_downloader_gui.py

Entry point for Free Book Downloader — a desktop GUI for searching and
downloading free, public-domain books from Project Gutenberg, Open Library,
and Google Books.
"""

from __future__ import annotations

import argparse
import os
import threading

from book_downloader import __version__
from book_downloader.downloader import (
    DOWNLOAD_DIR,
    FORMAT_EXTENSIONS,
    FORMAT_PRIORITY,
    build_dest_path,
    default_download_dir,
    download_file,
    open_folder,
    pick_format,
    sanitize_filename,
)
from book_downloader.sources import (
    GUTENDEX_FORMAT_MAP,
    HEADERS,
    REQUEST_TIMEOUT,
    SESSION,
    SOURCES,
    fetch_archive_org_formats,
    search_all_sources,
    search_google_books,
    search_gutenberg,
    search_openlibrary,
)
from book_downloader.ui import BookDownloaderApp, enable_hidpi, tk

__all__ = [
    "DOWNLOAD_DIR",
    "FORMAT_EXTENSIONS",
    "FORMAT_PRIORITY",
    "GUTENDEX_FORMAT_MAP",
    "HEADERS",
    "REQUEST_TIMEOUT",
    "SESSION",
    "SOURCES",
    "BookDownloaderApp",
    "__version__",
    "build_dest_path",
    "default_download_dir",
    "download_file",
    "enable_hidpi",
    "fetch_archive_org_formats",
    "main",
    "open_folder",
    "os",
    "pick_format",
    "sanitize_filename",
    "search_all_sources",
    "search_google_books",
    "search_gutenberg",
    "search_openlibrary",
    "threading",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="book_downloader_gui",
        description="Search and download free public-domain books.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.parse_args(argv)

    if tk is None:
        raise RuntimeError(
            "tkinter is not installed. Install python3-tk (e.g. `sudo apt install python3-tk`)."
        )
    enable_hidpi()
    root = tk.Tk()
    BookDownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
