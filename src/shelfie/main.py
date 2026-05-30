"""
main.py — FastAPI application for Shelfie.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from importlib.resources import files as _pkg_files
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydantic import BaseModel
from sqlmodel import Session, select

from shelfie import __version__
from shelfie.config import (
    ALLOWED_IMAGE_TYPES,
    CONFIG_FILE,
    COVERS_DIR,
    DEFAULT_LIBRARY_PATH,
    MAX_UPLOAD_BYTES,
)
from shelfie.database import (
    Book,
    BookQuote,
    ProgressLog,
    Tag,
    add_progress_log,
    create_db_and_tables,
    get_all_books,
    get_or_create_tag,
    get_progress_logs,
    get_quotes,
    get_session,
    get_stats,
)
from shelfie.scanner import (
    extract_epub_metadata,
    extract_pdf_metadata,
    scan_library,
    start_watchdog,
)
from shelfie.version_check import check_for_newer_release

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

_UPDATE_CHECK_TTL_SECONDS = 6 * 60 * 60
_update_check_lock = threading.Lock()
_update_check_last_run = 0.0
_update_check_latest: str | None = None


# ── Library-path helpers ───────────────────────────────────────────────────────

def _is_docker_runtime() -> bool:
    return os.environ.get("SHELFIE_DATA_DIR", "").startswith("/data")


def _update_check_disabled() -> bool:
    return os.environ.get("SHELFIE_DISABLE_UPDATE_CHECK", "").lower() in {"1", "true", "yes"}


def _update_upgrade_hint() -> str:
    if _is_docker_runtime():
        return "Update your image (for example: docker pull mhmdsamerdev/shelfie)."
    return "Upgrade with: pip install --upgrade shelfie-py"


def _get_newer_release_cached(force: bool = False) -> str | None:
    global _update_check_last_run, _update_check_latest

    if _update_check_disabled():
        return None

    now = time.time()
    if not force and _update_check_last_run and (now - _update_check_last_run) < _UPDATE_CHECK_TTL_SECONDS:
        return _update_check_latest

    with _update_check_lock:
        now = time.time()
        if not force and _update_check_last_run and (now - _update_check_last_run) < _UPDATE_CHECK_TTL_SECONDS:
            return _update_check_latest

        _update_check_latest = check_for_newer_release(__version__)
        _update_check_last_run = now
        return _update_check_latest


def _log_if_update_available() -> None:
    latest = _get_newer_release_cached()
    if latest:
        logger.warning(
            "A newer Shelfie release is available: %s (installed: %s). %s",
            latest,
            __version__,
            _update_upgrade_hint(),
        )

def load_library_path() -> str:
    if CONFIG_FILE.exists():
        saved = CONFIG_FILE.read_text().strip()
        if saved:
            return saved
    env_path = os.environ.get("LIBRARY_PATH", "").strip()
    if env_path:
        return env_path
    return DEFAULT_LIBRARY_PATH

def save_library_path(path: str) -> None:
    CONFIG_FILE.write_text(path.strip())


# ── Locate package assets via importlib.resources ─────────────────────────────
# This resolves correctly whether the package is installed as a wheel, editable
# install, or run straight from source.  It does NOT depend on cwd.

def _asset_path(subdir: str) -> Path:
    """Return the absolute path to a subdirectory inside the installed package."""
    ref = _pkg_files("shelfie").joinpath(subdir)
    # importlib.resources may return a traversal object; resolve to a real path.
    # For editable installs and source trees this is a plain Path.
    # For zip-imported packages we materialise to a temp dir (rarely needed).
    try:
        return Path(str(ref))
    except Exception:
        import importlib.resources as _ir
        with _ir.as_file(ref) as p:
            return p


TEMPLATES_DIR: Path = _asset_path("templates")
STATIC_DIR:    Path = _asset_path("static")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(title="Shelfie", version=__version__)

# Mount the package's bundled static folder AND expose the user-data covers dir
# under the same /static prefix so cover images are served correctly.
app.mount(
    "/static/covers",
    StaticFiles(directory=str(COVERS_DIR)),
    name="covers",
)
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static",
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_watchdog_observer = None


@app.on_event("startup")
def on_startup() -> None:
    global _watchdog_observer
    if not _update_check_disabled():
        threading.Thread(target=_log_if_update_available, daemon=True).start()
    create_db_and_tables()
    lib = load_library_path()
    if Path(lib).exists():
        threading.Thread(target=scan_library, args=(lib,), daemon=True).start()
        _watchdog_observer = start_watchdog(lib)
    else:
        logger.warning("Library folder not found: %s — set it via the UI.", lib)


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class BookUpdate(BaseModel):
    current_page: int | None = None
    category: str | None = None
    notes: str | None = None
    is_read: bool | None = None
    tags: list[str] | None = None

class BookDetailUpdate(BaseModel):
    custom_title: str | None = None
    admin_notes: str | None = None
    date_started: str | None = None   # ISO-8601 or "" to clear
    date_finished: str | None = None
    current_page: int | None = None
    category: str | None = None
    notes: str | None = None
    is_read: bool | None = None
    tags: list[str] | None = None
    log_progress: bool = False

class ProgressLogIn(BaseModel):
    page: int
    note: str | None = None

class ProgressLogNoteUpdate(BaseModel):
    note: str

class QuoteIn(BaseModel):
    quote_text:  str
    page_number: int | None = None

class LibraryPathIn(BaseModel):
    path: str


# ── Internal helpers ───────────────────────────────────────────────────────────

def _parse_dt(val: str | None) -> datetime | None:
    if not val or not val.strip():
        return None
    try:
        return datetime.fromisoformat(val.strip())
    except ValueError:
        return None


def _apply_auto_dates(book: Book, new_page: int) -> None:
    now = datetime.utcnow()
    if new_page > 0 and book.current_page == 0 and not book.date_started:
        book.date_started = now
    if book.total_pages and new_page >= book.total_pages and not book.date_finished:
        book.date_finished = now
        book.is_read = True


def book_to_out(book: Book) -> dict:
    return {
        "id":             book.id,
        "file_path":      book.file_path,
        "title":          book.display_title,
        "original_title": book.title,
        "custom_title":   book.custom_title,
        "file_type":      book.file_type,
        "cover_path":     book.cover_path,
        "total_pages":    book.total_pages,
        "current_page":   book.current_page,
        "progress":       book.progress_percent,
        "category":       book.category,
        "notes":          book.notes,
        "admin_notes":    book.admin_notes,
        "is_read":        book.is_read,
        "date_added":     book.date_added.isoformat(),
        "last_opened":    book.last_opened.isoformat() if book.last_opened else None,
        "date_started":   book.date_started.isoformat() if book.date_started else None,
        "date_finished":  book.date_finished.isoformat() if book.date_finished else None,
        "tags":           [t.name for t in book.tags],
    }


def log_to_out(log: ProgressLog) -> dict:
    return {
        "id":        log.id,
        "page":      log.page,
        "timestamp": log.timestamp.isoformat(),
        "note":      log.note,
    }


def quote_to_out(q: BookQuote) -> dict:
    return {
        "id":          q.id,
        "book_id":     q.book_id,
        "quote_text":  q.quote_text,
        "page_number": q.page_number,
        "date_added":  q.date_added.isoformat(),
    }


# ── UI route ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"library_path": load_library_path()},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.png")


# ── Book CRUD ──────────────────────────────────────────────────────────────────

@app.get("/api/books")
def list_books(
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    session: Session = Depends(get_session),
):
    books = session.exec(select(Book)).all()
    if category:
        books = [b for b in books if (b.category or "").lower() == category.lower()]
    if tag:
        books = [b for b in books if any(t.name == tag.lower() for t in b.tags)]
    if q:
        ql = q.lower()
        books = [b for b in books
                 if ql in b.display_title.lower() or ql in (b.notes or "").lower()]
    return [book_to_out(b) for b in books]


@app.get("/api/books/{book_id}")
def get_book(book_id: int, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return book_to_out(book)


@app.patch("/api/books/{book_id}")
def patch_book(book_id: int, data: BookUpdate, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if data.current_page is not None:
        _apply_auto_dates(book, data.current_page)
        book.current_page = data.current_page
    if data.category is not None:
        book.category = data.category
    if data.notes is not None:
        book.notes = data.notes
    if data.is_read is not None:
        book.is_read = data.is_read
    if data.tags is not None:
        book.tags = [get_or_create_tag(session, t) for t in data.tags if t.strip()]
    session.add(book)
    session.commit()
    session.refresh(book)
    return book_to_out(book)


@app.put("/api/books/{book_id}")
def put_book(book_id: int, data: BookDetailUpdate, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    if data.current_page is not None:
        old_page = book.current_page
        _apply_auto_dates(book, data.current_page)
        book.current_page = data.current_page
        if data.log_progress or data.current_page != old_page:
            pct  = round((data.current_page / book.total_pages) * 100) if book.total_pages else 0
            note = f"Reached page {data.current_page}" + (f" ({pct}%)" if book.total_pages else "")
            add_progress_log(session, book_id, data.current_page, note)

    if data.custom_title is not None:
        book.custom_title = data.custom_title or None
    if data.admin_notes is not None:
        book.admin_notes = data.admin_notes
    if data.category is not None:
        book.category = data.category
    if data.notes is not None:
        book.notes = data.notes
    if data.is_read is not None:
        book.is_read = data.is_read
    if data.date_started is not None:
        book.date_started = _parse_dt(data.date_started)
    if data.date_finished is not None:
        book.date_finished = _parse_dt(data.date_finished)
    if data.tags is not None:
        book.tags = [get_or_create_tag(session, t) for t in data.tags if t.strip()]

    session.add(book)
    session.commit()
    session.refresh(book)
    return book_to_out(book)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if book.cover_path:
        try:
            Path(book.cover_path).unlink(missing_ok=True)
        except Exception:
            pass
    book.tags = []
    session.flush()
    session.delete(book)
    session.commit()
    return {"ok": True}


# ── Cover upload / delete ──────────────────────────────────────────────────────

@app.post("/api/books/{book_id}/cover")
async def upload_cover(
    book_id: int,
    file:    UploadFile = File(...),
    session: Session    = Depends(get_session),
):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, f"Unsupported type: {file.content_type}. Use JPEG, PNG or WebP.")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Image too large (max 10 MB)")

    try:
        img  = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((600, 900), Image.Resampling.LANCZOS)
        fname    = f"custom_{book_id}.jpg"
        out_path = COVERS_DIR / fname
        img.save(str(out_path), "JPEG", quality=88)
    except Exception as e:
        logger.warning("Cover save failed for book %d: %s", book_id, e)
        raise HTTPException(500, "Could not process image") from e

    if book.cover_path and book.cover_path != f"static/covers/{fname}":
        try:
            old = Path(book.cover_path)
            if old.exists() and not old.name.startswith("custom_"):
                old.unlink(missing_ok=True)
        except Exception:
            pass

    book.cover_path = f"static/covers/{fname}"
    session.add(book)
    session.commit()
    session.refresh(book)
    return book_to_out(book)


@app.delete("/api/books/{book_id}/cover")
def delete_cover(book_id: int, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    custom = COVERS_DIR / f"custom_{book_id}.jpg"
    if custom.exists():
        custom.unlink(missing_ok=True)

    new_cover = None
    try:
        if book.file_type == "pdf":
            _, _, new_cover = extract_pdf_metadata(book.file_path)
        else:
            _, _, new_cover = extract_epub_metadata(book.file_path)
    except Exception:
        pass

    book.cover_path = new_cover
    session.add(book)
    session.commit()
    session.refresh(book)
    return book_to_out(book)


# ── File open (page-aware) ─────────────────────────────────────────────────────

@app.post("/api/books/{book_id}/open")
def open_book(book_id: int, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    fp = Path(book.file_path)
    if not fp.exists():
        raise HTTPException(404, "File not found on disk")

    book.last_opened = datetime.utcnow()
    session.add(book)
    session.commit()

    page = max(book.current_page or 1, 1)
    try:
        if book.file_type == "pdf" and page > 1:
            file_url = fp.as_uri() + f"#page={page}"
            if sys.platform == "win32":
                webbrowser.open(file_url)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", file_url])
            else:
                subprocess.Popen(["xdg-open", file_url])
        else:
            if sys.platform == "win32":
                os.startfile(str(fp))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(fp)])
            else:
                subprocess.Popen(["xdg-open", str(fp)])
    except Exception as e:
        raise HTTPException(500, f"Could not open file: {e}") from e

    return {"ok": True, "page": page}


# ── Progress log endpoints ─────────────────────────────────────────────────────

@app.get("/api/books/{book_id}/progress")
def get_book_progress(book_id: int, session: Session = Depends(get_session)):
    if not session.get(Book, book_id):
        raise HTTPException(404, "Book not found")
    return [log_to_out(log) for log in get_progress_logs(session, book_id)]


@app.post("/api/books/{book_id}/progress")
def log_progress(book_id: int, data: ProgressLogIn, session: Session = Depends(get_session)):
    book = session.get(Book, book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if data.page < 0:
        raise HTTPException(400, "Page cannot be negative")

    note = data.note
    if not note:
        pct  = round((data.page / book.total_pages) * 100) if book.total_pages else 0
        note = f"Reached page {data.page}" + (f" ({pct}%)" if book.total_pages else "")

    _apply_auto_dates(book, data.page)
    book.current_page = data.page
    session.add(book)
    return log_to_out(add_progress_log(session, book_id, data.page, note))


@app.patch("/api/books/{book_id}/progress/{log_id}")
def update_log_note(
    book_id: int, log_id: int,
    data: ProgressLogNoteUpdate,
    session: Session = Depends(get_session),
):
    log = session.get(ProgressLog, log_id)
    if not log or log.book_id != book_id:
        raise HTTPException(404, "Log entry not found")
    log.note = data.note
    session.add(log)
    session.commit()
    session.refresh(log)
    return log_to_out(log)


@app.delete("/api/books/{book_id}/progress/{log_id}")
def delete_progress_log(book_id: int, log_id: int, session: Session = Depends(get_session)):
    log = session.get(ProgressLog, log_id)
    if not log or log.book_id != book_id:
        raise HTTPException(404, "Log entry not found")
    session.delete(log)
    session.commit()
    return {"ok": True}


# ── Quote endpoints ────────────────────────────────────────────────────────────

@app.get("/api/books/{book_id}/quotes")
def list_quotes(book_id: int, session: Session = Depends(get_session)):
    if not session.get(Book, book_id):
        raise HTTPException(404, "Book not found")
    return [quote_to_out(q) for q in get_quotes(session, book_id)]


@app.post("/api/books/{book_id}/quotes")
def add_quote(book_id: int, data: QuoteIn, session: Session = Depends(get_session)):
    if not session.get(Book, book_id):
        raise HTTPException(404, "Book not found")
    if not data.quote_text.strip():
        raise HTTPException(400, "Quote text cannot be empty")
    quote = BookQuote(
        book_id     = book_id,
        quote_text  = data.quote_text.strip(),
        page_number = data.page_number,
    )
    session.add(quote)
    session.commit()
    session.refresh(quote)
    return quote_to_out(quote)


@app.delete("/api/books/{book_id}/quotes/{quote_id}")
def delete_quote(book_id: int, quote_id: int, session: Session = Depends(get_session)):
    quote = session.get(BookQuote, quote_id)
    if not quote or quote.book_id != book_id:
        raise HTTPException(404, "Quote not found")
    session.delete(quote)
    session.commit()
    return {"ok": True}


# ── Misc endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats(session: Session = Depends(get_session)):
    return get_stats(session)

@app.get("/api/tags")
def list_tags(session: Session = Depends(get_session)):
    return [{"id": t.id, "name": t.name} for t in session.exec(select(Tag)).all()]

@app.get("/api/categories")
def list_categories(session: Session = Depends(get_session)):
    return sorted({b.category for b in get_all_books(session) if b.category})

@app.get("/api/library-path")
def get_library_path():
    return {
        "path": load_library_path(),
        "is_docker": os.environ.get("SHELFIE_DATA_DIR", "").startswith("/data"),
    }


@app.get("/api/version")
def get_version_info():
    latest = _get_newer_release_cached()
    return {
        "installed_version": __version__,
        "latest_version": latest,
        "update_available": bool(latest),
        "upgrade_hint": _update_upgrade_hint() if latest else None,
        "release_url": "https://pypi.org/project/shelfie-py/",
        "update_check_disabled": _update_check_disabled(),
    }

@app.post("/api/library-path")
def set_library_path(data: LibraryPathIn):
    global _watchdog_observer
    p = Path(data.path.strip()).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, "Path does not exist or is not a directory")
    save_library_path(str(p))
    if _watchdog_observer:
        try:
            _watchdog_observer.stop()
            _watchdog_observer.join(timeout=2)
        except Exception:
            pass
    threading.Thread(target=scan_library, args=(str(p),), daemon=True).start()
    _watchdog_observer = start_watchdog(str(p))
    return {"ok": True, "path": str(p)}

@app.post("/api/rescan")
def rescan():
    lib = load_library_path()
    threading.Thread(target=scan_library, args=(lib,), daemon=True).start()
    return {"ok": True}


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    """
    CLI entry point — registered as 'shelfie' in pyproject.toml.

    Examples
    --------
    shelfie
    shelfie --host 0.0.0.0 --port 8080
    shelfie --reload          # development mode
    """
    parser = argparse.ArgumentParser(
        prog="shelfie",
        description="Local Library Manager — self-hosted book tracker",
    )
    parser.add_argument("--host",   default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port",   default=8000, type=int, help="Bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true",   help="Enable auto-reload (dev only)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"],
                        help="Uvicorn log level (default: info)")
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(
        "shelfie.main:app",
        host      = args.host,
        port      = args.port,
        reload    = args.reload,
        log_level = args.log_level,
    )


if __name__ == "__main__":
    main()
