"""Le serveur HTTP : l'API JSON, le streaming des fichiers, et l'interface web (hub/web).

Bibliothèque standard uniquement. Un fil par connexion (ThreadingHTTPServer) : un film qui se lit n'empêche pas de feuilleter.

Pas de chemin de fichier dans l'API : le client ne connaît que des identifiants, le serveur retrouve le fichier dans la base. Un identifiant inconnu est un 404, jamais une lecture de disque.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from libraries.books import shelf
from libraries.books.archive import BookError
from libraries.common.text import fold, initial
from libraries.music import artistinfo, musicbrainz

from .. import __version__
from .. import enrich
from .. import playlists
from ..db import Database, make_id
from ..media import books as bookmedia
from ..media import probe as prober
from ..media import subs as subtitles
from ..media.images import Images, sniff
from .jobs import Jobs

PROFILE = "default"
CHUNK = 256 * 1024
SESSION_DAYS = 90
WATCHED_AT = 0.92          # au-delà de cette part, ou à moins d'une minute et demie de la fin, le film est vu
WATCHED_TAIL = 90
RESUME_MIN = 20            # en deçà (secondes), on ne reprend pas : on recommence

for ext, mime in {".mkv": "video/x-matroska", ".flac": "audio/flac", ".opus": "audio/ogg", ".m4a": "audio/mp4", ".webp": "image/webp", ".cbz": "application/vnd.comicbook+zip",
                  ".epub": "application/epub+zip", ".avi": "video/x-msvideo", ".m4v": "video/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg", ".mp3": "audio/mpeg",
                  ".webmanifest": "application/manifest+json", ".js": "text/javascript", ".svg": "image/svg+xml"}.items():
    mimetypes.add_type(mime, ext)


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def safe_json(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


class Hub:
    """L'état de l'application : réglages, base, images, tâches."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.db = Database(cfg.data / "hub.db")
        self.images = Images(cfg, self.db)
        self.jobs = Jobs(cfg, self.db)
        self.streams = threading.BoundedSemaphore(cfg.max_streams)
        self.active = 0
        self.secret = self._secret()
        self.started = time.time()

    def _secret(self):
        value = self.db.kv_get("secret")
        if not value:
            value = secrets.token_hex(32)
            self.db.kv_set("secret", value)
        return value

    def token(self):
        return hmac.new(self.secret.encode(), b"hub-session", hashlib.sha256).hexdigest()

    def path_of(self, entry):
        lib = self.cfg.library(entry["lib"])
        if lib is None:
            raise HttpError(404, "bibliothèque inconnue")
        path = Path(os.path.normpath(Path(lib.path) / entry["rel"]))
        if not str(path).startswith(os.path.normpath(str(lib.path))):
            raise HttpError(400, "chemin refusé")
        return lib, path

    def entry(self, ident):
        row = self.db.one("SELECT * FROM entries WHERE id=?", (ident,))
        if row is None:
            raise HttpError(404, "fichier inconnu")
        return row

    def item(self, ident):
        row = self.db.one("SELECT * FROM items WHERE id=?", (ident,))
        if row is None:
            raise HttpError(404, "élément inconnu")
        return row

    def first_scan(self):
        """Une bibliothèque jamais scannée l'est au démarrage : sans ça, le site s'ouvre sur du vide."""
        for lib in self.cfg.libraries:
            if self.db.kv_get(f"scanned:{lib.id}") is None:
                self.jobs.scan(lib)

    def autoscan(self):
        hours = self.cfg.autoscan_hours
        if hours <= 0:
            return

        def loop():
            while True:
                time.sleep(hours * 3600)
                if not self.jobs.busy():
                    self.jobs.scan_all()
        threading.Thread(target=loop, name="autoscan", daemon=True).start()


# ---------------------------------------------------------------------------
# Sérialisation
# ---------------------------------------------------------------------------
def summary(row, seen=None):
    meta = safe_json(row["meta"], {})
    out = {"id": row["id"], "type": row["type"], "title": row["title"], "year": row["year"], "grp": row["grp"], "n": row["count"], "letter": row["letter"],
           "added": row["added"], "genres": safe_json(row["genres"], [])}
    if meta.get("part") is not None:
        out["part"] = meta["part"]
    if row["type"] == "album" and meta.get("duration"):
        out["duration"] = meta["duration"]
    if row["type"] == "album" and meta.get("artist_id"):
        out["artist_id"] = meta["artist_id"]
    if row["type"] == "series" and meta.get("missing"):
        out["missing"] = sum(len(m["numbers"]) for m in meta["missing"])
    if seen is not None:
        out["seen"] = seen
    return out


def entry_json(row, progress=None):
    meta = safe_json(row["meta"], {})
    out = {"id": row["id"], "kind": row["kind"], "title": row["title"], "summary": row["summary"], "season": row["season"], "disc": row["disc"],
           "number": row["number"], "duration": row["duration"], "format": meta.get("format", ""), "label": meta.get("label", ""), "sub": meta.get("sub", "")}
    info = meta.get("info") or {}
    if row["kind"] == "video" and info.get("date"):
        out["date"] = info["date"]
    if row["kind"] == "audio":
        out["artist"] = info.get("artist", "")
    if row["kind"] == "book" and info.get("PageCount"):
        out["pages"] = info["PageCount"]
    if progress is not None:
        out["progress"] = {"position": progress["position"], "duration": progress["duration"], "done": bool(progress["done"]), "updated": progress["updated"]}
    return out


def order_key(row):
    return (row["season"] if row["season"] is not None else -1, row["disc"] or 0, row["number"] if row["number"] is not None else 1e9, row["rel"].casefold())


# ---------------------------------------------------------------------------
# Le gestionnaire HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"media_hub/{__version__}"
    timeout = 120
    hub: Hub = None
    routes: list = []

    def log_message(self, fmt, *args):         # le journal ne garde que les erreurs (voir _fail)
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True        # le lecteur est parti en cours de route : rien d'anormal

    # --- Réponses ---------------------------------------------------------
    def send_body(self, body, mime, status=200, headers=None, head=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_json(self, obj, status=200, headers=None):
        self.send_body(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", status, {"Cache-Control": "no-store", **(headers or {})}, self.command == "HEAD")

    def send_file(self, path, mime=None, headers=None):
        """Le fichier, avec les requêtes Range (reprise, avance rapide, lecteur vidéo)."""
        try:
            size = path.stat().st_size
            handle = open(path, "rb")
        except OSError:
            raise HttpError(404, "fichier introuvable")
        with handle:
            mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end, status = 0, size - 1, 200
            header = self.headers.get("Range")
            if header:
                found = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
                if found and (found.group(1) or found.group(2)):
                    if found.group(1):
                        start = int(found.group(1))
                        end = int(found.group(2)) if found.group(2) else size - 1
                    else:
                        start = max(0, size - int(found.group(2)))
                    end = min(end, size - 1)
                    if start >= size or start > end:
                        self.send_body(b"", mime, 416, {"Content-Range": f"bytes */{size}"})
                        return
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            self.connection.settimeout(3600)       # un film mis en pause ne doit pas couper la connexion
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # --- Aiguillage -------------------------------------------------------
    def authorized(self, path):
        if not self.hub.cfg.password:
            return True
        if path in ("/login", "/api/login", "/favicon.svg", "/icon.svg"):
            return True
        cookie = self.headers.get("Cookie", "")
        found = re.search(r"(?:^|;\s*)hub=([0-9a-f]+)", cookie)
        return bool(found) and hmac.compare_digest(found.group(1), self.hub.token())

    def dispatch(self):
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        try:
            if not self.authorized(path):
                if path.startswith("/api/"):
                    raise HttpError(401, "connexion requise")
                self.send_body(b"", "text/plain", 302, {"Location": "/login"})
                return
            for method, pattern, func in self.routes:
                if method == self.command or (self.command == "HEAD" and method == "GET"):
                    found = pattern.fullmatch(path)
                    if found:
                        func(self, found, query)
                        return
            if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
                self.static(path)
                return
            raise HttpError(404, "introuvable")
        except HttpError as e:
            self._fail(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception as e:                        # une requête qui plante ne doit pas arrêter le serveur
            traceback.print_exc(file=sys.stderr)
            self._fail(500, f"erreur interne : {e}")

    do_GET = do_HEAD = do_POST = dispatch

    def _fail(self, status, message):
        try:
            self.send_json({"error": message}, status)
        except OSError:
            self.close_connection = True
        if status == 500 or status > 501:
            print(f"{status} {self.command} {self.path} : {message}", file=sys.stderr)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1 << 20:
            raise HttpError(413, "corps trop gros")
        return safe_json(self.rfile.read(length).decode("utf-8", "replace") if length else "", {})

    def body_bytes(self, limit):
        """Le corps de la requête tel quel (un fichier envoyé) ; 413 au-delà de `limit` octets."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise HttpError(413, f"fichier trop gros (au plus {limit >> 20} Mo)")
        return self.rfile.read(length)

    # --- Fichiers du site -------------------------------------------------
    def static(self, path):
        web = self.hub.cfg.web.resolve()
        name = path.lstrip("/") or "index.html"
        target = (web / name).resolve()
        if web not in target.parents and target != web:
            raise HttpError(404, "introuvable")
        if not target.is_file():
            if "." in name.rsplit("/", 1)[-1]:
                raise HttpError(404, "introuvable")
            target = web / "index.html"          # route de l'application : elle se débrouille côté navigateur
        stamp = f'"{int(target.stat().st_mtime)}-{target.stat().st_size}"'
        if self.headers.get("If-None-Match") == stamp:
            self.send_body(b"", "text/plain", 304, {"ETag": stamp})
            return
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/json", "application/manifest+json", "image/svg+xml"):
            mime += "; charset=utf-8"
        self.send_body(target.read_bytes(), mime, 200, {"ETag": stamp, "Cache-Control": "no-cache"}, self.command == "HEAD")


ROUTES = []


def route(method, pattern):
    def decorator(func):
        ROUTES.append((method, re.compile(pattern), func))
        return func
    return decorator


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------
LOGIN_PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Médiathèque</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e8e8ea;font:18px system-ui,sans-serif}form{display:grid;gap:14px;width:min(360px,86vw)}
h1{font-size:26px;margin:0 0 8px}input,button{font:inherit;padding:14px 16px;border-radius:12px;border:1px solid #2a2c34;background:#1c1e26;color:inherit}button{background:#7cc4ff;color:#0b1220;border:0;font-weight:700;cursor:pointer}
input:focus,button:focus{outline:3px solid #7cc4ff}p{color:#ff8b8b;margin:0;min-height:1.2em}</style>
<form method="post" action="/api/login"><h1>Médiathèque</h1><input type="password" name="password" placeholder="Mot de passe" autofocus autocomplete="current-password"><button>Entrer</button><p>%s</p></form></html>"""


@route("GET", r"/login")
def login_page(h, m, q):
    h.send_body((LOGIN_PAGE % ("Mot de passe incorrect." if q.get("error") else "")).encode("utf-8"), "text/html; charset=utf-8", 200, {"Cache-Control": "no-store"}, h.command == "HEAD")


@route("POST", r"/api/login")
def login(h, m, q):
    length = int(h.headers.get("Content-Length") or 0)
    raw = h.rfile.read(min(length, 4096)).decode("utf-8", "replace")
    password = parse_qs(raw).get("password", [""])[0] if "=" in raw and not raw.lstrip().startswith("{") else safe_json(raw, {}).get("password", "")
    time.sleep(0.4)       # un essai coûte du temps : pas de force brute à pleine vitesse
    if h.hub.cfg.password and hmac.compare_digest(password.encode(), h.hub.cfg.password.encode()):
        cookie = f"hub={h.hub.token()}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"
        h.send_body(b"", "text/plain", 303, {"Location": "/", "Set-Cookie": cookie})
    else:
        h.send_body(b"", "text/plain", 303, {"Location": "/login?error=1"})


@route("GET", r"/api/logout")
def logout(h, m, q):
    h.send_body(b"", "text/plain", 303, {"Location": "/login", "Set-Cookie": "hub=; Path=/; Max-Age=0"})


# ---------------------------------------------------------------------------
# État général, accueil, recherche
# ---------------------------------------------------------------------------
def library_or_404(hub, lib_id):
    lib = hub.cfg.library(lib_id)
    if lib is None:
        raise HttpError(404, "bibliothèque inconnue")
    return lib


def seen_counts(hub, lib_id=None):
    """{item_id: (fichiers vus, fichiers entamés)} : de quoi afficher « vu » ou « 3/12 » sur une vignette."""
    sql = ("SELECT e.item_id AS item, SUM(p.done) AS seen, COUNT(*) AS touched FROM progress p JOIN entries e ON e.id=p.entry_id "
           "WHERE p.profile=? AND e.kind != 'audio'" + (" AND e.lib=?" if lib_id else "") + " GROUP BY e.item_id")
    params = (PROFILE, lib_id) if lib_id else (PROFILE,)
    return {r["item"]: (r["seen"] or 0, r["touched"]) for r in hub.db.query(sql, params)}


def with_seen(row, seen):
    done, touched = seen.get(row["id"], (0, 0))
    out = summary(row)
    if touched:
        out["seen"] = done
        out["started"] = touched - done
    return out


@route("GET", r"/api/state")
def api_state(h, m, q):
    hub = h.hub
    libs = []
    for lib in hub.cfg.libraries:
        count = hub.db.one("SELECT COUNT(*) AS n FROM items WHERE lib=?", (lib.id,))["n"]
        libs.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "count": count, "scanned": hub.db.kv_get(f"scanned:{lib.id}"), "exists": lib.path.is_dir()})
    h.send_json({"version": __version__, "libraries": libs, "ffmpeg": bool(hub.cfg.ffmpeg), "ffprobe": bool(hub.cfg.ffprobe), "tmdb": bool(hub.cfg.tmdb_key),
                 "auth": bool(hub.cfg.password), "busy": hub.jobs.busy(), "streams": hub.active, "max_streams": hub.cfg.max_streams})


def next_entry(hub, entry):
    """Le fichier qui suit celui-ci dans son élément (épisode suivant, tome suivant, partie suivante), ou None."""
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (entry["item_id"],)), key=order_key)
    for index, row in enumerate(rows):
        if row["id"] == entry["id"]:
            return rows[index + 1] if index + 1 < len(rows) else None
    return None


def dismiss_key(item_id):
    return f"dismiss:{PROFILE}:{item_id}"


def continue_row(hub):
    """Ce qu'on a commencé : le film ou l'épisode à reprendre, l'épisode qui suit celui qu'on vient de finir, le tome suivant."""
    rows = hub.db.query(
        "SELECT p.entry_id, p.position, p.duration AS pduration, p.done, p.updated, e.* FROM progress p JOIN entries e ON e.id=p.entry_id "
        "WHERE p.profile=? AND e.kind IN ('video','book') ORDER BY p.updated DESC LIMIT 200", (PROFILE,))
    seen_items, out = set(), []
    for row in rows:
        if row["item_id"] in seen_items:
            continue
        seen_items.add(row["item_id"])
        if row["updated"] <= hub.db.kv_get(dismiss_key(row["item_id"]), -1):
            continue
        item = hub.db.one("SELECT * FROM items WHERE id=?", (row["item_id"],))
        if item is None:
            continue
        entry, resume = row, True
        if row["done"]:
            if item["type"] == "movie":
                continue
            following = next_entry(hub, row)
            if following is None:
                continue
            progress = hub.db.progress_for(PROFILE, [following["id"]]).get(following["id"])
            if progress is not None and progress["done"]:
                continue
            entry, resume = following, False
            position, total = (progress["position"], progress["duration"]) if progress else (0, 0)
        else:
            if row["position"] < (1 if row["kind"] == "book" else RESUME_MIN):
                continue
            position, total = row["position"], row["pduration"]
        card = entry_json(entry)
        card.update({"item": summary(item), "position": position, "total": total or entry["duration"], "resume": resume, "updated": row["updated"]})
        out.append(card)
        if len(out) >= 24:
            break
    return out


@route("GET", r"/api/home")
def api_home(h, m, q):
    hub = h.hub
    seen = seen_counts(hub)
    rows = []
    for lib in hub.cfg.libraries:
        recent = hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY added DESC, sort LIMIT 24", (lib.id,))
        rows.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "recent": [with_seen(r, seen) for r in recent],
                     "count": hub.db.one("SELECT COUNT(*) AS n FROM items WHERE lib=?", (lib.id,))["n"]})
    h.send_json({"continue": continue_row(hub), "libraries": rows})


@route("GET", r"/api/search")
def api_search(h, m, q):
    needle = fold(q.get("q", "")).strip()
    if len(needle) < 2:
        h.send_json({"results": []})
        return
    hub = h.hub
    seen = seen_counts(hub)
    results = []
    for lib in hub.cfg.libraries:
        found = []
        for row in hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY sort", (lib.id,)):
            if needle in fold(row["title"]) or needle in fold(row["grp"]):
                found.append(with_seen(row, seen))
                if len(found) >= 40:
                    break
        if found:
            results.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "items": found})
    h.send_json({"results": results})


# ---------------------------------------------------------------------------
# Une bibliothèque
# ---------------------------------------------------------------------------
SORTS = {
    "title": (lambda r: r["sort"], False),
    "added": (lambda r: (r["added"], r["sort"]), True),
    "year": (lambda r: (r["year"] or "0000", r["sort"]), True),
    "year-asc": (lambda r: (r["year"] or "9999", r["sort"]), False),
    "part": (lambda r: (part_of(r) is None, part_of(r) or 0, r["year"] or "9999", r["sort"]), False),     # l'ordre d'une saga : film 1, film 2...
}
# Le tri « Titre » de la musique et des livres range par artiste / collection ; chez un artiste, les albums vont du plus ancien au plus récent, puis par nom (ceux sans année en dernier)
TITLE_SORTS = {
    "music": lambda r: (fold(r["grp"]) or "￿", r["year"] or "9999", r["sort"]),
    "books": lambda r: (fold(r["grp"]), r["sort"]),
}


def part_of(row):
    return safe_json(row["meta"], {}).get("part")


def section_of(row):
    """La section d'une série de livres (le dossier de premier niveau : Manga, Comics, Artbook…) ; « Autres » quand rien n'est indiqué."""
    return safe_json(row["meta"], {}).get("section") or "Autres"


def sections_json(counts):
    """Les sections dans l'ordre de l'étagère (Manga, BD, Comics… puis les autres par ordre alphabétique, « Autres » en dernier)."""
    def rank(name):
        if name == shelf.LOOSE_SECTION:
            return (2, 0, "")
        if name in shelf.SECTION_ORDER:
            return (0, shelf.SECTION_ORDER.index(name), "")
        return (1, 0, fold(name))
    return [[name, counts[name]] for name in sorted(counts, key=rank)]


def film_seen(row, seen):
    return seen.get(row["id"], (0, 0))[0] >= max(1, row["count"])


def collapse_sagas(rows, seen):
    """Les films d'une même saga (au moins deux dans la bibliothèque) deviennent une seule vignette ; les autres films restent seuls.

    Une vignette de saga est un dictionnaire qui a les mêmes clés qu'une ligne de la base, plus `members` : les films, dans l'ordre de la saga.
    """
    groups = {}
    for row in rows:
        if row["type"] == "movie" and row["grp"]:
            groups.setdefault(row["grp"], []).append(row)
    out = [row for row in rows if not (row["type"] == "movie" and row["grp"] and len(groups[row["grp"]]) > 1)]
    for name, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=SORTS["part"][0])
        years = sorted(m["year"] for m in members if m["year"])
        genres = []
        for m in members:
            genres += [g for g in safe_json(m["genres"], []) if g not in genres]
        out.append({"id": make_id(members[0]["lib"], "saga:" + name), "lib": members[0]["lib"], "type": "saga", "title": name, "sort": fold(name), "letter": initial(name),
                    "year": years[0] if years else "", "years": [years[0], years[-1]] if years else [], "summary": "", "genres": json.dumps(genres, ensure_ascii=False), "grp": "",
                    "meta": "{}", "added": max(m["added"] for m in members), "count": len(members), "members": members,
                    "all_seen": all(film_seen(m, seen) for m in members)})
    return out


def saga_json(saga, seen):
    done = sum(1 for m in saga["members"] if film_seen(m, seen))
    started = sum(1 for m in saga["members"] if not film_seen(m, seen) and seen.get(m["id"], (0, 0))[1])
    out = {"id": saga["id"], "lib": saga["lib"], "type": "saga", "title": saga["title"], "year": saga["year"], "years": saga["years"], "grp": "", "n": saga["count"], "letter": saga["letter"],
           "added": saga["added"], "genres": safe_json(saga["genres"], []), "covers": [m["id"] for m in saga["members"][:3]]}
    if done:
        out["seen"] = done
    if started:
        out["started"] = started
    return out


@route("GET", r"/api/library/([\w-]+)")
def api_library(h, m, q):
    hub = h.hub
    lib = library_or_404(hub, m.group(1))
    rows = hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,))
    seen = seen_counts(hub, lib.id)
    sagas = lib.kind == "movies" and q.get("sagas") == "1" and not q.get("group")
    if sagas:
        rows = collapse_sagas(rows, seen)
    letters, genres, sections, of = {}, {}, {}, {}
    books = lib.kind == "books"
    for row in rows:
        letters[row["letter"]] = letters.get(row["letter"], 0) + 1
        for genre in safe_json(row["genres"], []):
            genres[genre] = genres.get(genre, 0) + 1
        if books:
            of[row["id"]] = section_of(row)
            sections[of[row["id"]]] = sections.get(of[row["id"]], 0) + 1
    section = q.get("section", "") if books else ""
    needle = fold(q.get("q", "")).strip()
    letter, genre, group = q.get("letter", ""), q.get("genre", ""), q.get("group", "")
    unseen = q.get("filter") == "unseen"
    chosen = []
    for row in rows:
        is_saga = row["type"] == "saga"
        if letter and row["letter"] != letter:
            continue
        if group and row["grp"] != group:
            continue
        if section and of.get(row["id"]) != section:
            continue
        if genre and genre not in safe_json(row["genres"], []):
            continue
        if needle and needle not in fold(row["title"]) and needle not in fold(row["grp"]) and not (is_saga and any(needle in fold(f["title"]) for f in row["members"])):
            continue
        if unseen and (row["all_seen"] if is_saga else seen.get(row["id"], (0, 0))[0] >= max(1, row["count"])):
            continue
        chosen.append(row)
    sort = q.get("sort", "title")
    key, reverse = SORTS.get(sort, SORTS["title"])
    if sort == "title" and lib.kind in TITLE_SORTS:
        key = TITLE_SORTS[lib.kind]
    chosen.sort(key=key, reverse=reverse)
    try:
        offset, limit = max(0, int(q.get("offset", 0))), min(600, max(1, int(q.get("limit", 240))))
    except ValueError:
        offset, limit = 0, 240
    page = chosen[offset:offset + limit]
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "total": len(chosen), "offset": offset,
           "items": [saga_json(r, seen) if r["type"] == "saga" else with_seen(r, seen) for r in page],
           "letters": sorted(letters.items(), key=lambda kv: (kv[0] == "#", kv[0])), "genres": sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:60]}
    if books:
        out["sections"] = sections_json(sections)
    if sagas:
        out["sagas"] = sum(1 for r in chosen if r["type"] == "saga")
        out["films"] = sum(r["count"] if r["type"] == "saga" else 1 for r in chosen)
    h.send_json(out)


# ---------------------------------------------------------------------------
# Un élément : film, série, album, série de livres
# ---------------------------------------------------------------------------
def resume_target(rows, progress):
    """Le fichier à proposer en premier : celui entamé, sinon le premier non vu, sinon le premier."""
    started = [(progress[r["id"]]["updated"], r) for r in rows if r["id"] in progress and not progress[r["id"]]["done"] and progress[r["id"]]["position"] > 0]
    if started:
        return max(started, key=lambda t: t[0])[1]["id"]
    for row in rows:
        if row["id"] not in progress or not progress[row["id"]]["done"]:
            return row["id"]
    return rows[0]["id"] if rows else None


@route("GET", r"/api/item/(\w+)")
def api_item(h, m, q):
    hub = h.hub
    item = hub.item(m.group(1))
    lib = library_or_404(hub, item["lib"])
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (item["id"],)), key=order_key)
    progress = hub.db.progress_for(PROFILE, [r["id"] for r in rows])
    meta = safe_json(item["meta"], {})
    out = summary(item)
    out.update({"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "summary": item["summary"], "tagged": meta.get("tagged", False)})
    for key in ("directors", "writers", "actors", "artist", "artist_id", "label", "catalog", "formats", "authors", "publisher", "section", "collection", "status", "episodes_total",
                "seasons_total", "tmdb", "missing", "unit", "units", "years", "discs", "season_summaries"):
        if meta.get(key) not in (None, "", []):
            out[key] = meta[key]
    out["entries"] = [entry_json(r, progress.get(r["id"])) for r in rows]
    out["resume"] = resume_target(rows, progress)
    if item["type"] == "show":
        out["seasons"] = meta.get("seasons", [])
    if item["type"] == "movie" and item["grp"]:                 # la saga : les autres films qui la composent
        saga = hub.db.query("SELECT * FROM items WHERE lib=? AND grp=? ORDER BY sort", (item["lib"], item["grp"]))
        seen = seen_counts(hub, lib.id)
        out["saga"] = sorted((with_seen(r, seen) for r in saga), key=lambda r: (r.get("part") is None, r.get("part") or 0, r["title"]))
    if item["type"] == "album":
        out["tracks_total"] = len(rows)
    h.send_json(out)


def artist_sheet(info, mbid=""):
    """Ce que la fiche montre d'un artiste, à partir de ce que MusicBrainz et Wikipédia en ont dit (voir libraries.music.artistinfo)."""
    members = [{"name": mem["name"], "roles": mem.get("roles", []), "period": artistinfo.period(mem), "former": bool(mem.get("ended"))} for mem in info.get("members") or []]
    links = enrich.pick_links(info.get("links", []))       # aussi à l'affichage : les fiches déjà gardées gardent peut-être d'autres liens
    if (info.get("wiki") or {}).get("url"):
        links.append({"label": "Wikipédia", "url": info["wiki"]["url"]})
    if mbid:
        links.append({"label": "MusicBrainz", "url": musicbrainz.artist_url(mbid)})
    return {"facts": artistinfo.facts(info), "disambiguation": info.get("disambiguation", ""), "wiki": info.get("wiki"), "members": members, "links": links}


def artist_albums(hub, lib_id, artist_id):
    """Les albums d'un artiste, du plus ancien au plus récent ; 404 si la bibliothèque n'est pas de la musique ou si l'artiste n'y est pas."""
    lib = library_or_404(hub, lib_id)
    if lib.kind != "music":
        raise HttpError(404, "pas une bibliothèque de musique")
    rows = [r for r in hub.db.query("SELECT * FROM items WHERE lib=? AND type='album'", (lib.id,)) if safe_json(r["meta"], {}).get("artist_id") == artist_id]
    if not rows:
        raise HttpError(404, "artiste inconnu")
    return lib, sorted(rows, key=TITLE_SORTS["music"])


def artist_kept(hub, rows):
    """La fiche gardée par « Fiches artistes » (voir enrich.enrich_artists), ou None : celle du premier album qui porte un identifiant MusicBrainz et qui en a une."""
    for row in rows:
        mbid = safe_json(row["meta"], {}).get("artist_mbid")
        kept = hub.db.kv_get(enrich.artist_key(mbid)) if mbid else None
        if kept:
            return {**kept, "mbid": mbid}
    return None


PHOTO_UPLOAD_MAX = 10 << 20
PHOTO_TYPES = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"), (b"RIFF", ".webp"))


def custom_photos(hub, artist_id):
    """Les photos que l'utilisateur a envoyées pour cet artiste (une seule en pratique) : data/artists/custom-<id>.<ext>. Elles passent avant celle de Wikipédia et un « Fiches artistes » n'y touche pas."""
    return sorted((hub.cfg.data / "artists").glob(f"custom-{artist_id}.*"))


def artist_photo(hub, artist_id, kept):
    """Le fichier de la photo d'un artiste : celle qu'on lui a envoyée, sinon celle de Wikipédia (téléchargée par « Fiches artistes ») ; None s'il n'en a pas."""
    mine = custom_photos(hub, artist_id)
    if mine:
        return mine[0]
    name = ((kept or {}).get("info") or {}).get("photo")
    path = hub.cfg.data / "artists" / name if name and name == os.path.basename(name) else None
    return path if path and path.is_file() else None


@route("GET", r"/api/artist/([\w-]+)/(\w+)")
def api_artist(h, m, q):
    """La fiche d'un artiste ou d'un groupe : ses albums de la médiathèque, de quoi tout lire, ses genres, et ce que MusicBrainz et Wikipédia en disent, photo comprise (une fois les fiches demandées dans Gestion)."""
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    seen = seen_counts(hub, lib.id)
    name = rows[0]["grp"] or "Artiste inconnu"
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "id": m.group(2), "name": name, "albums": [with_seen(r, seen) for r in rows], "tracks": []}
    genres = {}
    for row in rows:
        for entry in sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (row["id"],)), key=order_key):
            out["tracks"].append({"id": entry["id"], "title": entry["title"], "artist": entry_json(entry)["artist"] or name, "album": row["title"], "item": row["id"], "duration": entry["duration"]})
        for genre in safe_json(row["genres"], []):
            genres[genre] = genres.get(genre, 0) + 1
    out["genres"] = [g for g, _ in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    out["duration"] = sum(t["duration"] or 0 for t in out["tracks"])
    kept = artist_kept(hub, rows)
    if kept:
        out["info"] = artist_sheet(kept["info"], kept["mbid"])
    out["photo"] = artist_photo(hub, m.group(2), kept) is not None
    out["own_photo"] = bool(custom_photos(hub, m.group(2)))
    h.send_json(out)


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo")
def api_artist_photo_set(h, m, q):
    """Remplace la photo d'un artiste par l'image envoyée (le corps de la requête : JPEG, PNG ou WebP, 10 Mo au plus)."""
    hub = h.hub
    artist_albums(hub, m.group(1), m.group(2))
    data = h.body_bytes(PHOTO_UPLOAD_MAX)
    ext = next((ext for magic, ext in PHOTO_TYPES if data.startswith(magic)), None)
    if not ext or (ext == ".webp" and data[8:12] != b"WEBP"):
        raise HttpError(415, "ce n'est pas une image JPEG, PNG ou WebP")
    folder = hub.cfg.data / "artists"
    folder.mkdir(parents=True, exist_ok=True)
    for old in custom_photos(hub, m.group(2)):
        old.unlink()
    (folder / f"custom-{m.group(2)}{ext}").write_bytes(data)
    h.send_json({"ok": True})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo/delete")
def api_artist_photo_delete(h, m, q):
    """Retire la photo envoyée : l'artiste retrouve celle de Wikipédia, ou la pochette de son premier album."""
    hub = h.hub
    artist_albums(hub, m.group(1), m.group(2))
    h.body_json()
    removed = custom_photos(hub, m.group(2))
    for old in removed:
        old.unlink()
    h.send_json({"ok": True, "removed": len(removed)})


@route("GET", r"/img/artist/([\w-]+)/(\w+)")
def artist_image(h, m, q):
    """La photo d'un artiste, réduite comme les pochettes ; 404 s'il n'en a pas (la page garde alors la pochette de son premier album)."""
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    path = artist_photo(hub, m.group(2), artist_kept(hub, rows))
    try:
        width = int(q.get("w", 0))
    except ValueError:
        width = 0
    found = hub.images.file(f"artist:{lib.id}:{m.group(2)}", path, width) if path else None
    if found is None:
        raise HttpError(404, "pas de photo")
    data, mime, key = found
    tag = f'"{key}"'
    if h.headers.get("If-None-Match") == tag:
        h.send_body(b"", mime, 304, {"ETag": tag, "Cache-Control": "private, no-cache"})
        return
    h.send_body(data, mime, 200, {"ETag": tag, "Cache-Control": "private, no-cache"}, h.command == "HEAD")


@route("POST", r"/api/item/(\w+)/seen")
def api_item_seen(h, m, q):
    """Marque tout un élément vu (ou non vu) : un film, une saison, une série entière."""
    hub = h.hub
    item = hub.item(m.group(1))
    body = h.body_json()
    done = bool(body.get("done", True))
    season = body.get("season")
    count = 0
    for row in hub.db.query("SELECT * FROM entries WHERE item_id=?", (item["id"],)):
        if season is not None and row["season"] != season:
            continue
        if done:
            hub.db.save_progress(PROFILE, row["id"], row["duration"], row["duration"], True)
        else:
            hub.db.execute("DELETE FROM progress WHERE profile=? AND entry_id=?", (PROFILE, row["id"]))
        count += 1
    h.send_json({"ok": True, "count": count})


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------
def playlist_call(func, *args):
    try:
        return func(*args)
    except playlists.PlaylistError as e:
        raise HttpError(e.status, e.message)


@route("GET", r"/api/playlists")
def api_playlists(h, m, q):
    h.send_json({"playlists": playlists.listing(h.hub.db, PROFILE)})


@route("POST", r"/api/playlists")
def api_playlist_create(h, m, q):
    body = h.body_json()
    entries = body.get("entries") if isinstance(body.get("entries"), list) else []
    pid = playlist_call(playlists.create, h.hub.db, PROFILE, body.get("name"), entries)
    h.send_json(playlist_call(playlists.detail, h.hub.db, PROFILE, pid), 201)


@route("GET", r"/api/playlist/(\w+)")
def api_playlist(h, m, q):
    h.send_json(playlist_call(playlists.detail, h.hub.db, PROFILE, m.group(1)))


@route("POST", r"/api/playlist/(\w+)/(add|remove|move|rename|delete)")
def api_playlist_edit(h, m, q):
    db, pid, action, body = h.hub.db, m.group(1), m.group(2), h.body_json()
    if action == "add":
        playlist_call(playlists.add, db, PROFILE, pid, body.get("entries") if isinstance(body.get("entries"), list) else [])
    elif action == "remove":
        playlist_call(playlists.remove, db, PROFILE, pid, body.get("index"))
    elif action == "move":
        playlist_call(playlists.move, db, PROFILE, pid, body.get("from"), body.get("to"))
    elif action == "rename":
        playlist_call(playlists.rename, db, PROFILE, pid, body.get("name"))
    else:
        playlist_call(playlists.delete, db, PROFILE, pid)
        h.send_json({"ok": True})
        return
    h.send_json(playlist_call(playlists.detail, db, PROFILE, pid))


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------
def is_finished(kind, position, duration):
    if duration <= 0:
        return False
    if kind == "book":
        return position >= duration - 1
    return position / duration >= WATCHED_AT or duration - position <= min(WATCHED_TAIL, duration * 0.08)


@route("POST", r"/api/progress")
def api_progress(h, m, q):
    body = h.body_json()
    entry = h.hub.entry(str(body.get("entry", "")))
    try:
        position, duration = max(0.0, float(body.get("position", 0))), max(0.0, float(body.get("duration", 0)))
    except (TypeError, ValueError):
        raise HttpError(400, "position invalide")
    duration = duration or entry["duration"]
    done = bool(body.get("done")) or is_finished(entry["kind"], position, duration)
    if entry["kind"] == "audio":                 # la musique n'a pas de reprise : seulement « écouté »
        done = done or (duration > 0 and position / duration > 0.8)
        position = 0 if done else position
    h.hub.db.save_progress(PROFILE, entry["id"], duration if done and entry["kind"] != "book" else position, duration, done)
    h.send_json({"ok": True, "done": done})


@route("POST", r"/api/item/(\w+)/dismiss")
def api_item_dismiss(h, m, q):
    """Retire un élément de « Reprendre » sans toucher à la progression : il y revient dès qu'on le relance."""
    hub = h.hub
    h.body_json()                 # le corps doit être lu, même vide de sens : sinon il reste sur la connexion et colle à la requête suivante (501)
    item = hub.item(m.group(1))
    latest = hub.db.one("SELECT MAX(p.updated) AS t FROM progress p JOIN entries e ON e.id=p.entry_id WHERE p.profile=? AND e.item_id=?", (PROFILE, item["id"]))
    hub.db.kv_set(dismiss_key(item["id"]), latest["t"] or 0)
    h.send_json({"ok": True})


@route("POST", r"/api/entry/(\w+)/seen")
def api_entry_seen(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    if h.body_json().get("done", True):
        hub.db.save_progress(PROFILE, entry["id"], entry["duration"], entry["duration"], True)
    else:
        hub.db.execute("DELETE FROM progress WHERE profile=? AND entry_id=?", (PROFILE, entry["id"]))
    h.send_json({"ok": True})


# ---------------------------------------------------------------------------
# Lecture vidéo
# ---------------------------------------------------------------------------
def probe_of(hub, entry):
    """Le résumé ffprobe d'un fichier, lu une fois et gardé dans la base."""
    cached = safe_json(entry["probe"], None)
    if cached:
        return cached
    lib, path = hub.path_of(entry)
    try:
        probe = prober.run_ffprobe(hub.cfg.ffprobe, path)
    except prober.ProbeError as e:
        raise HttpError(500, str(e))
    hub.db.set_probe(entry["id"], probe)
    return probe


def track_label(track):
    parts = [subtitles.language(track["lang"]) or (f"Piste {track['index'] + 1}" if not track["title"] else ""), track["title"]]
    codec = track.get("codec", "")
    extra = {"ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS", "truehd": "TrueHD", "aac": "AAC", "flac": "FLAC", "opus": "Opus"}.get(codec, codec.upper())
    channels = {1: "mono", 2: "stéréo", 6: "5.1", 8: "7.1"}.get(track.get("channels"), "")
    detail = " ".join(x for x in (extra, channels) if x)
    return " · ".join(x for x in parts if x) + (f" ({detail})" if detail else "")


def sub_label(track):
    parts = [subtitles.language(track["lang"]) or f"Piste {track['index'] + 1}", track["title"], "Forcé" if track.get("forced") else ""]
    return " · ".join(x for x in parts if x)


def subtitle_list(hub, entry, probe, path):
    out = []
    for sub in probe["subs"]:
        out.append({"id": f"e{sub['index']}", "label": sub_label(sub), "lang": sub["lang"], "forced": sub["forced"], "default": sub["default"], "text": sub["text"], "where": "mkv"})
    for side in subtitles.side_files(path):
        out.append({"id": f"s{side['index']}", "label": side["label"], "lang": side["lang"], "forced": side["forced"], "default": False, "text": True, "where": "fichier"})
    for sub in out:
        sub["url"] = f"/api/subs/{entry['id']}/{sub['id']}.vtt" if sub["text"] else ""
    return out


@route("POST", r"/api/play/(\w+)")
def api_play(h, m, q):
    """Le plan de lecture d'un fichier vidéo pour CE lecteur. Le client l'appelle à l'ouverture, puis à chaque changement de piste audio."""
    hub = h.hub
    entry = hub.entry(m.group(1))
    if entry["kind"] != "video":
        raise HttpError(400, "ce fichier n'est pas une vidéo")
    body = h.body_json()
    lib, path = hub.path_of(entry)
    probe = probe_of(hub, entry)
    caps = body.get("caps") or {}
    try:
        audio = int(body["audio"]) if body.get("audio") is not None else None
        kbps, height = int(body.get("max_kbps") or 0), int(body.get("max_height") or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "réglages invalides")
    decision = prober.plan(probe, caps, audio, kbps, height)
    chosen = decision["audio"]
    video = probe.get("video") or {}
    if decision["mode"] == "transcode" and not height and video.get("height", 0) > 1080:
        height = 1080                  # convertir un 4K en pleine taille ferait ramer le processeur pour rien : 1080p par défaut
    base = f"/media/{entry['id']}"
    if decision["mode"] == "direct":
        url = f"{base}/file"
    else:
        args = {"m": decision["mode"], "ca": int(decision["copy_audio"]), "k": kbps, "h": height, "c": min(int(caps.get("channels") or 2), 6)}
        if chosen is not None:
            args["a"] = chosen
        url = f"{base}/stream?" + "&".join(f"{k}={v}" for k, v in args.items())
    warning = ""
    if decision["mode"] == "transcode" and video.get("height", 0) >= 2000 and video.get("codec") in ("hevc", "av1"):
        warning = "Image 4K à convertir : le serveur n'a pas de carte vidéo pour cela, la lecture risque de saccader. Le débit demandé peut être baissé."
    progress = hub.db.progress_for(PROFILE, [entry["id"]]).get(entry["id"])
    resume = progress["position"] if progress and not progress["done"] and progress["position"] >= RESUME_MIN else 0
    following = next_entry(hub, entry)
    duration = probe["duration"] or entry["duration"]
    item = hub.db.one("SELECT title, type FROM items WHERE id=?", (entry["item_id"],))
    h.send_json({
        "entry": entry["id"], "item": entry["item_id"], "title": entry["title"], "show": item["title"] if item and item["type"] == "show" else "", "season": entry["season"], "number": entry["number"],
        "mode": decision["mode"], "why": decision["why"], "url": url, "seekable": decision["mode"] == "direct", "duration": duration, "resume": resume, "warning": warning,
        "audio": [{"index": t["index"], "label": track_label(t), "lang": t["lang"], "selected": t["index"] == chosen, "commentary": t["commentary"]} for t in probe["audio"]],
        "subs": subtitle_list(hub, entry, probe, path),
        "video": {"codec": video.get("codec"), "width": video.get("width"), "height": video.get("height"), "hdr": video.get("hdr")} if video else None,
        "next": {"id": following["id"], "title": following["title"], "season": following["season"], "number": following["number"]} if following and following["kind"] == "video" else None,
    })


@route("GET", r"/media/(\w+)/file")
def media_file(h, m, q):
    entry = h.hub.entry(m.group(1))
    lib, path = h.hub.path_of(entry)
    extra = {"Cache-Control": "private, max-age=3600"}
    if q.get("download"):
        extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(path.name)
    h.send_file(path, headers=extra)


@route("GET", r"/media/(\w+)/stream")
def media_stream(h, m, q):
    """Un MP4 fragmenté produit à la volée. Pas de Content-Length, pas de Range : pour avancer, le client redemande avec ?start=."""
    hub = h.hub
    entry = hub.entry(m.group(1))
    lib, path = hub.path_of(entry)
    if not hub.cfg.ffmpeg:
        raise HttpError(501, "ffmpeg est introuvable sur le serveur : seule la lecture directe est possible.")
    probe = probe_of(hub, entry)
    mode = "transcode" if q.get("m") == "transcode" else "remux"

    def number(name, cast=int, default=0):
        try:
            return cast(q.get(name, default))
        except ValueError:
            raise HttpError(400, f"paramètre {name} invalide")
    audio = number("a") if "a" in q else None
    decision = {"mode": mode, "audio": audio if probe["audio"] else None, "copy_video": mode == "remux", "copy_audio": mode == "remux" and number("ca", int, 1) == 1}
    cmd = prober.ffmpeg_command(hub.cfg.ffmpeg, path, probe, decision, start=max(0.0, number("start", float, 0.0)), max_kbps=number("k"), max_height=number("h"),
                                channels=max(1, min(6, number("c", int, 2))))
    if not hub.streams.acquire(blocking=False):
        raise HttpError(503, f"{hub.cfg.max_streams} conversions en cours : attends la fin de l'une d'elles.")
    hub.active += 1
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        errors = []
        threading.Thread(target=lambda: errors.append(proc.stderr.read().decode("utf-8", "replace")), daemon=True).start()
        first = proc.stdout.read1(CHUNK)
        if not first:
            proc.wait(timeout=5)
            time.sleep(0.05)
            raise HttpError(500, "ffmpeg n'a rien produit : " + ((errors[0].strip().splitlines() or ["erreur inconnue"])[-1] if errors else "erreur inconnue"))
        h.send_response(200)
        h.send_header("Content-Type", "video/mp4")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Accept-Ranges", "none")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        if h.command == "HEAD":
            return
        h.connection.settimeout(3600)
        h.wfile.write(first)
        while True:
            chunk = proc.stdout.read1(CHUNK)
            if not chunk:
                break
            h.wfile.write(chunk)
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.SubprocessError:
                pass
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
        hub.active -= 1
        hub.streams.release()


@route("GET", r"/api/subs/(\w+)/([es])(\d+)\.vtt")
def api_subs(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    lib, path = hub.path_of(entry)
    kind, index = m.group(2), int(m.group(3))
    cache = hub.cfg.data / "subs"
    cache.mkdir(parents=True, exist_ok=True)
    stamp = f"{entry['id']}-{entry['mtime']}-{kind}{index}.vtt"
    target = cache / stamp
    if not target.is_file():
        try:
            if kind == "e":
                text = subtitles.ffmpeg_vtt(hub.cfg.ffmpeg, path, index)
            else:
                sides = subtitles.side_files(path)
                if index >= len(sides):
                    raise HttpError(404, "sous-titres introuvables")
                text = subtitles.side_vtt(hub.cfg.ffmpeg, sides[index]["path"])
        except HttpError:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise HttpError(500, str(e))
        target.write_text(text, encoding="utf-8")
    h.send_body(target.read_bytes(), "text/vtt; charset=utf-8", 200, {"Cache-Control": "private, max-age=86400"}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
@route("GET", r"/img/(item|entry)/(\w+)")
def image(h, m, q):
    try:
        width = int(q.get("w", 0))
    except ValueError:
        width = 0
    found = h.hub.images.get(m.group(1), m.group(2), width)
    if found is None:
        raise HttpError(404, "pas d'image")
    data, mime, key = found
    tag = f'"{key}"'
    if h.headers.get("If-None-Match") == tag:
        h.send_body(b"", mime, 304, {"ETag": tag, "Cache-Control": "private, no-cache"})
        return
    h.send_body(data, mime, 200, {"ETag": tag, "Cache-Control": "private, no-cache"}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Livres
# ---------------------------------------------------------------------------
@route("GET", r"/api/book/(\w+)")
def api_book(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    if entry["kind"] != "book":
        raise HttpError(400, "ce fichier n'est pas un livre")
    lib, path = hub.path_of(entry)
    kind = safe_json(entry["meta"], {}).get("format", "")
    progress = hub.db.progress_for(PROFILE, [entry["id"]]).get(entry["id"])
    out = {"id": entry["id"], "item": entry["item_id"], "title": entry["title"], "format": kind, "position": progress["position"] if progress and not progress["done"] else 0,
           "next": None}
    try:
        if kind == "cbz":
            out["pages"] = len(bookmedia.cbz_pages(path))
        elif kind == "epub":
            spine = bookmedia.epub_spine(path)
            out["chapters"] = [f"/book/{entry['id']}/epub/{c}" for c in spine["spine"]]
        else:
            out["readable"] = False
    except BookError as e:
        raise HttpError(422, str(e))
    following = next_entry(hub, entry)
    if following is not None:
        out["next"] = {"id": following["id"], "title": following["title"]}
    h.send_json(out)


@route("GET", r"/book/(\w+)/page/(\d+)")
def book_page(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    lib, path = hub.path_of(entry)
    try:
        data = bookmedia.cbz_page(path, int(m.group(2)))
    except BookError as e:
        raise HttpError(404, str(e))
    h.send_body(data, sniff(data), 200, {"Cache-Control": "private, max-age=86400"}, h.command == "HEAD")


@route("GET", r"/book/(\w+)/epub/(.+)")
def book_epub(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    lib, path = hub.path_of(entry)
    try:
        data, mime = bookmedia.epub_member(path, m.group(2))
    except BookError as e:
        raise HttpError(404, str(e))
    if mime in ("application/xhtml+xml", "text/html", "image/svg+xml") or data[:5] == b"<?xml":
        mime = "application/xhtml+xml" if mime != "image/svg+xml" and bookmedia.valid_xml(data) else ("text/html" if mime != "image/svg+xml" else mime)
    if mime.startswith("text/") or mime.endswith("xml") or mime.endswith("css"):
        mime += "; charset=utf-8"
    # Le contenu d'un livre est celui d'un tiers : pas de script, pas de requête vers l'extérieur.
    csp = "default-src 'self' data: blob:; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; connect-src 'none'"
    h.send_body(data, mime, 200, {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": csp}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------
@route("GET", r"/api/jobs")
def api_jobs(h, m, q):
    h.send_json({"jobs": h.hub.jobs.snapshot(with_log=q.get("log") == "1"), "busy": h.hub.jobs.busy()})


@route("POST", r"/api/scan")
def api_scan(h, m, q):
    body = h.body_json()
    jobs = h.hub.jobs
    job = jobs.scan(library_or_404(h.hub, body["lib"])) if body.get("lib") else jobs.scan_all()
    h.send_json({"job": job.public()}, 202)


@route("POST", r"/api/enrich")
def api_enrich(h, m, q):
    body = h.body_json()
    lib = library_or_404(h.hub, str(body.get("lib", "")))
    if lib.kind == "music":                     # MusicBrainz et Wikipédia : pas de clé
        h.send_json({"job": h.hub.jobs.enrich_artists(lib, bool(body.get("force"))).public()}, 202)
        return
    if lib.kind != "shows":
        raise HttpError(400, "l'enrichissement ne concerne que les séries, les animations et la musique")
    if not h.hub.cfg.tmdb_key:
        raise HttpError(400, "pas de clé TMDB : renseigne TMDB_KEY sur le serveur")
    h.send_json({"job": h.hub.jobs.enrich(lib, bool(body.get("force"))).public()}, 202)


@route("POST", r"/api/cleanup")
def api_cleanup(h, m, q):
    h.send_json({"job": h.hub.jobs.cleanup(bool(h.body_json().get("apply"))).public()}, 202)


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(cfg):
    hub = Hub(cfg)
    handler = type("BoundHandler", (Handler,), {"hub": hub, "routes": ROUTES})
    server = Server((cfg.bind, cfg.port), handler)
    server.hub = hub
    return server
