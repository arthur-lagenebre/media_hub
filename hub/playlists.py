"""Les playlists : des listes de pistes que l'on compose soi-même, gardées dans la base (elles survivent aux mises à jour).

Une playlist ne retient que des identifiants de pistes. Si un fichier disparaît ou change de nom, sa piste n'apparaît plus dans la liste ; elle revient d'elle-même si le fichier revient.
"""

from __future__ import annotations

import json
import secrets
import time

from .db import Database

MAX_NAME = 80
MAX_TRACKS = 5000


class PlaylistError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def clean_name(name):
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name:
        raise PlaylistError(400, "donne un nom à la playlist")
    return name


def _order(db: Database, pid):
    return [r["entry_id"] for r in db.query("SELECT entry_id FROM playlist_tracks WHERE playlist_id=? ORDER BY pos", (pid,))]


def _write(db: Database, pid, entry_ids):
    with db.write_lock:
        conn = db.connection()
        try:
            conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
            conn.executemany("INSERT INTO playlist_tracks(playlist_id, pos, entry_id) VALUES(?,?,?)", [(pid, n, e) for n, e in enumerate(entry_ids)])
            conn.execute("UPDATE playlists SET updated=? WHERE id=?", (int(time.time()), pid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def audio_entries(db: Database, entry_ids):
    """{id: ligne} des pistes audio qui existent parmi `entry_ids`."""
    ids = list(dict.fromkeys(entry_ids))
    found = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for row in db.query(f"SELECT e.*, i.title AS album, i.id AS album_id FROM entries e JOIN items i ON i.id = e.item_id WHERE e.kind='audio' AND e.id IN ({marks})", tuple(chunk)):
            found[row["id"]] = row
    return found


def track_json(row):
    info = (json.loads(row["meta"] or "{}").get("info")) or {}
    return {"id": row["id"], "title": row["title"], "artist": info.get("artist", ""), "album": row["album"], "item": row["album_id"], "duration": row["duration"], "number": row["number"]}


def exists(db: Database, profile, pid):
    row = db.one("SELECT * FROM playlists WHERE id=? AND profile=?", (pid, profile))
    if row is None:
        raise PlaylistError(404, "playlist inconnue")
    return row


def summary(db: Database, row):
    entries = audio_entries(db, _order(db, row["id"]))
    ordered = [entries[e] for e in _order(db, row["id"]) if e in entries]
    covers = list(dict.fromkeys(r["album_id"] for r in ordered))[:4]
    return {"id": row["id"], "name": row["name"], "count": len(ordered), "duration": sum(r["duration"] for r in ordered), "covers": covers, "updated": row["updated"]}


def listing(db: Database, profile):
    rows = db.query("SELECT * FROM playlists WHERE profile=? ORDER BY name COLLATE NOCASE", (profile,))
    return [summary(db, r) for r in rows]


def detail(db: Database, profile, pid):
    row = exists(db, profile, pid)
    order = _order(db, pid)
    entries = audio_entries(db, order)
    tracks = [track_json(entries[e]) for e in order if e in entries]
    out = summary(db, row)
    out["tracks"] = tracks
    out["missing"] = len(order) - len(tracks)
    return out


def create(db: Database, profile, name, entry_ids=()):
    """Crée une playlist ; si une playlist de ce nom existe déjà, on la reprend (taper deux fois le même nom ne fait pas de doublon)."""
    name = clean_name(name)
    same = next((r for r in db.query("SELECT * FROM playlists WHERE profile=?", (profile,)) if r["name"].casefold() == name.casefold()), None)
    if same is None:
        pid = secrets.token_hex(8)
        now = int(time.time())
        db.execute("INSERT INTO playlists(id, profile, name, created, updated) VALUES(?,?,?,?,?)", (pid, profile, name, now, now))
    else:
        pid = same["id"]
    if entry_ids:
        add(db, profile, pid, entry_ids)
    return pid


def add(db: Database, profile, pid, entry_ids):
    exists(db, profile, pid)
    wanted = [e for e in entry_ids if isinstance(e, str)]
    known = audio_entries(db, wanted)
    fresh = [e for e in wanted if e in known]
    if not fresh:
        raise PlaylistError(400, "aucune piste valide à ajouter")
    order = _order(db, pid)
    if len(order) + len(fresh) > MAX_TRACKS:
        raise PlaylistError(400, f"une playlist ne peut pas dépasser {MAX_TRACKS} pistes")
    _write(db, pid, order + fresh)
    return len(fresh)


def remove(db: Database, profile, pid, index):
    exists(db, profile, pid)
    order = _order(db, pid)
    known = audio_entries(db, order)
    present = [n for n, e in enumerate(order) if e in known]     # l'index vient de la liste affichée, sans les pistes disparues
    if not isinstance(index, int) or not 0 <= index < len(present):
        raise PlaylistError(400, "position invalide")
    del order[present[index]]
    _write(db, pid, order)


def move(db: Database, profile, pid, src, dst):
    exists(db, profile, pid)
    order = _order(db, pid)
    known = audio_entries(db, order)
    present = [n for n, e in enumerate(order) if e in known]
    if not all(isinstance(x, int) and 0 <= x < len(present) for x in (src, dst)):
        raise PlaylistError(400, "position invalide")
    visible = [order[n] for n in present]
    visible.insert(dst, visible.pop(src))
    hidden = [e for e in order if e not in known]
    _write(db, pid, visible + hidden)


def rename(db: Database, profile, pid, name):
    exists(db, profile, pid)
    db.execute("UPDATE playlists SET name=?, updated=? WHERE id=?", (clean_name(name), int(time.time()), pid))


def delete(db: Database, profile, pid):
    exists(db, profile, pid)
    with db.write_lock:
        conn = db.connection()
        conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (pid,))
        conn.execute("DELETE FROM playlists WHERE id=?", (pid,))
        conn.commit()
