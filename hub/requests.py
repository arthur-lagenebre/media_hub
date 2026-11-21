"""Les demandes : la liste d'attente des films, des séries et des artistes qu'un compte aimerait voir arriver, que l'administrateur lit.

Le hub ne télécharge rien et n'écrit pas dans la médiathèque : une demande n'est qu'une ligne que l'administrateur lit, puis qu'il marque « ajoutée » ou « refusée ». Une demande désigne un film ou une série par son identifiant TMDB (`kind` : `movie` ou `show`), un artiste par son nom (`kind` : `artist`, un simple champ texte sans recherche, comparé par `labels.comparable`), une seule ligne par élément (`requests`), et chaque compte qui la fait s'y joint (`request_users`, avec son message facultatif et `ack` : il a vu qu'elle est arrivée).

Confidentialité : un compte lit ses demandes, et seulement « quelqu'un d'autre l'a demandé » pour les autres (`mine: false`), sans nom ni nombre. Seul l'administrateur voit les demandeurs et leurs messages.

L'état « ajouté » se calcule à la lecture : une demande en attente dont l'élément est dans la médiathèque (n'importe quelle bibliothèque : voir `owned`) passe à « added » et y reste, écrit par la première lecture qui le voit, en une requête pour toutes les demandes concernées.
Un artiste est « là » quand l'artiste d'un album (`grp`) de ce nom comparable est dans une bibliothèque de musique ; les albums manquants (clé `wanted:`) ne comptent pas, ils ne sont pas des éléments.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from functools import partial

from . import labels, missing
from . import sagas as sagafile
from . import urls

KINDS = ("movie", "show")       # ce que TMDB connaît : ce que la recherche des demandes propose
ARTIST = "artist"               # une demande faite par un simple nom, sans TMDB (voir `named`)
ALL_KINDS = (*KINDS, ARTIST)
STATUSES = ("pending", "added", "declined")
MAX_TEXT = 200                  # message, motif de refus, titre, nom d'un artiste
YEAR_RE = re.compile(r"[0-9]{4}")
TMDB_PATHS = {"movie": "movie", "show": "tv"}     # où TMDB range chaque sorte, sur son site comme dans son API


def requests_table(name="requests"):
    """Le DDL de la table des demandes, sous le nom `name` : la migration 12 (db.py) la recrée sous un autre nom avant de remplacer l'ancienne (SQLite ne change pas une contrainte en place)."""
    return f"""
CREATE TABLE IF NOT EXISTS {name}(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k) for k in ALL_KINDS)})),
    tmdb     INTEGER,                           -- l'identifiant TMDB du film ou de la série ; NULL pour un artiste
    name_key TEXT,                              -- le nom d'un artiste tel qu'on le compare (labels.comparable) ; NULL pour un film ou une série
    title    TEXT NOT NULL DEFAULT '',          -- le titre, ou le nom d'un artiste tel que le premier demandeur l'a tapé
    year     TEXT NOT NULL DEFAULT '',
    status   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    reason   TEXT NOT NULL DEFAULT '',          -- le motif d'un refus (vide sinon)
    created  INTEGER NOT NULL,
    updated  INTEGER NOT NULL,
    CHECK (CASE WHEN kind = {ARTIST!r} THEN COALESCE(name_key, '') <> '' ELSE tmdb IS NOT NULL END)
)"""


REQUESTS_TABLE = requests_table()
# Une seule demande par film ou série (par sorte et identifiant TMDB), et par artiste (par nom comparable). Pas dans TABLES : SCHEMA tourne aussi sur une base d'avant la version 12, dont la table n'a pas `name_key` ; ils se créent
# avec la migration 12 et à l'ouverture d'une base neuve (db.Database._upgrade).
REQUEST_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS requests_tmdb ON requests(kind, tmdb) WHERE tmdb IS NOT NULL",
    f"CREATE UNIQUE INDEX IF NOT EXISTS requests_artist ON requests(kind, name_key) WHERE kind = {ARTIST!r}",
)
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


def _artist_id(meta_text):
    """L'identifiant de la fiche d'un artiste (`/api/artist/<bibliothèque>/<id>`) que porte l'album, "" quand il n'en a pas (une fiche sans adresse stable)."""
    try:
        meta = json.loads(meta_text or "{}")
    except ValueError:
        return ""
    return meta.get("artist_id") or "" if isinstance(meta, dict) else ""


def _gathered(rows):
    """{(kind, identifiant TMDB): [(id de l'élément, bibliothèque), …]} des lignes de OWNED_SQL ; une ligne sans identifiant lisible est écartée, un élément n'y est qu'une fois."""
    found = {}
    for row in rows:
        key = _owned_key(row)
        if key is not None and (row["item"], row["lib"]) not in found.get(key, []):
            found.setdefault(key, []).append((row["item"], row["lib"]))
    return found


def _artists(db, music):
    """{("artist", nom comparable): [(id de la fiche ou "", bibliothèque, nom), …]} des artistes d'album (`grp`) de ces bibliothèques de musique (identifiants), un par nom et par bibliothèque. Les albums manquants n'y sont pas : ils ne sont pas des éléments."""
    if not music:
        return {}
    named = {}
    for row in db.query(f"SELECT lib, grp, meta FROM items WHERE type='album' AND grp != '' AND lib IN ({','.join('?' * len(music))})", music):
        if not named.get((row["lib"], row["grp"])):
            named[(row["lib"], row["grp"])] = _artist_id(row["meta"])
    found = {}
    for (lib, name), ident in named.items():
        key = labels.comparable(name)
        if key:
            found.setdefault((ARTIST, key), []).append((ident, lib, name))
    return found


def _owned_now(db, movies, shows, music):
    """Ce que `owned` rend, lu dans la base pour ces bibliothèques (identifiants) : une requête pour les films et les séries, une pour les artistes ; rien sans bibliothèque."""
    found = {}
    if movies or shows:
        sql = OWNED_SQL.format(movies=",".join("?" * len(movies)) or "NULL", shows=",".join("?" * len(shows)) or "NULL")
        found = _gathered(db.query(sql, [*movies, *shows, *shows, '%"movie/%']))
    return {**found, **_artists(db, music)}


def owned(db, libs, artists=False):
    """{(kind, identifiant TMDB ou nom comparable d'un artiste): [(id de l'élément, bibliothèque), …]} de ce que ces bibliothèques `libs` (objets Library) ont : les films comme `missing.owned_films` (y compris un film rangé dans une bibliothèque de séries, par l'étiquette de son fichier), les séries par leur fiche TMDB,
    les artistes, seulement si `artists`, par le nom de l'artiste de leurs albums (chacun : (id de sa fiche ou "", bibliothèque, nom), voir `_artists`) : un foyer qui ne demande que des films ne fait pas lire ses albums.
    Une ou deux requêtes, gardées en mémoire jusqu'au prochain scan ou enrichissement (Database.derived)."""
    movies, shows, music = ([lib.id for lib in libs if lib.kind == kind and (kind != "music" or artists)] for kind in ("movies", "shows", "music"))
    return db.derived("requests-owned:" + ",".join(movies) + "|" + ",".join(shows) + "|" + ",".join(music), partial(_owned_now, db, movies, shows, music))


def ref(row):
    """Ce qui désigne la demande `row` parmi celles de sa sorte : l'identifiant TMDB d'un film ou d'une série, le nom comparable d'un artiste."""
    return row["name_key"] if row["kind"] == ARTIST else row["tmdb"]


def _visible(found, key, visible):
    """Le premier de `found[key]` qui est dans une bibliothèque de `visible` (None : toutes), ou None."""
    return next((place for place in found.get(key, []) if visible is None or place[1] in visible), None)


def item_for(found, kind, tmdb, visible):
    """L'élément (son identifiant) qui répond à cette demande de film ou de série dans une bibliothèque de `visible` (None : toutes), ou None."""
    place = _visible(found, (kind, tmdb), visible)
    return place[0] if place else None


def arrival(found, kind, ident, visible):
    """Ce qui répond à une demande (`ident` : voir `ref`) dans une bibliothèque de `visible`, ou None : l'identifiant de l'élément pour un film ou une série, `{"lib", "name", "id"?}` pour un artiste (`id` : sa fiche, absent quand elle n'a pas d'adresse stable : on filtre alors la bibliothèque sur son nom)."""
    if kind != ARTIST:
        return item_for(found, kind, ident, visible)
    place = _visible(found, (kind, ident), visible)
    return {"lib": place[1], "name": place[2], **({"id": place[0]} if place[0] else {})} if place else None


def _there(kind, place):
    """`place` (voir `arrival`) sous son nom dans une réponse : `item` pour un film ou une série, `artist` pour un artiste."""
    return {"artist" if kind == ARTIST else "item": place}


def promote(db, rows, found):
    """Les demandes en attente de `rows` (dictionnaires, modifiés sur place) dont l'élément est dans `found` (voir `owned`) passent à « added », en une écriture pour toutes."""
    ids = {r["id"] for r in rows if r["status"] == "pending" and (r["kind"], ref(r)) in found}
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
    found = owned(db, libs, any(r["kind"] == ARTIST for r in rows)) if any(r["status"] in ("pending", "added") for r in rows) else {}
    return promote(db, rows, found), found


# ---------------------------------------------------------------------------
# Lire
# ---------------------------------------------------------------------------
def tmdb_path(kind, tmdb):
    """Le chemin d'un film ou d'une série chez TMDB : `movie/<id>` ou `tv/<id>`."""
    return f"{TMDB_PATHS[kind]}/{tmdb}"


def tmdb_url(kind, tmdb):
    """L'adresse de la fiche d'un film ou d'une série sur le site de TMDB."""
    from libraries.video.tmdb import WEB_BASE       # ici et pas en tête : ce module est importé (par hub.accounts, hub.__main__) avant que media_library soit sur le chemin
    return f"{WEB_BASE}/{tmdb_path(kind, tmdb)}"


def poster_file(kind, tmdb):
    """Le nom de l'affiche TMDB d'un film ou d'une série dans HUB_DATA/tmdb : celui que l'enrichissement lui donne (`film-<id>.jpg`, voir missing.poster_name ; `<id>.jpg` pour une série, voir enrich.enrich_show)."""
    return missing.poster_name(tmdb) if kind == "movie" else f"{tmdb}.jpg"


def cover(cfg, row, item):
    """L'image d'une demande : celle de l'élément quand le compte le voit, sinon l'affiche gardée dans HUB_DATA/tmdb (par l'enrichissement, ou par la tâche « Affiches des demandes ») :
    `/img/film/<tmdb>` pour un film (comme les films absents des sagas), `/img/request/<id>` pour une série ; sinon rien."""
    if item:
        return urls.item_cover(item)
    if not (cfg.data / "tmdb" / poster_file(row["kind"], row["tmdb"])).is_file():
        return ""
    return urls.missing_film(row["tmdb"]) if row["kind"] == "movie" else urls.request_poster(row["id"])


def shown(cfg, row, found, visible):
    """Une demande telle que l'API la donne ; `item` (l'élément à ouvrir) ou, pour un artiste, `artist` (sa fiche, voir `arrival`) quand elle est arrivée dans une bibliothèque que le compte voit. Un artiste n'a ni `tmdb` ni `url`, et sa `cover` est vide."""
    place = arrival(found, row["kind"], ref(row), visible) if row["status"] == "added" else None
    out = {"id": row["id"], "kind": row["kind"], "title": row["title"], "year": row["year"], "status": row["status"], "reason": row["reason"], "created": row["created"], "updated": row["updated"]}
    if row["kind"] == ARTIST:
        out["cover"] = ""
    else:
        out.update({"tmdb": row["tmdb"], "cover": cover(cfg, row, place), "url": tmdb_url(row["kind"], row["tmdb"])})
    if place:
        out.update(_there(row["kind"], place))
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
    rows, _ = refreshed(db, cfg.libraries, db.query("SELECT id, kind, tmdb, name_key, status, updated FROM requests WHERE status='pending'"))
    return sum(1 for r in rows if r["status"] == "pending")


def ready(db, cfg, uid, visible):
    """Les demandes du compte arrivées dans une bibliothèque qu'il voit et qu'il n'a pas encore vues (`ack` à 0) : [(demande, ce qui y répond : voir `arrival`)]. Une lecture des demandes ; la médiathèque n'est regardée que si le compte en a."""
    rows = db.query("SELECT r.* FROM requests r JOIN request_users u ON u.request_id = r.id WHERE u.user_id=? AND u.ack=0 AND r.status IN ('pending', 'added') ORDER BY r.updated DESC, r.id DESC", (uid,))
    if not rows:
        return []
    rows, found = refreshed(db, cfg.libraries, rows)
    return [(r, place) for r in rows if r["status"] == "added" for place in [arrival(found, r["kind"], ref(r), visible)] if place]


def marks(db, cfg, uid, kind="movie"):
    """{identifiant TMDB: {"status", "mine", "id"?}} des demandes de cette sorte : `mine` dit si ce compte en est, rien d'autre (ni qui, ni combien) ; `id` (de quoi la retirer) seulement quand c'est la sienne, jamais pour celle d'un autre. Une lecture des demandes."""
    rows = db.query("SELECT r.id, r.kind, r.tmdb, r.status, r.updated, MAX(u.user_id = ?) AS mine FROM requests r JOIN request_users u ON u.request_id = r.id WHERE r.kind=? GROUP BY r.id", (uid, kind))
    rows, _ = refreshed(db, cfg.libraries, rows)
    return {r["tmdb"]: _mark(r) for r in rows}


def _mark(row):
    """Ce qu'un compte sait d'une demande (`row` porte `mine`) : son état, si c'est la sienne, et alors seulement son identifiant."""
    return {"status": row["status"], "mine": True, "id": row["id"]} if row["mine"] else {"status": row["status"], "mine": False}


def search_marks(db, cfg, uid, keys):
    """Pour la recherche dans TMDB : ({(kind, tmdb): marque (voir `_mark`)} des demandes de ces éléments `keys`, ce que la médiathèque a (voir `owned`)).
    Une lecture des demandes et un regard sur la médiathèque pour tous les résultats ; rien du tout sans résultat."""
    if not keys:
        return {}, {}
    found = owned(db, cfg.libraries)
    ids = sorted({tmdb for _, tmdb in keys})
    rows = db.query(f"SELECT r.id, r.kind, r.tmdb, r.status, r.updated, MAX(u.user_id = ?) AS mine FROM requests r JOIN request_users u ON u.request_id = r.id "
                    f"WHERE r.tmdb IN ({','.join('?' * len(ids))}) GROUP BY r.id", (uid, *ids))
    rows = promote(db, [dict(r) for r in rows], found)
    wanted = set(keys)
    return {(r["kind"], r["tmdb"]): _mark(r) for r in rows if (r["kind"], r["tmdb"]) in wanted}, found


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


def _kind(value, allowed=KINDS):
    """La sorte de demande : une de `allowed` (par défaut celles de TMDB, que la recherche propose) ; 400 sinon."""
    if value not in allowed:
        raise RequestError(400, "sorte de demande inconnue (movie ou show)")
    return value


def _artist_name(value):
    """(nom, nom comparable) d'un nom d'artiste : un texte de 1 à MAX_TEXT caractères (espaces de bord retirés, le nom reste tel que tapé), sans caractère de contrôle ni de mise en forme ; 400 sinon.
    Un nom sans rien de comparable (que des signes combinants) est vide."""
    if not isinstance(value, str):
        raise RequestError(400, "nom d'artiste attendu : un texte")
    name = value.strip()
    if any(unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp") for c in name):
        raise RequestError(400, "nom d'artiste : aucun caractère de contrôle permis")
    if len(name) > MAX_TEXT:
        raise RequestError(400, f"nom d'artiste trop long ({MAX_TEXT} caractères au plus)")
    key = labels.comparable(name)
    if not key:
        raise RequestError(400, "nom d'artiste vide")
    return name, key


def _tmdb_id(value):
    """Un identifiant TMDB : un entier (pas un booléen) de 1 à 2⁵³ exclu ; 400 sinon."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 1 << 53:
        raise RequestError(400, "identifiant TMDB attendu : un entier positif")
    return value


def _year(value):
    """Une année facultative : absente ou vide, sinon 4 chiffres (texte ou nombre, pas un booléen) ; 400 sinon."""
    if value in (None, ""):
        return ""
    text = str(value) if type(value) in (str, int) else ""          # un booléen est un entier, une liste ou un nombre à virgule n'est pas une année
    if not YEAR_RE.fullmatch(text):
        raise RequestError(400, "année attendue : 4 chiffres")
    return text


def checked(body):
    """(kind, ref, message, titre, année) d'un corps de demande (`ref` : voir `ref`) ; 400 en français pour ce qui ne va pas, dans cet ordre.
    Un artiste : `{"kind": "artist", "name", "note"?}`, son titre est son nom tel que tapé, sans année ; un film ou une série : `tmdb`, et `title` et `year` facultatifs."""
    kind = _kind(body.get("kind"), ALL_KINDS)
    if kind == ARTIST:
        name, key = _artist_name(body.get("name"))
        return kind, key, _text(body.get("note"), "message"), name, ""
    return kind, _tmdb_id(body.get("tmdb")), _text(body.get("note"), "message"), _text(body.get("title"), "titre"), _year(body.get("year"))


def _named(film):
    """(titre, année) d'une fiche de film gardée par l'enrichissement, ou None quand elle n'a pas de titre."""
    return (film["title"], film.get("year") or "") if isinstance(film, dict) and film.get("title") else None


def _in_filmography(text, tmdb):
    """(titre, année) du film `tmdb` dans une filmographie gardée (`personinfo:<id>`, voir enrich.enrich_person), ou None."""
    known = json.loads(text)
    films = (known.get("films") or []) if isinstance(known, dict) else []
    return next((_named(f) for f in films if isinstance(f, dict) and f.get("tmdb") == tmdb and _named(f)), None)


def known_film(db, tmdb):
    """(titre, année) d'un film que l'enrichissement a décrit (absent d'une saga, ou de la filmographie d'une personne), ou None."""
    found = _named(missing.load(db)["films"].get(str(tmdb)))
    if found:
        return found
    rows = db.query("SELECT value FROM kv WHERE key LIKE 'personinfo:%' AND value LIKE ?", (f'%"tmdb": {tmdb},%',))
    return next((hit for hit in (_in_filmography(row["value"], tmdb) for row in rows) if hit), None)


def _described(db, kind, tmdb, title, year, describe, uid, limit):
    """(titre, année) d'une demande : ce que le serveur sait d'un film ; sinon, pour une demande nouvelle, ce que `describe(kind, tmdb)` en dit (TMDB, voir request_search.Finder.details :
    ses RequestError passent, rien n'est alors écrit) ; sinon (sans `describe`, ou demande qui existe déjà et garde son titre) ce que le corps a dit.
    Avant d'interroger TMDB, les 409 (refusée, plafond atteint) sont vérifiés : une demande perdue d'avance ne coûte pas d'appel. La transaction de `create` les revérifie."""
    if kind == ARTIST:                      # un nom n'a rien à demander à TMDB
        return title, year
    known = known_film(db, tmdb) if kind == "movie" else None
    if not known and describe is not None:
        conn = db.connection()
        row, _ = _existing(conn, kind, tmdb, uid)
        if row is None:
            _check_room(conn, None, uid, limit)
            known = describe(kind, tmdb)
    return (known[0][:MAX_TEXT], known[1][:4]) if known else (title, year)


def _existing(conn, kind, ident, uid):
    """(la demande de cet élément (`ident` : voir `ref`) ou None, ce compte en est-il) ; 409 quand elle a été refusée."""
    row = conn.execute("SELECT * FROM requests WHERE kind=? AND name_key=?" if kind == ARTIST else "SELECT * FROM requests WHERE kind=? AND tmdb=?", (kind, ident)).fetchone()
    if row is not None and row["status"] == "declined":
        raise RequestError(409, f"cette demande a été refusée : {row['reason']}" if row["reason"] else "cette demande a été refusée")
    mine = row is not None and conn.execute("SELECT 1 FROM request_users WHERE request_id=? AND user_id=?", (row["id"], uid)).fetchone() is not None
    return row, mine


def _check_room(conn, row, uid, limit):
    """409 quand le compte a déjà `limit` demandes en attente (None : sans plafond) et que celle-ci en serait une de plus (nouvelle, ou en attente)."""
    waiting = conn.execute("SELECT COUNT(*) FROM request_users u JOIN requests r ON r.id = u.request_id WHERE u.user_id=? AND r.status='pending'", (uid,)).fetchone()[0]
    if limit is not None and (row is None or row["status"] == "pending") and waiting >= limit:
        raise RequestError(409, f"trop de demandes en attente ({limit})")


def _store(conn, row, uid, fields, found, now):
    """Écrit la demande (si `row` est None : nouvelle, déjà « added » si l'élément est là) et le compte parmi ses demandeurs ; rend l'identifiant de la demande. `fields` : (kind, ref, message, titre, année)."""
    kind, ident, note, title, year = fields
    if row is None:
        status = "added" if (kind, ident) in found else "pending"
        tmdb, name_key = (None, ident) if kind == ARTIST else (ident, None)
        rid = conn.execute("INSERT INTO requests(kind, tmdb, name_key, title, year, status, reason, created, updated) VALUES(?,?,?,?,?,?,'',?,?)", (kind, tmdb, name_key, title, year, status, now, now)).lastrowid
    else:
        rid = row["id"]
    conn.execute("INSERT INTO request_users(request_id, user_id, note, created, ack) VALUES(?,?,?,?,0)", (rid, uid, note, now))
    return rid


def create(db, cfg, uid, body, visible, limit, describe=None):
    """Le compte `uid` demande un film ou une série (`body` : {kind, tmdb, note, title, year}) ou un artiste (`body` : {kind: "artist", name, note}, voir `checked`) ; rend (demande, nouvelle ?). `visible` : les bibliothèques qu'il voit (None : toutes) ; `limit` : ses demandes en attente au plus (None : sans plafond).
    `describe(kind, tmdb)` : (titre, année) d'un élément que le serveur ne connaît pas (voir `_described`), None pour s'en tenir au corps.

    409 quand l'élément est déjà dans une bibliothèque qu'il voit (avec son identifiant, `item`, ou sa fiche, `artist`), quand la demande a été refusée, ou au-delà de `limit`. Une demande qu'il a déjà faite rend la même, sans doublon."""
    kind, ident, note, title, year = checked(body)
    found = owned(db, cfg.libraries, kind == ARTIST)
    place = arrival(found, kind, ident, visible)
    if place:
        raise RequestError(409, "déjà dans la médiathèque", _there(kind, place))
    title, year = _described(db, kind, ident, title, year, describe, uid, limit)
    now = int(time.time())
    with db.write_lock:
        conn = db.connection()
        try:
            row, mine = _existing(conn, kind, ident, uid)
            if mine:
                conn.rollback()
                return dict(row), False
            _check_room(conn, row, uid, limit)
            rid = _store(conn, row, uid, (kind, ident, note, title, year), found, now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    stored = dict(db.one("SELECT * FROM requests WHERE id=?", (rid,)))
    return promote(db, [stored], found)[0], row is None


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


def set_status(db, rid, status, reason, libs=()):
    """L'administrateur passe une demande à « added », « declined » (avec un motif facultatif) ou la remet en « pending ». Le motif n'est gardé que pour un refus ; `ack` revient à 0 pour tous ses demandeurs (une arrivée suivante se montrera de nouveau).
    409 pour remettre en attente une demande dont l'élément est dans l'une des bibliothèques `libs` (objets Library) : la lecture suivante la repasserait à « added » (voir `promote`). Pour « ce n'est pas la bonne version », la refuser avec un motif."""
    if status not in STATUSES:
        raise RequestError(400, "statut inconnu (pending, added ou declined)")
    reason = _text(reason, "motif") if status == "declined" else ""
    if status == "pending":
        row = db.one("SELECT kind, tmdb, name_key FROM requests WHERE id=?", (rid,))
        if row is not None and (row["kind"], ref(row)) in owned(db, libs, row["kind"] == ARTIST):
            raise RequestError(409, "L'élément est déjà dans la médiathèque : la demande ne peut pas rester en attente.")
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
