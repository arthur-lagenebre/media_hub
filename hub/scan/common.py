"""Ce que tous les scanners partagent : la forme d'un item et d'une entrée, et le tri des titres."""

from __future__ import annotations

import json
import os
import re

from libraries.common.text import fold, initial
from libraries.music import coverfile

from ..db import make_id

DIGITS = re.compile(r"\d+")


def sort_key(title):
    """Titre pour classer : sans accents ni casse, les nombres comptés comme tels ("Rocky 2" avant "Rocky 10")."""
    return DIGITS.sub(lambda m: m.group().zfill(6), fold(title))


def item_row(lib, key, kind, title, *, year="", summary="", genres=(), grp="", meta=None, added=0, count=0):
    return {
        "id": make_id(lib, key), "lib": lib, "type": kind, "title": title, "sort": sort_key(title), "letter": initial(title),
        "year": str(year or ""), "summary": summary or "", "genres": json.dumps(list(genres), ensure_ascii=False), "grp": grp or "",
        "meta": json.dumps(meta or {}, ensure_ascii=False), "added": int(added or 0), "count": int(count or 0),
    }


def entry_row(lib, item_id, rel, kind, *, title="", summary="", season=None, disc=None, number=None, duration=0, size=0, mtime=0, meta=None):
    return {
        "id": make_id(lib, rel), "item_id": item_id, "lib": lib, "rel": rel, "kind": kind, "season": season, "disc": disc,
        "number": number, "title": title or "", "summary": summary or "", "duration": float(duration or 0), "size": int(size or 0),
        "mtime": int(mtime or 0), "meta": json.dumps(meta or {}, ensure_ascii=False), "probe": None,
    }


def unchanged(row, size, mtime):
    """Le fichier est-il tel que le dernier scan l'a lu ?"""
    return row is not None and row["size"] == size and row["mtime"] == mtime


IMAGE_NAMES = ("poster", "folder", "cover", "front", "thumb")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def is_album_cover(name):
    """La seule pochette d'un album : cover.jpg, cover.png... (la casse ne compte pas). Ni folder.jpg, ni la pochette embarquée dans les pistes."""
    stem, ext = os.path.splitext(name.casefold())
    return stem == "cover" and ext in coverfile.EXTS


def folder_image(folder):
    """Nom de l'image posée dans le dossier (folder.jpg, poster.png...), ou ""."""
    try:
        names = {p.name.casefold(): p.name for p in folder.iterdir() if p.is_file()}
    except OSError:
        return ""
    for base in IMAGE_NAMES:
        for ext in IMAGE_EXTS:
            if base + ext in names:
                return names[base + ext]
    return ""
