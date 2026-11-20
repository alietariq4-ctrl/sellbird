"""Where Sellbird's database is, and how to open it.

A database is named by a locator: a SQLite file path (the default, and all a
self-hosted install ever uses), ``:memory:``, or ``<prefix>:<name>`` for a
storage backend registered by an extension, such as a server keeping each
workspace in a shared database.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, AsyncIterator, Protocol, runtime_checkable

BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MS = 5000
ENTRY_POINT_GROUP = "sellbird.storage"


class IntegrityError(sqlite3.IntegrityError):
    """A constraint failed in a backend other than SQLite. Subclasses sqlite3's,
    so the stores' ``except sqlite3.IntegrityError`` handlers keep working."""


@runtime_checkable
class StorageBackend(Protocol):
    async def connect(self, name: str, *, timeout: float) -> Any:
        """An open, aiosqlite-shaped connection to the database called ``name``."""

    async def check_schema(self, name: str) -> None:
        """Raise if the schema is missing or behind. Never migrates."""

    def exists(self, name: str) -> bool:
        """Whether the database is there."""


_BACKENDS: dict[str, StorageBackend] = {}
_ENTRY_POINTS_LOADED = False
_SHARED_MEM_URI = "file:sellbird_shared_mem?mode=memory&cache=shared"
_SHARED_MEM_ANCHOR: sqlite3.Connection | None = None
_MEM_LOCKS: dict[str, asyncio.Lock] = {}
_HELD_MEM_URIS: ContextVar[frozenset[str]] = ContextVar(
    "sellbird_held_mem_uris", default=frozenset()
)


def _ensure_shared_mem_anchor() -> str:
    global _SHARED_MEM_ANCHOR
    if _SHARED_MEM_ANCHOR is None:
        _SHARED_MEM_ANCHOR = sqlite3.connect(
            _SHARED_MEM_URI, uri=True, check_same_thread=False
        )
    return _SHARED_MEM_URI


@asynccontextmanager
async def _mem_uri_guard(uri: str) -> AsyncIterator[None]:
    """Serialize shared-cache in-memory connections (SQLite's shared-cache
    table locks ignore busy_timeout), while staying re-entrant per task."""
    held = _HELD_MEM_URIS.get()
    if uri in held:
        yield
        return
    lock = _MEM_LOCKS.setdefault(uri, asyncio.Lock())
    async with lock:
        token = _HELD_MEM_URIS.set(held | {uri})
        try:
            yield
        finally:
            _HELD_MEM_URIS.reset(token)


def register_backend(prefix: str, backend: StorageBackend) -> None:
    """Route locators ``<prefix>:<name>`` to ``backend``. Re-registering replaces."""
    if not prefix or not prefix.isidentifier():
        raise ValueError("a storage prefix is a short name like 'pg'")
    if not isinstance(backend, StorageBackend):
        raise TypeError("a storage backend needs connect, check_schema and exists")
    _BACKENDS[prefix] = backend


def unregister_backend(prefix: str) -> None:
    _BACKENDS.pop(prefix, None)


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name in _BACKENDS:
            continue
        register_backend(ep.name, ep.load()())


def split(locator) -> tuple[StorageBackend | None, str]:
    """The backend a locator names (None for SQLite) and the name it gives it."""
    text = str(locator)
    prefix, sep, rest = text.partition(":")
    if (
        not sep
        or text == ":memory:"
        or text.startswith("file:")
        or not prefix.isidentifier()
        or len(prefix) == 1
    ):
        # A path, :memory:, SQLite file: URI, or a Windows drive letter (C:\...).
        return None, text
    _load_entry_points()
    backend = _BACKENDS.get(prefix)
    if backend is None:
        raise LookupError(f"no storage backend is registered for '{prefix}:'")
    return backend, rest


def manages_schema(locator) -> bool:
    """True when a backend, not the SQLite migrations, owns this database's schema."""
    return split(locator)[0] is not None


@asynccontextmanager
async def connect(locator, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> AsyncIterator[Any]:
    """Open the database a locator names, and close it after."""
    backend, name = split(locator)
    if backend is None:
        import aiosqlite

        target = _ensure_shared_mem_anchor() if name == ":memory:" else name
        is_uri = target.startswith("file:")
        is_shared_mem = is_uri and "mode=memory" in target
        if is_shared_mem:
            async with _mem_uri_guard(target):
                db = await aiosqlite.connect(target, timeout=timeout, uri=True)
                await db.execute("PRAGMA synchronous=NORMAL")
                await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                try:
                    yield db
                finally:
                    await db.close()
            return
        db = await aiosqlite.connect(target, timeout=timeout, uri=is_uri)
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    else:
        db = await backend.connect(name, timeout=timeout)
    try:
        yield db
    finally:
        await db.close()


async def check_schema(locator) -> None:
    backend, name = split(locator)
    if backend is not None:
        await backend.check_schema(name)


def exists(locator) -> bool:
    """Whether the database is there: the file for SQLite, the backend's answer otherwise."""
    backend, name = split(locator)
    if backend is None:
        if name == ":memory:" or (name.startswith("file:") and "mode=memory" in name):
            return True
        return Path(name).exists()
    return backend.exists(name)
