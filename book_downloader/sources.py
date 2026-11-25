"""Public-domain book source clients (Project Gutenberg, Open Library, Google Books)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests

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

_THREAD_LOCAL = threading.local()


class _SessionProxy:
    """Thread-local requests.Session proxy so concurrent searches stay thread-safe."""

    def _get(self) -> requests.Session:
        sess = getattr(_THREAD_LOCAL, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(HEADERS)
            _THREAD_LOCAL.session = sess
        return sess

    def get(self, *args, **kwargs):
        return self._get().get(*args, **kwargs)

    @property
    def headers(self):
        return self._get().headers


SESSION = _SessionProxy()

GUTENDEX_FORMAT_MAP = [
    ("EPUB", "application/epub+zip"),
    ("MOBI", "application/x-mobipocket-ebook"),
    ("PDF", "application/pdf"),
    ("TXT", "text/plain"),
]

# Open Library values that explicitly indicate a non-downloadable / borrow-only item.
_OL_RESTRICTED_ACCESS = frozenset({"borrowable", "printdisabled", "no_ebook"})


def _format_rate_limit_log(source_name: str, resp) -> str:
    """Format a concise HTTP 429 log line without exposing full query URLs."""
    retry_after = ""
    headers = getattr(resp, "headers", None)
    if headers:
        raw_retry = headers.get("Retry-After")
        if raw_retry:
            retry_after = f" (Retry-After: {raw_retry})"
    return f"  [!] {source_name} rate limit reached (HTTP 429){retry_after}. Try again later."


def search_gutenberg(title, log_fn=None, cancel_event=None, **_kwargs):
    results = []
    if cancel_event and cancel_event.is_set():
        return results
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
        if cancel_event and cancel_event.is_set():
            break
        formats = {}
        raw_formats = book.get("formats", {})
        for label, mime in GUTENDEX_FORMAT_MAP:
            for fmt_mime, url in raw_formats.items():
                if fmt_mime.startswith(mime) and label not in formats:
                    formats[label] = url
                    break
        if not formats:
            continue
        authors = ", ".join(a["name"] for a in (book.get("authors") or []) if a.get("name")) or (
            "Unknown"
        )
        langs = ", ".join(str(lang).upper() for lang in (book.get("languages") or []) if lang)
        book_id = book.get("id")
        id_str = str(book_id) if book_id is not None else ""
        results.append(
            {
                "source": "Gutenberg",
                "id": id_str,
                "title": book.get("title", title),
                "author": authors,
                "language": langs or "—",
                "year": "—",
                "info_url": f"https://www.gutenberg.org/ebooks/{id_str}" if id_str else "",
                "formats": formats,
            }
        )
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


def search_openlibrary(title, log_fn=None, cancel_event=None, **_kwargs):
    results = []
    if cancel_event and cancel_event.is_set():
        return results
    try:
        resp = SESSION.get(
            "https://openlibrary.org/search.json",
            params={
                "q": title,
                "limit": 8,
                "fields": "key,title,author_name,ia,ebook_access,first_publish_year,language",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        docs = resp.json().get("docs") or []
    except (requests.exceptions.RequestException, ValueError) as exc:
        if log_fn:
            log_fn(f"  [!] Open Library error for '{title}': {exc}")
        return results

    candidates = []
    for doc in docs:
        ia_ids = doc.get("ia") or []
        if not ia_ids:
            continue
        if doc.get("ebook_access") in _OL_RESTRICTED_ACCESS:
            continue  # skip borrow-only / printdisabled before extra HTTP round-trips
        candidates.append((doc, ia_ids[0]))
        if len(candidates) >= 5:
            break

    if not candidates or (cancel_event and cancel_event.is_set()):
        return results

    with ThreadPoolExecutor(max_workers=min(5, len(candidates))) as pool:
        future_to_doc = {
            pool.submit(fetch_archive_org_formats, ia_id, log_fn=log_fn): (doc, ia_id)
            for doc, ia_id in candidates
        }
        for fut in as_completed(future_to_doc):
            if cancel_event and cancel_event.is_set():
                break
            doc, ia_id = future_to_doc[fut]
            try:
                formats = fut.result()
            except Exception as exc:
                if log_fn:
                    log_fn(f"  [!] Internet Archive error for '{ia_id}': {exc}")
                continue
            if not formats:
                continue

            authors = ", ".join(doc.get("author_name") or []) or "Unknown"
            langs = ", ".join(str(lang).upper() for lang in (doc.get("language") or [])[:3] if lang)
            year = str(doc.get("first_publish_year") or "—")
            ol_key = str(doc.get("key") or "")
            info_url = (
                f"https://openlibrary.org{ol_key}"
                if ol_key.startswith("/")
                else f"https://archive.org/details/{quote(str(ia_id), safe='')}"
            )
            results.append(
                {
                    "source": "Open Library",
                    "id": str(ia_id),
                    "title": doc.get("title", title),
                    "author": authors,
                    "language": langs or "—",
                    "year": year,
                    "info_url": info_url,
                    "formats": formats,
                }
            )
    return results


def search_google_books(title, log_fn=None, on_rate_limit=None, cancel_event=None, **_kwargs):
    results = []
    if cancel_event and cancel_event.is_set():
        return results
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
        if cancel_event and cancel_event.is_set():
            break
        info = item.get("volumeInfo", {})
        access = item.get("accessInfo", {})

        if not access.get("publicDomain"):
            continue

        formats = {}
        epub = access.get("epub", {})
        pdf = access.get("pdf", {})
        if epub.get("isAvailable") and epub.get("downloadLink"):
            formats["EPUB"] = epub["downloadLink"]
        if pdf.get("isAvailable") and pdf.get("downloadLink"):
            formats["PDF"] = pdf["downloadLink"]

        if not formats:
            continue

        authors = ", ".join(info.get("authors") or []) or "Unknown"
        pub_date = str(info.get("publishedDate") or "")
        year = pub_date[:4] if len(pub_date) >= 4 and pub_date[:4].isdigit() else "—"
        lang = str(info.get("language") or "").upper() or "—"
        results.append(
            {
                "source": "Google Books",
                "id": str(item.get("id") or ""),
                "title": info.get("title", title),
                "author": authors,
                "language": lang,
                "year": year,
                "info_url": str(info.get("infoLink") or ""),
                "formats": formats,
            }
        )
    return results


SOURCES = [search_gutenberg, search_openlibrary, search_google_books]
SOURCE_NAMES = {
    "Gutenberg": search_gutenberg,
    "Open Library": search_openlibrary,
    "Google Books": search_google_books,
}


def search_all_sources(
    title,
    log_fn,
    on_rate_limit=None,
    skip_sources=None,
    enabled_sources=None,
    cancel_event=None,
):
    """Query enabled public-domain book sources concurrently for *title*."""
    active_sources = []
    for source_fn in SOURCES:
        name = getattr(source_fn, "__name__", "")
        if skip_sources and (source_fn in skip_sources or name in skip_sources):
            continue
        if enabled_sources is not None and source_fn not in enabled_sources and name not in (
            enabled_sources
        ):
            continue
        active_sources.append(source_fn)

    if not active_sources or (cancel_event and cancel_event.is_set()):
        return []

    def _invoke(fn):
        try:
            return fn(
                title,
                log_fn=log_fn,
                on_rate_limit=on_rate_limit,
                cancel_event=cancel_event,
            )
        except TypeError:
            # Support custom/monkeypatched test doubles with simpler signatures.
            if getattr(fn, "__name__", "") == "search_google_books":
                return fn(title, log_fn=log_fn, on_rate_limit=on_rate_limit)
            return fn(title, log_fn=log_fn)

    all_results = []
    with ThreadPoolExecutor(max_workers=len(active_sources)) as pool:
        futures = [pool.submit(_invoke, fn) for fn in active_sources]
        for source_fn, fut in zip(active_sources, futures):
            if cancel_event and cancel_event.is_set():
                break
            try:
                all_results.extend(fut.result())
            except Exception as exc:
                log_fn(f"  [!] {source_fn.__name__} unexpected error for '{title}': {exc}")
    return all_results
