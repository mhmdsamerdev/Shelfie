"""
database.py — SQLModel models and zero-loss database initialisation.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import event, text
from sqlmodel import Field, Relationship, Session, SQLModel, create_engine, select

from shelfie.config import DATABASE_URL, CONFIG_FILE, DEFAULT_LIBRARY_PATH

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},   # needed for SQLite + FastAPI threads
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")   # concurrent reads + one writer
    cursor.execute("PRAGMA busy_timeout=5000")  # retry for up to 5 s before raising OperationalError
    cursor.close()


# ── Library ────────────────────────────────────────────────────────────────────

class Library(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    
    folders: list["LibraryFolder"] = Relationship(
        back_populates="library",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    books: list["Book"] = Relationship(
        back_populates="library",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


# ── LibraryFolder ──────────────────────────────────────────────────────────────

class LibraryFolder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    library_id: int = Field(foreign_key="library.id", index=True)
    path: str = Field(index=True, unique=True)
    
    library: Optional[Library] = Relationship(back_populates="folders")


# ── Link table ─────────────────────────────────────────────────────────────────

class BookTagLink(SQLModel, table=True):
    book_id: int | None = Field(default=None, foreign_key="book.id", primary_key=True)
    tag_id: int | None = Field(default=None, foreign_key="tag.id", primary_key=True)


# ── Tag ────────────────────────────────────────────────────────────────────────

class Tag(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    books: list["Book"] = Relationship(back_populates="tags", link_model=BookTagLink)


# ── ProgressLog ────────────────────────────────────────────────────────────────

class ProgressLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    book_id: int = Field(foreign_key="book.id", index=True)
    page: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    note: str | None = None
    book: Optional["Book"] = Relationship(back_populates="progress_logs")


# ── BookQuote ──────────────────────────────────────────────────────────────────

class BookQuote(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    book_id: int = Field(foreign_key="book.id", index=True)
    quote_text: str
    page_number: int | None = None
    date_added: datetime = Field(default_factory=datetime.utcnow)
    book: Optional["Book"] = Relationship(back_populates="quotes")


# ── Book ───────────────────────────────────────────────────────────────────────

class Book(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    library_id: int | None = Field(default=None, foreign_key="library.id", index=True)
    file_path: str | None = Field(default=None, index=True, unique=True)

    title: str
    custom_title: str | None = None

    file_type: str  # "pdf" | "epub"
    cover_path: str | None = None
    total_pages: int | None = None
    current_page: int = 0
    category: str | None = None

    notes: str | None = None
    admin_notes: str | None = None

    is_read: bool = False
    date_added: datetime = Field(default_factory=datetime.utcnow)
    last_opened: datetime | None = None
    date_started: datetime | None = None
    date_finished: datetime | None = None

    library: Optional[Library] = Relationship(back_populates="books")
    tags: list[Tag] = Relationship(back_populates="books", link_model=BookTagLink)
    progress_logs: list[ProgressLog] = Relationship(
        back_populates="book",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    quotes: list[BookQuote] = Relationship(
        back_populates="book",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    @property
    def display_title(self) -> str:
        return (self.custom_title or self.title or "Untitled").strip()

    @property
    def progress_percent(self) -> float:
        if self.total_pages and self.total_pages > 0:
            return round((self.current_page / self.total_pages) * 100, 1)
        return 0.0


# ── Initialisation ─────────────────────────────────────────────────────────────

def create_db_and_tables() -> None:
    """Create all tables (no-op for tables that already exist) then migrate."""
    SQLModel.metadata.create_all(engine)
    _safe_migrate()
    with Session(engine) as session:
        _backfill_default_library(session)


def _safe_migrate() -> None:
    """
    Idempotent DDL migration — safe to call on every startup.
    Handles upgrades from any previous version without touching existing rows.
    """
    # Tables that may be absent in older databases
    new_tables = [
        (
            "progresslog",
            """CREATE TABLE IF NOT EXISTS progresslog (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id   INTEGER NOT NULL REFERENCES book(id),
                page      INTEGER NOT NULL,
                timestamp DATETIME NOT NULL DEFAULT (datetime('now')),
                note      TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS ix_progresslog_book_id ON progresslog (book_id)",
        ),
        (
            "bookquote",
            """CREATE TABLE IF NOT EXISTS bookquote (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id     INTEGER NOT NULL REFERENCES book(id),
                quote_text  TEXT    NOT NULL,
                page_number INTEGER,
                date_added  DATETIME NOT NULL DEFAULT (datetime('now'))
            )""",
            "CREATE INDEX IF NOT EXISTS ix_bookquote_book_id ON bookquote (book_id)",
        ),
    ]

    # Columns added to book table after v1
    book_new_columns = [
        ("custom_title",  "TEXT"),
        ("admin_notes",   "TEXT"),
        ("date_started",  "DATETIME"),
        ("date_finished", "DATETIME"),
        ("library_id",    "INTEGER"),
    ]

    with engine.connect() as conn:
        for _name, create_ddl, index_ddl in new_tables:
            conn.execute(text(create_ddl.strip()))
            conn.execute(text(index_ddl.strip()))

        for col, col_type in book_new_columns:
            try:
                conn.execute(text(f"ALTER TABLE book ADD COLUMN {col} {col_type}"))
                logger.info("Migration: added column book.%s", col)
            except Exception:
                pass   # already exists — intentional

        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_book_library_id ON book (library_id)"))
        except Exception:
            pass

        conn.commit()

    logger.info("Database migration complete.")


def _backfill_default_library(session: Session) -> None:
    """Idempotently create Default Library and link existing orphaned books."""
    first_lib = session.exec(select(Library)).first()
    if not first_lib:
        # Load path
        path_str = ""
        if CONFIG_FILE.exists():
            path_str = CONFIG_FILE.read_text().strip()
        if not path_str:
            path_str = os.environ.get("LIBRARY_PATH", "").strip()
        if not path_str:
            path_str = DEFAULT_LIBRARY_PATH
        
        path_str = str(Path(path_str).expanduser().resolve())

        # Create library
        default_lib = Library(name="Default Library")
        session.add(default_lib)
        session.commit()
        session.refresh(default_lib)

        # Create folder
        folder = LibraryFolder(library_id=default_lib.id, path=path_str)
        session.add(folder)
        session.commit()

        # Update books
        session.exec(text(f"UPDATE book SET library_id = {default_lib.id} WHERE library_id IS NULL"))
        session.commit()
        logger.info("Created 'Default Library' and associated all existing books and path.")
    else:
        # Link any newly created books or orphans to the first library
        orphan_books = session.exec(select(Book).where(Book.library_id == None)).all()
        if orphan_books:
            for book in orphan_books:
                book.library_id = first_lib.id
                session.add(book)
            session.commit()
            logger.info("Associated %d orphaned books with library '%s'.", len(orphan_books), first_lib.name)


# ── Session helper ─────────────────────────────────────────────────────────────

def get_session():
    with Session(engine) as session:
        yield session


# ── Query helpers ──────────────────────────────────────────────────────────────

def get_or_create_tag(session: Session, name: str) -> Tag:
    name = name.strip().lower()
    tag = session.exec(select(Tag).where(Tag.name == name)).first()
    if not tag:
        tag = Tag(name=name)
        session.add(tag)
        session.commit()
        session.refresh(tag)
    return tag

def get_all_books(session: Session) -> list[Book]:
    return list(session.exec(select(Book)).all())

def get_book_by_path(session: Session, file_path: str) -> Book | None:
    return session.exec(select(Book).where(Book.file_path == file_path)).first()

def get_progress_logs(session: Session, book_id: int) -> list[ProgressLog]:
    return list(
        session.exec(
        select(ProgressLog)
        .where(ProgressLog.book_id == book_id)
        .order_by("timestamp")
    ).all()
    )

def add_progress_log(session: Session, book_id: int, page: int,
                     note: str | None = None) -> ProgressLog:
    log = ProgressLog(book_id=book_id, page=page, note=note)
    session.add(log)
    session.commit()
    session.refresh(log)
    return log

def get_quotes(session: Session, book_id: int) -> list[BookQuote]:
    return list(
        session.exec(
        select(BookQuote)
        .where(BookQuote.book_id == book_id)
        .order_by("date_added")
    ).all()
    )

def get_stats(session: Session, library_id: int | None = None) -> dict:
    if library_id is not None:
        books = list(session.exec(select(Book).where(Book.library_id == library_id)).all())
    else:
        books = get_all_books(session)
    total = len(books)
    read = sum(1 for b in books if b.is_read)
    in_progress = sum(1 for b in books if b.current_page > 0 and not b.is_read)

    tag_counts: dict[str, int] = {}
    for book in books:
        for tag in book.tags:
            tag_counts[tag.name] = tag_counts.get(tag.name, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "total":       total,
        "read":        read,
        "in_progress": in_progress,
        "unread":      total - read - in_progress,
        "top_tags":    [{"name": n, "count": c} for n, c in top_tags],
    }
