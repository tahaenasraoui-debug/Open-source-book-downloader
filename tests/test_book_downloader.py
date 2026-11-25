import threading
from unittest.mock import MagicMock

import pytest
import requests

import book_downloader.downloader as dl_mod
import book_downloader.sources as src_mod
import book_downloader.ui as ui_mod
import book_downloader_gui as app_mod


class _FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeWidget:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


class _FakeProgress(_FakeWidget):
    def __init__(self):
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0
        self.modes = []

    def configure(self, **kwargs):
        super().configure(**kwargs)
        if "mode" in kwargs:
            self.modes.append(kwargs["mode"])

    def start(self, interval=None):
        self.start_calls += 1

    def stop(self):
        self.stop_calls += 1


class _FakeTree:
    def __init__(self):
        self.rows = {}
        self._selected = ()
        self._counter = 0

    def get_children(self):
        return tuple(self.rows.keys())

    def delete(self, *items):
        for item in items:
            self.rows.pop(item, None)

    def insert(self, _parent, _index, values=(), tags=()):
        self._counter += 1
        row_id = f"row-{self._counter}"
        self.rows[row_id] = {"values": values, "tags": tags}
        return row_id

    def selection(self):
        return self._selected

    def selection_set(self, items):
        self._selected = tuple(items)

    def item(self, row_id, option=None, **kwargs):
        if kwargs:
            self.rows[row_id].update(kwargs)
            return None
        if option == "values":
            return self.rows[row_id]["values"]
        return self.rows[row_id]

    def move(self, row_id, _parent, index):
        items = list(self.rows.items())
        items = [pair for pair in items if pair[0] != row_id]
        items.insert(index, (row_id, self.rows[row_id]))
        self.rows = dict(items)

    def heading(self, _col, **_kwargs):
        pass


class _FakeText(_FakeWidget):
    def __init__(self):
        super().__init__()
        self.lines = []

    def insert(self, _index, text):
        self.lines.append(text)

    def see(self, _index):
        pass


class _FakeRoot:
    def after(self, _delay, func, *args):
        func(*args)


def _make_headless_app():
    app = app_mod.BookDownloaderApp.__new__(app_mod.BookDownloaderApp)
    app.root = _FakeRoot()
    app.row_data = {}
    app._searching = False
    app._downloading = False
    app._indeterminate_active = False
    app._placeholder = "e.g. metamorphosis, dracula, moby dick"
    app.query_var = _FakeVar("")
    app.status_var = _FakeVar("Ready.")
    app.search_btn = _FakeWidget()
    app.download_btn = _FakeWidget()
    app.tree = _FakeTree()
    app.progress = _FakeProgress()
    app.log_text = _FakeText()
    return app


def _mock_stream_response(chunks, headers=None, error=None, status_error=None):
    resp = MagicMock()
    resp.headers = headers or {}
    if status_error is not None:
        resp.raise_for_status.side_effect = status_error
    else:
        resp.raise_for_status.return_value = None

    def _iter_content(chunk_size=8192):
        yield from chunks
        if error is not None:
            raise error

    resp.iter_content.side_effect = _iter_content
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = False
    return ctx


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

def test_sanitize_filename_basic_and_punctuation():
    assert app_mod.sanitize_filename("Moby Dick") == "Moby_Dick"
    assert app_mod.sanitize_filename("Kafka, Franz: Metamorphosis!") == "Kafka_Franz_Metamorphosis"
    assert app_mod.sanitize_filename("Dr. Jekyll & Mr. Hyde") == "Dr._Jekyll_Mr._Hyde"


def test_sanitize_filename_prevents_path_traversal_and_empty():
    sanitized = app_mod.sanitize_filename("../../etc/passwd")
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert ".." not in sanitized
    assert sanitized == "etc_passwd"

    assert app_mod.sanitize_filename("") == "untitled"
    assert app_mod.sanitize_filename("   ") == "untitled"
    assert app_mod.sanitize_filename("///...") == "untitled"


def test_sanitize_filename_truncation_strips_trailing_separators():
    raw = ("a" * 99) + " " + ("b" * 20)
    sanitized = app_mod.sanitize_filename(raw, max_len=100)
    assert len(sanitized) <= 100
    assert not sanitized.endswith(("_", "."))
    assert sanitized == "a" * 99


# ---------------------------------------------------------------------------
# Collision-safe filename generation
# ---------------------------------------------------------------------------

def test_build_dest_path_distinguishes_authors_and_ids(tmp_path):
    book_a = {
        "source": "Gutenberg",
        "id": "5200",
        "title": "Metamorphosis",
        "author": "Kafka, Franz",
    }
    book_b = {
        "source": "Gutenberg",
        "id": "9999",
        "title": "Metamorphosis",
        "author": "De Vet, Charles V.",
    }
    book_c = {
        "source": "Gutenberg",
        "id": "21279",
        "title": "Metamorphosis",
        "author": "Kafka, Franz",
    }

    path_a = app_mod.build_dest_path(book_a, "epub", download_dir=str(tmp_path))
    path_b = app_mod.build_dest_path(book_b, "epub", download_dir=str(tmp_path))
    path_c = app_mod.build_dest_path(book_c, "epub", download_dir=str(tmp_path))

    assert len({path_a, path_b, path_c}) == 3
    assert "Kafka_Franz" in path_a
    assert "5200" in path_a
    assert "De_Vet_Charles_V" in path_b
    assert "21279" in path_c


def test_build_dest_path_appends_dedup_suffix_for_existing_and_part_files(tmp_path):
    book = {
        "source": "Open Library",
        "title": "Dracula",
        "author": "Bram Stoker",
    }

    first = app_mod.build_dest_path(book, "epub", download_dir=str(tmp_path))
    assert first.endswith("Open_Library_Dracula_Bram_Stoker.epub")

    with open(first, "wb") as f:
        f.write(b"book-1")

    second = app_mod.build_dest_path(book, "epub", download_dir=str(tmp_path))
    assert second.endswith("Open_Library_Dracula_Bram_Stoker_2.epub")

    # An active .part file should also force advancing to _3.
    with open(f"{second}.part", "wb") as f:
        f.write(b"partial")

    third = app_mod.build_dest_path(book, "epub", download_dir=str(tmp_path))
    assert third.endswith("Open_Library_Dracula_Bram_Stoker_3.epub")


# ---------------------------------------------------------------------------
# Archive.org URL encoding
# ---------------------------------------------------------------------------

def test_fetch_archive_org_formats_url_encodes_filenames(monkeypatch):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "metadata": {"access-restricted-item": "false"},
        "files": [
            {"format": "EPUB", "name": "My Book (1920) #1?.epub"},
            {"format": "Text PDF", "name": "scans/Chapter 1 & 2.pdf"},
            {"format": "DjVuTXT", "name": "100% clean.txt"},
        ],
    }
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=resp))

    formats = app_mod.fetch_archive_org_formats("item id/with space")

    assert formats["EPUB"] == (
        "https://archive.org/download/item%20id%2Fwith%20space/"
        "My%20Book%20%281920%29%20%231%3F.epub"
    )
    assert formats["PDF"] == (
        "https://archive.org/download/item%20id%2Fwith%20space/"
        "scans/Chapter%201%20%26%202.pdf"
    )
    assert formats["TXT"] == (
        "https://archive.org/download/item%20id%2Fwith%20space/"
        "100%25%20clean.txt"
    )


def test_fetch_archive_org_formats_skips_restricted_items(monkeypatch):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "metadata": {"access-restricted-item": "true"},
        "files": [{"format": "EPUB", "name": "book.epub"}],
    }
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=resp))

    assert app_mod.fetch_archive_org_formats("restricted_id") == {}


# ---------------------------------------------------------------------------
# Atomic download & .part cleanup
# ---------------------------------------------------------------------------

def test_download_file_atomic_success_and_no_part_leftover(tmp_path, monkeypatch):
    dest = tmp_path / "book.epub"
    ctx = _mock_stream_response(
        [b"hello ", b"world"],
        headers={"content-length": "11"},
    )
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    progress_events = []
    ok, err = app_mod.download_file(
        "https://example.org/book.epub",
        str(dest),
        lambda done, total: progress_events.append((done, total)),
    )

    assert ok is True
    assert err is None
    assert dest.read_bytes() == b"hello world"
    assert not (tmp_path / "book.epub.part").exists()
    assert progress_events == [(0, 11), (6, 11), (11, 11)]


def test_download_file_cleans_part_on_midstream_failure(tmp_path, monkeypatch):
    dest = tmp_path / "broken.epub"
    ctx = _mock_stream_response(
        [b"partial-data"],
        headers={"content-length": "100"},
        error=requests.exceptions.ConnectionError("stream dropped"),
    )
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    ok, err = app_mod.download_file("https://example.org/broken.epub", str(dest))

    assert ok is False
    assert "stream dropped" in err
    assert not dest.exists()
    assert not (tmp_path / "broken.epub.part").exists()


def test_download_file_cleans_part_on_replace_oserror(tmp_path, monkeypatch):
    dest = tmp_path / "locked.epub"
    ctx = _mock_stream_response([b"data"], headers={"content-length": "4"})
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))
    monkeypatch.setattr(
        dl_mod.os,
        "replace",
        MagicMock(side_effect=OSError("permission denied")),
    )

    ok, err = app_mod.download_file("https://example.org/locked.epub", str(dest))

    assert ok is False
    assert "permission denied" in err
    assert not dest.exists()
    assert not (tmp_path / "locked.epub.part").exists()


def test_download_file_handles_missing_or_invalid_content_length(tmp_path, monkeypatch):
    dest = tmp_path / "unknown_len.txt"
    ctx = _mock_stream_response([b"abc"], headers={"content-length": "not-a-number"})
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    progress_events = []
    ok, err = app_mod.download_file(
        "https://example.org/unknown_len.txt",
        str(dest),
        lambda done, total: progress_events.append((done, total)),
    )

    assert ok is True
    assert err is None
    assert dest.read_bytes() == b"abc"
    assert progress_events == [(0, 0), (3, 0)]


# ---------------------------------------------------------------------------
# Google Books 429 rate-limit handling
# ---------------------------------------------------------------------------

def test_search_google_books_handles_429_status_gracefully(monkeypatch):
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"Retry-After": "60"}
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=resp))

    logs = []
    rate_limited = []
    results = app_mod.search_google_books(
        "metamorphosis",
        log_fn=logs.append,
        on_rate_limit=lambda: rate_limited.append(True),
    )

    assert results == []
    assert rate_limited == [True]
    assert len(logs) == 1
    assert "HTTP 429" in logs[0]
    assert "Retry-After: 60" in logs[0]


def test_search_google_books_handles_429_http_error_gracefully(monkeypatch):
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
        "429 Too Many Requests", response=resp
    )
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=resp))

    logs = []
    rate_limited = []
    results = app_mod.search_google_books(
        "dracula",
        log_fn=logs.append,
        on_rate_limit=lambda: rate_limited.append(True),
    )

    assert results == []
    assert rate_limited == [True]
    assert any("HTTP 429" in line for line in logs)


def test_headless_search_worker_reports_google_429_in_status_and_skips_repeat(monkeypatch):
    app = _make_headless_app()
    google_calls = []

    def fake_gutenberg(title, log_fn=None):
        return [{
            "source": "Gutenberg",
            "id": "5200",
            "title": title,
            "author": "Kafka, Franz",
            "formats": {"EPUB": "https://example.org/5200.epub"},
        }]

    def fake_openlibrary(title, log_fn=None):
        return []

    def fake_google(title, log_fn=None, on_rate_limit=None):
        google_calls.append(title)
        if log_fn:
            log_fn("  [!] Google Books rate limit reached (HTTP 429). Try again later.")
        if on_rate_limit:
            on_rate_limit()
        return []

    fake_google.__name__ = "search_google_books"
    monkeypatch.setattr(src_mod, "search_google_books", fake_google)
    monkeypatch.setattr(src_mod, "SOURCES", [fake_gutenberg, fake_openlibrary, fake_google])

    app._searching = True
    app._search_worker(["metamorphosis", "moby dick"])

    assert google_calls == ["metamorphosis"]
    assert len(app.row_data) == 2
    assert "429" in app.status_var.get()
    assert "Google Books" in app.status_var.get()
    assert app._searching is False
    assert app.search_btn.config["state"] == "normal"


# ---------------------------------------------------------------------------
# Headless GUI re-entrancy & progress bar tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag_name", ["_searching", "_downloading"])
def test_start_search_blocks_reentrant_trigger(monkeypatch, flag_name):
    app = _make_headless_app()
    app.query_var.set("dracula")
    existing_id = app.tree.insert("", "end", values=("Gutenberg", "Existing", "Author", "EPUB"))
    app.row_data[existing_id] = {"title": "Existing"}
    setattr(app, flag_name, True)

    thread_started = []
    monkeypatch.setattr(
        ui_mod.threading,
        "Thread",
        lambda *args, **kwargs: thread_started.append((args, kwargs)),
    )
    msg_mock = MagicMock()
    monkeypatch.setattr(ui_mod, "messagebox", msg_mock)

    app.start_search()

    assert thread_started == []
    assert existing_id in app.row_data
    assert app.tree.get_children() == (existing_id,)
    msg_mock.showinfo.assert_not_called()


def _add_dummy_book_row(app):
    row_id = app.tree.insert("", "end", values=("Gutenberg", "Dracula", "Stoker", "EPUB"))
    app.row_data[row_id] = {
        "source": "Gutenberg",
        "id": "345",
        "title": "Dracula",
        "author": "Stoker",
        "formats": {"EPUB": "https://example.org/345.epub"},
    }
    return row_id


def test_download_worker_indeterminate_progress_starts_and_stops(tmp_path, monkeypatch):
    app = _make_headless_app()
    app.download_dir_var = _FakeVar(str(tmp_path))
    msg_mock = MagicMock()
    monkeypatch.setattr(ui_mod, "messagebox", msg_mock)
    row_id = _add_dummy_book_row(app)

    ctx = _mock_stream_response([b"chunk1", b"chunk2"], headers={})
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    app._downloading = True
    app._download_worker((row_id,))

    assert app.progress.start_calls == 1
    assert app.progress.stop_calls >= 1
    assert app.progress.config["mode"] == "determinate"
    assert app.progress.config["value"] == 0
    assert app._downloading is False
    assert (tmp_path / "Gutenberg_Dracula_Stoker_345.epub").read_bytes() == b"chunk1chunk2"
    msg_mock.showinfo.assert_called_once()


def test_download_worker_indeterminate_stops_on_failure(tmp_path, monkeypatch):
    app = _make_headless_app()
    app.download_dir_var = _FakeVar(str(tmp_path))
    msg_mock = MagicMock()
    monkeypatch.setattr(ui_mod, "messagebox", msg_mock)
    row_id = _add_dummy_book_row(app)

    ctx = _mock_stream_response(
        [b"partial"],
        headers={},
        error=requests.exceptions.ConnectionError("lost connection"),
    )
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    app._downloading = True
    app._download_worker((row_id,))

    assert app.progress.start_calls == 1
    assert app.progress.stop_calls >= 1
    assert app.progress.config["mode"] == "determinate"
    assert app.progress.config["value"] == 0
    assert app._downloading is False
    assert not (tmp_path / "Gutenberg_Dracula_Stoker_345.epub").exists()
    assert not (tmp_path / "Gutenberg_Dracula_Stoker_345.epub.part").exists()
    assert "Downloaded 0, failed 1." in app.status_var.get()


def test_download_worker_determinate_progress_clamps_and_resets(tmp_path, monkeypatch):
    app = _make_headless_app()
    app.download_dir_var = _FakeVar(str(tmp_path))
    msg_mock = MagicMock()
    monkeypatch.setattr(ui_mod, "messagebox", msg_mock)
    row_id = _add_dummy_book_row(app)

    observed_values = []
    orig_configure = app.progress.configure

    def tracking_configure(**kwargs):
        if "value" in kwargs:
            observed_values.append(kwargs["value"])
        orig_configure(**kwargs)

    app.progress.configure = tracking_configure

    # Simulate gzip-compressed response where decoded chunks exceed Content-Length
    ctx = _mock_stream_response([b"1234", b"567890"], headers={"content-length": "5"})
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    app._downloading = True
    app._download_worker((row_id,))

    assert app.progress.start_calls == 0
    assert max(observed_values) <= 100
    assert 80 in observed_values
    assert 100 in observed_values
    assert observed_values[-1] == 0
    assert app.progress.config["mode"] == "determinate"


@pytest.mark.parametrize(
    "raw_query",
    ["", "   ", "e.g. metamorphosis, dracula, moby dick", " , , "],
)
def test_start_search_rejects_empty_placeholder_and_comma_only_input(monkeypatch, raw_query):
    app = _make_headless_app()
    app.query_var.set(raw_query)
    msg_mock = MagicMock()
    monkeypatch.setattr(ui_mod, "messagebox", msg_mock)

    app.start_search()

    assert app._searching is False
    msg_mock.showinfo.assert_called_once()


def test_search_gutenberg_and_openlibrary_include_book_ids(monkeypatch):
    gutenberg_resp = MagicMock()
    gutenberg_resp.raise_for_status.return_value = None
    gutenberg_resp.json.return_value = {
        "results": [{
            "id": 5200,
            "title": "Metamorphosis",
            "authors": [{"name": "Kafka, Franz"}],
            "formats": {"application/epub+zip": "https://gutenberg.org/5200.epub"},
        }]
    }
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=gutenberg_resp))

    gb_results = app_mod.search_gutenberg("metamorphosis")
    assert len(gb_results) == 1
    assert gb_results[0]["id"] == "5200"

    ol_resp = MagicMock()
    ol_resp.raise_for_status.return_value = None
    ol_resp.json.return_value = {
        "docs": [{
            "title": "Moby Dick",
            "author_name": ["Herman Melville"],
            "ia": ["mobydick_ia_01"],
            "ebook_access": "public",
        }]
    }
    ia_resp = MagicMock()
    ia_resp.raise_for_status.return_value = None
    ia_resp.json.return_value = {
        "metadata": {"access-restricted-item": "false"},
        "files": [{"format": "EPUB", "name": "Moby Dick.epub"}],
    }
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(side_effect=[ol_resp, ia_resp]))

    ol_results = app_mod.search_openlibrary("moby dick")
    assert len(ol_results) == 1
    assert ol_results[0]["id"] == "mobydick_ia_01"
    assert ol_results[0]["formats"]["EPUB"] == (
        "https://archive.org/download/mobydick_ia_01/Moby%20Dick.epub"
    )


# ---------------------------------------------------------------------------
# New feature tests: format preference, cancel, sorting, metadata, CLI
# ---------------------------------------------------------------------------

def test_default_download_dir_uses_home_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(dl_mod.Path, "home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()
    assert dl_mod.default_download_dir() == str(tmp_path / "Downloads" / "FreeBookDownloader")


def test_pick_format_respects_preference_with_fallback():
    formats = {"EPUB": "u1", "PDF": "u2", "TXT": "u3"}
    assert app_mod.pick_format(formats, "Auto") == ("EPUB", "u1")
    assert app_mod.pick_format(formats, "PDF") == ("PDF", "u2")
    assert app_mod.pick_format(formats, "MOBI") == ("EPUB", "u1")


def test_download_file_cancels_cleanly_and_removes_part(tmp_path, monkeypatch):
    dest = tmp_path / "cancel.pdf"
    cancel_ev = threading.Event()

    def chunk_gen():
        yield b"chunk1"
        cancel_ev.set()
        yield b"chunk2"

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-length": "12"}
    resp.iter_content.return_value = chunk_gen()
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = False
    monkeypatch.setattr(app_mod.SESSION, "get", MagicMock(return_value=ctx))

    ok, err = app_mod.download_file(
        "https://example.org/cancel.pdf", str(dest), cancel_event=cancel_ev
    )
    assert ok is False
    assert "Cancelled" in err
    assert not dest.exists()
    assert not (tmp_path / "cancel.pdf.part").exists()


def test_search_openlibrary_skips_borrow_only_before_ia_call(monkeypatch):
    ol_resp = MagicMock()
    ol_resp.raise_for_status.return_value = None
    ol_resp.json.return_value = {
        "docs": [
            {
                "title": "Restricted",
                "ia": ["restricted_ia"],
                "ebook_access": "borrowable",
            },
            {
                "title": "Public Book",
                "author_name": ["Jane Doe"],
                "ia": ["public_ia"],
                "ebook_access": "public",
                "first_publish_year": 1899,
                "language": ["eng"],
                "key": "/works/OL1W",
            },
        ]
    }
    ia_calls = []

    def fake_ia(ia_id, log_fn=None):
        ia_calls.append(ia_id)
        return {"EPUB": f"https://archive.org/download/{ia_id}/book.epub"}

    monkeypatch.setattr(src_mod.SESSION, "get", MagicMock(return_value=ol_resp))
    monkeypatch.setattr(src_mod, "fetch_archive_org_formats", fake_ia)

    results = src_mod.search_openlibrary("test")
    assert ia_calls == ["public_ia"]
    assert len(results) == 1
    assert results[0]["year"] == "1899"
    assert results[0]["language"] == "ENG"
    assert results[0]["info_url"] == "https://openlibrary.org/works/OL1W"


def test_headless_sort_select_all_and_cancel():
    app = _make_headless_app()
    app._sort_state = (None, False)
    app.count_var = _FakeVar("")
    app.cancel_btn = _FakeWidget()
    app._cancel_event = threading.Event()
    app._add_row(
        {
            "source": "Gutenberg",
            "title": "Zebra",
            "author": "A",
            "year": "1920",
            "language": "EN",
            "formats": {"EPUB": "u1"},
        }
    )
    app._add_row(
        {
            "source": "Open Library",
            "title": "Alpha",
            "author": "B",
            "year": "1890",
            "language": "FR",
            "formats": {"PDF": "u2"},
        }
    )

    app._sort_by_column("title")
    ordered_titles = [app.tree.item(r, "values")[1] for r in app.tree.get_children()]
    assert ordered_titles == ["Alpha", "Zebra"]

    app._select_all_rows()
    assert len(app.tree.selection()) == 2
    assert app.count_var.get() == "2 results · 2 selected"

    app._searching = True
    app.cancel_active()
    assert app._cancel_event.is_set() is True


def test_cli_version_exits_zero():
    with pytest.raises(SystemExit) as exc:
        app_mod.main(["--version"])
    assert exc.value.code == 0
