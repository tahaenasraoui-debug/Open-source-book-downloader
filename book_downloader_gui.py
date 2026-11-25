#!/usr/bin/env python3
"""
book_downloader_gui.py

A desktop GUI for searching and downloading free, legally-downloadable
books from three public sources:

    - Project Gutenberg   (via the Gutendex API)
    - Open Library        (via Internet Archive, public-domain scans only)
    - Google Books        (only items Google itself flags public-domain
                            and offers a direct download link for)

SCOPE / WHAT THIS DOES NOT DO:
    This tool only surfaces books that the source itself marks as freely
    downloadable in the public domain:
      - Open Library results that are "borrow only" (access-restricted on
        Internet Archive) are skipped entirely — no DRM bypass, no login.
      - Google Books results are skipped unless Google's own API marks
        them public domain AND provides a direct download link.
    Shadow libraries (Anna's Archive, Z-Library, LibGen, etc.) are not
    included and never will be — those distribute copyrighted material
    without licence.

DEPENDENCIES:
    pip install requests
    (tkinter ships with standard Python on most platforms; on some Linux
    distros install it separately, e.g. `sudo apt install python3-tk`)

USAGE:
    python3 book_downloader_gui.py
    Type one or more titles (comma-separated) into the search box, hit
    Search, select the rows you want in the results table, then click
    Download Selected.
"""

import contextlib
import os
import re
import threading
from urllib.parse import quote

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # pragma: no cover - allows headless unit tests without python3-tk
    tk = None
    ttk = None
    messagebox = None

import requests

DOWNLOAD_DIR = "downloads"
REQUEST_TIMEOUT = 20

# Cloudflare and similar CDNs sometimes stall requests carrying Python's
# default User-Agent instead of returning a clean error, so we send a
# normal browser-style UA on every request.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Preference order when a book has more than one downloadable format.
FORMAT_PRIORITY = ["EPUB", "PDF", "TXT", "MOBI"]
FORMAT_EXTENSIONS = {
    "EPUB": "epub",
    "PDF": "pdf",
    "TXT": "txt",
    "MOBI": "mobi",
}


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


def _format_rate_limit_log(source_name: str, resp) -> str:
    """Format a concise HTTP 429 log line without exposing full query URLs."""
    retry_after = ""
    headers = getattr(resp, "headers", None)
    if headers:
        raw_retry = headers.get("Retry-After")
        if raw_retry:
            retry_after = f" (Retry-After: {raw_retry})"
    return (
        f"  [!] {source_name} rate limit reached (HTTP 429){retry_after}. "
        "Try again later."
    )


# ---------------------------------------------------------------------------
# Source: Project Gutenberg (via Gutendex)
# ---------------------------------------------------------------------------

GUTENDEX_FORMAT_MAP = [
    ("EPUB", "application/epub+zip"),
    ("MOBI", "application/x-mobipocket-ebook"),
    ("PDF", "application/pdf"),
    ("TXT", "text/plain"),
]


def search_gutenberg(title, log_fn=None):
    results = []
    try:
        resp = SESSION.get(
            "https://gutendex.com/books",
            params={"search": title},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("results") or []
    except (requests.exceptions.RequestException, ValueError) as exc:
        if log_fn:
            log_fn(f"  [!] Gutenberg error for '{title}': {exc}")
        return results

    for book in data[:5]:
        formats = {}
        raw_formats = book.get("formats", {})
        for label, mime in GUTENDEX_FORMAT_MAP:
            for fmt_mime, url in raw_formats.items():
                if fmt_mime.startswith(mime) and label not in formats:
                    formats[label] = url
                    break
        if not formats:
            continue
        authors = ", ".join(
            a["name"] for a in (book.get("authors") or []) if a.get("name")
        ) or "Unknown"
        book_id = book.get("id")
        results.append({
            "source": "Gutenberg",
            "id": str(book_id) if book_id is not None else "",
            "title": book.get("title", title),
            "author": authors,
            "formats": formats,
        })
    return results


# ---------------------------------------------------------------------------
# Source: Open Library -> Internet Archive (public-domain scans only)
# ---------------------------------------------------------------------------

def search_openlibrary(title, log_fn=None):
    results = []
    try:
        resp = SESSION.get(
            "https://openlibrary.org/search.json",
            params={"q": title, "limit": 5, "fields": "title,author_name,ia,ebook_access"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        docs = resp.json().get("docs") or []
    except (requests.exceptions.RequestException, ValueError) as exc:
        if log_fn:
            log_fn(f"  [!] Open Library error for '{title}': {exc}")
        return results

    for doc in docs[:5]:
        ia_ids = doc.get("ia") or []
        if not ia_ids:
            continue  # nothing hosted on Internet Archive to fetch
        # Only bother with items Open Library itself marks as fully open.
        if doc.get("ebook_access") not in ("public", "borrowable_or_public", "public_domain"):
            # still worth checking IA metadata directly, since ebook_access
            # is sometimes missing/stale; we verify with IA below anyway.
            pass

        ia_id = ia_ids[0]
        formats = fetch_archive_org_formats(ia_id, log_fn=log_fn)
        if not formats:
            continue  # borrow-only or no usable files - skip, no DRM bypass

        authors = ", ".join(doc.get("author_name") or []) or "Unknown"
        results.append({
            "source": "Open Library",
            "id": str(ia_id),
            "title": doc.get("title", title),
            "author": authors,
            "formats": formats,
        })
    return results


def fetch_archive_org_formats(ia_id, log_fn=None):
    """
    Look up an Internet Archive item's metadata. Returns {} if the item is
    access-restricted (borrow-only / DRM) - we never attempt to work around
    that. Otherwise returns a dict of label -> direct download URL.
    """
    encoded_id = quote(str(ia_id), safe="")
    try:
        resp = SESSION.get(
            f"https://archive.org/metadata/{encoded_id}",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        meta = resp.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        if log_fn:
            log_fn(f"  [!] Internet Archive error for '{ia_id}': {exc}")
        return {}

    if (meta.get("metadata") or {}).get("access-restricted-item") in ("true", True):
        return {}  # borrow-only item; skip entirely

    formats = {}
    for f in meta.get("files") or []:
        fmt = (f.get("format") or "").upper()
        name = f.get("name", "")
        if not name:
            continue
        encoded_name = quote(name.lstrip("/"), safe="/")
        url = f"https://archive.org/download/{encoded_id}/{encoded_name}"
        if "EPUB" in fmt and "EPUB" not in formats:
            formats["EPUB"] = url
        elif fmt in ("TEXT PDF", "PDF") or ("PDF" in fmt and "PDF" not in formats):
            formats.setdefault("PDF", url)
        elif "DJVUTXT" in fmt.replace(" ", "") and "TXT" not in formats:
            formats["TXT"] = url
    return formats


# ---------------------------------------------------------------------------
# Source: Google Books (public-domain, directly downloadable items only)
# ---------------------------------------------------------------------------

def search_google_books(title, log_fn=None, on_rate_limit=None):
    results = []
    try:
        resp = SESSION.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": title, "maxResults": 5},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            if log_fn:
                log_fn(_format_rate_limit_log("Google Books", resp))
            if on_rate_limit:
                on_rate_limit()
            return results
        resp.raise_for_status()
        items = resp.json().get("items") or []
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            if log_fn:
                log_fn(_format_rate_limit_log("Google Books", exc.response))
            if on_rate_limit:
                on_rate_limit()
            return results
        if log_fn:
            log_fn(f"  [!] Google Books error for '{title}': {exc}")
        return results
    except (requests.exceptions.RequestException, ValueError) as exc:
        if log_fn:
            log_fn(f"  [!] Google Books error for '{title}': {exc}")
        return results

    for item in items:
        info = item.get("volumeInfo", {})
        access = item.get("accessInfo", {})

        if not access.get("publicDomain"):
            continue  # not free to redistribute - skip

        formats = {}
        epub = access.get("epub", {})
        pdf = access.get("pdf", {})
        if epub.get("isAvailable") and epub.get("downloadLink"):
            formats["EPUB"] = epub["downloadLink"]
        if pdf.get("isAvailable") and pdf.get("downloadLink"):
            formats["PDF"] = pdf["downloadLink"]

        if not formats:
            continue  # public domain but no direct download link exposed

        authors = ", ".join(info.get("authors") or []) or "Unknown"
        results.append({
            "source": "Google Books",
            "id": str(item.get("id") or ""),
            "title": info.get("title", title),
            "author": authors,
            "formats": formats,
        })
    return results


SOURCES = [search_gutenberg, search_openlibrary, search_google_books]


def search_all_sources(title, log_fn, on_rate_limit=None, skip_sources=None):
    all_results = []
    for source_fn in SOURCES:
        if skip_sources and (
            source_fn in skip_sources or getattr(source_fn, "__name__", "") in skip_sources
        ):
            continue
        try:
            if getattr(source_fn, "__name__", "") == "search_google_books":
                found = source_fn(title, log_fn=log_fn, on_rate_limit=on_rate_limit)
            else:
                found = source_fn(title, log_fn=log_fn)
            all_results.extend(found)
        except Exception as exc:  # defensive: one bad source shouldn't kill the search
            log_fn(f"  [!] {source_fn.__name__} unexpected error for '{title}': {exc}")
    return all_results


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def _parse_content_length(raw_value) -> int:
    try:
        total = int(raw_value) if raw_value is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(0, total)


def _remove_if_exists(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def download_file(url, dest_path, progress_cb=None):
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
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
        os.replace(part_path, dest_path)
        return True, None
    except (requests.exceptions.RequestException, OSError) as exc:
        if created_part:
            _remove_if_exists(part_path)
        return False, str(exc)
    except Exception:
        if created_part:
            _remove_if_exists(part_path)
        raise


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class BookDownloaderApp:
    # Simple, readable color palette
    BG = "#f4f5f7"
    PANEL_BG = "#ffffff"
    ACCENT = "#2f6fed"
    TEXT = "#1c1f26"
    MUTED = "#6b7280"
    ROW_ALT = "#f0f3fa"

    def __init__(self, root):
        self.root = root
        self.root.title("Free Book Downloader")
        self.root.geometry("1000x640")
        self.root.minsize(760, 480)
        self.root.configure(bg=self.BG)

        self._setup_style()

        # Maps Treeview row id -> result dict (with 'formats')
        self.row_data = {}
        self._searching = False
        self._downloading = False
        self._indeterminate_active = False

        self._build_widgets()

    def _setup_style(self):
        style = ttk.Style(self.root)
        # 'clam' is the most theme-able built-in ttk theme across platforms
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")

        default_font = ("Segoe UI", 10)
        heading_font = ("Segoe UI", 13, "bold")
        subtle_font = ("Segoe UI", 9)

        style.configure(".", font=default_font, background=self.BG)
        style.configure(
            "Header.TLabel", font=heading_font, background=self.BG, foreground=self.TEXT
        )
        style.configure(
            "Subtle.TLabel", font=subtle_font, background=self.BG, foreground=self.MUTED
        )
        style.configure("TFrame", background=self.BG)
        style.configure("Card.TFrame", background=self.PANEL_BG)

        style.configure("TButton", font=default_font, padding=(14, 8))
        style.map("Accent.TButton",
                  background=[("!disabled", self.ACCENT), ("active", "#2559c7")],
                  foreground=[("!disabled", "#ffffff")])
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))

        style.configure("Treeview",
                         font=default_font,
                         rowheight=28,
                         background=self.PANEL_BG,
                         fieldbackground=self.PANEL_BG,
                         foreground=self.TEXT,
                         borderwidth=0)
        style.configure("Treeview.Heading",
                         font=("Segoe UI", 10, "bold"),
                         background="#e7e9ee",
                         foreground=self.TEXT,
                         relief="flat")
        style.map("Treeview",
                  background=[("selected", self.ACCENT)],
                  foreground=[("selected", "#ffffff")])

        style.configure("Horizontal.TProgressbar",
                         troughcolor="#e7e9ee",
                         background=self.ACCENT,
                         thickness=14)

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)  # results area grows/shrinks with window

        # -- header -------------------------------------------------------
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(header, text="Free Book Downloader", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Project Gutenberg  ·  Open Library  ·  Google Books  (public domain only)",
            style="Subtle.TLabel",
        ).pack(anchor="w")

        # -- search bar -----------------------------------------------------
        search_bar = ttk.Frame(outer)
        search_bar.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        search_bar.columnconfigure(0, weight=1)

        self.query_var = tk.StringVar()
        entry = ttk.Entry(search_bar, textvariable=self.query_var, font=("Segoe UI", 11))
        entry.grid(row=0, column=0, sticky="ew", ipady=5)
        entry.insert(0, "")
        entry.bind("<Return>", lambda e: self.start_search())
        self._entry = entry
        self._apply_placeholder()

        self.search_btn = ttk.Button(
            search_bar, text="Search", style="Accent.TButton", command=self.start_search
        )
        self.search_btn.grid(row=0, column=1, padx=(10, 0))

        # -- results table (with scrollbars, so nothing gets clipped) ------
        table_card = ttk.Frame(outer, style="Card.TFrame")
        table_card.grid(row=2, column=0, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)

        columns = ("source", "title", "author", "formats")
        headings = ("Source", "Title", "Author", "Formats")
        self.tree = ttk.Treeview(
            table_card, columns=columns, show="headings", selectmode="extended"
        )
        for col, heading, width in zip(columns, headings, (110, 360, 220, 140)):
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, minwidth=80, anchor="w", stretch=True)
        self.tree.tag_configure("odd", background=self.ROW_ALT)
        self.tree.tag_configure("even", background=self.PANEL_BG)

        vsb = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # -- action bar ------------------------------------------------------
        action_bar = ttk.Frame(outer)
        action_bar.grid(row=3, column=0, sticky="ew", pady=(12, 8))

        self.download_btn = ttk.Button(
            action_bar,
            text="Download Selected",
            style="Accent.TButton",
            command=self.start_download,
        )
        self.download_btn.pack(side="left")

        self.progress = ttk.Progressbar(action_bar, mode="determinate", length=260)
        self.progress.pack(side="left", padx=14)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(action_bar, textvariable=self.status_var, style="Subtle.TLabel").pack(side="left")

        # -- log -------------------------------------------------------------
        log_frame = ttk.LabelFrame(outer, text=" Log ", padding=8)
        log_frame.grid(row=4, column=0, sticky="ew")
        self.log_text = tk.Text(
            log_frame, height=7, state="disabled", wrap="word",
            font=("Consolas", 9), bg=self.PANEL_BG, fg=self.TEXT,
            relief="flat", borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)

    def _apply_placeholder(self):
        placeholder = "e.g. metamorphosis, dracula, moby dick"
        self._entry.insert(0, placeholder)
        self._entry.configure(foreground=self.MUTED)

        def on_focus_in(_event):
            if self._entry.get() == placeholder:
                self._entry.delete(0, "end")
                self._entry.configure(foreground=self.TEXT)

        def on_focus_out(_event):
            if not self._entry.get().strip():
                self._entry.insert(0, placeholder)
                self._entry.configure(foreground=self.MUTED)

        self._entry.bind("<FocusIn>", on_focus_in)
        self._entry.bind("<FocusOut>", on_focus_out)
        self._placeholder = placeholder

    # -- logging helper, safe to call from background threads --------------
    def log(self, message):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _append)

    def set_status(self, text):
        self.root.after(0, self.status_var.set, text)

    def _reset_progress(self):
        self._indeterminate_active = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)

    def _start_progress_indeterminate(self):
        if not self._indeterminate_active:
            self._indeterminate_active = True
            self.progress.configure(mode="indeterminate")
            self.progress.start()

    def _set_progress_determinate(self, pct):
        if self._indeterminate_active:
            self._indeterminate_active = False
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress.configure(value=pct)

    # -- search --------------------------------------------------------
    def start_search(self):
        if self._searching or self._downloading:
            return
        raw = self.query_var.get().strip()
        if not raw or raw == self._placeholder:
            messagebox.showinfo("No input", "Enter at least one book title.")
            return
        titles = [t.strip() for t in raw.split(",") if t.strip()]
        if not titles:
            messagebox.showinfo("No input", "Enter at least one book title.")
            return

        self._searching = True
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        self.search_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self.set_status("Searching...")

        threading.Thread(target=self._search_worker, args=(titles,), daemon=True).start()

    def _search_worker(self, titles):
        google_rate_limited = False
        try:
            skip_sources = set()

            def on_rate_limit():
                nonlocal google_rate_limited
                google_rate_limited = True
                skip_sources.add(search_google_books)
                self.set_status("Searching... (Google Books rate-limited: HTTP 429)")

            for title in titles:
                self.log(f"Searching for: {title}")
                results = search_all_sources(
                    title,
                    self.log,
                    on_rate_limit=on_rate_limit,
                    skip_sources=skip_sources,
                )
                if not results:
                    self.log(f"  No freely downloadable matches found for '{title}'.")
                for r in results:
                    self.root.after(0, self._add_row, r)
        finally:
            status = (
                "Search complete (Google Books rate-limited: HTTP 429 — try again later)."
                if google_rate_limited
                else "Search complete."
            )
            self.root.after(0, self._finish_search, status)

    def _finish_search(self, status_text):
        self._searching = False
        self.status_var.set(status_text)
        self.search_btn.configure(state="normal")
        self.download_btn.configure(state="normal")

    def _add_row(self, result):
        fmt_labels = ", ".join(result["formats"].keys())
        row_count = len(self.tree.get_children())
        tag = "even" if row_count % 2 == 0 else "odd"
        row_id = self.tree.insert(
            "", "end",
            values=(result["source"], result["title"], result["author"], fmt_labels),
            tags=(tag,),
        )
        self.row_data[row_id] = result

    # -- download --------------------------------------------------------
    def start_download(self):
        if self._searching or self._downloading:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows to download.")
            return
        self._downloading = True
        self.download_btn.configure(state="disabled")
        self.search_btn.configure(state="disabled")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        threading.Thread(target=self._download_worker, args=(selected,), daemon=True).start()

    def _download_worker(self, row_ids):
        downloaded, failed = 0, 0
        try:
            for row_id in row_ids:
                result = self.row_data.get(row_id)
                if not result:
                    continue
                label, url = self._pick_format(result["formats"])
                ext = FORMAT_EXTENSIONS.get(label, "bin")
                dest_path = build_dest_path(result, ext, download_dir=DOWNLOAD_DIR)

                self.set_status(f"Downloading {result['title']} ({label})...")
                self.log(f"Downloading: {result['title']} [{result['source']}, {label}]")

                last_pct = -1
                indeterminate_sent = False

                def progress_cb(downloaded_bytes, total_bytes):
                    nonlocal last_pct, indeterminate_sent
                    if total_bytes > 0:
                        pct = min(100, downloaded_bytes * 100 // total_bytes)
                        if pct != last_pct:
                            last_pct = pct
                            self.root.after(0, self._set_progress_determinate, pct)
                    elif not indeterminate_sent:
                        indeterminate_sent = True
                        self.root.after(0, self._start_progress_indeterminate)

                self.root.after(0, self._reset_progress)
                ok, err = download_file(url, dest_path, progress_cb)
                self.root.after(0, self._reset_progress)
                if ok:
                    downloaded += 1
                    self.log(f"  [OK] Saved to {dest_path}")
                else:
                    failed += 1
                    self.log(f"  [!] Failed: {err}")
        finally:
            self.root.after(0, self._finish_download, downloaded, failed)

    def _finish_download(self, downloaded, failed):
        self._reset_progress()
        self._downloading = False
        self.status_var.set(f"Done. Downloaded {downloaded}, failed {failed}.")
        self.download_btn.configure(state="normal")
        self.search_btn.configure(state="normal")
        messagebox.showinfo(
            "Download summary", f"Downloaded: {downloaded}\nFailed: {failed}"
        )

    @staticmethod
    def _pick_format(formats):
        for label in FORMAT_PRIORITY:
            if label in formats:
                return label, formats[label]
        # fall back to whatever is available
        label = next(iter(formats))
        return label, formats[label]


def main():
    if tk is None:
        raise RuntimeError(
            "tkinter is not installed. Install python3-tk (e.g. `sudo apt install python3-tk`)."
        )
    root = tk.Tk()
    BookDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
