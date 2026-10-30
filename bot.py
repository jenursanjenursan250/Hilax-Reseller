"""Telegram reseller bot for 3x-ui  (pyTelegramBotAPI / telebot)."""
import functools
import html
import logging
import logging.handlers
import threading
import time
import uuid
from datetime import datetime

import telebot
from telebot import types

import config
import db
import service
from xui import XUIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML", threaded=True)
esc = html.escape

# Pending /create requests waiting for a port choice: token -> {uid, name, gb, days, ts}
_pending = {}
_pending_lock = threading.Lock()


# ------------------------------------------------------------------- helpers
def reply(message, text: str, **kwargs):
    """Send text, splitting it if it exceeds Telegram's 4096-char limit."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)
    for chunk in chunks:
        if chunk.strip():
            bot.send_message(message.chat.id, chunk, disable_web_page_preview=True, **kwargs)


def user_error_text(exc: Exception, uid) -> str:
    """Turn any exception into a safe, friendly message."""
    if isinstance(exc, service.ServiceError):
        return f"❌ {esc(str(exc))}"
    if isinstance(exc, XUIError):
        log.error("Panel error for user %s: %s", uid, exc)
        return f"❌ Panel error: {esc(str(exc))}"
    log.exception("Unhandled error for user %s", uid, exc_info=exc)
    return "❌ Unexpected error. Please try again later."


def guard(role: str = ""):
    """Role check + uniform error handling. role: 'admin', 'reseller' or '' (anyone)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(message):
            uid = message.from_user.id
            try:
                if role == "admin" and uid not in config.ADMIN_IDS:
                    return reply(message, "⛔ Admin only.")
                if role == "reseller":
                    r = db.get_reseller(uid)
                    if not r or not r["active"]:
                        return reply(message, "⛔ You are not an active reseller.\n"
                                              f"Your Telegram ID: <code>{uid}</code>")
                return fn(message)
            except Exception as exc:
                reply(message, user_error_text(exc, uid))
        return wrapper
    return deco


def args_of(message) -> list:
    return (message.text or "").split()[1:]


def need(message, n: int, usage: str) -> list:
    a = args_of(message)
    if len(a) < n:
        raise service.ServiceError(f"Usage: {usage}")
    return a


def to_float(text: str) -> float:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise service.ServiceError(f"'{text}' is not a valid number.")
    if value != value or value in (float("inf"), float("-inf")):
        raise service.ServiceError("Invalid number.")
    return value


def to_int(text: str, label: str = "number") -> int:
    try:
        return int(text)
    except ValueError:
        raise service.ServiceError(f"'{text}' is not a valid {label}.")


def fmt_expiry(ms: int) -> str:
    return "never" if not ms else datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _purge_pending():
    now = time.time()
    for token in [t for t, p in _pending.items() if now - p["ts"] > config.PENDING_TTL]:
        _pending.pop(token, None)


# ----------------------------------------------------------------- everyone
@bot.message_handler(commands=["start", "help"])
@guard()
def cmd_help(message):
    uid = message.from_user.id
    text = [f"👋 <b>VPN Reseller Bot</b>\nYour Telegram ID: <code>{uid}</code>"]
    if uid in config.ADMIN_IDS:
        text.append(
            "\n<b>Admin commands</b>\n"
            "/addreseller &lt;tg_id&gt; [username]\n"
            "/removereseller &lt;tg_id&gt;  (deactivate)\n"
            "/addgb &lt;tg_id&gt; &lt;gb&gt;\n"
            "/subgb &lt;tg_id&gt; &lt;gb&gt;\n"
            "/resellers\n"
            "/panel  (test panel + inbounds)"
        )
    r = db.get_reseller(uid)
    if r and r["active"]:
        ports = ", ".join(str(p) for p, _ in config.INBOUNDS)
        text.append(
            "\n<b>Reseller commands</b>\n"
            "/balance\n"
            f"/create &lt;name&gt; &lt;gb&gt; [days]  → then pick a port ({ports}). "
            f"Default {config.DEFAULT_DAYS} days, 0 = no expiry\n"
            "/topup &lt;name&gt; &lt;gb&gt;\n"
            "/myclients\n"
            "/link &lt;name&gt;\n"
            "/delete &lt;name&gt;"
        )
    elif uid not in config.ADMIN_IDS:
        text.append("\nYou are not a reseller yet. Send your ID above to the admin.")
    reply(message, "\n".join(text))


# -------------------------------------------------------------------- admin
@bot.message_handler(commands=["addreseller"])
@guard("admin")
def cmd_addreseller(message):
    a = need(message, 1, "/addreseller <tg_id> [username]")
    tg_id = to_int(a[0], "Telegram ID")
    db.add_reseller(tg_id, a[1].lstrip("@") if len(a) > 1 else "")
    reply(message, f"✅ Reseller <code>{tg_id}</code> is active. Use /addgb to give credit.")


@bot.message_handler(commands=["removereseller"])
@guard("admin")
def cmd_removereseller(message):
    a = need(message, 1, "/removereseller <tg_id>")
    tg_id = to_int(a[0], "Telegram ID")
    if not db.set_active(tg_id, False):
        raise service.ServiceError("Unknown reseller.")
    reply(message, f"✅ Reseller <code>{tg_id}</code> deactivated (existing clients keep working).")


def _adjust(message, sign: int):
    cmd = "/addgb" if sign > 0 else "/subgb"
    a = need(message, 2, f"{cmd} <tg_id> <gb>")
    tg_id, gb = to_int(a[0], "Telegram ID"), to_float(a[1])
    if gb <= 0:
        raise service.ServiceError("GB must be greater than 0.")
    new = db.credit(tg_id, sign * gb, f"admin {message.from_user.id}: {cmd} {gb:g}")
    if new is None:
        raise service.ServiceError("Unknown reseller, or that would make the balance negative.")
    reply(message, f"✅ New balance of <code>{tg_id}</code>: <b>{new:g} GB</b>")
    if sign > 0:
        try:
            bot.send_message(tg_id, f"💳 {gb:g} GB credit added. Balance: <b>{new:g} GB</b>")
        except Exception:
            log.info("Could not notify reseller %s", tg_id)


@bot.message_handler(commands=["addgb"])
@guard("admin")
def cmd_addgb(message):
    _adjust(message, +1)


@bot.message_handler(commands=["subgb"])
@guard("admin")
def cmd_subgb(message):
    _adjust(message, -1)


@bot.message_handler(commands=["resellers"])
@guard("admin")
def cmd_resellers(message):
    rows = db.list_resellers()
    if not rows:
        return reply(message, "No resellers yet. Use /addreseller.")
    lines = ["<b>Resellers</b>"]
    for r in rows:
        name = f" @{esc(r['username'])}" if r["username"] else ""
        state = "🟢" if r["active"] else "🔴"
        lines.append(f"{state} <code>{r['tg_id']}</code>{name} — {r['balance_gb']:g} GB, "
                     f"{r['n_clients']} clients")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["panel"])
@guard("admin")
def cmd_panel(message):
    service.panel.check()
    lines = ["✅ Panel login OK."]
    for port, inbound_id in config.INBOUNDS:
        try:
            n = len(service.panel.get_stats(inbound_id))
            lines.append(f"• Port {port} → inbound {inbound_id}: OK ({n} clients)")
        except XUIError as exc:
            lines.append(f"• Port {port} → inbound {inbound_id}: ⚠️ {esc(str(exc))}")
    reply(message, "\n".join(lines))


# ----------------------------------------------------------------- reseller
@bot.message_handler(commands=["balance"])
@guard("reseller")
def cmd_balance(message):
    r = db.get_reseller(message.from_user.id)
    reply(message, f"💳 Balance: <b>{r['balance_gb']:g} GB</b>")


@bot.message_handler(commands=["create"])
@guard("reseller")
def cmd_create(message):
    uid = message.from_user.id
    a = need(message, 2, "/create <name> <gb> [days]")
    name, gb = a[0], to_float(a[1])
    days = to_int(a[2], "number of days") if len(a) > 2 else config.DEFAULT_DAYS
    if days < 0 or days > 3650:
        raise service.ServiceError("Days must be between 0 and 3650.")

    # Validates name / size / credit BEFORE showing the menu. Nothing is charged yet.
    gb = service.precheck_create(uid, name, gb)

    token = uuid.uuid4().hex[:10]
    with _pending_lock:
        _purge_pending()
        _pending[token] = {"uid": uid, "name": name, "gb": gb, "days": days, "ts": time.time()}

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[types.InlineKeyboardButton(f"🔌 Port {port}", callback_data=f"c:{token}:{inbound_id}")
             for port, inbound_id in config.INBOUNDS])
    kb.add(types.InlineKeyboardButton("✖️ Cancel", callback_data=f"x:{token}"))
    expiry = "no expiry" if days == 0 else f"{days} days"
    bot.send_message(
        message.chat.id,
        f"Create <b>{esc(name)}</b> — {gb:g} GB, {expiry}\nChoose the port:",
        reply_markup=kb,
    )


def _take_pending(call, token: str):
    """Pop a pending request if it belongs to this user and hasn't expired."""
    with _pending_lock:
        _purge_pending()
        pend = _pending.get(token)
        if not pend or pend["uid"] != call.from_user.id:
            return None
        return _pending.pop(token)


def _edit(call, text: str):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              disable_web_page_preview=True)
    except Exception:
        log.warning("Could not edit message; sending a new one")
        bot.send_message(call.message.chat.id, text, disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("c:", "x:")))
def on_create_choice(call):
    uid = call.from_user.id
    try:
        parts = call.data.split(":")
        token = parts[1]
        pend = _take_pending(call, token)
        if pend is None:
            return bot.answer_callback_query(
                call.id, "This menu expired. Send /create again.", show_alert=True)
        if parts[0] == "x":
            bot.answer_callback_query(call.id, "Cancelled")
            return _edit(call, "✖️ Cancelled.")

        inbound_id = int(parts[2])
        bot.answer_callback_query(call.id, "Creating…")
        _edit(call, f"⏳ Creating <b>{esc(pend['name'])}</b> on port "
                    f"{service.port_of(inbound_id)}…")
        res = service.create_client(uid, pend["name"], pend["gb"], pend["days"], inbound_id)
        lines = [
            f"✅ Client <b>{esc(res['name'])}</b> created",
            f"🔌 Port: {res['port']}",
            f"📦 Data: {res['gb']:g} GB",
            f"⏳ Expires: {fmt_expiry(res['expiry_ms'])}",
            f"💳 Balance left: {res['balance']:g} GB",
        ]
        if res["link"]:
            lines.append(f"\n🔗 Subscription:\n<code>{esc(res['link'])}</code>")
        else:
            lines.append(f"\n🆔 Client ID: <code>{res['uuid']}</code>\n"
                         "(Set SUB_URL_BASE in .env to also get a subscription link.)")
        _edit(call, "\n".join(lines))
    except Exception as exc:
        try:
            _edit(call, user_error_text(exc, uid))
        except Exception:
            log.exception("Failed to report error to user %s", uid)


@bot.message_handler(commands=["topup"])
@guard("reseller")
def cmd_topup(message):
    a = need(message, 2, "/topup <name> <gb>")
    res = service.topup_client(message.from_user.id, a[0], to_float(a[1]))
    reply(message, f"✅ <b>{esc(res['name'])}</b> now has {res['total_gb']:g} GB total.\n"
                   f"💳 Balance left: {res['balance']:g} GB")


@bot.message_handler(commands=["myclients"])
@guard("reseller")
def cmd_myclients(message):
    rows = service.client_overview(message.from_user.id)
    if not rows:
        return reply(message, "You have no clients yet. Use /create.")
    lines = ["<b>Your clients</b>"]
    for c in rows:
        used = "?" if c["used_gb"] is None else f"{c['used_gb']:.2f}"
        flag = "🔴" if c["enabled"] is False else "🟢"
        lines.append(f"{flag} <b>{esc(c['name'])}</b> (port {c['port']}) — "
                     f"{used}/{c['total_gb']:g} GB, exp {fmt_expiry(c['expiry_ms'])}")
    reply(message, "\n".join(lines))


@bot.message_handler(commands=["link"])
@guard("reseller")
def cmd_link(message):
    a = need(message, 1, "/link <name>")
    c = db.get_client(message.from_user.id, a[0])
    if not c:
        raise service.ServiceError(f"No client named '{a[0]}'.")
    link = service.sub_link(c["sub_id"])
    if not link:
        raise service.ServiceError("SUB_URL_BASE is not configured on the bot.")
    reply(message, f"🔗 <b>{esc(c['name'])}</b>\n<code>{esc(link)}</code>")


@bot.message_handler(commands=["delete"])
@guard("reseller")
def cmd_delete(message):
    a = need(message, 1, "/delete <name>")
    res = service.delete_client(message.from_user.id, a[0])
    extra = f"\n♻️ Refunded {res['refund']:g} GB unused data." if res["refund"] else ""
    reply(message, f"🗑 Client <b>{esc(res['name'])}</b> deleted.{extra}\n"
                   f"💳 Balance: {res['balance']:g} GB")


# ---------------------------------------------------------------------- main
@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    bot.send_message(message.chat.id, "Unknown command. Send /help.")


def main():
    db.init()
    log.info("Starting bot (admins: %s, ports: %s)", sorted(config.ADMIN_IDS),
             [p for p, _ in config.INBOUNDS])
    try:
        service.panel.check()
    except XUIError as exc:
        # Do not crash: panel may be briefly down. The client re-logs in on demand.
        log.warning("Panel not reachable at startup: %s", exc)
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception:
            log.exception("Polling crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
