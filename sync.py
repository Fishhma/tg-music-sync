"""
tg-music-sync: раз в месяц проверяет Telegram-каналы, присылает через бота
список новых релизов и по кнопке скачивает выбранное в папку Navidrome.

Два клиента в одном процессе:
  * user — отдельный аккаунт Telegram, подписанный на каналы: читает и качает;
  * bot  — ваш бот из @BotFather: пишет вам отчёт и принимает нажатия кнопок.

Запуск:
  python sync.py --login   # один раз, интерактивный вход в аккаунт-читалку
  python sync.py           # основной режим
"""
import asyncio
import datetime as dt
import html
import json
import logging
import math
import os
import re
import sys
from zoneinfo import ZoneInfo

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeAudio

try:
    import mutagen
except ImportError:
    mutagen = None

# ---------- настройки ----------

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "").split(",") if c.strip()]
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music/telegram")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SCAN_DAY = min(max(int(os.environ.get("SCAN_DAY", "1")), 1), 28)
SCAN_HOUR = min(max(int(os.environ.get("SCAN_HOUR", "10")), 0), 23)
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
FIX_TAGS = os.environ.get("FIX_TAGS", "true").lower() == "true"

STATE_FILE = os.path.join(DATA_DIR, "state.json")
PAGE = 8  # релизов на одной странице сообщения

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-music-sync")
logging.getLogger("telethon").setLevel(logging.WARNING)

# ---------- состояние ----------
# channels: {ref: {id, title, last_id}}  — докуда просмотрен канал
# pending:  {key: release}               — найденные, но ещё не разобранные релизы
# last_scan: "YYYY-MM"                   — месяц последнего планового скана


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("channels", {})
    st.setdefault("pending", {})
    st.setdefault("last_scan", "")
    return st


state = load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------- утилиты ----------

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(name, fallback="unknown"):
    name = BAD_CHARS.sub("_", name or "").strip(" .")
    return name[:150] or fallback


def channel_ref(ch):
    return int(ch) if re.fullmatch(r"-?\d+", ch) else ch


def esc(s):
    return html.escape(s or "")


def mb(size):
    return f"{size / 1048576:.0f} МБ"


def is_track(msg):
    f = msg.file
    if not f or msg.voice or msg.video_note:
        return False
    mime = (f.mime_type or "").lower()
    ext = (f.ext or "").lower()
    return mime.startswith("audio/") or ext in {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}


def audio_attr(msg):
    for a in (msg.document.attributes if msg.document else []):
        if isinstance(a, DocumentAttributeAudio):
            return a
    return None


def track_label(msg):
    a = audio_attr(msg)
    if a and (a.performer or a.title):
        return " — ".join(x for x in (a.performer, a.title) if x)
    return os.path.splitext(msg.file.name or f"track {msg.id}")[0]


def file_name(msg):
    if msg.file.name:
        return clean(msg.file.name)
    return clean(track_label(msg)) + (msg.file.ext or ".mp3")


def fix_tags(path, msg, album):
    """Заполняет только пустые теги, существующие не трогает."""
    if not (FIX_TAGS and mutagen):
        return
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        a = audio_attr(msg)
        wanted = {
            "title": (a.title if a else None) or os.path.splitext(os.path.basename(path))[0],
            "artist": a.performer if a else None,
            "album": album,
        }
        changed = False
        for key, value in wanted.items():
            if value and not audio.tags.get(key):
                audio.tags[key] = value
                changed = True
        if changed:
            audio.save()
    except Exception as e:
        log.warning("tags: %s: %s", path, e)


# ---------- сканирование ----------

async def scan(user):
    """Просматривает новые посты, складывает найденные релизы в pending.
    Возвращает (сколько новых релизов, список ошибок)."""
    new_total, errors = 0, []
    for ref in CHANNELS:
        try:
            ent = await user.get_entity(channel_ref(ref))
            title = getattr(ent, "title", None) or ref
            ch = state["channels"].setdefault(ref, {"last_id": 0})
            ch.update(id=ent.id, title=title)

            releases = {}  # альбом в Telegram = одна медиагруппа (grouped_id)
            last = ch["last_id"]
            async for msg in user.iter_messages(ent, min_id=ch["last_id"], reverse=True):
                last = msg.id
                if not is_track(msg):
                    continue
                key = f"{ent.id}_{msg.grouped_id or msg.id}"
                r = releases.setdefault(key, {
                    "ref": ref, "chan_id": ent.id, "chan": title,
                    "ids": [], "tracks": 0, "size": 0,
                    "date": msg.date.astimezone(TZ).strftime("%d.%m.%Y"),
                    "album": "", "first": track_label(msg),
                })
                r["ids"].append(msg.id)
                r["tracks"] += 1
                r["size"] += msg.file.size or 0
                if msg.text and not r["album"]:
                    r["album"] = msg.text.strip().splitlines()[0][:80]

            for key, r in releases.items():
                name = r["album"] or r["first"]
                if r["tracks"] > 1:
                    name += f" · {r['tracks']} тр."
                r["title"] = name[:100]
                state["pending"][key] = r
            ch["last_id"] = last
            save_state()
            new_total += len(releases)
            log.info("[%s] новых релизов: %d", title, len(releases))
        except FloodWaitError as e:
            errors.append(f"{ref}: Telegram просит подождать {e.seconds} с")
        except Exception as e:
            errors.append(f"{ref}: {e}")
            log.exception("Ошибка на канале %s", ref)
    return new_total, errors


async def download(user, key):
    """Скачивает один релиз, возвращает число сохранённых файлов."""
    r = state["pending"][key]
    ent = await user.get_entity(channel_ref(r["ref"]))
    msgs = await user.get_messages(ent, ids=r["ids"])
    folder = os.path.join(MUSIC_DIR, clean(r["chan"]))
    os.makedirs(folder, exist_ok=True)
    album = r["album"] or r["chan"]
    saved = 0
    for m in msgs:
        if not (m and is_track(m)):
            continue  # пост могли удалить
        path = os.path.join(folder, file_name(m))
        if os.path.exists(path):
            continue
        tmp = path + ".part"  # Navidrome не увидит недокачанный файл
        await m.download_media(file=tmp)
        os.replace(tmp, path)
        fix_tags(path, m, album)
        saved += 1
        log.info("[%s] + %s", r["chan"], os.path.basename(path))
    del state["pending"][key]
    save_state()
    return saved


# ---------- сообщения бота ----------

def chan_keys(cid):
    keys = [k for k, r in state["pending"].items() if r["chan_id"] == cid]
    return sorted(keys, key=lambda k: state["pending"][k]["ids"][0])


def chan_title(cid):
    for ch in state["channels"].values():
        if ch.get("id") == cid:
            return ch["title"]
    return str(cid)


def channel_view(cid, page=0):
    keys = chan_keys(cid)
    title = chan_title(cid)
    if not keys:
        return f"✅ <b>{esc(title)}</b>: всё разобрано", None
    pages = math.ceil(len(keys) / PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = keys[page * PAGE:(page + 1) * PAGE]

    lines = [f"🎵 <b>{esc(title)}</b> — ждут решения: {len(keys)}", ""]
    btns = []
    for i, k in enumerate(chunk, start=page * PAGE + 1):
        r = state["pending"][k]
        lines.append(f"{i}. {esc(r['title'])} <i>({r['date']}, {mb(r['size'])})</i>")
        btns.append(Button.inline(f"⬇ {i}", f"dl:{k}:{page}"))

    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    nav = []
    if page > 0:
        nav.append(Button.inline("◀", f"pg:{cid}:{page - 1}"))
    if pages > 1:
        nav.append(Button.inline(f"{page + 1}/{pages}", f"pg:{cid}:{page}"))
    if page < pages - 1:
        nav.append(Button.inline("▶", f"pg:{cid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        Button.inline(f"⬇ Скачать все ({len(keys)})", f"all:{cid}"),
        Button.inline("✖ Пропустить все", f"skip:{cid}"),
    ])
    return "\n".join(lines), rows


async def send_pending(bot):
    cids = sorted({r["chan_id"] for r in state["pending"].values()})
    for cid in cids:
        text, rows = channel_view(cid)
        await bot.send_message(OWNER_ID, text, buttons=rows)
    return len(cids)


scan_lock = asyncio.Lock()
dl_lock = asyncio.Lock()


async def run_scan(user, bot, reason):
    async with scan_lock:
        log.info("Скан (%s)", reason)
        new, errors = await scan(user)
        head = f"🗓 <b>Проверка каналов</b> ({reason})\nНовых релизов: {new}"
        if errors:
            head += "\n\n⚠️ Ошибки:\n" + "\n".join(esc(e) for e in errors)
        await bot.send_message(OWNER_ID, head)
        if not await send_pending(bot):
            await bot.send_message(OWNER_ID, "Нечего скачивать 🙂")


async def scheduler(user, bot):
    """Раз в час проверяет, не пора ли делать ежемесячный скан.
    Если контейнер был выключен в нужный день, скан пройдёт при запуске."""
    while True:
        now = dt.datetime.now(TZ)
        month = now.strftime("%Y-%m")
        slot = now.replace(day=SCAN_DAY, hour=SCAN_HOUR, minute=0, second=0, microsecond=0)
        if now >= slot and state["last_scan"] != month:
            try:
                await run_scan(user, bot, f"плановая, {now:%m.%Y}")
                state["last_scan"] = month
                save_state()
            except Exception:
                log.exception("Плановый скан не удался, повторю через час")
        await asyncio.sleep(3600)


# ---------- запуск ----------

def setup_handlers(user, bot):
    def is_owner(ev):
        return OWNER_ID and ev.sender_id == OWNER_ID

    @bot.on(events.NewMessage(pattern=r"^/start"))
    async def on_start(ev):
        if not OWNER_ID:
            await ev.reply(f"Ваш id: <code>{ev.sender_id}</code>\nВпишите его в OWNER_ID и перезапустите контейнер.")
        elif is_owner(ev):
            await ev.reply(
                "Привет! Раз в месяц ({} число, {}:00) я проверю каналы и пришлю новые релизы.\n\n"
                "/scan — проверить сейчас\n/pending — показать неразобранное".format(SCAN_DAY, SCAN_HOUR)
            )

    @bot.on(events.NewMessage(pattern=r"^/scan"))
    async def on_scan(ev):
        if is_owner(ev):
            if scan_lock.locked():
                return await ev.reply("Уже сканирую, подождите")
            await ev.reply("🔎 Сканирую…")
            await run_scan(user, bot, "вручную")

    @bot.on(events.NewMessage(pattern=r"^/pending"))
    async def on_pending(ev):
        if is_owner(ev) and not await send_pending(bot):
            await ev.reply("Неразобранных релизов нет")

    async def refresh(ev, cid, page):
        text, rows = channel_view(cid, page)
        try:
            await ev.edit(text, buttons=rows)
        except MessageNotModifiedError:
            pass

    @bot.on(events.CallbackQuery)
    async def on_button(ev):
        if not is_owner(ev):
            return await ev.answer("Нет доступа")
        action, *args = ev.data.decode().split(":")

        if action == "pg":
            await ev.answer()
            await refresh(ev, int(args[0]), int(args[1]))

        elif action == "dl":
            key, page = args[0], int(args[1])
            r = state["pending"].get(key)
            if not r:
                await ev.answer("Уже разобрано")
                return await refresh(ev, int(key.split("_")[0]), page)
            await ev.answer("Качаю…")
            async with dl_lock:
                try:
                    n = await download(user, key)
                    await bot.send_message(OWNER_ID, f"✅ {esc(r['title'])}: файлов {n}")
                except Exception as e:
                    log.exception("download")
                    await bot.send_message(OWNER_ID, f"❌ {esc(r['title'])}: {esc(str(e))}")
            await refresh(ev, r["chan_id"], page)

        elif action == "all":
            cid = int(args[0])
            keys = chan_keys(cid)
            await ev.answer(f"Качаю {len(keys)}…")
            progress = await bot.send_message(OWNER_ID, f"⏳ {esc(chan_title(cid))}: 0/{len(keys)}")
            files = failed = 0
            async with dl_lock:
                for i, key in enumerate(keys, 1):
                    if key not in state["pending"]:
                        continue
                    try:
                        files += await download(user, key)
                    except Exception:
                        failed += 1
                        log.exception("download %s", key)
                    try:
                        await progress.edit(f"⏳ {esc(chan_title(cid))}: {i}/{len(keys)}")
                    except Exception:
                        pass
            await progress.edit(
                f"✅ {esc(chan_title(cid))}: скачано файлов {files}" + (f", ошибок {failed}" if failed else "")
            )
            await refresh(ev, cid, 0)

        elif action == "skip":
            cid = int(args[0])
            for key in chan_keys(cid):
                del state["pending"][key]
            save_state()
            await ev.answer("Пропущено")
            await refresh(ev, cid, 0)


async def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    user = TelegramClient(os.path.join(DATA_DIR, "user"), API_ID, API_HASH)
    user.flood_sleep_threshold = 120

    if "--login" in sys.argv:
        await user.start()
        me = await user.get_me()
        log.info("Вход выполнен: %s (id %s)", me.first_name, me.id)
        await user.disconnect()
        return

    await user.connect()
    if not await user.is_user_authorized():
        log.error("Нет сессии. Сначала: docker compose run --rm tg-music-sync python sync.py --login")
        sys.exit(1)

    bot = TelegramClient(os.path.join(DATA_DIR, "bot"), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    bot.parse_mode = "html"
    setup_handlers(user, bot)

    if not OWNER_ID:
        log.warning("OWNER_ID не задан: напишите боту /start, он пришлёт ваш id")
    elif not CHANNELS:
        log.error("CHANNELS пуст")
    else:
        bot._scheduler_task = asyncio.create_task(scheduler(user, bot))  # держим ссылку

    log.info("Бот запущен. Плановый скан: %d число, %02d:00 (%s)", SCAN_DAY, SCAN_HOUR, TZ)
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
