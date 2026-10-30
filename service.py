"""Business logic (credit rules + panel calls). Telegram-independent so it is easy to test.

Credit rule: a reseller's balance is checked first, the client is created on the panel, and the GB is
deducted ONLY after the panel confirmed success. Each reseller's operations are serialised with a lock,
so two quick requests can never overspend.
"""
import logging
import re
import threading
import time

import config
import db
from xui import GB, XUIClient, XUIError

log = logging.getLogger(__name__)

panel = XUIClient(config.XUI_URL, config.XUI_USERNAME, config.XUI_PASSWORD,
                  verify_ssl=config.XUI_VERIFY_SSL, timeout=config.XUI_TIMEOUT)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")

_locks = {}
_locks_guard = threading.Lock()


class ServiceError(Exception):
    """A problem whose message is safe to show to the user."""


def _reseller_lock(reseller_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(reseller_id, threading.Lock())


def _validate_name(name: str):
    if not NAME_RE.match(name):
        raise ServiceError("Name must be 3-24 characters: letters, digits, _ or -.")


def _validate_gb(gb: float):
    if gb <= 0 or round(gb, 3) <= 0:
        raise ServiceError("GB must be greater than 0.")
    if gb > config.MAX_GB_PER_CLIENT:
        raise ServiceError(f"Maximum per client is {config.MAX_GB_PER_CLIENT:g} GB.")


def _email(reseller_id: int, name: str) -> str:
    return f"r{reseller_id}_{name}"


def port_of(inbound_id: int):
    return config.PORT_BY_INBOUND.get(inbound_id)


def sub_link(sub_id: str) -> str:
    if not config.SUB_URL_BASE:
        return ""
    return config.SUB_URL_BASE.rstrip("/") + "/" + sub_id


def _expiry_ms(days: int) -> int:
    return 0 if days <= 0 else int((time.time() + days * 86400) * 1000)


def _active_reseller(reseller_id: int):
    r = db.get_reseller(reseller_id)
    if not r or not r["active"]:
        raise ServiceError("You are not an active reseller.")
    return r


def _rollback_panel(inbound_id: int, client_uuid: str, email: str):
    try:
        panel.delete_client(inbound_id, client_uuid)
    except Exception:
        log.critical("ORPHAN CLIENT on panel (inbound %s): %s uuid %s", inbound_id, email, client_uuid)


# ----------------------------------------------------------------- operations
def precheck_create(reseller_id: int, name: str, gb: float) -> float:
    """Validate a /create request before showing the port menu. Returns the normalised GB."""
    _validate_name(name)
    gb = round(gb, 3)
    _validate_gb(gb)
    r = _active_reseller(reseller_id)
    if db.get_client(reseller_id, name):
        raise ServiceError(f"You already have a client named '{name}'.")
    if r["balance_gb"] <= 0:
        raise ServiceError("You have no credit left. Ask the admin to add GB.")
    if r["balance_gb"] < gb:
        raise ServiceError(f"Not enough balance. You have {r['balance_gb']:g} GB, need {gb:g} GB.")
    return gb


def create_client(reseller_id: int, name: str, gb: float, days: int, inbound_id: int) -> dict:
    if inbound_id not in config.INBOUND_BY_ID:
        raise ServiceError("Unknown port selection.")
    with _reseller_lock(reseller_id):
        gb = precheck_create(reseller_id, name, gb)
        expiry = _expiry_ms(days)
        email = _email(reseller_id, name)

        # 1) create on the panel (raises XUIError on failure -> nothing is charged)
        created = panel.add_client(inbound_id, email, gb, expiry, config.LIMIT_IP, config.XUI_FLOW)

        # 2) only now take the credit
        if not db.deduct(reseller_id, gb, f"create {name} on port {port_of(inbound_id)} ({gb:g} GB)"):
            _rollback_panel(inbound_id, created["uuid"], email)
            raise ServiceError("Your balance changed. The client was not created.")

        # 3) record it locally; undo everything if that fails
        try:
            db.add_client(reseller_id, name, email, inbound_id, created["uuid"],
                          created["sub_id"], gb, expiry)
        except Exception:
            log.exception("DB write failed after panel create; rolling back %s", email)
            _rollback_panel(inbound_id, created["uuid"], email)
            db.credit(reseller_id, gb, f"refund: db failed creating {name}")
            raise ServiceError("Internal error while saving the client. Nothing was charged.")

    return {"name": name, "gb": gb, "days": days, "expiry_ms": expiry,
            "inbound_id": inbound_id, "port": port_of(inbound_id),
            "uuid": created["uuid"], "sub_id": created["sub_id"],
            "link": sub_link(created["sub_id"]),
            "balance": db.get_reseller(reseller_id)["balance_gb"]}


def topup_client(reseller_id: int, name: str, gb: float) -> dict:
    gb = round(gb, 3)
    _validate_gb(gb)
    with _reseller_lock(reseller_id):
        r = _active_reseller(reseller_id)
        client = db.get_client(reseller_id, name)
        if not client:
            raise ServiceError(f"No client named '{name}'.")
        new_total = round(client["total_gb"] + gb, 3)
        if new_total > config.MAX_GB_PER_CLIENT:
            raise ServiceError(f"Client total would exceed {config.MAX_GB_PER_CLIENT:g} GB.")
        if r["balance_gb"] < gb:
            raise ServiceError(f"Not enough balance. You have {r['balance_gb']:g} GB, need {gb:g} GB.")

        def set_total(total):
            panel.update_client_total(client["inbound_id"], client["uuid"], client["email"],
                                      client["sub_id"], total, client["expiry_ms"],
                                      config.LIMIT_IP, config.XUI_FLOW)

        set_total(new_total)                       # panel first; charge only on success
        if not db.deduct(reseller_id, gb, f"top-up {name} (+{gb:g} GB)"):
            try:
                set_total(client["total_gb"])
            except Exception:
                log.critical("Could not revert top-up of %s", client["email"])
            raise ServiceError("Your balance changed. The top-up was not applied.")
        db.set_client_total(client["id"], new_total)
    return {"name": name, "total_gb": new_total,
            "balance": db.get_reseller(reseller_id)["balance_gb"]}


def delete_client(reseller_id: int, name: str) -> dict:
    with _reseller_lock(reseller_id):
        client = db.get_client(reseller_id, name)
        if not client:
            raise ServiceError(f"No client named '{name}'.")
        refund = 0.0
        if config.REFUND_UNUSED_ON_DELETE:
            stat = panel.get_stats(client["inbound_id"]).get(client["email"])
            if stat:
                used_gb = (stat.get("up", 0) + stat.get("down", 0)) / GB
                refund = max(round(client["total_gb"] - used_gb, 3), 0.0)
        try:
            panel.delete_client(client["inbound_id"], client["uuid"])
        except XUIError as exc:
            if "not found" not in str(exc).lower():   # already gone from the panel -> fine
                raise
        db.delete_client(client["id"])
        if refund > 0:
            db.credit(reseller_id, refund, f"refund unused on delete {name}")
    return {"name": name, "refund": refund,
            "balance": db.get_reseller(reseller_id)["balance_gb"]}


def client_overview(reseller_id: int) -> list:
    """List the reseller's clients merged with live usage from the panel (one call per inbound)."""
    clients = db.list_clients(reseller_id)
    if not clients:
        return []
    stats_by_inbound = {}
    for inbound_id in {c["inbound_id"] for c in clients}:
        stats_by_inbound[inbound_id] = panel.get_stats(inbound_id)
    rows = []
    for c in clients:
        s = stats_by_inbound[c["inbound_id"]].get(c["email"])
        used = ((s.get("up", 0) + s.get("down", 0)) / GB) if s else None
        rows.append({"name": c["name"], "port": port_of(c["inbound_id"]),
                     "total_gb": c["total_gb"], "used_gb": used,
                     "expiry_ms": c["expiry_ms"],
                     "enabled": s.get("enable", True) if s else None,
                     "sub_id": c["sub_id"], "link": sub_link(c["sub_id"])})
    return rows
