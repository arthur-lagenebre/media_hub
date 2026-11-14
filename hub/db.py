"""La base SQLite : ce que les scans ont trouvé, et où en est chacun de sa lecture.

Un fichier unique dans HUB_DATA. Les scans y écrivent par bibliothèque, en une transaction : le site ne voit jamais une bibliothèque à moitié remplie. Une connexion par fil d'exécution (sqlite3 l'exige), en mode WAL pour que les lectures du site ne bloquent pas un scan.

Ce qui n'est pas ici : les fichiers eux-mêmes (jamais copiés), et les images, qui vivent dans le cache disque (voir images.py).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from functools import partial
from pathlib import Path

ITEM_COLUMNS = {"title", "sort", "letter", "year", "summary", "genres", "grp", "meta", "added", "count"}      # ce que update_item_meta peut changer
RATINGS = ("up", "mid", "down")      # l'avis d'un compte sur un film ou une série : J'aime, Bof, Je n'aime pas. Pas de ligne = pas d'avis ; « Bof » n'est donc pas l'absence d'avis
ORPHAN_PROGRESS_DAYS = 90      # une progression dont le fichier a disparu se garde ce temps : un volume démonté ou une bibliothèque momentanément vide ne doit pas effacer ce qu'on avait vu


def _add_column(conn, table, column, definition):
    """ALTER TABLE ... ADD COLUMN, sauf si la colonne y est déjà : une table neuve (SCHEMA) l'a d'emblée, celle d'une base plus ancienne non. Un identifiant de table ou de colonne ne se lie pas comme paramètre : ils viennent du code ci-dessous, jamais d'une donnée, et ValueError si l'un n'est pas un identifiant."""
    if not (table.isidentifier() and column.isidentifier()):
        raise ValueError(f"table ou colonne invalide : {table!r}, {column!r}")
    if column not in [r["name"] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_ratings(conn):
    """7 : la table `ratings`. Déjà là sur une base qui vient d'être créée (SCHEMA), pas sur une base d'avant : CREATE IF NOT EXISTS, rien d'existant n'est touché."""
    conn.execute(RATINGS_TABLE)


# {version: fonction(connexion)} : ce qui fait passer la base à `version` depuis la précédente. CREATE TABLE IF NOT EXISTS (SCHEMA) ajoute des tables, pas une colonne : toute autre évolution s'écrit ici, ex. `3: lambda conn: conn.execute("ALTER TABLE items ADD COLUMN x TEXT")`
# 4 : « lire » (`rights.play`) vaut 1 pour les lignes d'avant, qui n'avaient pas ce droit à retirer. 5 : le droit « Envoyer » (`users.can_upload`) vaut 0 pour les comptes d'avant ; l'administrateur envoie toujours, quelle que soit la colonne. 6 : `users.inbox_dir` (le dossier du compte dans la boîte de réception) vaut NULL pour les comptes d'avant : `Inbox` le fixe à la première utilisation, ou au démarrage (migration de l'ancien dossier nommé d'après l'identifiant).
# 7 : la table `ratings` (l'avis personnel d'un compte, voir plus bas), qui ne change aucune table existante.
# (Une virgule doit précéder chaque numéro : .github/scripts/check_version.py lit ces clés dans le texte.)
MIGRATIONS = {4: partial(_add_column, table="rights", column="play", definition="INTEGER NOT NULL DEFAULT 1"),
              5: partial(_add_column, table="users", column="can_upload", definition="INTEGER NOT NULL DEFAULT 0"),
              6: partial(_add_column, table="users", column="inbox_dir", definition="TEXT"),
              7: _create_ratings}
SCHEMA_VERSION = 7          # 7 : table `ratings` (l'avis personnel d'un compte sur un film ou une série) ; 6 : colonne `users.inbox_dir` (le dossier du compte dans la boîte de réception, fixé une fois) ; 5 : colonne `users.can_upload` (envoyer dans la boîte de réception) ; 2 : table `users` (comptes) ; 3 : table `rights` (droits par compte et par bibliothèque) ; 4 : colonne `rights.play` (lire). Une table manquante est créée au démarrage (CREATE IF NOT EXISTS), rien d'existant n'est touché

RATINGS_TABLE = f"""
CREATE TABLE IF NOT EXISTS ratings(
    user_id TEXT NOT NULL,                  -- users.id, pas une clé étrangère (comme `rights`) : un compte supprimé est nettoyé par accounts.delete
    item_id TEXT NOT NULL,                  -- items.id, qui vient du chemin : un dossier renommé perd son avis, un élément disparu laisse sa ligne (qui ne se voit plus) et la retrouve s'il revient
    rating  TEXT NOT NULL CHECK (rating IN ({", ".join(repr(r) for r in RATINGS)})),
    updated INTEGER NOT NULL,
    PRIMARY KEY(user_id, item_id)
)"""

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
CREATE TABLE IF NOT EXISTS users(
    id            TEXT PRIMARY KEY,         -- « default » pour le premier compte (il garde la progression et les playlists d'avant les comptes), aléatoire ensuite ; c'est le `profile` de progress / playlists
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pass_hash     TEXT NOT NULL,            -- voir accounts.py (scrypt ou pbkdf2, un sel par compte)
    role          TEXT NOT NULL DEFAULT 'user',   -- admin | user
    token_version INTEGER NOT NULL DEFAULT 1,     -- change à chaque nouveau mot de passe : les jetons de session de ce compte cessent de valoir
    created       INTEGER NOT NULL,
    can_upload    INTEGER NOT NULL DEFAULT 0,     -- le droit « Envoyer » (boîte de réception) ; l'administrateur envoie toujours, sans tenir compte de cette colonne
    inbox_dir     TEXT                            -- le nom du dossier du compte sous la boîte de réception, fixé à la première utilisation (voir inbox.py) ; NULL tant que le compte n'a rien envoyé. Un renommage ou une suppression de compte ne le change pas
);
CREATE TABLE IF NOT EXISTS rights(
    user_id  TEXT NOT NULL,                 -- users.id
    lib      TEXT NOT NULL,                 -- identifiant de bibliothèque (config), pas une clé étrangère : une bibliothèque peut être retirée puis remise
    view     INTEGER NOT NULL DEFAULT 1,    -- voir la bibliothèque (liste, fiches, images)
    play     INTEGER NOT NULL DEFAULT 1,    -- la lire en ligne (vidéo, musique, livres) ; sans effet quand view est à 0
    download INTEGER NOT NULL DEFAULT 1,    -- télécharger ses fichiers ; sans effet quand view est à 0
    PRIMARY KEY(user_id, lib)
);                                          -- pas de ligne = tout autorisé : les comptes d'avant les droits n'ont rien perdu, et une bibliothèque ajoutée s'ouvre à tous
""" + RATINGS_TABLE + ";"


def make_id(*parts):
    """Identifiant stable : il ne change que si le chemin change."""
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()[:16]


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.Lock()
        self.items_version = 0                  # monte à chaque écriture de `items` (scan, enrichissement) : voir `derived`
        self.probes_version = 0                 # monte à chaque sonde ffprobe gardée (set_probe) : seul ce qui lit les sondes en dépend
        self._derived, self._derived_lock = {}, threading.Lock()
        with self.write_lock:
            conn = self.connection()
            conn.executescript(SCHEMA)
            self._upgrade(conn)

    def items_changed(self):
        with self._derived_lock:
            self.items_version += 1

    def derived(self, name, build, probes=False):
        """Ce que `build()` tire de la table `items`, gardé en mémoire sous `name` jusqu'à la prochaine écriture des items (scan, enrichissement). Un seul processus écrit dans la base : le compteur suffit.
        `probes` : `build()` lit aussi les sondes ffprobe ; une nouvelle sonde le renouvelle, sans toucher à ce qui ne lit que les items (l'analyse des vidéos en garde une par fichier).
        La version est lue *avant* de construire : une écriture survenue pendant la construction rend la valeur gardée périmée dès l'appel suivant, jamais l'inverse."""
        with self._derived_lock:
            version = (self.items_version, self.probes_version) if probes else (self.items_version,)          # les compteurs ne font que monter : l'ordre des versions suit celui des écritures
            hit = self._derived.get(name)
            if hit is not None and hit[0] == version:
                return hit[1]
        value = build()
        with self._derived_lock:
            hit = self._derived.get(name)
            if hit is None or hit[0] < version:
                self._derived[name] = (version, value)
        return value

    def _upgrade(self, conn):
        """Amène la base à SCHEMA_VERSION, une version à la fois, chacune en une transaction : une migration qui échoue laisse la base telle qu'elle était, et le hub ne démarre pas.

        Une base plus récente que ce hub (on est revenu à une ancienne version) n'est pas touchée : le numéro gardé reste le sien, et on le dit.
        """
        row = conn.execute("SELECT value FROM kv WHERE key='schema'").fetchone()
        try:
            stored = int(json.loads(row["value"])) if row else SCHEMA_VERSION         # sans numéro : base neuve, SCHEMA vient de la créer
        except (ValueError, TypeError):
            stored = 0
        if stored > SCHEMA_VERSION:
            print(f"ATTENTION : la base est en version {stored}, ce hub n'en connaît que {SCHEMA_VERSION} : mets le hub à jour (la base n'est pas modifiée).", file=sys.stderr)
            return
        for version in range(stored + 1, SCHEMA_VERSION + 1):
            migrate = MIGRATIONS.get(version)
            try:
                conn.execute("BEGIN IMMEDIATE")
                if migrate:
                    migrate(conn)
                conn.execute("INSERT INTO kv(key, value) VALUES('schema', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"la migration de la base vers la version {version} a échoué ({e}) : la base est restée en version {version - 1}") from e
        if not row:
            conn.execute("INSERT INTO kv(key, value) VALUES('schema', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
            conn.commit()

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

    def kv_delete(self, key):
        self.execute("DELETE FROM kv WHERE key=?", (key,))

    # --- Scans ---------------------------------------------------------------
    def known_entries(self, lib):
        """{rel: Row} des entrées d'une bibliothèque : de quoi ne relire que les fichiers qui ont bougé."""
        return {row["rel"]: row for row in self.query("SELECT * FROM entries WHERE lib=?", (lib,))}

    def known_items(self, lib):
        return {row["id"]: row for row in self.query("SELECT * FROM items WHERE lib=?", (lib,))}

    def replace_library(self, lib, items, entries):
        """Remplace le contenu d'une bibliothèque en une transaction. Les sondes ffprobe déjà faites sont gardées si le fichier n'a pas bougé.

        La progression n'est pas touchée (elle suit l'identifiant de l'entrée, qui vient du chemin : le fichier qui revient la retrouve) ; seules celles de fichiers disparus depuis plus de ORPHAN_PROGRESS_DAYS jours sont retirées. Toutes les lectures passent par une jointure sur `entries` : une ligne sans fichier ne se voit pas.
        """
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
                conn.execute("DELETE FROM progress WHERE updated < ? AND entry_id NOT IN (SELECT id FROM entries)", (int(time.time()) - ORPHAN_PROGRESS_DAYS * 86400,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            self.items_changed()

    def set_probe(self, entry_id, probe):
        self.execute("UPDATE entries SET probe=? WHERE id=?", (json.dumps(probe), entry_id))
        with self._derived_lock:
            self.probes_version += 1

    def update_item_meta(self, item_id, **fields):
        """Complète un item (enrichissement TMDB) : `meta` est fusionné, le reste remplace. Seules les colonnes de ITEM_COLUMNS : leur nom entre dans la requête, il ne vient jamais d'ailleurs."""
        unknown = set(fields) - ITEM_COLUMNS
        if unknown:
            raise ValueError(f"colonne inconnue : {', '.join(sorted(unknown))}")
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
            self.items_changed()

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

    # --- Avis personnel ------------------------------------------------------
    def set_rating(self, user_id, item_id, rating):
        """Pose l'avis (`up`, `mid` ou `down`) d'un compte sur un élément, ou l'efface (`None`). Rien d'autre qu'une de ces trois valeurs : la base la refuse aussi (CHECK)."""
        if rating is None:
            self.execute("DELETE FROM ratings WHERE user_id=? AND item_id=?", (user_id, item_id))
            return
        if rating not in RATINGS:
            raise ValueError(f"avis inconnu : {rating!r}")
        self.execute(
            "INSERT INTO ratings(user_id, item_id, rating, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, item_id) DO UPDATE SET rating=excluded.rating, updated=excluded.updated",
            (user_id, item_id, rating, int(time.time())))

    def ratings_for(self, user_id):
        """{id d'élément: avis} de ce compte, en une lecture : les avis d'un autre compte ne se lisent nulle part."""
        return {r["item_id"]: r["rating"] for r in self.query("SELECT item_id, rating FROM ratings WHERE user_id=?", (user_id,))}
