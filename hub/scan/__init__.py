"""Scan des bibliothèques : lit les dossiers, range le résultat dans la base.

Un scan est incrémental : un fichier dont la taille et la date n'ont pas bougé n'est pas relu. Il s'écrit en une transaction par bibliothèque (voir Database.replace_library).
"""

from __future__ import annotations

import json
import time

from .. import labels
from . import books, music, video

SCANNERS = {"movies": video.scan_movies, "shows": video.scan_shows, "music": music.scan_music, "books": books.scan_books}


def enriched_for(db, lib_id):
    """{id d'item: données TMDB} gardées d'un enrichissement précédent : un scan ne les efface pas."""
    prefix = f"enrich:{lib_id}:"
    return {row["key"][len(prefix):]: json.loads(row["value"]) for row in db.query("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",))}


def scan_library(db, lib, log=print):
    """Scanne une bibliothèque. Retourne un dictionnaire de bilan."""
    started = time.time()
    if not lib.path.is_dir():
        log(f"[{labels.shown(db, lib)}] dossier introuvable : {lib.path}")
        return {"lib": lib.id, "error": "dossier introuvable", "items": 0, "entries": 0}
    known = db.known_entries(lib.id)
    items, entries, counts = SCANNERS[lib.kind](lib, known, enriched_for(db, lib.id))
    if not items and known:
        log(f"[{labels.shown(db, lib)}] ATTENTION : plus aucun élément alors que la bibliothèque en avait {len(known)} : volume démonté ? La progression de lecture est gardée.")
    db.replace_library(lib.id, items, entries)
    for key, value in getattr(counts, "kv", {}).items():
        db.kv_set(key, value)
    seconds = round(time.time() - started, 1)
    log(f"[{labels.shown(db, lib)}] {len(items)} élément(s), {len(entries)} fichier(s) ({counts.read} lu(s), {counts.reused} repris) en {seconds} s")
    db.kv_set(f"scanned:{lib.id}", int(time.time()))
    return {"lib": lib.id, "items": len(items), "entries": len(entries), "read": counts.read, "reused": counts.reused, "seconds": seconds}
