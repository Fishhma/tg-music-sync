"""
tagfix — приводит теги в порядок, чтобы Navidrome правильно собирал альбомы.

  python3 tagfix.py [ПАПКА]              # просмотр: что изменится (файлы не трогаются)
  python3 tagfix.py [ПАПКА] --apply      # применить; старые теги -> DATA_DIR/tags_backup_*.json
  python3 tagfix.py --undo ФАЙЛ_БЭКАПА   # вернуть теги из резервной копии

Правила:
  1. Чистка: лишние пробелы, хвосты вида "By_XXXX" у исполнителей, ссылки и
     префикс года ("2014 - Альбом") в названии альбома.
  2. Одинаковое написание исполнителей: варианты, отличающиеся регистром,
     пробелами и знаками препинания, приводятся к самому частому.
  3. Мусор вместо альбома (пусто, "single", "#хэштег", название канала, ссылка)
     -> сингл: альбом = название трека.
  4. Исполнитель альбома (albumartist) для каждого альбома:
       одна группа (в т.ч. "X feat. Y")      -> X
       сборник (Vol., Teil, II…) или > 3 групп -> Various Artists + флаг compilation
       2–3 группы (сплит)                    -> "A & B"
  5. Один год на весь альбом — самый частый.
  Ручные исключения: DATA_DIR/tag_overrides.json (см. tag_overrides.example.json).
"""
import collections
import datetime as dt
import json
import os
import re
import sys

import mutagen

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus"}
FIELDS = ["artist", "albumartist", "album", "title", "date", "compilation"]
VA = "Various Artists"
MAX_SPLIT_ARTISTS = 3

JUNK_ALBUMS = {"single", "singles", "сингл", "синглы", "same", "unknown", "unknown album",
               "неизвестно", "без альбома", "untitled", "track", "new"}
COMPILATION_RE = re.compile(
    r"\b(vol(ume)?\b\.?|teil|part|chapter|sampler|compilation|collection|сборник|tribute)"
    r"|\s(ii|iii|iv|v|vi|vii|viii|ix|x)\s*$",
    re.I,
)
FEAT_RE = re.compile(r"\s*[(\[]?\s*\b(feat\.?|ft\.|featuring|при участии)\s.*$", re.I)
BY_RE = re.compile(r"\s+by_\S+$", re.I)
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL = re.compile(r"(https?://|t\.me/)\S+")
YEAR_PREFIX = re.compile(r"^[(\[]?\d{4}[)\]]?\s*[-–—.:]\s*")


def default_data_dir():
    if os.environ.get("DATA_DIR"):
        return os.environ["DATA_DIR"]
    if os.path.isdir("/data"):
        return "/data"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def space(s):
    return re.sub(r"\s+", " ", s or "").strip()


def loose(s):
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def primary(artist):
    return FEAT_RE.sub("", artist).strip() or artist


def get(tags, key):
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if not v:
        return ""
    return str(v[0]) if isinstance(v, list) else str(v)


# ---------- чтение ----------

def load(root):
    recs = []
    for folder, _, files in os.walk(root):
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                continue
            path = os.path.join(folder, name)
            try:
                audio = mutagen.File(path, easy=True)
            except Exception:
                continue
            if audio is None:
                continue
            tags = audio.tags or {}
            orig = {f: get(tags, f) for f in FIELDS}
            rel = os.path.relpath(path, root)
            recs.append({
                "path": path, "rel": rel,
                "channel": rel.split(os.sep)[0] if os.sep in rel else "",
                "stem": os.path.splitext(name)[0],
                "orig": orig, "new": dict(orig), "single": False,
            })
    return recs


def load_overrides(data_dir):
    p = os.path.join(data_dir, "tag_overrides.json")
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f).get("albums", {})
        return {space(k).casefold(): v for k, v in raw.items()}
    except FileNotFoundError:
        return {}


# ---------- правила ----------

def plan(recs, overrides=None):
    overrides = overrides or {}
    channels = {r["channel"].casefold() for r in recs if r["channel"]}

    # 1. чистка
    for r in recs:
        n = r["new"]
        for f in ("artist", "albumartist"):
            n[f] = BY_RE.sub("", space(n[f]))
        a = URL.sub("", MD_LINK.sub(r"\1", n["album"]))
        a = space(a).strip(" -–—|")
        a2 = YEAR_PREFIX.sub("", a).strip()
        n["album"] = a2 or a
        n["title"] = space(n["title"])

    # 2. одно написание исполнителя
    spell = collections.defaultdict(collections.Counter)
    for r in recs:
        for f in ("artist", "albumartist"):
            if r["new"][f]:
                spell[loose(r["new"][f])][r["new"][f]] += 1
    canon = {k: c.most_common(1)[0][0] for k, c in spell.items()}
    for r in recs:
        for f in ("artist", "albumartist"):
            if r["new"][f]:
                r["new"][f] = canon[loose(r["new"][f])]

    # 3. мусор вместо альбома -> сингл
    for r in recs:
        n = r["new"]
        al = n["album"].casefold()
        if (not al or al in JUNK_ALBUMS or al.startswith("#") or al in channels
                or URL.search(r["orig"]["album"]) or "](" in r["orig"]["album"]):
            n["album"] = n["title"] or r["stem"]
            n["albumartist"] = n["albumartist"] or primary(n["artist"])
            n["compilation"] = ""
            r["single"] = True
        elif n["title"] and al == n["title"].casefold():
            r["single"] = True  # уже оформленный сингл: альбом = название трека

    # 4–5. альбомы
    groups = collections.defaultdict(list)
    for r in recs:
        n = r["new"]
        key = (n["album"].casefold(), loose(primary(n["albumartist"] or n["artist"]))) if r["single"] \
            else (n["album"].casefold(), "")
        groups[key].append(r)

    for (album_cf, _), rs in groups.items():
        album = collections.Counter(r["new"]["album"] for r in rs).most_common(1)[0][0]
        for r in rs:
            r["new"]["album"] = album

        if not rs[0]["single"]:
            ov = overrides.get(album_cf)
            aas = {r["new"]["albumartist"] for r in rs}
            if ov:
                aa = VA if ov.get("compilation") else ov.get("albumartist", "")
                comp = "1" if ov.get("compilation") else ""
            elif len(aas) == 1 and "" not in aas:
                aa, comp = aas.pop(), rs[0]["new"]["compilation"]
            else:
                order = collections.Counter()
                for r in rs:
                    order[primary(r["new"]["albumartist"] or r["new"]["artist"])] += 1
                prims = [p for p, _ in order.most_common()]
                if len(prims) == 1:
                    aa, comp = prims[0], ""
                elif COMPILATION_RE.search(album) or len(prims) > MAX_SPLIT_ARTISTS:
                    aa, comp = VA, "1"
                else:
                    aa, comp = " & ".join(prims), ""
            if aa:
                for r in rs:
                    r["new"]["albumartist"] = aa
                    r["new"]["compilation"] = comp

        dates = [r["new"]["date"] for r in rs]
        if len(set(dates)) > 1:
            known = collections.Counter(d for d in dates if d)
            if known:
                best = known.most_common(1)[0][0]
                for r in rs:
                    r["new"]["date"] = best

    for r in recs:
        r["changes"] = {f: (r["orig"][f], r["new"][f]) for f in FIELDS if r["orig"][f] != r["new"][f]}
    return recs


# ---------- запись ----------

def write_tags(path, values):
    audio = mutagen.File(path, easy=True)
    if audio.tags is None:
        audio.add_tags()
    for f, v in values.items():
        try:
            if v:
                audio.tags[f] = v
            elif f in audio.tags:
                del audio.tags[f]
        except (KeyError, ValueError):
            pass  # формат не поддерживает это поле
    audio.save()


def apply(recs, data_dir):
    changed = [r for r in recs if r["changes"]]
    if not changed:
        return 0, None
    os.makedirs(data_dir, exist_ok=True)
    backup = os.path.join(data_dir, f"tags_backup_{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    with open(backup, "w", encoding="utf-8") as f:
        json.dump({r["path"]: {k: o for k, (o, _) in r["changes"].items()} for r in changed},
                  f, ensure_ascii=False, indent=1)
    done = 0
    for r in changed:
        try:
            write_tags(r["path"], {k: n for k, (_, n) in r["changes"].items()})
            done += 1
        except Exception as e:
            print(f"! {r['rel']}: {e}", file=sys.stderr)
    return done, backup


def undo(backup):
    with open(backup, encoding="utf-8") as f:
        data = json.load(f)
    done = 0
    for path, values in data.items():
        try:
            write_tags(path, values)
            done += 1
        except Exception as e:
            print(f"! {path}: {e}", file=sys.stderr)
    return done


def run(root, do_apply=False, data_dir=None):
    """Для бота: применить правила и вернуть (изменено файлов, путь бэкапа)."""
    data_dir = data_dir or default_data_dir()
    recs = plan(load(root), load_overrides(data_dir))
    if not do_apply:
        return sum(1 for r in recs if r["changes"]), None
    return apply(recs, data_dir)


# ---------- вывод ----------

def show(recs):
    changed = [r for r in recs if r["changes"]]
    by_album = collections.defaultdict(list)
    for r in changed:
        by_album[(r["new"]["albumartist"], r["new"]["album"], r["single"])].append(r)
    for (aa, album, single), rs in sorted(by_album.items(), key=lambda x: (x[0][2], x[0][0].casefold(), x[0][1].casefold())):
        print(f"\n■ {aa or '—'} — {album}" + ("  [сингл]" if single else "") + f"  ({len(rs)} файл.)")
        for r in rs:
            ch = "; ".join(f"{k}: «{o or '—'}» → «{n or '—'}»" for k, (o, n) in r["changes"].items())
            print(f"    {r['rel']}: {ch}")
    stats = collections.Counter(k for r in changed for k in r["changes"])
    print(f"\nИтого: файлов {len(recs)}, будет изменено {len(changed)}")
    for k, n in stats.most_common():
        print(f"  {k:12} {n}")


def main(argv):
    if "--undo" in argv:
        backup = argv[argv.index("--undo") + 1]
        print(f"Восстановлено файлов: {undo(backup)}")
        return
    pos = [a for a in argv if not a.startswith("--")]
    root = pos[0] if pos else os.environ.get("MUSIC_DIR", "/music/telegram")
    if not os.path.isdir(root):
        sys.exit(f"Нет папки {root}")
    data_dir = default_data_dir()
    recs = plan(load(root), load_overrides(data_dir))
    show(recs)
    if "--apply" in argv:
        done, backup = apply(recs, data_dir)
        print(f"\nПрименено к {done} файлам. Откат: python3 tagfix.py --undo {backup}" if backup else "\nМенять нечего.")
    else:
        print("\nЭто просмотр, файлы не изменены. Применить: добавьте --apply")


if __name__ == "__main__":
    main(sys.argv[1:])
