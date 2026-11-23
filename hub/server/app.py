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
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from contextlib import contextmanager, nullcontext
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
from .. import artistedit
from .. import backup
from .. import changelog
from .. import countries
from .. import enrich
from .. import labels
from .. import missing
from .. import people
from .. import playlists
from .. import prefs
from .. import recommend
from .. import requests as hub_requests
from .. import request_search
from .. import artist_search
from .. import suggestions
from .. import urls
from ..activity import Activity, attempt_id
from .. import sagas as sagafile
from ..sagas import Sagas
from ..db import RATINGS, Database, make_id
from ..inbox import EXTENSIONS, Inbox, InboxError
from ..origin import origin_of
from ..media import badges
from ..media import books as bookmedia
from ..media import probe as prober
from ..media import subs as subtitles
from ..media import tracks
from ..media.images import POSTER_KEY, ConvertError, Images, NoConverter, crop_box, sniff, to_jpeg, write_atomic
from ..scan import edits
from ..scan.common import STACK_MAX
from .jobs import Jobs

API_VERSION = 1             # le contrat décrit dans docs/api.md ; n'augmente qu'en cas de rupture (champ retiré, renommé ou de sens différent)
CHUNK = 256 * 1024
DOWNLOADS_ASKED = 50        # les tentatives de téléchargement que /api/downloads accepte en une question
INBOX_DRAIN_SECONDS = 5    # un envoi refusé (POST /api/inbox) laisse au client ce temps, au plus, pour finir d'envoyer son corps avant que la connexion soit coupée
INBOX_DRAIN_BYTES = 64 << 20    # ... et ce nombre d'octets, au plus : un client qui n'arrête jamais d'envoyer est coupé à la première borne atteinte
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
    """`text` lu en JSON, ou `default` : texte vide, illisible, ou imbriqué au point de dépasser la pile (`[[[[…` lève RecursionError, pas ValueError)."""
    try:
        return json.loads(text) if text else default
    except (ValueError, RecursionError):
        return default


def meta_json(text):
    """Le `meta` d'un élément ou d'un fichier lu en objet : {} quand il est vide, illisible, ou un JSON valide qui n'est pas un objet (`null`, liste, texte, nombre)."""
    meta = safe_json(text, {})
    return meta if isinstance(meta, dict) else {}


class Hub:
    """L'état de l'application : réglages, base, images, tâches."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.db = Database(cfg.data / "hub.db")
        self.images = Images(cfg, self.db)
        self.jobs = Jobs(cfg, self.db)
        self.sagas = Sagas(cfg.data / "sagas.json")
        self.throttle = accounts.Throttle()                    # les essais de connexion ratés, par adresse
        self.inbox = Inbox(cfg, self.db)                       # les envois des comptes (voir inbox.py)
        self.finder = request_search.Finder(cfg)                # la recherche dans TMDB des titres à demander (voir request_search.py)
        self.artist_finder = artist_search.Finder(cfg)          # la recherche dans MusicBrainz des artistes à demander (voir artist_search.py)
        self.streams = threading.BoundedSemaphore(cfg.max_streams)
        self.active = 0                                        # les flux ffmpeg en cours (voir count_stream)
        self.active_lock = threading.Lock()
        self.activity = Activity()                             # qui lit, télécharge ou envoie en ce moment, et les dernières décisions de lecture (voir activity.py)
        self.secret = self._secret()
        self.started = time.time()
        accounts.seed(self.db, cfg)

    def count_stream(self, delta):
        """Un flux ffmpeg de plus (+1) ou de moins (-1) : un fil par connexion, le compteur est sous verrou."""
        with self.active_lock:
            self.active += delta

    def _secret(self):
        value = self.db.kv_get("secret")
        if not value:
            value = secrets.token_hex(32)
            self.db.kv_set("secret", value)
        return value

    def session_token(self, row):
        """Le jeton de session d'un compte (cookie `hub=` ou `Authorization: Bearer`), émis maintenant : `<identifiant>.<émis>.<mac>`, voir accounts.py."""
        return accounts.session_token(self.secret, row)

    def auth(self):
        """Vrai dès qu'il existe un compte. Sans compte (ni HUB_PASSWORD au premier démarrage), le serveur est ouvert à tous."""
        return accounts.exist(self.db)

    def open_user(self):
        """Qui est « connecté » quand le serveur est ouvert : le profil `default`, administrateur (c'est lui que le premier compte reprendra)."""
        return {"id": accounts.FIRST_ID, "name": self.cfg.admin, "role": "admin", "can_upload": False, "can_request": True, "remote_download": True}        # sans compte, personne n'envoie : un envoi anonyme serait une porte ouverte sur le disque

    def media_token(self, row):
        """Le jeton de média du compte `row`, celui qui voyage dans les URLs (`?token=`) : il ne vaut que sur /media, /img, /book et /api/subs (sous-titres, lecture seule), nulle part ailleurs sur /api/ ;
        une URL de pochette qui fuit ne donne donc ni le nettoyage ni le scan. Il désigne le compte : ses droits (voir, télécharger) s'appliquent à la requête."""
        return accounts.media_token(self.secret, row)

    def vlc_token(self, row, entry_id):
        """Le jeton VLC du compte `row` pour ce seul fichier, 24 h : voir accounts.vlc_token. Il ne vaut que sur `/media/<entrée>/file` (voir Handler.vlc_user)."""
        return accounts.vlc_token(self.secret, row, entry_id)

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
        """Le message sans les chemins du serveur (dossiers des bibliothèques, HUB_DATA, HUB_INBOX, media_library) : ffprobe, ffmpeg et les OSError les écrivent tels quels, et la réponse part à n'importe quel compte connecté."""
        text = str(text)
        roots = [Path(lib.path) for lib in self.cfg.libraries] + [Path(self.cfg.data), Path(self.cfg.inbox)] + ([Path(self.cfg.repo)] if self.cfg.repo else [])
        forms = {form for root in roots for form in (str(root), root.as_posix(), str(root.resolve()), root.resolve().as_posix()) if len(form) > 3}
        forms |= {form.replace("\\", "\\\\") for form in forms if "\\" in form}      # une OSError écrit son chemin entre guillemets, antislashs doublés (Windows)
        for form in sorted(forms, key=len, reverse=True):          # le plus long d'abord : un dossier peut en contenir un autre
            text = text.replace(form, "…")
        return request_search.without_key(text, self.cfg.tmdb_key)          # ceinture : une exception du client TMDB peut porter l'URL, donc la clé

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
                if not self.jobs.blocking():
                    self.jobs.scan_all()
        threading.Thread(target=loop, name="autoscan", daemon=True).start()


# ---------------------------------------------------------------------------
# Sérialisation
# ---------------------------------------------------------------------------
MIN_VOTES = 25          # en dessous, une moyenne TMDB tient à quelques avis : elle ne se montre pas


def shown_rating(rating):
    """La note TMDB gardée (voir enrich.rating_of) si elle mérite d'être montrée : une moyenne (un nombre fini, non nul), sur au moins MIN_VOTES votes (un entier) ; None sinon."""
    if not isinstance(rating, dict):
        return None
    vote, votes = rating.get("vote"), rating.get("votes")
    if isinstance(vote, bool) or not isinstance(vote, (int, float)) or not math.isfinite(vote) or not vote:
        return None                     # une note qui n'est pas un nombre fini (texte, liste, booléen, NaN) n'est pas montrée : elle ne se trie ni ne s'écrit en JSON strict
    return rating if isinstance(votes, int) and not isinstance(votes, bool) and votes >= MIN_VOTES else None


def typed_summary(row, meta):
    """Ce que seul un genre d'élément porte : la durée et l'artiste d'un album, les épisodes manquants et la pile d'affiches d'une série."""
    out = {}
    if row["type"] == "album":
        if meta.get("duration"):
            out["duration"] = meta["duration"]
        if meta.get("artist_id"):
            out["artist_id"] = meta["artist_id"]
        if credits := album_credits(meta):
            out["credits"] = [{"name": c["name"], "id": c["id"]} for c in credits]
    if row["type"] == "series":
        if meta.get("missing"):
            out["missing"] = sum(len(m["numbers"]) for m in meta["missing"])
        if meta.get("stack"):
            out["cover_urls"] = [urls.item_cover(row["id"])] + [urls.entry_image(e) for e in meta["stack"]][:STACK_MAX - 1]
    return out


def album_credits(meta):
    """[{"name", "id", "mbid"}] des artistes crédités d'une collaboration (voir scan.music.album_credits) ; vide pour l'album d'un seul artiste, ou dont les pistes n'ont pas ALBUMARTISTS."""
    credits = meta.get("credits") if isinstance(meta, dict) else None
    return credits if isinstance(credits, list) and len(credits) > 1 else []


def credits_artist(meta, artist_id):
    """Vrai si l'album est de cet artiste : il en est l'artiste principal, ou l'un des crédités de la collaboration."""
    return meta.get("artist_id") == artist_id or any(c.get("id") == artist_id for c in album_credits(meta))


def summary(row, seen=None, quality=None, opinion=None):
    """La vignette d'un élément. `quality` : ce que rend card_quality ; un film qui y est porte `quality` et `hdr`. `opinion` : l'avis du compte sur cet élément (`up`, `mid` ou `down`, le champ `rating` de la vignette), absent de la vignette quand il n'a pas d'avis."""
    meta = meta_json(row["meta"])
    out = {"id": row["id"], "type": row["type"], "title": row["title"], "year": row["year"], "grp": row["grp"], "n": row["count"], "letter": row["letter"],
           "added": row["added"], "genres": safe_json(row["genres"], []), "cover": urls.item_cover(row["id"]), **typed_summary(row, meta)}
    rating = shown_rating(meta.get("tmdb_rating"))
    if rating:
        out["tmdb_rating"], out["tmdb_votes"] = rating["vote"], rating["votes"]
    if seen is not None:
        out["seen"] = seen
    out.update((quality or {}).get(row["id"], {}))
    if opinion and row["type"] in RATED_TYPES:          # une ligne perdue (base copiée, type changé) ne fait paraître d'avis ni sur un album ni sur un livre
        out["rating"] = opinion
    return out


CARD_QUALITY_SQL = ("SELECT e.item_id, e.season, e.disc, e.number, e.rel, json_extract(e.probe, '$.video.width') AS width, json_extract(e.probe, '$.video.height') AS height, "
                    "json_extract(e.probe, '$.video.hdr') AS hdr FROM entries e JOIN items i ON i.id = e.item_id "
                    "WHERE i.type = 'movie' AND e.kind = 'video' AND e.probe IS NOT NULL AND json_valid(e.probe)")


def card_quality(db):
    """{id d'un film: {quality, hdr}} : la qualité d'image de son premier fichier sondé qui a une image (l'ordre de la fiche), pour sa vignette. Les films sans fichier sondé n'y sont pas.
    Lu en une requête et gardé en mémoire jusqu'au prochain scan ou à la prochaine sonde (voir Database.derived)."""
    def build():
        rows = sorted((r for r in db.query(CARD_QUALITY_SQL) if r["width"] is not None), key=order_key)
        first = {}
        for row in rows:
            first.setdefault(row["item_id"], row)
        return {ident: {"quality": badges.quality(row["width"], row["height"]), "hdr": bool(row["hdr"])} for ident, row in first.items()}
    return db.derived("card-quality", build, probes=True)


def entry_json(row, progress=None):
    meta = meta_json(row["meta"])
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
    return with_progress(out, progress)


def with_progress(out, progress):
    """`out` (un fichier tel que l'API le montre), avec `progress` : où en est le compte, s'il l'a commencé ; sans `progress`, le compte ne l'a pas commencé (c'est ce que l'interface floute pour « Masquer les épisodes non vus »).
    Le même champ partout où un épisode se montre : la fiche, « Reprendre », le plan de lecture et son `next`."""
    if progress is not None:
        out["progress"] = {"position": progress["position"], "duration": progress["duration"], "done": bool(progress["done"]), "updated": progress["updated"]}
    return out


def order_key(row):
    return (row["season"] if row["season"] is not None else -1, row["disc"] or 0, row["number"] if row["number"] is not None else 1e9, row["rel"].casefold(), row["rel"])


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
    session: tuple = None                      # ("cookie" | "bearer", jeton de session) quand c'est lui qui a reconnu le compte (voir session_user)
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

    def transfer(self, kind, ref, title, library="", leave_empty=True):
        """Le transfert de cette requête dans l'activité du hub (voir activity.py) : qui le fait, d'où et avec quoi ; `ref` reconnaît ce qui est transféré (`leave_empty` : voir Activity.transfer)."""
        return self.hub.activity.transfer(self.user["id"], self.user["name"], ref, kind, client_ip(self), title, library, self.headers.get("User-Agent", ""), leave_empty, origin=origin(self))

    def send_file(self, path, mime=None, headers=None, track=None, attempt=None):
        """Le fichier, avec les requêtes Range (reprise, avance rapide, lecteur vidéo). `track` : de quoi inscrire l'envoi dans l'activité (une fonction sans argument qui rend `Handler.transfer(…)`), ouvert autour de l'envoi réel seulement :
        ni un HEAD, ni un 404, ni un 416 n'y laissent de trace. `attempt` de même, pour une tentative de téléchargement : une fonction de (début, taille du fichier) qui rend `Activity.attempt(…)`."""
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
                with (track or nullcontext)() as sent, (attempt or no_attempt)(start, size) as reached:
                    self.copy_range(handle, start, end - start + 1, counters(sent, reached))

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

    def copy_range(self, handle, start, length, sent=None):
        """Envoie `length` octets du fichier à partir de `start`, par morceaux ; `sent(n)` compte ce qui est parti."""
        self.connection.settimeout(3600)       # un film mis en pause ne doit pas couper la connexion
        try:
            handle.seek(start)
            remaining = length
            while remaining:               # `length` est au moins 1 et chaque morceau en retire au plus `remaining` : jamais négatif
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                if sent:
                    sent(len(chunk))
        finally:
            self.connection.settimeout(self.timeout)       # la connexion persiste : un client inactif ne garde pas un fil une heure de plus

    @property
    def lang(self):
        """'fr' ou 'en' : la langue choisie dans l'interface (cookie `hub_lang`) ; le français quand rien n'est choisi."""
        return page_language(self, {})[0]

    # --- Aiguillage -------------------------------------------------------
    def session_user(self):
        """Le compte du jeton de session, en cookie puis en `Authorization: Bearer` ; None s'il n'y en a pas ou s'il ne vaut rien.
        D'où vient le jeton reconnu, et lequel, est gardé dans `self.session` (`("cookie" | "bearer", jeton)`) : `/api/state` s'en sert pour le renouveler."""
        cookie = self.headers.get("Cookie", "")
        found = re.search(r"(?:^|;\s*)hub=(\w{1,64}(?:\.[0-9]{1,12})?\.[0-9a-f]{64})\s*(?:;|$)", cookie, re.ASCII)       # valeur entière, sans rien derrière : `hub=<id>.<émis>.<mac>.junk` ne passe pas
        bearer = re.fullmatch(r"Bearer\s+(\S+)", self.headers.get("Authorization", "").strip(), re.IGNORECASE)
        for source, token in (("cookie", found.group(1) if found else None), ("bearer", bearer.group(1) if bearer else None)):
            row = accounts.user_for_token(self.hub.db, self.hub.secret, token) if token else None
            if row is not None:
                self.session = (source, token)
                return accounts.public(row)
        return None

    def authorized(self, path, query):
        """Trois façons de se présenter : le jeton de session (`<identifiant>.<mac>`) en cookie (navigateur) ou en `Authorization: Bearer` (clients natifs),
        le jeton de média en `?token=` (balises <img> / <video>, Cast), celui-ci seulement sur les fichiers et les sous-titres (/api/subs/), nulle part ailleurs sur /api/.

        Le compte reconnu est posé sur `self.user` (`{"id", "name", "role"}`) : c'est lui, et lui seul, qui désigne le profil de la progression et des playlists, et dont les droits s'appliquent.
        Le jeton de média est celui d'un compte : il le désigne aussi, mais ne sert que sur les routes de fichiers."""
        self.user = self.granted = self.session = None
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
            self.user = accounts.public(row) if row is not None else self.vlc_user(path, query["token"])
        return self.user is not None

    def vlc_user(self, path, token):
        """Le compte d'un jeton VLC (`?token=`), seulement sur `/media/<son entrée>/file`, en GET ou HEAD et sans `download` ; None partout ailleurs. Ses droits restent ceux du compte : `entry_of` les vérifie à chaque requête."""
        found = re.fullmatch(r"/media/(\w+)/file", path)
        asked = {k for k, _ in parse_qsl(urlsplit(self.path).query, keep_blank_values=True)}         # même une clé `download` vide, ou encodée (`%64ownload`), est refusée
        if found is None or self.command not in ("GET", "HEAD") or "download" in asked:
            return None
        row = accounts.user_for_vlc_token(self.hub.db, self.hub.secret, token, found.group(1))
        return accounts.public(row) if row is not None else None

    def granted_to_account(self, lib_id, perm):
        """Le droit `perm` du compte sur cette bibliothèque, tel que l'administrateur l'a réglé (la table `rights`), sans regarder d'où vient la requête."""
        if self.user is None:
            return False
        if self.user["role"] != "admin" and self.granted is None:
            self.granted = accounts.grants(self.hub.db, self.user["id"])
        return accounts.allows(self.user["role"], self.granted or {}, lib_id, perm)

    def remote_download_refused(self):
        """Le compte n'a pas « Télécharger à distance » et la requête n'est pas locale (voir origin.py) : un administrateur télécharge de partout."""
        return not self.user["remote_download"] and origin(self) != "local"

    def can(self, lib_id, perm="view"):
        """Le compte de la requête peut-il `view` (voir), `play` (lire en ligne) ou `download` (télécharger) cette bibliothèque ? Un administrateur le peut toujours ; sans rien dans `rights`, tout est permis.
        `download` demande en plus « Télécharger à distance » quand la connexion n'est pas locale ; la lecture n'en dépend jamais."""
        return self.granted_to_account(lib_id, perm) and not (perm == "download" and self.remote_download_refused())

    def require(self, lib_id, perm="view"):
        """403 quand le compte n'a pas ce droit sur la bibliothèque (et, pour `download`, à distance sans « Télécharger à distance » : le message le dit)."""
        if not self.can(lib_id, perm):
            if perm == "download" and self.granted_to_account(lib_id, perm):
                raise HttpError(403, "téléchargement non autorisé à distance")
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
            print(request_search.without_key(traceback.format_exc(), self.hub.cfg.tmdb_key), file=sys.stderr, end="")       # la trace sans la clé TMDB : un message d'exception peut porter l'URL
            self._fail(500, f"erreur interne : {e}")

    do_GET = do_HEAD = do_POST = do_DELETE = dispatch

    def _fail(self, status, message):
        if status == 500 or status > 501:           # avant l'envoi : un client qui lit le journal dès la réponse reçue y trouve la ligne, et un envoi qui échoue ne la perd pas
            self.log_failure(status, message)
        sent = True
        try:
            self.send_json({"error": self.hub.scrub(message)}, status)
        except OSError:
            self.close_connection, sent = True, False
        if self.command in ("POST", "DELETE") and not self.body_taken:      # refus avant toute lecture (401, 404, route qui lève trop tôt) : le corps ne doit pas coller à la requête suivante
            self.drop_body()
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
        hidden = {"token", "q"} if unquote(split.path) in ("/api/requests/search", "/api/requests/artists") else {"token"}         # le texte d'une recherche dans TMDB ou dans MusicBrainz ne va pas au journal
        pairs = [(k, "***" if k in hidden else v) for k, v in parse_qsl(split.query, keep_blank_values=True)]
        shown = split.path + ("?" + urlencode(pairs, safe="*") if pairs else "")
        print(f"{status} {self.command} {shown} : {request_search.without_key(message, self.hub.cfg.tmdb_key)}", file=sys.stderr)

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
    """La dernière valeur de l'en-tête `name` (`X-Forwarded-For`, `X-Forwarded-Proto`) quand la connexion vient de la boucle locale ; None si elle n'en vient pas ou si l'en-tête est ABSENT.
    Un en-tête présent mais vide (`""`, `"8.8.8.8,"`) rend `""`, pas None : l'appelant ne doit pas le prendre pour une absence (`origin` en fait `internet`).
    La dernière, jamais la première : le proxy ajoute la sienne à la fin de ce que le client a pu écrire (plusieurs lignes d'en-tête comprises)."""
    if not from_loopback(h):
        return None
    lines = h.headers.get_all(name, [])
    return ",".join(lines).rpartition(",")[2].strip() if lines else None


def origin(h):
    """`local`, `tailscale` ou `internet` pour la connexion de cette requête (voir origin.py) ; le dernier `X-Forwarded-For` n'est lu que d'une connexion de la boucle locale."""
    return origin_of(h.client_address[0], forwarded(h, "X-Forwarded-For"))


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


def session_cookie(hub, row, secure, token=None):
    """Le cookie de session du compte `row` : `token` s'il est donné (le même que celui que la réponse rend), sinon un jeton émis maintenant."""
    return f"hub={token or hub.session_token(row)}; Path=/; Max-Age={accounts.SESSION_DAYS * 86400}{cookie_flags(secure)}"


def refreshed_session(h):
    """Le jeton de session de la requête a passé la moitié de sa vie, ou a l'ancienne forme sans date : (en-têtes, champs) qui en donnent un neuf, sinon (None, {}).
    Le navigateur le reçoit en cookie, un client natif (`Authorization: Bearer`) dans le champ `token` ; jamais un cookie `HttpOnly` dans une réponse que le JavaScript lit."""
    if h.session is None or not accounts.session_due(h.session[1]):
        return None, {}
    row = accounts.by_id(h.hub.db, h.user["id"])
    if row is None:                                   # compte supprimé entre-temps : rien à renouveler
        return None, {}
    if h.session[0] == "cookie":
        return {"Set-Cookie": session_cookie(h.hub, row, secure_request(h))}, {}
    return None, {"token": h.hub.session_token(row)}


def ended_session_cookie(secure):
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


def strict_body_dict(h):
    """Le corps JSON de la requête, qui doit être un objet : un corps vide, illisible (JSON ou UTF-8 invalide) ou qui n'en est pas un (liste, nombre, texte) est refusé en 400, au lieu de valoir `{}` comme avec `body_json`.
    Pour les routes où « aucun champ » a un sens (ici, rétablir le nom d'origine) et ne doit pas se confondre avec une requête mal formée."""
    h.body_taken = True
    length = h.content_length()
    if length > 1 << 20:
        h.close_connection = True
        raise HttpError(413, "corps trop gros")
    try:
        data = json.loads(h.rfile.read(length).decode("utf-8")) if length else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise HttpError(400, "le corps doit être un objet JSON")
    return data


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
    return {"ok": True, "token": token}, {"Set-Cookie": session_cookie(h.hub, row, secure_request(h), token)}


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


ACCOUNT_FLAGS = {"upload": accounts.set_can_upload, "request": accounts.set_can_request, "remote_download": accounts.set_remote_download}       # clé de la route et du corps -> ce qui change le droit


@route("POST", r"/api/user/(\w+)/(upload|request|remote_download)")
def api_user_flag(h, m, q):
    """L'administrateur donne ou retire un droit global d'un compte : `/upload` `{"upload": bool}` (envoyer dans la boîte de réception), `/request` `{"request": bool}` (demander des films et des séries)
    ou `/remote_download` `{"remote_download": bool}` (télécharger quand la connexion n'est pas locale : appareil Tailscale ou Funnel).
    Pas pour un administrateur (400 : il a toujours ces droits). Rend le compte. Valable à la requête suivante ; ce qui a déjà été envoyé ou demandé reste."""
    require_admin(h)
    need_accounts(h)
    account_call(ACCOUNT_FLAGS[m.group(2)], h.hub.db, m.group(1), body_dict(h).get(m.group(2)))
    h.send_json(user_listing(h.hub, accounts.by_id(h.hub.db, m.group(1))))


@route("POST", r"/api/user/(\w+)/rename")
def api_user_rename(h, m, q):
    """L'administrateur renomme un compte : `{"name": "..."}`. Seul le nom change (sessions, progression, playlists, droits, dossier de la boîte de réception restent). 409 si le nom est pris. Rend le compte."""
    require_admin(h)
    need_accounts(h)
    row = account_call(accounts.rename, h.hub.db, m.group(1), body_dict(h).get("name"))
    h.send_json(user_listing(h.hub, row))


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


@route("POST", r"/api/me/name")
def api_me_name(h, m, q):
    """Chacun change son nom, en donnant son mot de passe actuel : `{"current": "...", "name": "..."}`. La session reste valable (pas de nouveau jeton). 409 si le nom est pris. Rend le compte."""
    need_accounts(h)
    body = body_dict(h)
    row = check_current(h, body.get("current"))
    h.send_json(accounts.public(account_call(accounts.rename, h.hub.db, row["id"], body.get("name"))))


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


def with_seen(row, seen, quality=None, ratings=None):
    """La vignette d'un élément avec ce que le compte en a vu (`seen`) et l'avis qu'il en a (`ratings` : {id: avis}, voir Database.ratings_for)."""
    done, touched = watched(row, seen)
    out = summary(row, quality=quality, opinion=(ratings or {}).get(row["id"]))
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
        libs.append({**labels.describe(hub.db, lib), "count": count, "scanned": hub.db.kv_get(f"scanned:{lib.id}"), "exists": lib.path.is_dir(), "play": h.can(lib.id, "play"), "download": h.can(lib.id, "download")})
    headers, renewed = refreshed_session(h)
    h.send_json({"version": __version__, "libraries": libs, "ffmpeg": bool(hub.cfg.ffmpeg), "ffprobe": bool(hub.cfg.ffprobe), "tmdb": bool(hub.cfg.tmdb_key),
                 "auth": hub.auth(), "user": h.user, "settings": prefs.load(hub.db, h.user["id"]), "api": API_VERSION, "busy": hub.jobs.busy(), "streams": hub.active, "max_streams": hub.cfg.max_streams,
                 "can_request": h.user["can_request"], "request_search": bool(h.user["can_request"] and hub.finder.configured()), **requests_state(h), **suggestions_state(h), **renewed}, 200, headers)


@route("GET", r"/api/changelog")
def api_changelog(h, m, q):
    """Les nouveautés de chaque version (`hub/CHANGELOG.md`), pour Réglages › À propos ; sans fichier, aucune."""
    h.send_json({"versions": changelog.load()})


def next_entry(hub, entry):
    """Le fichier qui suit celui-ci dans son élément (épisode suivant, tome suivant, partie suivante), ou None."""
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (entry["item_id"],)), key=order_key)
    for index, row in enumerate(rows):
        if row["id"] == entry["id"]:
            return rows[index + 1] if index + 1 < len(rows) else None
    return None


def dismiss_key(profile, item_id):
    return f"dismiss:{profile}:{item_id}"


def progress_card(entry, item, progress, resume, updated, ratings=None):
    """Une carte « Reprendre » : le fichier, avec `progress` comme sur la fiche (absent : pas encore commencé), et `position` / `total` pour la barre."""
    card = entry_json(entry, progress)
    position, total = (progress["position"], progress["duration"]) if progress else (0, 0)
    card.update({"item": summary(item, opinion=(ratings or {}).get(item["id"])), "position": position, "total": total or entry["duration"], "resume": resume, "updated": updated})
    return card


def finished_card(hub, profile, row, item, ratings=None):
    """Un fichier fini ne se reprend pas : on propose l'épisode ou le tome qui le suit, s'il y en a un et qu'il n'est pas fini lui aussi. Un film fini n'a rien à proposer."""
    if item["type"] == "movie":
        return None
    following = next_entry(hub, row)
    if following is None:
        return None
    progress = hub.db.progress_for(profile, [following["id"]]).get(following["id"])
    if progress is not None and progress["done"]:
        return None
    return progress_card(following, item, progress, False, row["updated"], ratings)


def resume_card(hub, profile, row, item, ratings=None):
    """La carte « Reprendre » d'un fichier de la progression, ou None quand il n'y a rien à proposer (fini sans suite, à peine commencé)."""
    if row["done"]:
        return finished_card(hub, profile, row, item, ratings)
    if row["position"] < (1 if row["kind"] == "book" else RESUME_MIN):
        return None
    progress = {"position": row["position"], "duration": row["pduration"], "done": row["done"], "updated": row["updated"]}
    return progress_card(row, item, progress, True, row["updated"], ratings)


def continue_row(hub, profile, libs=None, ratings=None):
    """Ce qu'on a commencé : le film ou l'épisode à reprendre, l'épisode qui suit celui qu'on vient de finir, le tome suivant. `libs` : les bibliothèques où le compte peut lire (None : toutes) ; on ne propose pas de reprendre ce qu'il ne peut pas lire. `ratings` : ses avis, pour la vignette de chaque carte."""
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
        card = resume_card(hub, profile, row, item, ratings) if item is not None else None
        if card is None:
            continue
        out.append(card)
        if len(out) >= 24:
            break
    return out


today = missing.today                       # le jour (« 2026-11-04 ») : un nom à part pour que les tests puissent le fixer
UNSEEN = 2 * prefs.SHELF_CARDS              # ce que le serveur offre par bibliothèque : le navigateur en retire ce que « Nouveautés » montre (SHELF_CARDS au plus) et doit encore pouvoir remplir un rayon de SHELF_CARDS ; avec moins, les tris « date d'ajout » et « note » (les mieux notés sont souvent les plus récents) videraient le rayon


def unseen_row(rows, seen, profile, day, n=UNSEEN, sort="random", quality=None, ratings=None):
    """Les films ou les séries d'une bibliothèque que ce compte n'a pas encore touchés (ni vus, ni commencés : aucune progression sur aucun de leurs fichiers ; c'est l'état « à voir » de watch_state), `n` au plus.

    L'ordre est choisi par le réglage `home_unseen_sort` du compte, et appliqué avant la coupe à `n` : `random` (défaut) ne dépend que du compte et du jour (le même toute la journée, un autre demain) ; `rating` : la note TMDB montrée, de la meilleure à la moins bonne (comme le tri « Mieux notés » de la bibliothèque : ce qui a moins de MIN_VOTES votes ou aucune note vient à la fin) ; `added` : le plus récemment ajouté d'abord.

    Un élément que le compte a noté « Je n'aime pas » (`down`, voir `ratings`) n'est pas proposé : c'est son « pas intéressé ». Il reste dans la bibliothèque et dans son filtre « À voir »."""
    ratings = ratings or {}
    pool = [r for r in rows if r["id"] not in seen and ratings.get(r["id"]) != "down"]
    if sort == "rating":
        pool.sort(key=rating_key)
    elif sort == "added":
        pool.sort(key=lambda r: (-r["added"], r["sort"], r["id"]))
    else:
        pool.sort(key=lambda r: hashlib.sha1(f"{day}:{profile}:{r['id']}".encode()).hexdigest())
    return [summary(r, quality=quality, opinion=ratings.get(r["id"])) for r in pool[:n]]


UNSEEN_KINDS = ("movies", "shows")          # les genres de bibliothèque qui ont un rayon « Pas encore vu » : une nouvelle bibliothèque vidéo d'un de ces genres en a un sans autre réglage


@route("GET", r"/api/home")
def api_home(h, m, q):
    hub = h.hub
    seen, quality, ratings = seen_counts(hub, h.user["id"]), card_quality(hub.db), hub.db.ratings_for(h.user["id"])
    day, rows = today(), []
    settings = prefs.load(hub.db, h.user["id"])
    order = settings["home_unseen_sort"]
    for lib in h.visible_libs():
        items = shown_rows(hub, lib, hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,)))
        if lib.kind == "music":
            recent = recent_music(items, seen, prefs.SHELF_CARDS)
        else:
            recent = [with_seen(r, seen, quality, ratings) for r in hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY added DESC, sort, id LIMIT ?", (lib.id, prefs.SHELF_CARDS))]
        row = {"id": lib.id, "label": labels.shown(hub.db, lib), "kind": lib.kind, "recent": recent, "count": len(items)}
        if lib.kind in UNSEEN_KINDS:
            row["unseen"] = unseen_row(items, seen, h.user["id"], day, sort=order, quality=quality, ratings=ratings)
        rows.append(row)
    h.send_json({"continue": continue_row(hub, h.user["id"], h.visible_ids("play"), ratings), "libraries": rows, "requests_ready": requests_ready(h, seen, quality, ratings) if settings["home_requests"] == "show" else []})        # rayon caché par le compte : rien à calculer, le champ reste


@route("GET", r"/api/recommendations")
def api_recommendations(h, m, q):
    """Les rayons « Parce que tu as aimé <titre> » du compte de la requête (voir recommend.py) : ses avis et sa progression seulement, dans les bibliothèques vidéo qu'il peut lire. Calculés quel que soit le réglage `home_recommend` : c'est l'interface qui n'appelle pas la route quand il est à `hide`."""
    hub, uid = h.hub, h.user["id"]
    readable = {lib.id for lib in hub.cfg.libraries if lib.kind in UNSEEN_KINDS and h.can(lib.id, "play")}
    opinions, progress = hub.db.dated_ratings_for(uid), recommend.progress_of(hub.db, uid)
    found = recommend.shelves(recommend.index(hub.db, lambda meta: (shown_rating(meta.get("tmdb_rating")) or {}).get("vote")), opinions, progress, readable, uid, today())
    seen, quality, ratings = {i: p[:2] for i, p in progress.items()}, card_quality(hub.db), {i: o[0] for i, o in opinions.items()}
    h.send_json({"shelves": [{"anchor": {k: anchor.row[k] for k in ("id", "title", "year", "type")},
                              "items": [{**with_seen(cand.row, seen, quality, ratings), "why": why} for cand, why in cards]} for anchor, cards in found]})


@route("GET", r"/api/search")
def api_search(h, m, q):
    needle = fold(q.get("q", "")).strip()
    if len(needle) < 2:
        h.send_json({"results": [], "people": []})
        return
    hub = h.hub
    seen, quality, ratings = seen_counts(hub, h.user["id"]), card_quality(hub.db), hub.db.ratings_for(h.user["id"])
    results = []
    for lib in h.visible_libs():
        found = []
        for row in shown_rows(hub, lib, hub.db.query("SELECT * FROM items WHERE lib=? ORDER BY sort", (lib.id,))):
            if needle in fold(row["title"]) or needle in fold(row["grp"]) or any(needle in fold(c["name"]) for c in album_credits(meta_json(row["meta"]))):
                found.append(with_seen(row, seen, quality, ratings))
                if len(found) >= 40:
                    break
        if found:
            results.append({"id": lib.id, "label": labels.shown(hub.db, lib), "kind": lib.kind, "items": found})
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
    seen, ratings = (seen_counts(hub, h.user["id"]), hub.db.ratings_for(h.user["id"])) if rows else ({}, {})
    extras = person_extras(hub, person, libs)
    mark_requests(h, extras.get("missing"))
    h.send_json({"name": shown or " ".join(name.split()), "roles": roles, "results": person_results(hub.db, libs, rows, seen, ratings), **extras})


def person_rows(db, person, visible):
    """({rôle: nombre d'éléments}, {id: ligne}) des éléments que cette personne (None : personne) a réalisés ou joués dans les bibliothèques `visible`."""
    roles, rows = {role: 0 for role in people.ROLES}, {}
    for role in people.ROLES:
        for row in items_by_id(db, person[role] if person else ()):
            if row["lib"] in visible:
                roles[role] += 1
                rows[row["id"]] = row
    return roles, rows


def person_results(db, libs, rows, seen, ratings=None):
    """Les éléments `rows` groupés par bibliothèque (dans l'ordre de `libs`, sans les bibliothèques vides), du plus récent au plus ancien."""
    quality = card_quality(db)
    results = []
    for lib in libs:
        mine = sorted((r for r in rows.values() if r["lib"] == lib.id), key=lambda r: (r["sort"], r["id"]))
        mine.sort(key=lambda r: r["year"] or "", reverse=True)          # sans année en dernier ; à égalité, par titre (le tri est stable)
        if mine:
            results.append({"id": lib.id, "label": labels.shown(db, lib), "kind": lib.kind, "items": [with_seen(r, seen, quality, ratings) for r in mine]})
    return results


def person_extras(hub, person, libs):
    """`enrichable` (le bouton « films manquants » a un sens : un identifiant TMDB unique et une clé) et, quand il y en a, `missing` : les films que TMDB attribue à cette personne (voir enrich.enrich_person) et qu'aucune bibliothèque de films de `libs` n'a, comparés par identifiant TMDB du film à chaque requête."""
    ident = people.tmdb_id(person, [lib.id for lib in libs])
    known = hub.db.kv_get(enrich.person_key(ident)) if ident else None
    films = known.get("films") if isinstance(known, dict) else None
    absent = missing.of_person(films, missing.owned_films(hub.db, libs)) if films else []
    return {"enrichable": bool(ident and hub.cfg.tmdb_key), **({"missing": absent} if absent else {}), **person_sheet(hub, ident, known)}


def person_sheet(hub, ident, known):
    """`{"info": {biography, birthday, deathday, place_of_birth, department, photo, own_photo, tmdb}}` : la fiche que la passe des personnes a gardée (`known`, voir enrich.fetch_person), corrigée champ par champ par l'administrateur (enrich.edited_sheet), dates en ISO ; `photo` vrai quand elle en a une (envoyée, sinon celle de TMDB), `own_photo` quand c'est celle envoyée ; `tmdb` l'identifiant de la personne (la photo est `/img/person/<tmdb>`). Rien sans fiche : pas d'identifiant unique, jamais demandée, ou gardée avant la fiche. Rien n'est demandé en ligne."""
    info = known.get("info") if isinstance(known, dict) else None
    if not isinstance(info, dict):
        return {}
    shown = enrich.edited_sheet(info, enrich.person_edits(hub.db, ident))
    return {"info": {**shown, "photo": enrich.person_photo(hub.cfg, ident) is not None, "own_photo": enrich.own_photo(hub.cfg, ident) is not None, "tmdb": ident}}


PERSON_EDIT_LOCK = threading.Lock()          # lire-modifier-écrire de `personedit:` : deux administrateurs en même temps ne s'écrasent pas


def person_target(hub, ident):
    """(identifiant, fiche gardée par la passe) de la personne `ident` (les chiffres de l'adresse) ; 404 quand la passe ne lui a pas fait de fiche : on corrige une fiche, on n'en crée pas."""
    ident = int(ident)
    known = hub.db.kv_get(enrich.person_key(ident))
    if not (isinstance(known, dict) and isinstance(known.get("info"), dict)):
        raise HttpError(404, "pas de fiche pour cette personne : lance d'abord « Chercher ses films manquants »")
    return ident, known


@route("POST", r"/api/person/([0-9]{1,9})/sheet")
def api_person_sheet_edit(h, m, q):
    """Corrige la fiche d'une personne : corps `{champ: texte}` parmi biography, department, birthday, deathday (AAAA-MM-JJ), place_of_birth. Un champ envoyé vide, ou égal à ce que dit TMDB, revient à TMDB ; un champ absent ne change pas. Les corrections sont gardées à part (`personedit:<id>`) : une nouvelle passe ne les efface pas. Rend `{"info"}` comme /api/person. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    ident, known = person_target(hub, m.group(1))
    body = h.body_json()
    with PERSON_EDIT_LOCK:
        try:
            edits = enrich.new_edits(known["info"], enrich.person_edits(hub.db, ident), body)
        except ValueError as e:
            raise HttpError(400, str(e))
        if edits:
            hub.db.kv_set(enrich.person_edit_key(ident), edits)
        else:
            hub.db.kv_delete(enrich.person_edit_key(ident))
    h.send_json(person_sheet(hub, ident, known))


@route("POST", r"/api/person/([0-9]{1,9})/photo")
def api_person_photo_set(h, m, q):
    """Donne à une personne la photo envoyée (le corps : JPEG, PNG ou WebP, 10 Mo au plus), à la place de celle de TMDB ; `crop=x,y,largeur,hauteur` (fractions de l'image) ne garde que ce cadre. Gardée sous HUB_DATA/people : la passe n'y touche pas. Administrateur seulement."""
    require_admin(h)
    ident, _ = person_target(h.hub, m.group(1))
    box = photo_box(q)
    data = h.body_bytes(PHOTO_UPLOAD_MAX)
    if not enrich.image_ext(data):
        raise HttpError(415, "ce n'est pas une image JPEG, PNG ou WebP")
    save_person_photo(h.hub, ident, data, box)
    h.send_json({"ok": True})


@route("POST", r"/api/person/([0-9]{1,9})/photo/crop")
def api_person_photo_crop(h, m, q):
    """Ne garde de la photo montrée (envoyée, sinon celle de TMDB) que le cadre `{"x", "y", "w", "h"}` (fractions de l'image) ; le résultat devient la photo envoyée. Administrateur seulement."""
    require_admin(h)
    ident, _ = person_target(h.hub, m.group(1))
    box = body_box(h.body_json())
    path = enrich.person_photo(h.hub.cfg, ident)
    if path is None:
        raise HttpError(404, "pas de photo à recadrer")
    save_person_photo(h.hub, ident, read_photo(path), box)
    h.send_json({"ok": True})


@route("POST", r"/api/person/([0-9]{1,9})/photo/remove")
def api_person_photo_remove(h, m, q):
    """Retire la photo envoyée : celle de TMDB revient (s'il y en a une). 404 quand il n'y en a pas. Administrateur seulement."""
    require_admin(h)
    ident, _ = person_target(h.hub, m.group(1))
    h.body_json()          # rien à y lire, mais le corps est lu : sur une connexion persistante, il collerait à la requête suivante
    try:
        removed = enrich.remove_person_photo(h.hub.cfg, ident)
    except OSError as e:
        raise HttpError(500, f"photo non retirée : {e}")
    if not removed:
        raise HttpError(404, "pas de photo envoyée")
    h.send_json({"ok": True})


def save_person_photo(hub, ident, data, box=None):
    """enrich.save_person_photo, ses échecs rendus en réponses HTTP (voir photo_errors)."""
    with photo_errors():
        enrich.save_person_photo(hub.cfg, ident, data, box)


@route("POST", r"/api/enrich/people")
def api_enrich_people(h, m, q):
    """Demande à TMDB les films des réalisateurs et acteurs (`{"lib": "<id>", "force": false}`) : les réalisateurs et les acteurs de premier plan, pour que `/api/person` dise ce qui manque à chacun. Sans `lib` : les bibliothèques de films ; avec : cette bibliothèque (films ou séries), une tâche par catégorie. Administrateur seulement ; tâche de fond (202, `job`) : une heure ou plus la première fois, une personne connue depuis moins d'un mois n'est pas redemandée (sauf `force`)."""
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
            raise HttpError(400, "les films manquants des personnes ne concernent que les bibliothèques de films et de séries")
        libs, label = [lib], labels.shown(hub.db, lib)
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
    "title": (lambda r: (r["sort"], r["id"]), False),          # chaque tri finit par l'identifiant : deux éléments de même titre gardent le même rang, quel que soit l'ordre de la base
    "added": (lambda r: (r["added"], r["sort"], r["id"]), True),
    "year": (lambda r: (r["year"] or "0000", r["sort"], r["id"]), True),
    "year-asc": (lambda r: (r["year"] or "9999", r["sort"], r["id"]), False),
    "part": (lambda r: release_key(r), False),      # l'ordre d'une saga
    "rating": (lambda r: rating_key(r), False),     # « Mieux notés » : la clé range elle-même du mieux au moins bien noté
}
RATED_KINDS = ("movies", "shows")                   # les bibliothèques dont les éléments portent un avis personnel, et où le tri « Mon avis » (`my-rating`) existe
RATED_TYPES = ("movie", "show")                     # les éléments qui se notent : un film ou une série entière, jamais un épisode, une saga, un album ni un livre
MY_RATING_RANK = {"up": 0, "mid": 1, "down": 3}     # « Mon avis » : J'aime, Bof, sans avis (2), Je n'aime pas
# Le tri « Titre » de la musique et des livres range par artiste / collection ; chez un artiste, les albums vont du plus ancien au plus récent, puis par nom (ceux sans année en dernier)
TITLE_SORTS = {
    "music": lambda r: (fold(r["grp"]) or "￿", r["year"] or "9999", r["sort"], r["id"]),
    "books": lambda r: (fold(r["grp"]), r["sort"], r["id"]),
}


def row_rating(row):
    """(note, votes) d'une ligne : sa note TMDB montrée (voir shown_rating), ou pour une vignette de saga celle de son meilleur film (à égalité, le plus voté) : une saga vaut ce qu'elle a de mieux, Terminator vaut Terminator 2 ; (None, 0) sans note montrée."""
    if isinstance(row, dict) and "members" in row:
        return max(map(row_rating, row["members"]), key=lambda found: (found[0] is not None, found[0], found[1]), default=(None, 0))
    rating = shown_rating(meta_json(row["meta"]).get("tmdb_rating"))
    return (rating["vote"], rating["votes"]) if rating else (None, 0)


def rating_key(row):
    """Le tri « Mieux notés », sans inversion : d'abord ce qui a une note montrée, de la meilleure à la moins bonne, puis le plus de votes ; ensuite le reste ; à égalité, par titre, puis par identifiant : deux films de même titre, de même note et de même nombre de votes gardent le même rang, quel que soit l'ordre dans lequel la base les rend."""
    vote, votes = row_rating(row)
    return (vote is None, () if vote is None else (-vote, -votes), row["sort"], row["id"])          # sans note montrée, rien à comparer : le titre et l'identifiant seuls rangent ce qui reste


def my_rating_key(ratings):
    """Le tri « Mon avis » : `up`, `mid`, sans avis, puis `down` ; à égalité, par titre, puis par identifiant (un ordre total : le même quel que soit l'ordre où la base rend les lignes). Une vignette de saga n'a pas d'avis."""
    return lambda row: (MY_RATING_RANK.get(ratings.get(row["id"]), 2), row["sort"], row["id"])


def release_key(row):
    """L'ordre d'une saga : par date de sortie. Le numéro posé sur le disque peut être celui d'un dossier de rangement (le MCU : Ant-Man 12, 20, 31), il ne départage que deux films sortis le même jour."""
    meta = meta_json(row["meta"])
    part = meta.get("part")
    return (meta.get("date") or row["year"] or "9999", part is None, part or 0, row["sort"], row["id"])


def number_films(films, ranks=None, start=1):
    """Le numéro affiché sur l'affiche d'un film de saga : son rang dans la saga du fichier (`ranks`, {id du film: rang}), sinon sa place dans la saga, de 1 à N."""
    for n, film in enumerate(films, start):
        film["part"] = ranks[film["id"]] if ranks else n
    return films


def section_of(row):
    """La section d'une série de livres (le dossier de premier niveau : Manga, Comics, Artbook…) ; « Autres » quand rien n'est indiqué."""
    return meta_json(row["meta"]).get("section") or "Autres"


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
        first, *_ = members
        years = sorted(m["year"] for m in members if m["year"])
        genres = []
        for m in members:
            genres += [g for g in safe_json(m["genres"], []) if g not in genres]
        out.append({"id": make_id(first["lib"], "saga:" + name), "lib": first["lib"], "type": "saga", "title": name, "sort": fold(name), "letter": initial(name),
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


UNKNOWN_ARTISTS = hub_requests.UNKNOWN_ARTISTS        # rangés en dernier : ce ne sont pas des artistes qu'on cherche par leur nom (et qu'on ne demande pas)


def artist_name(grp):
    """Le nom que montre la carte d'un artiste : « Various Artists » se dit en français, un album sans artiste est « Artiste inconnu »."""
    return "Artistes divers" if fold(grp) == "various artists" else grp or "Artiste inconnu"


def shown_rows(hub, lib, rows):
    """Les lignes d'une bibliothèque telles qu'on les montre : en musique, chaque album porte le nom affiché corrigé de son artiste (voir artistedit.renamed), si bien que listes, tri, recherche et vue « Artistes » le voient ; la base garde les étiquettes."""
    names = artistedit.names(hub.db, lib.id) if lib.kind == "music" else {}
    return [artistedit.renamed(row, names) for row in rows] if names else rows


def artist_rows(rows):
    """Les albums d'une bibliothèque de musique regroupés par artiste : une ligne par artiste, de la forme d'une ligne d'item, si bien que les filtres, le tri et la pagination de la bibliothèque s'y appliquent
    tels quels. `title` est le nom de l'artiste, `count` son nombre d'albums, `meta.album` le plus ancien (sa pochette, quand il n'a pas de photo) ; `albums` les titres de tous, pour que la recherche d'un
    album trouve son artiste. Les artistes sans nom connu viennent en dernier."""
    groups = {}
    for row in rows:
        groups.setdefault(meta_json(row["meta"]).get("artist_id") or fold(row["grp"]), []).append(row)
    out = []
    for key, albums in groups.items():
        albums.sort(key=lambda r: (r["year"] or "9999", r["sort"], r["id"]))
        name = artist_name(artist_tagged_name(albums, key))         # pas « A & B » quand sa plus ancienne sortie est une collaboration
        out.append({"id": key, "lib": albums[0]["lib"], "type": "artist", "title": name, "grp": name, "letter": initial(name), "year": "", "summary": "",
                    "sort": ("~" if fold(name) in UNKNOWN_ARTISTS else "") + fold(name), "genres": json.dumps(sorted({g for a in albums for g in safe_json(a["genres"], [])}), ensure_ascii=False),
                    "meta": json.dumps({"album": albums[0]["id"]}), "added": max(a["added"] for a in albums), "count": len(albums), "albums": [a["title"] for a in albums],
                    "kinds": Counter(release_kind(a) for a in albums)})
    return out


def artist_card(row):
    """La carte d'un artiste dans la liste : son identifiant (celui de sa fiche, `/api/artist/<lib>/<id>`), son nom, son nombre d'albums et l'album dont on prendra la pochette faute de photo."""
    return {"id": row["id"], "lib": row["lib"], "type": "artist", "title": row["title"], "n": row["count"], "letter": row["letter"], "added": row["added"],
            "album": meta_json(row["meta"]).get("album", "")}


ARTIST_ARRIVAL = 7 * 24 * 3600       # les albums d'un artiste arrivés à moins d'une semaine les uns des autres sont l'arrivée de l'artiste


def recent_music(rows, seen, limit):
    """Les derniers ajouts d'une bibliothèque de musique, du plus récent au plus ancien. Un album ajouté à un artiste qu'on avait déjà reste un album ; l'arrivée d'un artiste (au moins deux albums,
    tous arrivés à une semaine d'écart au plus) n'est qu'une carte, la sienne. Les compilations et les artistes inconnus restent des albums : les regrouper cacherait ce qui est arrivé."""
    groups = {}
    for row in rows:
        groups.setdefault(meta_json(row["meta"]).get("artist_id") or fold(row["grp"]), []).append(row)
    entries = []
    for albums in groups.values():
        added = [a["added"] for a in albums]
        arrived = len(albums) > 1 and max(added) - min(added) <= ARTIST_ARRIVAL and fold(albums[0]["grp"]) not in UNKNOWN_ARTISTS
        entries += artist_rows(albums) if arrived else albums
    entries.sort(key=lambda r: (-r["added"], r["sort"], r["id"]))
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
    """Les conditions que la requête pose à une vignette (saga, section, genre, recherche, à voir / en cours / terminés) ; elles s'ajoutent. Un `filter` qui n'est ni vide, ni `todo` / `progress` / `done`, ni l'ancien `unseen`, est une erreur (400) : il ne filtre plus « rien » en silence."""
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
    state = asked_state(q, kind)
    if state:
        tests.append(lambda row: watch_state(row, seen) == state)
    return tests


def asked_state(q, kind):
    """L'état que demande `filter` (`todo`, `progress` ou `done`, l'ancien `unseen` valant `todo`), ou "" s'il n'y en a pas. 400 pour un filtre inconnu, et pour la musique : on n'« a pas vu » un album, plutôt qu'une liste qui a l'air filtrée, un refus."""
    asked = q.get("filter", "")
    state = {"unseen": "todo"}.get(asked, asked)
    if state and state not in WATCH_STATES:
        raise HttpError(400, f"filtre inconnu : {asked}")
    if state and kind == "music":
        raise HttpError(400, "ce filtre n'existe pas pour la musique")
    return state


def library_order(kind, q, ranks, artists=False, ratings=None):
    """(tri, clé, sens) du tri demandé ; la musique n'a qu'un ordre : par artiste (dans la liste des artistes, par leur nom, les inconnus à la fin). `my-rating` (« Mon avis », d'après `ratings`) n'existe que pour les films et les séries : ailleurs c'est un tri inconnu."""
    sort = "title" if kind == "music" else q.get("sort", "title")
    key, reverse = SORTS.get(sort, SORTS["title"])
    if sort == "title" and kind in TITLE_SORTS and not artists:
        key = TITLE_SORTS[kind]
    if sort == "part" and ranks is not None:
        key = lambda r: ranks[r["id"]]
    if kind in RATED_KINDS:
        key = {"my-rating": my_rating_key(ratings or {})}.get(sort, key)
    return sort, key, reverse


def page_bounds(q):
    """(départ, taille) de la page demandée ; une valeur qui n'est pas un nombre ramène aux valeurs par défaut."""
    try:
        return max(0, int(q.get("offset", 0))), min(600, max(1, int(q.get("limit", 240))))
    except ValueError:
        return 0, 240


def library_item(row, seen, quality=None, ratings=None):
    if row["type"] == "artist":
        return artist_card(row)
    return saga_json(row, seen) if row["type"] == "saga" else with_seen(row, seen, quality, ratings)


def artist_key(row):
    """Ce qui regroupe les albums d'un même artiste (voir artist_rows)."""
    return meta_json(row["meta"]).get("artist_id") or fold(row["grp"])


RELEASE_KINDS = {"ep": "eps", "single": "singles"}        # le compteur d'un type principal de sortie ; tout autre type (album, compilation, live, bande originale, aucun) compte comme album


def release_kind(row):
    """Le compteur où va une sortie : `eps`, `singles` ou `albums`, d'après son type principal (le premier de `release_types`, voir scan/music.py)."""
    types = meta_json(row["meta"]).get("release_types") or [""]
    return RELEASE_KINDS.get(str(types[0]).lower(), "albums")


def music_counts(chosen, artists):
    """{artists, albums, eps, singles} de ce que la musique montre : en mode artistes, une carte par artiste portant le décompte de ses sorties par type ; en mode albums, une carte par sortie,
    et les artistes sont ceux de ces sorties. Les albums manquants (`wanted:`) ne sont pas des items : ils ne comptent pas."""
    kinds = Counter()
    for row in chosen:
        kinds.update(row["kinds"] if artists else [release_kind(row)])
    count = len(chosen) if artists else len({artist_key(row) for row in chosen})
    return {"artists": count, "albums": kinds["albums"], "eps": kinds["eps"], "singles": kinds["singles"]}


def genre_menu(genres, asked=""):
    """Les 60 genres les plus fréquents (à égalité, par nom), rangés par ordre alphabétique sans accent ni casse : c'est l'ordre du menu de l'interface.
    Le genre `asked` (celui du filtre, venu de l'adresse ou d'un clic sur une fiche) y est toujours, avec son nombre, même rare ; un genre que la bibliothèque n'a pas n'ajoute rien."""
    top = dict(sorted(genres.items(), key=lambda kv: (-kv[1], kv[0]))[:60])
    if asked in genres:
        top[asked] = genres[asked]
    return sorted(top.items(), key=lambda kv: (fold(kv[0]), kv[0]))


def library_extras(out, kind, sections, absent, sagas, chosen, artists=False):
    """Les champs que la réponse n'a que dans certains cas : les sections des livres, les films qui manquent à une saga, le décompte des sagas d'une grille de sagas (un film de deux sagas ne compte qu'une fois),
    le nombre d'artistes, d'albums, d'EP et de singles de la musique (de tout ce que filtrent les critères, pas seulement de la page)."""
    if kind == "books":
        out["sections"] = sections_json(sections)
    if kind == "music":
        out.update(music_counts(chosen, artists))
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
    rows = shown_rows(hub, lib, hub.db.query("SELECT * FROM items WHERE lib=?", (lib.id,)))
    seen, ratings = seen_counts(hub, h.user["id"], lib.id), hub.db.ratings_for(h.user["id"])
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
    sort, key, reverse = library_order(lib.kind, q, ranks, artists, ratings)
    chosen.sort(key=key, reverse=reverse)
    offset, limit = page_bounds(q)
    page = chosen[offset:offset + limit]
    out = {"library": {"id": lib.id, "label": labels.shown(hub.db, lib), "kind": lib.kind}, "total": len(chosen), "offset": offset,
           "items": [library_item(r, seen, card_quality(hub.db), ratings) for r in page],          # la table ne connaît que des films : une autre bibliothèque n'y trouve rien
           "genres": genre_menu(genres, q.get("genre", ""))}
    if group and sort == "part":
        number_films(out["items"], ranks, offset + 1)
    mark_requests(h, absent)
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


def tagged_films(hub, group, movies, info):
    """(films dans l'ordre de la saga, rangs, manquants) de la saga de l'étiquette `group` : l'ordre de la collection TMDB quand on la connaît (les rangs viennent d'elle), sinon celui des dates de sortie (rangs : None)."""
    tagged = sorted((r for r in movies if r["grp"] == group), key=release_key)
    ranks, absent = missing.tagged(group, tagged, info, set(hub.sagas.ignored()))
    if ranks is not None:
        tagged.sort(key=lambda r: ranks[r["id"]])
    return tagged, ranks, absent


def tagged_saga(hub, item, movies, seen, info, ratings=None):
    """(films, manquants) de la saga de l'étiquette d'un film : numérotés d'après la collection TMDB quand on la connaît, sinon par date de sortie."""
    tagged, ranks, absent = tagged_films(hub, item["grp"], movies, info)
    return number_films([with_seen(r, seen, card_quality(hub.db), ratings) for r in tagged], ranks), absent


def tagged_places(hub, group, movies):
    """[(film, rang)] de la saga de l'étiquette `group`, dans l'ordre de sa page. Sans collection TMDB, les versions d'un même film (même identifiant TMDB) prennent la place de la première sortie : comme dans une saga du fichier, elles ont le même rang."""
    films, ranks, _ = tagged_films(hub, group, movies, missing.load(hub.db))
    if ranks is None:
        places = {}
        ranks = {film["id"]: places.setdefault(sagafile.tmdb_of(film) or film["id"], len(places)) for film in films}
    return sorted(((film, ranks[film["id"]]) for film in films), key=lambda place: place[1])


def file_saga(name, films, listed, seen, owned, info, quality=None, ratings=None):
    """Une saga de `sagas.json` : ses films numérotés d'après leur place dans la liste, et ceux de la liste qu'on n'a pas."""
    numbered = number_films([with_seen(f, seen, quality, ratings) for f, _ in films], {f["id"]: rank for f, rank in films})
    return {"name": name, "films": numbered, "missing": missing.listed(listed.get(name, []), owned, info)}


def library_movies(hub, lib_id):
    """Les films d'une bibliothèque : une saga ne compare jamais que ceux-là."""
    return hub.db.query("SELECT * FROM items WHERE lib=? AND type='movie'", (lib_id,))


def file_sagas_of(custom, item):
    """Les sagas du fichier (`custom`, voir Sagas.of_movies) où figure ce film, dans l'ordre du fichier."""
    return {name: films for name, films in custom.items() if any(f["id"] == item["id"] for f, _ in films)}


def movie_sagas(hub, item, lib, user, ratings=None):
    """Ses sagas : celle de l'étiquette d'abord (`saga`, gardée pour les clients d'avant `sagas`), puis celles du fichier (sagas.py), chacune avec les films qui la composent et ceux qui lui manquent."""
    movies = library_movies(hub, item["lib"])
    seen = seen_counts(hub, user, lib.id)
    custom = hub.sagas.of_movies(movies)
    mine = file_sagas_of(custom, item)
    info, owned = (missing.load(hub.db), sagafile.by_tmdb(movies)) if item["grp"] or mine else (None, None)          # TMDB ne sert que s'il y a une saga
    out, sagas = {}, []
    if item["grp"]:
        out["saga"], absent = tagged_saga(hub, item, movies, seen, info, ratings)
        if item["grp"] not in custom:                   # une saga du fichier de même nom la remplace : c'est elle que ce nom désigne dans la grille
            sagas.append({"name": item["grp"], "films": out["saga"], "missing": absent})
    listed = dict(hub.sagas.all())
    sagas += [file_saga(name, films, listed, seen, owned, info, card_quality(hub.db), ratings) for name, films in mine.items()]
    sagas = visible_sagas(sagas)
    if sagas:
        out["sagas"] = sagas
    return out


def visible_sagas(sagas):
    """Les sagas de plus d'un film (présents ou manquants), sans la liste des manquants quand elle est vide."""
    return [{k: v for k, v in s.items() if v or k != "missing"} for s in sagas if len(s["films"]) + len(s["missing"]) > 1]


def video_badges(hub, rows):
    """{id du fichier: {quality, hdr, audio, subs}} pour les vidéos déjà sondées (à la lecture, ou par la tâche « Analyse des vidéos ») : voir badges.of_probe. Les autres n'y sont pas.
    Les sous-titres posés à côté de la vidéo comptent : chaque dossier n'est lu qu'une fois, même pour une saison entière."""
    listed, found = {}, {}
    for row in rows:
        probe = safe_json(row["probe"], None) if row["kind"] == "video" else None
        if not probe:
            continue
        path = hub.path_of(row)[1]
        if path.parent not in listed:
            listed[path.parent] = subtitles.siblings(path.parent)
        ours = badges.of_probe(probe, subtitles.side_files(path, listed[path.parent]))
        if ours:
            found[row["id"]] = ours
    return found


def season_badges(rows, found):
    """{numéro de saison (une chaîne, comme toute clé JSON): {quality, hdr, audio, subs}} d'après les épisodes sondés (`found` : ce que rend video_badges) ; une saison sans épisode sondé n'y est pas."""
    seasons = {}
    for row in rows:
        if row["season"] is not None and row["id"] in found:
            seasons.setdefault(str(row["season"]), []).append(found[row["id"]])
    return {season: badges.of_season(episodes) for season, episodes in seasons.items()}


@route("GET", r"/api/item/(\w+)")
def api_item(h, m, q):
    hub = h.hub
    tagged = item_of(h, m.group(1))
    lib = library_or_404(hub, tagged["lib"])
    item = shown_rows(hub, lib, [tagged])[0]
    rows = sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (item["id"],)), key=order_key)
    progress = hub.db.progress_for(h.user["id"], [r["id"] for r in rows])
    meta = meta_json(item["meta"])
    ratings = hub.db.ratings_for(h.user["id"])
    out = summary(item, opinion=ratings.get(item["id"]))
    out.update({"library": {"id": lib.id, "label": labels.shown(hub.db, lib), "kind": lib.kind}, "summary": item["summary"], "tagged": bool(meta.get("tagged"))})
    out.update({key: meta[key] for key in ITEM_META_KEYS if meta.get(key) not in (None, "", [])})
    links = album_links(meta) if item["type"] == "album" else video_links(item, meta)
    if links:
        out["links"] = links
    found = video_badges(hub, rows)
    out["entries"] = [{**entry_json(r, progress.get(r["id"])), **found.get(r["id"], {})} for r in rows]
    out["resume"] = resume_target(rows, progress)
    if item["type"] == "show":
        out["seasons"] = meta.get("seasons", [])
        if hub.db.kv_get(enrich.pin_key(item)):
            out["pinned"] = True                    # la fiche TMDB a été choisie à la main dans le hub : `POST /api/item/<id>/tmdb` avec `{"tmdb": null}` la retire
        by_season = season_badges(rows, found)
        if by_season:
            out["season_badges"] = by_season
    if item["type"] == "movie":
        out.update(movie_sagas(hub, item, lib, h.user["id"], ratings))
        mark_requests(h, *[saga.get("missing") for saga in out.get("sagas", [])])
    if item["type"] == "album":
        album_extras(h, out, tagged, item)
    h.send_json(out)


def album_extras(h, out, tagged, shown):
    """Ce que la fiche d'un album ajoute : son nombre de pistes, `edited` pour l'administrateur, et le crédit des pistes qui portaient le nom de l'artiste des étiquettes (`tagged`), remplacé par son nom affiché (`shown`, voir artistedit)."""
    out["tracks_total"] = len(out["entries"])
    out.update(edited_sheet(h, tagged))
    for track in out["entries"]:
        track["artist"] = artistedit.credit(track["artist"], tagged["grp"], shown["grp"])


def sheet_facts(info, lang, fixes):
    """« Groupe · France · 1993 – 2021 » : le type, le pays (traduit en français pour `lang` = 'fr', sinon tel que MusicBrainz le donne) et les années d'activité, ces deux derniers corrigés par l'administrateur s'il l'a fait (`fixes`, voir artistedit)."""
    area = fixes.get("country") or (countries.french(info.get("area")) if lang == "fr" else info.get("area"))
    parts = (artistinfo.TYPES.get(info.get("type"), ""), area, fixes.get("active") or artistinfo.lifespan(info))
    return " · ".join(p for p in parts if p)


def artist_sheet(info, mbid="", lang="fr", edits=None, fixes=None):
    """Ce que la fiche montre d'un artiste, à partir de ce que MusicBrainz et Wikipédia en ont dit (voir libraries.music.artistinfo) et de ce que l'administrateur a corrigé (`fixes` : pays, années d'activité, résumé ; voir artistedit).
    Un résumé corrigé est rendu sous `summary`, à la place de celui de Wikipédia (`wiki` vaut alors None : le texte n'est plus le sien ; son adresse reste parmi les liens).
    `edits` (voir artist_link_edits) : les liens que l'administrateur a ajoutés (avant Wikipédia et MusicBrainz) et retirés (de la liste finale, quels qu'ils soient)."""
    fixes = fixes or {}
    members = [{"name": mem["name"], "roles": mem.get("roles", []), "period": artistinfo.period(mem), "former": bool(mem.get("ended"))} for mem in info.get("members") or []]
    out = {"facts": sheet_facts(info, lang, fixes), "disambiguation": info.get("disambiguation", ""), "wiki": info.get("wiki"), "members": members, "links": sheet_links(info, mbid, edits or {})}
    if "summary" in fixes:
        out.update(summary=fixes["summary"], wiki=None)
    return out


def sheet_links(info, mbid, edits):
    """Les liens de la fiche : ceux de MusicBrainz ramenés à WANTED_LINKS, ceux que l'administrateur a ajoutés, Wikipédia et MusicBrainz, sans ceux qu'il a retirés, chaque adresse une fois."""
    links = enrich.pick_links(info.get("links", []))       # aussi à l'affichage : les fiches déjà gardées gardent peut-être d'autres liens
    links.extend(edits.get("added", []))
    if (info.get("wiki") or {}).get("url"):
        links.append({"label": "Wikipédia", "url": info["wiki"]["url"]})
    if mbid:
        links.append({"label": "MusicBrainz", "url": musicbrainz.artist_url(mbid)})
    removed = set(edits.get("removed", []))
    seen = set()
    return [link for link in links if link["url"] not in removed and not (link["url"] in seen or seen.add(link["url"]))]       # une adresse une seule fois : une fiche refaite peut avoir trouvé ce que l'administrateur avait ajouté


def artist_albums(hub, lib_id, artist_id):
    """Les albums d'un artiste, les siens comme ses collaborations (voir credits_artist), du plus ancien au plus récent ; 404 si la bibliothèque n'est pas de la musique ou si l'artiste n'a ni album ni collaboration."""
    lib = library_or_404(hub, lib_id)
    if lib.kind != "music":
        raise HttpError(404, "pas une bibliothèque de musique")
    rows = [r for r in hub.db.query("SELECT * FROM items WHERE lib=? AND type='album'", (lib.id,)) if credits_artist(meta_json(r["meta"]), artist_id)]
    if not rows:
        raise HttpError(404, "artiste inconnu")
    return lib, sorted(rows, key=TITLE_SORTS["music"])


def kept_sheet(hub, mbid):
    """La fiche MusicBrainz gardée sous ce `mbid` (avec lui), ou None."""
    kept = hub.db.kv_get(enrich.artist_key(mbid)) if mbid else None
    return {**kept, "mbid": mbid} if kept else None


def artist_mbid_of(meta, artist_id):
    """L'identifiant MusicBrainz qu'un album donne à cet artiste : celui de son crédit dans une collaboration, sinon celui de l'artiste de l'album."""
    credit = next((c for c in album_credits(meta) if c.get("id") == artist_id), None)
    return credit.get("mbid", "") if credit else meta.get("artist_mbid")


def artist_kept(hub, rows, artist_id):
    """La fiche gardée par « Fiches artistes » (voir enrich.enrich_artists), ou None : celle du premier album qui donne à l'artiste un identifiant MusicBrainz et qui en a une. C'est le rapprochement automatique ; la photo s'en tient à lui."""
    return next(filter(None, (kept_sheet(hub, artist_mbid_of(meta_json(row["meta"]), artist_id)) for row in rows)), None)


def artist_shown(hub, rows, fixes, artist_id):
    """La fiche que montre la page de l'artiste : celle que l'administrateur a choisie (`fixes["mbid"]`, voir artistedit) si elle est gardée, sinon le rapprochement automatique (artist_kept)."""
    return kept_sheet(hub, fixes.get("mbid")) or artist_kept(hub, rows, artist_id)


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
    if not web or any(c.isspace() or ord(c) < 33 for c in url):
        raise HttpError(400, "l'adresse doit être un lien web (http:// ou https://)")
    return url


def artist_link_target(hub, m):
    """(mbid, fiche gardée) de l'artiste visé par une route de liens ; 404 s'il est inconnu ou n'a pas de fiche (sans identifiant MusicBrainz, ou « Fiches artistes » ne l'a pas encore gardée).
    C'est la fiche montrée (artist_shown) : les liens corrigés, rangés sous son mbid, suivent la fiche choisie."""
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    kept = artist_shown(hub, rows, artistedit.of_artist(hub.db, lib.id, m.group(2)), m.group(2))
    if kept is None:
        raise HttpError(404, "pas de fiche pour cet artiste")
    return kept["mbid"], kept


def artist_links_now(h, kept, edits):
    """Les liens de la fiche tels qu'elle les montre, modifications comprises."""
    return artist_sheet(kept["info"], kept["mbid"], h.lang, edits)["links"]


PHOTO_UPLOAD_MAX = 10 << 20


def artist_home(lib, rows, artist_id):
    """Le dossier de l'artiste à la racine de la médiathèque (voir scan.music.artist_dirs), où va sa photo ; None si le dernier scan ne lui en a pas trouvé ou s'il n'est plus là.
    Seuls comptent les albums dont il est l'artiste principal : un participant d'une collaboration n'a pas le dossier de l'album, sa photo va sous HUB_DATA/artists."""
    for row in rows:
        meta = meta_json(row["meta"])
        name = meta.get("artist_dir") if meta.get("artist_id") == artist_id else None
        if name and (lib.path / name).is_dir():
            return lib.path / name
    return None


def artist_photo(hub, artist_id, kept, home=None):
    """Le fichier de la photo d'un artiste, la seule qu'il ait (celle que l'utilisateur a envoyée) ; None s'il n'en a pas."""
    return enrich.photo_file(hub.cfg.data / "artists", artist_id, (kept or {}).get("mbid", ""), home)


def artist_photo_of(hub, lib, rows, artist_id):
    """artist_photo de l'artiste dont `rows` sont les albums (voir artist_albums)."""
    return artist_photo(hub, artist_id, artist_kept(hub, rows, artist_id), artist_home(lib, rows, artist_id))


def save_artist_photo(hub, lib, rows, artist_id, data, box=None):
    """enrich.save_photo, ses échecs rendus en réponses HTTP (voir photo_errors)."""
    with photo_errors():
        enrich.save_photo(hub.cfg, artist_id, data, artist_home(lib, rows, artist_id), box, (artist_kept(hub, rows, artist_id) or {}).get("mbid", ""))


@contextmanager
def photo_errors():
    """Les échecs de l'enregistrement d'une photo (d'artiste, de personne) rendus en réponses HTTP : 415 (image illisible), 501 (rien pour convertir), 500 (écriture impossible : le dossier de la médiathèque est peut-être en lecture seule)."""
    try:
        yield
    except NoConverter as e:
        raise HttpError(501, str(e))
    except ConvertError as e:
        raise HttpError(415, str(e))
    except OSError as e:
        raise HttpError(500, f"photo non enregistrée : {e}")


def body_box(body):
    """Le cadre `{"x", "y", "w", "h"}` (fractions de l'image) d'un corps de requête ; 400 s'il n'en est pas un."""
    try:
        return crop_box(",".join(str(body.get(k, "")) for k in ("x", "y", "w", "h")))
    except ValueError as e:
        raise HttpError(400, str(e))


def read_photo(path):
    """Les octets de la photo `path` ; 500 si elle ne se lit pas."""
    try:
        return path.read_bytes()
    except OSError as e:
        raise HttpError(500, f"photo illisible : {e}")


def photo_box(q):
    """Le cadre `crop=x,y,largeur,hauteur` (fractions de l'image) de la requête, ou None."""
    text = q.get("crop")
    if not text:
        return None
    try:
        return crop_box(text)
    except ValueError as e:
        raise HttpError(400, str(e))


def artist_sections(rows, seen):
    """{"albums", "collaborations"} de la fiche d'un artiste : ses albums où il est le seul crédité, et ceux qu'il partage avec d'autres (voir album_credits)."""
    out = {"albums": [], "collaborations": []}
    for row in rows:
        out["collaborations" if album_credits(meta_json(row["meta"])) else "albums"].append(with_seen(row, seen))
    return out


def artist_tagged_name(rows, artist_id):
    """Le nom de l'artiste selon les étiquettes : celui de son premier album à lui seul, sinon celui de son crédit dans sa première collaboration."""
    solo = [row for row in rows if not album_credits(meta_json(row["meta"]))]
    if solo:
        return solo[0]["grp"] or "Artiste inconnu"
    return next((c["name"] for row in rows for c in album_credits(meta_json(row["meta"])) if c.get("id") == artist_id), "Artiste inconnu")


def artist_tracks(hub, rows, name):
    """(pistes, {genre: nombre d'albums}) de tous les albums d'un artiste, les pistes dans l'ordre de chaque album. `name` : le nom affiché ; une piste créditée du nom des étiquettes prend celui-là (voir artistedit.credit)."""
    tracks, genres = [], {}
    for row in rows:
        for entry in sorted(hub.db.query("SELECT * FROM entries WHERE item_id=?", (row["id"],)), key=order_key):
            artist = artistedit.credit(entry_json(entry)["artist"], row["grp"], None if album_credits(meta_json(row["meta"])) else name) or name      # une collaboration garde « A & B »
            tracks.append({"id": entry["id"], "title": entry["title"], "artist": artist, "album": row["title"], "item": row["id"], "duration": entry["duration"],
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
    fixes = artistedit.of_artist(hub.db, lib.id, m.group(2))
    name = fixes.get("name") or artist_tagged_name(rows, m.group(2))
    tracks, genres = artist_tracks(hub, rows, name)
    shown = [artistedit.renamed(r, {m.group(2): fixes["name"]}) for r in rows] if "name" in fixes else rows
    out = {"library": {"id": lib.id, "label": labels.shown(hub.db, lib), "kind": lib.kind}, "id": m.group(2), "name": name, "tracks": tracks, **artist_sections(shown, seen)}
    out.update(artist_genres(hub, lib, genres, fixes))
    out["duration"] = sum(t["duration"] or 0 for t in out["tracks"])
    info = artist_info(h, artist_shown(hub, rows, fixes, m.group(2)), fixes)
    if info:
        out["info"] = info
    out["wanted"] = [{"year": w["year"], "title": w["title"], "cover": f"/img/wanted/{lib.id}/{m.group(2)}/{w['key']}" if w["cover"] else ""} for w in wanted_albums(hub, lib.id, m.group(2))]
    out["photo"] = artist_photo_of(hub, lib, rows, m.group(2)) is not None          # la photo suit le rapprochement automatique, jamais la fiche choisie
    if out["photo"]:
        out["image"] = urls.artist_image(lib.id, m.group(2))
    if h.user["role"] == "admin":
        out["edited"] = artistedit.listed(fixes)            # les autres comptes ne savent pas que la fiche a été corrigée
    h.send_json(out)


def artist_genres(hub, lib, counted, fixes):
    """`genres` de la fiche (les cinq plus fréquents de ses albums, ou ceux que l'administrateur a donnés) et `genres_linkable` : ceux que le filtre « Genre » de la bibliothèque connaît, les seuls dont l'interface fait un lien. Un genre corrigé ne change pas le filtre, qui suit les albums."""
    if "genres" not in fixes:
        top = [g for g, _ in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        return {"genres": top, "genres_linkable": top}
    known = {g for row in hub.db.query("SELECT genres FROM items WHERE lib=?", (lib.id,)) for g in safe_json(row["genres"], [])}
    return {"genres": fixes["genres"], "genres_linkable": [g for g in fixes["genres"] if g in known]}


def artist_info(h, kept, fixes):
    """La fiche (voir artist_sheet) d'après la fiche MusicBrainz montrée `kept` et les corrections ; sans fiche gardée, celle que font les seules corrections (pays, années, résumé), ou None s'il n'y en a pas."""
    if kept:
        return artist_sheet(kept["info"], kept["mbid"], h.lang, artist_link_edits(h.hub, kept["mbid"]), fixes)
    if any(field in fixes for field in artistedit.SHEET_FIELDS):
        return artist_sheet({}, "", h.lang, None, fixes)
    return None


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


# ---------------------------------------------------------------------------
# Corriger un artiste dans le hub (voir artistedit)
# ---------------------------------------------------------------------------
def save_artist_edits(h, lib, artist_id, changes, resets):
    """Garde les corrections de l'artiste et répond `{"ok": true, "edited": [champs corrigés]}`."""
    with artistedit.LOCK:
        kept = artistedit.save(h.hub.db, lib.id, artist_id, changes, resets)
    h.hub.db.items_changed()            # ce que la musique montre a changé (nom corrigé) : ce que les demandes en ont tiré, gardé jusqu'au prochain scan, est à refaire
    h.send_json({"ok": True, "edited": artistedit.listed(kept)})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/edit")
def api_artist_edit(h, m, q):
    """Corrige la fiche d'un artiste dans le hub (administrateur seulement), sans rien écrire dans la médiathèque ni changer son identifiant : `{"name", "genres", "summary", "country", "active", "mbid"}`, un ou plusieurs champs ; `null` rétablit un champ (pour `mbid`, revient au rapprochement automatique : la fiche se choisit par `POST /api/artist/<bibliothèque>/<id>/musicbrainz`).
    Nom de 1 à 200 caractères, 20 genres de 60 caractères au plus, résumé de 5000 caractères au plus (les retours à la ligne sont gardés), pays de 100, années d'activité de 60 ; aucun caractère de contrôle. 400 au moindre champ refusé : rien n'est gardé. Rend `{"ok": true, "edited": [champs corrigés]}`."""
    require_admin(h)
    lib, _ = artist_albums(h.hub, m.group(1), m.group(2))
    try:
        changes, resets = artistedit.parse(h.body_json())
    except artistedit.Invalid as e:
        raise HttpError(400, str(e))
    save_artist_edits(h, lib, m.group(2), changes, resets)


MB_RESULTS = 10
MB_QUERY_MAX = 200


def musicbrainz_failure(e):
    """Un échec de MusicBrainz, rendu en réponse HTTP : 502, avec ce que MusicBrainz a dit."""
    return HttpError(502, f"MusicBrainz : {e}")


@route("GET", r"/api/artist/([\w-]+)/(\w+)/musicbrainz")
def api_artist_musicbrainz_search(h, m, q):
    """Les artistes que MusicBrainz trouve sous le nom `q` (200 caractères au plus ; 10 réponses au plus, de la plus à la moins pertinente) : `{"results": [{"mbid", "name", "type", "country", "disambiguation", "score"}]}`. Administrateur seulement."""
    require_admin(h)
    artist_albums(h.hub, m.group(1), m.group(2))
    query = q.get("q", "").strip()
    if not query or len(query) > MB_QUERY_MAX:
        raise HttpError(400, f"q : de 1 à {MB_QUERY_MAX} caractères")
    try:
        found = enrich.musicbrainz(h.hub.cfg).search_artists(query, MB_RESULTS)
    except enrich.MusicBrainzError as e:
        raise musicbrainz_failure(e)
    h.send_json({"results": [{"mbid": a.get("id", ""), "name": a.get("name", ""), "type": a.get("type", ""), "country": a.get("country", ""), "disambiguation": a.get("disambiguation", ""),
                              "score": a.get("score", 0)} for a in found[:MB_RESULTS]]})


@route("POST", r"/api/artist/([\w-]+)/(\w+)/musicbrainz")
def api_artist_musicbrainz(h, m, q):
    """Choisit la fiche MusicBrainz d'un artiste : `{"mbid": "<identifiant>"}` (ou l'adresse de sa fiche). Elle est demandée à MusicBrainz tout de suite (résumé, membres, liens) et passe avant celle de ses albums. `{"mbid": null}` revient au rapprochement automatique.
    Les liens corrigés sont rangés sous la fiche : ils suivent celle qui est montrée. 400 pour un identifiant invalide, 502 si MusicBrainz ne répond pas ou ne connaît pas la fiche (rien n'est alors changé). Administrateur seulement. Rend `{"ok": true, "edited": [champs corrigés]}`."""
    require_admin(h)
    lib, _ = artist_albums(h.hub, m.group(1), m.group(2))
    body = h.body_json()
    if "mbid" in body and body["mbid"] is None:
        save_artist_edits(h, lib, m.group(2), {}, ["mbid"])
        return
    try:
        mbid = artistedit.mbid(body.get("mbid"))
    except artistedit.Invalid as e:
        raise HttpError(400, str(e))
    try:
        enrich.choose_artist(h.hub.cfg, h.hub.db, mbid)
    except enrich.MusicBrainzError as e:
        raise musicbrainz_failure(e)
    save_artist_edits(h, lib, m.group(2), {"mbid": mbid}, [])


@route("POST", r"/api/artist/([\w-]+)/(\w+)/photo/crop")
def api_artist_photo_crop(h, m, q):
    """Ne garde de la photo de l'artiste que le cadre `{"x", "y", "w", "h"}` (fractions de l'image) : ce qui est hors du cadre disparaît du fichier. Administrateur seulement."""
    require_admin(h)
    hub = h.hub
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    box = body_box(h.body_json())
    path = artist_photo_of(hub, lib, rows, m.group(2))
    if path is None:
        raise HttpError(404, "pas de photo à recadrer")
    save_artist_photo(hub, lib, rows, m.group(2), read_photo(path), box)
    h.send_json({"ok": True})


@route("GET", r"/img/artist/([\w-]+)/(\w+)")
def artist_image(h, m, q):
    """La photo d'un artiste, réduite comme les pochettes ; 404 s'il n'en a pas (la page garde alors la pochette de son premier album)."""
    hub = h.hub
    h.require(m.group(1))
    lib, rows = artist_albums(hub, m.group(1), m.group(2))
    path = artist_photo_of(hub, lib, rows, m.group(2))
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
    h.hub.activity.touch(h.user["id"], entry["id"])          # un film lu depuis la mémoire tampon du navigateur ne demande plus rien : sa ligne d'activité ne doit pas s'éteindre pour autant
    h.send_json({"ok": True, "done": done})


def rated_item(h, ident):
    """L'élément `ident` pour poser un avis : 404 quand il n'existe pas ou que le compte ne voit pas sa bibliothèque (on ne les distingue pas : le refus ne dit rien d'une bibliothèque cachée), 400 quand ce n'est pas un film ni une série."""
    item = h.hub.item(ident)
    if not h.can(item["lib"]):
        raise HttpError(404, "élément inconnu")
    if item["type"] == "saga":                  # garde pour une ligne `items` de type saga, que le scan ne produit pas : une saga calculée n'a pas d'identifiant d'élément et répond 404 plus haut
        raise HttpError(400, "une saga ne se note pas")
    if item["type"] not in RATED_TYPES:
        raise HttpError(400, "cette bibliothèque ne se note pas")
    return item


@route("POST", r"/api/item/(\w+)/rating")
def api_item_rating(h, m, q):
    """Pose, change ou efface l'avis du compte sur un film ou une série : `{"rating": "up" | "mid" | "down" | null}` (`null` ou clé absente : effacer). Rend `{"id", "rating"}`. 404 pour un élément inconnu ou d'une bibliothèque qu'il ne voit pas, 400 pour un corps qui n'est pas un objet JSON, un avis inconnu ou un élément qui ne se note pas (musique, livres)."""
    body = strict_body_dict(h)                 # un corps vide ou illisible est un 400, pas un « effacer » ; lu avant rated_item : un 404 ne dit pas ce que l'élément est
    item = rated_item(h, m.group(1))
    rating = body.get("rating")
    if rating is not None and rating not in RATINGS:
        raise HttpError(400, f"avis inconnu : {str(rating)[:40]}")
    h.hub.db.set_rating(h.user["id"], item["id"], rating)
    h.send_json({"id": item["id"], "rating": rating})


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
    """Le résumé ffprobe d'un fichier, lu une fois et gardé dans la base. Un résumé d'une version plus ancienne sert tel quel (sans chapitres) : c'est l'analyse en fond qui le refait, jamais la lecture."""
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


FULL_HD = 1080              # la hauteur à laquelle on ramène un 4K à convertir ; une vidéo dont on ne connaît pas la hauteur n'est pas au-dessus
UHD_FROM = 2000             # à partir de cette hauteur, une image est du 4K


def default_height(decision, video, height):
    """La hauteur demandée ; à défaut, 1080 p pour un 4K à convertir (en pleine taille, il ferait ramer le processeur pour rien)."""
    if decision["mode"] == "transcode" and not height and video.get("height", FULL_HD) > FULL_HD:
        return FULL_HD
    return height


def show_title(hub, entry):
    """Le titre de la série d'un épisode ; vide pour un film."""
    item = hub.db.one("SELECT title, type FROM items WHERE id=?", (entry["item_id"],))
    return item["title"] if item and item["type"] == "show" else ""


def entry_label(show, entry):
    """Le titre d'un fichier pour l'administrateur : « Série · Épisode », ou le titre seul."""
    return f"{show} · {entry['title']}" if show else entry["title"]


def play_warning(decision, video):
    if decision["mode"] == "transcode" and video.get("height", UHD_FROM - 1) >= UHD_FROM and video.get("codec") in ("hevc", "av1"):
        return "Image 4K à convertir : le serveur n'a pas de carte vidéo pour cela, la lecture risque de saccader. Le débit demandé peut être baissé."
    return ""


def resume_position(hub, user, entry):
    """Où reprendre : la position enregistrée, tant que le fichier n'est pas fini et qu'on n'en est pas au tout début."""
    progress = progress_of(hub, user, entry)
    return progress["position"] if progress and not progress["done"] and progress["position"] >= RESUME_MIN else 0


def next_summary(following):
    """Le fichier vidéo qui suit celui-ci (épisode suivant, partie suivante), ou None."""
    if following and following["kind"] == "video":
        return {"id": following["id"], "title": following["title"], "season": following["season"], "number": following["number"]}
    return None


def sagas_to_follow(hub, item, movies):
    """Les sagas où chercher la suite d'un film, (nom, [(film, rang)]) : celles du fichier dans leur ordre, puis celle de l'étiquette, sauf si une saga du fichier porte son nom (elle la remplace, comme sur la fiche)."""
    custom = hub.sagas.of_movies(movies)
    yield from file_sagas_of(custom, item).items()
    if item["grp"] and item["grp"] not in custom:
        yield item["grp"], tagged_places(hub, item["grp"], movies)


def first_video(hub, item_id):
    """Le premier fichier vidéo d'un élément, ou None."""
    return min(hub.db.query("SELECT * FROM entries WHERE item_id=? AND kind='video'", (item_id,)), key=order_key, default=None)


def film_after(hub, item, films):
    """(film, fichier) du premier film présent après `item` dans `films` ([(film, rang)] dans l'ordre de la saga), ou None. Les versions de `item` ont son rang : aucune ne le suit."""
    rank = next(place for film, place in films if film["id"] == item["id"])
    later = ((film, first_video(hub, film["id"])) for film, place in films if place > rank)
    return next((found for found in later if found[1] is not None), None)


def next_in_saga(hub, entry):
    """Ce qui suit un film fini dans sa saga, sous la forme de `next` avec le nom de la saga en plus ; None si ce n'est pas un film ou s'il n'y a pas de suite présente. Seule sa bibliothèque compte : le droit de lire le film suivant est celui de lire ce film."""
    movies = library_movies(hub, entry["lib"])
    item = next((movie for movie in movies if movie["id"] == entry["item_id"]), None)
    if item is None:
        return None
    for name, films in sagas_to_follow(hub, item, movies):
        found = film_after(hub, item, films)
        if found:
            return {**next_summary(found[1]), "title": found[0]["title"], "saga": name}
    return None


def play_next(hub, user, entry):
    """Le `next` du plan de lecture : le fichier suivant de l'élément, avec la progression du compte `user` (voir with_progress), sinon, après la dernière partie d'un film, le film suivant de sa saga."""
    following = next_entry(hub, entry)
    if following is None:
        return next_in_saga(hub, entry)
    found = next_summary(following)
    return found and with_progress(found, progress_of(hub, user, following))


def progress_of(hub, user, entry):
    """La ligne de progression du compte `user` sur ce fichier, ou None s'il ne l'a pas commencé."""
    return hub.db.progress_for(user, [entry["id"]]).get(entry["id"])


@route("POST", r"/api/play/(\w+)")
def api_play(h, m, q):
    """Le plan de lecture d'un fichier vidéo pour CE lecteur. Le client l'appelle à l'ouverture, puis à chaque changement de piste audio.

    Sans piste demandée, la piste audio suit les langues audio du compte (`audio_langs`, voir tracks.pick_audio) ; `selected` marque la piste audio jouée et
    au plus un sous-titre choisi d'office d'après `sub_mode` et `sub_langs` (à défaut, la langue de la page)."""
    hub = h.hub
    entry = entry_of(h, m.group(1), "play")
    if entry["kind"] != "video":
        raise HttpError(400, "ce fichier n'est pas une vidéo")
    body = h.body_json()
    lib, path = hub.path_of(entry)
    probe = probe_of(hub, entry)
    caps, audio, kbps, height = play_settings(body)
    settings = prefs.load(hub.db, h.user["id"])
    if audio is None:                                                                       # une piste demandée par le lecteur (choix pendant la lecture) l'emporte toujours
        audio = tracks.pick_audio(probe["audio"], settings["audio_langs"])
    decision = prober.plan(probe, caps, audio, kbps, height)
    show = show_title(hub, entry)
    hub.activity.decide(h.user["id"], h.user["name"], client_ip(h), h.headers.get("User-Agent", ""), body.get("device"), entry["id"], entry_label(show, entry), caps, decision["mode"], decision["why"])
    chosen = decision["audio"]
    video = probe.get("video") or {}
    height = default_height(decision, video, height)
    subs = subtitle_list(hub, entry, probe, path, h.lang)
    heard = next((t["lang"] for t in probe["audio"] if t["index"] == chosen), "")
    picked = tracks.pick_sub(subs, settings["sub_mode"], settings["sub_langs"] or [h.lang], heard, settings["audio_langs"])
    for sub in subs:
        sub["selected"] = sub["id"] == picked
    h.send_json(with_progress({
        "entry": entry["id"], "item": entry["item_id"], "title": entry["title"], "show": show, "season": entry["season"], "number": entry["number"],
        "mode": decision["mode"], "why": decision["why"], "url": play_url(entry, decision, caps, kbps, height), "seekable": decision["mode"] == "direct",
        "duration": probe["duration"] or entry["duration"], "resume": resume_position(hub, h.user["id"], entry), "warning": play_warning(decision, video),
        "audio": [{"index": t["index"], "label": track_label(t, h.lang), "lang": t["lang"], "selected": t["index"] == chosen, "commentary": t["commentary"]} for t in probe["audio"]],
        "subs": subs,
        "video": {"codec": video.get("codec"), "width": video.get("width"), "height": video.get("height"), "hdr": video.get("hdr")} if video else None,
        "next": play_next(hub, h.user["id"], entry),
        "chapters": prober.with_skips(probe.get("chapters", [])),
    }, progress_of(hub, h.user["id"], entry)))


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


@route("POST", r"/api/play/(\w+)/vlc")
def api_play_vlc(h, m, q):
    """Pour « Ouvrir dans VLC » : `{url, title, resume}`. `url` est relative (`/media/<id>/file?token=<jeton VLC>`, le navigateur y met son adresse), sans jeton quand le serveur est ouvert ; `resume`, la position à reprendre en secondes."""
    h.body_json()
    entry = entry_of(h, m.group(1), "play")
    if entry["kind"] != "video":
        raise HttpError(400, "ce fichier n'est pas une vidéo")
    url = f"/media/{entry['id']}/file"
    if h.hub.auth():
        url += "?token=" + h.hub.vlc_token(accounts.by_id(h.hub.db, h.user["id"]), entry["id"])
    h.send_json({"url": url, "title": entry_label(show_title(h.hub, entry), entry), "resume": resume_position(h.hub, h.user["id"], entry)})


@route("GET", r"/media/(\w+)/file")
def media_file(h, m, q):
    entry = entry_of(h, m.group(1), "download" if q.get("download") else "play")         # `?download=1` demande le droit de télécharger ; sans lui, c'est la lecture directe (Range) : le droit de lire
    lib, path = h.hub.path_of(entry)
    extra = {"Cache-Control": "private, max-age=3600"}
    if q.get("download"):
        extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(path.name)
    h.send_file(path, headers=extra, track=file_track(h, entry, bool(q.get("download"))), attempt=download_attempt(h, q))


def download_attempt(h, q):
    """La tentative de téléchargement que nomme `dl` (voir Activity.attempt), ou None : sans `?download=1`, ou avec un `dl` absent ou mal formé, le fichier part comme avant.
    `dl` ne donne aucun droit : `entry_of` a déjà vérifié celui de télécharger."""
    dl = attempt_id(q.get("dl")) if q.get("download") else ""
    return (lambda start, size: h.hub.activity.attempt(h.user["id"], dl, start, size)) if dl else None


def no_attempt(start, size):
    return nullcontext()


def counters(*counts):
    """Une seule fonction de comptage pour `copy_range`, qui appelle toutes celles qui sont là ; None s'il n'y en a aucune."""
    present = [count for count in counts if count]
    if not present:
        return None
    return lambda size: [count(size) for count in present]


def file_track(h, entry, download):
    """Ce que l'envoi de ce fichier inscrit dans l'activité (voir Handler.send_file), ou None : un téléchargement, une vidéo lue en direct (`direct`) ou une piste (`audio`).
    Un livre qu'on lit n'est pas suivi : ses pages sont chargées d'un coup, un redémarrage ne coupe pas une page déjà là."""
    kind = "download" if download else {"video": "direct", "audio": "audio"}.get(entry["kind"])
    if kind is None:
        return None
    return lambda: h.transfer(kind, entry["id"], entry_label(show_title(h.hub, entry), entry), entry["lib"])


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


def send_stream(h, proc, first, sent):
    """Les octets de ffmpeg vers le client, comptés par `sent` ; rien pour un HEAD."""
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
    sent(len(first))
    while True:
        chunk = proc.stdout.read1(CHUNK)
        if not chunk:
            break
        h.wfile.write(chunk)
        sent(len(chunk))


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
    hub.count_stream(1)
    proc = None
    try:
        kind = "transcode" if q.get("m") == "transcode" else "remux"
        with h.transfer(kind, entry["id"], entry_label(show_title(hub, entry), entry), entry["lib"], leave_empty=False) as sent:          # dès le lancement : ffmpeg peut mettre des secondes à produire son premier bloc, et relancer le serveur couperait déjà cette lecture
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            errors = []
            threading.Thread(target=lambda: errors.append(proc.stderr.read().decode("utf-8", "replace")), daemon=True).start()
            send_stream(h, proc, first_chunk(proc, errors), sent)
    finally:
        if proc is not None:
            stop_stream(proc)
        hub.count_stream(-1)
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
POSTER_TYPES = ("movie", "show", "album")


def admin_item(h, ident, types, refusal):
    """L'item `ident` (administrateur seulement ; 404 s'il n'existe pas) quand son type est l'un de `types` ; 400 `refusal` sinon."""
    require_admin(h)
    item = h.hub.item(ident)
    if item["type"] not in types:
        h.discard_body()
        raise HttpError(400, refusal)
    return item


def poster_item(h, ident):
    """L'item dont on change l'affiche (administrateur) : un film, une série ou un album."""
    return admin_item(h, ident, POSTER_TYPES, "seuls un film, une série ou un album ont une affiche à changer")


@route("POST", r"/api/item/(\w+)/poster")
def api_item_poster(h, m, q):
    """Remplace l'affiche d'un film ou d'une série, ou la pochette d'un album, par l'image envoyée (le corps de la requête : JPEG, PNG ou WebP, 10 Mo au plus). Un WebP est converti en JPEG ; 415 s'il ne se lit pas, 501 si rien ne sait le convertir. L'image reste dans HUB_DATA : rien n'est écrit dans la médiathèque. Une autre image remplace celle-ci ; pour revenir à l'image d'origine d'un album, `POST /api/item/<id>/edit` avec `{"poster": null}`."""
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
# Corriger un album dans le hub
# ---------------------------------------------------------------------------
def edited_fields(hub, item):
    """Les champs d'un album que l'administrateur a corrigés, dans l'ordre de `edits.FIELDS`, puis `poster` si une pochette a été choisie."""
    kept = edits.kept(hub.db.kv_get(edits.key(item["lib"], item["id"])))
    return edits.listed(kept) + (["poster"] if hub.db.kv_get(POSTER_KEY + item["id"]) else [])


def edited_sheet(h, item):
    """`{"edited": [...]}` pour l'administrateur seul : les autres comptes ne savent pas qu'un album a été corrigé."""
    return {"edited": edited_fields(h.hub, item)} if h.user["role"] == "admin" else {}


@route("POST", r"/api/item/(\w+)/edit")
def api_item_edit(h, m, q):
    """Corrige un album dans le hub (administrateur seulement), sans rien écrire dans la médiathèque : `{"title", "year", "genres", "label", "type", "poster"}`, un ou plusieurs champs ; `null` rétablit la valeur lue dans les étiquettes (pour `poster`, l'image d'origine : la pochette se choisit par `POST /api/item/<id>/poster`).
    Titre de 1 à 300 caractères, année de quatre chiffres (de 1000 à l'an prochain), 20 genres de 60 caractères au plus, label de 200 au plus, type parmi `album`, `ep`, `single`, `compilation`, `live`, `soundtrack`, `remix` (le premier des types de sortie). 400 au moindre champ refusé : rien n'est gardé. Rend `{"ok": true, "edited": [champs corrigés]}`."""
    item = admin_item(h, m.group(1), ("album",), "seul un album se corrige")
    try:
        changes, resets = edits.parse(h.body_json())
    except edits.Invalid as e:
        raise HttpError(400, str(e))
    with edits.LOCK:          # le même verrou que le scan : deux administrateurs ne s'écrasent pas, et un scan n'écrit pas la bibliothèque entre la lecture de la ligne et sa réécriture
        edits.save(h.hub.db, h.hub.item(item["id"]), changes, resets)          # la ligne est relue sous le verrou : celle d'avant peut avoir changé (un autre enregistrement, un scan)
        if "poster" in resets:
            h.hub.images.clear_poster(item["id"])
    h.send_json({"ok": True, "edited": edited_fields(h.hub, item)})


# ---------------------------------------------------------------------------
# Fiche TMDB d'une série
# ---------------------------------------------------------------------------
TMDB_SHOW_RE = re.compile(r"^(?:https?://(?:www\.)?themoviedb\.org/tv/)?([0-9]+)(?:[-/?#].*)?$", re.IGNORECASE)         # un nombre, ou l'adresse de la fiche : « …/tv/4615-hercules-the-legendary-journeys »
TMDB_RESULTS = 10


def tmdb_show(h, ident):
    """(série, bibliothèque, client TMDB, dossier des affiches) pour choisir sa fiche TMDB (administrateur) ; 400 pour autre chose qu'une série, ou sans clé TMDB."""
    require_admin(h)
    item = h.hub.item(ident)
    if item["type"] != "show":
        h.discard_body()
        raise HttpError(400, "seule une série a une fiche TMDB à choisir")
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
    """Fixe la fiche TMDB d'une série (`{"tmdb": 4615}`, ou l'adresse de sa fiche) et l'enrichit à nouveau. Le choix reste dans la base du hub : la médiathèque n'est pas touchée. Administrateur seulement.
    `{"tmdb": null}` retire le choix : la série est enrichie comme avant (le nom du dossier décide) ; 404 si TMDB ne la trouve pas ainsi, et le choix reste."""
    item, lib, client, posters = tmdb_show(h, m.group(1))
    body = h.body_json()
    unpin = "tmdb" in body and body["tmdb"] is None
    found = None if unpin else TMDB_SHOW_RE.match(str(body.get("tmdb", "")).strip())
    if not unpin and not found:
        raise HttpError(400, "tmdb doit être l'identifiant d'une série TMDB, ou l'adresse de sa fiche")
    try:
        if unpin:
            if enrich.unpin_show(client, posters, h.hub.db, lib, item) is None:
                raise HttpError(404, "TMDB ne trouve pas cette série sans choix manuel : la fiche choisie est gardée")
        else:
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
    meta = meta_json(entry["meta"])
    kind = meta.get("format", "")
    progress = hub.db.progress_for(h.user["id"], [entry["id"]]).get(entry["id"])
    out = {"id": entry["id"], "item": entry["item_id"], "title": entry["title"], "format": kind, "position": progress["position"] if progress and not progress["done"] else 0,
           "next": None, "file": urls.entry_file(entry["id"])}
    try:
        if kind == "cbz":
            out["pages"] = len(bookmedia.cbz_pages(path))
            out["page_url"] = f"/book/{entry['id']}/page/{{n}}"          # `{n}` : le numéro de page, à partir de 0
            out["rtl"] = bookmedia.reads_right_to_left(meta.get("info"), meta_json(hub.item(entry["item_id"])["meta"]).get("section"))
        elif kind == "epub":
            book = bookmedia.epub_book(path)
            out["chapters"] = [f"/book/{entry['id']}/epub/{c}" for c in book["spine"]]
            out["toc"] = book["toc"]
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
            out.update({k: v for k, v in missing.entry(ident, None, info).items() if k in ("title", "year", "cover")})
        return out
    return {"tmdb": ident, "date": meta_json(row["meta"]).get("date") or row["year"], "item": summary(row)}


def sagas_json(hub):
    owned, info = sagafile.by_tmdb(movie_rows(hub)), missing.load(hub.db)
    ignored = [{k: v for k, v in missing.entry(i, None, info).items() if k in ("tmdb", "title", "year")} if str(i) in info["films"] else {"tmdb": i} for i in hub.sagas.ignored()]
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
    send_poster(h, q, f"film:{m.group(1)}", h.hub.cfg.data / "tmdb" / missing.poster_name(m.group(1)))


PERSON_ID_RE = re.compile(r"[0-9]{1,9}")          # un identifiant TMDB de personne : des chiffres ASCII (\d laisserait passer les chiffres d'autres écritures), neuf au plus


@route("GET", r"/img/person/([^/]*)")
def person_image(h, m, q):
    """La photo d'une personne : celle que l'administrateur a envoyée (HUB_DATA/people), sinon celle de TMDB que la passe des personnes a posée (HUB_DATA/tmdb, voir missing.photo_name) ; rien n'est demandé en ligne ici. 400 pour un identifiant qui n'est pas un nombre de neuf chiffres au plus, 404 sans photo."""
    ident = m.group(1)
    if not PERSON_ID_RE.fullmatch(ident):
        raise HttpError(400, "identifiant de personne invalide : un nombre de 9 chiffres au plus")
    path = enrich.person_photo(h.hub.cfg, int(ident)) or h.hub.cfg.data / "tmdb" / missing.photo_name(ident)          # sans photo : un chemin absent, send_poster répond 404
    send_poster(h, q, f"person:{ident}", path, "pas de photo")


def send_poster(h, q, ref, path, absent="pas d'affiche"):
    """Envoie l'affiche `path` (HUB_DATA/tmdb), réduite à `?w=` et gardée sous `ref` ; 404 (`absent`) quand le fichier n'y est pas."""
    path = path if path.is_file() else None
    try:
        width = int(q.get("w", 0))
    except ValueError:
        width = 0
    served = h.hub.images.file(ref, path, width) if path else None
    if served is None:
        raise HttpError(404, absent)
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
    found.sort(key=lambda f: release_key(f[1]) if group else (f[1]["sort"], f[1]["year"], f[1]["id"]))
    h.send_json({"films": [saga_film(ident, row) for ident, row in found[:limit]], "total": len(found)})


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------
@route("GET", r"/api/jobs")
def api_jobs(h, m, q):
    if q.get("log") == "1":                     # le journal des tâches est réservé à l'administrateur ; l'état « occupé » reste ouvert à tout compte
        require_admin(h)
    h.send_json({"jobs": h.hub.jobs.snapshot(with_log=q.get("log") == "1"), "busy": h.hub.jobs.busy()})


@route("GET", r"/api/activity")
def api_activity(h, m, q):
    """Ce qui se passe en ce moment, pour savoir si le serveur peut être relancé sans couper quelqu'un : administrateur seulement. Ni chemin, ni adresse de client, ni secret (voir activity.py)."""
    require_admin(h)
    hub = h.hub
    h.send_json({"version": __version__, "uptime": int(max(0, time.time() - hub.started)), "busy": hub.jobs.busy(), "blocking": hub.jobs.blocking(), "streams": hub.active, "max_streams": hub.cfg.max_streams, **hub.activity.report()})


@route("GET", r"/api/downloads")
def api_downloads(h, m, q):
    """Où en sont les tentatives de téléchargement demandées (`?dl=<id>,<id>,…`), pour le compte de la requête seulement : l'interface lance ainsi une file un fichier après l'autre.
    Un identifiant mal formé est écarté ; une tentative inconnue, oubliée ou d'un autre compte est `unknown`. Ni chemin, ni titre, ni adresse."""
    parts = [part for part in (q.get("dl") or "").split(",") if part]
    if len(parts) > DOWNLOADS_ASKED:
        raise HttpError(400, f"trop de téléchargements demandés d'un coup ({DOWNLOADS_ASKED} au plus)")
    ids = [dl for dl in map(attempt_id, parts) if dl]
    h.send_json({"downloads": h.hub.activity.attempts_of(h.user["id"], ids)})


@route("POST", r"/api/scan")
def api_scan(h, m, q):
    require_admin(h)
    body = h.body_json()
    jobs = h.hub.jobs
    job = jobs.scan(library_or_404(h.hub, body["lib"])) if body.get("lib") else jobs.scan_all()
    h.send_json({"job": job.public()}, 202)


@route("POST", r"/api/library/([\w-]+)/label")
def api_library_label(h, m, q):
    """L'administrateur renomme l'AFFICHAGE d'une bibliothèque : `{"label": "<texte>"}` (1 à 40 caractères, sans caractère de contrôle ni de mise en forme ; un objet sans `label`, `null` ou un texte vide rétablit le nom d'origine).
    Le corps doit être un objet JSON : vide, illisible ou d'un autre type, 400 (il ne rétablit rien). L'identifiant ne change jamais. 400 si le nom est déjà celui d'une autre bibliothèque
    (à la casse et aux accents près, nom d'origine compris). Rend `{"id", "label", "default_label", "kind"}`."""
    require_admin(h)
    lib = library_or_404(h.hub, m.group(1))
    body = strict_body_dict(h)
    try:
        labels.rename(h.hub.db, h.hub.cfg.libraries, lib, body.get("label"))
    except labels.LabelError as e:
        raise HttpError(400, str(e))
    h.send_json(labels.describe(h.hub.db, lib))


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
        raise HttpError(400, "l'enrichissement ne concerne que les films (sagas), les séries et la musique")
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

    def __init__(self, source, sent=None):
        self.source, self.count, self.sent = source, 0, sent

    def read(self, size):
        data = self.source.read(size)
        self.count += len(data)
        if self.sent:
            self.sent(len(data))                  # l'activité du hub compte ce qui arrive (voir api_inbox_send)
        return data


@route("POST", r"/api/inbox")
def api_inbox_send(h, m, q):
    """Un fichier envoyé dans la boîte de réception : `?name=<nom du fichier>`, le fichier en corps brut, `Content-Length` obligatoire, et `&dir=<chemin relatif>` (facultatif : des dossiers séparés par `/`, 4 niveaux au plus). Il va dans le dossier du compte sous HUB_INBOX (par défaut `HUB_DATA/inbox`), nommé d'après le compte,
    dans le sous-dossier demandé (créé s'il manque), sous un nom nettoyé, sans écraser (voir inbox.py).
    Les refus (droit, nom, extension, chemin, espace, nombre d'envois, plafond de sous-dossiers) viennent avant la lecture du corps ; la connexion est alors coupée, puisque le corps n'est pas lu, mais après avoir laissé la réponse arriver au client qui envoie encore (voir Handler.let_the_reply_arrive).
    Rend 201 `{"name", "size", "account", "dir"}` (`account` : le nom du dossier du compte, celui des URL `/api/inbox/<dossier>/<nom>` ; `dir` : le sous-dossier retenu, `""` à la racine)."""
    h.body_taken = True                     # le corps se lit par morceaux, ou pas du tout : _fail ne doit pas le jeter
    name = q.get("name") or ""
    with h.transfer("upload", f"upload:{q.get('dir') or ''}/{name}", name) as sent:         # un refus avant le premier octet n'y laisse rien (voir Activity.transfer)
        body = CountedBody(h.rfile, sent)
        try:
            require_sender(h)
            stored = module_call(InboxError, h.hub.inbox.receive, h.user["id"], q.get("name"), body, announced_length(h), q.get("dir"))
        except Exception:                   # un refus (HttpError), mais aussi une erreur système avant la lecture : dans tous les cas le corps n'est pas lu, ou à moitié
            h.close_connection = True
            h.body_consumed = body.count
            raise
    h.send_json(stored, 201)


def inbox_files(hub, files):
    """Les fichiers de la boîte avec le nom du compte qui les a envoyés (`user`). Le dossier d'un compte supprimé reste, avec ses fichiers : `user` est alors le nom du dossier, et `deleted` vaut vrai."""
    names = hub.inbox.owners()
    return [{**f, "user": names.get(f["account"], f["account"]), "deleted": f["account"] not in names} for f in files]


@route("GET", r"/api/inbox")
def api_inbox_list(h, m, q):
    """L'administrateur voit tous les envois ; `?mine=1` (un compte qui a le droit d'envoyer, l'administrateur compris) ne montre que les siens, avec l'espace qu'ils occupent : `used` (octets), et `extensions`, les extensions acceptées (triées), pour que l'interface écarte d'avance les fichiers refusés.
    Chaque fichier porte `dir` : son sous-dossier (chemin relatif, `""` à la racine)."""
    box = h.hub.inbox
    if q.get("mine") != "1":
        require_admin(h)
        h.send_json({"files": inbox_files(h.hub, box.listing())})
        return
    require_sender(h)
    h.send_json({"files": inbox_files(h.hub, box.listing(h.user["id"])), "used": box.used(h.user["id"]), "extensions": sorted(EXTENSIONS)})


@route("GET", r"/api/inbox/([^/]+)/([^/]+)")
def api_inbox_file(h, m, q):
    """Un fichier de la boîte, en pièce jointe (`Range` accepté pour reprendre) : administrateur seulement. `?dir=<chemin relatif>` pour un fichier d'un sous-dossier. Jamais analysé ni servi autrement : type `application/octet-stream`, `nosniff`."""
    require_admin(h)
    path = module_call(InboxError, h.hub.inbox.path_of, m.group(1), m.group(2), q.get("dir"))
    h.send_file(path, "application/octet-stream", {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.name), "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
                track=lambda: h.transfer("download", f"inbox:{m.group(1)}/{q.get('dir') or ''}/{path.name}", path.name))


@route("GET", r"/api/backup")
def api_backup(h, m, q):
    """Une sauvegarde des données du hub (voir backup.py), en pièce jointe `media_hub-<version>-<date>.zip` : administrateur seulement. Construite dans un dossier
    temporaire de HUB_DATA, cohérente même si le serveur écrit, et effacée une fois envoyée : rien ne reste sur le serveur. Elle contient les hachés des
    mots de passe et la clé des sessions : `no-store`, jamais gardée par un cache."""
    require_admin(h)
    work = Path(tempfile.mkdtemp(prefix=".sauvegarde-", dir=h.hub.cfg.data))
    try:
        name = backup.archive_name()
        with h.transfer("backup", "backup", name, leave_empty=False) as sent:          # dès la construction de l'archive : relancer le serveur l'interromprait
            try:
                backup.create(h.hub.db.path, h.hub.cfg.data, work / name)
            except (backup.BackupError, OSError) as e:
                raise HttpError(500, f"sauvegarde impossible : {getattr(e, 'message', e)}")
            h.send_file(work / name, "application/zip", {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name), "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
                        track=lambda: nullcontext(sent))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@route("DELETE", r"/api/inbox/([^/]+)/([^/]+)")
def api_inbox_delete(h, m, q):
    """L'administrateur supprime n'importe quel fichier de la boîte ; un compte qui a le droit d'envoyer, seulement les siens (403 pour ceux des autres, qu'ils existent ou non). `?dir=<chemin relatif>` pour un fichier d'un sous-dossier, qui part avec le dernier fichier qu'il contient."""
    h.discard_body()
    account, name = m.groups()
    if h.user["role"] != "admin":
        require_sender(h)
        if account != h.hub.inbox.folder_of(h.user["id"]):
            raise HttpError(403, "ce fichier n'est pas à toi")
    module_call(InboxError, h.hub.inbox.remove, account, name, q.get("dir"))
    h.send_json({"ok": True})


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


# ---------------------------------------------------------------------------
# Demandes (voir requests.py) : une liste d'attente que l'administrateur lit ; le hub ne télécharge rien
# ---------------------------------------------------------------------------
request_call = partial(module_call, hub_requests.RequestError)


def requests_state(h):
    """Ce que `/api/state` dit des demandes : leur nombre en attente, pour l'administrateur seulement."""
    return {"requests_pending": hub_requests.pending_count(h.hub.db, h.hub.cfg)} if h.user["role"] == "admin" else {}


def requests_ready(h, seen, quality, ratings):
    """Pour l'accueil : les demandes du compte arrivées dans une bibliothèque qu'il voit et qu'il n'a pas encore vues (`ack`). Un film ou une série : sa vignette (`card`, comme celles des rayons) ; un artiste : sa fiche (`artist`, voir requests.arrival), sans vignette. Vide quand il n'en a pas."""
    found = hub_requests.ready(h.hub.db, h.hub.cfg, h.user["id"], h.visible_ids())
    wanted = {place for request, place in found if request["kind"] != hub_requests.ARTIST}
    rows = {row["id"]: row for row in items_by_id(h.hub.db, wanted)} if wanted else {}
    return [requests_ready_entry(request, place, rows, seen, quality, ratings) for request, place in found if request["kind"] == hub_requests.ARTIST or place in rows]


def requests_ready_entry(request, place, rows, seen, quality, ratings):
    """Une demande arrivée pour l'accueil (voir requests_ready) : `place` est l'identifiant de l'élément (`rows` : ses lignes) ou, pour un artiste, sa fiche."""
    if request["kind"] == hub_requests.ARTIST:
        return {"id": request["id"], "kind": request["kind"], "title": request["title"], "year": request["year"], "artist": place, **hub_requests.mbid_fields(request)}
    return {"id": request["id"], "kind": request["kind"], "tmdb": request["tmdb"], "title": request["title"], "year": request["year"], "item": place, "card": with_seen(rows[place], seen, quality, ratings)}


def mark_requests(h, *lists):
    """Les films absents de ces listes (`missing` d'une saga ou d'une personne) reçoivent `request` ({"status", "mine"}) quand quelqu'un les a demandés ; `mine` dit seulement si c'est ce compte. Une lecture des demandes pour toutes les listes, aucune quand elles sont vides."""
    films = [film for films in lists for film in films or ()]
    if not films:
        return
    found = hub_requests.marks(h.hub.db, h.hub.cfg, h.user["id"])
    for film in films:
        if film.get("tmdb") in found:
            film["request"] = dict(found[film["tmdb"]])


def request_failure(h, call, *args):
    """`call(*args)` ; une RequestError devient la réponse, avec ses champs en plus (`item` d'un élément déjà là) : None est alors rendu, la réponse est partie."""
    try:
        return call(*args)
    except hub_requests.RequestError as e:
        if not e.extra:
            raise HttpError(e.status, e.message)
        h.send_json({"error": e.message, **e.extra}, e.status)
        return None


@route("GET", r"/api/requests")
def api_requests(h, m, q):
    """`?mine=1` : les demandes du compte ; sans, toutes les demandes avec leurs demandeurs, pour l'administrateur seulement (403 sinon), `?status=` en garde une sorte."""
    hub = h.hub
    if q.get("mine") == "1":
        h.send_json({"requests": hub_requests.mine(hub.db, hub.cfg, h.user["id"], h.visible_ids())})
        return
    require_admin(h)
    h.send_json({"requests": request_call(hub_requests.everything, hub.db, hub.cfg, h.user["id"], q.get("status"))})


@route("POST", r"/api/requests")
def api_request_create(h, m, q):
    """Le compte demande un film ou une série : `{"kind": "movie" | "show", "tmdb": <entier>, "note": "…", "title": "…", "year": "AAAA"}` (titre et année ne servent que si le serveur ne connaît pas le film),
    ou un artiste : `{"kind": "artist", "name": "…", "note"?: "…"}` (1 à 200 caractères sans caractère de contrôle ; deux noms qui ne diffèrent que par la casse, les accents ou les espaces sont la même demande).
    201 : nouvelle demande ; 200 : le compte rejoint une demande existante, ou l'avait déjà faite. L'administrateur n'a pas de plafond."""
    if not h.user["can_request"]:
        h.discard_body()
    require_requester(h)
    body = strict_body_dict(h)
    hub = h.hub
    limit = None if h.user["role"] == "admin" else hub.cfg.request_max
    describe = partial(hub.finder.details, who=h.user["id"]) if hub.finder.configured() else None           # sans clé TMDB, le titre et l'année du corps, comme avant la recherche
    done = request_failure(h, hub_requests.create, hub.db, hub.cfg, h.user["id"], body, h.visible_ids(), limit, describe)
    if done is None:
        return
    row, new = done
    h.send_json(request_reply(row), 201 if new else 200)
    if wants_posters(row, new, describe):
        fetch_request_posters(hub)


def request_reply(row):
    """La réponse à une demande faite ou rejointe : son identifiant, sa sorte, son état et `mine` ; `tmdb` sauf pour un artiste, qui n'en a pas."""
    return {"id": row["id"], "kind": row["kind"], **({} if row["kind"] == hub_requests.ARTIST else {"tmdb": row["tmdb"]}), "status": row["status"], "mine": True}


def wants_posters(row, new, describe):
    """Faut-il lancer la tâche « Affiches des demandes » : après une demande **nouvelle** et en attente, quand le serveur a une clé TMDB (`describe`), sauf pour un artiste, qui n'a pas d'affiche."""
    return new and row["status"] == "pending" and describe is not None and row["kind"] != hub_requests.ARTIST


def fetch_request_posters(hub):
    """Met en file la tâche « Affiches des demandes », la réponse déjà partie : un échec ici ne touche pas la demande, il est seulement dit au journal."""
    try:
        hub.jobs.request_posters()
    except Exception as e:                  # la demande est faite : rien ne doit la défaire
        print(f"Affiches des demandes : la tâche n'a pas pu être lancée ({e})", file=sys.stderr)


def require_requester(h):
    """403 quand le compte n'a pas le droit « Peut demander »."""
    if not h.user["can_request"]:
        raise HttpError(403, "tu n'as pas le droit de demander des films, des séries ni des artistes")


@route("GET", r"/api/requests/search")
def api_request_search(h, m, q):
    """Cherche dans TMDB un film (`kind=movie`), une série (`kind=show`) ou les deux (sans `kind`) à demander : `q`, 2 à 100 caractères. Au plus 10 résultats par sorte, de la première page de TMDB ;
    chacun donne sa fiche sur le site de TMDB (`url`), dit s'il est déjà demandé (`request`, comme les cases grisées) et, quand il est dans une bibliothèque que le compte voit, son élément (`item`). Garde-fous : voir request_search.py."""
    require_requester(h)
    text = request_call(request_search.search_text, q.get("q"))
    kinds = request_call(request_search.search_kinds, q.get("kind"))
    hub = h.hub
    if not hub.finder.configured():
        raise HttpError(503, request_search.UNCONFIGURED)
    request_call(hub.finder.pace.take, h.user["id"])
    results = request_call(hub.finder.search, text, kinds)
    marks, found = hub_requests.search_marks(hub.db, hub.cfg, h.user["id"], [(r["kind"], r["tmdb"]) for r in results])
    visible = h.visible_ids()
    for result in results:
        key = (result["kind"], result["tmdb"])
        result["url"] = hub_requests.tmdb_url(*key)
        if key in marks:
            result["request"] = marks[key]
        item = hub_requests.item_for(found, *key, visible)
        if item:
            result["item"] = item
    h.send_json({"results": results})


@route("GET", r"/api/requests/artists")
def api_request_artists(h, m, q):
    """Cherche dans MusicBrainz un artiste à demander : `q`, 1 à 200 caractères. Au plus 10 résultats, dans l'ordre de MusicBrainz : `{"results": [{"mbid", "name", "type", "country", "disambiguation", "begin", "end", "url", "request"?, "artist"?}]}` ;
    `url` : sa fiche sur le site de MusicBrainz, `request` : la demande de ce nom (comme les résultats de TMDB), `artist` : l'artiste d'une bibliothèque de musique que le compte voit, quand il y est déjà (la règle du 409). Garde-fous : voir artist_search.py.
    MusicBrainz injoignable, ou la recherche occupée : 502 ou 503 en français, qui disent d'envoyer le nom tel quel (`POST /api/requests` sans `mbid`)."""
    require_requester(h)
    text = request_call(artist_search.search_text, q.get("q"))
    hub = h.hub
    request_call(hub.artist_finder.pace.take, h.user["id"])
    results = request_call(hub.artist_finder.search, text)
    h.send_json({"results": hub_requests.artist_results(hub.db, hub.cfg, h.user["id"], results, h.visible_ids())})


@route("GET", r"/img/request/([0-9]{1,15})")
def request_image(h, m, q):
    """L'affiche d'une demande (HUB_DATA/tmdb, voir request_search.fetch_posters), pour ses demandeurs et l'administrateur ; 404 sans affiche, et pour la demande d'un autre (comme une inconnue)."""
    hub = h.hub
    row = hub.db.one("SELECT r.kind, r.tmdb FROM requests r WHERE r.id=? AND (? OR EXISTS (SELECT 1 FROM request_users u WHERE u.request_id = r.id AND u.user_id=?))",
                     (int(m.group(1)), h.user["role"] == "admin", h.user["id"]))
    if row is None or row["kind"] == hub_requests.ARTIST:              # un artiste n'a pas d'affiche
        raise HttpError(404, "pas d'affiche")
    send_poster(h, q, f"request:{row['kind']}:{row['tmdb']}", hub.cfg.data / "tmdb" / hub_requests.poster_file(row["kind"], row["tmdb"]))


def withdrawn(h, m, call, withdraw):
    """Le corps commun des routes `…/<id>/delete` des demandes et des suggestions : `withdraw(db, compte, id, administrateur ?)` passé par `call` (son erreur devient la réponse), puis `{"ok": true}`."""
    h.body_json()
    call(withdraw, h.hub.db, h.user["id"], int(m.group(1)), h.user["role"] == "admin")
    h.send_json({"ok": True})


@route("POST", r"/api/requests/([0-9]{1,15})/delete")
def api_request_delete(h, m, q):
    """Un compte retire sa demande ; l'administrateur supprime la demande entière. 404 pour une demande inconnue ou qui n'est pas celle du compte."""
    withdrawn(h, m, request_call, hub_requests.withdraw)


@route("POST", r"/api/requests/([0-9]{1,15})/status")
def api_request_status(h, m, q):
    """L'administrateur passe une demande à `added`, `declined` (`reason` : un motif facultatif, 200 caractères au plus) ou la remet en `pending` ; rend la demande comme `GET /api/requests`.
    409 pour remettre en attente une demande dont l'élément est déjà dans la médiathèque (la lecture suivante la repasserait à `added`)."""
    require_admin(h)
    body = strict_body_dict(h)
    rid = int(m.group(1))
    request_call(hub_requests.set_status, h.hub.db, rid, body.get("status"), body.get("reason"), h.hub.cfg.libraries)
    changed = next((r for r in hub_requests.everything(h.hub.db, h.hub.cfg, h.user["id"]) if r["id"] == rid), None)
    if changed is None:                     # supprimée entre le changement et la relecture (une autre requête)
        raise HttpError(404, "demande inconnue")
    h.send_json(changed)


@route("POST", r"/api/requests/([0-9]{1,15})/ack")
def api_request_ack(h, m, q):
    """Le compte a vu que sa demande est arrivée : elle quitte `requests_ready` de son accueil. 404 pour une demande inconnue ou qui n'est pas la sienne."""
    h.body_json()
    request_call(hub_requests.acknowledge, h.hub.db, h.user["id"], int(m.group(1)))
    h.send_json({"ok": True})


# ---------------------------------------------------------------------------
# Suggestions (voir suggestions.py) : idées et remarques sur le hub, entre un compte et l'administrateur
# ---------------------------------------------------------------------------
suggestion_call = partial(module_call, suggestions.SuggestionError)


def suggestions_state(h):
    """Ce que `/api/state` dit des suggestions : leur nombre pas encore traité (`new`), pour l'administrateur seulement."""
    return {"suggestions_new": suggestions.new_count(h.hub.db)} if h.user["role"] == "admin" else {}


@route("GET", r"/api/suggestions")
def api_suggestions(h, m, q):
    """`?mine=1` : les suggestions du compte ; sans, toutes avec leurs auteurs (et le contexte des signalements), pour l'administrateur seulement (403 sinon), `?status=` et `?category=` en gardent une sorte, `?reports=1` les seuls signalements."""
    if q.get("mine") == "1":
        h.send_json({"suggestions": suggestions.mine(h.hub.db, h.user["id"], h.visible_ids())})
        return
    require_admin(h)
    h.send_json({"suggestions": suggestion_call(suggestions.everything, h.hub.db, q.get("status"), q.get("category"), q.get("reports") == "1")})


def report_ident(value, name):
    """Un identifiant du corps d'un signalement : un texte, `""` quand il est absent (None) ; 400 sinon."""
    if value is not None and not isinstance(value, str):
        raise HttpError(400, f"{name} : un identifiant est attendu")
    return value or ""


def report_target(h, body):
    """Le titre qu'un signalement désigne (`body["item"]`, et `body["entry"]` pour un épisode ou une piste) : `{"item", "entry", "subject", "context"}`.
    404 `élément inconnu` pour un élément qui n'existe pas **ou que le compte ne voit pas** (les deux répondent pareil : rien n'est révélé), 400 pour un fichier qui n'est pas dans cet élément (ou qui n'existe pas)."""
    db = h.hub.db
    item_id, entry_id = report_ident(body["item"], "item"), report_ident(body.get("entry"), "entry")
    item, visible = db.one("SELECT * FROM items WHERE id=?", (item_id,)), h.visible_ids()
    if item is None or (visible is not None and item["lib"] not in visible):
        raise HttpError(404, "élément inconnu")
    file = db.one("SELECT title FROM entries WHERE id=? AND item_id=?", (entry_id, item_id)) if entry_id else None
    if entry_id and file is None:
        raise HttpError(400, "ce fichier n'est pas dans cet élément")
    files = [entry_id] if entry_id else [r["id"] for r in db.query("SELECT id FROM entries WHERE item_id=?", (item_id,))]
    return {"item": item_id, "entry": entry_id, "subject": suggestions.subject_of(item["title"], file["title"] if file else ""), "context": h.hub.activity.last_context(h.user["id"], files)}


@route("POST", r"/api/suggestions")
def api_suggestion_create(h, m, q):
    """Le compte propose une amélioration du hub : `{"category": "evolution" | "improvement" | "bug" | "other", "text": "…"}` ; 201 et la suggestion. Tout compte, sans plafond.
    Avec `item` (et `entry`, un fichier de cet élément), c'est un **signalement** : `{"item", "entry"?, "cause": "image" | "sound" | "subtitles" | "playback" | "other", "text"?}`, la catégorie est `bug`, le texte facultatif, et la dernière décision de lecture du compte pour ce fichier est jointe (l'administrateur seul la lit)."""
    body = strict_body_dict(h)
    target = report_target(h, body) if body.get("item") is not None else None
    h.send_json(suggestion_call(suggestions.create, h.hub.db, h.user["id"], body, h.visible_ids(), target), 201)


@route("POST", r"/api/suggestions/([0-9]{1,15})/status")
def api_suggestion_status(h, m, q):
    """L'administrateur donne un état (`new`, `planned`, `done`, `declined`) et, s'il veut, une réponse (`reply`, 500 caractères au plus ; absente, celle d'avant reste) ; rend la suggestion comme `GET /api/suggestions`."""
    require_admin(h)
    body = strict_body_dict(h)
    sid = int(m.group(1))
    suggestion_call(suggestions.set_status, h.hub.db, sid, body.get("status"), body.get("reply"))
    h.send_json(suggestion_call(suggestions.one, h.hub.db, sid))          # supprimée entre-temps : 404, comme une inconnue


@route("POST", r"/api/suggestions/([0-9]{1,15})/delete")
def api_suggestion_delete(h, m, q):
    """L'auteur retire sa suggestion ; l'administrateur supprime n'importe laquelle. 404 pour une suggestion inconnue ou qui n'est pas celle du compte."""
    withdrawn(h, m, suggestion_call, suggestions.withdraw)


def make_server(cfg):
    hub = Hub(cfg)
    handler = type("BoundHandler", (Handler,), {"hub": hub, "routes": ROUTES})
    server = Server((cfg.bind, cfg.port), handler)
    server.hub = hub
    return server
