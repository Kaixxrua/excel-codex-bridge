"""Private study storage and crash-safe submission accounting."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def private_directory(path: Path, *, new=False) -> None:
    if path.is_symlink():
        raise ValueError("A private directory cannot be a symlink")
    if path.exists() and not new:
        if not path.is_dir():
            raise ValueError("Expected a private directory")
        if os.name != "nt" and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()):
            raise ValueError("Choose an owner-only directory; existing directory permissions will not be changed")
        return
    path.mkdir(parents=True, exist_ok=not new, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    else:
        # Temporary CLI auth must not inherit permissions for other local users.
        import re
        import subprocess
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True)
        match = re.search(rb"S-1-[0-9-]+", result.stdout)
        if not match:
            raise ValueError("Cannot determine the current Windows user")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        "*" + match[0].decode("ascii") + ":(OI)(CI)F"], capture_output=True, check=True)


def write_json(path: Path, value, *, new=False) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink")
    raw = canonical(value) + b"\n"
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive(path: Path):
    """An OS lock releases after a crash; the file itself is not a stale lock."""
    if path.is_symlink():
        raise ValueError("Lock cannot be a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("This study or credential record is already in use") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BudgetExhausted(Exception):
    pass


class Ledger:
    def __init__(self, directory: Path, manifest: dict, *, create=False):
        self.directory, self.manifest = directory, manifest
        path = directory / "ledger.sqlite3"
        if path.is_symlink() or (not create and not path.is_file()):
            raise ValueError("The study ledger is missing or invalid; never recreate it to reset a budget")
        if create:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE calls(id INTEGER PRIMARY KEY, arm TEXT NOT NULL, route TEXT NOT NULL,
                  turn INTEGER NOT NULL, submitted REAL NOT NULL, result TEXT);
                CREATE TABLE outcomes(arm TEXT PRIMARY KEY, result TEXT NOT NULL);
                CREATE TABLE bindings(route TEXT PRIMARY KEY, identity TEXT NOT NULL);
                CREATE TABLE disabled(route TEXT PRIMARY KEY, reason TEXT NOT NULL);
                CREATE TABLE windows(number INTEGER PRIMARY KEY, opened REAL NOT NULL);
            """)
            self.db.execute("INSERT INTO metadata VALUES('manifest',?)", (digest(manifest),))
            self.db.commit()
        row = self.db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        if row is None or row[0] != digest(manifest):
            self.db.close()
            raise ValueError("The frozen study changed; preserve this ledger and prepare a new study")

    def close(self):
        self.db.close()

    def rows(self, table: str):
        if table not in {"calls", "outcomes", "bindings", "disabled", "windows"}:
            raise ValueError("Unknown ledger table")
        rows = [dict(row) for row in self.db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
        for row in rows:
            for key in ("result", "identity"):
                if row.get(key) is not None:
                    row[key] = json.loads(row[key])
        return rows

    def bind(self, route: str, identity: dict):
        row = self.db.execute("SELECT identity FROM bindings WHERE route=?", (route,)).fetchone()
        value = canonical(identity).decode()
        if row and row[0] != value:
            raise ValueError("Selected account, credential reference or endpoint changed; prepare a new study")
        if not row:
            self.db.execute("INSERT INTO bindings VALUES(?,?)", (route, value))
            self.db.commit()

    def reserve(self, arm: str, route: str, turn: int) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            count = self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            if count >= self.manifest["config"]["max_calls"]:
                raise BudgetExhausted()
            cursor = self.db.execute("INSERT INTO calls(arm,route,turn,submitted) VALUES(?,?,?,?)",
                                     (arm, route, turn, time.time()))
            self.db.commit()  # durable before any network/subprocess submission
            return cursor.lastrowid
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, call: int, result: dict):
        self.db.execute("UPDATE calls SET result=? WHERE id=? AND result IS NULL", (canonical(result).decode(), call))
        self.db.commit()

    def outcome(self, arm: str, result: dict):
        self.db.execute("INSERT INTO outcomes VALUES(?,?)", (arm, canonical(result).decode()))
        self.db.commit()

    def disable(self, route: str, reason: str):
        self.db.execute("INSERT OR IGNORE INTO disabled VALUES(?,?)", (route, reason))
        self.db.commit()

    def open_window(self, number: int, *, now=None):
        now = time.time() if now is None else now
        if self.db.execute("SELECT 1 FROM windows WHERE number=?", (number,)).fetchone():
            return
        if number > 1:
            expected = {arm["arm"] for arm in self.manifest["schedule"] if arm["window"] == number - 1}
            completed = {row[0] for row in self.db.execute("SELECT arm FROM outcomes")}
            if not expected <= completed:
                raise ValueError("Complete the previous confirmation window before opening the next one")
            previous = self.db.execute("SELECT opened FROM windows WHERE number=?", (number - 1,)).fetchone()
            if previous is None or now - previous[0] < self.manifest["config"]["window_gap_seconds"]:
                raise ValueError("The next frozen confirmation window is not due yet")
        self.db.execute("INSERT INTO windows VALUES(?,?)", (number, now))
        self.db.commit()
