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

import os
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox

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


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s\-\.]", "", name)
    return re.sub(r"\s+", "_", name).strip("_")[:100]


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
        data = resp.json().get("results", [])
    except requests.exceptions.RequestException as exc:
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
        authors = ", ".join(a["name"] for a in book.get("authors", [])) or "Unknown"
        results.append({
            "source": "Gutenberg",
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
        docs = resp.json().get("docs", [])
    except requests.exceptions.RequestException as exc:
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

        authors = ", ".join(doc.get("author_name", [])) or "Unknown"
        results.append({
            "source": "Open Library",
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
    try:
        resp = SESSION.get(
            f"https://archive.org/metadata/{ia_id}",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        meta = resp.json()
    except requests.exceptions.RequestException as exc:
        if log_fn:
            log_fn(f"  [!] Internet Archive error for '{ia_id}': {exc}")
        return {}

    if meta.get("metadata", {}).get("access-restricted-item") in ("true", True):
        return {}  # borrow-only item; skip entirely

    formats = {}
    for f in meta.get("files", []):
        fmt = (f.get("format") or "").upper()
        name = f.get("name", "")
        if not name:
            continue
        url = f"https://archive.org/download/{ia_id}/{name}"
        if "EPUB" in fmt and "EPUB" not in formats:
            formats["EPUB"] = url
        elif fmt == "TEXT PDF" or fmt == "PDF" or ("PDF" in fmt and "PDF" not in formats):
            formats.setdefault("PDF", url)
        elif "DJVUTXT" in fmt.replace(" ", "") and "TXT" not in formats:
            formats["TXT"] = url
    return formats


# ---------------------------------------------------------------------------
# Source: Google Books (public-domain, directly downloadable items only)
# ---------------------------------------------------------------------------

def search_google_books(title, log_fn=None):
    results = []
    try:
        resp = SESSION.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": title, "maxResults": 5},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except requests.exceptions.RequestException as exc:
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

        authors = ", ".join(info.get("authors", [])) or "Unknown"
        results.append({
            "source": "Google Books",
            "title": info.get("title", title),
            "author": authors,
            "formats": formats,
        })
    return results


SOURCES = [search_gutenberg, search_openlibrary, search_google_books]


def search_all_sources(title, log_fn):
    all_results = []
    for source_fn in SOURCES:
        try:
            found = source_fn(title, log_fn=log_fn)
            all_results.extend(found)
        except Exception as exc:  # defensive: one bad source shouldn't kill the search
            log_fn(f"  [!] {source_fn.__name__} unexpected error for '{title}': {exc}")
    return all_results


# ---------------------------------------------------------------------------
# Download helper
# ---------------------------------------------------------------------------

def download_file(url, dest_path, progress_cb):
    try:
        with SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress_cb(downloaded, total)
        return True, None
    except requests.exceptions.RequestException as exc:
        return False, str(exc)


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

        self._build_widgets()

    def _setup_style(self):
        style = ttk.Style(self.root)
        # 'clam' is the most theme-able built-in ttk theme across platforms
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        default_font = ("Segoe UI", 10)
        heading_font = ("Segoe UI", 13, "bold")
        subtle_font = ("Segoe UI", 9)

        style.configure(".", font=default_font, background=self.BG)
        style.configure("Header.TLabel", font=heading_font, background=self.BG, foreground=self.TEXT)
        style.configure("Subtle.TLabel", font=subtle_font, background=self.BG, foreground=self.MUTED)
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
            action_bar, text="Download Selected", style="Accent.TButton", command=self.start_download
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
        self.root.after(0, lambda: self.status_var.set(text))

    # -- search --------------------------------------------------------
    def start_search(self):
        raw = self.query_var.get().strip()
        if not raw or raw == self._placeholder:
            messagebox.showinfo("No input", "Enter at least one book title.")
            return
        titles = [t.strip() for t in raw.split(",") if t.strip()]

        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        self.search_btn.configure(state="disabled")
        self.set_status("Searching...")

        threading.Thread(target=self._search_worker, args=(titles,), daemon=True).start()

    def _search_worker(self, titles):
        for title in titles:
            self.log(f"Searching for: {title}")
            results = search_all_sources(title, self.log)
            if not results:
                self.log(f"  No freely downloadable matches found for '{title}'.")
            for r in results:
                self.root.after(0, self._add_row, r)
        self.set_status("Search complete.")
        self.root.after(0, lambda: self.search_btn.configure(state="normal"))

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
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows to download.")
            return
        self.download_btn.configure(state="disabled")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        threading.Thread(target=self._download_worker, args=(selected,), daemon=True).start()

    def _download_worker(self, row_ids):
        downloaded, failed = 0, 0
        for row_id in row_ids:
            result = self.row_data.get(row_id)
            if not result:
                continue
            label, url = self._pick_format(result["formats"])
            ext = {"EPUB": "epub", "PDF": "pdf", "TXT": "txt", "MOBI": "mobi"}.get(label, "bin")
            filename = f"{sanitize_filename(result['source'])}_{sanitize_filename(result['title'])}.{ext}"
            dest_path = os.path.join(DOWNLOAD_DIR, filename)

            self.set_status(f"Downloading {result['title']} ({label})...")
            self.log(f"Downloading: {result['title']} [{result['source']}, {label}]")

            def progress_cb(downloaded_bytes, total_bytes):
                if total_bytes:
                    pct = downloaded_bytes * 100 // total_bytes
                    self.root.after(0, lambda: self.progress.configure(value=pct))
                else:
                    self.root.after(0, lambda: self.progress.configure(mode="indeterminate"))

            self.root.after(0, lambda: self.progress.configure(mode="determinate", value=0))
            ok, err = download_file(url, dest_path, progress_cb)
            if ok:
                downloaded += 1
                self.log(f"  [OK] Saved to {dest_path}")
            else:
                failed += 1
                self.log(f"  [!] Failed: {err}")

        self.root.after(0, lambda: self.progress.configure(value=0))
        self.set_status(f"Done. Downloaded {downloaded}, failed {failed}.")
        self.root.after(0, lambda: self.download_btn.configure(state="normal"))
        self.root.after(0, lambda: messagebox.showinfo(
            "Download summary", f"Downloaded: {downloaded}\nFailed: {failed}"
        ))

    @staticmethod
    def _pick_format(formats):
        for label in FORMAT_PRIORITY:
            if label in formats:
                return label, formats[label]
        # fall back to whatever is available
        label = next(iter(formats))
        return label, formats[label]


def main():
    root = tk.Tk()
    app = BookDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
