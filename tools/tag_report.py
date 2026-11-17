"""
Отчёт о тегах в папке музыки. Ничего не меняет — только читает.

Показывает, почему Navidrome делит один альбом на несколько или склеивает разные:
разные исполнители альбома (сборники и сплиты), разные даты, разное написание,
пустые теги, дубли треков.

Запуск в контейнере (из папки проекта):
  docker compose run --rm -v "$PWD/tools:/tools:ro" tg-music-sync python /tools/tag_report.py

Полная таблица всех файлов сохраняется в data/tag_report.csv.
"""
import collections
import csv
import os
import re
import sys

import mutagen

MUSIC_DIR = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MUSIC_DIR", "/music/telegram")
OUT = os.path.join(os.environ.get("DATA_DIR", "/data"), "tag_report.csv")
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
KEYS = ["artist", "albumartist", "album", "title", "date", "originaldate",
        "tracknumber", "discnumber", "compilation", "musicbrainz_albumid"]
LIMIT = 40  # сколько проблемных альбомов показывать на экране


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def loose(s):
    """Для поиска разного написания: только буквы и цифры."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def get(tags, key):
    try:
        v = tags.get(key)
    except Exception:
        return ""
    if not v:
        return ""
    return "; ".join(str(x) for x in v) if isinstance(v, list) else str(v)


def has_cover(path):
    try:
        f = mutagen.File(path)
    except Exception:
        return "?"
    if f is None:
        return "?"
    if getattr(f, "pictures", None):
        return "yes"
    tags = f.tags
    if tags is None:
        return "no"
    for k in tags.keys():
        if k.startswith("APIC") or k == "covr" or k.lower() == "metadata_block_picture":
            return "yes"
    return "no"


def read_all():
    rows, broken = [], []
    for root, _, files in os.walk(MUSIC_DIR):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            path = os.path.join(root, name)
            if name.endswith(".part"):
                broken.append((path, "недокачанный .part"))
                continue
            if ext not in AUDIO_EXT:
                continue
            try:
                audio = mutagen.File(path, easy=True)
            except Exception as e:
                broken.append((path, f"не читается: {e}"))
                continue
            if audio is None:
                broken.append((path, "формат не распознан"))
                continue
            tags = audio.tags or {}
            row = {k: get(tags, k) for k in KEYS}
            row["year"] = (row["date"] or row["originaldate"])[:4]
            row["cover"] = has_cover(path)
            row["folder"] = os.path.relpath(root, MUSIC_DIR)
            row["file"] = name
            rows.append(row)
    return rows, broken


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main():
    if not os.path.isdir(MUSIC_DIR):
        sys.exit(f"Нет папки {MUSIC_DIR}")
    rows, broken = read_all()

    # ---------- сводка ----------
    section(f"СВОДКА: {len(rows)} файлов в {MUSIC_DIR}")
    by_ext = collections.Counter(os.path.splitext(r["file"])[1].lower() for r in rows)
    print("Форматы:", ", ".join(f"{e} {n}" for e, n in by_ext.most_common()))
    for key, label in [("artist", "без исполнителя"), ("album", "без альбома"),
                       ("albumartist", "без исполнителя альбома (albumartist)"),
                       ("year", "без года"), ("tracknumber", "без номера трека"),
                       ("musicbrainz_albumid", "без MusicBrainz ID")]:
        n = sum(1 for r in rows if not r[key])
        print(f"  {label:45} {n:5}  ({n * 100 // max(len(rows), 1)}%)")
    n = sum(1 for r in rows if r["cover"] == "no")
    print(f"  {'без встроенной обложки':45} {n:5}")
    n = sum(1 for r in rows if r["compilation"] in ("1", "true", "True"))
    print(f"  {'с флагом compilation (сборник)':45} {n:5}")
    if broken:
        print(f"  {'проблемные файлы':45} {len(broken):5}")

    # ---------- альбомы, которые Navidrome разделит ----------
    albums = collections.defaultdict(list)
    for r in rows:
        if r["album"]:
            albums[norm(r["album"])].append(r)

    problems = []
    for key, rs in albums.items():
        if all(norm(r["title"]) == key for r in rs):
            continue  # синглы разных групп с одинаковым названием — это нормально
        eff_artists = collections.Counter(r["albumartist"] or r["artist"] for r in rs)
        years = collections.Counter(r["year"] for r in rs)
        spellings = collections.Counter(r["album"] for r in rs)
        reasons = []
        if len(eff_artists) > 1:
            if all(not r["albumartist"] for r in rs):
                reasons.append("разные исполнители, albumartist пустой → сборник/сплит?")
            else:
                reasons.append("разный albumartist")
        if len(years) > 1:
            reasons.append("разные годы")
        if len(spellings) > 1:
            reasons.append("разное написание альбома")
        if reasons:
            problems.append((len(rs), key, rs, eff_artists, years, spellings, reasons))
    problems.sort(key=lambda p: -p[0])

    section(f"АЛЬБОМЫ, КОТОРЫЕ РАСПАДУТСЯ: {len(problems)}")
    reason_count = collections.Counter(r for p in problems for r in p[6])
    for reason, n in reason_count.most_common():
        print(f"  {n:4} × {reason}")
    for n, key, rs, eff_artists, years, spellings, reasons in problems[:LIMIT]:
        print()
        print(f"■ «{rs[0]['album']}» — {n} тр. — {'; '.join(reasons)}")
        if len(spellings) > 1:
            print("    написание:", " | ".join(f"«{s}» ×{c}" for s, c in spellings.most_common()))
        print("    исполнитель альбома:", " | ".join(f"{a or '—'} ×{c}" for a, c in eff_artists.most_common(8)))
        if len(years) > 1:
            print("    годы:", " | ".join(f"{y or '—'} ×{c}" for y, c in years.most_common()))
        folders = collections.Counter(r["folder"] for r in rs)
        print("    папки:", " | ".join(f"{f} ×{c}" for f, c in folders.most_common(3)))
    if len(problems) > LIMIT:
        print(f"\n… и ещё {len(problems) - LIMIT} — полный список в CSV")

    # ---------- один исполнитель + одна папка, но много «альбомов» по 1 треку ----------
    singles = collections.defaultdict(collections.Counter)
    for key, rs in albums.items():
        if len(rs) == 1:
            r = rs[0]
            singles[r["albumartist"] or r["artist"]][r["album"]] += 1
    many = [(a, c) for a, c in singles.items() if len(c) >= 3]
    many.sort(key=lambda x: -len(x[1]))
    section(f"ИСПОЛНИТЕЛИ С МНОЖЕСТВОМ «АЛЬБОМОВ» ИЗ ОДНОГО ТРЕКА: {len(many)}")
    print("(часто значит, что в album записано не название альбома)")
    for artist, c in many[:15]:
        print(f"  {artist or '—'}: {len(c)} шт. — " + ", ".join(f"«{a}»" for a in list(c)[:5]) + (" …" if len(c) > 5 else ""))

    # ---------- разное написание исполнителей ----------
    names = collections.defaultdict(collections.Counter)
    for r in rows:
        for field in ("artist", "albumartist"):
            if r[field]:
                names[loose(r[field])][r[field]] += 1
    variants = [(k, c) for k, c in names.items() if len(c) > 1]
    section(f"ИСПОЛНИТЕЛИ С РАЗНЫМ НАПИСАНИЕМ: {len(variants)}")
    for _, c in sorted(variants, key=lambda x: -sum(x[1].values()))[:20]:
        print("  " + " | ".join(f"«{n}» ×{k}" for n, k in c.most_common()))

    # ---------- дубли ----------
    dup = collections.defaultdict(list)
    for r in rows:
        if r["title"]:
            dup[(loose(r["artist"]), loose(r["title"]))].append(r)
    dups = [rs for rs in dup.values() if len(rs) > 1]
    section(f"ВОЗМОЖНЫЕ ДУБЛИ (тот же исполнитель + название): {len(dups)}")
    for rs in dups[:15]:
        print(f"  {rs[0]['artist']} — {rs[0]['title']}: " + " | ".join(f"{r['folder']}/{r['file']}" for r in rs))

    if broken:
        section(f"ПРОБЛЕМНЫЕ ФАЙЛЫ: {len(broken)}")
        for path, why in broken[:20]:
            print(f"  {why}: {os.path.relpath(path, MUSIC_DIR)}")

    # ---------- CSV ----------
    try:
        with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["folder", "file"] + KEYS + ["year", "cover"], delimiter=";")
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (norm(r["album"]), r["tracknumber"], r["file"])))
        print(f"\nПолная таблица: {OUT}")
    except OSError as e:
        print(f"\nCSV не сохранён: {e}")


if __name__ == "__main__":
    main()
