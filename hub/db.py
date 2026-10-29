"""La base SQLite : ce que les scans ont trouvé, et où en est chacun de sa lecture.

Un fichier unique dans HUB_DATA. Les scans y écrivent par bibliothèque, en une transaction : le site ne voit jamais une bibliothèque à moitié remplie. Une connexion par fil d'exécution (sqlite3 l'exige), en mode WAL pour que les lectures du site ne bloquent pas un scan.

Ce qui n'est pas ici : les fichiers eux-mêmes (jamais copiés), et les images, qui vivent dans le cache disque (voir images.py).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
    id      TEXT PRIMARY KEY,
    lib     TEXT NOT NULL,
    type    TEXT NOT NULL,            -- movie | show | album | series
    title   TEXT NOT NULL,
    sort    TEXT NOT NULL,
    letter  TEXT NOT NULL DEFAULT '#',
    year    TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    genres  TEXT NOT NULL DEFAULT '[]',
    grp     TEXT NOT NULL DEFAULT '', -- saga, artiste, section : ce qui regroupe l'item avec ses voisins
    meta    TEXT NOT NULL DEFAULT '{}',
    added   INTEGER NOT NULL DEFAULT 0,
    count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_lib ON items(lib, sort);
CREATE TABLE IF NOT EXISTS entries(
    id       TEXT PRIMARY KEY,
    item_id  TEXT NOT NULL,
    lib      TEXT NOT NULL,
    rel      TEXT NOT NULL,           -- chemin relatif à la bibliothèque, séparé par "/"
    kind     TEXT NOT NULL,           -- video | audio | book
    season   INTEGER,
    disc     INTEGER,
    number   REAL,
    title    TEXT NOT NULL DEFAULT '',
    summary  TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0, -- secondes
    size     INTEGER NOT NULL DEFAULT 0,
    mtime    INTEGER NOT NULL DEFAULT 0,
    meta     TEXT NOT NULL DEFAULT '{}',
    probe    TEXT                     -- sortie de ffprobe, gardée à la première lecture
);
CREATE INDEX IF NOT EXISTS entries_item ON entries(item_id);
CREATE INDEX IF NOT EXISTS entries_lib ON entries(lib);
CREATE TABLE IF NOT EXISTS progress(
    profile  TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    position REAL NOT NULL DEFAULT 0, -- secondes (vidéo, audio) ou page / chapitre (livres)
    duration REAL NOT NULL DEFAULT 0,
    done     INTEGER NOT NULL DEFAULT 0,
    updated  INTEGER NOT NULL,
    PRIMARY KEY(profile, entry_id)
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS playlists(
    id      TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    name    TEXT NOT NULL,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks(
    playlist_id TEXT NOT NULL,
    pos         INTEGER NOT NULL,       -- 0, 1, 2... sans trou
    entry_id    TEXT NOT NULL,          -- un titre peut y figurer deux fois ; il survit aux scans (l'identifiant vient du chemin)
    PRIMARY KEY(playlist_id, pos)
);
"""


def make_id(*parts):
    """Identifiant stable : il ne change que si le chemin change."""
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:16]


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.Lock()
        with self.write_lock:
            self.connection().executescript(SCHEMA)
            self.connection().execute("INSERT OR IGNORE INTO kv(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),))
            self.connection().commit()

    def connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            self._local.conn = conn
        return conn

    def query(self, sql, params=()):
        return self.connection().execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.connection().execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        with self.write_lock:
            conn = self.connection()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def kv_get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    # --- Scans ---------------------------------------------------------------
    def known_entries(self, lib):
        """{rel: Row} des entrées d'une bibliothèque : de quoi ne relire que les fichiers qui ont bougé."""
        return {row["rel"]: row for row in self.query("SELECT * FROM entries WHERE lib=?", (lib,))}

    def known_items(self, lib):
        return {row["id"]: row for row in self.query("SELECT * FROM items WHERE lib=?", (lib,))}

    def replace_library(self, lib, items, entries):
        """Remplace le contenu d'une bibliothèque en une transaction. Les sondes ffprobe déjà faites sont gardées si le fichier n'a pas bougé."""
        with self.write_lock:
            conn = self.connection()
            try:
                old_probes = {r["id"]: (r["size"], r["mtime"], r["probe"]) for r in conn.execute("SELECT id, size, mtime, probe FROM entries WHERE lib=? AND probe IS NOT NULL", (lib,))}
                conn.execute("DELETE FROM items WHERE lib=?", (lib,))
                conn.execute("DELETE FROM entries WHERE lib=?", (lib,))
                conn.executemany(
                    "INSERT INTO items(id, lib, type, title, sort, letter, year, summary, genres, grp, meta, added, count) "
                    "VALUES(:id, :lib, :type, :title, :sort, :letter, :year, :summary, :genres, :grp, :meta, :added, :count)", items)
                for e in entries:
                    kept = old_probes.get(e["id"])
                    e["probe"] = kept[2] if kept and kept[0] == e["size"] and kept[1] == e["mtime"] else None
                conn.executemany(
                    "INSERT INTO entries(id, item_id, lib, rel, kind, season, disc, number, title, summary, duration, size, mtime, meta, probe) "
                    "VALUES(:id, :item_id, :lib, :rel, :kind, :season, :disc, :number, :title, :summary, :duration, :size, :mtime, :meta, :probe)", entries)
                conn.execute("DELETE FROM progress WHERE entry_id NOT IN (SELECT id FROM entries)")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def set_probe(self, entry_id, probe):
        self.execute("UPDATE entries SET probe=? WHERE id=?", (json.dumps(probe), entry_id))

    def update_item_meta(self, item_id, **fields):
        """Complète un item (enrichissement TMDB) : `meta` est fusionné, le reste remplace."""
        row = self.one("SELECT meta FROM items WHERE id=?", (item_id,))
        if row is None:
            return
        sets, params = [], []
        for key, value in fields.items():
            if key == "meta":
                merged = {**json.loads(row["meta"]), **value}
                sets.append("meta=?")
                params.append(json.dumps(merged, ensure_ascii=False))
            elif key == "genres":
                sets.append("genres=?")
                params.append(json.dumps(value, ensure_ascii=False))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        if sets:
            self.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*params, item_id))

    # --- Progression ---------------------------------------------------------
    def save_progress(self, profile, entry_id, position, duration, done):
        self.execute(
            "INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(profile, entry_id) DO UPDATE SET position=excluded.position, duration=excluded.duration, done=excluded.done, updated=excluded.updated",
            (profile, entry_id, position, duration, 1 if done else 0, int(time.time())))

    def progress_for(self, profile, entry_ids):
        ids = list(entry_ids)
        found = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.query(f"SELECT * FROM progress WHERE profile=? AND entry_id IN ({marks})", (profile, *chunk)):
                found[row["entry_id"]] = row
        return found
