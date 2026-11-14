"""Les demandes : la liste d'attente des films et des séries qu'un compte aimerait voir arriver, que l'administrateur lit.

Le hub ne télécharge rien et n'écrit pas dans la médiathèque : une demande n'est qu'une ligne que l'administrateur lit, puis qu'il marque « ajoutée » ou « refusée ». Une demande désigne un film ou une série par son identifiant TMDB (`kind` : `movie` ou `show`), une seule ligne par élément (`requests`), et chaque compte qui la fait s'y joint (`request_users`, avec son message facultatif et `ack` : il a vu qu'elle est arrivée).

Confidentialité : un compte lit ses demandes, et seulement « quelqu'un d'autre l'a demandé » pour les autres (`mine: false`), sans nom ni nombre. Seul l'administrateur voit les demandeurs et leurs messages.

L'état « ajouté » se calcule à la lecture : une demande en attente dont l'élément est dans la médiathèque (n'importe quelle bibliothèque : voir `owned`) passe à « added » et y reste, écrit par la première lecture qui le voit, en une requête pour toutes les demandes concernées.
"""

from __future__ import annotations

import json
import re
import time

from . import missing
from . import sagas as sagafile
from . import urls

KINDS = ("movie", "show")
STATUSES = ("pending", "added", "declined")
MAX_TEXT = 200                  # message, motif de refus, titre
YEAR_RE = re.compile(r"[0-9]{4}")

REQUESTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS requests(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k) for k in KINDS)})),
    tmdb    INTEGER NOT NULL,                   -- l'identifiant TMDB du film ou de la série
    title   TEXT NOT NULL DEFAULT '',
    year    TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    reason  TEXT NOT NULL DEFAULT '',           -- le motif d'un refus (vide sinon)
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL,
    UNIQUE(kind, tmdb)
)"""
REQUEST_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS request_users(
    request_id INTEGER NOT NULL,                -- requests.id ; la dernière ligne d'une demande partie, la demande est supprimée avec elle
    user_id    TEXT NOT NULL,                   -- users.id, pas une clé étrangère (comme `rights`) : un compte supprimé est nettoyé par accounts.delete
    note       TEXT NOT NULL DEFAULT '',        -- le message du demandeur, que seul l'administrateur lit
    created    INTEGER NOT NULL,
    ack        INTEGER NOT NULL DEFAULT 0,      -- le demandeur a vu que sa demande est arrivée (accueil)
    PRIMARY KEY(request_id, user_id)
)"""
TABLES = (REQUESTS_TABLE, REQUEST_USERS_TABLE)

ORPHANS = "DELETE FROM requests WHERE id NOT IN (SELECT request_id FROM request_users)"


class RequestError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


# ---------------------------------------------------------------------------
# Ce que la médiathèque a
# ---------------------------------------------------------------------------
OWNED_SQL = ("SELECT id AS item, lib, type, meta, 0 AS file FROM items WHERE (lib IN ({movies}) AND type='movie') OR (lib IN ({shows}) AND type='show') "
             "UNION ALL SELECT item_id, lib, 'movie', meta, 1 FROM entries WHERE lib IN ({shows}) AND meta LIKE ?")


def show_id(meta_text):
    """L'identifiant TMDB d'une série : celui que l'enrichissement garde dans sa fiche (`meta["tmdb"]`, voir enrich.show_facts), sinon celui du nom de son dossier (`{tmdb-…}`, `meta["tmdb_pinned"]`) ; None sinon."""
    meta = json.loads(meta_text or "{}") if meta_text else {}
    if not isinstance(meta, dict):
        return None
    return sagafile.film_id(meta.get("tmdb")) or sagafile.film_id(meta.get("tmdb_pinned"))


def _owned_key(row):
    if row["file"]:
        ident = missing.film_of_entry(row["meta"])
        return ("movie", ident) if ident is not None else None
    if row["type"] == "movie":
        ident = sagafile.tmdb_of(row)
        return ("movie", ident) if ident is not None else None
    try:
        ident = show_id(row["meta"])
    except ValueError:
        return None
    return ("show", ident) if ident is not None else None


def owned(db, libs):
    """{(kind, identifiant TMDB): [(id de l'élément, bibliothèque), …]} de ce que ces bibliothèques `libs` (objets Library) ont : les films comme `missing.owned_films` (y compris un film rangé dans une bibliothèque de séries, par l'étiquette de son fichier), les séries par leur fiche TMDB.
    Une requête, gardée en mémoire jusqu'au prochain scan ou enrichissement (Database.derived)."""
    movies, shows = [lib.id for lib in libs if lib.kind == "movies"], [lib.id for lib in libs if lib.kind == "shows"]

    def build():
        found = {}
        if not movies and not shows:
            return found
        sql = OWNED_SQL.format(movies=",".join("?" * len(movies)) or "NULL", shows=",".join("?" * len(shows)) or "NULL")
        for row in db.query(sql, [*movies, *shows, *shows, '%"movie/%']):
            key = _owned_key(row)
            if key is not None and (row["item"], row["lib"]) not in found.get(key, []):
                found.setdefault(key, []).append((row["item"], row["lib"]))
        return found
    return db.derived("requests-owned:" + ",".join(movies) + "|" + ",".join(shows), build)


def item_for(found, kind, tmdb, visible):
    """L'élément (son identifiant) qui répond à cette demande dans une bibliothèque de `visible` (None : toutes), ou None."""
    return next((item for item, lib in found.get((kind, tmdb), []) if visible is None or lib in visible), None)


def promote(db, rows, found):
    """Les demandes en attente de `rows` (dictionnaires, modifiés sur place) dont l'élément est dans `found` (voir `owned`) passent à « added », en une écriture pour toutes."""
    ids = {r["id"] for r in rows if r["status"] == "pending" and (r["kind"], r["tmdb"]) in found}
    if not ids:
        return rows
    now, listed = int(time.time()), sorted(ids)
    for start in range(0, len(listed), 500):
        chunk = listed[start:start + 500]
        db.execute(f"UPDATE requests SET status='added', updated=? WHERE status='pending' AND id IN ({','.join('?' * len(chunk))})", (now, *chunk))
    for r in rows:
        if r["id"] in ids:
            r["status"], r["updated"] = "added", now
    return rows


def refreshed(db, libs, rows):
    """(lignes, ce que la médiathèque a) : `rows` à jour de ce qui est arrivé. La médiathèque n'est regardée que s'il y a une demande en attente ou ajoutée."""
    rows = [dict(r) for r in rows]
    found = owned(db, libs) if any(r["status"] in ("pending", "added") for r in rows) else {}
    return promote(db, rows, found), found


# ---------------------------------------------------------------------------
# Lire
# ---------------------------------------------------------------------------
def cover(cfg, row, item):
    """L'image d'une demande : celle de l'élément quand le compte le voit, sinon l'affiche que l'enrichissement a gardée d'un film absent (saga, personne), sinon rien."""
    if item:
        return urls.item_cover(item)
    if row["kind"] == "movie" and (cfg.data / "tmdb" / missing.poster_name(row["tmdb"])).is_file():
        return urls.missing_film(row["tmdb"])
    return ""


def shown(cfg, row, found, visible):
    """Une demande telle que l'API la donne ; `item` (l'élément à ouvrir) quand elle est arrivée dans une bibliothèque que le compte voit."""
    item = item_for(found, row["kind"], row["tmdb"], visible) if row["status"] == "added" else None
    out = {"id": row["id"], "kind": row["kind"], "tmdb": row["tmdb"], "title": row["title"], "year": row["year"], "status": row["status"], "reason": row["reason"],
           "created": row["created"], "updated": row["updated"], "cover": cover(cfg, row, item)}
    if item:
        out["item"] = item
    return out


def mine(db, cfg, uid, visible):
    """Les demandes du compte `uid`, de la plus récente à la plus ancienne, avec son message (`note`) et `ack`. Une lecture des demandes."""
    rows = db.query("SELECT r.*, u.note, u.ack, u.created AS asked FROM requests r JOIN request_users u ON u.request_id = r.id WHERE u.user_id=? ORDER BY u.created DESC, r.id DESC", (uid,))
    rows, found = refreshed(db, cfg.libraries, rows)
    return [{**shown(cfg, r, found, visible), "note": r["note"], "ack": bool(r["ack"]), "asked": r["asked"], "mine": True} for r in rows]


def everything(db, cfg, uid, status=None):
    """Toutes les demandes, pour l'administrateur : chacune avec ses demandeurs (`users` : identifiant, nom, message, date), de la plus récente à la plus ancienne ; `status` en garde une sorte. Une lecture des demandes."""
    if status is not None and status not in STATUSES:
        raise RequestError(400, "statut inconnu (pending, added ou declined)")
    rows = db.query("SELECT r.*, u.user_id, u.note, u.created AS asked, COALESCE(a.name, '') AS name FROM requests r JOIN request_users u ON u.request_id = r.id "
                    "LEFT JOIN users a ON a.id = u.user_id ORDER BY r.created DESC, r.id DESC, u.rowid")
    requests, users = {}, {}
    for row in rows:
        requests.setdefault(row["id"], row)
        users.setdefault(row["id"], []).append({"id": row["user_id"], "name": row["name"], "note": row["note"], "created": row["asked"]})
    listed, found = refreshed(db, cfg.libraries, list(requests.values()))
    return [{**shown(cfg, r, found, None), "users": users[r["id"]], "mine": any(u["id"] == uid for u in users[r["id"]])}
            for r in listed if status is None or r["status"] == status]


def pending_count(db, cfg):
    """Le nombre de demandes en attente (pour l'administrateur), une fois retirées celles qui sont arrivées."""
    rows, _ = refreshed(db, cfg.libraries, db.query("SELECT id, kind, tmdb, status, updated FROM requests WHERE status='pending'"))
    return sum(1 for r in rows if r["status"] == "pending")


def ready(db, cfg, uid, visible):
    """Les demandes du compte arrivées dans une bibliothèque qu'il voit et qu'il n'a pas encore vues (`ack` à 0) : [(demande, id de l'élément)]. Une lecture des demandes ; la médiathèque n'est regardée que si le compte en a."""
    rows = db.query("SELECT r.* FROM requests r JOIN request_users u ON u.request_id = r.id WHERE u.user_id=? AND u.ack=0 AND r.status IN ('pending', 'added') ORDER BY r.updated DESC, r.id DESC", (uid,))
    if not rows:
        return []
    rows, found = refreshed(db, cfg.libraries, rows)
    return [(r, item) for r in rows if r["status"] == "added" for item in [item_for(found, r["kind"], r["tmdb"], visible)] if item]


def marks(db, cfg, uid, kind="movie"):
    """{identifiant TMDB: {"status", "mine"}} des demandes de cette sorte : `mine` dit si ce compte en est, rien d'autre (ni qui, ni combien). Une lecture des demandes."""
    rows = db.query("SELECT r.id, r.kind, r.tmdb, r.status, r.updated, MAX(u.user_id = ?) AS mine FROM requests r JOIN request_users u ON u.request_id = r.id WHERE r.kind=? GROUP BY r.id", (uid, kind))
    rows, _ = refreshed(db, cfg.libraries, rows)
    return {r["tmdb"]: {"status": r["status"], "mine": bool(r["mine"])} for r in rows}


# ---------------------------------------------------------------------------
# Écrire
# ---------------------------------------------------------------------------
def _text(value, what, limit=MAX_TEXT):
    """Un texte facultatif d'au plus `limit` caractères (espaces de bord retirés) ; 400 sinon."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RequestError(400, f"{what} : un texte est attendu")
    value = value.strip()
    if len(value) > limit:
        raise RequestError(400, f"{what} trop long ({limit} caractères au plus)")
    return value


def checked(body):
    """(kind, tmdb, message, titre, année) d'un corps de demande ; 400 en français pour ce qui ne va pas."""
    kind, tmdb = body.get("kind"), body.get("tmdb")
    if kind not in KINDS:
        raise RequestError(400, "sorte de demande inconnue (movie ou show)")
    if isinstance(tmdb, bool) or not isinstance(tmdb, int) or not 0 < tmdb < 1 << 53:
        raise RequestError(400, "identifiant TMDB attendu : un entier positif")
    note = _text(body.get("note"), "message")
    title = _text(body.get("title"), "titre")
    year = body.get("year")
    year = "" if year in (None, "") else str(year) if isinstance(year, (str, int)) and not isinstance(year, bool) else None
    if year is None or (year and not YEAR_RE.fullmatch(year)):
        raise RequestError(400, "année attendue : 4 chiffres")
    return kind, tmdb, note, title, year


def known_film(db, tmdb):
    """(titre, année) d'un film que l'enrichissement a décrit (absent d'une saga, ou de la filmographie d'une personne), ou None."""
    film = missing.load(db)["films"].get(str(tmdb))
    if isinstance(film, dict) and film.get("title"):
        return film["title"], film.get("year") or ""
    for row in db.query("SELECT value FROM kv WHERE key LIKE 'personinfo:%' AND value LIKE ?", (f'%"tmdb": {tmdb},%',)):
        known = json.loads(row["value"])
        film = next((f for f in (known.get("films") or []) if isinstance(f, dict) and f.get("tmdb") == tmdb and f.get("title")), None) if isinstance(known, dict) else None
        if film:
            return film["title"], film.get("year") or ""
    return None


def create(db, cfg, uid, body, visible, limit):
    """Le compte `uid` demande un film ou une série (`body` : {kind, tmdb, note, title, year}) ; rend (demande, nouvelle ?). `visible` : les bibliothèques qu'il voit (None : toutes) ; `limit` : ses demandes en attente au plus (None : sans plafond).

    409 quand l'élément est déjà dans une bibliothèque qu'il voit (avec son identifiant, `item`), quand la demande a été refusée, ou au-delà de `limit`. Une demande qu'il a déjà faite rend la même, sans doublon."""
    kind, tmdb, note, title, year = checked(body)
    found = owned(db, cfg.libraries)
    item = item_for(found, kind, tmdb, visible)
    if item:
        raise RequestError(409, "déjà dans la médiathèque", {"item": item})
    known = known_film(db, tmdb) if kind == "movie" else None
    title, year = (known[0][:MAX_TEXT], known[1][:4]) if known else (title, year)
    now = int(time.time())
    with db.write_lock:
        conn = db.connection()
        try:
            row = conn.execute("SELECT * FROM requests WHERE kind=? AND tmdb=?", (kind, tmdb)).fetchone()
            if row is not None and row["status"] == "declined":
                raise RequestError(409, f"cette demande a été refusée : {row['reason']}" if row["reason"] else "cette demande a été refusée")
            if row is not None and conn.execute("SELECT 1 FROM request_users WHERE request_id=? AND user_id=?", (row["id"], uid)).fetchone():
                conn.rollback()
                return dict(row), False
            waiting = conn.execute("SELECT COUNT(*) FROM request_users u JOIN requests r ON r.id = u.request_id WHERE u.user_id=? AND r.status='pending'", (uid,)).fetchone()[0]
            if limit is not None and (row is None or row["status"] == "pending") and waiting >= limit:
                raise RequestError(409, f"trop de demandes en attente ({limit})")
            if row is None:
                status = "added" if (kind, tmdb) in found else "pending"
                rid = conn.execute("INSERT INTO requests(kind, tmdb, title, year, status, reason, created, updated) VALUES(?,?,?,?,?,'',?,?)", (kind, tmdb, title, year, status, now, now)).lastrowid
            else:
                rid = row["id"]
            conn.execute("INSERT INTO request_users(request_id, user_id, note, created, ack) VALUES(?,?,?,?,0)", (rid, uid, note, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    joined = row is not None
    row = dict(db.one("SELECT * FROM requests WHERE id=?", (rid,)))
    return promote(db, [row], found)[0], not joined


def withdraw(db, uid, rid, admin):
    """Un compte retire sa demande (le dernier demandeur parti, la demande disparaît) ; l'administrateur supprime la demande entière. 404 pour une demande inconnue ou qui n'est pas celle du compte (les deux se répondent pareil)."""
    with db.write_lock:
        conn = db.connection()
        try:
            if admin:
                gone = conn.execute("DELETE FROM requests WHERE id=?", (rid,)).rowcount
                conn.execute("DELETE FROM request_users WHERE request_id=?", (rid,))
            else:
                gone = conn.execute("DELETE FROM request_users WHERE request_id=? AND user_id=?", (rid, uid)).rowcount
                conn.execute("DELETE FROM requests WHERE id=? AND NOT EXISTS (SELECT 1 FROM request_users WHERE request_id=?)", (rid, rid))
            if not gone:
                raise RequestError(404, "demande inconnue")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def set_status(db, rid, status, reason):
    """L'administrateur passe une demande à « added », « declined » (avec un motif facultatif) ou la remet en « pending ». Le motif n'est gardé que pour un refus ; `ack` revient à 0 pour tous ses demandeurs (une arrivée suivante se montrera de nouveau)."""
    if status not in STATUSES:
        raise RequestError(400, "statut inconnu (pending, added ou declined)")
    reason = _text(reason, "motif") if status == "declined" else ""
    with db.write_lock:
        conn = db.connection()
        try:
            if not conn.execute("UPDATE requests SET status=?, reason=?, updated=? WHERE id=?", (status, reason, int(time.time()), rid)).rowcount:
                raise RequestError(404, "demande inconnue")
            conn.execute("UPDATE request_users SET ack=0 WHERE request_id=?", (rid,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def acknowledge(db, uid, rid):
    """Le compte a vu que sa demande est arrivée : elle quitte son accueil. 404 pour une demande inconnue ou qui n'est pas la sienne."""
    if not db.execute("UPDATE request_users SET ack=1 WHERE request_id=? AND user_id=?", (rid, uid)).rowcount:
        raise RequestError(404, "demande inconnue")


def forget_user(conn, uid):
    """Dans la transaction de la suppression d'un compte (accounts.delete) : ses demandes le perdent, et une demande qui n'a plus de demandeur disparaît."""
    conn.execute("DELETE FROM request_users WHERE user_id=?", (uid,))
    conn.execute(ORPHANS)
