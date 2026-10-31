"""Films et séries (donc animations) : lus dans les tags Matroska que Metadata.py a écrits.

Un film bien étiqueté se suffit : titre, date de sortie, synopsis, genres, casting, saga, identifiant TMDB. Un fichier qui ne l'est pas garde son nom (« Inception (2010) ») : il apparaît quand même, sans synopsis. La lecture se fait en Python pur (ebml.py) ; ffprobe n'est appelé qu'à la lecture d'un film, pour savoir comment le jouer (voir media/probe.py).
"""

from __future__ import annotations

import json
import re

from libraries.video import ebml, naming

from ..db import make_id
from .common import entry_row, folder_image, item_row, unchanged

SKIPPED_PREFIXES = ("__", "#", "@", ".")
MAX_DEPTH = 3
NAME_JUNK = re.compile(r"[._]+")


def _first(tags, level, name):
    return next((value for lvl, key, value in tags if lvl == level and key == name and value), "")


def _all(tags, level, name):
    return [value for lvl, key, value in tags if lvl == level and key == name and value]


def _number(text):
    try:
        value = float(str(text).replace(",", "."))
    except ValueError:
        return None
    return int(value) if value == int(value) else value


def _split_actor(text):
    """'Tom Hanks (Forrest)' -> ('Tom Hanks', 'Forrest')."""
    found = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", text)
    return (found.group(1), found.group(2)) if found else (text, "")


def read_file(path):
    """(infos tirées des tags, durée en secondes) d'un fichier vidéo. Un fichier qui n'est pas un .mkv, ou qui ne se lit pas, n'a pas de tags."""
    if path.suffix.lower() != ".mkv":
        return {}, 0
    tags, minutes = ebml.read_metadata(path, ordered=True)
    if not tags:
        return {}, (minutes or 0) * 60
    info = {
        "title": _first(tags, 50, "TITLE"),
        "synopsis": _first(tags, 50, "SYNOPSIS") or _first(tags, 50, "SUMMARY"),
        "date": _first(tags, 50, "DATE_RELEASED"),
        "tmdb": _first(tags, 50, "TMDB"),
        "genres": [g.strip() for g in _first(tags, 50, "GENRE").split(",") if g.strip()],
        "directors": _all(tags, 50, "DIRECTOR"),
        "writers": _all(tags, 50, "WRITTEN_BY"),
        "actors": [dict(zip(("name", "role"), _split_actor(a))) for a in _all(tags, 50, "ACTOR")],
        "number": _number(_first(tags, 50, "PART_NUMBER")),
        "collection": _first(tags, 70, "TITLE"),
        "collection_part": _number(_first(tags, 70, "PART_NUMBER")),
        "season": _number(_first(tags, 60, "PART_NUMBER")),
        "season_title": _first(tags, 60, "TITLE"),
        "has_tags": True,
    }
    return {k: v for k, v in info.items() if v not in ("", None, [])}, (minutes or 0) * 60


def _stat(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, int(st.st_mtime)


class _Counts:
    def __init__(self, reused, read):
        self.reused, self.read = reused, read


class Reader:
    """Lit un fichier vidéo, ou reprend ce que le scan précédent en avait lu s'il n'a pas bougé."""

    def __init__(self, lib, known):
        self.lib, self.known, self.reused, self.read = lib, known, 0, 0

    def __call__(self, path, rel):
        stat = _stat(path)
        if stat is None:
            return None
        size, mtime = stat
        row = self.known.get(rel)
        if unchanged(row, size, mtime):
            meta = json.loads(row["meta"])
            if "info" in meta:
                self.reused += 1
                return size, mtime, meta["info"], row["duration"]
        self.read += 1
        info, duration = read_file(path)
        return size, mtime, info, duration


def clean_name(name):
    """Nom de dossier -> titre : sans l'identifiant TMDB épinglé."""
    _, name = naming.extract_tmdb_id(name)
    return name.strip() or name


# ---------------------------------------------------------------------------
# Films
# ---------------------------------------------------------------------------
def scan_movies(lib, known, enriched=None):
    reader = Reader(lib.id, known)
    items, entries = [], []
    for movie in naming.find_movies(lib.path):
        parts = []
        for path in movie.files:
            rel = path.relative_to(lib.path).as_posix()
            result = reader(path, rel)
            if result:
                parts.append((path, rel, *result))
        if not parts:
            continue
        info = parts[0][4]
        guessed, guessed_year, order = naming.parse_title_year(naming.extract_tmdb_id(movie.rawname)[1])
        title = info.get("title") or guessed or movie.rawname
        year = (info.get("date") or "")[:4] or guessed_year or ""
        key = parts[0][1] if len(parts) == 1 else movie.folder.relative_to(lib.path).as_posix() + "/" + parts[0][1]
        folder = movie.folder if movie.owns_folder else None
        image = folder_image(folder) if folder else ""
        meta = {
            "tmdb": info.get("tmdb", ""), "directors": info.get("directors", []), "writers": info.get("writers", []),
            "actors": info.get("actors", []), "date": info.get("date", ""), "part": info.get("collection_part") if info.get("collection_part") is not None else order,
            "image": (movie.folder.relative_to(lib.path) / image).as_posix() if image else "", "tagged": bool(info.get("has_tags")),
            "folder": movie.folder.relative_to(lib.path).as_posix() if movie.folder != lib.path else "",
        }
        meta["duration"] = sum(p[5] for p in parts)
        item = item_row(lib.id, key, "movie", title, year=year, summary=info.get("synopsis", ""), genres=info.get("genres", []),
                        grp=info.get("collection", ""), meta=meta, added=max(p[3] for p in parts), count=len(parts))
        items.append(item)
        for n, (path, rel, size, mtime, pinfo, duration) in enumerate(parts, 1):
            entries.append(entry_row(lib.id, item["id"], rel, "video", title=title if len(parts) == 1 else f"{title} - partie {n}", number=n,
                                     duration=duration, size=size, mtime=mtime, meta={"info": pinfo}))
    return items, entries, _Counts(reader.reused, reader.read)


# ---------------------------------------------------------------------------
# Séries
# ---------------------------------------------------------------------------
def _videos(folder):
    try:
        return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in naming.VIDEO_EXTS and not naming.EXTRA_NAME_RE.search(p.stem)), key=lambda p: p.name.casefold())
    except OSError:
        return []


def _subdirs(folder):
    try:
        return sorted((p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(SKIPPED_PREFIXES) and not naming.SKIP_DIR_RE.match(p.name)), key=lambda p: p.name.casefold())
    except OSError:
        return []


def find_shows(root, folder=None, container="", depth=0):
    """[(dossier de la série, nom du dossier qui la range)] : un dossier est une série s'il contient des saisons ou des vidéos ; sinon il range (« _Marvel »)."""
    folder = folder or root
    if folder != root and (naming.find_seasons(folder) or _videos(folder)):
        return [(folder, container)]
    if depth >= MAX_DEPTH:
        return []
    found = []
    for sub in _subdirs(folder):
        found += find_shows(root, sub, container if folder == root else (container or folder.name), depth + 1)
    return found


def episode_title(path):
    stem = NAME_JUNK.sub(" ", path.stem).strip()
    return re.sub(r"^\s*(?:s\d{1,2}\s*e\d{1,3}|\d{1,3})\s*[-–.]?\s*", "", stem, flags=re.I).strip() or stem


def scan_shows(lib, known, enriched=None):
    reader = Reader(lib.id, known)
    items, entries = [], []
    enriched = enriched or {}
    for folder, container in find_shows(lib.path):
        seasons = naming.find_seasons(folder) or [(folder, None)]
        found = []          # (saison, chemin, rel, size, mtime, info, durée)
        for season_dir, number in seasons:
            for path in _videos(season_dir):
                rel = path.relative_to(lib.path).as_posix()
                result = reader(path, rel)
                if not result:
                    continue
                size, mtime, info, duration = result
                season = number
                if season is None:             # épisodes posés à plat dans le dossier de la série
                    guess = naming.P_SE.search(path.name)
                    season = int(guess.group(1)) if guess else int(info.get("season") or 1)
                found.append((season, path, rel, size, mtime, info, duration))
        if not found:
            continue
        rel_dir = folder.relative_to(lib.path).as_posix()
        pinned, name = naming.extract_tmdb_id(folder.name)
        first = next((f[5] for f in found if f[5].get("collection")), {})
        title = first.get("collection") or name
        years = sorted((f[5].get("date") or "")[:4] for f in found if f[5].get("date"))
        item_id = make_id(lib.id, rel_dir)
        extra = enriched.get(item_id, {})
        image = folder_image(folder)
        season_images = {}
        for season_dir, number in seasons:
            if number is not None and season_dir != folder:
                img = folder_image(season_dir)
                if img:
                    season_images[str(number)] = (season_dir.relative_to(lib.path) / img).as_posix()
        meta = {"tmdb_pinned": pinned or "", "image": (folder.relative_to(lib.path) / image).as_posix() if image else "", "seasons": sorted({f[0] for f in found}),
                "season_images": season_images, "folder": rel_dir, "tagged": any(f[5].get("has_tags") for f in found), **{k: v for k, v in extra.items() if k not in ("summary", "genres", "year", "title")}}
        item = item_row(lib.id, rel_dir, "show", extra.get("title") or title, year=extra.get("year") or (years[0] if years else ""), summary=extra.get("summary", ""),
                        genres=extra.get("genres", []), grp=container, meta=meta, added=max(f[4] for f in found), count=len(found))
        items.append(item)
        for season, path, rel, size, mtime, info, duration in sorted(found, key=lambda f: (f[0], f[5].get("number") or naming.detect_episode_number(f[1].name) or 9999, f[1].name.casefold())):
            number = info.get("number")
            if number is None:
                number = naming.detect_episode_number(path.name)
            entries.append(entry_row(lib.id, item["id"], rel, "video", title=info.get("title") or episode_title(path), summary=info.get("synopsis", ""), season=season, number=number,
                                     duration=duration, size=size, mtime=mtime, meta={"info": info}))
    return items, entries, _Counts(reader.reused, reader.read)
