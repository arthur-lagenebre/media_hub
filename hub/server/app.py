"""Le serveur HTTP : l'API JSON, le streaming des fichiers, et l'interface web (hub/web).

Bibliothèque standard uniquement. Un fil par connexion (ThreadingHTTPServer) : un film qui se lit n'empêche pas de feuilleter.

Pas de chemin de fichier dans l'API : le client ne connaît que des identifiants, le serveur retrouve le fichier dans la base. Un identifiant inconnu est un 404, jamais une lecture de disque.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

from libraries.books import shelf
from libraries.books.archive import BookError
from libraries.common.text import fold, initial
from libraries.music import artistinfo, musicbrainz
from libraries.video import tmdb

from .. import __version__
from .. import accounts
from .. import enrich
from .. import missing
from .. import playlists
from .. import urls
from .. import sagas as sagafile
from ..sagas import Sagas
from ..db import Database, make_id
from ..media import books as bookmedia
from ..media import probe as prober
from ..media import subs as subtitles
from ..media.images import ConvertError, Images, NoConverter, crop_box, sniff, write_atomic
from .jobs import Jobs

API_VERSION = 1             # le contrat décrit dans docs/api.md ; n'augmente qu'en cas de rupture (champ retiré, renommé ou de sens différent)
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


def require_admin(h):
    """Les tâches de gestion (scan, enrichissement, nettoyage) et les photos d'artistes modifient l'index ou le disque : réservées au rôle admin."""
    if h.user is None or h.user["role"] != "admin":
        h.discard_body()
        raise HttpError(403, "réservé à l'administrateur")


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
        self.sagas = Sagas(cfg.data / "sagas.json")
        self.throttle = accounts.Throttle()                    # les essais de connexion ratés, par adresse
        self.streams = threading.BoundedSemaphore(cfg.max_streams)
        self.active = 0
        self.secret = self._secret()
        self.started = time.time()
        accounts.seed(self.db, cfg)

    def _secret(self):
        value = self.db.kv_get("secret")
        if not value:
            value = secrets.token_hex(32)
            self.db.kv_set("secret", value)
        return value

    def session_token(self, row):
        """Le jeton de session d'un compte (cookie `hub=` ou `Authorization: Bearer`) : `<identifiant>.<mac>`, voir accounts.py."""
        return accounts.session_token(self.secret, row)

    def auth(self):
        """Vrai dès qu'il existe un compte. Sans compte (ni HUB_PASSWORD au premier démarrage), le serveur est ouvert à tous."""
        return accounts.exist(self.db)

    def open_user(self):
        """Qui est « connecté » quand le serveur est ouvert : le profil `default`, administrateur (c'est lui que le premier compte reprendra)."""
        return {"id": accounts.FIRST_ID, "name": self.cfg.admin, "role": "admin"}

    def media_token(self):
        """Le jeton qui voyage dans les URLs (`?token=`) : il ne vaut que sur /media, /img, /book et /api/subs (sous-titres, lecture seule), nulle part ailleurs sur /api/ ;
        une URL de pochette qui fuit ne donne donc ni le nettoyage ni le scan."""
        return hmac.new(self.secret.encode(), b"hub-media", hashlib.sha256).hexdigest()

    def path_of(self, entry):
        lib = self.cfg.library(entry["lib"])
        if lib is None:
            raise HttpError(404, "bibliothèque inconnue")
        root = Path(os.path.normpath(lib.path))
        path = Path(os.path.normpath(root / entry["rel"]))
        if path != root and root not in path.parents:               # `/srv/Musique2` n'est pas dans `/srv/Musique`
            raise HttpError(400, "chemin refusé")
        return lib, path

    def scrub(self, text):
        """Le message sans les chemins du serveur (dossiers des bibliothèques, HUB_DATA, media_library) : ffprobe, ffmpeg et les OSError les écrivent tels quels, et la réponse part à n'importe quel compte connecté."""
        text = str(text)
        roots = [Path(lib.path) for lib in self.cfg.libraries] + [Path(self.cfg.data)] + ([Path(self.cfg.repo)] if self.cfg.repo else [])
        forms = {form for root in roots for form in (str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()) if len(form) > 3}
        for form in sorted(forms, key=len, reverse=True):          # le plus long d'abord : un dossier peut en contenir un autre
            text = text.replace(form, "…")
        return text

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
           "added": row["added"], "genres": safe_json(row["genres"], []), "cover": urls.item_cover(row["id"])}
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
           "number": row["number"], "duration": row["duration"], "format": meta.get("format", ""), "label": meta.get("label", ""), "sub": meta.get("sub", ""),
           "file": urls.entry_file(row["id"])}
    if row["kind"] != "audio":                 # une piste n'a pas d'image à elle : sa pochette est celle de l'album (voir `cover` des pistes)
        out["image"] = urls.entry_image(row["id"])
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
    user: dict = None                          # le compte de la requête en cours (voir authorized)
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
            try:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            finally:
                self.connection.settimeout(self.timeout)       # la connexion persiste : un client inactif ne garde pas un fil une heure de plus

    # --- Aiguillage -------------------------------------------------------
    def authorized(self, path, query):
        """Trois façons de se présenter : le jeton de session (`<identifiant>.<mac>`) en cookie (navigateur) ou en `Authorization: Bearer` (clients natifs),
        le jeton de média en `?token=` (balises <img> / <video>, Cast), celui-ci seulement sur les fichiers et les sous-titres (/api/subs/), nulle part ailleurs sur /api/.

        Le compte reconnu est posé sur `self.user` (`{"id", "name", "role"}`) : c'est lui, et lui seul, qui désigne le profil de la progression et des playlists.
        Avec le jeton de média, `self.user` reste None : les routes de fichiers n'en ont pas besoin."""
        self.user = None
        if not self.hub.auth():
            self.user = self.hub.open_user()
            return True
        if path in ("/login", "/api/login", "/api/token", "/favicon.svg", "/icon.svg"):
            return True
        cookie = self.headers.get("Cookie", "")
        found = re.search(r"(?:^|;\s*)hub=(\w{1,64}\.[0-9a-f]{64})\s*(?:;|$)", cookie, re.ASCII)       # valeur entière, sans rien derrière : `hub=<id>.<mac>.junk` ne passe pas
        bearer = re.fullmatch(r"Bearer\s+(\S+)", self.headers.get("Authorization", "").strip(), re.IGNORECASE)
        for token in (found.group(1) if found else None, bearer.group(1) if bearer else None):
            row = accounts.user_for_token(self.hub.db, self.hub.secret, token) if token else None
            if row is not None:
                self.user = accounts.public(row)
                return True
        if path.startswith(("/media/", "/img/", "/book/", "/api/subs/")) and "token" in query:
            return hmac.compare_digest(query["token"].encode("utf-8", "replace"), self.hub.media_token().encode())
        return False

    def dispatch(self):
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        self.body_taken = False                       # le corps du POST a-t-il été lu ? sinon _fail le jette (connexion persistante)
        try:
            if not self.authorized(path, query):
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
            if self.command in ("GET", "HEAD") and not path.startswith(("/api/", "/media/", "/img/", "/book/")):         # un chemin inconnu d'un fichier servi est un 404, pas la page d'accueil
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
            self.send_json({"error": self.hub.scrub(message)}, status)
        except OSError:
            self.close_connection = True
        if self.command == "POST" and not self.body_taken:      # refus avant toute lecture (401, 404, route qui lève trop tôt) : le corps ne doit pas coller à la requête suivante
            try:
                self.discard_body()
            except OSError:
                self.close_connection = True
        if status == 500 or status > 501:
            split = urlsplit(self.path)             # le jeton ne va pas dans le journal : masqué d'après les clés décodées (`?%74oken=` aussi)
            pairs = [(k, "***" if k == "token" else v) for k, v in parse_qsl(split.query, keep_blank_values=True)]
            shown = split.path + ("?" + urlencode(pairs, safe="*") if pairs else "")
            print(f"{status} {self.command} {shown} : {message}", file=sys.stderr)

    def content_length(self):
        """La taille annoncée du corps. Illisible ou négative : 400, et la connexion est coupée (on ne sait pas où le corps finit : lire jusqu'à la fin de la connexion bloquerait un fil)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            raise HttpError(400, "Content-Length illisible")
        return length

    def body_json(self):
        """Le corps JSON de la requête, toujours un objet : un texte qui n'en est pas un (une liste, un nombre, du bruit) vaut {}."""
        self.body_taken = True
        length = self.content_length()
        if length > 1 << 20:
            self.close_connection = True            # corps non lu : la connexion ne peut plus servir
            raise HttpError(413, "corps trop gros")
        data = safe_json(self.rfile.read(length).decode("utf-8", "replace") if length else "", {})
        return data if isinstance(data, dict) else {}

    def discard_body(self):
        """Lit et jette le corps d'une requête qu'on refuse sans l'avoir lue : sinon il reste sur la connexion persistante et colle à la requête suivante. Trop gros pour cela : on coupe la connexion."""
        self.body_taken = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 1 << 20:
            self.close_connection = True
        elif length:
            self.rfile.read(length)

    def body_bytes(self, limit):
        """Le corps de la requête tel quel (un fichier envoyé) ; 413 au-delà de `limit` octets (la connexion est alors coupée : le corps n'est pas lu)."""
        self.body_taken = True
        length = self.content_length()
        if length > limit:
            self.close_connection = True
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
<form method="post" action="/api/login"><h1>Médiathèque</h1>%s<input type="password" name="password" placeholder="Mot de passe" autofocus autocomplete="current-password"><button>Entrer</button><p>%s</p></form></html>"""
LOGIN_USER_FIELD = '<input type="text" name="user" placeholder="Nom" autocomplete="username" autofocus>'     # seulement quand il y a plusieurs comptes


@route("GET", r"/login")
def login_page(h, m, q):
    field = LOGIN_USER_FIELD if len(accounts.rows(h.hub.db)) > 1 else ""
    shown = {"1": "Nom ou mot de passe incorrect.", "2": "Trop d'essais : réessaie dans quelques minutes."}.get(q.get("error", ""), "")
    h.send_body((LOGIN_PAGE % (field, shown)).encode("utf-8"), "text/html; charset=utf-8", 200, {"Cache-Control": "no-store"}, h.command == "HEAD")


def session_cookie(hub, row):
    return f"hub={hub.session_token(row)}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"


@route("POST", r"/api/login")
def login(h, m, q):
    h.body_taken = True
    length = h.content_length()
    if length > 4096:
        h.close_connection = True                   # le reste du corps ne sera pas lu : la connexion ne peut plus servir
    raw = h.rfile.read(min(length, 4096)).decode("utf-8", "replace")
    if "=" in raw and not raw.lstrip().startswith("{"):
        form = parse_qs(raw)
        password, name = form.get("password", [""])[0], form.get("user", [""])[0]
    else:
        body = safe_json(raw, {})
        body = body if isinstance(body, dict) else {}
        password, name = body.get("password", ""), body.get("user")
    who = h.client_address[0]
    try:
        wait = h.hub.throttle.delay(who) if h.hub.auth() else 0
    except accounts.AccountError:                # trop d'essais ratés : rien n'est vérifié
        h.send_body(b"", "text/plain", 303, {"Location": "/login?error=2"})
        return
    time.sleep(wait)      # un essai coûte du temps, de plus en plus après chaque échec : pas de force brute à pleine vitesse
    try:
        row = accounts.authenticate(h.hub.db, name, password) if h.hub.auth() else None
    except accounts.AccountError:
        row = None
    if row is not None:
        h.hub.throttle.passed(who)
        h.send_body(b"", "text/plain", 303, {"Location": "/", "Set-Cookie": session_cookie(h.hub, row)})
    else:
        h.hub.throttle.failed(who)
        h.send_body(b"", "text/plain", 303, {"Location": "/login?error=1"})


@route("POST", r"/api/token")
def api_token(h, m, q):
    """Pour un client natif : le nom (facultatif s'il n'y a qu'un compte) et le mot de passe en JSON contre le jeton (à renvoyer en `Authorization: Bearer`) et le jeton de média (`?token=`)."""
    h.body_taken = True
    length = h.content_length()
    if length > 4096:
        h.close_connection = True                   # le corps ne sera pas lu : la connexion ne peut plus servir à une autre requête
        raise HttpError(413, "corps trop gros")
    raw = h.rfile.read(length)                      # toujours lu, même sans mot de passe : sinon il se retrouve en tête de la requête suivante (connexion persistante)
    if not h.hub.auth():
        h.send_json({"token": "", "media_token": ""})       # rien à protéger : aucun jeton n'est demandé
        return
    body = safe_json(raw.decode("utf-8", "replace"), {})
    body = body if isinstance(body, dict) else {}
    password, name = body.get("password"), body.get("user")
    who = h.client_address[0]
    try:
        wait = h.hub.throttle.delay(who)
    except accounts.AccountError as e:                # trop d'essais ratés : rien n'est vérifié
        raise HttpError(e.status, e.message)
    time.sleep(wait)      # même ralentissement que /api/login
    if not isinstance(password, str) or not (name is None or isinstance(name, str)):
        h.hub.throttle.failed(who)
        raise HttpError(401, "mot de passe incorrect")
    try:
        row = accounts.authenticate(h.hub.db, name, password)
    except accounts.AccountError as e:
        h.hub.throttle.failed(who)
        raise HttpError(e.status, e.message)
    h.hub.throttle.passed(who)
    h.send_json({"token": h.hub.session_token(row), "media_token": h.hub.media_token(), "user": accounts.public(row)})


# ---------------------------------------------------------------------------
# Comptes
# ---------------------------------------------------------------------------
def account_call(func, *args, **kw):
    try:
        return func(*args, **kw)
    except accounts.AccountError as e:
        raise HttpError(e.status, e.message)


def body_dict(h):
    body = h.body_json()
    return body if isinstance(body, dict) else {}


def need_accounts(h):
    """Sans compte (serveur ouvert), il n'y a ni mot de passe à changer ni compte à gérer : en créer un par l'API fermerait la porte derrière celui qui l'a créé, sans qu'il ait de jeton."""
    if not h.hub.auth():
        h.discard_body()
        raise HttpError(400, "le serveur est ouvert, sans compte : définis HUB_PASSWORD avant le premier démarrage, ou crée un compte avec « python -m hub user add »")


def check_current(h, current):
    """Le mot de passe actuel du compte de la requête, redemandé avant d'en changer : 400 s'il est faux. Renvoie le compte."""
    time.sleep(0.4)       # même ralentissement qu'à la connexion
    row = accounts.by_id(h.hub.db, h.user["id"])
    if row is None or not isinstance(current, str) or not accounts.verify_password(current, row["pass_hash"]):
        raise HttpError(400, "mot de passe actuel incorrect")
    return row


def renewed_session(h, row):
    """Après un changement de son propre mot de passe, l'ancien jeton ne vaut plus : le nouveau, dans la réponse et en cookie (le navigateur reste connecté)."""
    token = h.hub.session_token(row)
    return {"ok": True, "token": token}, {"Set-Cookie": session_cookie(h.hub, row)}


@route("GET", r"/api/users")
def api_users(h, m, q):
    require_admin(h)
    h.send_json({"users": [accounts.listing(r) for r in accounts.rows(h.hub.db)]})


@route("POST", r"/api/users")
def api_user_create(h, m, q):
    require_admin(h)
    need_accounts(h)
    body = body_dict(h)
    row = account_call(accounts.create, h.hub.db, body.get("name"), body.get("password"), body.get("role", "user"))
    h.send_json(accounts.listing(row), 201)


@route("POST", r"/api/user/(\w+)/password")
def api_user_password(h, m, q):
    """L'administrateur donne un nouveau mot de passe à un autre compte sans connaître l'ancien. Pour le sien, il doit redonner l'actuel (`current`, comme `/api/me/password`) :
    un jeton de session volé ne suffit pas à prendre le compte."""
    require_admin(h)
    need_accounts(h)
    body = body_dict(h)
    if m.group(1) == h.user["id"]:
        check_current(h, body.get("current"))
    row = account_call(accounts.set_password, h.hub.db, m.group(1), body.get("password"))
    if row["id"] == h.user["id"]:
        out, headers = renewed_session(h, row)
        h.send_json(out, 200, headers)
    else:
        h.send_json({"ok": True})


@route("POST", r"/api/user/(\w+)/delete")
def api_user_delete(h, m, q):
    require_admin(h)
    need_accounts(h)
    h.body_json()
    account_call(accounts.delete, h.hub.db, m.group(1))
    h.send_json({"ok": True}, 200, {"Set-Cookie": "hub=; Path=/; Max-Age=0"} if m.group(1) == h.user["id"] else None)


@route("POST", r"/api/me/password")
def api_me_password(h, m, q):
    """Chacun change son mot de passe, en donnant l'actuel."""
    need_accounts(h)
    body = body_dict(h)
    row = check_current(h, body.get("current"))
    row = account_call(accounts.set_password, h.hub.db, row["id"], body.get("new"))
    out, headers = renewed_session(h, row)
    h.send_json(out, 200, headers)


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


def seen_counts(hub, profile, lib_id=None):
    """{item_id: (fichiers vus, fichiers entamés)} : de quoi afficher « vu » ou « 3/12 » sur une vignette."""
    sql = ("SELECT e.item_id AS item, SUM(p.done) AS seen, COUNT(*) AS touched FROM progress p JOIN entries e ON e.id=p.entry_id "
           "WHERE p.profile=? AND e.kind != 'audio'" + (" AND e.lib=?" if lib_id else "") + " GROUP BY e.item_id")
    params = (profile, lib_id) if lib_id else (profile,)
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
                 "auth": hub.auth(), "user": h.user, "api": API_VERSION, "busy": hub.jobs.busy(), "streams": hub.active, "max_streams": hub.cfg.max_streams})


def next_entry(hub, entry):
    """Le fichier qui suit celui-ci dans son élément (épisode suivant, tome suivant, partie suivante), ou None."""
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (entry["item_id"],)), key=order_key)
    for index, row in enumerate(rows):
        if row["id"] == entry["id"]:
            return rows[index + 1] if index + 1 < len(rows) else None
    return None


def dismiss_key(profile, item_id):
    return f"dismiss:{profile}:{item_id}"


def continue_row(hub, profile):
    """Ce qu'on a commencé : le film ou l'épisode à reprendre, l'épisode qui suit celui qu'on vient de finir, le tome suivant."""
    rows = hub.db.query(
        "SELECT p.entry_id, p.position, p.duration AS pduration, p.done, p.updated, e.* FROM progress p JOIN entries e ON e.id=p.entry_id "
        "WHERE p.profile=? AND e.kind IN ('video','book') ORDER BY p.updated DESC LIMIT 200", (profile,))
    seen_items, out = set(), []
    for row in rows:
        if row["item_id"] in seen_items:
            continue
        seen_items.add(row["item_id"])
        if row["updated"] <= hub.db.kv_get(dismiss_key(profile, row["item_id"]), -1):
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
            progress = hub.db.progress_for(profile, [following["id"]]).get(following["id"])
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
    seen = seen_counts(hub, h.user["id"])
    rows = []
    for lib in hub.cfg.libraries:
        recent = hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY added DESC, sort LIMIT 24", (lib.id,))
        rows.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "recent": [with_seen(r, seen) for r in recent],
                     "count": hub.db.one("SELECT COUNT(*) AS n FROM items WHERE lib=?", (lib.id,))["n"]})
    h.send_json({"continue": continue_row(hub, h.user["id"]), "libraries": rows})


@route("GET", r"/api/search")
def api_search(h, m, q):
    needle = fold(q.get("q", "")).strip()
    if len(needle) < 2:
        h.send_json({"results": []})
        return
    hub = h.hub
    seen = seen_counts(hub, h.user["id"])
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
    "part": (lambda r: release_key(r), False),      # l'ordre d'une saga
}
# Le tri « Titre » de la musique et des livres range par artiste / collection ; chez un artiste, les albums vont du plus ancien au plus récent, puis par nom (ceux sans année en dernier)
TITLE_SORTS = {
    "music": lambda r: (fold(r["grp"]) or "￿", r["year"] or "9999", r["sort"]),
    "books": lambda r: (fold(r["grp"]), r["sort"]),
}


def release_key(row):
    """L'ordre d'une saga : par date de sortie. Le numéro posé sur le disque peut être celui d'un dossier de rangement (le MCU : Ant-Man 12, 20, 31), il ne départage que deux films sortis le même jour."""
    meta = safe_json(row["meta"], {})
    part = meta.get("part")
    return (meta.get("date") or row["year"] or "9999", part is None, part or 0, row["sort"])


def number_films(films, ranks=None, start=1):
    """Le numéro affiché sur l'affiche d'un film de saga : son rang dans la saga du fichier (`ranks`, {id du film: rang}), sinon sa place dans la saga, de 1 à N."""
    for n, film in enumerate(films, start):
        film["part"] = ranks[film["id"]] if ranks else n
    return films


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


def saga_groups(rows, custom):
    """(sagas, home) : {nom: [(film, rang)]} pour chaque saga d'au moins deux films de la bibliothèque, dans l'ordre de la saga ; {id d'un film: nom de la saga qui le range dans la grille}.

    `custom` : les sagas du fichier (voir sagas.py). Elles passent avant la collection de l'étiquette, et la première écrite avant les suivantes : un film n'a qu'une vignette. Une saga du fichier qui porte le nom d'une saga de l'étiquette la remplace (page, fiche et grille s'accordent sur ce que ce nom désigne), même si elle n'a qu'un film ici.
    """
    sagas = {name: films for name, films in custom.items() if len(films) > 1}
    tagged = {}
    for row in rows:
        if row["type"] == "movie" and row["grp"] and row["grp"] not in custom:
            tagged.setdefault(row["grp"], []).append(row)
    tagged = {name: sorted(films, key=release_key) for name, films in tagged.items() if len(films) > 1}
    home = {}
    for name in sagas:
        for film, _ in sagas[name]:
            home.setdefault(film["id"], name)
    for row in rows:
        if row["type"] == "movie" and row["grp"] in tagged:
            home.setdefault(row["id"], row["grp"])
    sagas.update({name: list(zip(films, range(1, len(films) + 1))) for name, films in tagged.items()})
    return sagas, home


def collapse_sagas(rows, seen, custom):
    """Les films d'une même saga (au moins deux dans la bibliothèque) deviennent une seule vignette ; les autres films restent seuls.

    Une vignette de saga est un dictionnaire qui a les mêmes clés qu'une ligne de la base, plus `members` : les films, dans l'ordre de la saga. Elle existe tant qu'un film au moins y est rangé : un film de deux sagas n'a qu'une vignette (voir saga_groups), mais figure dans la page de chacune.
    """
    sagas, home = saga_groups(rows, custom)
    out = [row for row in rows if row["id"] not in home]
    for name, films in sagas.items():
        members = [film for film, _ in films]
        if not any(home.get(m["id"]) == name for m in members):
            continue
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
           "added": saga["added"], "genres": safe_json(saga["genres"], []), "covers": [m["id"] for m in saga["members"][:3]],
           "cover_urls": [urls.item_cover(m["id"]) for m in saga["members"][:3]]}
    if done:
        out["seen"] = done
    if started:
        out["started"] = started
    return out


def letter_of(row, kind):
    """La lettre sous laquelle une vignette se range : l'artiste pour la musique (qui se range par artiste, pas par titre d'album), le titre sinon."""
    return initial(row["grp"]) if kind == "music" else row["letter"]


def library_counts(rows, kind):
    """Ce que la barre latérale compte : ({lettre: n}, {genre: n}, {section: n}, {id: section}). Les sections, pour les livres seulement."""
    letters = Counter(letter_of(row, kind) for row in rows)
    genres = Counter(genre for row in rows for genre in safe_json(row["genres"], []))
    of = {row["id"]: section_of(row) for row in rows} if kind == "books" else {}
    return letters, genres, Counter(of.values()), of


def sagas_of_films(custom):
    """{id d'un film: [noms des sagas du fichier où il figure]}."""
    out = {}
    for name, films in custom.items():
        for film, _ in films:
            out.setdefault(film["id"], []).append(name)
    return out


def group_ranks(hub, rows, custom, group, movies):
    """(rangs, manquants) de la saga `group` d'une page de films : le rang de chaque film ({id: rang}, ou None quand la saga garde l'ordre par date de sortie) et les films qui lui manquent (voir missing.py)."""
    if not (movies and group):
        return None, []
    info, owned = missing.load(hub.db), sagafile.by_tmdb(rows)
    if group in custom:
        return {film["id"]: rank for film, rank in custom[group]}, missing.listed(dict(hub.sagas.all()).get(group, []), owned, info)         # une saga du fichier : le rang est sa place dans la liste
    return missing.tagged(group, [r for r in rows if r["grp"] == group], info, set(hub.sagas.ignored()))                                       # numérotés d'après la collection TMDB


def has_letter(row, letter, kind):
    return letter_of(row, kind) == letter


def in_group(row, group, ranks):
    return row["id"] in ranks if ranks is not None else row["grp"] == group


def in_section(row, section, of):
    return of.get(row["id"]) == section


def has_genre(row, genre):
    return genre in safe_json(row["genres"], [])


def matches_needle(row, needle, in_custom):
    """Le titre, la saga de l'étiquette ou une saga du fichier contient la recherche ; une vignette de saga, aussi le titre d'un de ses films."""
    return (needle in fold(row["title"]) or needle in fold(row["grp"]) or any(needle in fold(n) for n in in_custom.get(row["id"], ()))
            or (row["type"] == "saga" and any(needle in fold(f["title"]) for f in row["members"])))


def is_unseen(row, seen):
    if row["type"] == "saga":
        return not row["all_seen"]
    return seen.get(row["id"], (0, 0))[0] < max(1, row["count"])


def library_filters(q, kind, ranks, of, in_custom, seen):
    """Les conditions que la requête pose à une vignette (lettre, saga, section, genre, recherche, non vus) ; elles s'ajoutent."""
    letter, genre, group = q.get("letter", ""), q.get("genre", ""), q.get("group", "")
    section = q.get("section", "") if kind == "books" else ""
    needle = fold(q.get("q", "")).strip()
    tests = []
    if letter:
        tests.append(partial(has_letter, letter=letter, kind=kind))
    if group:
        tests.append(partial(in_group, group=group, ranks=ranks))
    if section:
        tests.append(partial(in_section, section=section, of=of))
    if genre:
        tests.append(partial(has_genre, genre=genre))
    if needle:
        tests.append(partial(matches_needle, needle=needle, in_custom=in_custom))
    if q.get("filter") == "unseen":
        tests.append(partial(is_unseen, seen=seen))
    return tests


def library_order(kind, q, ranks):
    """(tri, clé, sens) du tri demandé ; la musique n'a qu'un ordre : par artiste."""
    sort = "title" if kind == "music" else q.get("sort", "title")
    key, reverse = SORTS.get(sort, SORTS["title"])
    if sort == "title" and kind in TITLE_SORTS:
        key = TITLE_SORTS[kind]
    if sort == "part" and ranks is not None:
        key = lambda r: ranks[r["id"]]
    return sort, key, reverse


def page_bounds(q):
    """(départ, taille) de la page demandée ; une valeur qui n'est pas un nombre ramène aux valeurs par défaut."""
    try:
        return max(0, int(q.get("offset", 0))), min(600, max(1, int(q.get("limit", 240))))
    except ValueError:
        return 0, 240


def library_item(row, seen):
    return saga_json(row, seen) if row["type"] == "saga" else with_seen(row, seen)


def library_extras(out, kind, sections, absent, sagas, chosen):
    """Les champs que la réponse n'a que dans certains cas : les sections des livres, les films qui manquent à une saga, le décompte des sagas d'une grille de sagas (un film de deux sagas ne compte qu'une fois)."""
    if kind == "books":
        out["sections"] = sections_json(sections)
    if absent:
        out["missing"] = absent
    if sagas:
        out["sagas"] = sum(1 for r in chosen if r["type"] == "saga")
        out["films"] = len({m["id"] for r in chosen for m in (r["members"] if r["type"] == "saga" else [r])})


@route("GET", r"/api/library/([\w-]+)")
def api_library(h, m, q):
    hub = h.hub
    lib = library_or_404(hub, m.group(1))
    rows = hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,))
    seen = seen_counts(hub, h.user["id"], lib.id)
    movies = lib.kind == "movies"
    custom = hub.sagas.of_movies(rows) if movies else {}          # les sagas du fichier, voir sagas.py
    group = q.get("group", "")
    sagas = movies and q.get("sagas") == "1" and not group
    if sagas:
        rows = collapse_sagas(rows, seen, custom)
    letters, genres, sections, of = library_counts(rows, lib.kind)
    ranks, absent = group_ranks(hub, rows, custom, group, movies)
    tests = library_filters(q, lib.kind, ranks, of, sagas_of_films(custom), seen)
    chosen = [row for row in rows if all(test(row) for test in tests)]
    sort, key, reverse = library_order(lib.kind, q, ranks)
    chosen.sort(key=key, reverse=reverse)
    offset, limit = page_bounds(q)
    page = chosen[offset:offset + limit]
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "total": len(chosen), "offset": offset,
           "items": [library_item(r, seen) for r in page],
           "letters": sorted(letters.items(), key=lambda kv: (kv[0] == "#", kv[0])), "genres": sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:60]}
    if group and sort == "part":
        number_films(out["items"], ranks, offset + 1)
    library_extras(out, lib.kind, sections, absent, sagas, chosen)
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


TMDB_TAG_RE = re.compile(r"^(?:(movie|tv)/)?([0-9]+)$", re.IGNORECASE)         # comme le lecteur de l'atelier (libraries.video.mkv) : la casse ne compte pas


def video_links(item, meta):
    """Les liens d'un film ou d'une série : sa fiche TMDB, quand l'identifiant est connu (étiquette du fichier, nom du dossier ou enrichissement) ; vide sinon."""
    if item["type"] not in ("movie", "show"):
        return []
    found = TMDB_TAG_RE.match(str(meta.get("tmdb") or meta.get("tmdb_pinned") or "").strip())      # l'étiquette d'un film s'écrit « movie/27205 » ; l'enrichissement d'une série garde le seul nombre
    if not found:
        return []
    kind = (found.group(1) or ("movie" if item["type"] == "movie" else "tv")).lower()
    return [{"label": "TMDB", "url": f"{tmdb.WEB_BASE}/{kind}/{found.group(2)}"}]


@route("GET", r"/api/item/(\w+)")
def api_item(h, m, q):
    hub = h.hub
    item = hub.item(m.group(1))
    lib = library_or_404(hub, item["lib"])
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (item["id"],)), key=order_key)
    progress = hub.db.progress_for(h.user["id"], [r["id"] for r in rows])
    meta = safe_json(item["meta"], {})
    out = summary(item)
    out.update({"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "summary": item["summary"], "tagged": bool(meta.get("tagged"))})
    for key in ("directors", "writers", "actors", "artist", "artist_id", "label", "catalog", "formats", "authors", "publisher", "section", "collection", "status", "episodes_total",
                "seasons_total", "tmdb", "missing", "unit", "units", "years", "discs", "season_summaries"):
        if meta.get(key) not in (None, "", []):
            out[key] = meta[key]
    links = video_links(item, meta)
    if links:
        out["links"] = links
    out["entries"] = [entry_json(r, progress.get(r["id"])) for r in rows]
    out["resume"] = resume_target(rows, progress)
    if item["type"] == "show":
        out["seasons"] = meta.get("seasons", [])
    if item["type"] == "movie":
        movies = hub.db.query("SELECT * FROM items WHERE lib=? AND type='movie'", (item["lib"],))
        seen = seen_counts(hub, h.user["id"], lib.id)
        sagas = []                  # ses sagas : celle de l'étiquette d'abord, puis celles du fichier (sagas.py), chacune avec les films qui la composent et ceux qui lui manquent
        custom = hub.sagas.of_movies(movies)
        mine = {name: films for name, films in custom.items() if any(f["id"] == item["id"] for f, _ in films)}
        info, owned = (missing.load(hub.db), sagafile.by_tmdb(movies)) if item["grp"] or mine else (None, None)          # TMDB ne sert que s'il y a une saga
        if item["grp"]:
            tagged = sorted((r for r in movies if r["grp"] == item["grp"]), key=release_key)
            ranks, absent = missing.tagged(item["grp"], tagged, info, set(hub.sagas.ignored()))
            if ranks is not None:
                tagged.sort(key=lambda r: ranks[r["id"]])
            out["saga"] = number_films([with_seen(r, seen) for r in tagged], ranks)       # la saga de l'étiquette seule : gardée pour les clients d'avant `sagas`
            if item["grp"] not in custom:                   # une saga du fichier de même nom la remplace : c'est elle que ce nom désigne dans la grille
                sagas.append({"name": item["grp"], "films": out["saga"], "missing": absent})
        ids = dict(hub.sagas.all())
        for name, films in mine.items():
            numbered = number_films([with_seen(f, seen) for f, _ in films], {f["id"]: rank for f, rank in films})
            sagas.append({"name": name, "films": numbered, "missing": missing.listed(ids.get(name, []), owned, info)})
        sagas = [{k: v for k, v in s.items() if v or k != "missing"} for s in sagas if len(s["films"]) + len(s["missing"]) > 1]
        if sagas:
            out["sagas"] = sagas
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


def wanted_albums(hub, lib_id, artist_id):
    """Les albums qui manquent à un artiste (voir libraries.music.wanted), tels que le dernier scan les a vus ; vide s'il n'en manque aucun."""
    return (hub.db.kv_get(f"wanted:{lib_id}") or {}).get(artist_id, [])


PHOTO_UPLOAD_MAX = 10 << 20


def artist_home(lib, rows):
    """Le dossier de l'artiste à la racine de la médiathèque (voir scan.music.artist_dirs), où va sa photo ; None si le dernier scan ne lui en a pas trouvé ou s'il n'est plus là."""
    for row in rows:
        name = safe_json(row["meta"], {}).get("artist_dir")
        if name and (lib.path / name).is_dir():
            return lib.path / name
    return None


def artist_photo(hub, artist_id, kept, home=None):
    """Le fichier de la photo d'un artiste, la seule qu'il ait (elle vient de Wikipédia, via « Fiches artistes », ou de l'utilisateur) ; None s'il n'en a pas."""
    return enrich.photo_file(hub.cfg.data / "artists", artist_id, (kept or {}).get("mbid", ""), home)


def save_artist_photo(hub, lib, rows, artist_id, data, box=None):
    """enrich.save_photo, ses échecs rendus en réponses HTTP : 415 (image illisible), 501 (rien pour convertir), 500 (écriture impossible : le dossier de la médiathèque est peut-être en lecture seule)."""
    try:
        enrich.save_photo(hub.cfg, artist_id, data, artist_home(lib, rows), box, (artist_kept(hub, rows) or {}).get("mbid", ""))
    except NoConverter as e:
        raise HttpError(501, str(e))
    except ConvertError as e:
        raise HttpError(415, str(e))
    except OSError as e:
        raise HttpError(500, f"photo non enregistrée : {e}")


def photo_box(q):
    """Le cadre `crop=x,y,largeur,hauteur` (fractions de l'image) de la requête, ou None."""
    text = q.get("crop")
    if not text:
        return None
    try:
        return crop_box(text)
    except ValueError as e:
        raise HttpError(400, str(e))


@route("GET", r"/api/artist/([\w-]+)/(\w+)")
def api_artist(h, m, q):
    """La fiche d'un artiste ou d'un groupe : ses albums de la médiathèque, de quoi tout lire, ses genres, et ce que MusicBrainz et Wikipédia en disent, photo comprise (une fois les fiches demandées dans Gestion)."""
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    seen = seen_counts(hub, h.user["id"], lib.id)
    name = rows[0]["grp"] or "Artiste inconnu"
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "id": m.group(2), "name": name, "albums": [with_seen(r, seen) for r in rows], "tracks": []}
    genres = {}
    for row in rows:
        for entry in sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (row["id"],)), key=order_key):
            out["tracks"].append({"id": entry["id"], "title": entry["title"], "artist": entry_json(entry)["artist"] or name, "album": row["title"], "item": row["id"], "duration": entry["duration"],
                                "cover": urls.item_cover(row["id"]), "file": urls.entry_file(entry["id"])})
        for genre in safe_json(row["genres"], []):
            genres[genre] = genres.get(genre, 0) + 1
    out["genres"] = [g for g, _ in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    out["duration"] = sum(t["duration"] or 0 for t in out["tracks"])
    kept = artist_kept(hub, rows)
    if kept:
        out["info"] = artist_sheet(kept["info"], kept["mbid"])
    out["wanted"] = [{"year": w["year"], "title": w["title"], "cover": f"/img/wanted/{lib.id}/{m.group(2)}/{w['key']}" if w["cover"] else ""} for w in wanted_albums(hub, lib.id, m.group(2))]
    out["photo"] = artist_photo(hub, m.group(2), kept, artist_home(lib, rows)) is not None
    if out["photo"]:
        out["image"] = urls.artist_image(lib.id, m.group(2))
    h.send_json(out)


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo")
def api_artist_photo_set(h, m, q):
    """Remplace la photo d'un artiste, quelle qu'elle soit, par l'image envoyée (le corps de la requête : JPEG, PNG ou WebP, 10 Mo au plus). `crop=x,y,largeur,hauteur` (fractions de l'image) ne garde que ce cadre. Elle est convertie en WebP et posée dans le dossier de l'artiste. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    box = photo_box(q)
    data = h.body_bytes(PHOTO_UPLOAD_MAX)
    if not enrich.image_ext(data):
        raise HttpError(415, "ce n'est pas une image JPEG, PNG ou WebP")
    save_artist_photo(hub, lib, rows, m.group(2), data, box)
    h.send_json({"ok": True})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo/crop")
def api_artist_photo_crop(h, m, q):
    """Ne garde de la photo de l'artiste que le cadre `{"x", "y", "w", "h"}` (fractions de l'image) : ce qui est hors du cadre disparaît du fichier. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    body = h.body_json()
    try:
        box = crop_box(",".join(str(body.get(k, "")) for k in ("x", "y", "w", "h")))
    except ValueError as e:
        raise HttpError(400, str(e))
    path = artist_photo(hub, m.group(2), artist_kept(hub, rows), artist_home(lib, rows))
    if path is None:
        raise HttpError(404, "pas de photo à recadrer")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise HttpError(500, f"photo illisible : {e}")
    save_artist_photo(hub, lib, rows, m.group(2), data, box)
    h.send_json({"ok": True})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo/wikipedia")
def api_artist_photo_wikipedia(h, m, q):
    """Remplace la photo d'un artiste par celle de son article Wikipédia (celui que sa fiche garde : il faut donc que « Fiches artistes » soit passé). Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    h.body_json()
    kept = artist_kept(hub, rows)
    wiki = ((kept or {}).get("info") or {}).get("wiki")
    if not wiki:
        raise HttpError(404, "pas d'article Wikipédia connu pour cet artiste")
    try:
        enrich.wikipedia_photo(hub.cfg, m.group(2), wiki, artist_home(lib, rows), kept["mbid"])
    except OSError as e:
        raise HttpError(502, f"photo non récupérée : {e}")
    h.send_json({"ok": True})


@route("GET", r"/img/artist/([\w-]+)/(\w+)")
def artist_image(h, m, q):
    """La photo d'un artiste, réduite comme les pochettes ; 404 s'il n'en a pas (la page garde alors la pochette de son premier album)."""
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    path = artist_photo(hub, m.group(2), artist_kept(hub, rows), artist_home(lib, rows))
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


@route("GET", r"/img/wanted/([\w-]+)/(\w+)/([\w-]+)")
def wanted_image(h, m, q):
    """La pochette d'un album manquant, posée dans le dossier de l'artiste ; 404 s'il n'en a pas (la fiche montre alors une case grisée)."""
    hub = h.hub
    lib, _ = artist_albums(hub, m.group(1), m.group(2))
    found = next((w for w in wanted_albums(hub, lib.id, m.group(2)) if w["key"] == m.group(3) and w["cover"]), None)
    path = (lib.path / found["cover"]).resolve() if found else None
    if path is None or lib.path.resolve() not in path.parents:
        raise HttpError(404, "pas de pochette")
    try:
        width = int(q.get("w", 0))
    except ValueError:
        width = 0
    served = hub.images.file(f"wanted:{lib.id}:{m.group(2)}:{m.group(3)}", path, width)
    if served is None:
        raise HttpError(404, "pas de pochette")
    data, mime, key = served
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
            hub.db.save_progress(h.user["id"], row["id"], row["duration"], row["duration"], True)
        else:
            hub.db.execute("DELETE FROM progress WHERE profile=? AND entry_id=?", (h.user["id"], row["id"]))
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
    h.send_json({"playlists": playlists.listing(h.hub.db, h.user["id"])})


@route("POST", r"/api/playlists")
def api_playlist_create(h, m, q):
    body = h.body_json()
    entries = body.get("entries") if isinstance(body.get("entries"), list) else []
    pid = playlist_call(playlists.create, h.hub.db, h.user["id"], body.get("name"), entries)
    h.send_json(playlist_call(playlists.detail, h.hub.db, h.user["id"], pid), 201)


@route("GET", r"/api/playlist/(\w+)")
def api_playlist(h, m, q):
    h.send_json(playlist_call(playlists.detail, h.hub.db, h.user["id"], m.group(1)))


@route("POST", r"/api/playlist/(\w+)/(add|remove|move|rename|delete)")
def api_playlist_edit(h, m, q):
    db, pid, action, body, profile = h.hub.db, m.group(1), m.group(2), h.body_json(), h.user["id"]
    if action == "add":
        playlist_call(playlists.add, db, profile, pid, body.get("entries") if isinstance(body.get("entries"), list) else [])
    elif action == "remove":
        playlist_call(playlists.remove, db, profile, pid, body.get("index"))
    elif action == "move":
        playlist_call(playlists.move, db, profile, pid, body.get("from"), body.get("to"))
    elif action == "rename":
        playlist_call(playlists.rename, db, profile, pid, body.get("name"))
    else:
        playlist_call(playlists.delete, db, profile, pid)
        h.send_json({"ok": True})
        return
    h.send_json(playlist_call(playlists.detail, db, profile, pid))


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
    if not (math.isfinite(position) and math.isfinite(duration)):          # `Infinity` passe json.loads : stocké, il rendrait la fiche du compte illisible pour JSON.parse
        raise HttpError(400, "position invalide")
    duration = duration or entry["duration"]
    done = bool(body.get("done")) or is_finished(entry["kind"], position, duration)
    if entry["kind"] == "audio":                 # la musique n'a pas de reprise : seulement « écouté »
        done = done or (duration > 0 and position / duration > 0.8)
        position = 0 if done else position
    h.hub.db.save_progress(h.user["id"], entry["id"], duration if done and entry["kind"] != "book" else position, duration, done)
    h.send_json({"ok": True, "done": done})


@route("POST", r"/api/item/(\w+)/dismiss")
def api_item_dismiss(h, m, q):
    """Retire un élément de « Reprendre » sans toucher à la progression : il y revient dès qu'on le relance."""
    hub = h.hub
    h.body_json()                 # le corps doit être lu, même vide de sens : sinon il reste sur la connexion et colle à la requête suivante (501)
    item = hub.item(m.group(1))
    latest = hub.db.one("SELECT MAX(p.updated) AS t FROM progress p JOIN entries e ON e.id=p.entry_id WHERE p.profile=? AND e.item_id=?", (h.user["id"], item["id"]))
    hub.db.kv_set(dismiss_key(h.user["id"], item["id"]), latest["t"] or 0)
    h.send_json({"ok": True})


@route("POST", r"/api/entry/(\w+)/seen")
def api_entry_seen(h, m, q):
    hub = h.hub
    entry = hub.entry(m.group(1))
    if h.body_json().get("done", True):
        hub.db.save_progress(h.user["id"], entry["id"], entry["duration"], entry["duration"], True)
    else:
        hub.db.execute("DELETE FROM progress WHERE profile=? AND entry_id=?", (h.user["id"], entry["id"]))
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
    progress = hub.db.progress_for(h.user["id"], [entry["id"]]).get(entry["id"])
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
    side = None
    if kind == "s":                                  # le fichier posé à côté : sa taille et sa date entrent dans la clé, sinon corriger un .srt resterait sans effet
        sides = subtitles.side_files(path)
        if index >= len(sides):
            raise HttpError(404, "sous-titres introuvables")
        side = sides[index]["path"]
        try:
            st = side.stat()
        except OSError:
            raise HttpError(404, "sous-titres introuvables")
    stamp = f"{entry['id']}-{entry['mtime']}-{kind}{index}" + (f"-{st.st_size}-{st.st_mtime_ns}" if side else "")
    target = cache / f"{stamp}.vtt"
    if not target.is_file():
        try:
            text = subtitles.ffmpeg_vtt(hub.cfg.ffmpeg, path, index) if kind == "e" else subtitles.side_vtt(hub.cfg.ffmpeg, side)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise HttpError(500, str(e))
        write_atomic(target, text.encode("utf-8"))
    tag = f'"{stamp}"'
    if h.headers.get("If-None-Match") == tag:
        h.send_body(b"", "text/vtt; charset=utf-8", 304, {"ETag": tag, "Cache-Control": "private, no-cache"})
        return
    h.send_body(target.read_bytes(), "text/vtt; charset=utf-8", 200, {"ETag": tag, "Cache-Control": "private, no-cache"}, h.command == "HEAD")


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
    progress = hub.db.progress_for(h.user["id"], [entry["id"]]).get(entry["id"])
    out = {"id": entry["id"], "item": entry["item_id"], "title": entry["title"], "format": kind, "position": progress["position"] if progress and not progress["done"] else 0,
           "next": None, "file": urls.entry_file(entry["id"])}
    try:
        if kind == "cbz":
            out["pages"] = len(bookmedia.cbz_pages(path))
            out["page_url"] = f"/book/{entry['id']}/page/{{n}}"          # `{n}` : le numéro de page, à partir de 0
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
    csp = "default-src 'self' data: blob:; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"
    h.send_body(data, mime, 200, {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": csp}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Sagas composées à la main (sagas.json, voir sagas.py) : réservées à l'administrateur
# ---------------------------------------------------------------------------
def saga_call(func, *args):
    try:
        return func(*args)
    except sagafile.SagaError as e:
        raise HttpError(e.status, e.message)


def movie_rows(hub):
    """Les films de toutes les bibliothèques de films."""
    rows = []
    for lib in hub.cfg.libraries:
        if lib.kind == "movies":
            rows += hub.db.query("SELECT * FROM items WHERE lib=? AND type='movie'", (lib.id,))
    return rows


def saga_film(ident, row, info=None):
    """Un film d'une saga du fichier pour l'éditeur : son identifiant TMDB, sa date de sortie (pour trier) et l'élément s'il est dans la médiathèque (`item` vaut null sinon, avec `title`, `year` et `cover` quand l'enrichissement TMDB l'a décrit)."""
    if row is None:
        film = (info or {"films": {}})["films"].get(str(ident))
        out = {"tmdb": ident, "date": (film or {}).get("date") or "", "item": None}
        if film:
            out.update({k: v for k, v in missing.entry(ident, 0, info).items() if k in ("title", "year", "cover")})
        return out
    return {"tmdb": ident, "date": safe_json(row["meta"], {}).get("date") or row["year"], "item": summary(row)}


def sagas_json(hub):
    owned, info = sagafile.by_tmdb(movie_rows(hub)), missing.load(hub.db)
    ignored = [{k: v for k, v in missing.entry(i, 0, info).items() if k in ("tmdb", "title", "year")} if str(i) in info["films"] else {"tmdb": i} for i in hub.sagas.ignored()]
    out = {"version": hub.sagas.version(), "ignored": ignored,
           "sagas": [{"name": name, "films": [saga_film(i, owned.get(i), info) for i in films]} for name, films in hub.sagas.all()]}
    if hub.sagas.damaged():
        out["damaged"] = True               # le fichier existe mais n'est pas lisible : les écritures sont refusées (409) tant qu'on ne l'a pas corrigé
    return out


@route("GET", r"/api/sagas")
def api_sagas(h, m, q):
    require_admin(h)
    h.send_json(sagas_json(h.hub))


@route("POST", r"/api/sagas")
def api_sagas_save(h, m, q):
    """Remplace toutes les sagas. `version` (celle du dernier GET) évite d'écraser ce qu'un autre a modifié entre-temps."""
    require_admin(h)
    body = body_dict(h)
    version = body.get("version")
    if version is not None and not isinstance(version, str):
        raise HttpError(400, "version illisible")
    saga_call(h.hub.sagas.save, saga_call(sagafile.clean, body.get("sagas")), version)
    h.send_json(sagas_json(h.hub))


@route("POST", r"/api/sagas/add")
def api_sagas_add(h, m, q):
    """Ajoute des films à la fin d'une saga, créée au besoin : `films` (identifiants TMDB), puis `items` (éléments de la médiathèque : c'est ce que la fiche d'un film envoie)."""
    require_admin(h)
    body = body_dict(h)
    ids = []
    for value in body.get("films") if isinstance(body.get("films"), list) else []:
        ident = sagafile.film_id(value)
        if ident is None:
            raise HttpError(400, f"identifiant TMDB illisible ({value!r})")
        ids.append(ident)
    for value in body.get("items") if isinstance(body.get("items"), list) else []:
        row = h.hub.db.one("SELECT * FROM items WHERE id=?", (str(value),))
        if row is None or row["type"] != "movie":
            raise HttpError(404, "film inconnu")
        found = sagafile.by_tmdb([row])
        if not found:
            raise HttpError(400, f"« {row['title']} » n'a pas d'identifiant TMDB : étiquette-le avec Metadata.py")
        ids += list(found)
    if not ids:
        raise HttpError(400, "aucun film à ajouter")
    name = saga_call(h.hub.sagas.add, body.get("name"), ids)
    h.send_json({**sagas_json(h.hub), "name": name})


@route("POST", r"/api/sagas/ignore")
def api_sagas_ignore(h, m, q):
    """Cache un film des manquants des sagas de l'étiquette (`ignore` vrai, par défaut) ou le rend visible (`ignore` faux). Rend comme `GET /api/sagas`."""
    require_admin(h)
    body = body_dict(h)
    ident = sagafile.film_id(body.get("tmdb"))
    if ident is None:
        raise HttpError(400, "identifiant TMDB illisible")
    flag = body.get("ignore", True)
    if not isinstance(flag, bool):
        raise HttpError(400, "« ignore » vaut vrai ou faux")
    saga_call(h.hub.sagas.ignore, ident, flag)
    h.send_json(sagas_json(h.hub))


@route("GET", r"/img/film/(\d+)")
def film_image(h, m, q):
    """L'affiche d'un film qui manque à une saga (HUB_DATA/tmdb, voir missing.py) ; 404 s'il n'en a pas (la page montre alors une case grisée)."""
    hub = h.hub
    film = missing.load(hub.db)["films"].get(m.group(1))
    path = hub.cfg.data / "tmdb" / missing.poster_name(m.group(1)) if film and film.get("poster") else None
    try:
        width = int(q.get("w", 0))
    except ValueError:
        width = 0
    served = hub.images.file(f"film:{m.group(1)}", path, width) if path else None
    if served is None:
        raise HttpError(404, "pas d'affiche")
    data, mime, key = served
    tag = f'"{key}"'
    if h.headers.get("If-None-Match") == tag:
        h.send_body(b"", mime, 304, {"ETag": tag, "Cache-Control": "private, no-cache"})
        return
    h.send_body(data, mime, 200, {"ETag": tag, "Cache-Control": "private, no-cache"}, h.command == "HEAD")


@route("GET", r"/api/sagas/films")
def api_sagas_films(h, m, q):
    """Les films qu'on peut mettre dans une saga (ceux qui portent un identifiant TMDB) : ceux dont le titre ou la saga contient `q`, ou ceux de la saga de l'étiquette `group` dans l'ordre de sortie. Au plus `limit` (40 par défaut, 600)."""
    require_admin(h)
    needle, group = fold(q.get("q", "")).strip(), q.get("group", "")
    try:
        limit = min(600, max(1, int(q.get("limit", 40))))
    except ValueError:
        limit = 40
    found = [(ident, row) for ident, row in sagafile.by_tmdb(movie_rows(h.hub)).items()
             if (not group or row["grp"] == group) and (not needle or needle in fold(row["title"]) or needle in fold(row["grp"]))]
    found.sort(key=lambda f: release_key(f[1]) if group else (f[1]["sort"], f[1]["year"]))
    h.send_json({"films": [saga_film(ident, row) for ident, row in found[:limit]], "total": len(found)})


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------
@route("GET", r"/api/jobs")
def api_jobs(h, m, q):
    if q.get("log") == "1":                     # le journal des tâches est réservé à l'administrateur ; l'état « occupé » reste ouvert à tout compte
        require_admin(h)
    h.send_json({"jobs": h.hub.jobs.snapshot(with_log=q.get("log") == "1"), "busy": h.hub.jobs.busy()})


@route("POST", r"/api/scan")
def api_scan(h, m, q):
    require_admin(h)
    body = h.body_json()
    jobs = h.hub.jobs
    job = jobs.scan(library_or_404(h.hub, body["lib"])) if body.get("lib") else jobs.scan_all()
    h.send_json({"job": job.public()}, 202)


@route("POST", r"/api/enrich")
def api_enrich(h, m, q):
    require_admin(h)
    body = h.body_json()
    lib = library_or_404(h.hub, str(body.get("lib", "")))
    if lib.kind == "music":                     # MusicBrainz et Wikipédia : pas de clé
        h.send_json({"job": h.hub.jobs.enrich_artists(lib, bool(body.get("force"))).public()}, 202)
        return
    if lib.kind not in ("shows", "movies"):
        raise HttpError(400, "l'enrichissement ne concerne que les films (sagas), les séries, les animations et la musique")
    if not h.hub.cfg.tmdb_key:
        raise HttpError(400, "pas de clé TMDB : renseigne TMDB_KEY sur le serveur")
    jobs = h.hub.jobs
    h.send_json({"job": (jobs.enrich_sagas if lib.kind == "movies" else jobs.enrich)(lib, bool(body.get("force"))).public()}, 202)


@route("POST", r"/api/cleanup")
def api_cleanup(h, m, q):
    require_admin(h)
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
