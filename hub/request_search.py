"""Chercher dans TMDB un film ou une série à demander, et les affiches des demandes.

Le serveur est seul à parler à TMDB : le client lui donne le texte cherché, le serveur le passe à TMDB (rien d'autre : ni le nom, ni l'identifiant du compte) et rend du texte
(titre, année, date de sortie, résumé), sans image. L'affiche d'un titre ne vient qu'après qu'il a été demandé : la tâche « Affiches des demandes » (`fetch_posters`) la range
dans HUB_DATA/tmdb, sous le nom que l'enrichissement lui donnerait (`film-<id>.jpg` pour un film, `<id>.jpg` pour une série), et ne télécharge rien pour ce qui est déjà là.

Garde-fous : un seul essai de 10 s par appel ; deux appels au plus en même temps dans le processus (au-delà : 503 tout de suite, sans attendre) ; trente recherches par minute
et par compte (429) ; aucun cache sur le disque (`tmdb-cache` n'est pas touché), seulement une mémoire de 200 réponses gardées 10 minutes. Le message d'un échec de TMDB
(qui peut contenir l'URL, donc la clé) ne part jamais dans une réponse ni dans le journal.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import quote

from libraries.video.tmdb import Tmdb, TmdbAuthError, TmdbError

from .requests import RequestError
from . import requests as hub_requests

LANGUAGE = "fr-FR"
TIMEOUT, ATTEMPTS = 10, 1           # secondes ; un essai : la personne attend la réponse, elle réessaiera elle-même
SLOTS = 2                           # appels à TMDB en même temps, pour tout le serveur
PER_KIND = 10                       # résultats par sorte (la première page de TMDB en a 20)
OVERVIEW = 300                      # caractères du résumé
MIN_TEXT, MAX_TEXT = 2, 100         # longueur du texte cherché, blancs de bord retirés
POSTER_SIZE = "w342"                # comme les affiches des films manquants (enrich.download_poster)
POSTERS_PER_RUN = 50                # affiches demandées au plus par passage de la tâche
SEARCHES = {"movie": ("search/movie", "title", "release_date"), "show": ("search/tv", "name", "first_air_date")}
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
IMAGE_RE = re.compile(r"/[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)")      # un `poster_path` de TMDB ; tout autre texte n'est pas suivi

BAD_TEXT = f"texte à chercher : de {MIN_TEXT} à {MAX_TEXT} caractères"
UNCONFIGURED = "la recherche n'est pas configurée sur ce serveur"
BUSY = "le serveur est occupé, réessaie dans un instant"
UNREACHABLE = "TMDB ne répond pas, réessaie plus tard"
UNKNOWN = "titre inconnu sur TMDB"
POSTERS_LABEL = "Affiches des demandes"


def client(cfg):
    """Le client TMDB de la recherche : un essai, 10 s, sans cache disque."""
    return Tmdb(cfg.tmdb_key, LANGUAGE, user_agent="media_hub/1.0", timeout=TIMEOUT, attempts=ATTEMPTS, cache=None)


def without_key(text, key):
    """`text` sans la clé TMDB `key` (remplacée par « … ») : le message d'une exception du client peut porter l'URL demandée, donc la clé."""
    text = str(text)
    return text.replace(key, "…") if key else text


def search_text(value):
    """Le texte cherché, blancs de bord retirés ; 400 s'il a moins de 2 caractères utiles (ni contrôle, ni format, ni blanc, ni séparateur : catégories Unicode C* et Z*)
    ou plus de 100 caractères en tout."""
    text = (value or "").strip()
    useful = sum(not unicodedata.category(c).startswith(("C", "Z")) for c in text)
    if useful < MIN_TEXT or len(text) > MAX_TEXT:
        raise RequestError(400, BAD_TEXT)
    return text


def search_kinds(value):
    """Les sortes cherchées : `movie`, `show`, ou les deux sans `kind`."""
    if value is None:
        return hub_requests.KINDS
    return (hub_requests._kind(value),)


def _date(value):
    return value if isinstance(value, str) and DATE_RE.fullmatch(value) else ""


def _text(value):
    return value if isinstance(value, str) else ""


def shaped(kind, data):
    """Les résultats d'une page de recherche TMDB tels que la route les rend (10 au plus) : un résultat sans identifiant lisible, ou marqué adulte, est écarté."""
    _, title_key, date_key = SEARCHES[kind]
    rows = data.get("results") if isinstance(data, dict) else None
    out = []
    for raw in rows if isinstance(rows, list) else ():
        if not isinstance(raw, dict) or raw.get("adult") is True or not _is_id(raw.get("id")):
            continue
        when = _date(raw.get(date_key))
        out.append({"kind": kind, "tmdb": raw["id"], "title": _text(raw.get(title_key)), "year": when[:4], "date": when, "overview": _text(raw.get("overview"))[:OVERVIEW]})
        if len(out) == PER_KIND:
            break
    return out


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 1 << 53


def described(kind, data):
    """(titre, année) d'une fiche TMDB (`movie/<id>`, `tv/<id>`) ; 404 quand elle n'en est pas une (ou qu'elle est marquée adulte)."""
    _, title_key, date_key = SEARCHES[kind]
    if not isinstance(data, dict) or data.get("adult") is True or not _text(data.get(title_key)).strip():
        raise RequestError(404, UNKNOWN)
    return data[title_key].strip()[:hub_requests.MAX_TEXT], _date(data.get(date_key))[:4]


def _asked(call):
    """`call()` ; un échec de TMDB devient une RequestError sans rien de son message (il peut contenir l'URL, donc la clé) : 404 pour une fiche inconnue, 502 pour le reste.
    Une ligne au journal dit seulement la sorte d'échec."""
    try:
        return call()
    except TmdbError as e:
        if getattr(e.__cause__, "code", None) == 404:
            raise RequestError(404, UNKNOWN) from None
        failure = "pas de réponse"
    except TmdbAuthError:
        failure = "clé refusée"
    except (OSError, ValueError):
        failure = "pas de réponse"
    except Exception as e:                  # imprévu (RecursionError d'un JSON trop imbriqué, erreur du client) : jamais son message, qui peut porter l'URL
        failure = f"réponse inattendue ({type(e).__name__})"
    print(f"TMDB (recherche des demandes) : {failure}", file=sys.stderr)
    raise RequestError(502, UNREACHABLE) from None


class Pace:
    """Les recherches de chaque compte, en mémoire : au plus LIMIT en WINDOW secondes, 429 au-delà (avec le délai). Chaque recherche compte, même celle que la mémoire sert.
    Au-delà de SWEEP comptes suivis, ceux dont la fenêtre est passée sont oubliés. Rien n'est gardé après un redémarrage."""
    LIMIT, WINDOW, SWEEP = 30, 60, 1024

    def __init__(self, clock=time.monotonic):
        self._clock, self._calls, self._lock = clock, {}, threading.Lock()

    def _recent(self, key, now):
        kept = [t for t in self._calls.get(key, ()) if now - t < self.WINDOW]
        if kept:
            self._calls[key] = kept
        else:
            self._calls.pop(key, None)
        return kept

    def take(self, key):
        """Compte une recherche du compte `key` ; RequestError 429 quand il en a déjà fait LIMIT dans la fenêtre."""
        with self._lock:
            now = self._clock()
            if len(self._calls) > self.SWEEP:
                for other in list(self._calls):
                    self._recent(other, now)
            kept = self._recent(key, now)
            if len(kept) >= self.LIMIT:
                raise RequestError(429, f"trop de recherches : réessaie dans {int(self.WINDOW - (now - kept[0])) + 1} s")
            self._calls[key] = kept + [now]


class Memo:
    """Les réponses de TMDB gardées en mémoire : SIZE au plus (la plus ancienne part la première), TTL secondes chacune."""
    SIZE, TTL = 200, 600

    def __init__(self, clock=time.monotonic):
        self._clock, self._kept, self._lock = clock, OrderedDict(), threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._kept.get(key)
            if hit is None:
                return None
            if self._clock() - hit[0] >= self.TTL:
                del self._kept[key]
                return None
            return hit[1]

    def put(self, key, value):
        with self._lock:
            self._kept[key] = (self._clock(), value)
            self._kept.move_to_end(key)
            while len(self._kept) > self.SIZE:
                self._kept.popitem(last=False)


class Finder:
    """Ce que le serveur demande à TMDB pour les demandes (une seule instance par serveur : `Hub.finder`) : les recherches, et la fiche d'un titre qu'on demande.
    `make(cfg)` rend le client (les tests en donnent un simulé)."""

    def __init__(self, cfg, make=client, clock=time.monotonic):
        self.cfg, self.make = cfg, make
        self.slots = threading.Semaphore(SLOTS)
        self.pace = Pace(clock)
        self.memo = Memo(clock)

    def configured(self):
        return bool(self.cfg.tmdb_key)

    @contextmanager
    def _slot(self):
        """Une des deux places d'appel à TMDB ; 503 tout de suite quand elles sont prises."""
        if not self.slots.acquire(blocking=False):
            raise RequestError(503, BUSY)
        try:
            yield
        finally:
            self.slots.release()

    def search(self, text, kinds):
        """Les résultats de `text` pour ces sortes (les films d'abord), sans ce que la base en dit : voir `shaped`. Une place, et un appel par sorte que la mémoire n'a pas."""
        found = {kind: self.memo.get(("search", kind, text.casefold())) for kind in kinds}
        todo = [kind for kind in kinds if found[kind] is None]
        if todo:
            with self._slot():
                tmdb = self.make(self.cfg)
                for kind in todo:
                    endpoint = f"{SEARCHES[kind][0]}?query={quote(text, safe='')}"
                    found[kind] = shaped(kind, _asked(lambda: tmdb.get(endpoint)))
                    self.memo.put(("search", kind, text.casefold()), found[kind])
        return [dict(result) for kind in kinds for result in found[kind]]

    def details(self, kind, tmdb_id, who=None):
        """(titre, année) du film ou de la série `tmdb_id` selon TMDB ; 404 s'il n'existe pas, 502 si TMDB ne répond pas, 503 si les places sont prises.
        `who` : le compte qui crée la demande ; l'appel compte dans sa cadence (429 au-delà), comme une recherche."""
        if who is not None:
            self.pace.take(who)
        key = ("details", kind, tmdb_id)
        hit = self.memo.get(key)
        if hit is None:
            with self._slot():
                data = _asked(lambda: self.make(self.cfg).get(hub_requests.tmdb_path(kind, tmdb_id)))
            hit = described(kind, data)
            self.memo.put(key, hit)
        return hit


# ---------------------------------------------------------------------------
# La tâche « Affiches des demandes »
# ---------------------------------------------------------------------------
def _poster(tmdb, folder, row, log):
    """Télécharge l'affiche de la demande `row` ; vrai si elle est arrivée, None si TMDB n'en a pas. Écrite d'un bloc : une écriture coupée ne laisse pas une affiche tronquée."""
    data = tmdb.get(hub_requests.tmdb_path(row["kind"], row["tmdb"]))
    path = data.get("poster_path") if isinstance(data, dict) else None
    if not isinstance(path, str) or not IMAGE_RE.fullmatch(path):
        log(f"  sans affiche sur TMDB : {row['title']}")
        return None
    name = hub_requests.poster_file(row["kind"], row["tmdb"])
    partial = folder / (name + ".tmp")
    try:
        tmdb.save_image(path, POSTER_SIZE, partial)
        os.replace(partial, folder / name)
    finally:
        partial.unlink(missing_ok=True)
    return True


def _poster_outcome(tmdb, folder, row, log):
    """Ce que devient l'affiche de `row` : "saved", "without" (TMDB n'en a pas) ou "failed" ; une clé refusée arrête la tâche."""
    try:
        return "saved" if _poster(tmdb, folder, row, log) else "without"
    except TmdbAuthError:
        raise RuntimeError("clé TMDB refusée par l'API") from None
    except Exception as e:              # RecursionError d'un JSON trop imbriqué compris : seule cette affiche échoue, et rien du message (il peut porter l'URL, donc la clé)
        log(f"  échec : {row['title']} ({type(e).__name__})")
        return "failed"


def fetch_posters(cfg, db, log, make=client):
    """Les affiches des demandes (sauf refusées) qui n'en ont pas et dont l'élément n'est dans aucune bibliothèque, depuis TMDB, dans HUB_DATA/tmdb. Un échec n'arrête que son affiche ;
    une clé refusée arrête la tâche. Rend {saved, failed, without} (`without` : TMDB n'en a pas)."""
    counts = {"saved": 0, "failed": 0, "without": 0}
    if not cfg.tmdb_key:
        log("Pas de clé TMDB : aucune affiche demandée.")
        return counts
    folder = cfg.data / "tmdb"
    folder.mkdir(parents=True, exist_ok=True)
    rows = db.query("SELECT kind, tmdb, title FROM requests WHERE status != 'declined' AND kind != ? ORDER BY created DESC, id DESC", (hub_requests.ARTIST,))        # un artiste n'a pas d'affiche
    found = hub_requests.owned(db, cfg.libraries) if rows else {}
    todo = [r for r in rows if (r["kind"], r["tmdb"]) not in found and not (folder / hub_requests.poster_file(r["kind"], r["tmdb"])).is_file()]
    tmdb = make(cfg)
    for row in todo[:POSTERS_PER_RUN]:
        counts[_poster_outcome(tmdb, folder, row, log)] += 1
    if len(todo) > POSTERS_PER_RUN:
        log(f"{len(todo) - POSTERS_PER_RUN} affiche(s) laissée(s) pour la prochaine fois.")
    log(f"{counts['saved']} affiche(s) téléchargée(s), {counts['without']} sans affiche sur TMDB, {counts['failed']} échec(s).")
    return counts
