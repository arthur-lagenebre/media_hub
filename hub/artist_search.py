"""Chercher dans MusicBrainz un artiste à demander (`GET /api/requests/artists`), comme la recherche TMDB des films (request_search.py).

Le serveur est seul à parler à MusicBrainz : il lui passe le texte cherché (rien d'autre : ni le nom, ni l'identifiant du compte ; `User-Agent` du hub, voir enrich.MB_USER_AGENT, pas de clé : MusicBrainz n'en demande pas)
et rend du texte (nom, type, pays, années d'activité, de quoi départager les homonymes), sans image. La recherche n'est qu'une aide : une demande d'artiste se fait aussi par le nom tapé tel quel
(`POST /api/requests`, sans `mbid`), si MusicBrainz ne répond pas, est occupé, ou ne connaît pas l'artiste ; les messages d'échec le disent.

Garde-fous : un seul essai de 10 s par appel ; une recherche à la fois dans le processus (la suivante attend son tour au plus 5 s, puis 503) et une requête par seconde au plus (MusicBrainz refuse au-delà : c'est sa règle,
voir libraries.music.musicbrainz.MIN_INTERVAL) ; trente recherches par minute et par compte (429) ; une mémoire de 200 réponses gardées 10 minutes, sans cache disque de plus que celui du client (`musicbrainz-cache`, une semaine).
Le message d'un échec de MusicBrainz ne part ni dans une réponse ni dans le journal : il peut porter l'adresse demandée, donc le texte cherché.
"""

from __future__ import annotations

import sys
import threading
import time

from . import enrich
from . import requests as hub_requests
from .request_search import Memo, Pace, bounded_text
from .requests import RequestError

TIMEOUT, ATTEMPTS = 10, 1           # secondes ; un essai : la personne attend la réponse, elle réessaiera, ou enverra le nom tel quel
INTERVAL = 1.1                      # secondes entre deux départs de requête (celle de media_library : à 1 s, MusicBrainz a déjà répondu 503)
WAIT = 5                            # secondes qu'une recherche attend son tour avant d'être refusée
RESULTS = 10                        # résultats au plus
MAX_TEXT = 200                      # longueur du texte cherché, blancs de bord retirés (celle d'un nom d'artiste)
FIELD = 200                         # longueur des textes d'un résultat (type, pays, précision)

BAD_TEXT = f"texte à chercher : de 1 à {MAX_TEXT} caractères"
UNREACHABLE = "MusicBrainz ne répond pas : tu peux envoyer le nom tel quel"
BUSY = "la recherche d'artistes est occupée : tu peux envoyer le nom tel quel"


def search_text(value):
    """Le texte cherché : de 1 à 200 caractères (voir request_search.bounded_text)."""
    return bounded_text(value, 1, MAX_TEXT, BAD_TEXT)


def _text(value):
    return value.strip()[:FIELD] if isinstance(value, str) else ""


def _year(value):
    """L'année d'une date de MusicBrainz (`1993`, `1993-05`, `1993-05-21`), "" quand ce n'en est pas une."""
    return value[:4] if isinstance(value, str) and hub_requests.YEAR_RE.fullmatch(value[:4]) else ""


def _result(raw):
    """Un artiste de la recherche de MusicBrainz tel que la route le rend, ou None : un résultat sans identifiant ni nom lisibles, ou dont le nom ne serait pas accepté par `POST /api/requests`
    (trop long, caractère de contrôle, « Various Artists » : voir requests._artist_name) est écarté, pour qu'un résultat proposé soit toujours un résultat qu'on peut demander."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not hub_requests.MBID_RE.fullmatch(raw["id"]) or not isinstance(raw.get("name"), str):
        return None
    try:
        name, _ = hub_requests._artist_name(raw["name"])
    except RequestError:
        return None
    span = raw.get("life-span") if isinstance(raw.get("life-span"), dict) else {}
    return {"mbid": raw["id"].lower(), "name": name, "type": _text(raw.get("type")), "country": _text(raw.get("country")), "disambiguation": _text(raw.get("disambiguation")),
            "begin": _year(span.get("begin")), "end": _year(span.get("end"))}


def shaped(found):
    """Les résultats d'une recherche de MusicBrainz tels que la route les rend (10 au plus, dans l'ordre de MusicBrainz, du plus au moins pertinent) : `mbid`, `name`, `type`, `country` (le code de MusicBrainz : `FR`),
    `disambiguation`, `begin` et `end` (les années d'activité, `""` quand MusicBrainz ne les donne pas)."""
    kept = (_result(raw) for raw in found) if isinstance(found, list) else ()
    return [result for result in kept if result][:RESULTS]


class Finder:
    """Ce que le serveur demande à MusicBrainz pour les demandes (une seule instance par serveur : `Hub.artist_finder`). `make(cfg, **options)` rend le client (par défaut `enrich.musicbrainz`, cherché à chaque appel : les tests en donnent un simulé) ;
    `clock` et `sleep` ne changent que dans les tests."""

    def __init__(self, cfg, make=None, clock=time.monotonic, sleep=time.sleep):
        self.cfg, self.make, self.clock, self.sleep = cfg, make, clock, sleep
        self.turn = threading.Lock()
        self.last = None            # le départ de la dernière requête, pour tout le serveur (le client de chaque appel est neuf, il ne peut pas tenir la cadence)
        self.pace = Pace(clock)
        self.memo = Memo(clock)

    def _client(self):
        return (self.make or enrich.musicbrainz)(self.cfg, timeout=TIMEOUT, attempts=ATTEMPTS)

    def _wait_for_the_interval(self):
        if self.last is not None and (wait := self.last + INTERVAL - self.clock()) > 0:
            self.sleep(wait)
        self.last = self.clock()

    def _asked(self, text):
        """Les résultats que MusicBrainz donne pour `text` ; une place seulement (503 après WAIT secondes), à une requête par INTERVAL. Un échec de MusicBrainz devient une RequestError 502 sans rien de son message ; une ligne au journal dit seulement la sorte d'échec."""
        if not self.turn.acquire(timeout=WAIT):
            raise RequestError(503, BUSY)
        try:
            self._wait_for_the_interval()
            return shaped(self._client().search_artists(text, RESULTS))
        except enrich.MusicBrainzError as e:
            failure = f"HTTP {e.code}" if e.code else "pas de réponse"
        except (OSError, ValueError):
            failure = "pas de réponse"
        except Exception as e:                  # imprévu (RecursionError d'un JSON trop imbriqué, réponse d'une autre forme) : jamais son message
            failure = f"réponse inattendue ({type(e).__name__})"
        finally:
            self.turn.release()
        print(f"MusicBrainz (recherche des demandes) : {failure}", file=sys.stderr)
        raise RequestError(502, UNREACHABLE) from None

    def search(self, text):
        """Les résultats de `text` (voir `shaped`), sans ce que la base en dit ; gardés en mémoire 10 minutes (un échec ne l'est pas)."""
        key = ("artists", text.casefold())
        found = self.memo.get(key)
        if found is None:
            found = self._asked(text)
            self.memo.put(key, found)
        return [dict(result) for result in found]
