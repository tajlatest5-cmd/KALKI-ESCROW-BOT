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
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
# BOT TOKEN: Railway Variables mein BOT_TOKEN daalo (code mein mat likho)
# ======================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "xxxx").strip() or "xxxx"
if BOT_TOKEN in ("", "xxxx"):
    raise SystemExit("BOT_TOKEN set nahi hai. Railway Variables mein daalo.")

# --- SETTINGS ---
GROUP_IDS = [-1004199368031]            # kam se kam ek ID zaroori
ADMIN_IDS = [7054168366, 7346725373]    # sirf ye /add /close /hold chala sakte hain

# Railway Volume lagao aur DB_PATH=/data/bot.db set karo, warna redeploy pe data jayega
DB_PATH = os.environ.get("DB_PATH", "bot.db")

SPAM_COUNT = 5
SPAM_SECONDS = 60

FORM_OK_TEXT = "Maal aa gaya"

PAYMENT_PHOTO_REQUIRED = False   # True: bina photo ke /add /close nahi chalenge
DELETE_ADMIN_PHOTO = False       # True: result ke baad admin ka photo message delete

LOCK_TIME = time(18, 30, tzinfo=timezone.utc)    # 12:00 AM IST
UNLOCK_TIME = time(0, 30, tzinfo=timezone.utc)   # 6:00 AM IST
IST = timezone(timedelta(hours=5, minutes=30))

HELP_CONTACT = "@RANCHO_HELP"
SCAM_COOLDOWN = 60  # ek chat mein itne second mein sirf ek scam alert

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
    return f"₹{(x or 0):,.2f}"

def money(x):
    return f"₹{(x or 0):.2f}"

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

async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logging.warning("edit failed: %s", e)

# --- DATABASE ---
G_SQL = "group_id IN (" + ",".join("?" for _ in GROUP_IDS) + ")"
G_ARGS = tuple(GROUP_IDS)

def _run(sql, params, mode):
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        con.execute("PRAGMA journal_mode=WAL")
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
    db_exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
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
        ("detail", "TEXT"),
        ("expected", "TEXT"),
        ("tc", "TEXT"),
    )
    for col, typ in extra:
        try:
            db_exec(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

TRADE_START = 33  # Trade ID is number se neeche kabhi nahi jayega

def new_trade_id():
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
    return f"DL-JERRY-{n}"

# --- PARSING ---
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
TRADE_RE = re.compile(r"DL-JERRY-\d+", re.IGNORECASE)

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
FORM_REQUIRED = ["SELLER", "BUYER", "DEAL DETAIL", "DEAL AMOUNT", "EXPECTED TIME"]

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
        f"PLEASE CONTACT FAST\n{HELP_CONTACT}",
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

# --- FEES (ab sab free) ---

def calc_fee(amount):
    return "0%", 0.0

HELP_TEXT = f"{WARN} <b>PLEASE CONTACT FAST</b>\n\n{HELP_CONTACT}"

def fees_reply_text():
    return "\n".join([
        f"{DONE} <b>100 DEAL THK KOI BHI FEE NHI HAI ENJOY KARO</b>",
        "",
        f"{SHIELD} <b>TRUSTED AND VERIFY ESCROWER SUPPORT</b>",
        "",
        HELP_TEXT,
    ])

# --- DISPLAY HELPERS ---
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

def dashboard_text(name):
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
    if not emoji_id:
        return InlineKeyboardButton(text, callback_data=data)
    try:
        return InlineKeyboardButton(
            text, callback_data=data, icon_custom_emoji_id=emoji_id
        )
    except TypeError:
        return InlineKeyboardButton(
            text, callback_data=data,
            api_kwargs={"icon_custom_emoji_id": emoji_id},
        )

def dashboard_buttons(icons=True):
    def b(text, data, eid):
        return icon_btn(text, data, eid if icons else None)
    rows = [
        [b("Total Deal", "total_deal", "5913702317667913862")],
        [b("My Deal Info", "my_deal", "5956561916573782596")],
        [b("My Status", "my_status", "5796440171364749940")],
        [b("My Hold", "my_hold", "5879895758202735862")],
        [b("Global Ranking", "ranking", "5994378914636500516")],
    ]
    return InlineKeyboardMarkup(rows)

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None:
        return
    if update.effective_chat.type != "private":
        await msg.reply_text("Dashboard ke liye bot ke private chat mein /start karo.")
        return
    name = update.effective_user.first_name or "User"
    try:
        await msg.reply_text(
            dashboard_text(name),
            parse_mode=ParseMode.HTML,
            reply_markup=dashboard_buttons(True),
        )
    except BadRequest as e:
        logging.warning("icon buttons rejected, plain buttons bhej raha hoon: %s", e)
        await msg.reply_text(
            dashboard_text(name),
            parse_mode=ParseMode.HTML,
            reply_markup=dashboard_buttons(False),
        )

# --- GROUP WELCOME ---

def group_welcome_text(users):
    names = ", ".join(mention(u.id, u.first_name) for u in users)
    lines = [
        f"{STAR} <b>Welcome</b> {names}!",
        DIVIDER,
        f"{SHIELD} <b>Escrow Service: @zenoxescrowservice</b>",
        "",
        f"{FILE} Deal ka form chahiye? <b>form</b> likho.",
        f"{BAG} Fee ke liye <b>fees</b> likho.",
        f"{WARN} Deal sirf escrower ke through hi karo.",
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
            msg.chat_id, group_welcome_text(users), parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.warning("welcome failed: %s", e)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    await reply_html(
        update.message, f"{CARD} <b>Your ID:</b> <code>{update.effective_user.id}</code>"
    )

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    await reply_html(
        update.message, f"{CARD} <b>Chat ID:</b> <code>{update.effective_chat.id}</code>"
    )

# --- DASHBOARD TEXTS ---

def total_deal_text():
    done = db_one(
        f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deals "
        f"WHERE {G_SQL} AND status='completed'", G_ARGS,
    )
    pending = db_one(
        f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM deals "
        f"WHERE {G_SQL} AND status='pending'", G_ARGS,
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
        f"WHERE {where} AND status='completed'", params,
    )
    pending = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {where} AND status='pending'", params
    )
    recent = db_all(
        f"SELECT trade_id, amount, status FROM deals "
        f"WHERE {where} ORDER BY id DESC LIMIT 5", params,
    )
    lines = [
        f"{FILE} <b>MY DEAL INFO</b>",
        DIVIDER,
        f"{DONE} <b>Completed:</b> {done[0]}",
        f"{GEM} <b>Total Volume:</b> {inr(done[1])}",
        f"{PIN} <b>Pending:</b> {pending[0]}",
    ]
    lines.append("")
    if recent:
        lines.append(f"{TREND} <b>Recent:</b>")
        for trade_id, amount, status in recent:
            mark = DONE if status == "completed" else TIME_E
            lines.append(f"{mark} <code>{esc(trade_id)}</code> - {inr(amount)}")
    else:
        lines.append("Abhi tak koi deal nahi mila.")
    return "\n".join(lines)

def my_status_text(user):
    rows = db_all(
        f"SELECT amount FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='completed'", G_ARGS + (user.id,),
    )
    count = len(rows)
    total = sum(r[0] or 0 for r in rows)
    fees = sum(calc_fee(r[0] or 0)[1] for r in rows)
    holds = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='pending' AND hold_at IS NOT NULL", G_ARGS + (user.id,),
    )[0]
    pending = db_one(
        f"SELECT COUNT(*) FROM deals WHERE {G_SQL} AND escrower_id=? "
        f"AND status='pending'", G_ARGS + (user.id,),
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
                f"{DONE} <code>{esc(trade_id)}</code> - {inr(amount)} ({esc(closed_at or '-')})"
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
            f"⏸ <code>{esc(trade_id)}</code> - {inr(amount)}\n"
            f"   {esc(shown(b, ''))} ➜ {esc(shown(s, ''))}\n"
            f"   {TIME_E} {esc(hold_at)}"
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
        lines.append(f"{mark} {mention(uid, name)} - {inr(volume)} ({count} deals)")
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
        text, markup = dashboard_text(user.first_name or "User"), dashboard_buttons()
    else:
        return
    await safe_edit(query, text, markup)

# --- GROUP TEXT: "form", "fees", bhara hua form ---
FORM_WORD = re.compile(r"/?form\W*", re.IGNORECASE)
FEES_WORD = re.compile(r"/?fees?\W*", re.IGNORECASE)

async def group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not msg.text or not in_group(update):
        return
    text = unicodedata.normalize("NFKC", msg.text).strip()

    if FORM_WORD.fullmatch(text):
        await msg.reply_text(BLANK_FORM)
        return
    if FEES_WORD.fullmatch(text):
        await reply_html(msg, fees_reply_text())
        return

    form = parse_form(text)
    if form is None:
        return
    missing = [k for k in FORM_REQUIRED if is_empty(form.get(k))]
    if missing:
        await reply_html(
            msg,
            f"{WARN} <b>Form adhura hai.</b> Ye bharo: "
            + ", ".join(esc(m) for m in missing),
        )
        return
    if find_amount(form.get("DEAL AMOUNT")) is None:
        await reply_html(msg, f"{WARN} <b>DEAL AMOUNT</b> mein sahi number likho.")
        return
    await msg.reply_text(FORM_OK_TEXT)

# --- /add /close /hold /unhold (sirf ADMIN_IDS) ---

def cmd_args(msg):
    return (msg.text or msg.caption or "").split()[1:]

def photo_id(msg):
    return msg.photo[-1].file_id if msg.photo else None

async def cleanup_photo(msg):
    if DELETE_ADMIN_PHOTO and msg.photo:
        try:
            await msg.delete()
        except Exception as e:
            logging.warning("photo delete failed: %s", e)

DEAL_COLS = ("id, trade_id, amount, status, buyer_show, seller_show, buyer, seller, "
             "escrower_tag, hold_at")

def find_deal(msg, gid):
    for arg in cmd_args(msg):
        if TRADE_RE.fullmatch(arg):
            return db_one(
                f"SELECT {DEAL_COLS} FROM deals WHERE trade_id=? AND group_id=?",
                (arg.upper(), gid),
            )
    src = msg.reply_to_message
    if src is not None:
        return db_one(
            f"SELECT {DEAL_COLS} FROM deals WHERE form_msg_id=? AND group_id=?",
            (src.message_id, gid),
        )
    return None

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update) or not is_admin(update):
        return
    gid = update.effective_chat.id
    photo = photo_id(msg)
    if PAYMENT_PHOTO_REQUIRED and not photo:
        await msg.reply_text("Payment ka photo lagakar /add likho (photo ke caption mein).")
        return
    src = msg.reply_to_message
    src_text = (src.text or src.caption) if src else None
    form = parse_form(src_text) if src_text else None
    if form is None:
        await msg.reply_text("Bhare hue form par reply karke /add likho.")
        return
    missing = [k for k in FORM_REQUIRED if is_empty(form.get(k))]
    if missing:
        await reply_html(
            msg, f"{WARN} Form adhura hai: " + ", ".join(esc(m) for m in missing)
        )
        return
    amount = find_amount(form.get("DEAL AMOUNT"))
    if amount is None:
        await msg.reply_text("DEAL AMOUNT sahi nahi hai.")
        return
    dup = db_one(
        "SELECT trade_id FROM deals WHERE group_id=? AND form_msg_id=?",
        (gid, src.message_id),
    )
    if dup:
        await reply_html(msg, f"{WARN} Ye deal pehle se add hai: <code>{esc(dup[0])}</code>")
        return

    admin = update.effective_user
    trade_id = new_trade_id()
    buyer_show = party_show(form.get("BUYER"))
    seller_show = party_show(form.get("SELLER"))
    form_user = src.from_user
    db_exec(
        "INSERT INTO deals (trade_id, group_id, amount, buyer, seller, escrower_id, "
        "escrower_name, status, created_at, form_msg_id, form_user_id, form_user_name, "
        "buyer_show, seller_show, add_photo, escrower_tag, detail, expected, tc) "
        "VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id, gid, amount,
            username_in(form.get("BUYER")) or "", username_in(form.get("SELLER")) or "",
            admin.id, admin.first_name, now_text(), src.message_id,
            form_user.id if form_user else None,
            form_user.first_name if form_user else None,
            buyer_show, seller_show, photo, "@" + admin.username if admin.username else admin.first_name,
            plain(form.get("DEAL DETAIL")), plain(form.get("EXPECTED TIME")),
            tc_show(form.get("T/C")),
        ),
    )
    lines = [
        f"{DONE} <b>DEAL ADDED</b>",
        DIVIDER,
        f"{TAGI} <b>Trade ID:</b> <code>{trade_id}</code>",
        f"{BUYER} <b>Buyer:</b> {esc(buyer_show)}",
        f"{SELLER} <b>Seller:</b> {esc(seller_show)}",
        f"{DETAIL_E} <b>Detail:</b> {esc(plain(form.get('DEAL DETAIL')))}",
        f"{BAG} <b>Amount:</b> {inr(amount)}",
        f"{FEE} <b>Fee:</b> {money(0)} (FREE)",
        f"{TIME_E} <b>Expected:</b> {esc(plain(form.get('EXPECTED TIME')))}",
        f"{FILE} <b>T/C:</b> {esc(tc_show(form.get('T/C')))}",
        f"{SHIELD} <b>Escrower:</b> {user_tag(admin)}",
        "",
        f"{WARN} Deal sirf escrower ke through hi karo.",
        f"{HELP_CONTACT}",
    ]
    await src.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    await cleanup_photo(msg)

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update) or not is_admin(update):
        return
    gid = update.effective_chat.id
    photo = photo_id(msg)
    if PAYMENT_PHOTO_REQUIRED and not photo:
        await msg.reply_text("Payment ka photo lagakar /close likho (photo ke caption mein).")
        return
    deal = find_deal(msg, gid)
    if deal is None:
        await msg.reply_text("Form par reply karo ya /close DL-JERRY-33 likho.")
        return
    _id, trade_id, amount, status, b_show, s_show, b, s, e_tag, hold_at = deal
    if status == "completed":
        await reply_html(msg, f"{WARN} <code>{esc(trade_id)}</code> pehle se close hai.")
        return
    db_exec(
        "UPDATE deals SET status='completed', closed_at=?, released=?, close_photo=?, "
        "hold_at=NULL WHERE id=?",
        (now_text(), amount, photo, _id),
    )
    lines = [
        f"{DONE} <b>DEAL CLOSED</b>",
        DIVIDER,
        f"{TAGI} <b>Trade ID:</b> <code>{esc(trade_id)}</code>",
        f"{BUYER} <b>Buyer:</b> {esc(shown(b_show, b))}",
        f"{SELLER} <b>Seller:</b> {esc(shown(s_show, s))}",
        f"{BAG} <b>Released:</b> {inr(amount)}",
        f"{FEE} <b>Fee:</b> {money(0)} (FREE)",
        f"{SHIELD} <b>Escrower:</b> {user_tag(update.effective_user)}",
        f"{TIME_E} {now_text()}",
        "",
        f"{HELP_CONTACT}",
    ]
    target = msg.reply_to_message or msg
    await target.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    await cleanup_photo(msg)

async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update) or not is_admin(update):
        return
    deal = find_deal(msg, update.effective_chat.id)
    if deal is None:
        await msg.reply_text("Form par reply karo ya /hold DL-JERRY-33 likho.")
        return
    if deal[3] != "pending":
        await msg.reply_text("Ye deal pending nahi hai.")
        return
    db_exec("UPDATE deals SET hold_at=? WHERE id=?", (now_text(), deal[0]))
    await reply_html(msg, f"{LOCK} <code>{esc(deal[1])}</code> <b>HOLD</b> par hai.")

async def unhold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not in_group(update) or not is_admin(update):
        return
    deal = find_deal(msg, update.effective_chat.id)
    if deal is None:
        await msg.reply_text("Form par reply karo ya /unhold DL-JERRY-33 likho.")
        return
    db_exec("UPDATE deals SET hold_at=NULL WHERE id=?", (deal[0],))
    await reply_html(msg, f"{UNLOCK} <code>{esc(deal[1])}</code> hold se hat gayi.")

# --- SPAM ---

async def spam_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not msg.text or msg.from_user is None or not in_group(update):
        return
    if is_admin(update):
        return
    now = tm.time()
    key = (msg.from_user.id, " ".join(msg.text.lower().split()))
    store = context.chat_data.setdefault("spam", {})
    stamps = [t for t in store.get(key, []) if now - t < SPAM_SECONDS]
    stamps.append(now)
    store[key] = stamps
    if len(store) > 500:
        for k in [k for k, v in store.items() if not v or now - v[-1] >= SPAM_SECONDS]:
            del store[k]
    if len(stamps) >= SPAM_COUNT:
        store[key] = []
        if await is_group_admin(context, msg.chat_id, msg.from_user.id):
            return
        await reply_html(
            msg,
            f"{WARN} <b>Spam Warning:</b> {mention(msg.from_user.id, msg.from_user.first_name)}, "
            f"ek hi message baar baar mat bhejo.",
        )

# --- SCAM ALERT ---
SCAM_RE = re.compile(
    r"\b(scam|scammed|scammer|scamm?ing|fraud|fraudster|thug|thag|thaga|thagi|"
    r"dhokha|dhoka|dhokebaz|cheat|cheated|cheater)\b|स्कैम|धोखा|धोखे|फ्रॉड|ठग",
    re.IGNORECASE,
)

async def scam_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or not msg.text or not in_group(update):
        return
    if is_admin(update):
        return
    if not SCAM_RE.search(unicodedata.normalize("NFKC", msg.text)):
        return
    last = context.chat_data.get("scam_last", 0)
    if tm.time() - last < SCAM_COOLDOWN:
        return
    context.chat_data["scam_last"] = tm.time()
    try:
        await msg.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning("scam alert failed: %s", e)

# --- LOCK / UNLOCK ---

async def set_lock(context, locked):
    for gid in GROUP_IDS:
        try:
            await context.bot.set_chat_permissions(gid, LOCKED if locked else UNLOCKED)
            text = (
                f"{LOCK} <b>Group locked.</b> Subah 6:00 AM par unlock hoga."
                if locked else
                f"{UNLOCK} <b>Group unlocked.</b> Good morning!"
            )
            await context.bot.send_message(gid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.warning("lock/unlock failed for %s: %s", gid, e)

async def lock_job(context: ContextTypes.DEFAULT_TYPE):
    await set_lock(context, True)

async def unlock_job(context: ContextTypes.DEFAULT_TYPE):
    await set_lock(context, False)

async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not in_group(update) or not is_admin(update):
        return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, LOCKED)
        await reply_html(update.message, f"{LOCK} <b>Group locked.</b>")
    except Exception as e:
        await update.message.reply_text(f"Lock fail: {e}")

async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not in_group(update) or not is_admin(update):
        return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, UNLOCKED)
        await reply_html(update.message, f"{UNLOCK} <b>Group unlocked.</b>")
    except Exception as e:
        await update.message.reply_text(f"Unlock fail: {e}")

# --- JOIN REQUEST ---

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if req is None or req.chat.id not in GROUP_IDS:
        return
    try:
        await req.approve()
    except Exception as e:
        logging.warning("join approve failed: %s", e)

# --- ERRORS + MAIN ---

async def on_error(update, context):
    logging.error("Error", exc_info=context.error)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))

    # /add /close: text ke saath bhi aur photo caption ke saath bhi
    for name, fn in (("add", add_cmd), ("close", close_cmd)):
        app.add_handler(CommandHandler(name, fn, filters=filters.ChatType.GROUPS))
        app.add_handler(MessageHandler(
            filters.PHOTO & filters.CaptionRegex(rf"^/{name}(@\w+)?(\s|$)")
            & filters.ChatType.GROUPS, fn))
    app.add_handler(CommandHandler("hold", hold_cmd, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unhold", unhold_cmd, filters=filters.ChatType.GROUPS))

    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new))

    text_filter = filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS
    app.add_handler(MessageHandler(text_filter, group_text), group=0)
    app.add_handler(MessageHandler(text_filter, spam_check), group=1)
    app.add_handler(MessageHandler(text_filter, scam_alert), group=5)

    app.add_error_handler(on_error)

    jq = app.job_queue
    if jq is None:
        logging.warning("JobQueue nahi hai: requirements mein python-telegram-bot[job-queue] rakho")
    else:
        jq.run_daily(lock_job, LOCK_TIME, name="lock")
        jq.run_daily(unlock_job, UNLOCK_TIME, name="unlock")

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
