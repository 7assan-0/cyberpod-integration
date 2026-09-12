"""Durable single-process SQLite repository with CAS and transactional events."""
import json
import os
import sqlite3
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .errors import CoreError
from .models import Event, Session


class Repository(Protocol):
    deployment_id: str

    def get(self, session_id: str) -> Session: ...
    def all(self, owner_id: str | None = None) -> list[Session]: ...
    def create(self, session: Session, event: Event, key: str | None, fingerprint: str) -> None: ...
    def save(self, session: Session, event: Event | None = None,
             evaluation: tuple[str, str] | None = None) -> None: ...
    def replay(self, owner: str, key: str, fingerprint: str) -> Session | None: ...
    def evaluation_hash(self, session_id: str, generation: int, result_id: str) -> str | None: ...
    def events(self, after: int, limit: int, session_id: str | None = None) -> list[Event]: ...
    def close(self) -> None: ...


class SQLiteRepository:
    """One control process per database. Separate DBs must not manage the same sessions."""

    def __init__(self, path: Path):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lease = open(str(path) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lease.seek(0)
                self._lease.write(b"0")
                self._lease.flush()
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lease.close()
            raise CoreError("DATABASE_IN_USE", "A Core process already owns this database", 503) from exc
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                expires TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner);
            CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(status, expires);
            CREATE TABLE IF NOT EXISTS idempotency (
                owner TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id), PRIMARY KEY(owner, key));
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
                data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id, sequence);
            CREATE TABLE IF NOT EXISTS evaluations (
                session_id TEXT NOT NULL REFERENCES sessions(id), generation INTEGER NOT NULL,
                result_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(session_id, generation, result_id));
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('deployment_id', ?)", (str(uuid4()),))
        if self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
            self.close()
            raise CoreError("DATABASE_VERSION", "Unsupported database schema version", 503)
        self.deployment_id = self.db.execute(
            "SELECT value FROM metadata WHERE key='deployment_id'").fetchone()[0]

    def get(self, session_id: str) -> Session:
        row = self.db.execute("SELECT data FROM sessions WHERE id=?", (str(session_id),)).fetchone()
        if row is None:
            raise CoreError("SESSION_NOT_FOUND", "Session not found", 404)
        return Session.model_validate_json(row[0])

    def all(self, owner_id: str | None = None) -> list[Session]:
        if owner_id is None:
            rows = self.db.execute("SELECT data FROM sessions ORDER BY rowid")
        else:
            rows = self.db.execute("SELECT data FROM sessions WHERE owner=? ORDER BY rowid", (owner_id,))
        return [Session.model_validate_json(row[0]) for row in rows]

    def replay(self, owner: str, key: str, fingerprint: str) -> Session | None:
        row = self.db.execute("SELECT fingerprint,session_id FROM idempotency WHERE owner=? AND key=?",
                              (owner, key)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise CoreError("IDEMPOTENCY_CONFLICT", "Idempotency key was used for a different request")
        return self.get(row[1])

    def _event(self, event: Event):
        self.db.execute("INSERT INTO events(session_id,data) VALUES (?,?)",
                        (str(event.session_id), event.model_dump_json()))

    def create(self, session: Session, event: Event, key: str | None, fingerprint: str):
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (
                str(session.session_id), session.owner_id, session.status.value,
                session.expires_at.isoformat(), session.revision, session.model_dump_json()))
            self._event(event)
            if key:
                self.db.execute("INSERT INTO idempotency VALUES (?,?,?,?)",
                                (session.owner_id, key, fingerprint, str(session.session_id)))

    def save(self, session: Session, event: Event | None = None,
             evaluation: tuple[str, str] | None = None):
        revision = session.revision
        persisted = session.model_copy(update={"revision": revision + 1})
        with self.db:
            changed = self.db.execute(
                "UPDATE sessions SET status=?,expires=?,revision=?,data=? WHERE id=? AND revision=?",
                (persisted.status.value, persisted.expires_at.isoformat(), persisted.revision,
                 persisted.model_dump_json(), str(session.session_id), revision)).rowcount
            if changed != 1:
                raise CoreError("REVISION_CONFLICT", "Session changed; reload before retrying")
            if event:
                self._event(event)
            if evaluation:
                self.db.execute("INSERT INTO evaluations VALUES (?,?,?,?)",
                                (str(session.session_id), session.generation, *evaluation))
        session.revision = persisted.revision

    def evaluation_hash(self, session_id: str, generation: int, result_id: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM evaluations WHERE session_id=? AND generation=? AND result_id=?",
            (str(session_id), generation, result_id)).fetchone()
        return row[0] if row else None

    def events(self, after: int, limit: int, session_id: str | None = None) -> list[Event]:
        if session_id is None:
            rows = self.db.execute("SELECT sequence,data FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                                   (after, limit))
        else:
            rows = self.db.execute(
                "SELECT sequence,data FROM events WHERE sequence>? AND session_id=? ORDER BY sequence LIMIT ?",
                (after, str(session_id), limit))
        return [Event.model_validate({**json.loads(data), "sequence": sequence}) for sequence, data in rows]

    def close(self):
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if not self._lease.closed:
            self._lease.close()

