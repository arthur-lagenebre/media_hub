"""Le serveur HTTP : l'API JSON, le streaming des fichiers, et l'interface web (hub/web).

Bibliothèque standard uniquement. Un fil par connexion (ThreadingHTTPServer) : un film qui se lit n'empêche pas de feuilleter.

Pas de chemin de fichier dans l'API : le client ne connaît que des identifiants, le serveur retrouve le fichier dans la base. Un identifiant inconnu est un 404, jamais une lecture de disque.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import socket
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
from .. import countries
from .. import enrich
from .. import missing
from .. import people
from .. import playlists
from .. import prefs
from .. import urls
from .. import sagas as sagafile
from ..sagas import Sagas
from ..db import Database, make_id
from ..inbox import Inbox, InboxError
from ..media import books as bookmedia
from ..media import probe as prober
from ..media import subs as subtitles
from ..media.images import ConvertError, Images, NoConverter, crop_box, sniff, to_jpeg, write_atomic
from ..scan.common import STACK_MAX
from .jobs import Jobs

API_VERSION = 1             # le contrat décrit dans docs/api.md ; n'augmente qu'en cas de rupture (champ retiré, renommé ou de sens différent)
CHUNK = 256 * 1024
INBOX_DRAIN_SECONDS = 5    # un envoi refusé (POST /api/inbox) laisse au client ce temps, au plus, pour finir d'envoyer son corps avant que la connexion soit coupée
INBOX_DRAIN_BYTES = 64 << 20    # ... et ce nombre d'octets, au plus : un client qui n'arrête jamais d'envoyer est coupé à la première borne atteinte
SESSION_DAYS = 90
WATCHED_AT = 0.92          # au-delà de cette part, le film est vu
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
        self.inbox = Inbox(cfg)                                # les envois des comptes (voir inbox.py)
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
        return {"id": accounts.FIRST_ID, "name": self.cfg.admin, "role": "admin", "can_upload": False}        # sans compte, personne n'envoie : un envoi anonyme serait une porte ouverte sur le disque

    def media_token(self, row):
        """Le jeton de média du compte `row`, celui qui voyage dans les URLs (`?token=`) : il ne vaut que sur /media, /img, /book et /api/subs (sous-titres, lecture seule), nulle part ailleurs sur /api/ ;
        une URL de pochette qui fuit ne donne donc ni le nettoyage ni le scan. Il désigne le compte : ses droits (voir, télécharger) s'appliquent à la requête."""
        return accounts.media_token(self.secret, row)

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
        return self.known("entries", ident, "fichier inconnu")

    def item(self, ident):
        return self.known("items", ident, "élément inconnu")

    def known(self, table, ident, unknown):
        row = self.db.one(f"SELECT * FROM {table} WHERE id=?", (ident,))
        if row is None:
            raise HttpError(404, unknown)
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
MIN_VOTES = 25          # en dessous, une moyenne TMDB tient à quelques avis : elle ne se montre pas


def shown_rating(rating):
    """La note TMDB gardée (voir enrich.rating_of) si elle mérite d'être montrée : une moyenne, sur au moins MIN_VOTES votes ; None sinon."""
    if isinstance(rating, dict) and rating.get("vote") and (rating.get("votes") or 0) >= MIN_VOTES:
        return rating
    return None


def typed_summary(row, meta):
    """Ce que seul un genre d'élément porte : la durée et l'artiste d'un album, les épisodes manquants et la pile d'affiches d'une série."""
    out = {}
    if row["type"] == "album":
        if meta.get("duration"):
            out["duration"] = meta["duration"]
        if meta.get("artist_id"):
            out["artist_id"] = meta["artist_id"]
    if row["type"] == "series":
        if meta.get("missing"):
            out["missing"] = sum(len(m["numbers"]) for m in meta["missing"])
        if meta.get("stack"):
            out["cover_urls"] = [urls.item_cover(row["id"])] + [urls.entry_image(e) for e in meta["stack"]][:STACK_MAX - 1]
    return out


def summary(row, seen=None):
    meta = safe_json(row["meta"], {})
    out = {"id": row["id"], "type": row["type"], "title": row["title"], "year": row["year"], "grp": row["grp"], "n": row["count"], "letter": row["letter"],
           "added": row["added"], "genres": safe_json(row["genres"], []), "cover": urls.item_cover(row["id"]), **typed_summary(row, meta)}
    rating = shown_rating(meta.get("tmdb_rating"))
    if rating:
        out["tmdb_rating"], out["tmdb_votes"] = rating["vote"], rating["votes"]
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
        if info.get("artists"):                # ARTISTS du fichier ; absent d'une piste que media_library n'a pas encore repassée
            out["artists"] = info["artists"]
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
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def range_bounds(header, size):
    """(début, fin, statut) de ce que demande l'en-tête Range pour un fichier de `size` octets : 206 pour une partie, 200 pour le tout (pas d'en-tête, ou un qu'on ne comprend pas) ; None quand la partie demandée sort du fichier (416)."""
    found = RANGE_RE.match(header.strip()) if header else None
    first, last = found.groups() if found else ("", "")
    if not (first or last):
        return 0, size - 1, 200
    if first:
        start, end = int(first), int(last) if last else size
    else:
        start, end = max(0, size - int(last)), size
    end = min(end, size - 1)
    if start > end:
        return None
    return start, end, 206


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"media_hub/{__version__}"
    timeout = 120
    hub: Hub = None
    user: dict = None                          # le compte de la requête en cours (voir authorized)
    granted: dict = None                       # ses droits par bibliothèque, lus à la première question (voir can)
    body_consumed = 0                          # les octets du corps que la route a déjà lus (voir api_inbox_send)
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
            bounds = range_bounds(self.headers.get("Range"), size)
            if bounds is None:
                self.send_body(b"", mime, 416, {"Content-Range": f"bytes */{size}"})
                return
            start, end, status = bounds
            self.send_file_headers(mime, status, start, end, size, headers)
            if self.command != "HEAD":
                self.copy_range(handle, start, end - start + 1)

    def send_file_headers(self, mime, status, start, end, size, headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def copy_range(self, handle, start, length):
        """Envoie `length` octets du fichier à partir de `start`, par morceaux."""
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

    @property
    def lang(self):
        """'fr' ou 'en' : la langue choisie dans l'interface (cookie `hub_lang`) ; le français quand rien n'est choisi."""
        return page_language(self, {})[0]

    # --- Aiguillage -------------------------------------------------------
    def session_user(self):
        """Le compte du jeton de session, en cookie puis en `Authorization: Bearer` ; None s'il n'y en a pas ou s'il ne vaut rien."""
        cookie = self.headers.get("Cookie", "")
        found = re.search(r"(?:^|;\s*)hub=(\w{1,64}\.[0-9a-f]{64})\s*(?:;|$)", cookie, re.ASCII)       # valeur entière, sans rien derrière : `hub=<id>.<mac>.junk` ne passe pas
        bearer = re.fullmatch(r"Bearer\s+(\S+)", self.headers.get("Authorization", "").strip(), re.IGNORECASE)
        for token in (found.group(1) if found else None, bearer.group(1) if bearer else None):
            row = accounts.user_for_token(self.hub.db, self.hub.secret, token) if token else None
            if row is not None:
                return accounts.public(row)
        return None

    def authorized(self, path, query):
        """Trois façons de se présenter : le jeton de session (`<identifiant>.<mac>`) en cookie (navigateur) ou en `Authorization: Bearer` (clients natifs),
        le jeton de média en `?token=` (balises <img> / <video>, Cast), celui-ci seulement sur les fichiers et les sous-titres (/api/subs/), nulle part ailleurs sur /api/.

        Le compte reconnu est posé sur `self.user` (`{"id", "name", "role"}`) : c'est lui, et lui seul, qui désigne le profil de la progression et des playlists, et dont les droits s'appliquent.
        Le jeton de média est celui d'un compte : il le désigne aussi, mais ne sert que sur les routes de fichiers."""
        self.user = self.granted = None
        if not self.hub.auth():
            self.user = self.hub.open_user()
            return True
        if path in ("/login", "/api/login", "/api/token", "/favicon.svg", "/icon.svg"):
            return True
        self.user = self.session_user()
        if self.user is not None:
            return True
        if path.startswith(("/media/", "/img/", "/book/", "/api/subs/")) and "token" in query:
            row = accounts.user_for_token(self.hub.db, self.hub.secret, query["token"], "media")
            self.user = accounts.public(row) if row is not None else None
        return self.user is not None

    def can(self, lib_id, perm="view"):
        """Le compte de la requête peut-il `view` (voir), `play` (lire en ligne) ou `download` (télécharger) cette bibliothèque ? Un administrateur le peut toujours ; sans rien dans `rights`, tout est permis."""
        if self.user is None:
            return False
        if self.user["role"] != "admin" and self.granted is None:
            self.granted = accounts.grants(self.hub.db, self.user["id"])
        return accounts.allows(self.user["role"], self.granted or {}, lib_id, perm)

    def require(self, lib_id, perm="view"):
        """403 quand le compte n'a pas ce droit sur la bibliothèque."""
        if not self.can(lib_id, perm):
            raise HttpError(403, {"download": "téléchargement non autorisé", "play": "lecture non autorisée"}.get(perm, "accès à cette bibliothèque non autorisé"))

    def visible_libs(self):
        """Les bibliothèques que le compte peut voir, dans l'ordre de la configuration."""
        return [lib for lib in self.hub.cfg.libraries if self.can(lib.id)]

    def visible_ids(self, perm="view"):
        """Les identifiants des bibliothèques où le compte a ce droit (`view` par défaut), ou None pour un administrateur (tout, y compris ce qu'une bibliothèque retirée de la configuration a laissé dans la base)."""
        return None if self.user is not None and self.user["role"] == "admin" else {lib.id for lib in self.hub.cfg.libraries if self.can(lib.id, perm)}

    def find_route(self, path):
        """(fonction, correspondance) de la première route qui répond à la méthode et au chemin ; HEAD répond aux routes GET. None s'il n'y en a pas."""
        for method, pattern, func in self.routes:
            if method == self.command or (self.command == "HEAD" and method == "GET"):
                found = pattern.fullmatch(path)
                if found:
                    return func, found
        return None

    def dispatch(self):
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = {k: v[-1] for k, v in parse_qs(split.query).items()}
        self.body_taken = False                       # le corps du POST a-t-il été lu ? sinon _fail le jette (connexion persistante)
        self.body_consumed = 0
        try:
            if not self.authorized(path, query):
                if path.startswith("/api/"):
                    raise HttpError(401, "connexion requise")
                self.send_body(b"", "text/plain", 302, {"Location": "/login"})
                return
            found = self.find_route(path)
            if found:
                found[0](self, found[1], query)
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

    do_GET = do_HEAD = do_POST = do_DELETE = dispatch

    def _fail(self, status, message):
        sent = True
        try:
            self.send_json({"error": self.hub.scrub(message)}, status)
        except OSError:
            self.close_connection, sent = True, False
        if self.command in ("POST", "DELETE") and not self.body_taken:      # refus avant toute lecture (401, 404, route qui lève trop tôt) : le corps ne doit pas coller à la requête suivante
            self.drop_body()
        if status == 500 or status > 501:
            self.log_failure(status, message)
        if sent and self.close_connection:
            self.let_the_reply_arrive()

    def drop_body(self):
        """Jette le corps d'une requête refusée sans l'avoir lue ; la connexion se coupe si le socket n'y arrive pas."""
        try:
            self.discard_body()
        except OSError:
            self.close_connection = True

    def log_failure(self, status, message):
        """Une erreur du serveur (500, 502 et plus) au journal ; le jeton n'y va pas : masqué d'après les clés décodées (`?%74oken=` aussi)."""
        split = urlsplit(self.path)
        pairs = [(k, "***" if k == "token" else v) for k, v in parse_qsl(split.query, keep_blank_values=True)]
        shown = split.path + ("?" + urlencode(pairs, safe="*") if pairs else "")
        print(f"{status} {self.command} {shown} : {message}", file=sys.stderr)

    def inbox_unread(self):
        """Pour un envoi (`POST /api/inbox`) : ce que le client peut encore envoyer du corps annoncé par `Content-Length`, moins ce que la route en a lu. 0 pour toute autre requête, et quand la taille n'est pas annoncée proprement (voir announced_length)."""
        if self.command != "POST" or unquote(urlsplit(self.path).path) != "/api/inbox":
            return 0
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1 or self.headers.get("Transfer-Encoding") is not None or not LENGTH_RE.fullmatch(values[0].strip()):
            return 0
        return max(0, int(values[0]) - self.body_consumed)

    def let_the_reply_arrive(self):
        """Appelé quand la connexion va être coupée après une réponse d'erreur. Un envoi refusé de la boîte de réception, dont le client n'a pas fini d'envoyer le corps : fermer une socket qui a encore des octets non lus fait partir un RST, qui peut effacer la réponse déjà écrite (le navigateur voit une erreur réseau au lieu du 403, 415, 429 ou 507). On ferme donc l'écriture, puis on lit et on jette le corps encore envoyé, borné par INBOX_DRAIN_SECONDS et INBOX_DRAIN_BYTES ; la connexion est coupée ensuite par l'appelant.
        Ne fait rien pour les autres routes, ni quand tout le corps a déjà été lu (ou qu'un envoi accepté n'a pas échoué) ; ce qui est déjà reçu se lit et se jette sans attendre."""
        left = min(self.inbox_unread(), INBOX_DRAIN_BYTES)
        if left <= 0:
            return
        deadline = time.monotonic() + INBOX_DRAIN_SECONDS
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            while left > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(CHUNK, left))
                if not chunk:                           # le client a fini ou fermé
                    break
                left -= len(chunk)
        except OSError:                                 # le client est parti, ou n'envoie plus : la connexion se coupe quand même
            pass

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
            self.body_consumed = length

    def body_bytes(self, limit):
        """Le corps de la requête tel quel (un fichier envoyé) ; 413 au-delà de `limit` octets (la connexion est alors coupée : le corps n'est pas lu)."""
        self.body_taken = True
        length = self.content_length()
        if length > limit:
            self.close_connection = True
            raise HttpError(413, f"fichier trop gros (au plus {limit >> 20} Mo)")
        return self.rfile.read(length)

    # --- Fichiers du site -------------------------------------------------
    def static_target(self, path):
        """Le fichier du site que demande `path` ; 404 s'il sort du dossier du site ou si c'est un fichier (avec une extension) qui n'existe pas, sinon la page d'accueil."""
        web = self.hub.cfg.web.resolve()
        name = path.lstrip("/") or "index.html"
        target = (web / name).resolve()
        if web not in target.parents and target != web:
            raise HttpError(404, "introuvable")
        if target.is_file():
            return target
        if "." in name.rsplit("/", 1)[-1]:
            raise HttpError(404, "introuvable")
        return web / "index.html"          # route de l'application : elle se débrouille côté navigateur

    def static(self, path):
        target = self.static_target(path)
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
LOGIN_PAGE = """<!doctype html><html lang="%(lang)s"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%(title)s</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e8e8ea;font:18px system-ui,sans-serif}form{display:grid;gap:14px;width:min(360px,86vw)}
h1{font-size:26px;margin:0 0 8px}input,button{font:inherit;padding:14px 16px;border-radius:12px;border:1px solid #2a2c34;background:#1c1e26;color:inherit}button{background:#7cc4ff;color:#0b1220;border:0;font-weight:700;cursor:pointer}
input:focus,button:focus{outline:3px solid #7cc4ff}p{color:#ff8b8b;margin:0;min-height:1.2em}a{color:#9aa0ad;font-size:15px;text-align:center}</style>
<form method="post" action="/api/login"><h1>%(title)s</h1>%(field)s<input type="password" name="password" placeholder="%(password)s" autofocus autocomplete="current-password"><button>%(enter)s</button><p>%(shown)s</p><a href="/login?lang=%(other)s">%(other_name)s</a></form></html>"""
# La page de connexion est la seule page que le serveur écrit lui-même : sa langue est celle du cookie `hub_lang` (posé par l'interface), ou de `?lang=` quand on la change ici.
LOGIN_LANGS = {
    "fr": {"title": "Médiathèque", "password": "Mot de passe", "name": "Nom", "enter": "Entrer", "other": "en", "other_name": "English",
           "errors": {"1": "Nom ou mot de passe incorrect.", "2": "Trop d'essais : réessaie dans quelques minutes."}},
    "en": {"title": "Media library", "password": "Password", "name": "Name", "enter": "Sign in", "other": "fr", "other_name": "Français",
           "errors": {"1": "Wrong name or password.", "2": "Too many attempts: try again in a few minutes."}},
}
LANG_COOKIE = "hub_lang"


def page_language(h, query):
    """('fr' | 'en', cookie à poser ou None) : `?lang=` d'abord, sinon le cookie `hub_lang` ; le français quand rien n'est choisi."""
    asked = query.get("lang")
    if asked in LOGIN_LANGS:
        return asked, f"{LANG_COOKIE}={asked}; Path=/; Max-Age=31536000; SameSite=Lax"
    found = re.search(rf"(?:^|;\s*){LANG_COOKIE}=(fr|en)\s*(?:;|$)", h.headers.get("Cookie", ""))
    return (found.group(1) if found else "fr"), None


@route("GET", r"/login")
def login_page(h, m, q):
    lang, cookie = page_language(h, q)
    words = LOGIN_LANGS[lang]
    field = f'<input type="text" name="user" placeholder="{words["name"]}" autocomplete="username" autofocus>' if len(accounts.rows(h.hub.db)) > 1 else ""     # seulement quand il y a plusieurs comptes
    page = LOGIN_PAGE % {**words, "lang": lang, "field": field, "shown": words["errors"].get(q.get("error", ""), "")}
    headers = {"Cache-Control": "no-store"}
    if cookie:
        headers["Set-Cookie"] = cookie
    h.send_body(page.encode("utf-8"), "text/html; charset=utf-8", 200, headers, h.command == "HEAD")


def from_loopback(h):
    """La connexion vient-elle de la machine elle-même (127.0.0.1, ::1) ? C'est le cas de tout ce qui passe par `tailscale serve` / Funnel : le conteneur tailscale partage le réseau de media_hub.
    Seule une telle connexion a le droit de dire, par ses en-têtes `X-Forwarded-*`, qui est le vrai client ; une connexion du réseau local ou du tailnet n'est jamais crue sur ces en-têtes."""
    try:
        address = ipaddress.ip_address(h.client_address[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)            # ::ffff:127.0.0.1 : une prise à double pile montre ainsi une connexion IPv4
    return (mapped or address).is_loopback


def forwarded(h, name):
    """La dernière valeur de l'en-tête `name` (`X-Forwarded-For`, `X-Forwarded-Proto`) quand la connexion vient de la boucle locale ; None sinon, ou s'il est absent.
    La dernière, jamais la première : le proxy ajoute la sienne à la fin de ce que le client a pu écrire (plusieurs lignes d'en-tête comprises)."""
    if not from_loopback(h):
        return None
    last = ",".join(h.headers.get_all(name, [])).rpartition(",")[2].strip()
    return last or None


def client_ip(h):
    """L'adresse réelle du client, la clé de l'anti-force-brute (`Throttle`) : derrière le proxy, la dernière entrée de `X-Forwarded-For` ; sinon l'adresse de la connexion.
    L'en-tête ne compte que s'il arrive de la boucle locale et contient une adresse valide ; sinon (absent, illisible, ou envoyé par le réseau local) on retombe sur `client_address[0]`.
    Une adresse IPv6 est comptée par réseau /64 (« 2001:db8::/64 »), une IPv4 par adresse."""
    try:
        address = ipaddress.ip_address(forwarded(h, "X-Forwarded-For") or "")
    except ValueError:
        return h.client_address[0]
    address = getattr(address, "ipv4_mapped", None) or address      # ::ffff:203.0.113.9 et 203.0.113.9 : une seule clé
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))        # un abonné a tout un /64 : changer d'adresse à chaque essai ne doit pas échapper au compte
    return str(address)


def secure_request(h):
    """La requête est-elle arrivée en HTTPS ? Seul le dit `X-Forwarded-Proto: https` venu de la boucle locale ; en `http://` direct (la télé sur le réseau local) la réponse est que non."""
    return (forwarded(h, "X-Forwarded-Proto") or "").lower() == "https"


def cookie_flags(secure):
    return "; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")


def session_cookie(hub, row, secure=False):
    return f"hub={hub.session_token(row)}; Path=/; Max-Age={SESSION_DAYS * 86400}{cookie_flags(secure)}"


def ended_session_cookie(secure=False):
    """Le cookie qui efface la session : les mêmes attributs que celui qui l'a posée, sinon le navigateur peut le garder."""
    return f"hub=; Path=/; Max-Age=0{cookie_flags(secure)}"


def login_credentials(h):
    """(nom, mot de passe) du formulaire de connexion (application/x-www-form-urlencoded) ou d'un corps JSON ; 4 Ko au plus sont lus."""
    h.body_taken = True
    length = h.content_length()
    if length > 4096:
        h.close_connection = True                   # le reste du corps ne sera pas lu : la connexion ne peut plus servir
    raw = h.rfile.read(min(length, 4096)).decode("utf-8", "replace")
    if "=" in raw and not raw.lstrip().startswith("{"):
        form = parse_qs(raw)
        return form.get("user", [""])[0], form.get("password", [""])[0]
    body = safe_json(raw, {})
    body = body if isinstance(body, dict) else {}
    return body.get("user"), body.get("password", "")


@route("POST", r"/api/login")
def login(h, m, q):
    name, password = login_credentials(h)
    who = client_ip(h)
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
        h.send_body(b"", "text/plain", 303, {"Location": "/", "Set-Cookie": session_cookie(h.hub, row, secure_request(h))})
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
    who = client_ip(h)
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
    h.send_json({"token": h.hub.session_token(row), "media_token": h.hub.media_token(row), "user": accounts.public(row)})


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
    return {"ok": True, "token": token}, {"Set-Cookie": session_cookie(h.hub, row, secure_request(h))}


@route("GET", r"/api/users")
def api_users(h, m, q):
    require_admin(h)
    h.send_json({"users": [user_listing(h.hub, r) for r in accounts.rows(h.hub.db)]})


@route("POST", r"/api/users")
def api_user_create(h, m, q):
    require_admin(h)
    need_accounts(h)
    body = body_dict(h)
    row = account_call(accounts.create, h.hub.db, body.get("name"), body.get("password"), body.get("role", "user"))
    h.send_json(user_listing(h.hub, row), 201)


def user_listing(hub, row):
    """Un compte pour l'administrateur : ce que `accounts.listing` dit, et ses droits sur chaque bibliothèque configurée."""
    return {**accounts.listing(row), "rights": accounts.rights_json(row["role"], accounts.grants(hub.db, row["id"]), [lib.id for lib in hub.cfg.libraries])}


@route("POST", r"/api/user/(\w+)/rights")
def api_user_rights(h, m, q):
    """L'administrateur change ce qu'un compte peut voir, lire et télécharger : `{"rights": {"<bibliothèque>": {"view": bool, "play": bool, "download": bool}}}`, seules les bibliothèques citées changent
    (et un droit omis ne change pas). Retirer « voir » retire « lire » et « télécharger ». Pas pour un administrateur (400). Rend le compte avec ses droits."""
    require_admin(h)
    need_accounts(h)
    body = body_dict(h)
    account_call(accounts.set_rights, h.hub.db, m.group(1), {lib.id for lib in h.hub.cfg.libraries}, body.get("rights"))
    h.send_json(user_listing(h.hub, accounts.by_id(h.hub.db, m.group(1))))


@route("POST", r"/api/user/(\w+)/upload")
def api_user_upload(h, m, q):
    """L'administrateur donne ou retire le droit d'envoyer dans la boîte de réception : `{"upload": bool}`. Pas pour un administrateur (400 : il envoie toujours). Rend le compte. Valable à la requête suivante."""
    require_admin(h)
    need_accounts(h)
    account_call(accounts.set_can_upload, h.hub.db, m.group(1), body_dict(h).get("upload"))
    h.send_json(user_listing(h.hub, accounts.by_id(h.hub.db, m.group(1))))


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
    h.send_json({"ok": True}, 200, {"Set-Cookie": ended_session_cookie(secure_request(h))} if m.group(1) == h.user["id"] else None)


def change_own_session(h, change):
    """Ce qui change les jetons du compte de la requête et demande son mot de passe actuel : `change(compte, corps)` renvoie le compte mis à jour, la réponse porte le nouveau jeton."""
    need_accounts(h)
    body = body_dict(h)
    row = check_current(h, body.get("current"))
    out, headers = renewed_session(h, change(row, body))
    h.send_json(out, 200, headers)


@route("POST", r"/api/me/password")
def api_me_password(h, m, q):
    """Chacun change son mot de passe, en donnant l'actuel."""
    change_own_session(h, lambda row, body: account_call(accounts.set_password, h.hub.db, row["id"], body.get("new")))


@route("POST", r"/api/me/signout-others")
def api_me_signout_others(h, m, q):
    """Chacun déconnecte tous ses appareils sauf celui-ci, en donnant son mot de passe (sans lui, un jeton volé suffirait à mettre dehors son propriétaire)."""
    change_own_session(h, lambda row, body: account_call(accounts.revoke_sessions, h.hub.db, row["id"]))


@route("GET", r"/api/me/settings")
def api_me_settings(h, m, q):
    """Les réglages d'affichage du compte (gardés sur le serveur : ils le suivent d'un appareil à l'autre)."""
    h.send_json({"settings": prefs.load(h.hub.db, h.user["id"])})


@route("POST", r"/api/me/settings")
def api_me_settings_save(h, m, q):
    """Change un ou plusieurs réglages du compte, `{"music_view": "artists"}` ; rend tous les réglages. 400 pour un réglage ou une valeur inconnus."""
    try:
        settings = prefs.save(h.hub.db, h.user["id"], h.body_json())
    except ValueError as e:
        raise HttpError(400, str(e))
    h.send_json({"settings": settings})


@route("GET", r"/api/logout")
def logout(h, m, q):
    h.send_body(b"", "text/plain", 303, {"Location": "/login", "Set-Cookie": ended_session_cookie(secure_request(h))})


# ---------------------------------------------------------------------------
# État général, accueil, recherche
# ---------------------------------------------------------------------------
def library_or_404(hub, lib_id):
    lib = hub.cfg.library(lib_id)
    if lib is None:
        raise HttpError(404, "bibliothèque inconnue")
    return lib


def entry_of(h, ident, perm="view"):
    """Le fichier `ident` (404 s'il n'existe pas), à condition que le compte ait ce droit sur sa bibliothèque (403 sinon)."""
    entry = h.hub.entry(ident)
    h.require(entry["lib"], perm)
    return entry


def item_of(h, ident):
    """L'élément `ident` (404 s'il n'existe pas), à condition que le compte voie sa bibliothèque (403 sinon)."""
    item = h.hub.item(ident)
    h.require(item["lib"])
    return item


def seen_counts(hub, profile, lib_id=None):
    """{item_id: (fichiers vus, fichiers entamés)} : de quoi afficher « vu » ou « 3/12 » sur une vignette."""
    sql = ("SELECT e.item_id AS item, SUM(p.done) AS seen, COUNT(*) AS touched FROM progress p JOIN entries e ON e.id=p.entry_id "
           "WHERE p.profile=? AND e.kind != 'audio'" + (" AND e.lib=?" if lib_id else "") + " GROUP BY e.item_id")
    params = (profile, lib_id) if lib_id else (profile,)
    return {r["item"]: (r["seen"] or 0, r["touched"]) for r in hub.db.query(sql, params)}


def with_seen(row, seen):
    done, touched = watched(row, seen)
    out = summary(row)
    if touched:
        out["seen"] = done
        out["started"] = touched - done
    return out


@route("GET", r"/api/state")
def api_state(h, m, q):
    hub = h.hub
    libs = []
    for lib in h.visible_libs():                  # une bibliothèque qu'on ne voit pas n'existe pas pour ce compte
        count = hub.db.one("SELECT COUNT(*) AS n FROM items WHERE lib=?", (lib.id,))["n"]
        libs.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "count": count, "scanned": hub.db.kv_get(f"scanned:{lib.id}"), "exists": lib.path.is_dir(), "play": h.can(lib.id, "play"), "download": h.can(lib.id, "download")})
    h.send_json({"version": __version__, "libraries": libs, "ffmpeg": bool(hub.cfg.ffmpeg), "ffprobe": bool(hub.cfg.ffprobe), "tmdb": bool(hub.cfg.tmdb_key),
                 "auth": hub.auth(), "user": h.user, "settings": prefs.load(hub.db, h.user["id"]), "api": API_VERSION, "busy": hub.jobs.busy(), "streams": hub.active, "max_streams": hub.cfg.max_streams})


def next_entry(hub, entry):
    """Le fichier qui suit celui-ci dans son élément (épisode suivant, tome suivant, partie suivante), ou None."""
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (entry["item_id"],)), key=order_key)
    for index, row in enumerate(rows):
        if row["id"] == entry["id"]:
            return rows[index + 1] if index + 1 < len(rows) else None
    return None


def dismiss_key(profile, item_id):
    return f"dismiss:{profile}:{item_id}"


def progress_card(entry, item, position, total, resume, updated):
    card = entry_json(entry)
    card.update({"item": summary(item), "position": position, "total": total or entry["duration"], "resume": resume, "updated": updated})
    return card


def finished_card(hub, profile, row, item):
    """Un fichier fini ne se reprend pas : on propose l'épisode ou le tome qui le suit, s'il y en a un et qu'il n'est pas fini lui aussi. Un film fini n'a rien à proposer."""
    if item["type"] == "movie":
        return None
    following = next_entry(hub, row)
    if following is None:
        return None
    progress = hub.db.progress_for(profile, [following["id"]]).get(following["id"])
    if progress is not None and progress["done"]:
        return None
    position, total = (progress["position"], progress["duration"]) if progress else (0, 0)
    return progress_card(following, item, position, total, False, row["updated"])


def resume_card(hub, profile, row, item):
    """La carte « Reprendre » d'un fichier de la progression, ou None quand il n'y a rien à proposer (fini sans suite, à peine commencé)."""
    if row["done"]:
        return finished_card(hub, profile, row, item)
    if row["position"] < (1 if row["kind"] == "book" else RESUME_MIN):
        return None
    return progress_card(row, item, row["position"], row["pduration"], True, row["updated"])


def continue_row(hub, profile, libs=None):
    """Ce qu'on a commencé : le film ou l'épisode à reprendre, l'épisode qui suit celui qu'on vient de finir, le tome suivant. `libs` : les bibliothèques où le compte peut lire (None : toutes) ; on ne propose pas de reprendre ce qu'il ne peut pas lire."""
    rows = hub.db.query(
        "SELECT p.entry_id, p.position, p.duration AS pduration, p.done, p.updated, e.* FROM progress p JOIN entries e ON e.id=p.entry_id "
        "WHERE p.profile=? AND e.kind IN ('video','book') ORDER BY p.updated DESC LIMIT 200", (profile,))
    seen_items, out = set(), []
    for row in rows:
        if row["item_id"] in seen_items or (libs is not None and row["lib"] not in libs):
            continue
        seen_items.add(row["item_id"])
        if row["updated"] <= hub.db.kv_get(dismiss_key(profile, row["item_id"]), -1):
            continue
        item = hub.db.one("SELECT * FROM items WHERE id=?", (row["item_id"],))
        card = resume_card(hub, profile, row, item) if item is not None else None
        if card is None:
            continue
        out.append(card)
        if len(out) >= 24:
            break
    return out


today = missing.today                       # le jour (« 2026-11-04 ») : un nom à part pour que les tests puissent le fixer
UNSEEN = 48                                 # ce que le serveur offre par bibliothèque : le navigateur en retire ce que « Nouveautés » (24) montre déjà, puis coupe à `home_unseen_count` (24 au plus) ; à 24 seulement, les tris « date d'ajout » et « note » (les mieux notés sont souvent les plus récents) vidaient le rayon


def unseen_row(rows, seen, profile, day, n=UNSEEN, sort="random"):
    """Les films ou les séries d'une bibliothèque que ce compte n'a pas encore touchés (ni vus, ni commencés : aucune progression sur aucun de leurs fichiers ; c'est l'état « à voir » de watch_state), `n` au plus.

    L'ordre est choisi par le réglage `home_unseen_sort` du compte, et appliqué avant la coupe à `n` : `random` (défaut) ne dépend que du compte et du jour (le même toute la journée, un autre demain) ; `rating` : la note TMDB montrée, de la meilleure à la moins bonne (comme le tri « Mieux notés » de la bibliothèque : ce qui a moins de MIN_VOTES votes ou aucune note vient à la fin) ; `added` : le plus récemment ajouté d'abord."""
    pool = [r for r in rows if r["id"] not in seen]
    if sort == "rating":
        pool.sort(key=rating_key)
    elif sort == "added":
        pool.sort(key=lambda r: (-r["added"], r["sort"]))
    else:
        pool.sort(key=lambda r: hashlib.sha1(f"{day}:{profile}:{r['id']}".encode()).hexdigest())
    return [summary(r) for r in pool[:n]]


UNSEEN_KINDS = ("movies", "shows")          # les genres de bibliothèque qui ont un rayon « Pas encore vu » : une nouvelle bibliothèque vidéo d'un de ces genres en a un sans autre réglage


@route("GET", r"/api/home")
def api_home(h, m, q):
    hub = h.hub
    seen = seen_counts(hub, h.user["id"])
    day, rows = today(), []
    order = prefs.load(hub.db, h.user["id"])["home_unseen_sort"]
    for lib in h.visible_libs():
        items = hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,))
        if lib.kind == "music":
            recent = recent_music(items, seen, 24)
        else:
            recent = [with_seen(r, seen) for r in hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY added DESC, sort LIMIT 24", (lib.id,))]
        row = {"id": lib.id, "label": lib.label, "kind": lib.kind, "recent": recent, "count": len(items)}
        if lib.kind in UNSEEN_KINDS:
            row["unseen"] = unseen_row(items, seen, h.user["id"], day, sort=order)
        rows.append(row)
    h.send_json({"continue": continue_row(hub, h.user["id"], h.visible_ids("play")), "libraries": rows})


@route("GET", r"/api/search")
def api_search(h, m, q):
    needle = fold(q.get("q", "")).strip()
    if len(needle) < 2:
        h.send_json({"results": [], "people": []})
        return
    hub = h.hub
    seen = seen_counts(hub, h.user["id"])
    results = []
    for lib in h.visible_libs():
        found = []
        for row in hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY sort", (lib.id,)):
            if needle in fold(row["title"]) or needle in fold(row["grp"]):
                found.append(with_seen(row, seen))
                if len(found) >= 40:
                    break
        if found:
            results.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "items": found})
    hits = people.search(hub.db, people.key_of(needle), [lib.id for lib in h.visible_libs()])
    h.send_json({"results": results, "people": [{"name": name, "titles": titles} for name, titles in hits]})


def items_by_id(db, ids):
    """Les lignes de ces éléments, par paquets (SQLite limite le nombre de paramètres)."""
    ids, rows = list(ids), []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows += db.query(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return rows


@route("GET", r"/api/person")
def api_person(h, m, q):
    """Ce qu'une personne a réalisé ou joué dans les films et séries que ce compte voit. Un nom inconnu, ou connu seulement d'une bibliothèque qu'il ne voit pas, répond pareil : rien."""
    name = q.get("name", "")
    if len(name) > people.MAX_NAME:
        raise HttpError(400, f"nom trop long ({people.MAX_NAME} caractères au plus)")
    if not name.strip():
        raise HttpError(400, "nom attendu : /api/person?name=<nom>")
    hub = h.hub
    libs = h.visible_libs()
    person = people.find(hub.db, name)
    shown = people.spelling(person, [lib.id for lib in libs]) if person else None
    person = person if shown else None              # ce que le compte ne voit pas n'existe pas pour lui
    roles, rows = person_rows(hub.db, person, {lib.id for lib in libs})
    seen = seen_counts(hub, h.user["id"]) if rows else {}
    h.send_json({"name": shown or " ".join(name.split()), "roles": roles, "results": person_results(libs, rows, seen), **person_extras(hub, person, libs)})


def person_rows(db, person, visible):
    """({rôle: nombre d'éléments}, {id: ligne}) des éléments que cette personne (None : personne) a réalisés ou joués dans les bibliothèques `visible`."""
    roles, rows = {role: 0 for role in people.ROLES}, {}
    for role in people.ROLES:
        for row in items_by_id(db, person[role] if person else ()):
            if row["lib"] in visible:
                roles[role] += 1
                rows[row["id"]] = row
    return roles, rows


def person_results(libs, rows, seen):
    """Les éléments `rows` groupés par bibliothèque (dans l'ordre de `libs`, sans les bibliothèques vides), du plus récent au plus ancien."""
    results = []
    for lib in libs:
        mine = sorted((r for r in rows.values() if r["lib"] == lib.id), key=lambda r: r["sort"])
        mine.sort(key=lambda r: r["year"] or "", reverse=True)          # sans année en dernier ; à égalité, par titre (le tri est stable)
        if mine:
            results.append({"id": lib.id, "label": lib.label, "kind": lib.kind, "items": [with_seen(r, seen) for r in mine]})
    return results


def person_extras(hub, person, libs):
    """`enrichable` (le bouton « films manquants » a un sens : un identifiant TMDB unique et une clé) et, quand il y en a, `missing` : les films que TMDB attribue à cette personne (voir enrich.enrich_person) et qu'aucune bibliothèque de films de `libs` n'a, comparés par identifiant TMDB du film à chaque requête."""
    ident = people.tmdb_id(person, [lib.id for lib in libs])
    known = hub.db.kv_get(enrich.person_key(ident)) if ident else None
    films = known.get("films") if isinstance(known, dict) else None
    absent = missing.of_person(films, missing.owned_films(hub.db, libs)) if films else []
    return {"enrichable": bool(ident and hub.cfg.tmdb_key), **({"missing": absent} if absent else {})}


@route("POST", r"/api/enrich/people")
def api_enrich_people(h, m, q):
    """Demande à TMDB les films des réalisateurs et acteurs (`{"lib": "<id>", "force": false}`) : les réalisateurs et les acteurs de premier plan, pour que `/api/person` dise ce qui manque à chacun. Sans `lib` : les bibliothèques de films ; avec : cette bibliothèque (films, séries ou animations), une tâche par catégorie. Administrateur seulement ; tâche de fond (202, `job`) : une heure ou plus la première fois, une personne connue depuis moins d'un mois n'est pas redemandée (sauf `force`)."""
    require_admin(h)
    body = h.body_json()
    hub = h.hub
    if not hub.cfg.tmdb_key:
        raise HttpError(400, "pas de clé TMDB : renseigne TMDB_KEY sur le serveur")
    libs, label = [lib for lib in hub.cfg.libraries if lib.kind == "movies"], "bibliothèques de films"
    if "lib" in body:
        if not isinstance(body["lib"], str):
            raise HttpError(400, 'bibliothèque attendue : {"lib": "<identifiant>"}')
        lib = library_or_404(hub, body["lib"])
        if lib.kind not in ("movies", "shows"):
            raise HttpError(400, "les films manquants des personnes ne concernent que les bibliothèques de films, de séries et d'animations")
        libs, label = [lib], lib.label
    h.send_json({"job": hub.jobs.enrich_people(libs, label, bool(body.get("force"))).public()}, 202)


@route("POST", r"/api/enrich/person")
def api_enrich_person(h, m, q):
    """Demande à TMDB les films d'une personne (`{"name": "<nom>", "force": false}`), pour que `/api/person` dise lesquels manquent. Administrateur seulement ; tâche de fond (202, `job`).
    Le nom doit désigner une seule personne ayant un identifiant TMDB dans les étiquettes : voir people.tmdb_id."""
    require_admin(h)
    body = h.body_json()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > people.MAX_NAME:
        raise HttpError(400, 'nom attendu : {"name": "<nom>"}')
    hub = h.hub
    if not hub.cfg.tmdb_key:
        raise HttpError(400, "pas de clé TMDB : renseigne TMDB_KEY sur le serveur")
    person = people.find(hub.db, name)
    if person is None:
        raise HttpError(404, "personne inconnue")
    libs = [lib.id for lib in h.visible_libs()]
    shown = people.spelling(person, libs) or " ".join(name.split())
    ident = people.tmdb_id(person, libs)
    if ident is None and not people.ids_seen(person, libs):
        raise HttpError(400, f"« {shown} » n'a pas d'identifiant TMDB : les étiquettes de ses films ne le portent pas")
    if ident is None:
        raise HttpError(400, f"plusieurs personnes s'appellent « {shown} » : impossible de savoir laquelle enrichir")
    h.send_json({"job": hub.jobs.enrich_person(shown, ident, bool(body.get("force"))).public()}, 202)


# ---------------------------------------------------------------------------
# Une bibliothèque
# ---------------------------------------------------------------------------
SORTS = {
    "title": (lambda r: r["sort"], False),
    "added": (lambda r: (r["added"], r["sort"]), True),
    "year": (lambda r: (r["year"] or "0000", r["sort"]), True),
    "year-asc": (lambda r: (r["year"] or "9999", r["sort"]), False),
    "part": (lambda r: release_key(r), False),      # l'ordre d'une saga
    "rating": (lambda r: rating_key(r), False),     # « Mieux notés » : la clé range elle-même du mieux au moins bien noté
}
# Le tri « Titre » de la musique et des livres range par artiste / collection ; chez un artiste, les albums vont du plus ancien au plus récent, puis par nom (ceux sans année en dernier)
TITLE_SORTS = {
    "music": lambda r: (fold(r["grp"]) or "￿", r["year"] or "9999", r["sort"]),
    "books": lambda r: (fold(r["grp"]), r["sort"]),
}


def row_rating(row):
    """(note, votes) d'une ligne : sa note TMDB montrée (voir shown_rating), ou pour une vignette de saga celle de son meilleur film (à égalité, le plus voté) : une saga vaut ce qu'elle a de mieux, Terminator vaut Terminator 2 ; (None, 0) sans note montrée."""
    if isinstance(row, dict) and "members" in row:
        return max(map(row_rating, row["members"]), key=lambda found: (found[0] is not None, found[0] or 0, found[1]), default=(None, 0))
    rating = shown_rating(safe_json(row["meta"], {}).get("tmdb_rating"))
    return (rating["vote"], rating["votes"]) if rating else (None, 0)


def rating_key(row):
    """Le tri « Mieux notés », sans inversion : d'abord ce qui a une note montrée, de la meilleure à la moins bonne, puis le plus de votes ; ensuite le reste ; à égalité, par titre."""
    vote, votes = row_rating(row)
    return (vote is None, -(vote or 0), -votes, row["sort"])


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
            return (2, "")
        if name in shelf.SECTION_ORDER:
            return (0, shelf.SECTION_ORDER.index(name))
        return (1, fold(name))
    return [[name, counts[name]] for name in sorted(counts, key=rank)]


def watched(row, seen):
    """(fichiers vus, position en cours) de ce que le compte a regardé de cet élément ; (0, 0) quand il n'y a pas touché."""
    return seen.get(row["id"], (0, 0))


def film_seen(row, seen):
    return watched(row, seen)[0] >= max(1, row["count"])


def saga_groups(rows, custom):
    """(sagas, home) : {nom: [(film, rang)]} pour chaque saga d'au moins deux films de la bibliothèque, dans l'ordre de la saga ; {id d'un film: nom de la saga qui le range dans la grille}.

    `custom` : les sagas du fichier (voir sagas.py). Elles passent avant la collection de l'étiquette, et la première écrite avant les suivantes : un film n'a qu'une vignette. Une saga du fichier qui porte le nom d'une saga de l'étiquette la remplace (page, fiche et grille s'accordent sur ce que ce nom désigne), même si elle n'a qu'un film ici.
    """
    sagas = {name: films for name, films in custom.items() if len(films) > 1}
    tagged = tagged_groups(rows, custom)
    home = {}
    for name in sagas:
        for film, _ in sagas[name]:
            home.setdefault(film["id"], name)
    for row in rows:
        if row["type"] == "movie" and row["grp"] in tagged:
            home.setdefault(row["id"], row["grp"])
    sagas.update({name: list(zip(films, range(1, len(films) + 1))) for name, films in tagged.items()})
    return sagas, home


def tagged_groups(rows, custom):
    """{nom: films par date de sortie} des sagas de l'étiquette qui ont au moins deux films et que `custom` (les sagas du fichier) ne remplace pas."""
    tagged = {}
    for row in rows:
        if row["type"] == "movie" and row["grp"] and row["grp"] not in custom:
            tagged.setdefault(row["grp"], []).append(row)
    return {name: sorted(films, key=release_key) for name, films in tagged.items() if len(films) > 1}


def collapse_sagas(rows, custom):
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
                    "meta": "{}", "added": max(m["added"] for m in members), "count": len(members), "members": members})
    return out


def saga_json(saga, seen):
    done = sum(1 for m in saga["members"] if film_seen(m, seen))
    started = sum(1 for m in saga["members"] if not film_seen(m, seen) and watched(m, seen)[1])
    out = {"id": saga["id"], "lib": saga["lib"], "type": "saga", "title": saga["title"], "year": saga["year"], "years": saga["years"], "grp": "", "n": saga["count"], "letter": saga["letter"],
           "added": saga["added"], "genres": safe_json(saga["genres"], []), "covers": [m["id"] for m in saga["members"][:STACK_MAX]],
           "cover_urls": [urls.item_cover(m["id"]) for m in saga["members"][:STACK_MAX]]}
    if done:
        out["seen"] = done
    if started:
        out["started"] = started
    return out


UNKNOWN_ARTISTS = ("artiste inconnu", "various artists", "artistes divers")        # rangés en dernier : ce ne sont pas des artistes qu'on cherche par leur nom


def artist_name(grp):
    """Le nom que montre la carte d'un artiste : « Various Artists » se dit en français, un album sans artiste est « Artiste inconnu »."""
    return "Artistes divers" if fold(grp) == "various artists" else grp or "Artiste inconnu"


def artist_rows(rows):
    """Les albums d'une bibliothèque de musique regroupés par artiste : une ligne par artiste, de la forme d'une ligne d'item, si bien que les filtres, le tri et la pagination de la bibliothèque s'y appliquent
    tels quels. `title` est le nom de l'artiste, `count` son nombre d'albums, `meta.album` le plus ancien (sa pochette, quand il n'a pas de photo) ; `albums` les titres de tous, pour que la recherche d'un
    album trouve son artiste. Les artistes sans nom connu viennent en dernier."""
    groups = {}
    for row in rows:
        groups.setdefault(safe_json(row["meta"], {}).get("artist_id") or fold(row["grp"]), []).append(row)
    out = []
    for key, albums in groups.items():
        albums.sort(key=lambda r: (r["year"] or "9999", r["sort"]))
        name = artist_name(albums[0]["grp"])
        out.append({"id": key, "lib": albums[0]["lib"], "type": "artist", "title": name, "grp": name, "letter": initial(name), "year": "", "summary": "",
                    "sort": ("~" if fold(name) in UNKNOWN_ARTISTS else "") + fold(name), "genres": json.dumps(sorted({g for a in albums for g in safe_json(a["genres"], [])}), ensure_ascii=False),
                    "meta": json.dumps({"album": albums[0]["id"]}), "added": max(a["added"] for a in albums), "count": len(albums), "albums": [a["title"] for a in albums]})
    return out


def artist_card(row):
    """La carte d'un artiste dans la liste : son identifiant (celui de sa fiche, `/api/artist/<lib>/<id>`), son nom, son nombre d'albums et l'album dont on prendra la pochette faute de photo."""
    return {"id": row["id"], "lib": row["lib"], "type": "artist", "title": row["title"], "n": row["count"], "letter": row["letter"], "added": row["added"],
            "album": safe_json(row["meta"], {}).get("album", "")}


ARTIST_ARRIVAL = 7 * 24 * 3600       # les albums d'un artiste arrivés à moins d'une semaine les uns des autres sont l'arrivée de l'artiste


def recent_music(rows, seen, limit):
    """Les derniers ajouts d'une bibliothèque de musique, du plus récent au plus ancien. Un album ajouté à un artiste qu'on avait déjà reste un album ; l'arrivée d'un artiste (au moins deux albums,
    tous arrivés à une semaine d'écart au plus) n'est qu'une carte, la sienne. Les compilations et les artistes inconnus restent des albums : les regrouper cacherait ce qui est arrivé."""
    groups = {}
    for row in rows:
        groups.setdefault(safe_json(row["meta"], {}).get("artist_id") or fold(row["grp"]), []).append(row)
    entries = []
    for albums in groups.values():
        added = [a["added"] for a in albums]
        arrived = len(albums) > 1 and max(added) - min(added) <= ARTIST_ARRIVAL and fold(albums[0]["grp"]) not in UNKNOWN_ARTISTS
        entries += artist_rows(albums) if arrived else albums
    entries.sort(key=lambda r: (-r["added"], r["sort"]))
    return [artist_card(r) if r["type"] == "artist" else with_seen(r, seen) for r in entries[:limit]]


def library_counts(rows, kind):
    """Ce que la barre latérale compte : ({genre: n}, {section: n}, {id: section}). Les sections, pour les livres seulement."""
    genres = Counter(genre for row in rows for genre in safe_json(row["genres"], []))
    of = {row["id"]: section_of(row) for row in rows} if kind == "books" else {}
    return genres, Counter(of.values()), of


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


WATCH_STATES = ("todo", "progress", "done")      # les filtres de bibliothèque `filter=` ; `unseen`, l'ancien nom de `todo`, est encore accepté pour les favoris


def watch_state(row, seen):
    """Où en est le compte d'une vignette : `todo` (« À voir » : aucun fichier entamé, la règle du rayon « Pas encore vu » de l'accueil), `done` (« Terminé » : tous ses fichiers vus), `progress` (« En cours » : le reste).

    Un film ou une série se jugent sur leurs fichiers, d'après `seen` (voir seen_counts : fichiers vus, fichiers entamés, vus compris) : une série dont 3 épisodes sur 10 sont vus est en cours, vue en entier elle est terminée, et un seul épisode entamé la sort de « à voir ». Une vignette de saga ne compte pas pour elle-même : ses films comptent un par un, terminée si tous le sont, à voir si aucun n'est entamé, en cours sinon."""
    if row["type"] == "saga":
        states = {watch_state(film, seen) for film in row["members"]}
        return "done" if states == {"done"} else "todo" if states <= {"todo"} else "progress"
    if film_seen(row, seen):
        return "done"
    return "progress" if row["id"] in seen else "todo"


def library_filters(q, kind, ranks, of, in_custom, seen):
    """Les conditions que la requête pose à une vignette (saga, section, genre, recherche, à voir / en cours / terminés) ; elles s'ajoutent."""
    genre, group = q.get("genre", ""), q.get("group", "")
    section = q.get("section", "") if kind == "books" else ""
    needle = fold(q.get("q", "")).strip()
    tests = []
    if group:
        tests.append(partial(in_group, group=group, ranks=ranks))
    if section:
        tests.append(partial(in_section, section=section, of=of))
    if genre:
        tests.append(partial(has_genre, genre=genre))
    if needle:
        tests.append(partial(matches_needle, needle=needle, in_custom=in_custom))
    state = {"unseen": "todo"}.get(q.get("filter"), q.get("filter"))
    if state in WATCH_STATES:
        tests.append(lambda row: watch_state(row, seen) == state)
    return tests


def library_order(kind, q, ranks, artists=False):
    """(tri, clé, sens) du tri demandé ; la musique n'a qu'un ordre : par artiste (dans la liste des artistes, par leur nom, les inconnus à la fin)."""
    sort = "title" if kind == "music" else q.get("sort", "title")
    key, reverse = SORTS.get(sort, SORTS["title"])
    if sort == "title" and kind in TITLE_SORTS and not artists:
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
    if row["type"] == "artist":
        return artist_card(row)
    return saga_json(row, seen) if row["type"] == "saga" else with_seen(row, seen)


def artist_key(row):
    """Ce qui regroupe les albums d'un même artiste (voir artist_rows)."""
    return safe_json(row["meta"], {}).get("artist_id") or fold(row["grp"])


def music_counts(chosen, artists):
    """(artistes, albums) de ce que la musique montre : en mode artistes, une carte par artiste portant son nombre d'albums ; en mode albums, une carte par album, et les artistes sont ceux de ces albums."""
    if artists:
        return len(chosen), sum(row["count"] for row in chosen)
    return len({artist_key(row) for row in chosen}), len(chosen)


def genre_menu(genres):
    """Les 60 genres les plus fréquents (à égalité, par nom), rangés par ordre alphabétique sans accent ni casse : c'est l'ordre du menu de l'interface."""
    top = sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:60]
    return sorted(top, key=lambda kv: (fold(kv[0]), kv[0]))


def library_extras(out, kind, sections, absent, sagas, chosen, artists=False):
    """Les champs que la réponse n'a que dans certains cas : les sections des livres, les films qui manquent à une saga, le décompte des sagas d'une grille de sagas (un film de deux sagas ne compte qu'une fois),
    le nombre d'artistes et d'albums de la musique (de tout ce que filtrent les critères, pas seulement de la page)."""
    if kind == "books":
        out["sections"] = sections_json(sections)
    if kind == "music":
        out["artists"], out["albums"] = music_counts(chosen, artists)
    if absent:
        out["missing"] = absent
    if sagas:
        out["sagas"] = sum(1 for r in chosen if r["type"] == "saga")
        out["films"] = len({m["id"] for r in chosen for m in (r["members"] if r["type"] == "saga" else [r])})


@route("GET", r"/api/library/([\w-]+)")
def api_library(h, m, q):
    hub = h.hub
    lib = library_or_404(hub, m.group(1))
    h.require(lib.id)
    rows = hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,))
    seen = seen_counts(hub, h.user["id"], lib.id)
    movies = lib.kind == "movies"
    custom = hub.sagas.of_movies(rows) if movies else {}          # les sagas du fichier, voir sagas.py
    extra_names = sagas_of_films(custom)
    artists = lib.kind == "music" and q.get("view") == "artists"
    if artists:                                                   # la liste des artistes, sans leurs albums : un album se cherche par son titre, et trouve son artiste
        rows = artist_rows(rows)
        extra_names = {row["id"]: row["albums"] for row in rows}
    group = q.get("group", "")
    sagas = movies and q.get("sagas") == "1" and not group
    if sagas:
        rows = collapse_sagas(rows, custom)
    genres, sections, of = library_counts(rows, lib.kind)
    ranks, absent = group_ranks(hub, rows, custom, group, movies)
    tests = library_filters(q, lib.kind, ranks, of, extra_names, seen)
    chosen = [row for row in rows if all(test(row) for test in tests)]
    sort, key, reverse = library_order(lib.kind, q, ranks, artists)
    chosen.sort(key=key, reverse=reverse)
    offset, limit = page_bounds(q)
    page = chosen[offset:offset + limit]
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "total": len(chosen), "offset": offset,
           "items": [library_item(r, seen) for r in page],
           "genres": genre_menu(genres)}
    if group and sort == "part":
        number_films(out["items"], ranks, offset + 1)
    library_extras(out, lib.kind, sections, absent, sagas, chosen, artists)
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


MBID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def album_links(meta):
    """Le lien d'un album vers sa sortie sur MusicBrainz, quand les fichiers en donnent l'identifiant (tous le même) ; vide sinon."""
    mbid = str(meta.get("mbid") or "")
    return [{"label": "MusicBrainz", "url": musicbrainz.release_url(mbid)}] if MBID_RE.fullmatch(mbid) else []


ITEM_META_KEYS = ("directors", "writers", "actors", "artist", "artist_id", "artist_mbids", "label", "catalog", "formats", "authors", "publisher", "section", "collection", "status", "episodes_total",
                  "seasons_total", "tmdb", "missing", "unit", "units", "years", "discs", "disc_titles", "release_types", "season_summaries", "doubts")


def tagged_saga(hub, item, movies, seen, info):
    """(films, manquants) de la saga de l'étiquette d'un film : numérotés d'après la collection TMDB quand on la connaît, sinon par date de sortie."""
    tagged = sorted((r for r in movies if r["grp"] == item["grp"]), key=release_key)
    ranks, absent = missing.tagged(item["grp"], tagged, info, set(hub.sagas.ignored()))
    if ranks is not None:
        tagged.sort(key=lambda r: ranks[r["id"]])
    return number_films([with_seen(r, seen) for r in tagged], ranks), absent


def file_saga(name, films, listed, seen, owned, info):
    """Une saga de `sagas.json` : ses films numérotés d'après leur place dans la liste, et ceux de la liste qu'on n'a pas."""
    numbered = number_films([with_seen(f, seen) for f, _ in films], {f["id"]: rank for f, rank in films})
    return {"name": name, "films": numbered, "missing": missing.listed(listed.get(name, []), owned, info)}


def movie_sagas(hub, item, lib, user):
    """Ses sagas : celle de l'étiquette d'abord (`saga`, gardée pour les clients d'avant `sagas`), puis celles du fichier (sagas.py), chacune avec les films qui la composent et ceux qui lui manquent."""
    movies = hub.db.query("SELECT * FROM items WHERE lib=? AND type='movie'", (item["lib"],))
    seen = seen_counts(hub, user, lib.id)
    custom = hub.sagas.of_movies(movies)
    mine = {name: films for name, films in custom.items() if any(f["id"] == item["id"] for f, _ in films)}
    info, owned = (missing.load(hub.db), sagafile.by_tmdb(movies)) if item["grp"] or mine else (None, None)          # TMDB ne sert que s'il y a une saga
    out, sagas = {}, []
    if item["grp"]:
        out["saga"], absent = tagged_saga(hub, item, movies, seen, info)
        if item["grp"] not in custom:                   # une saga du fichier de même nom la remplace : c'est elle que ce nom désigne dans la grille
            sagas.append({"name": item["grp"], "films": out["saga"], "missing": absent})
    listed = dict(hub.sagas.all())
    sagas += [file_saga(name, films, listed, seen, owned, info) for name, films in mine.items()]
    sagas = visible_sagas(sagas)
    if sagas:
        out["sagas"] = sagas
    return out


def visible_sagas(sagas):
    """Les sagas de plus d'un film (présents ou manquants), sans la liste des manquants quand elle est vide."""
    return [{k: v for k, v in s.items() if v or k != "missing"} for s in sagas if len(s["films"]) + len(s["missing"]) > 1]


@route("GET", r"/api/item/(\w+)")
def api_item(h, m, q):
    hub = h.hub
    item = item_of(h, m.group(1))
    lib = library_or_404(hub, item["lib"])
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (item["id"],)), key=order_key)
    progress = hub.db.progress_for(h.user["id"], [r["id"] for r in rows])
    meta = safe_json(item["meta"], {})
    out = summary(item)
    out.update({"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "summary": item["summary"], "tagged": bool(meta.get("tagged"))})
    out.update({key: meta[key] for key in ITEM_META_KEYS if meta.get(key) not in (None, "", [])})
    links = album_links(meta) if item["type"] == "album" else video_links(item, meta)
    if links:
        out["links"] = links
    out["entries"] = [entry_json(r, progress.get(r["id"])) for r in rows]
    out["resume"] = resume_target(rows, progress)
    if item["type"] == "show":
        out["seasons"] = meta.get("seasons", [])
    if item["type"] == "movie":
        out.update(movie_sagas(hub, item, lib, h.user["id"]))
    if item["type"] == "album":
        out["tracks_total"] = len(rows)
    h.send_json(out)


def artist_sheet(info, mbid="", lang="fr", edits=None):
    """Ce que la fiche montre d'un artiste, à partir de ce que MusicBrainz et Wikipédia en ont dit (voir libraries.music.artistinfo). Le pays est traduit en français pour `lang` = 'fr', sinon tel que MusicBrainz le donne.
    `edits` (voir artist_link_edits) : les liens que l'administrateur a ajoutés (avant Wikipédia et MusicBrainz) et retirés (de la liste finale, quels qu'ils soient)."""
    members = [{"name": mem["name"], "roles": mem.get("roles", []), "period": artistinfo.period(mem), "former": bool(mem.get("ended"))} for mem in info.get("members") or []]
    edits = edits or {}
    links = enrich.pick_links(info.get("links", []))       # aussi à l'affichage : les fiches déjà gardées gardent peut-être d'autres liens
    links.extend(edits.get("added", []))
    if (info.get("wiki") or {}).get("url"):
        links.append({"label": "Wikipédia", "url": info["wiki"]["url"]})
    if mbid:
        links.append({"label": "MusicBrainz", "url": musicbrainz.artist_url(mbid)})
    removed = set(edits.get("removed", []))
    seen = set()
    links = [link for link in links if link["url"] not in removed and not (link["url"] in seen or seen.add(link["url"]))]       # une adresse une seule fois : une fiche refaite peut avoir trouvé ce que l'administrateur avait ajouté
    return {"facts": artistinfo.facts({**info, "area": countries.french(info.get("area")) if lang == "fr" else info.get("area")}), "disambiguation": info.get("disambiguation", ""), "wiki": info.get("wiki"), "members": members, "links": links}


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


LINK_URL_MAX = 2000
LINK_LABEL_MAX = 80
LINKS_LOCK = threading.Lock()           # lire-modifier-écrire de `artistlinks:` : deux administrateurs en même temps ne s'écrasent pas


def artist_link_edits(hub, mbid):
    """{'removed': [url], 'added': [{'label', 'url'}]} gardés pour cet artiste (voir enrich.links_key) ; vides s'il n'y a rien, ou si ce qui est gardé est abîmé."""
    kept = hub.db.kv_get(enrich.links_key(mbid))
    kept = kept if isinstance(kept, dict) else {}
    removed = [u for u in kept.get("removed") or [] if isinstance(u, str)]
    added = [{"label": str(a.get("label", "")), "url": a["url"]} for a in kept.get("added") or [] if isinstance(a, dict) and isinstance(a.get("url"), str)]
    return {"removed": removed, "added": added}


def artist_link_url(value):
    """L'adresse d'un lien à ajouter, nettoyée ; 400 si ce n'est pas une adresse web (http ou https, avec un nom d'hôte : jamais `javascript:` ni `data:`) ou si elle dépasse 2000 caractères."""
    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, "adresse du lien manquante")
    url = value.strip()
    if len(url) > LINK_URL_MAX:
        raise HttpError(400, f"adresse trop longue ({LINK_URL_MAX} caractères au plus)")
    try:
        parts = urlsplit(url)
        web = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        web = False
    if not web or any(c.isspace() or ord(c) < 32 for c in url):
        raise HttpError(400, "l'adresse doit être un lien web (http:// ou https://)")
    return url


def artist_link_target(hub, m):
    """(mbid, fiche gardée) de l'artiste visé par une route de liens ; 404 s'il est inconnu ou n'a pas de fiche (sans identifiant MusicBrainz, ou « Fiches artistes » ne l'a pas encore gardée)."""
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    kept = artist_kept(hub, rows)
    if kept is None:
        raise HttpError(404, "pas de fiche pour cet artiste")
    return kept["mbid"], kept


def artist_links_now(h, kept, edits):
    """Les liens de la fiche tels qu'elle les montre, modifications comprises."""
    return artist_sheet(kept["info"], kept["mbid"], h.lang, edits)["links"]


PHOTO_UPLOAD_MAX = 10 << 20


def artist_home(lib, rows):
    """Le dossier de l'artiste à la racine de la médiathèque (voir scan.music.artist_dirs), où va sa photo ; None si le dernier scan ne lui en a pas trouvé ou s'il n'est plus là."""
    for row in rows:
        name = safe_json(row["meta"], {}).get("artist_dir")
        if name and (lib.path / name).is_dir():
            return lib.path / name
    return None


def artist_photo(hub, artist_id, kept, home=None):
    """Le fichier de la photo d'un artiste, la seule qu'il ait (celle que l'utilisateur a envoyée) ; None s'il n'en a pas."""
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


def artist_tracks(hub, rows, name):
    """(pistes, {genre: nombre d'albums}) de tous les albums d'un artiste, les pistes dans l'ordre de chaque album."""
    tracks, genres = [], {}
    for row in rows:
        for entry in sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (row["id"],)), key=order_key):
            tracks.append({"id": entry["id"], "title": entry["title"], "artist": entry_json(entry)["artist"] or name, "album": row["title"], "item": row["id"], "duration": entry["duration"],
                           "cover": urls.item_cover(row["id"]), "file": urls.entry_file(entry["id"])})
        for genre in safe_json(row["genres"], []):
            genres[genre] = genres.get(genre, 0) + 1
    return tracks, genres


@route("GET", r"/api/artist/([\w-]+)/(\w+)")
def api_artist(h, m, q):
    """La fiche d'un artiste ou d'un groupe : ses albums de la médiathèque, de quoi tout lire, ses genres, et ce que MusicBrainz et Wikipédia en disent (une fois les fiches demandées dans Gestion), et sa photo si l'utilisateur en a envoyé une."""
    hub = h.hub
    h.require(m.group(1))
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    seen = seen_counts(hub, h.user["id"], lib.id)
    name = rows[0]["grp"] or "Artiste inconnu"
    tracks, genres = artist_tracks(hub, rows, name)
    out = {"library": {"id": lib.id, "label": lib.label, "kind": lib.kind}, "id": m.group(2), "name": name, "albums": [with_seen(r, seen) for r in rows], "tracks": tracks}
    out["genres"] = [g for g, _ in sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    out["duration"] = sum(t["duration"] or 0 for t in out["tracks"])
    kept = artist_kept(hub, rows)
    if kept:
        out["info"] = artist_sheet(kept["info"], kept["mbid"], h.lang, artist_link_edits(hub, kept["mbid"]))
    out["wanted"] = [{"year": w["year"], "title": w["title"], "cover": f"/img/wanted/{lib.id}/{m.group(2)}/{w['key']}" if w["cover"] else ""} for w in wanted_albums(hub, lib.id, m.group(2))]
    out["photo"] = artist_photo(hub, m.group(2), kept, artist_home(lib, rows)) is not None
    if out["photo"]:
        out["image"] = urls.artist_image(lib.id, m.group(2))
    h.send_json(out)


def artist_link_label(label, url):
    """Le libellé d'un lien : celui qu'on donne (espaces compactés), sinon le nom du site ; 400 s'il n'est pas un texte ou s'il est trop long."""
    if label is not None and not isinstance(label, str):
        raise HttpError(400, "libellé du lien invalide")
    label = " ".join((label or "").split()) or artistinfo.host_label(url)
    if len(label) > LINK_LABEL_MAX:
        raise HttpError(400, f"libellé trop long ({LINK_LABEL_MAX} caractères au plus)")
    return label


@route("POST", r"/api/artist/([\w-]+)/(\w+)/links")
def api_artist_link_add(h, m, q):
    """Ajoute un lien à la fiche d'un artiste : corps `{"url", "label"?}` (adresse http ou https ; sans libellé, le nom du site). Ajouter une adresse retirée plus tôt annule le retrait. Aucune requête n'est faite vers l'adresse. Rend `{"links": [...]}`, les liens de la fiche à jour. 404 si l'artiste n'a pas de fiche. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    mbid, kept = artist_link_target(hub, m)
    body = h.body_json()
    url = artist_link_url(body.get("url"))
    label = artist_link_label(body.get("label"), url)
    with LINKS_LOCK:
        edits = artist_link_edits(hub, mbid)
        was_removed = url in edits["removed"]
        edits["removed"] = [u for u in edits["removed"] if u != url]
        if any(link["url"] == url for link in artist_links_now(h, kept, edits)):
            if not was_removed:
                raise HttpError(400, "ce lien est déjà sur la fiche")
        else:
            edits["added"].append({"label": label, "url": url})
        if edits["removed"] or edits["added"]:
            hub.db.kv_set(enrich.links_key(mbid), edits)
        else:
            hub.db.kv_delete(enrich.links_key(mbid))
    h.send_json({"links": artist_links_now(h, kept, edits)})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/links/remove")
def api_artist_link_remove(h, m, q):
    """Retire un lien de la fiche d'un artiste, automatique ou ajouté, Wikipédia et MusicBrainz compris : corps `{"url"}`. Rend `{"links": [...]}`, les liens de la fiche à jour. 404 si l'artiste n'a pas de fiche ou si l'adresse n'y est pas. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    mbid, kept = artist_link_target(hub, m)
    url = h.body_json().get("url")
    if not isinstance(url, str) or not url.strip():
        raise HttpError(400, "adresse du lien manquante")
    url = url.strip()
    with LINKS_LOCK:
        edits = artist_link_edits(hub, mbid)
        if not any(link["url"] == url for link in artist_links_now(h, kept, edits)):
            raise HttpError(404, "ce lien n'est pas sur la fiche")
        edits["added"] = [link for link in edits["added"] if link["url"] != url]       # un lien ajouté à la main s'oublie tout simplement
        if any(link["url"] == url for link in artist_links_now(h, kept, edits)):       # encore là : c'est aussi un lien de la fiche
            edits["removed"].append(url)
        if edits["removed"] or edits["added"]:
            hub.db.kv_set(enrich.links_key(mbid), edits)
        else:
            hub.db.kv_delete(enrich.links_key(mbid))
    h.send_json({"links": artist_links_now(h, kept, edits)})


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


@route("GET", r"/img/artist/([\w-]+)/(\w+)")
def artist_image(h, m, q):
    """La photo d'un artiste, réduite comme les pochettes ; 404 s'il n'en a pas (la page garde alors la pochette de son premier album)."""
    hub = h.hub
    h.require(m.group(1))
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
    h.require(m.group(1))
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
    item = item_of(h, m.group(1))
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
def module_call(failure, func, *args):
    """Appelle `func` ; l'erreur `failure` d'un module (elle porte un `status` et un `message`) devient la réponse HTTP."""
    try:
        return func(*args)
    except failure as e:
        raise HttpError(e.status, e.message)


playlist_call = partial(module_call, playlists.PlaylistError)


@route("GET", r"/api/playlists")
def api_playlists(h, m, q):
    h.send_json({"playlists": playlists.listing(h.hub.db, h.user["id"], h.visible_ids())})


@route("POST", r"/api/playlists")
def api_playlist_create(h, m, q):
    body = h.body_json()
    entries = body.get("entries") if isinstance(body.get("entries"), list) else []
    pid = playlist_call(playlists.create, h.hub.db, h.user["id"], body.get("name"), entries, h.visible_ids())
    h.send_json(playlist_call(playlists.detail, h.hub.db, h.user["id"], pid, h.visible_ids()), 201)


@route("GET", r"/api/playlist/(\w+)")
def api_playlist(h, m, q):
    h.send_json(playlist_call(playlists.detail, h.hub.db, h.user["id"], m.group(1), h.visible_ids()))


@route("POST", r"/api/playlist/(\w+)/(add|remove|move|rename|delete)")
def api_playlist_edit(h, m, q):
    db, pid, action, body, profile, libs = h.hub.db, m.group(1), m.group(2), h.body_json(), h.user["id"], h.visible_ids()
    if action == "add":
        playlist_call(playlists.add, db, profile, pid, body.get("entries") if isinstance(body.get("entries"), list) else [], libs)
    elif action == "remove":
        playlist_call(playlists.remove, db, profile, pid, body.get("index"), libs)
    elif action == "move":
        playlist_call(playlists.move, db, profile, pid, body.get("from"), body.get("to"), libs)
    elif action == "rename":
        playlist_call(playlists.rename, db, profile, pid, body.get("name"))
    else:
        playlist_call(playlists.delete, db, profile, pid)
        h.send_json({"ok": True})
        return
    h.send_json(playlist_call(playlists.detail, db, profile, pid, libs))


# ---------------------------------------------------------------------------
# Progression
# ---------------------------------------------------------------------------
def is_finished(kind, position, duration):
    if duration <= 0:
        return False
    if kind == "book":
        return position >= duration - 1
    return position / duration >= WATCHED_AT


def progress_numbers(body):
    """(position, durée) envoyées par le lecteur, positives ; 400 si ce ne sont pas des nombres finis."""
    try:
        position, duration = max(0.0, float(body.get("position", 0))), max(0.0, float(body.get("duration", 0)))
    except (TypeError, ValueError):
        raise HttpError(400, "position invalide")
    if not (math.isfinite(position) and math.isfinite(duration)):          # `Infinity` passe json.loads : stocké, il rendrait la fiche du compte illisible pour JSON.parse
        raise HttpError(400, "position invalide")
    return position, duration


@route("POST", r"/api/progress")
def api_progress(h, m, q):
    body = h.body_json()
    entry = entry_of(h, str(body.get("entry", "")))
    position, duration = progress_numbers(body)
    duration = duration or entry["duration"]
    done = bool(body.get("done")) or is_finished(entry["kind"], position, duration)
    if entry["kind"] == "audio":                 # la musique n'a pas de reprise : seulement « écouté »
        done = done or (duration > 0 and position / duration > 0.8)
    h.hub.db.save_progress(h.user["id"], entry["id"], duration if done and entry["kind"] != "book" else position, duration, done)
    h.send_json({"ok": True, "done": done})


@route("POST", r"/api/item/(\w+)/dismiss")
def api_item_dismiss(h, m, q):
    """Retire un élément de « Reprendre » sans toucher à la progression : il y revient dès qu'on le relance."""
    hub = h.hub
    h.body_json()                 # le corps doit être lu, même vide de sens : sinon il reste sur la connexion et colle à la requête suivante (501)
    item = item_of(h, m.group(1))
    latest = hub.db.one("SELECT MAX(p.updated) AS t FROM progress p JOIN entries e ON e.id=p.entry_id WHERE p.profile=? AND e.item_id=?", (h.user["id"], item["id"]))
    hub.db.kv_set(dismiss_key(h.user["id"], item["id"]), latest["t"] or 0)
    h.send_json({"ok": True})


@route("POST", r"/api/entry/(\w+)/seen")
def api_entry_seen(h, m, q):
    hub = h.hub
    entry = entry_of(h, m.group(1))
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


LABEL_WORDS = {"fr": {"track": "Piste", "forced": "Forcé", "stereo": "stéréo", "subtitles": "Sous-titres"},
               "en": {"track": "Track", "forced": "Forced", "stereo": "stereo", "subtitles": "Subtitles"}}


def track_label(track, lang="fr"):
    words = LABEL_WORDS.get(lang, LABEL_WORDS["fr"])
    parts = [subtitles.language(track["lang"]) or (f"{words['track']} {track['index'] + 1}" if not track["title"] else ""), track["title"]]
    codec = track.get("codec", "")
    extra = {"ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS", "truehd": "TrueHD", "aac": "AAC", "flac": "FLAC", "opus": "Opus"}.get(codec, codec.upper())
    channels = {1: "mono", 2: words["stereo"], 6: "5.1", 8: "7.1"}.get(track.get("channels"), "")
    detail = " ".join(x for x in (extra, channels) if x)
    return " · ".join(x for x in parts if x) + (f" ({detail})" if detail else "")


def sub_label(track, lang="fr"):
    words = LABEL_WORDS.get(lang, LABEL_WORDS["fr"])
    parts = [subtitles.language(track["lang"]) or f"{words['track']} {track['index'] + 1}", track["title"], words["forced"] if track.get("forced") else ""]
    return " · ".join(x for x in parts if x)


def side_label(label, lang="fr"):
    """Le libellé d'un fichier de sous-titres voisin (« Français · Forcé »), dans la langue de la page : seuls les mots du hub changent, pas la langue ni le nom du fichier."""
    if lang == "fr":
        return label
    swap = {LABEL_WORDS["fr"]["forced"]: LABEL_WORDS[lang]["forced"], LABEL_WORDS["fr"]["subtitles"]: LABEL_WORDS[lang]["subtitles"]}
    return " · ".join(swap.get(part, part) for part in label.split(" · "))


def subtitle_list(hub, entry, probe, path, lang="fr"):
    out = []
    for sub in probe["subs"]:
        image = "pgs" if subtitles.is_pgs(sub.get("codec")) else ""
        out.append({"id": f"e{sub['index']}", "label": sub_label(sub, lang), "lang": sub["lang"], "forced": sub["forced"], "default": sub["default"], "text": sub["text"], "image": image, "where": "mkv"})
    for side in subtitles.side_files(path):
        out.append({"id": f"s{side['index']}", "label": side_label(side["label"], lang), "lang": side["lang"], "forced": side["forced"], "default": False, "text": True, "image": "", "where": "fichier"})
    for sub in out:
        extension = "vtt" if sub["text"] else "sup" if sub["image"] else ""
        sub["url"] = f"/api/subs/{entry['id']}/{sub['id']}.{extension}" if extension else ""
    return out


def play_settings(body):
    """(capacités du lecteur, piste audio demandée, débit et hauteur demandés) d'après le corps de la requête ; 400 si un réglage n'est pas un nombre."""
    caps = body.get("caps") or {}
    try:
        audio = int(body["audio"]) if body.get("audio") is not None else None
        return caps, audio, int(body.get("max_kbps") or 0), int(body.get("max_height") or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "réglages invalides")


def play_url(entry, decision, caps, kbps, height):
    """Où le lecteur va chercher le fichier : tel quel (avec des Range) ou converti à la volée par /stream."""
    base = f"/media/{entry['id']}"
    if decision["mode"] == "direct":
        return f"{base}/file"
    args = {"m": decision["mode"], "ca": int(decision["copy_audio"]), "k": kbps, "h": height, "c": min(int(caps.get("channels") or 2), 6)}
    if decision["audio"] is not None:
        args["a"] = decision["audio"]
    return f"{base}/stream?" + "&".join(f"{k}={v}" for k, v in args.items())


def default_height(decision, video, height):
    """La hauteur demandée ; à défaut, 1080 p pour un 4K à convertir (en pleine taille, il ferait ramer le processeur pour rien)."""
    if decision["mode"] == "transcode" and not height and video.get("height", 0) > 1080:
        return 1080
    return height


def show_title(hub, entry):
    """Le titre de la série d'un épisode ; vide pour un film."""
    item = hub.db.one("SELECT title, type FROM items WHERE id=?", (entry["item_id"],))
    return item["title"] if item and item["type"] == "show" else ""


def play_warning(decision, video):
    if decision["mode"] == "transcode" and video.get("height", 0) >= 2000 and video.get("codec") in ("hevc", "av1"):
        return "Image 4K à convertir : le serveur n'a pas de carte vidéo pour cela, la lecture risque de saccader. Le débit demandé peut être baissé."
    return ""


def resume_position(hub, user, entry):
    """Où reprendre : la position enregistrée, tant que le fichier n'est pas fini et qu'on n'en est pas au tout début."""
    progress = hub.db.progress_for(user, [entry["id"]]).get(entry["id"])
    return progress["position"] if progress and not progress["done"] and progress["position"] >= RESUME_MIN else 0


def next_summary(following):
    """Le fichier vidéo qui suit celui-ci (épisode suivant, partie suivante), ou None."""
    if following and following["kind"] == "video":
        return {"id": following["id"], "title": following["title"], "season": following["season"], "number": following["number"]}
    return None


@route("POST", r"/api/play/(\w+)")
def api_play(h, m, q):
    """Le plan de lecture d'un fichier vidéo pour CE lecteur. Le client l'appelle à l'ouverture, puis à chaque changement de piste audio."""
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    if entry["kind"] != "video":
        raise HttpError(400, "ce fichier n'est pas une vidéo")
    body = h.body_json()
    lib, path = hub.path_of(entry)
    probe = probe_of(hub, entry)
    caps, audio, kbps, height = play_settings(body)
    decision = prober.plan(probe, caps, audio, kbps, height)
    chosen = decision["audio"]
    video = probe.get("video") or {}
    height = default_height(decision, video, height)
    h.send_json({
        "entry": entry["id"], "item": entry["item_id"], "title": entry["title"], "show": show_title(hub, entry), "season": entry["season"], "number": entry["number"],
        "mode": decision["mode"], "why": decision["why"], "url": play_url(entry, decision, caps, kbps, height), "seekable": decision["mode"] == "direct",
        "duration": probe["duration"] or entry["duration"], "resume": resume_position(hub, h.user["id"], entry), "warning": play_warning(decision, video),
        "audio": [{"index": t["index"], "label": track_label(t, h.lang), "lang": t["lang"], "selected": t["index"] == chosen, "commentary": t["commentary"]} for t in probe["audio"]],
        "subs": subtitle_list(hub, entry, probe, path, h.lang),
        "video": {"codec": video.get("codec"), "width": video.get("width"), "height": video.get("height"), "hdr": video.get("hdr")} if video else None,
        "next": next_summary(next_entry(hub, entry)),
    })


@route("GET", r"/api/play/(\w+)/start")
def api_play_start(h, m, q):
    """D'où part vraiment un flux `remux` redemandé à `t` secondes : l'image est recopiée, elle ne peut commencer que sur une image clé. Le client compte sa position (horloge, sous-titres, progression) depuis ce `start`, et demande le flux avec `&start=<start>`."""
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    if entry["kind"] != "video":
        raise HttpError(400, "ce fichier n'est pas une vidéo")
    at = max(0.0, query_number(q, "t", float, 0.0))
    lib, path = hub.path_of(entry)
    h.send_json({"start": prober.keyframe_at(hub.cfg.ffprobe, path, at)})


@route("GET", r"/media/(\w+)/file")
def media_file(h, m, q):
    entry = entry_of(h, m.group(1), "download" if q.get("download") else "play")         # `?download=1` demande le droit de télécharger ; sans lui, c'est la lecture directe (Range) : le droit de lire
    lib, path = h.hub.path_of(entry)
    extra = {"Cache-Control": "private, max-age=3600"}
    if q.get("download"):
        extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(path.name)
    h.send_file(path, headers=extra)


def query_number(q, name, cast=int, default=0):
    try:
        return cast(q.get(name, default))
    except ValueError:
        raise HttpError(400, f"paramètre {name} invalide")


def stream_command(hub, path, probe, q):
    """La commande ffmpeg qui répond à la requête : copie de l'image et du son (remux) ou ré-encodage (transcode), départ, plafonds et nombre de voies demandés."""
    mode = "transcode" if q.get("m") == "transcode" else "remux"
    audio = query_number(q, "a") if "a" in q else None
    decision = {"mode": mode, "audio": audio if probe["audio"] else None, "copy_video": mode == "remux", "copy_audio": mode == "remux" and query_number(q, "ca", int, 1) == 1}
    return prober.ffmpeg_command(hub.cfg.ffmpeg, path, probe, decision, start=max(0.0, query_number(q, "start", float, 0.0)), max_kbps=query_number(q, "k"),
                                 max_height=query_number(q, "h"), channels=max(1, min(6, query_number(q, "c", int, 2))))


def first_chunk(proc, errors):
    """Les premiers octets que ffmpeg produit ; 500, avec sa dernière ligne d'erreur, s'il n'en produit pas."""
    first = proc.stdout.read1(CHUNK)
    if not first:
        proc.wait(timeout=5)
        time.sleep(0.05)
        raise HttpError(500, "ffmpeg n'a rien produit : " + ((errors[0].strip().splitlines() or ["erreur inconnue"])[-1] if errors else "erreur inconnue"))
    return first


def send_stream(h, proc, first):
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


def stop_stream(proc):
    """Arrête ffmpeg s'il tourne encore et ferme ses tubes."""
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


@route("GET", r"/media/(\w+)/stream")
def media_stream(h, m, q):
    """Un MP4 fragmenté produit à la volée. Pas de Content-Length, pas de Range : pour avancer, le client redemande avec ?start=."""
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    lib, path = hub.path_of(entry)
    if not hub.cfg.ffmpeg:
        raise HttpError(501, "ffmpeg est introuvable sur le serveur : seule la lecture directe est possible.")
    cmd = stream_command(hub, path, probe_of(hub, entry), q)
    if not hub.streams.acquire(blocking=False):
        raise HttpError(503, f"{hub.cfg.max_streams} conversions en cours : attends la fin de l'une d'elles.")
    hub.active += 1
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        errors = []
        threading.Thread(target=lambda: errors.append(proc.stderr.read().decode("utf-8", "replace")), daemon=True).start()
        send_stream(h, proc, first_chunk(proc, errors))
    finally:
        if proc is not None:
            stop_stream(proc)
        hub.active -= 1
        hub.streams.release()


@route("GET", r"/api/subs/(\w+)/([es])(\d+)\.(vtt|sup)")
def api_subs(h, m, q):
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    lib, path = hub.path_of(entry)
    kind, index, extension = m.group(2), int(m.group(3)), m.group(4)
    cache = hub.cfg.data / "subs"
    cache.mkdir(parents=True, exist_ok=True)
    if extension == "sup":
        send_sup(h, hub, entry, path, kind, index, cache)
        return
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


def send_sup(h, hub, entry, path, kind, index, cache):
    """Une piste PGS du fichier, en .sup, pour que le navigateur la dessine : le fichier voisin et les pistes qui ne sont pas des PGS n'en ont pas (404)."""
    tracks = probe_of(hub, entry)["subs"]
    if kind != "e" or not any(t["index"] == index and subtitles.is_pgs(t.get("codec")) for t in tracks):
        raise HttpError(404, "sous-titres introuvables")
    stamp = f"{entry['id']}-{entry['mtime']}-e{index}"
    target = cache / f"{stamp}.sup"
    if not target.is_file():
        try:
            data = subtitles.ffmpeg_sup(hub.cfg.ffmpeg, path, index)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise HttpError(500, str(e))
        write_atomic(target, data)
    tag = f'"{stamp}"'
    if h.headers.get("If-None-Match") == tag:
        h.send_body(b"", "application/octet-stream", 304, {"ETag": tag, "Cache-Control": "private, no-cache"})
        return
    h.send_body(target.read_bytes(), "application/octet-stream", 200, {"ETag": tag, "Cache-Control": "private, no-cache"}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
POSTER_TYPES = ("movie", "show")


def poster_item(h, ident):
    """L'item dont on change l'affiche (administrateur) : un film, une série ou une animation."""
    require_admin(h)
    item = h.hub.item(ident)
    if item["type"] not in POSTER_TYPES:
        h.discard_body()
        raise HttpError(400, "seuls un film, une série ou une animation ont une affiche à changer")
    return item


@route("POST", r"/api/item/(\w+)/poster")
def api_item_poster(h, m, q):
    """Remplace l'affiche d'un film ou d'une série par l'image envoyée (le corps de la requête : JPEG, PNG ou WebP, 10 Mo au plus). Un WebP est converti en JPEG ; 415 s'il ne se lit pas, 501 si rien ne sait le convertir. L'image reste dans HUB_DATA : rien n'est écrit dans la médiathèque. Il n'y a pas de retour à l'affiche d'avant : la nouvelle devient l'affiche."""
    item = poster_item(h, m.group(1))
    data = h.body_bytes(PHOTO_UPLOAD_MAX)
    ext = enrich.image_ext(data)
    if not ext:
        raise HttpError(415, "ce n'est pas une image JPEG, PNG ou WebP")
    if ext == ".webp":
        try:
            data, ext = to_jpeg(data, h.hub.cfg.ffmpeg), ".jpg"
        except NoConverter as e:
            raise HttpError(501, str(e))
        except ConvertError as e:
            raise HttpError(415, str(e))
    h.hub.images.set_poster(item["id"], data, ext)
    h.send_json({"ok": True})


# ---------------------------------------------------------------------------
# Fiche TMDB d'une série
# ---------------------------------------------------------------------------
TMDB_SHOW_RE = re.compile(r"^(?:https?://(?:www\.)?themoviedb\.org/tv/)?([0-9]+)(?:[-/?#].*)?$", re.IGNORECASE)         # un nombre, ou l'adresse de la fiche : « …/tv/4615-hercules-the-legendary-journeys »
TMDB_RESULTS = 10


def tmdb_show(h, ident):
    """(série, bibliothèque, client TMDB, dossier des affiches) pour choisir sa fiche TMDB (administrateur) ; 400 pour autre chose qu'une série ou une animation, ou sans clé TMDB."""
    require_admin(h)
    item = h.hub.item(ident)
    if item["type"] != "show":
        h.discard_body()
        raise HttpError(400, "seules une série ou une animation ont une fiche TMDB à choisir")
    lib = library_or_404(h.hub, item["lib"])
    try:
        client, posters = enrich.tmdb_client(h.hub.cfg)
    except RuntimeError as e:
        h.discard_body()
        raise HttpError(400, str(e))
    return item, lib, client, posters


def tmdb_failure(e):
    """Un échec de TMDB, rendu en réponse HTTP : 502, avec ce que TMDB a dit."""
    return HttpError(502, f"TMDB : {e}")


@route("GET", r"/api/item/(\w+)/tmdb")
def api_item_tmdb_search(h, m, q):
    """Les séries que TMDB propose pour `q` (10 au plus, dans l'ordre de TMDB) : `{"results": [{"tmdb", "name", "year", "overview"}]}`. Administrateur seulement."""
    _, _, client, _ = tmdb_show(h, m.group(1))
    query = q.get("q", "").strip()
    if not query:
        raise HttpError(400, "q est vide")
    try:
        found = client.search_tv(query)
    except (tmdb.TmdbError, tmdb.TmdbAuthError) as e:
        raise tmdb_failure(e)
    h.send_json({"results": [{"tmdb": r["id"], "name": r.get("name") or "", "year": (r.get("first_air_date") or "")[:4], "overview": r.get("overview") or ""} for r in found[:TMDB_RESULTS]]})


@route("POST", r"/api/item/(\w+)/tmdb")
def api_item_tmdb(h, m, q):
    """Fixe la fiche TMDB d'une série ou d'une animation (`{"tmdb": 4615}`, ou l'adresse de sa fiche) et l'enrichit à nouveau. Le choix reste dans la base du hub : la médiathèque n'est pas touchée. Administrateur seulement."""
    item, lib, client, posters = tmdb_show(h, m.group(1))
    found = TMDB_SHOW_RE.match(str(h.body_json().get("tmdb", "")).strip())
    if not found:
        raise HttpError(400, "tmdb doit être l'identifiant d'une série TMDB, ou l'adresse de sa fiche")
    try:
        enrich.choose_show(client, posters, h.hub.db, lib, item, int(found.group(1)))
    except (tmdb.TmdbError, tmdb.TmdbAuthError) as e:
        raise tmdb_failure(e)
    except OSError as e:
        raise HttpError(500, f"affiche impossible à écrire : {e}")
    h.send_json({"ok": True})


@route("GET", r"/img/(item|entry)/(\w+)")
def image(h, m, q):
    owner = h.hub.db.one("SELECT lib FROM " + ("items" if m.group(1) == "item" else "entries") + " WHERE id=?", (m.group(2),))
    if owner is not None:
        h.require(owner["lib"])
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
    entry = entry_of(h, m.group(1), "play")
    if entry["kind"] != "book":
        raise HttpError(400, "ce fichier n'est pas un livre")
    lib, path = hub.path_of(entry)
    meta = safe_json(entry["meta"], {})
    kind = meta.get("format", "")
    progress = hub.db.progress_for(h.user["id"], [entry["id"]]).get(entry["id"])
    out = {"id": entry["id"], "item": entry["item_id"], "title": entry["title"], "format": kind, "position": progress["position"] if progress and not progress["done"] else 0,
           "next": None, "file": urls.entry_file(entry["id"])}
    try:
        if kind == "cbz":
            out["pages"] = len(bookmedia.cbz_pages(path))
            out["page_url"] = f"/book/{entry['id']}/page/{{n}}"          # `{n}` : le numéro de page, à partir de 0
            out["rtl"] = bookmedia.reads_right_to_left(meta.get("info"), safe_json(hub.item(entry["item_id"])["meta"], {}).get("section"))
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
    entry = entry_of(h, m.group(1), "play")
    lib, path = hub.path_of(entry)
    try:
        data = bookmedia.cbz_page(path, int(m.group(2)))
    except BookError as e:
        raise HttpError(404, str(e))
    h.send_body(data, sniff(data), 200, {"Cache-Control": "private, max-age=86400"}, h.command == "HEAD")


def epub_mime(data, mime):
    """Le type d'un fichier de l'epub : le XML bien formé se sert en XHTML, le reste en HTML ; l'encodage est dit pour tout ce qui est du texte."""
    if mime in ("application/xhtml+xml", "text/html", "image/svg+xml") or data[:5] == b"<?xml":
        mime = "application/xhtml+xml" if mime != "image/svg+xml" and bookmedia.valid_xml(data) else ("text/html" if mime != "image/svg+xml" else mime)
    if mime.startswith("text/") or mime.endswith("xml") or mime.endswith("css"):
        mime += "; charset=utf-8"
    return mime


@route("GET", r"/book/(\w+)/epub/(.+)")
def book_epub(h, m, q):
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    lib, path = hub.path_of(entry)
    try:
        data, mime = bookmedia.epub_member(path, m.group(2))
    except BookError as e:
        raise HttpError(404, str(e))
    mime = epub_mime(data, mime)
    # Le contenu d'un livre est celui d'un tiers : pas de script, pas de requête vers l'extérieur.
    csp = "default-src 'self' data: blob:; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"
    h.send_body(data, mime, 200, {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": csp}, h.command == "HEAD")


# ---------------------------------------------------------------------------
# Sagas composées à la main (sagas.json, voir sagas.py) : réservées à l'administrateur
# ---------------------------------------------------------------------------
saga_call = partial(module_call, sagafile.SagaError)


def movie_rows(hub, libs=None):
    """Les films de toutes les bibliothèques de films, ou de celles de `libs` (une liste vide n'en a aucune)."""
    rows = []
    for lib in hub.cfg.libraries if libs is None else libs:
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


def listed_in(body, key):
    """La liste `key` du corps de la requête ; vide si elle n'y est pas ou n'en est pas une."""
    return body.get(key) if isinstance(body.get(key), list) else []


def listed_film_ids(values):
    """Les identifiants TMDB (nombre ou « movie/27205 ») de `values` ; 400 au premier illisible."""
    ids = []
    for value in values:
        ident = sagafile.film_id(value)
        if ident is None:
            raise HttpError(400, f"identifiant TMDB illisible ({value!r})")
        ids.append(ident)
    return ids


def item_film_ids(db, values):
    """Les identifiants TMDB des films de la médiathèque désignés par leur élément ; 404 pour un élément qui n'est pas un film, 400 pour un film sans identifiant."""
    ids = []
    for value in values:
        row = db.one("SELECT * FROM items WHERE id=?", (str(value),))
        if row is None or row["type"] != "movie":
            raise HttpError(404, "film inconnu")
        found = sagafile.by_tmdb([row])
        if not found:
            raise HttpError(400, f"« {row['title']} » n'a pas d'identifiant TMDB : étiquette-le avec Metadata.py")
        ids += list(found)
    return ids


@route("POST", r"/api/sagas/add")
def api_sagas_add(h, m, q):
    """Ajoute des films à la fin d'une saga, créée au besoin : `films` (identifiants TMDB), puis `items` (éléments de la médiathèque : c'est ce que la fiche d'un film envoie)."""
    require_admin(h)
    body = body_dict(h)
    ids = listed_film_ids(listed_in(body, "films")) + item_film_ids(h.hub.db, listed_in(body, "items"))
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
    """L'affiche d'un film qui manque à une saga ou à une personne (HUB_DATA/tmdb, voir missing.py) ; 404 s'il n'en a pas (la page montre alors une case grisée). Le fichier fait foi : l'enrichissement des sagas et celui des personnes le posent chacun, sans rien se dire."""
    hub = h.hub
    path = hub.cfg.data / "tmdb" / missing.poster_name(m.group(1))
    path = path if path.is_file() else None
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


@route("POST", r"/api/images/refresh")
def api_images_refresh(h, m, q):
    """Fait relire à une bibliothèque ses images (`{"lib": "<id>"}`) : le cache des vignettes de ses éléments et de ses fichiers est vidé, chaque image se refait depuis le disque à la prochaine demande. Les affiches choisies à la main et celles de TMDB sont gardées. Administrateur seulement ; rend `{"ok": true, "removed": <fichiers retirés du cache>}`."""
    require_admin(h)
    lib = library_or_404(h.hub, str(h.body_json().get("lib", "")))
    h.send_json({"ok": True, "removed": h.hub.images.forget_library(lib.id)})


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
# Boîte de réception : les envois des comptes (voir inbox.py)
# ---------------------------------------------------------------------------
def require_sender(h):
    """403 sans compte (un serveur ouvert n'a pas d'envoi) ou pour un compte à qui l'administrateur n'a pas donné le droit d'envoyer ; l'administrateur l'a toujours."""
    if not h.hub.auth():
        raise HttpError(403, "pas d'envoi sans compte : crée un compte sur le serveur")
    if not h.user["can_upload"]:
        raise HttpError(403, "tu n'as pas le droit d'envoyer des fichiers")


LENGTH_RE = re.compile(r"[0-9]{1,15}")


def announced_length(h):
    """La taille du fichier envoyé, d'après `Content-Length` : obligatoire (411 sans lui, ou avec un corps en morceaux, que ce serveur ne sait pas lire), jamais vide (400).
    Un seul en-tête, fait de chiffres et de rien d'autre (400 sinon) : `int()` accepterait `1_0`, `+10` ou des chiffres non latins, et deux valeurs que le serveur et un proxy liraient différemment laisseraient la fin du corps passer pour une requête."""
    values = h.headers.get_all("Content-Length") or []
    if not values or h.headers.get("Transfer-Encoding") is not None:
        raise HttpError(411, "Content-Length obligatoire")
    if len(values) != 1 or not LENGTH_RE.fullmatch(values[0].strip()):
        raise HttpError(400, "Content-Length illisible")
    length = int(values[0])
    if length == 0:
        raise HttpError(400, "fichier vide")
    return length


class CountedBody:
    """Le corps de la requête tel qu'une route le lit : compte les octets lus, pour qu'un refus en cours de route sache ce qui reste à venir (voir Handler.let_the_reply_arrive)."""

    def __init__(self, source):
        self.source, self.count = source, 0

    def read(self, size):
        data = self.source.read(size)
        self.count += len(data)
        return data


@route("POST", r"/api/inbox")
def api_inbox_send(h, m, q):
    """Un fichier envoyé dans la boîte de réception : `?name=<nom du fichier>`, le fichier en corps brut, `Content-Length` obligatoire. Il va dans `HUB_DATA/inbox/<compte>/`, sous un nom nettoyé, sans écraser (voir inbox.py).
    Les refus (droit, nom, extension, espace, nombre d'envois) viennent avant la lecture du corps ; la connexion est alors coupée, puisque le corps n'est pas lu, mais après avoir laissé la réponse arriver au client qui envoie encore (voir Handler.let_the_reply_arrive). Rend 201 `{"name", "size", "account"}`."""
    h.body_taken = True                     # le corps se lit par morceaux, ou pas du tout : _fail ne doit pas le jeter
    body = CountedBody(h.rfile)
    try:
        require_sender(h)
        stored = module_call(InboxError, h.hub.inbox.receive, h.user["id"], q.get("name"), body, announced_length(h))
    except Exception:                       # un refus (HttpError), mais aussi une erreur système avant la lecture : dans tous les cas le corps n'est pas lu, ou à moitié
        h.close_connection = True
        h.body_consumed = body.count
        raise
    h.send_json({**stored, "account": h.user["id"]}, 201)


def inbox_files(hub, files):
    """Les fichiers de la boîte avec le nom du compte qui les a envoyés (`user`) ; l'identifiant d'un compte supprimé en tient lieu."""
    names = {row["id"]: row["name"] for row in accounts.rows(hub.db)}
    return [{**f, "user": names.get(f["account"], f["account"])} for f in files]


@route("GET", r"/api/inbox")
def api_inbox_list(h, m, q):
    """L'administrateur voit tous les envois ; `?mine=1` (un compte qui a le droit d'envoyer, l'administrateur compris) ne montre que les siens, avec l'espace qu'ils occupent : `used` (octets)."""
    box = h.hub.inbox
    if q.get("mine") != "1":
        require_admin(h)
        h.send_json({"files": inbox_files(h.hub, box.listing())})
        return
    require_sender(h)
    h.send_json({"files": inbox_files(h.hub, box.listing(h.user["id"])), "used": box.used(h.user["id"])})


@route("GET", r"/api/inbox/(\w+)/([^/]+)")
def api_inbox_file(h, m, q):
    """Un fichier de la boîte, en pièce jointe (`Range` accepté pour reprendre) : administrateur seulement. Jamais analysé ni servi autrement : type `application/octet-stream`, `nosniff`."""
    require_admin(h)
    path = module_call(InboxError, h.hub.inbox.path_of, m.group(1), m.group(2))
    h.send_file(path, "application/octet-stream", {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.name), "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@route("DELETE", r"/api/inbox/(\w+)/([^/]+)")
def api_inbox_delete(h, m, q):
    """L'administrateur supprime n'importe quel fichier de la boîte ; un compte qui a le droit d'envoyer, seulement les siens (403 pour ceux des autres, qu'ils existent ou non)."""
    h.discard_body()
    account, name = m.groups()
    if h.user["role"] != "admin":
        require_sender(h)
        if account != h.user["id"]:
            raise HttpError(403, "ce fichier n'est pas à toi")
    module_call(InboxError, h.hub.inbox.remove, account, name)
    h.send_json({"ok": True})


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
