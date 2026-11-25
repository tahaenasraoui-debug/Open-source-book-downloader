"""Tkinter desktop UI for Free Book Downloader."""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import webbrowser

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - allows headless unit tests without python3-tk
    tk = None
    ttk = None
    messagebox = None
    filedialog = None

from book_downloader.downloader import (
    FORMAT_EXTENSIONS,
    build_dest_path,
    default_download_dir,
    download_file,
    open_folder,
    pick_format,
)
from book_downloader.sources import (
    SOURCE_NAMES,
    search_all_sources,
    search_google_books,
)


def enable_hidpi() -> None:
    """Enable sharp DPI scaling on Windows without failing on other OSes."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]


def _create_app_icon(root) -> object | None:
    """Build a crisp minimal 16x16 book icon via PhotoImage (no external asset)."""
    if tk is None:
        return None
    try:
        icon = tk.PhotoImage(width=16, height=16, master=root)
        icon.put("#2563eb", to=(2, 1, 14, 15))
        icon.put("#1d4ed8", to=(2, 1, 5, 15))
        icon.put("#ffffff", to=(6, 3, 12, 13))
        icon.put("#93c5fd", to=(7, 5, 11, 6))
        icon.put("#93c5fd", to=(7, 8, 11, 9))
        return icon
    except tk.TclError:
        return None


class BookDownloaderApp:
    # Clean slate-and-blue desktop palette
    BG = "#f1f5f9"
    PANEL_BG = "#ffffff"
    BORDER = "#e2e8f0"
    ACCENT = "#2563eb"
    ACCENT_HOVER = "#1d4ed8"
    DANGER = "#dc2626"
    DANGER_HOVER = "#b91c1c"
    TEXT = "#0f172a"
    MUTED = "#64748b"
    ROW_ALT = "#f8fafc"

    PLACEHOLDER = "e.g. metamorphosis, dracula, moby dick"
    COLUMNS = ("source", "title", "author", "year", "lang", "formats")
    HEADINGS = {
        "source": "Source",
        "title": "Title",
        "author": "Author",
        "year": "Year",
        "lang": "Lang",
        "formats": "Formats",
    }
    COL_WIDTHS = {
        "source": 105,
        "title": 330,
        "author": 195,
        "year": 65,
        "lang": 70,
        "formats": 130,
    }

    def __init__(self, root):
        self.root = root
        self.root.title("Free Book Downloader")
        self.root.geometry("1080x700")
        self.root.minsize(820, 520)
        self.root.configure(bg=self.BG)

        self._icon = _create_app_icon(self.root)
        if self._icon is not None:
            with contextlib.suppress(tk.TclError):
                self.root.iconphoto(True, self._icon)

        self._setup_style()

        self.row_data: dict[str, dict] = {}
        self._searching = False
        self._downloading = False
        self._indeterminate_active = False
        self._cancel_event = threading.Event()
        self._sort_state: tuple[str | None, bool] = (None, False)
        self._placeholder = self.PLACEHOLDER

        self._build_widgets()

    def _setup_style(self):
        style = ttk.Style(self.root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")

        default_font = ("Segoe UI", 10)
        heading_font = ("Segoe UI", 14, "bold")
        subtle_font = ("Segoe UI", 9)

        style.configure(".", font=default_font, background=self.BG, foreground=self.TEXT)
        style.configure(
            "Header.TLabel", font=heading_font, background=self.BG, foreground=self.TEXT
        )
        style.configure(
            "Subtle.TLabel", font=subtle_font, background=self.BG, foreground=self.MUTED
        )
        style.configure("TFrame", background=self.BG)
        style.configure(
            "Card.TFrame",
            background=self.PANEL_BG,
            borderwidth=1,
            relief="solid",
        )
        style.configure("Inner.TFrame", background=self.PANEL_BG)
        style.configure(
            "Card.TCheckbutton", background=self.BG, foreground=self.TEXT, font=subtle_font
        )
        style.configure(
            "CardLabel.TLabel", background=self.PANEL_BG, foreground=self.MUTED, font=subtle_font
        )

        style.configure("TButton", font=default_font, padding=(12, 7))
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(14, 7),
            background=self.ACCENT,
            foreground="#ffffff",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#94a3b8"),
                ("active", self.ACCENT_HOVER),
                ("!disabled", self.ACCENT),
            ],
            foreground=[("disabled", "#f8fafc"), ("!disabled", "#ffffff")],
        )
        style.configure(
            "Danger.TButton",
            font=default_font,
            padding=(12, 7),
        )
        style.map(
            "Danger.TButton",
            foreground=[("disabled", self.MUTED), ("!disabled", self.DANGER)],
        )

        style.configure(
            "Treeview",
            font=default_font,
            rowheight=28,
            background=self.PANEL_BG,
            fieldbackground=self.PANEL_BG,
            foreground=self.TEXT,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background="#e2e8f0",
            foreground=self.TEXT,
            relief="flat",
            padding=(8, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", self.ACCENT)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#e2e8f0",
            background=self.ACCENT,
            thickness=14,
        )

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # -- Header + Source filter checkboxes -------------------------------
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)

        title_box = ttk.Frame(header)
        title_box.grid(row=0, column=0, sticky="w")
        ttk.Label(title_box, text="Free Book Downloader", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text="Public-domain books from Project Gutenberg, Open Library & Google Books",
            style="Subtle.TLabel",
        ).pack(anchor="w")

        sources_box = ttk.Frame(header)
        sources_box.grid(row=0, column=1, sticky="e")
        ttk.Label(sources_box, text="Sources:", style="Subtle.TLabel").pack(
            side="left", padx=(0, 6)
        )
        self.source_vars: dict[str, tk.BooleanVar] = {}
        for name in SOURCE_NAMES:
            var = tk.BooleanVar(value=True)
            self.source_vars[name] = var
            ttk.Checkbutton(
                sources_box,
                text=name,
                variable=var,
                style="Card.TCheckbutton",
            ).pack(side="left", padx=(0, 8))

        # -- Search + Options Card -------------------------------------------
        controls_card = ttk.Frame(outer, style="Card.TFrame", padding=12)
        controls_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        controls_card.columnconfigure(0, weight=1)

        search_row = ttk.Frame(controls_card, style="Inner.TFrame")
        search_row.grid(row=0, column=0, sticky="ew")
        search_row.columnconfigure(0, weight=1)

        self.query_var = tk.StringVar()
        entry = ttk.Entry(search_row, textvariable=self.query_var, font=("Segoe UI", 11))
        entry.grid(row=0, column=0, sticky="ew", ipady=5)
        entry.bind("<Return>", lambda _e: self.start_search())
        self._entry = entry
        self._apply_placeholder()

        self.search_btn = ttk.Button(
            search_row, text="Search", style="Accent.TButton", command=self.start_search
        )
        self.search_btn.grid(row=0, column=1, padx=(10, 6))

        self.cancel_btn = ttk.Button(
            search_row,
            text="Cancel",
            style="Danger.TButton",
            command=self.cancel_active,
            state="disabled",
        )
        self.cancel_btn.grid(row=0, column=2)

        options_row = ttk.Frame(controls_card, style="Inner.TFrame")
        options_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        options_row.columnconfigure(3, weight=1)

        ttk.Label(options_row, text="Preferred format:", style="CardLabel.TLabel").grid(
            row=0, column=0, padx=(0, 6)
        )
        self.format_var = tk.StringVar(value="Auto")
        format_box = ttk.Combobox(
            options_row,
            textvariable=self.format_var,
            values=("Auto", "EPUB", "PDF", "TXT", "MOBI"),
            state="readonly",
            width=9,
        )
        format_box.grid(row=0, column=1, padx=(0, 16))

        ttk.Label(options_row, text="Save to:", style="CardLabel.TLabel").grid(
            row=0, column=2, padx=(0, 6)
        )
        self.download_dir_var = tk.StringVar(value=default_download_dir())
        dir_entry = ttk.Entry(options_row, textvariable=self.download_dir_var, font=("Segoe UI", 9))
        dir_entry.grid(row=0, column=3, sticky="ew", ipady=2)

        ttk.Button(options_row, text="Browse...", command=self._choose_download_dir).grid(
            row=0, column=4, padx=(8, 4)
        )
        ttk.Button(options_row, text="Open Folder", command=self._open_download_dir).grid(
            row=0, column=5
        )

        # -- Results Header (counts + shortcut hint) -------------------------
        meta_bar = ttk.Frame(outer)
        meta_bar.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        meta_bar.columnconfigure(0, weight=1)
        self.count_var = tk.StringVar(value="0 results · 0 selected")
        ttk.Label(meta_bar, textvariable=self.count_var, style="Subtle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            meta_bar,
            text="Tip: Click headers to sort · Ctrl+A select all · Right-click for actions",
            style="Subtle.TLabel",
        ).grid(row=0, column=1, sticky="e")

        # -- Results Table ---------------------------------------------------
        table_card = ttk.Frame(outer, style="Card.TFrame")
        table_card.grid(row=3, column=0, sticky="nsew")
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_card, columns=self.COLUMNS, show="headings", selectmode="extended"
        )
        for col in self.COLUMNS:
            self.tree.heading(
                col,
                text=self.HEADINGS[col],
                command=lambda c=col: self._sort_by_column(c),
            )
            self.tree.column(
                col,
                width=self.COL_WIDTHS[col],
                minwidth=55,
                anchor="center" if col in ("year", "lang") else "w",
                stretch=col in ("title", "author"),
            )
        self.tree.tag_configure("odd", background=self.ROW_ALT)
        self.tree.tag_configure("even", background=self.PANEL_BG)

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_counts())
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Control-A>", self._select_all_rows)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected_info())
        self.tree.bind("<Button-3>", self._show_context_menu)

        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="Download Selected", command=self.start_download)
        self._menu.add_command(label="Open Book Page in Browser", command=self._open_selected_info)
        self._menu.add_command(label="Copy Book Page URL", command=self._copy_selected_url)
        self._menu.add_separator()
        self._menu.add_command(label="Select All", command=self._select_all_rows)

        vsb = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # -- Action + Progress Bar -------------------------------------------
        action_bar = ttk.Frame(outer)
        action_bar.grid(row=4, column=0, sticky="ew", pady=(10, 8))

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

        # -- Activity Log Card -----------------------------------------------
        log_header = ttk.Frame(outer)
        log_header.grid(row=5, column=0, sticky="ew")
        ttk.Label(log_header, text="Activity Log", style="Subtle.TLabel").pack(side="left")
        ttk.Button(log_header, text="Clear Log", command=self._clear_log).pack(side="right")

        log_card = ttk.Frame(outer, style="Card.TFrame", padding=6)
        log_card.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self.log_text = tk.Text(
            log_card,
            height=6,
            state="disabled",
            wrap="word",
            font=("Consolas", 9),
            bg=self.PANEL_BG,
            fg=self.TEXT,
            relief="flat",
            borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)

    def _apply_placeholder(self):
        self._entry.insert(0, self.PLACEHOLDER)
        self._entry.configure(foreground=self.MUTED)

        def on_focus_in(_event):
            if self._entry.get() == self.PLACEHOLDER:
                self._entry.delete(0, "end")
                self._entry.configure(foreground=self.TEXT)

        def on_focus_out(_event):
            if not self._entry.get().strip():
                self._entry.insert(0, self.PLACEHOLDER)
                self._entry.configure(foreground=self.MUTED)

        self._entry.bind("<FocusIn>", on_focus_in)
        self._entry.bind("<FocusOut>", on_focus_out)

    # -- Helpers -------------------------------------------------------------
    def _choose_download_dir(self):
        if filedialog is None:
            return
        initial = self.download_dir_var.get().strip() or default_download_dir()
        chosen = filedialog.askdirectory(initialdir=initial, title="Choose Download Folder")
        if chosen:
            self.download_dir_var.set(chosen)

    def _open_download_dir(self):
        target = self.download_dir_var.get().strip() or default_download_dir()
        open_folder(target)

    def _get_download_dir(self) -> str:
        var = getattr(self, "download_dir_var", None)
        if var is not None:
            val = var.get().strip()
            if val:
                return val
        return default_download_dir()

    def _get_preferred_format(self) -> str:
        var = getattr(self, "format_var", None)
        return var.get().strip() if var is not None else "Auto"

    def _get_enabled_sources(self) -> set | None:
        source_vars = getattr(self, "source_vars", None)
        if not source_vars:
            return None
        return {SOURCE_NAMES[name] for name, var in source_vars.items() if var.get()}

    def _update_counts(self):
        count_var = getattr(self, "count_var", None)
        if count_var is None:
            return
        total = len(self.tree.get_children())
        selected = len(self.tree.selection())
        count_var.set(f"{total} result{'s' if total != 1 else ''} · {selected} selected")

    def _select_all_rows(self, _event=None):
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children)
            self._update_counts()
        return "break"

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
                self._update_counts()
            self._menu.tk_popup(event.x_root, event.y_root)

    def _open_selected_info(self):
        selected = self.tree.selection()
        if not selected:
            return
        result = self.row_data.get(selected[0]) or {}
        url = result.get("info_url")
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("No link", "No web page URL is available for this entry.")

    def _copy_selected_url(self):
        selected = self.tree.selection()
        if not selected:
            return
        result = self.row_data.get(selected[0]) or {}
        url = result.get("info_url") or ""
        if url:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.set_status("Copied book page URL to clipboard.")

    def _sort_by_column(self, col: str):
        prev_col, prev_desc = self._sort_state
        desc = (not prev_desc) if prev_col == col else False
        self._sort_state = (col, desc)

        idx = self.COLUMNS.index(col)
        items = [(self.tree.item(k, "values")[idx], k) for k in self.tree.get_children()]

        def _sort_key(pair):
            val = str(pair[0] or "")
            if col == "year":
                return (0, int(val)) if val.isdigit() else (1, 0)
            return (0, val.lower())

        items.sort(key=_sort_key, reverse=desc)
        for pos, (_, row_id) in enumerate(items):
            self.tree.move(row_id, "", pos)
            self.tree.item(row_id, tags=("even" if pos % 2 == 0 else "odd",))

        for c in self.COLUMNS:
            arrow = (" ▼" if desc else " ▲") if c == col else ""
            self.tree.heading(c, text=f"{self.HEADINGS[c]}{arrow}")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def log(self, message):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _append)

    def set_status(self, text):
        self.root.after(0, self.status_var.set, text)

    def _set_cancel_enabled(self, enabled: bool):
        cancel_btn = getattr(self, "cancel_btn", None)
        if cancel_btn is not None:
            cancel_btn.configure(state="normal" if enabled else "disabled")

    def cancel_active(self):
        cancel_ev = getattr(self, "_cancel_event", None)
        if cancel_ev is not None and (self._searching or self._downloading):
            cancel_ev.set()
            self.set_status("Cancelling...")
            self._set_cancel_enabled(False)

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

    # -- Search --------------------------------------------------------------
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

        enabled_sources = self._get_enabled_sources()
        if enabled_sources is not None and not enabled_sources:
            messagebox.showinfo("No sources", "Enable at least one book source to search.")
            return

        if not hasattr(self, "_cancel_event") or self._cancel_event is None:
            self._cancel_event = threading.Event()
        self._cancel_event.clear()

        self._searching = True
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        self._update_counts()
        self.search_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self._set_cancel_enabled(True)
        self.set_status("Searching...")

        threading.Thread(
            target=self._search_worker,
            args=(titles, enabled_sources),
            daemon=True,
        ).start()

    def _search_worker(self, titles, enabled_sources=None):
        google_rate_limited = False
        cancel_ev = getattr(self, "_cancel_event", None)
        try:
            skip_sources = set()

            def on_rate_limit():
                nonlocal google_rate_limited
                google_rate_limited = True
                skip_sources.add("search_google_books")
                skip_sources.add(search_google_books)
                self.set_status("Searching... (Google Books rate-limited: HTTP 429)")

            for title in titles:
                if cancel_ev and cancel_ev.is_set():
                    break
                self.log(f"Searching for: {title}")
                results = search_all_sources(
                    title,
                    self.log,
                    on_rate_limit=on_rate_limit,
                    skip_sources=skip_sources,
                    enabled_sources=enabled_sources,
                    cancel_event=cancel_ev,
                )
                if cancel_ev and cancel_ev.is_set():
                    break
                if not results:
                    self.log(f"  No freely downloadable matches found for '{title}'.")
                for r in results:
                    self.root.after(0, self._add_row, r)
        finally:
            if cancel_ev and cancel_ev.is_set():
                status = "Search cancelled."
            elif google_rate_limited:
                status = "Search complete (Google Books rate-limited: HTTP 429 — try again later)."
            else:
                status = "Search complete."
            self.root.after(0, self._finish_search, status)

    def _finish_search(self, status_text):
        self._searching = False
        self.status_var.set(status_text)
        self.search_btn.configure(state="normal")
        self.download_btn.configure(state="normal")
        self._set_cancel_enabled(False)
        self._update_counts()

    def _add_row(self, result):
        fmt_labels = ", ".join(result["formats"].keys())
        row_count = len(self.tree.get_children())
        tag = "even" if row_count % 2 == 0 else "odd"
        row_id = self.tree.insert(
            "",
            "end",
            values=(
                result["source"],
                result["title"],
                result["author"],
                result.get("year", "—"),
                result.get("language", "—"),
                fmt_labels,
            ),
            tags=(tag,),
        )
        self.row_data[row_id] = result
        self._update_counts()

    # -- Download ------------------------------------------------------------
    def start_download(self):
        if self._searching or self._downloading:
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected", "Select one or more rows to download.")
            return

        if not hasattr(self, "_cancel_event") or self._cancel_event is None:
            self._cancel_event = threading.Event()
        self._cancel_event.clear()

        dest_dir = self._get_download_dir()
        os.makedirs(dest_dir, exist_ok=True)
        preferred_fmt = self._get_preferred_format()

        self._downloading = True
        self.download_btn.configure(state="disabled")
        self.search_btn.configure(state="disabled")
        self._set_cancel_enabled(True)
        threading.Thread(
            target=self._download_worker,
            args=(selected, dest_dir, preferred_fmt),
            daemon=True,
        ).start()

    def _download_worker(self, row_ids, dest_dir=None, preferred_fmt="Auto"):
        downloaded, failed = 0, 0
        target_dir = dest_dir or self._get_download_dir()
        cancel_ev = getattr(self, "_cancel_event", None)
        try:
            for row_id in row_ids:
                if cancel_ev and cancel_ev.is_set():
                    break
                result = self.row_data.get(row_id)
                if not result:
                    continue
                label, url = self._pick_format(result["formats"], preferred=preferred_fmt)
                ext = FORMAT_EXTENSIONS.get(label, "bin")
                dest_path = build_dest_path(result, ext, download_dir=target_dir)

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
                ok, err = download_file(url, dest_path, progress_cb, cancel_event=cancel_ev)
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
        cancel_ev = getattr(self, "_cancel_event", None)
        cancelled = bool(cancel_ev and cancel_ev.is_set())
        if cancelled:
            self.status_var.set(
                f"Cancelled. Downloaded {downloaded}, stopped/failed {failed}."
            )
        else:
            self.status_var.set(f"Done. Downloaded {downloaded}, failed {failed}.")
        self.download_btn.configure(state="normal")
        self.search_btn.configure(state="normal")
        self._set_cancel_enabled(False)
        prefix = "Cancelled.\n" if cancelled else ""
        messagebox.showinfo(
            "Download summary",
            f"{prefix}Downloaded: {downloaded}\nFailed: {failed}",
        )

    @staticmethod
    def _pick_format(formats, preferred="Auto"):
        return pick_format(formats, preferred=preferred)
