"""Ce que tous les scanners partagent : la forme d'un item et d'une entrée, et le tri des titres."""

from __future__ import annotations

import json
import os
import re
import unicodedata

from libraries.common.text import fold, initial
from libraries.music import coverfile

from ..db import make_id

STACK_MAX = 3          # couvertures empilées sur la carte d'une saga ou d'une série de livres : la première, puis celles des tomes suivants

DIGITS = re.compile(r"\d+")
JOINERS = "\u200c\u200d"          # liant sans chasse, liant de chasse nulle : visibles dans l'écriture de certaines langues (persan, hindi, émojis)


def invisible(c):
    return unicodedata.category(c) == "Cf"


def sort_key(title):
    """Titre pour classer : sans accents ni casse, les nombres comptés comme tels ("Rocky 2" avant "Rocky 10"), et sans caractère invisible, quel qu'il soit."""
    return DIGITS.sub(lambda m: m.group().zfill(6), "".join(c for c in fold(title) if not invisible(c)))


def clean_text(text):
    """Le texte d'une étiquette sans ses caractères invisibles de mise en forme (marque de sens de lecture, espace de largeur nulle, BOM, trait d'union conditionnel), ni les espaces aux bords : on ne les voit pas,
    mais ils rangent un titre après le Z. Tout ce qui se voit est gardé, et les deux liants (U+200C, U+200D) aussi : ils font partie de l'écriture de certaines langues."""
    return "".join(c for c in text or "" if not invisible(c) or c in JOINERS).strip()


def item_row(lib, key, kind, title, *, year="", summary="", genres=(), grp="", meta=None, added=0, count=0):
    title = clean_text(title)
    return {
        "id": make_id(lib, key), "lib": lib, "type": kind, "title": title, "sort": sort_key(title), "letter": initial(title),
        "year": str(year or ""), "summary": summary or "", "genres": json.dumps(list(genres), ensure_ascii=False), "grp": clean_text(grp),
        "meta": json.dumps(meta or {}, ensure_ascii=False), "added": int(added or 0), "count": int(count or 0),
    }


def entry_id(lib, rel):
    return make_id(lib, rel)


def entry_row(lib, item_id, rel, kind, *, title="", summary="", season=None, disc=None, number=None, duration=0, size=0, mtime=0, meta=None):
    return {
        "id": entry_id(lib, rel), "item_id": item_id, "lib": lib, "rel": rel, "kind": kind, "season": season, "disc": disc,
        "number": number, "title": clean_text(title), "summary": summary or "", "duration": float(duration or 0), "size": int(size or 0),
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
