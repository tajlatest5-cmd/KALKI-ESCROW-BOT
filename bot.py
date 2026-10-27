import os
import re
import html
import sqlite3
import logging
import unicodedata
import time as tm
from contextlib import closing
from datetime import datetime, time, timedelta, timezone

from telegram import (
    Update,
    ReplyParameters,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatJoinRequestHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ======================================================================
# BOT TOKEN: YAHAN DAALO (sirf yahi ek jagah)
#   Neeche wali line mein "YAHAN_APNA_TOKEN_DAALO" hatao aur BotFather ka
#   poora token likho, quotes " " ke ANDAR hi, bina space ke.
#   Railway Variables mein BOT_TOKEN ho to wahi use hoga.
#   GitHub repo PRIVATE rakhna. Token kisi ko mat bhejna.
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN","8864009675:AAHcjgEmy-D2xdA4zyE7CdudXtgXIsOf80I").strip() or "8864009675:AAHcjgEmy-D2xdA4zyE7CdudXtgXIsOf80I"

# --- SETTINGS (baaki settings) ---
# Group ke ID. Test group jodna ho to comma lagakar uska ID likho,
# jaise [-1004383728292, -100123456789]. ID /chatid se milta hai.
GROUP_IDS = [-1004199368031]

# Sirf in IDs wale log /add aur /close chala sakte hain.
ADMIN_IDS = [7054168366, 5797432326, 5928049700, 5803014746, 8359951414, 7161559973, 8161057089, 7396561160,]

# Database file. Railway Volume lagaya ho to DB_PATH=/data/bot.db rakho.
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Spam: itne second mein ek hi message itni baar bheja to warning
SPAM_COUNT = 5
SPAM_SECONDS = 60

# "fees" likhne ke baad amount bhejne ke liye kitne second milte hain
FEE_WAIT_SECONDS = 300

# Bhara hua form aane par bot ye likhega
FORM_OK_TEXT = "Maal aa gaya"

# /add aur /close mein payment ka photo
# True karoge to bina photo ke /add aur /close nahi chalenge
PAYMENT_PHOTO_REQUIRED = False
# True karoge to admin ka photo wala message result ke baad delete ho jayega
# (bot ko "Delete messages" right chahiye)
DELETE_ADMIN_PHOTO = False

# Lock/unlock time (UTC mein): 18:30 UTC = 12:00 AM IST
# aur 00:30 UTC = 6:00 AM IST
LOCK_TIME = time(18, 30, tzinfo=timezone.utc)
UNLOCK_TIME = time(0, 30, tzinfo=timezone.utc)

IST = timezone(timedelta(hours=5, minutes=30))

# --- PREMIUM EMOJIS ---

def ce(eid, fallback):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

STAR = ce("5958376256788502078", "⭐️")
CHAT = ce("5891243564309942507", "💬")
BOLT = ce("5843553939672274145", "⚡️")
SIGN = ce("5994636050033545139", "🪧")
BAG = ce("5811989245761426317", "💰")
GEM = ce("5771755323572359189", "💎")
TAGI = ce("5985433648810171091", "🏷")
CARD = ce("5936017305585586269", "🪪")
BUYER = ce("5877530150345641603", "👤")
SELLER = ce("5954175920506933873", "👤")
SHIELD = ce("5926783847453692661", "🛡")
DONE = ce("5920052658743283381", "✅")
FEE = ce("5967548335542767952", "💳")
CHART = ce("5913702317667913862", "📊")
FILE = ce("5956561916573782596", "📄")
TREND = ce("5994378914636500516", "📈")
PIN = ce("5796440171364749940", "📌")
WARN = ce("5881702736843511327", "⚠️")
LOCK = ce("5879895758202735862", "🔒")
UNLOCK = ce("6034962180875490251", "🔓")
DETAIL_E = ce("5257965174979042426", "📝")
TIME_E = ce("5258258882022612173", "⏲")

# /close ke emoji
CLOSE_ID_E = TAGI
CLOSE_ESC_E = SHIELD

DIVIDER = "╍╍╍╍╍╍╍╍╍╍╍╍╍╍"

# --- LOCK / UNLOCK PERMISSIONS ---
_PERMS = ("send_messages", "send_audios", "send_documents", "send_photos",
          "send_videos", "send_video_notes", "send_voice_notes", "send_polls",
          "send_other_messages", "add_web_page_previews", "invite_users")

def make_perms(on):
    d = {"can_" + n: on for n in _PERMS}
    d.update(can_change_info=False, can_pin_messages=False, can_manage_topics=False)
    return ChatPermissions(**d)

LOCKED, UNLOCKED = make_perms(False), make_perms(True)

# --- HELPERS ---

def esc(x):
    return html.escape(str(x))

def inr(x):
    return f"₹{x:,.2f}"

def money(x):
    return f"₹{x:.2f}"

def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name or "User")}</a>'

def user_tag(user):
    if user.username:
        return f"@{esc(user.username)}"
    return mention(user.id, user.first_name)

def now_text():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M")

def is_admin(update):
    user = update.effective_user
    return user is not None and user.id in ADMIN_IDS

def in_group(update):
    chat = update.effective_chat
    return chat is not None and chat.id in GROUP_IDS

async def reply_html(message, text, **kwargs):
    return await message.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)

async def is_group_admin(context, chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# --- DATABASE ---
G_SQL = "group_id IN (" + ",".join("?" for _ in GROUP_IDS) + ")"
G_ARGS = tuple(GROUP_IDS)

def _run(sql, params, mode):
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute(sql, params)
        if mode == "one":
            result = cur.fetchone()
        elif mode == "all":
            result = cur.fetchall()
        else:
            result = None
        con.commit()
        return result

def db_one(sql, params=()):
    return _run(sql, params, "one")

def db_all(sql, params=()):
    return _run(sql, params, "all")

def db_exec(sql, params=()):
    return _run(sql, params, None)

def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    db_exec(
        "CREATE TABLE IF NOT EXISTS deals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "trade_id TEXT, group_id INTEGER, amount REAL, "
        "buyer TEXT, seller TEXT, escrower_id INTEGER, "
        "escrower_name TEXT, status TEXT, "
        "created_at TEXT, closed_at TEXT)"
    )
    extra = (
        ("form_msg_id", "INTEGER"),
        ("form_user_id", "INTEGER"),
        ("form_user_name", "TEXT"),
        ("released", "REAL"),
        ("buyer_show", "TEXT"),
        ("seller_show", "TEXT"),
        ("add_photo", "TEXT"),
        ("close_photo", "TEXT"),
        ("hold_at", "TEXT"),
        ("escrower_tag", "TEXT"),
    )
    for col, typ in extra:
        try:
            db_exec(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

TRADE_START = 1  # Trade ID is number se neeche kabhi nahi jayega

def new_trade_id():
    # Pichhla number alag table mein yaad rakhta hai, deals ginne par nirbhar nahi
    db_exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
    last = db_one("SELECT v FROM meta WHERE k='last_trade_no'")
    last = last[0] if last else 0
    nums = [
        int(m.group(1))
        for (t,) in db_all("SELECT trade_id FROM deals")
        if t and (m := re.search(r"-(\d+)$", t))
    ]
    n = max(last, max(nums, default=0), TRADE_START - 1) + 1
    while db_one("SELECT 1 FROM deals WHERE trade_id=?", (f"DL-JERRY-{n}",)):
        n += 1
    db_exec("INSERT OR REPLACE INTO meta (k, v) VALUES ('last_trade_no', ?)", (n,))
    return f"DL-RANCHO-{n}"

# --- PARSING (amount, form) ---
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

def parse_amount(text):
    cleaned = (text or "").lower()
    cleaned = re.sub(r"rs\.?|inr|₹|,|\s", "", cleaned)
    if not re.fullmatch(r"\d+(?:\.\d+)?", cleaned):
        return None
    value = float(cleaned)
    return value if 0 < value < 1e9 else None

def find_amount(text):
    match = NUM_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(0).replace(",", ""))
    return value if 0 < value < 1e9 else None

def username_in(text):
    match = re.search(r"@([A-Za-z0-9_]{4,32})", text or "")
    return match.group(1).lower() if match else None

FORM_LABELS = {
    "SELLER": "SELLER",
    "BUYER": "BUYER",
    "DEAL DETAIL": "DEAL DETAIL",
    "ITEM": "DEAL DETAIL",
    "DEAL AMOUNT": "DEAL AMOUNT",
    "AMOUNT": "DEAL AMOUNT",
    "EXPECTED TIME TO COMPLETE DEAL": "EXPECTED TIME",
    "EXPECTED TIME": "EXPECTED TIME",
    "T/C (IF ANY)": "T/C",
    "T/C": "T/C",
}
FORM_REQUIRED = [
    "SELLER",
    "BUYER",
    "DEAL DETAIL",
    "DEAL AMOUNT",
    "EXPECTED TIME",
]

BLANK_FORM = "\n\n".join(
    [
        "ESCROW DEAL",
        "• SELLER : ",
        "• BUYER : ",
        "• DEAL DETAIL : ",
        "• DEAL AMOUNT : ",
        "• EXPECTED TIME TO COMPLETE DEAL : ",
        "• T/C (IF ANY) : ",
        "• ESCROW MORE THAN ESCROWER LIMIT AT YOUR OWN RISK",
    ]
)

def parse_fields(text):
    norm = unicodedata.normalize("NFKC", text or "")
    values = {}
    for line in norm.splitlines():
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        label = re.sub(r"[^A-Za-z/() ]", "", label)
        label = " ".join(label.split()).upper()
        key = FORM_LABELS.get(label)
        if key and key not in values:
            values[key] = value.strip()
    return values

def parse_form(text):
    norm = unicodedata.normalize("NFKC", text or "").upper()
    if "ESCROW DEAL" not in norm and "ESCROW FORM" not in norm:
        return None
    values = parse_fields(text)
    return values if len(values) >= 3 else None

def is_empty(value):
    return re.sub(r"[\s_\-.•·:|]+", "", value or "") == ""

# --- FEES ---

def calc_fee(amount):
    # Abhi sab deals par fee ZERO hai
    return "Free (0%)", 0.0

def fee_text(amount=None):
    lines = [
        f"{DONE} <b>100 DEAL TAK KOI BHI FEE NAHI HAI!</b>",
        "",
        f"{STAR} <b>ENJOY KARO</b>",
        f"{SHIELD} <b>TRUSTED &amp; VERIFIED ESCROWER SUPPORT</b>",
    ]
    return "\n".join(lines)

# --- DISPLAY HELPERS (/add aur /close ke liye) ---
NO_TC = ("na", "n/a", "none", "no", "nil", "nahi", "nahin", "-")

def party_show(value):
    value = (value or "").strip()
    if is_empty(value):
        return "-"
    found = re.search(r"@([A-Za-z0-9_]{4,32})", value)
    return "@" + found.group(1) if found else value[:60]

def shown(show, plain_name):
    if show:
        return show
    return "@" + plain_name if plain_name and plain_name != "-" else "-"

def plain(value):
    value = (value or "").strip()
    return "-" if is_empty(value) else value[:200]

def tc_show(value):
    value = (value or "").strip()
    if is_empty(value) or value.lower().strip(" .") in NO_TC:
        return "Not Provided"
    return value[:200]

# --- /start DASHBOARD ---

def group_welcome_text(users):
    names = ", ".join(user_tag(u) for u in users)
    lines = [
        f"{STAR} <b>Welcome {esc(name)}!</b>",
        DIVIDER,
        f"{SHIELD} <b>Escrow Bot for @zenoxescrowservice</b>",
        f"{CHAT} <b>Provided by @zenoxescrowservice</b>",
        "",
        f"{SIGN} <b>This is Your Personal Dashboard:</b>",
        DIVIDER,
        f"Select the option below {BOLT}",
    ]
    return "\n".join(lines)

def icon_btn(text, data, emoji_id=None):
    # Button ke aage premium emoji (bot owner ke paas Telegram Premium chahiye)
    if not emoji_id:
        return InlineKeyboardButton(text, callback_data=data)
    try:
        return InlineKeyboardButton(
            text, callback_data=data, icon_custom_emoji_id=emoji_id
        )
    except TypeError:
        # purana python-telegram-bot: seedha api_kwargs se bhejo
        return InlineKeyboardButton(
            text, callback_data=data,
            api_kwargs={"icon_custom_emoji_id": emoji_id},
        )

def dashboard_buttons():
    rows = [
        [icon_btn("Total Deal", "total_deal", "5913702317667913862")],
        [icon_btn("My Deal Info", "my_deal", "5956561916573782596")],
        [icon_btn("My Status", "my_status", "5796440171364749940")],
        [icon_btn("My Hold", "my_hold", "5879895758202735862")],
        [icon_btn("Global Ranking", "ranking", "5994378914636500516")],
    ]
    return InlineKeyboardMarkup(rows)

def back_button():
    row = [InlineKeyboardButton("🔙 Back", callback_data="back")]
    return InlineKeyboardMarkup([row])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    if update.effective_chat.type != "private":
        await msg.reply_text(
            "Dashboard ke liye bot ke private chat mein /start karo."
        )
        return
    name = update.effective_user.first_name or "User"
    await msg.reply_text(
        dashboard_text(name),
        parse_mode=ParseMode.HTML,
        reply_markup=dashboard_buttons(),
    )

# --- GROUP WELCOME (naya member aaye to) ---

def group_welcome_text(users):
    names = ", ".join(user_tag(u) for u in users)
    lines = [
        f"{DONE} <b>JOIN REQUEST ACCEPT</b>",
        "",
        f"👋 <b>WELCOME</b> {names}",
        "",
        f"{SHIELD} <b>TRUSTED DEAL ESCROW SERVICE</b>",
    ]
    return "\n".join(lines)

async def welcome_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update):
        return
    users = [u for u in (msg.new_chat_members or []) if not u.is_bot]
    if not users:
        return
    try:
        await context.bot.send_message(
            msg.chat_id,
            group_welcome_text(users),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logging.warning("welcome failed: %s", e)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    text = f"{CARD} <b>Your ID:</b> <code>{update.effective_user.id}</code>"
    await reply_html(update.message, text)

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    text = f"{CARD} <b>Chat ID:</b> <code>{update.effective_chat.id}</code>"
    await reply_html(update.message, text)

# --- DASHBOARD BUTTONS (Total Deal / My Deal Info / Ranking / Back) ---

def total_deal_text():
    done = db_one(
        f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deals "
        f"WHERE {G_SQL} AND status='completed'",
        G_ARGS,
    )
    pending = db_one(
        f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deals "
        f"WHERE {G_SQL} AND status='pending'",
        G_ARGS,
    )
    lines = [
        f"{CHART} <b>TOTAL DEALS</b>",
        DIVIDER,
        f"{DONE} <b>Completed:</b> {done[0]}",
        f"{GEM} <b>Completed Volume:</b> {inr(done[1])}",
        f"{PIN} <b>Pending:</b> {pending[0]}",
        f"{BAG} <b>Pending Volume:</b> {inr(pending[1])}",
    ]
    return "\n".join(lines)

def my_deal_text(user):
    uname = (user.username or "").lower()
    where = f"{G_SQL} AND (form_user_id=? OR (?<>'' AND (buyer=? OR seller=?)))"
    params = G_ARGS + (user.id, uname, uname, uname)
    done = db_one(
        f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deals "
        f"WHERE {where} AND status='completed'",
        params,
    )
    pending = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {where} AND status='pending'",
        params,
    )
    recent = db_all(
        f"SELECT trade_id, amount, status FROM deals "
        f"WHERE {where} ORDER BY id DESC LIMIT 5",
        params,
    )
    lines = [
        f"{FILE} <b>MY DEAL INFO</b>",
        DIVIDER,
        f"{DONE} <b>Completed:</b> {done[0]}",
        f"{GEM} <b>Total Volume:</b> {inr(done[1])}",
        f"{PIN} <b>Pending:</b> {pending[0]}",
    ]
    if recent:
        lines.append("")
        lines.append(f"{TREND} <b>Recent:</b>")
        for trade_id, amount, status in recent:
            mark = DONE if status == "completed" else TIME_E
            lines.append(f"{mark} <code>{trade_id}</code> - {inr(amount)}")
    else:
        lines.append("")
        lines.append("Abhi tak koi deal nahi mila.")
    return "\n".join(lines)

def my_status_text(user):
    rows = db_all(
        f"SELECT amount FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='completed'",
        G_ARGS + (user.id,),
    )
    count = len(rows)
    total = sum(r[0] for r in rows)
    fees = sum(calc_fee(r[0])[1] for r in rows)
    holds = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='pending' AND hold_at IS NOT NULL",
        G_ARGS + (user.id,),
    )[0]
    pending = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='pending'",
        G_ARGS + (user.id,),
    )[0]
    recent = db_all(
        f"SELECT trade_id, amount, closed_at FROM deals WHERE {G_SQL} "
        f"AND escrower_id=? AND status='completed' ORDER BY id DESC LIMIT 5",
        G_ARGS + (user.id,),
    )
    lines = [
        f"{PIN} <b>MY STATUS</b>",
        DIVIDER,
        f"{DONE} <b>Deals Karai:</b> {count}",
        f"{GEM} <b>Total Amount:</b> {inr(total)}",
        f"{BAG} <b>Total Fee:</b> {inr(fees)}",
        f"{PIN} <b>Pending:</b> {pending}",
        f"{LOCK} <b>Hold:</b> {holds}",
    ]
    if recent:
        lines += ["", f"{TREND} <b>Recent:</b>"]
        for trade_id, amount, closed_at in recent:
            lines.append(
                f"{DONE} <code>{trade_id}</code> - {inr(amount)} ({closed_at or '-'})"
            )
    return "\n".join(lines)

def my_hold_text(user):
    rows = db_all(
        f"SELECT trade_id, amount, hold_at, buyer_show, seller_show FROM deals "
        f"WHERE {G_SQL} AND status='pending' AND hold_at IS NOT NULL "
        f"AND (escrower_id=? OR form_user_id=?) ORDER BY id DESC LIMIT 10",
        G_ARGS + (user.id, user.id),
    )
    lines = [f"{LOCK} <b>MY HOLD</b>", DIVIDER]
    if not rows:
        lines.append("Abhi koi deal hold par nahi hai.")
        return "\n".join(lines)
    for trade_id, amount, hold_at, b, s in rows:
        lines.append(
            f"⏸ <code>{trade_id}</code> - {inr(amount)}\n"
            f"   {esc(shown(b, ''))} ➜ {esc(shown(s, ''))}\n"
            f"   {TIME_E} {hold_at}"
        )
    return "\n".join(lines)

def ranking_text():
    rows = db_all(
        f"SELECT form_user_id, MAX(form_user_name), COUNT(*), SUM(amount) "
        f"FROM deals WHERE {G_SQL} AND status='completed' "
        f"AND form_user_id IS NOT NULL "
        f"GROUP BY form_user_id ORDER BY SUM(amount) DESC LIMIT 10",
        G_ARGS,
    )
    lines = [f"{TREND} <b>GLOBAL RANKING</b>", DIVIDER]
    if not rows:
        lines.append("Abhi koi completed deal nahi hai.")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, name, count, volume) in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"{mark} {mention(uid, name)} - {inr(volume)} ({count} deals)"
        )
    return "\n".join(lines)

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    data = query.data
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    if data == "total_deal":
        text, markup = total_deal_text(), back_button()
    elif data == "my_deal":
        text, markup = my_deal_text(user), back_button()
    elif data == "my_status":
        text, markup = my_status_text(user), back_button()
    elif data == "my_hold":
        text, markup = my_hold_text(user), back_button()
    elif data == "ranking":
        text, markup = ranking_text(), back_button()
    elif data == "back":
        text = dashboard_text(user.first_name or "User")
        markup = dashboard_buttons()
    else:
        return

    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception as e:
        # "message is not modified" jaisi galti ignore
        logging.info("edit skipped: %s", e)

# --- DEALS: /add aur /close (payment photo ke saath ya bina photo ke) ---

# --- /hold ---
# Reply karke /hold  -> deal hold par (dobara /hold -> hold se hat jaye)
# Bina reply ke /hold -> hold list aur count

def hold_list_text():
    rows = db_all(
        f"SELECT trade_id, amount, hold_at, buyer_show, seller_show, "
        f"escrower_name FROM deals WHERE {G_SQL} AND status='pending' "
        f"AND hold_at IS NOT NULL ORDER BY id DESC LIMIT 30",
        G_ARGS,
    )
    lines = [f"{LOCK} <b>HOLD LIST</b>", DIVIDER]
    if not rows:
        lines.append("Abhi koi deal hold par nahi hai.")
        return "\n".join(lines)
    total = sum(r[1] for r in rows)
    lines.append(f"{PIN} <b>Total Hold:</b> {len(rows)}")
    lines.append(f"{BAG} <b>Hold Amount:</b> {inr(total)}")
    lines.append("")
    for trade_id, amount, hold_at, b, s_, ename in rows:
        lines.append(
            f"⏸ <code>{trade_id}</code> - {inr(amount)}\n"
            f"   {esc(shown(b, ''))} ➜ {esc(shown(s_, ''))}\n"
            f"   {TIME_E} {hold_at} | {SHIELD} {esc(ename or '-')}"
        )
    return "\n".join(lines)

async def hold_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update):
        return
    if not is_admin(update):
        await reply_html(msg, f"{WARN} Aap is command ke liye allowed nahi ho.")
        return

    replied = msg.reply_to_message
    if replied is None:
        await reply_html(msg, hold_list_text())
        return

    chat_id = update.effective_chat.id
    cols = (
        "SELECT trade_id, amount, hold_at, buyer_show, seller_show, "
        "form_msg_id FROM deals WHERE group_id=? AND status='pending' AND "
    )
    row = db_one(cols + "form_msg_id=?", (chat_id, replied.message_id))
    if row is None:
        body = (replied.text or replied.caption or "").upper()
        found = re.search(r"DL-[A-Z0-9-]+", body)
        if found:
            row = db_one(cols + "trade_id=?", (chat_id, found.group(0)))
    if not row:
        await reply_html(
            msg,
            f"{WARN} Is message ki pending deal nahi mili. "
            "Deal Added (/add) message par reply karke /hold likho.",
        )
        return

    trade_id, amount, hold_at, buyer_show, seller_show, form_msg_id = row
    admin = update.effective_user

    if hold_at:
        # dobara /hold -> hold se hatao
        db_exec("UPDATE deals SET hold_at=NULL WHERE trade_id=?", (trade_id,))
        title = f"{UNLOCK} <b>DEAL HOLD SE HATA DI</b>"
        by = "Removed By"
    else:
        db_exec(
            "UPDATE deals SET hold_at=? WHERE trade_id=?",
            (now_text(), trade_id),
        )
        title = f"{LOCK} <b>DEAL ON HOLD</b>"
        by = "Held By"

    lines = [
        title,
        DIVIDER,
        f"{TAGI} <b>Trade ID:</b> <code>{trade_id}</code>",
        f"{BAG} <b>Amount:</b> {inr(amount)}",
        f"{BUYER} <b>Buyer:</b> {esc(shown(buyer_show, ''))}",
        f"{SELLER} <b>Seller:</b> {esc(shown(seller_show, ''))}",
        f"{TIME_E} <b>Time:</b> {now_text()}",
        "",
        f"{SHIELD} <b>{by}:</b> {user_tag(admin)}",
    ]
    await msg.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_parameters=ReplyParameters(
            message_id=replied.message_id, allow_sending_without_reply=True
        ),
    )

# --- /add ---

async def add_deal(update, context, args, photo_id=None):
    msg = update.message
    if msg is None or not in_group(update):
        return
    if not is_admin(update):
        await reply_html(msg, f"{WARN} Aap is command ke liye allowed nahi ho.")
        return

    if PAYMENT_PHOTO_REQUIRED and photo_id is None:
        await reply_html(
            msg,
            f"{WARN} Payment ka <b>photo</b> bhejo: form par reply karke "
            "photo ke caption mein <code>/add 400</code> likho.",
        )
        return

    form = msg.reply_to_message
    if form is None:
        await reply_html(
            msg,
            f"{WARN} Form par <b>reply</b> karke likho: <code>/add 400</code>",
        )
        return

    form_user = form.from_user
    if form_user is None or form_user.is_bot:
        await reply_html(msg, f"{WARN} Ye form kisi member ka hona chahiye.")
        return

    form_text = form.text or form.caption or ""
    fields = parse_fields(form_text)

    if args:
        amount = parse_amount(args[0])
    else:
        amount = find_amount(fields.get("DEAL AMOUNT", ""))
    if amount is None:
        await reply_html(
            msg, f"{WARN} Amount sahi likho: <code>/add 400</code>"
        )
        return

    buyer_raw = args[1] if len(args) > 1 else fields.get("BUYER")
    seller_raw = args[2] if len(args) > 2 else fields.get("SELLER")
    buyer = (
        args[1].lstrip("@").lower() if len(args) > 1
        else username_in(fields.get("BUYER")) or "-"
    )
    seller = (
        args[2].lstrip("@").lower() if len(args) > 2
        else username_in(fields.get("SELLER")) or "-"
    )
    buyer_show = party_show(buyer_raw)
    seller_show = party_show(seller_raw)

    chat_id = update.effective_chat.id
    exists = db_one(
        "SELECT trade_id FROM deals "
        "WHERE group_id=? AND form_msg_id=? AND status='pending'",
        (chat_id, form.message_id),
    )
    if exists:
        await reply_html(
            msg,
            f"{WARN} Is form ka deal pehle se add hai: "
            f"<code>{exists[0]}</code>",
        )
        return

    admin = update.effective_user
    trade_id = new_trade_id()
    db_exec(
        "INSERT INTO deals (trade_id, group_id, amount, buyer, seller, "
        "escrower_id, escrower_name, status, created_at, "
        "form_msg_id, form_user_id, form_user_name, "
        "buyer_show, seller_show) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id, chat_id, amount, buyer, seller,
            admin.id, admin.first_name or "Admin", "pending", now_text(),
            form.message_id, form_user.id, form_user.first_name or "User",
            buyer_show, seller_show,
        ),
    )

    db_exec(
        "UPDATE deals SET escrower_tag=? WHERE trade_id=?",
        (user_tag(admin), trade_id),
    )

    rate_text, fee = calc_fee(amount)
    lines = [
        f"{BAG} <b>Deal Amount:</b> {inr(amount)}",
        f"{FEE} <b>Fee:</b> {inr(0)} (Free)",
        f"{GEM} <b>Net Release:</b> {inr(amount - fee)}",
        f"{TAGI} <b>Trade ID:</b> <code>{trade_id}</code>",
        "",
        f"{BUYER} <b>Buyer:</b> {esc(buyer_show)}",
        f"{SELLER} <b>Seller:</b> {esc(seller_show)}",
        f"{DETAIL_E} <b>Detail:</b> {esc(plain(fields.get('DEAL DETAIL')))}",
        f"{TIME_E} <b>Expected Time:</b> "
        f"{esc(plain(fields.get('EXPECTED TIME')))}",
        f"{PIN} <b>T/C:</b> {esc(tc_show(fields.get('T/C')))}",
        "",
        f"{SHIELD} <b>Escrowed By:</b> {user_tag(admin)}",
    ]
    if photo_id:
        db_exec(
            "UPDATE deals SET add_photo=? WHERE trade_id=?",
            (photo_id, trade_id),
        )
    await send_result(
        update, context, chat_id, form.message_id, "\n".join(lines), photo_id
    )

# --- /close ---

async def send_result(update, context, chat_id, reply_id, text, photo_id=None):
    params = {"parse_mode": ParseMode.HTML}
    if reply_id:
        params["reply_parameters"] = ReplyParameters(
            message_id=reply_id, allow_sending_without_reply=True
        )
    sent = None
    try:
        if photo_id:
            visible = len(re.sub(r"<[^>]+>", "", text))
            if visible <= 1000:
                sent = await context.bot.send_photo(
                    chat_id, photo_id, caption=text, **params
                )
            else:
                # caption bahut lamba: photo alag, text alag
                await context.bot.send_photo(
                    chat_id, photo_id,
                    **({"reply_parameters": params["reply_parameters"]}
                       if reply_id else {}),
                )
                sent = await context.bot.send_message(chat_id, text, **params)
        else:
            sent = await context.bot.send_message(chat_id, text, **params)
    except Exception as e:
        logging.warning("result send failed: %s", e)
        return await reply_html(update.message, text)

    if photo_id and DELETE_ADMIN_PHOTO:
        try:
            await update.message.delete()
        except Exception as e:
            logging.info("admin photo delete skipped: %s", e)
    return sent

async def close_deal(update, context, args, photo_id=None):
    msg = update.message
    if msg is None or not in_group(update):
        return
    if not is_admin(update):
        await reply_html(msg, f"{WARN} Aap is command ke liye allowed nahi ho.")
        return

    if PAYMENT_PHOTO_REQUIRED and photo_id is None:
        await reply_html(
            msg,
            f"{WARN} Payment ka <b>photo</b> bhejo: form par reply karke "
            "photo ke caption mein <code>/close 360</code> likho.",
        )
        return

    replied = msg.reply_to_message
    chat_id = update.effective_chat.id
    cols = (
        "SELECT id, trade_id, amount, buyer, seller, escrower_name, "
        "form_user_id, form_user_name, form_msg_id, "
        "buyer_show, seller_show FROM deals "
        "WHERE group_id=? AND status='pending' AND "
    )

    row = None
    amount_arg = None
    if replied is not None:
        amount_arg = args[0] if args else None
        row = db_one(cols + "form_msg_id=?", (chat_id, replied.message_id))
        if row is None:
            body = (replied.text or replied.caption or "").upper()
            found = re.search(r"DL-[A-Z0-9-]+", body)
            if found:
                row = db_one(
                    cols + "trade_id=?", (chat_id, found.group(0))
                )
    elif args:
        row = db_one(cols + "trade_id=?", (chat_id, args[0].upper()))
        amount_arg = args[1] if len(args) > 1 else None
    else:
        await reply_html(
            msg,
            f"{WARN} Form ya Deal Added message par <b>reply</b> karke "
            "likho: <code>/close 360</code>",
        )
        return

    if row is None:
        await reply_html(
            msg,
            f"{WARN} Is form ka koi pending deal nahi mila. "
            "Pehle /add karo (ya deal pehle hi close ho chuki hai).",
        )
        return

    (deal_id, trade_id, amount, buyer, seller, escrower, fuid, fname,
     form_msg_id, buyer_show, seller_show) = row

    if amount_arg is None:
        released = amount - calc_fee(amount)[1]
    else:
        released = parse_amount(amount_arg)
        if released is None:
            await reply_html(
                msg, f"{WARN} Release amount sahi number mein likho."
            )
            return

    if released > amount + 0.0001:
        await reply_html(
            msg,
            f"{WARN} Release amount deal amount ({inr(amount)}) "
            "se zyada nahi ho sakta.",
        )
        return

    db_exec(
        "UPDATE deals SET status='completed', closed_at=?, released=? "
        "WHERE id=?",
        (now_text(), released, deal_id),
    )

    closer = update.effective_user
    if closer.username:
        closer_plain = "@" + closer.username
        closer_top = esc(closer_plain)
    else:
        closer_plain = closer.first_name or "Escrower"
        closer_top = mention(closer.id, closer_plain)

    parties = [
        p for p in (shown(buyer_show, buyer), shown(seller_show, seller))
        if p != "-"
    ]
    names = " and ".join(esc(p) for p in parties) or "Buyer and Seller"

    lines = [
        f"{DONE} <b>Deal Completed</b>",
        f"{CLOSE_ID_E} <b>Trade ID:</b> {esc(trade_id)}",
        f"{GEM} <b>Released:</b> {inr(released)}",
        f"{CLOSE_ESC_E} <b>Escrowed By:</b> {closer_top}",
        "",
        f"~ {names} are requested to drop the vouch before leaving👇🏻",
        "",
        f"<code>🤝 I Vouch {esc(closer_plain)} for {inr(released)} "
        "smooth escrow deal</code>",
    ]
    if photo_id:
        db_exec(
            "UPDATE deals SET close_photo=? WHERE id=?", (photo_id, deal_id)
        )
    await send_result(
        update, context, chat_id, form_msg_id, "\n".join(lines), photo_id
    )

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_deal(update, context, context.args or [])

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await close_deal(update, context, context.args or [])

async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not msg.photo:
        return
    found = re.match(
        r"^/(add|close)(?:@\w+)?(?:\s+(.*))?$",
        (msg.caption or "").strip(),
        re.S | re.I,
    )
    if not found:
        return
    args = (found.group(2) or "").split()
    photo_id = msg.photo[-1].file_id
    if found.group(1).lower() == "add":
        await add_deal(update, context, args, photo_id)
    else:
        await close_deal(update, context, args, photo_id)

# --- FORM SYSTEM ---

async def send_blank_form(message, user):
    who = mention(user.id, user.first_name)
    text = (
        f"{FILE} {who} <b>Ye form copy karke poora bharo</b> 👇\n\n"
        f"<pre>{esc(BLANK_FORM)}</pre>"
    )
    await reply_html(message, text)

async def form_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not in_group(update):
        return
    await send_blank_form(update.message, update.effective_user)

async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update):
        return
    await reply_html(msg, fee_text())

async def ask_fee_amount(context, msg):
    # Fee zero hai, amount poochne ki zaroorat nahi
    await reply_html(msg, fee_text())

def is_spam(context, chat_id, user_id, text):
    store = context.bot_data.setdefault("spam", {})
    key = (chat_id, user_id)
    now = tm.time()
    norm = " ".join(text.lower().split())
    history = [
        (t, m) for (t, m) in store.get(key, []) if now - t <= SPAM_SECONDS
    ]
    history.append((now, norm))
    store[key] = history
    same = sum(1 for (_, m) in history if m == norm)
    if same >= SPAM_COUNT:
        store[key] = []
        return True
    # memory saaf rakho
    if len(store) > 5000:
        for k in list(store.keys()):
            if not store[k] or now - store[k][-1][0] > SPAM_SECONDS:
                store.pop(k, None)
    return False

# --- RELEASE / REFUND AGREE MESSAGE ---

TRADE_ID_RE = re.compile(r"DL-[A-Za-z0-9]+-\d+")
DEAL_COLS = (
    "trade_id, buyer, seller, buyer_show, seller_show, "
    "escrower_id, escrower_name, escrower_tag"
)

def find_deal_from_reply(chat_id, reply):
    if reply is None:
        return None
    found = TRADE_ID_RE.search(reply.text or reply.caption or "")
    if found:
        row = db_one(
            f"SELECT {DEAL_COLS} FROM deals WHERE trade_id=?",
            (found.group(0),),
        )
        if row:
            return row
    # form message par reply kiya ho to
    return db_one(
        f"SELECT {DEAL_COLS} FROM deals WHERE group_id=? AND form_msg_id=? "
        "ORDER BY id DESC",
        (chat_id, reply.message_id),
    )

def agree_text(deal, kind):
    trade_id, buyer, seller, buyer_show, seller_show, esc_id, esc_name, esc_tag = deal
    tag = esc_tag or mention(esc_id, esc_name)
    lines = [
        f"{TAGI} <b>Trade ID:</b> <code>{esc(trade_id)}</code>",
        f"{BUYER} <b>Buyer:</b> {esc(shown(buyer_show, buyer))}",
        f"{SELLER} <b>Seller:</b> {esc(shown(seller_show, seller))}",
        f"{SHIELD} <b>Escrower:</b> {tag}",
        "",
        f"<b>AGREE FOR {kind.upper()} ??</b>",
    ]
    return "\n".join(lines)

async def form_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or msg.from_user is None or msg.from_user.is_bot:
        return
    if not in_group(update):
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    user = msg.from_user
    chat_id = msg.chat_id

    # 0) Spam check (admin ko chhod kar)
    if user.id not in ADMIN_IDS and is_spam(context, chat_id, user.id, text):
        if not await is_group_admin(context, chat_id, user.id):
            await reply_html(
                msg,
                f"{WARN} {mention(user.id, user.first_name)} "
                "spam mat karo. Ek hi message baar baar mat bhejo.",
            )
            return

    lowered = text.lower()

    # 1) "form" likha -> khali form do
    if lowered == "form":
        await send_blank_form(msg, user)
        return

    # 1b) /add wale message par reply karke "release" / "refund" likha
    match = re.fullmatch(r"(release|refund)\W*", lowered)
    if match and msg.reply_to_message is not None:
        deal = find_deal_from_reply(chat_id, msg.reply_to_message)
        if deal:
            await reply_html(msg, agree_text(deal, match.group(1)))
            return

    # 2) "fee" / "fees" / "fees 500" / "500 fees" -> free fee message
    if re.fullmatch(r"fees?\b.*", lowered, re.S):
        await reply_html(msg, fee_text())
        return
    match = re.fullmatch(r"(.+?)\s+fees?", lowered)
    if match and parse_amount(match.group(1)) is not None:
        await reply_html(msg, fee_text())
        return

    # 4) Bhara hua form check karo
    fields = parse_form(text)
    if fields is None:
        return

    missing = [
        name for name in FORM_REQUIRED
        if name not in fields or is_empty(fields[name])
    ]
    if "DEAL AMOUNT" not in missing:
        if find_amount(fields.get("DEAL AMOUNT", "")) is None:
            missing.append("DEAL AMOUNT")

    who = mention(user.id, user.first_name)
    if missing:
        lines = [f"{WARN} {who} form adhura hai. Ye bharo:"]
        for name in missing:
            lines.append(f"• {esc(name)}")
        lines.append("")
        lines.append("Naya form ke liye <b>form</b> likho.")
        await reply_html(msg, "\n".join(lines))
        return

    # "Maal aa gaya" reply band hai

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if req is not None and req.chat.id in GROUP_IDS:
        try:
            await req.approve()
        except Exception as e:
            logging.warning("approve failed: %s", e)

# --- AUTO LOCK / UNLOCK (har minute schedule check karta hai) ---

def should_be_locked(now=None):
    if now is None:
        now = datetime.now(timezone.utc).time()
    lock = LOCK_TIME.replace(tzinfo=None)
    unlock = UNLOCK_TIME.replace(tzinfo=None)
    if lock > unlock:
        return now >= lock or now < unlock
    return lock <= now < unlock

async def notify_admins(context, text):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id, text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning("admin notify failed: %s", e)

async def apply_lock(context, gid, locked):
    perms = LOCKED if locked else UNLOCKED
    word = "lock" if locked else "unlock"
    try:
        await context.bot.set_chat_permissions(gid, perms)
    except Exception as e:
        logging.error("%s failed for %s: %s", word, gid, e)
        last = context.bot_data.get("lock_fail_at", 0)
        if tm.time() - last > 3600:
            context.bot_data["lock_fail_at"] = tm.time()
            await notify_admins(
                context,
                f"{WARN} Group <code>{gid}</code> {word} nahi ho paya:\n"
                f"<code>{esc(e)}</code>\n\n"
                "Bot ko group mein admin banao aur "
                "<b>Restrict members</b> (Ban users) permission do.",
            )
        return False

    if locked:
        text = f"{LOCK} <b>Group locked.</b> Subah 6:00 AM IST par khulega."
    else:
        text = f"{UNLOCK} <b>Group unlocked.</b> Ab messages bhej sakte ho."
    try:
        await context.bot.send_message(gid, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning("lock notice failed for %s: %s", gid, e)
    return True

async def schedule_check(context: ContextTypes.DEFAULT_TYPE):
    want = should_be_locked()
    states = context.bot_data.setdefault("lock_states", {})
    for gid in GROUP_IDS:
        last = states.get(gid)
        if last == want:
            continue
        if last is None:
            # Bot abhi start hua: group schedule se alag ho tabhi badlo
            try:
                chat = await context.bot.get_chat(gid)
            except Exception as e:
                logging.warning("get_chat failed for %s: %s", gid, e)
                continue
            can = chat.permissions.can_send_messages if chat.permissions else None
            if can is None or (not can) == want:
                states[gid] = want
                continue
        if await apply_lock(context, gid, want):
            states[gid] = want

# --- MANUAL /lock aur /unlock (sirf ADMIN_IDS, sirf group) ---

async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not in_group(update) or not is_admin(update):
        return
    for gid in GROUP_IDS:
        await apply_lock(context, gid, True)

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not in_group(update) or not is_admin(update):
        return
    for gid in GROUP_IDS:
        await apply_lock(context, gid, False)

async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Error: %s", context.error, exc_info=context.error)

def check_token():
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        raise SystemExit("Token galat hai: bot.py mein BOT_TOKEN wali line dekho.")

def main():
    check_token()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("hold", hold_deal))
    app.add_handler(CommandHandler("form", form_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new)
    )
    app.add_handler(CallbackQueryHandler(buttons))
    # app.add_handler(ChatJoinRequestHandler(join_request))
    # photo + caption mein /add ya /close (text wale handler se pehle)
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CaptionRegex(r"(?i)^/(add|close)\b"),
            photo_cmd,
        )
    )
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            form_handler,
        )
    )
    app.add_error_handler(on_error)

    if app.job_queue is None:
        logging.warning(
            "JobQueue nahi mila: auto lock/unlock band hai. "
            "requirements.txt mein python-telegram-bot[job-queue] likho."
        )
    else:
        app.job_queue.run_repeating(
            schedule_check, interval=60, first=10, name="schedule"
        )

    logging.info("Bot start ho gaya.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
