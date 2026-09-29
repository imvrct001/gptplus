"""
VRCT UPI Bot — guided Telegram flow for the UPI async task API.

Flow:
  /start -> [🇮🇳 Free Offer India] button
         -> paste Token/Session
         -> Checkout proxy (host:port:username:password)
         -> Update proxy   (host:port:username:password, must be DIFFERENT)
         -> live status updates -> 🎉 link

Setup:
    pip install "python-telegram-bot>=21" httpx
    export BOT_TOKEN="123456:ABC..."       # from @BotFather
    export ALLOWED_USER_IDS="111,222"      # optional whitelist
    python vrct_upi_bot.py
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BASE = "https://dasaobi.online/upi/"
BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

POLL_EVERY = 2
POLL_TIMEOUT = 600

DATA = Path("data")
DATA.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # never log URLs / tokens

ASK_TOKEN, ASK_CHECKOUT, ASK_UPDATE = range(3)

STATUS_EMOJI = {
    "queued": "🕐",
    "running": "⚙️",
    "cancel_requested": "🛑",
    "succeeded": "✅",
    "failed": "❌",
    "cancelled": "🚫",
}

# ------------------------------------------------------------ HTTP (cookie per user)
clients: dict[int, httpx.AsyncClient] = {}


def cookie_file(uid: int) -> Path:
    return DATA / f"cookies_{uid}.json"


def get_client(uid: int) -> httpx.AsyncClient:
    if uid not in clients:
        c = httpx.AsyncClient(base_url=BASE, timeout=30, follow_redirects=True)
        f = cookie_file(uid)
        if f.exists():
            for k, v in json.loads(f.read_text()).items():
                c.cookies.set(k, v, domain="dasaobi.online", path="/upi/")
        clients[uid] = c
    return clients[uid]


async def api(uid: int, method: str, path: str, **kw) -> httpx.Response:
    c = get_client(uid)
    r = await c.request(method, path, **kw)
    jar = {ck.name: ck.value for ck in c.cookies.jar if ck.name == "upi_paid_session"}
    if jar:
        cookie_file(uid).write_text(json.dumps(jar))
    return r


# ------------------------------------------------------------ helpers
def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


async def safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


def parse_token(text: str) -> str | None:
    """Accepts a raw AT, 'Bearer <AT>', or the full session JSON."""
    text = text.strip()
    if text.startswith("{"):
        try:
            j = json.loads(text)
            text = j.get("accessToken") or j.get("access_token") or ""
        except Exception:
            return None
    text = re.sub(r"^Bearer\s+", "", text, flags=re.I).strip()
    if len(text) >= 100 and re.fullmatch(r"[A-Za-z0-9_\-\.]+", text):
        return text
    return None


def parse_proxy(text: str) -> str | None:
    """host:port:username:password  ->  http://user:pass@host:port"""
    parts = text.strip().split(":")
    if len(parts) < 4:
        return None
    host, port, user = parts[0], parts[1], parts[2]
    pw = ":".join(parts[3:])
    if not host or not port.isdigit() or not user:
        return None
    return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:{port}"


def bar(p: int) -> str:
    p = max(0, min(100, int(p or 0)))
    n = p // 10
    return "▰" * n + "▱" * (10 - n) + f" {p}%"


def err_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        return str(j.get("error") or j.get("message") or j)[:400]
    except Exception:
        return r.text[:300]


def esc(s) -> str:
    return html.escape(str(s))


# ------------------------------------------------------------ conversation
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🇮🇳 Free Offer India", callback_data="offer")]]
    )
    await update.message.reply_text(
        "✨ <b>VRCT UPI Bot</b> ✨\n\n"
        "🚀 Get your India free-offer checkout link in a few steps.\n\n"
        "👇 Tap the button below to begin.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def begin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return ConversationHandler.END
    ctx.user_data.clear()
    await q.message.reply_text(
        "🔑 <b>Step 1/3</b>\n\n"
        "Paste your <b>Access Token / Session</b> 👇\n"
        "<i>(raw token, Bearer token, or full session JSON)</i>\n\n"
        "🔒 I'll delete your message right after reading it.\n"
        "❌ /cancel to stop.",
        parse_mode=ParseMode.HTML,
    )
    return ASK_TOKEN


async def got_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    token = parse_token(msg.text or "")
    await safe_delete(msg)
    if not token:
        await ctx.bot.send_message(
            msg.chat_id,
            "⚠️ That doesn't look like a valid token/session.\n🔁 Please paste it again.",
        )
        return ASK_TOKEN
    ctx.user_data["token"] = token
    await ctx.bot.send_message(
        msg.chat_id,
        "✅ Token received!\n\n"
        "🌐 <b>Step 2/3 — Checkout Proxy (IN)</b>\n\n"
        "Enter in this format 👇\n"
        "<code>host:port:username:password</code>",
        parse_mode=ParseMode.HTML,
    )
    return ASK_CHECKOUT


async def got_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    proxy = parse_proxy(msg.text or "")
    await safe_delete(msg)
    if not proxy:
        await ctx.bot.send_message(
            msg.chat_id,
            "⚠️ Wrong format.\nUse: <code>host:port:username:password</code>\n🔁 Try again.",
            parse_mode=ParseMode.HTML,
        )
        return ASK_CHECKOUT
    ctx.user_data["checkout"] = proxy
    await ctx.bot.send_message(
        msg.chat_id,
        "✅ Checkout proxy saved!\n\n"
        "🔄 <b>Step 3/3 — Update Proxy (IN)</b>\n\n"
        "⚠️ This must be <b>different</b> from the checkout proxy.\n"
        "Format 👇\n"
        "<code>host:port:username:password</code>",
        parse_mode=ParseMode.HTML,
    )
    return ASK_UPDATE


async def got_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    proxy = parse_proxy(msg.text or "")
    await safe_delete(msg)
    if not proxy:
        await ctx.bot.send_message(
            msg.chat_id,
            "⚠️ Wrong format.\nUse: <code>host:port:username:password</code>\n🔁 Try again.",
            parse_mode=ParseMode.HTML,
        )
        return ASK_UPDATE
    if proxy == ctx.user_data.get("checkout"):
        await ctx.bot.send_message(
            msg.chat_id,
            "🚫 Update proxy can't be the same as checkout proxy.\n🔁 Send a different one.",
        )
        return ASK_UPDATE

    body = {
        "access_token": ctx.user_data["token"],
        "checkout_proxy": ctx.user_data["checkout"],
        "update_proxy": proxy,
    }
    ctx.user_data.clear()
    uid = msg.from_user.id
    status = await ctx.bot.send_message(msg.chat_id, "🚀 Submitting your task…")

    try:
        r = await api(uid, "POST", "api/tasks", json=body)
    except httpx.HTTPError:
        await status.edit_text(
            "⚠️ Network error while submitting.\n"
            "The task may already exist — check with /tasks before retrying."
        )
        return ConversationHandler.END
    finally:
        body.clear()

    if r.status_code not in (200, 202):
        await status.edit_text(f"❌ Rejected (HTTP {r.status_code})\n{esc(err_text(r))}")
        return ConversationHandler.END

    task_id = r.json().get("task_id")
    asyncio.create_task(watch(uid, task_id, status))  # don't block the conversation
    return ConversationHandler.END


async def cancel_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("🛑 Cancelled. Send /start to begin again.")
    return ConversationHandler.END


# ------------------------------------------------------------ live status
def stop_kb(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Cancel task", callback_data=f"stop:{task_id}")]]
    )


async def watch(uid: int, task_id: str, status_msg):
    started = time.time()
    last = None
    while time.time() - started < POLL_TIMEOUT:
        try:
            r = await api(uid, "GET", f"api/tasks/{task_id}")
        except httpx.HTTPError:
            await asyncio.sleep(POLL_EVERY)
            continue

        if r.status_code == 404:
            await status_msg.edit_text("❌ Task not found (session expired or record cleared).")
            return
        if r.status_code != 200:
            await asyncio.sleep(POLL_EVERY)
            continue

        j = r.json()
        st = j.get("status")

        if st == "succeeded":
            url = (j.get("result") or {}).get("upi_url")
            if url:
                await status_msg.edit_text(
                    "🎉🎊 <b>Congratulations!</b> 🎊🎉\n\n"
                    "✅ Your checkout link is ready 👇\n\n"
                    f"🔗 <code>{esc(url)}</code>\n\n"
                    "💡 Tap the link to copy it.\n"
                    "⚡ Powered by VRCT",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await status_msg.edit_text("⚠️ Finished but no link was returned. Check /tasks.")
            return
        if st == "failed":
            await status_msg.edit_text(
                f"❌ <b>Task failed</b>\n\n📛 {esc(j.get('error', 'Unknown error'))}\n\n"
                "🔁 Send /start to try again.",
                parse_mode=ParseMode.HTML,
            )
            return
        if st == "cancelled":
            await status_msg.edit_text("🚫 Task cancelled.")
            return

        elapsed = int(time.time() - started)
        text = (
            "📡 <b>Live Server Update</b>\n\n"
            f"{STATUS_EMOJI.get(st, '⏳')} Status: <b>{esc(st)}</b>\n"
            f"🧭 Stage: <b>{esc(j.get('stage', '-'))}</b>\n"
            f"📊 {bar(j.get('progress', 0))}\n"
            f"⏱ Elapsed: {elapsed}s\n\n"
            f"🆔 <code>{esc(task_id)}</code>"
        )
        key = (st, j.get("stage"), j.get("progress"))
        if key != last:
            last = key
            try:
                await status_msg.edit_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=stop_kb(task_id)
                )
            except Exception:
                pass
        await asyncio.sleep(POLL_EVERY)

    await status_msg.edit_text(
        f"⌛ Still running after {POLL_TIMEOUT // 60} min.\n"
        f"Check later with /tasks\n🆔 <code>{esc(task_id)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def stop_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    task_id = q.data.split(":", 1)[1]
    try:
        r = await api(q.from_user.id, "POST", f"api/tasks/{task_id}/cancel")
        ok = r.status_code in (200, 202)
    except httpx.HTTPError:
        ok = False
    await q.answer("🛑 Cancel requested" if ok else "⚠️ Couldn't cancel", show_alert=not ok)


async def tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        r = await api(update.effective_user.id, "GET", "api/tasks")
    except httpx.HTTPError:
        await update.message.reply_text("⚠️ Network error, try again.")
        return
    if r.status_code != 200:
        await update.message.reply_text(f"⚠️ Error {r.status_code}: {err_text(r)}")
        return
    items = r.json().get("tasks", [])
    if not items:
        await update.message.reply_text("📭 No tasks yet.")
        return
    lines = []
    for t in items[-10:]:
        s = f"{STATUS_EMOJI.get(t.get('status'), '⏳')} {t.get('task_id')} — {t.get('status')}"
        url = (t.get("result") or {}).get("upi_url")
        if url:
            s += f"\n🔗 {url}"
        elif t.get("error"):
            s += f"\n📛 {t['error']}"
        lines.append(s)
    await update.message.reply_text("\n\n".join(lines))


def start_health_server():
    """Tiny HTTP server so Render (Web Service) sees an open port and cron-job can ping it."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"VRCT UPI Bot is alive")

        def log_message(self, *a):
            pass

    port = int(os.getenv("PORT", "10000"))
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), H).serve_forever(), daemon=True
    ).start()


def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    text_only = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin, pattern="^offer$"),
            CommandHandler("new", lambda u, c: start(u, c)),
        ],
        states={
            ASK_TOKEN: [MessageHandler(text_only, got_token)],
            ASK_CHECKOUT: [MessageHandler(text_only, got_checkout)],
            ASK_UPDATE: [MessageHandler(text_only, got_update)],
        },
        fallbacks=[CommandHandler("cancel", cancel_flow)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(stop_task, pattern="^stop:"))
    app.add_handler(CommandHandler("tasks", tasks))
    app.run_polling()


if __name__ == "__main__":
    main()
