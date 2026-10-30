"""SQLite storage: resellers, their clients and an audit ledger of every credit change."""
import sqlite3
import threading
import time

import config

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS resellers (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    balance_gb REAL NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reseller_id INTEGER NOT NULL REFERENCES resellers(tg_id),
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    inbound_id  INTEGER NOT NULL,
    uuid        TEXT NOT NULL,
    sub_id      TEXT NOT NULL,
    total_gb    REAL NOT NULL,
    expiry_ms   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    UNIQUE (reseller_id, name)
);
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    reseller_id INTEGER NOT NULL,
    delta_gb    REAL NOT NULL,
    reason      TEXT NOT NULL
);
"""


def init(path=None):
    global _conn
    with _lock:
        _conn = sqlite3.connect(path or config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        # Migration for databases created by the single-inbound version of this bot.
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(clients)")]
        if "inbound_id" not in cols:
            default_inbound = int(config.INBOUNDS[0][1])
            _conn.execute(
                f"ALTER TABLE clients ADD COLUMN inbound_id INTEGER NOT NULL DEFAULT {default_inbound}"
            )
        _conn.commit()


def _now() -> int:
    return int(time.time())


# ------------------------------------------------------------------ resellers
def add_reseller(tg_id: int, username: str = ""):
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO resellers (tg_id, username, created_at) VALUES (?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET active=1, "
            "username=COALESCE(NULLIF(excluded.username,''), username)",
            (tg_id, username, _now()),
        )


def set_active(tg_id: int, active: bool) -> bool:
    with _lock, _conn:
        cur = _conn.execute("UPDATE resellers SET active=? WHERE tg_id=?", (int(active), tg_id))
        return cur.rowcount > 0


def get_reseller(tg_id: int):
    with _lock:
        return _conn.execute("SELECT * FROM resellers WHERE tg_id=?", (tg_id,)).fetchone()


def list_resellers():
    with _lock:
        return _conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM clients c WHERE c.reseller_id=r.tg_id) AS n_clients "
            "FROM resellers r ORDER BY r.created_at"
        ).fetchall()


# -------------------------------------------------------------------- credits
def credit(tg_id: int, gb: float, reason: str):
    """Add (or with negative gb, remove) credit. Returns new balance, or None if unknown reseller
    or the change would push the balance below zero."""
    gb = round(gb, 3)
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE resellers SET balance_gb=ROUND(balance_gb+?,3) "
            "WHERE tg_id=? AND ROUND(balance_gb+?,3) >= 0",
            (gb, tg_id, gb),
        )
        if cur.rowcount == 0:
            return None
        _conn.execute(
            "INSERT INTO ledger (ts, reseller_id, delta_gb, reason) VALUES (?,?,?,?)",
            (_now(), tg_id, gb, reason),
        )
        return _conn.execute("SELECT balance_gb FROM resellers WHERE tg_id=?", (tg_id,)).fetchone()[0]


def deduct(tg_id: int, gb: float, reason: str) -> bool:
    """Atomically take gb from an ACTIVE reseller. False if inactive or not enough balance."""
    gb = round(gb, 3)
    with _lock, _conn:
        cur = _conn.execute(
            "UPDATE resellers SET balance_gb=ROUND(balance_gb-?,3) "
            "WHERE tg_id=? AND active=1 AND balance_gb>=?",
            (gb, tg_id, gb),
        )
        if cur.rowcount == 0:
            return False
        _conn.execute(
            "INSERT INTO ledger (ts, reseller_id, delta_gb, reason) VALUES (?,?,?,?)",
            (_now(), tg_id, -gb, reason),
        )
        return True


# -------------------------------------------------------------------- clients
def add_client(reseller_id, name, email, inbound_id, uuid_, sub_id, total_gb, expiry_ms):
    with _lock, _conn:
        _conn.execute(
            "INSERT INTO clients (reseller_id,name,email,inbound_id,uuid,sub_id,total_gb,"
            "expiry_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (reseller_id, name, email, inbound_id, uuid_, sub_id, total_gb, expiry_ms, _now()),
        )


def get_client(reseller_id: int, name: str):
    with _lock:
        return _conn.execute(
            "SELECT * FROM clients WHERE reseller_id=? AND name=?", (reseller_id, name)
        ).fetchone()


def list_clients(reseller_id: int):
    with _lock:
        return _conn.execute(
            "SELECT * FROM clients WHERE reseller_id=? ORDER BY created_at", (reseller_id,)
        ).fetchall()


def set_client_total(client_id: int, total_gb: float):
    with _lock, _conn:
        _conn.execute("UPDATE clients SET total_gb=? WHERE id=?", (round(total_gb, 3), client_id))


def delete_client(client_id: int):
    with _lock, _conn:
        _conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
