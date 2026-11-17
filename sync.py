"""
tg-music-sync: раз в месяц проверяет Telegram-каналы, присылает через бота
список новых релизов и по кнопке скачивает выбранное в папку Navidrome.

Два клиента в одном процессе:
  * user — отдельный аккаунт Telegram, подписанный на каналы: читает и качает;
  * bot  — ваш бот из @BotFather: пишет вам отчёт и принимает нажатия кнопок.

Запуск:
  python sync.py --login        # один раз, интерактивный вход в аккаунт-читалку
  python sync.py                # основной режим
  python sync.py --fix-tags     # показать, какие теги будут исправлены (tagfix.py)
  python sync.py --fix-tags --apply   # исправить; откат: python tagfix.py --undo <бэкап>
"""
import asyncio
import datetime as dt
import html
import json
import logging
import math
import os
import random
import re
import sys
import time
from zoneinfo import ZoneInfo

from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeAudio

try:
    import mutagen
except ImportError:
    mutagen = None

import tagfix

# ---------- настройки ----------

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "").split(",") if c.strip()]
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music/telegram")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SCAN_DAY = min(max(int(os.environ.get("SCAN_DAY", "1")), 1), 28)
SCAN_HOUR = min(max(int(os.environ.get("SCAN_HOUR", "10")), 0), 23)
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
FIX_TAGS = os.environ.get("FIX_TAGS", "true").lower() == "true"
AUTO_FIX_TAGS = os.environ.get("AUTO_FIX_TAGS", "true").lower() == "true"
DOWNLOAD_DELAY = max(float(os.environ.get("DOWNLOAD_DELAY", "3")), 0)   # пауза между файлами, с
MAX_FILES_PER_RUN = max(int(os.environ.get("MAX_FILES_PER_RUN", "200")), 1)

STATE_FILE = os.path.join(DATA_DIR, "state.json")
PAGE = 8                     # релизов на одной странице сообщения
DEFAULT_SPEED = 1024 * 1024  # байт/с — стартовая оценка, пока скорость не измерена
PROGRESS_EVERY = 5           # не чаще раза в N секунд обновлять сообщение о прогрессе
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-music-sync")
logging.getLogger("telethon").setLevel(logging.WARNING)

# ---------- подписи постов ----------

MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL = re.compile(r"(https?://|t\.me/|@)\S+")


def clean_caption(text, chan):
    """Первая осмысленная строка подписи: без ссылок, хэштегов и подписи канала."""
    for line in (text or "").splitlines():
        line = MD_LINK.sub(r"\1", line)
        line = URL.sub("", line).strip(" \t-—–|•·:")
        if len(line) < 2 or line.startswith("#"):
            continue
        if chan and line.casefold() == chan.casefold():
            continue
        return line[:80]
    return ""


def make_title(r):
    if r["tracks"] > 1:
        return f"{r['album'] or r['first']} · {r['tracks']} тр."[:100]
    return r["first"][:100]


# ---------- состояние ----------
# channels: {ref: {id, title, last_id}}  — докуда просмотрен канал
# pending:  {key: release}               — найденные, но ещё не разобранные релизы
# last_scan: "YYYY-MM"                   — месяц последнего планового скана
# speed: байт/с                          — измеренная скорость скачивания


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    st.setdefault("channels", {})
    st.setdefault("pending", {})
    st.setdefault("last_scan", "")
    # пересчёт записей старых версий: альбом — только у пачек и без ссылок
    for r in st["pending"].values():
        r["album"] = clean_caption(r.get("album", ""), r["chan"]) if r["tracks"] > 1 else ""
        r["title"] = make_title(r)
    return st


state = load_state() if os.path.isdir(DATA_DIR) else {"channels": {}, "pending": {}, "last_scan": ""}


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)  # атомарно: state.json не бывает «наполовину записан»


# ---------- утилиты ----------

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean(name, fallback="unknown"):
    name = BAD_CHARS.sub("_", name or "").strip(" .")
    return name[:150] or fallback


def channel_ref(ch):
    return int(ch) if re.fullmatch(r"-?\d+", ch) else ch


def esc(s):
    return html.escape(s or "")


def human_size(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} ГБ"
    return f"{size / 1024 ** 2:.0f} МБ"


def human_time(sec):
    sec = int(sec)
    if sec < 60:
        return "< 1 мин"
    if sec < 3600:
        return f"{round(sec / 60)} мин"
    h, m = divmod(round(sec / 60), 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def speed():
    return state.get("speed") or DEFAULT_SPEED


def eta(size, files):
    """Оценка времени: объём / скорость + паузы между файлами."""
    return size / speed() + files * DOWNLOAD_DELAY * 1.1


def speed_text():
    s = f"{speed() / 1024 ** 2:.1f} МБ/с"
    return s if state.get("speed") else s + ", оценка"


def is_track(msg):
    f = msg.file
    if not f or msg.voice or msg.video_note:
        return False
    mime = (f.mime_type or "").lower()
    return mime.startswith("audio/") or (f.ext or "").lower() in AUDIO_EXT


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
        title = (a.title if a else None) or os.path.splitext(os.path.basename(path))[0]
        wanted = {
            "title": title,
            "artist": a.performer if a else None,
            "album": album or title,
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


def remove_part_files():
    """Удаляет недокачанные .part (например, после docker stop посреди файла)."""
    n = 0
    for root, _, files in os.walk(MUSIC_DIR):
        for f in files:
            if f.endswith(".part"):
                os.remove(os.path.join(root, f))
                n += 1
    if n:
        log.info("Удалено недокачанных файлов: %d", n)


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
            async for msg in user.iter_messages(ent, min_id=ch["last_id"], reverse=True, wait_time=1):
                last = msg.id
                if not is_track(msg):
                    continue
                key = f"{ent.id}_{msg.grouped_id or msg.id}"
                r = releases.setdefault(key, {
                    "ref": ref, "chan_id": ent.id, "chan": title,
                    "ids": [], "tracks": 0, "size": 0,
                    "date": msg.date.astimezone(TZ).strftime("%d.%m.%Y"),
                    "caption": "", "first": track_label(msg),
                })
                r["ids"].append(msg.id)
                r["tracks"] += 1
                r["size"] += msg.file.size or 0
                if msg.raw_text and not r["caption"]:
                    r["caption"] = msg.raw_text

            for key, r in releases.items():
                caption = r.pop("caption")
                r["album"] = clean_caption(caption, title) if r["tracks"] > 1 else ""
                r["title"] = make_title(r)
                state["pending"][key] = r
            ch["last_id"] = last
            save_state()
            new_total += len(releases)
            log.info("[%s] новых релизов: %d", title, len(releases))
        except FloodWaitError as e:
            errors.append(f"{ref}: Telegram просит подождать {human_time(e.seconds)}, повторю в следующий раз")
        except Exception as e:
            errors.append(f"{ref}: {e}")
            log.exception("Ошибка на канале %s", ref)
    return new_total, errors


# ---------- скачивание с остановкой ----------

class Cancelled(Exception):
    pass


cancel_event = asyncio.Event()
job = {"task": None}


def job_running():
    return job["task"] is not None and not job["task"].done()


async def pause(seconds):
    """Пауза, которую можно прервать остановкой. True — если остановили."""
    try:
        await asyncio.wait_for(cancel_event.wait(), seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def cancellable(coro, tmp=None):
    """Выполняет запрос; при остановке прерывает его и удаляет недокачанный файл."""
    task = asyncio.ensure_future(coro)
    stop = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
    if task in done:
        if task.exception() and tmp and os.path.exists(tmp):
            os.remove(tmp)
        return task.result()
    task.cancel()
    try:
        await task
    except BaseException:
        pass
    if tmp and os.path.exists(tmp):
        os.remove(tmp)
    raise Cancelled


class Progress:
    """Сообщение с прогрессом, оценкой времени и кнопкой «Остановить»."""

    def __init__(self, bot, label, files, size):
        self.bot, self.label = bot, label
        self.files, self.size = files, size
        self.done_files = self.done_size = 0
        self.note, self.msg, self.last = "", None, 0.0

    def add(self, size):
        self.done_files += 1
        self.done_size += size

    def text(self):
        left_size = max(self.size - self.done_size, 0)
        left_files = max(self.files - self.done_files, 0)
        lines = [
            f"⬇ <b>{esc(self.label)}</b>",
            f"Файлы: {self.done_files}/{self.files} · {human_size(self.done_size)} из {human_size(self.size)}",
            f"Осталось ≈ {human_time(eta(left_size, left_files))} (скорость {speed_text()})",
        ]
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)

    async def start(self):
        self.msg = await self.bot.send_message(
            OWNER_ID, self.text(), buttons=[[Button.inline("⏹ Остановить", "stop")]]
        )
        self.last = time.monotonic()

    async def update(self, force=False):
        if not force and time.monotonic() - self.last < PROGRESS_EVERY:
            return
        self.last = time.monotonic()
        try:
            await self.msg.edit(self.text(), buttons=[[Button.inline("⏹ Остановить", "stop")]])
        except Exception:
            pass  # MessageNotModified, FloodWait бота и т.п. — прогресс не критичен

    async def finish(self, text):
        try:
            await self.msg.edit(text, buttons=None)
        except Exception:
            await self.bot.send_message(OWNER_ID, text)


async def flood_safe(factory, prog, tmp=None):
    """Повторяет запрос после FloodWait, честно выжидая паузу Telegram."""
    while True:
        try:
            return await cancellable(factory(), tmp)
        except FloodWaitError as e:
            log.warning("FloodWait %s с", e.seconds)
            prog.note = f"⏸ Telegram попросил паузу на {human_time(e.seconds)} — жду и продолжу"
            await prog.update(force=True)
            if await pause(e.seconds + 5):
                raise Cancelled
            prog.note = ""


def update_speed(size, seconds):
    if size < 512 * 1024 or seconds <= 0:
        return  # по мелким файлам скорость не мерим
    measured = size / seconds
    old = state.get("speed")
    state["speed"] = measured if not old else old * 0.7 + measured * 0.3


async def download(user, key, prog):
    """Скачивает один релиз. Релиз убирается из pending только целиком;
    при остановке уже скачанные файлы остаются и при повторе пропускаются."""
    r = state["pending"][key]
    ent = await flood_safe(lambda: user.get_entity(channel_ref(r["ref"])), prog)
    msgs = await flood_safe(lambda: user.get_messages(ent, ids=r["ids"]), prog)
    folder = os.path.join(MUSIC_DIR, clean(r["chan"]))
    os.makedirs(folder, exist_ok=True)
    # сингл: альбом = название трека; пачка без подписи: альбом = первый трек
    album = r["album"] or (r["first"] if r["tracks"] > 1 else None)
    saved = 0
    for m in msgs:
        if cancel_event.is_set():
            raise Cancelled
        if not (m and is_track(m)):
            continue  # пост могли удалить
        size = m.file.size or 0
        path = os.path.join(folder, file_name(m))
        if os.path.exists(path):
            prog.add(size)
            continue
        tmp = path + ".part"  # Navidrome не видит .part
        t0 = time.monotonic()
        await flood_safe(lambda: m.download_media(file=tmp), prog, tmp)
        os.replace(tmp, path)  # переименование в той же папке — без копии
        update_speed(size, time.monotonic() - t0)
        fix_tags(path, m, album)
        saved += 1
        prog.add(size)
        log.info("[%s] + %s", r["chan"], os.path.basename(path))
        await prog.update()
        if await pause(DOWNLOAD_DELAY * random.uniform(0.7, 1.5)):
            raise Cancelled
    del state["pending"][key]
    save_state()
    return saved


async def download_job(user, bot, keys, label):
    keys = [k for k in keys if k in state["pending"]]
    batch, files = [], 0
    for k in keys:  # не больше MAX_FILES_PER_RUN файлов за раз (минимум один релиз)
        n = state["pending"][k]["tracks"]
        if batch and files + n > MAX_FILES_PER_RUN:
            break
        batch.append(k)
        files += n
    size = sum(state["pending"][k]["size"] for k in batch)

    prog = Progress(bot, label, files, size)
    await prog.start()
    saved = failed = 0
    stopped = False
    started = time.monotonic()
    for k in batch:
        try:
            saved += await download(user, k, prog)
        except Cancelled:
            stopped = True
            break
        except Exception:
            failed += 1
            log.exception("download %s", k)
    save_state()  # сохранить измеренную скорость

    tag_note = ""
    if saved and AUTO_FIX_TAGS:
        try:
            n, _ = await asyncio.to_thread(tagfix.run, MUSIC_DIR, True, DATA_DIR)
            tag_note = f"Теги приведены в порядок: {n} файлов" if n else ""
        except Exception:
            log.exception("tagfix")
            tag_note = "⚠️ Не удалось исправить теги, см. логи"

    left = len([k for k in keys if k in state["pending"]])
    lines = [
        ("⏹ <b>Остановлено</b>" if stopped else "✅ <b>Готово</b>") + f": {esc(label)}",
        f"Скачано файлов: {saved} за {human_time(time.monotonic() - started)}",
    ]
    if failed:
        lines.append(f"Ошибок: {failed} (релизы остались в списке)")
    if stopped:
        lines.append("Недокачанный файл удалён, уже скачанные на месте.")
    if tag_note:
        lines.append(tag_note)
    if left:
        lines.append(f"Осталось релизов: {left} — /pending")
    await prog.finish("\n".join(lines))


def start_job(user, bot, keys, label):
    if job_running():
        return False
    cancel_event.clear()
    job["task"] = asyncio.create_task(download_job(user, bot, keys, label))
    return True


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

    rels = [state["pending"][k] for k in keys]
    total_size = sum(r["size"] for r in rels)
    total_files = sum(r["tracks"] for r in rels)
    lines = [
        f"🎵 <b>{esc(title)}</b> — ждут решения: {len(keys)}",
        f"Всего {total_files} файлов, {human_size(total_size)} · ≈ {human_time(eta(total_size, total_files))}",
    ]
    if total_files > MAX_FILES_PER_RUN:
        lines.append(f"<i>За одно нажатие качается до {MAX_FILES_PER_RUN} файлов</i>")
    lines.append("")
    btns = []
    for i, k in enumerate(chunk, start=page * PAGE + 1):
        r = state["pending"][k]
        lines.append(f"{i}. {esc(r['title'])} <i>({r['date']}, {human_size(r['size'])})</i>")
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
                f"Привет! Раз в месяц ({SCAN_DAY} число, {SCAN_HOUR}:00) я проверю каналы и пришлю новые релизы.\n\n"
                "/scan — проверить сейчас\n/pending — показать неразобранное\n/stop — остановить скачивание"
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

    @bot.on(events.NewMessage(pattern=r"^/stop"))
    async def on_stop(ev):
        if is_owner(ev):
            if job_running():
                cancel_event.set()
                await ev.reply("⏹ Останавливаю…")
            else:
                await ev.reply("Сейчас ничего не качается")

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

        if action == "stop":
            if job_running():
                cancel_event.set()
                await ev.answer("Останавливаю…")
            else:
                await ev.answer("Уже не качается")

        elif action == "pg":
            await ev.answer()
            await refresh(ev, int(args[0]), int(args[1]))

        elif action == "dl":
            key, page = args[0], int(args[1])
            r = state["pending"].get(key)
            if not r:
                await ev.answer("Уже разобрано")
                return await refresh(ev, int(key.split("_")[0]), page)
            if not start_job(user, bot, [key], r["title"]):
                return await ev.answer("Уже идёт скачивание — дождитесь или /stop", alert=True)
            await ev.answer(f"Качаю, ≈ {human_time(eta(r['size'], r['tracks']))}")

        elif action == "all":
            cid = int(args[0])
            if not start_job(user, bot, chan_keys(cid), chan_title(cid)):
                return await ev.answer("Уже идёт скачивание — дождитесь или /stop", alert=True)
            await ev.answer("Начинаю скачивание")

        elif action == "skip":
            cid = int(args[0])
            for key in chan_keys(cid):
                del state["pending"][key]
            save_state()
            await ev.answer("Пропущено")
            await refresh(ev, cid, 0)


async def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    if "--fix-tags" in sys.argv or "--fix-albums" in sys.argv:
        tagfix.main([MUSIC_DIR] + [a for a in sys.argv[1:] if a in ("--apply",)])
        return

    user = TelegramClient(os.path.join(DATA_DIR, "user"), API_ID, API_HASH)
    user.flood_sleep_threshold = 60  # короткие паузы Telethon выжидает сам, длинные — наш код

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

    remove_part_files()

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
