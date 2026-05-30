"""
scanner.py — File scanning, metadata extraction, cover generation, and Watchdog sync.
"""

import hashlib
import io
import logging
import os
from pathlib import Path

from sqlmodel import Session

from shelfie.config import COVERS_DIR
from shelfie.database import Book, engine, get_book_by_path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: frozenset = frozenset({".pdf", ".epub"})


# ── Utility ───────────────────────────────────────────────────────────────────

def _cover_filename(file_path: str) -> str:
    """Stable filename derived from the book's absolute path."""
    h = hashlib.md5(file_path.encode()).hexdigest()[:12]
    return f"{h}.jpg"

def _cover_url(fname: str) -> str:
    """URL path served by the StaticFiles mount (always forward-slashes)."""
    return f"static/covers/{fname}"


# ── PDF ───────────────────────────────────────────────────────────────────────

def extract_pdf_metadata(file_path: str) -> tuple[str, int | None, str | None]:
    """Return (title, total_pages, cover_url).  Never raises — returns fallbacks."""
    title       = Path(file_path).stem
    total_pages = None
    cover_url   = None

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(file_path)
        total_pages = doc.page_count

        meta = doc.metadata or {}
        if meta.get("title", "").strip():
            title = meta["title"].strip()

        try:
            page  = doc[0]
            mat   = fitz.Matrix(1.5, 1.5)
            pix   = page.get_pixmap(matrix=mat, alpha=False)
            fname = _cover_filename(file_path)
            out   = COVERS_DIR / fname
            pix.save(str(out), "JPEG")
            cover_url = _cover_url(fname)
        except Exception as e:
            logger.warning("Cover generation failed for %s: %s", file_path, e)

        doc.close()

    except Exception as e:
        logger.warning("PDF metadata extraction failed for %s: %s", file_path, e)

    return title, total_pages, cover_url


# ── EPUB ──────────────────────────────────────────────────────────────────────

def extract_epub_metadata(file_path: str) -> tuple[str, int | None, str | None]:
    """Return (title, None, cover_url).  EPUBs don't have a fixed page count."""
    title     = Path(file_path).stem
    cover_url = None

    try:
        import ebooklib
        from ebooklib import epub
        from PIL import Image

        book = epub.read_epub(file_path, options={"ignore_ncx": True})

        titles = book.get_metadata("DC", "title")
        if titles:
            title = titles[0][0].strip() or title

        cover_item = None
        try:
            cover_item = book.get_item_with_id("cover")
        except Exception:
            pass

        if cover_item is None:
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_IMAGE:
                    if "cover" in (item.get_name() or "").lower():
                        cover_item = item
                        break

        if cover_item is None:
            for item in book.get_items():
                if item.get_type() == ebooklib.ITEM_IMAGE:
                    cover_item = item
                    break

        if cover_item is not None:
            try:
                img_data = cover_item.get_content()
                img      = Image.open(io.BytesIO(img_data)).convert("RGB")
                img.thumbnail((400, 600))
                fname = _cover_filename(file_path)
                out   = COVERS_DIR / fname
                img.save(str(out), "JPEG")
                cover_url = _cover_url(fname)
            except Exception as e:
                logger.warning("EPUB cover save failed for %s: %s", file_path, e)

    except Exception as e:
        logger.warning("EPUB metadata extraction failed for %s: %s", file_path, e)

    return title, None, cover_url


# ── Core DB helpers ───────────────────────────────────────────────────────────

def add_book_to_db(session: Session, library_id: int, file_path: str) -> Book | None:
    """Add a book if not already tracked.  Returns the new Book or None."""
    # Normalise to absolute path so Docker volume paths are stable
    file_path = str(Path(file_path).resolve())

    existing = get_book_by_path(session, file_path)
    if existing:
        if existing.library_id != library_id:
            existing.library_id = library_id
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return None

    ext = Path(file_path).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None

    if ext == ".pdf":
        title, total_pages, cover_path = extract_pdf_metadata(file_path)
        file_type = "pdf"
    else:
        title, total_pages, cover_path = extract_epub_metadata(file_path)
        file_type = "epub"

    book = Book(
        library_id  = library_id,
        file_path   = file_path,
        title       = title,
        file_type   = file_type,
        cover_path  = cover_path,
        total_pages = total_pages,
    )
    session.add(book)
    session.commit()
    session.refresh(book)
    logger.info("Added to library %d: %s", library_id, title)
    return book


def remove_book_from_db(session: Session, file_path: str) -> None:
    """Remove a book (and its cover image) from the database."""
    file_path = str(Path(file_path).resolve())
    book = get_book_by_path(session, file_path)
    if book:
        if book.cover_path:
            try:
                # Remove static/ prefix if present to delete the actual file
                p = book.cover_path
                if p.startswith("static/"):
                    # Serve path is relative to package or DATA_DIR, but actual covers are in COVERS_DIR
                    fname = p.split("/")[-1]
                    cover_file = COVERS_DIR / fname
                    if cover_file.exists():
                        cover_file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Could not delete cover image file %s: %s", book.cover_path, e)
        book.tags = []
        session.flush()
        session.delete(book)
        session.commit()
        logger.info("Removed book: %s", file_path)


# ── Full scan ─────────────────────────────────────────────────────────────────

def scan_library(library_id: int) -> None:
    """Walk all folders watched by the library, add new files, remove stale DB entries."""
    from sqlmodel import select
    from shelfie.database import Library, Book

    with Session(engine) as session:
        library = session.get(Library, library_id)
        if not library:
            logger.error("Library %d not found for scanning.", library_id)
            return

        scanned_paths = set()
        for folder_item in library.folders:
            folder = Path(folder_item.path).resolve()
            if not folder.exists():
                logger.warning("Library folder does not exist: %s", folder)
                continue

            for root, _, files in os.walk(folder):
                for fname in files:
                    ext = Path(fname).suffix.lower()
                    if ext in SUPPORTED_EXTENSIONS:
                        full_path = str(Path(root).resolve() / fname)
                        add_book_to_db(session, library_id, full_path)
                        scanned_paths.add(full_path)

        # Prune books whose files have been deleted from disk (only for scanned books)
        books = session.exec(select(Book).where(Book.library_id == library_id)).all()
        for book in books:
            if book.file_path:
                resolved_path = Path(book.file_path).resolve()
                if not resolved_path.exists():
                    remove_book_from_db(session, book.file_path)

    logger.info("Scan complete for library: %s", library.name)


# ── Watchdog ──────────────────────────────────────────────────────────────────

import threading
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

_observer = None
_active_watches = {}  # folder_path -> Watch object
_watchdog_lock = threading.Lock()


class GlobalLibraryHandler(FileSystemEventHandler):
    @staticmethod
    def _supported(path: str) -> bool:
        return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

    def _get_library_id(self, file_path: str) -> int | None:
        from sqlmodel import select
        from shelfie.database import LibraryFolder
        with Session(engine) as session:
            folders = session.exec(select(LibraryFolder)).all()
            folders = sorted(folders, key=lambda f: len(f.path), reverse=True)
            p_file = Path(file_path).resolve()
            for folder in folders:
                try:
                    p_folder = Path(folder.path).resolve()
                    if p_folder == p_file or p_folder in p_file.parents:
                        return folder.library_id
                except Exception:
                    pass
        return None

    def on_created(self, event):
        if not event.is_directory and self._supported(event.src_path):
            lib_id = self._get_library_id(event.src_path)
            if lib_id is not None:
                with Session(engine) as s:
                    add_book_to_db(s, lib_id, event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self._supported(event.src_path):
            with Session(engine) as s:
                remove_book_from_db(s, event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            src_supported = self._supported(event.src_path)
            dest_supported = self._supported(event.dest_path)
            if src_supported or dest_supported:
                with Session(engine) as s:
                    src_lib_id = self._get_library_id(event.src_path)
                    dest_lib_id = self._get_library_id(event.dest_path)
                    
                    if src_lib_id is not None and dest_lib_id is not None and src_lib_id == dest_lib_id:
                        # Move within the same library: update file_path of existing Book to preserve metadata!
                        from shelfie.database import get_book_by_path
                        book = get_book_by_path(s, event.src_path)
                        if book:
                            book.file_path = str(Path(event.dest_path).resolve())
                            s.add(book)
                            s.commit()
                            logger.info("Moved book inside library %d: %s -> %s", dest_lib_id, event.src_path, event.dest_path)
                        else:
                            add_book_to_db(s, dest_lib_id, event.dest_path)
                    else:
                        # Deletion and creation across libraries/untracked
                        if src_supported:
                            remove_book_from_db(s, event.src_path)
                        if dest_supported and dest_lib_id is not None:
                            add_book_to_db(s, dest_lib_id, event.dest_path)


def sync_watchdog_observers() -> None:
    """Sync Watchdog watches with LibraryFolder entries in the database (diff-based)."""
    global _observer
    from sqlmodel import select
    from shelfie.database import LibraryFolder

    with _watchdog_lock:
        if _observer is None:
            _observer = PollingObserver(timeout=5)
            _observer.daemon = True
            _observer.start()
            logger.info("Global PollingObserver started.")

        with Session(engine) as session:
            folders = session.exec(select(LibraryFolder)).all()
            db_paths = {str(Path(f.path).resolve()): f for f in folders}

        # 1. Unschedule paths that are no longer in the DB
        stale_paths = []
        for path, watch_key in list(_active_watches.items()):
            if path not in db_paths:
                try:
                    _observer.unschedule(watch_key)
                    logger.info("Unscheduled watchdog for: %s", path)
                except Exception as e:
                    logger.warning("Failed to unschedule %s: %s", path, e)
                stale_paths.append(path)
        
        for path in stale_paths:
            del _active_watches[path]

        # 2. Schedule paths that are new in the DB
        handler = GlobalLibraryHandler()
        for path in db_paths:
            if path not in _active_watches:
                if Path(path).exists():
                    try:
                        watch_key = _observer.schedule(handler, path=path, recursive=True)
                        _active_watches[path] = watch_key
                        logger.info("Scheduled watchdog for: %s", path)
                    except Exception as e:
                        logger.error("Failed to schedule watchdog for %s: %s", path, e)
                else:
                    logger.warning("Cannot watch folder (not found): %s", path)


def stop_watchdog() -> None:
    """Stop the global observer and clear all active watches."""
    global _observer
    with _watchdog_lock:
        if _observer is not None:
            try:
                _observer.stop()
                _observer.join(timeout=2)
                _observer = None
                _active_watches.clear()
                logger.info("Global PollingObserver stopped.")
            except Exception as e:
                logger.warning("Error stopping observer: %s", e)
