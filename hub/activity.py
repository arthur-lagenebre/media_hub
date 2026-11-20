"""Ce que le hub fait en ce moment pour qui (lectures, téléchargements, envois, sauvegarde), et la trace des dernières décisions de lecture : de quoi savoir si on peut relancer le serveur sans couper quelqu'un.

Tout est en mémoire, sous un verrou (un fil par connexion) : ni table ni migration, et rien ne survit au redémarrage. L'horloge s'injecte pour les tests.

Un transfert est une ligne par (compte, élément transféré, nature, adresse du client) : la lecture directe d'un film se fait en nombreuses requêtes Range courtes, qui ne font qu'une ligne.
L'adresse sert à séparer deux appareils d'un même compte ; elle n'est jamais rendue (`origin` en dit seulement `local`, `tailscale` ou `internet`, voir origin.py).

Une tentative de téléchargement est autre chose : l'interface tire un identifiant (`dl`) par fichier à télécharger, et le compte demande ensuite où il en est (`attempts_of`). Elle dit si l'envoi est allé jusqu'au dernier octet, ce qu'une ligne ne sait pas, et reste bien plus longtemps.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial


ACTIVE_SECONDS = 60             # une ligne reste tant qu'une connexion est ouverte, puis ce temps après sa dernière activité (le navigateur lit un moment depuis sa mémoire tampon sans rien demander)
MAX_LINES = 500                 # au-delà, une nouvelle ligne remplace la ligne sans connexion la plus ancienne : demander un octet de milliers de fichiers ne gonfle pas le registre
KEEP_SECONDS = 1800             # l'issue d'une tentative reste ce temps après sa dernière activité : un onglet en arrière-plan ne demande qu'une fois par minute
ATTEMPTS_PER_USER = 200         # au-delà, une nouvelle tentative d'un compte remplace sa plus ancienne sans connexion ; au plafond général, la plus ancienne de tous
ATTEMPTS_MAX = 2000
ATTEMPT_RE = re.compile(r"[A-Za-z0-9]{8,32}", re.ASCII)
DECISIONS_KEPT = 100            # les décisions de lecture gardées, les plus récentes
KINDS = ("direct", "audio", "remux", "transcode", "download", "upload", "backup")
PLAYING = ("direct", "audio", "remux", "transcode")
DEVICES = ("pc", "phone", "tablet", "tv")
TITLE_MAX = 200
AGENT_MAX = 60
WHY_ITEMS = 8                   # les raisons d'une décision : au plus ce nombre de phrases, de ce nombre de caractères
WHY_TEXT = 300
CAPS_ITEMS = 12                 # des listes de `caps` : au plus ce nombre de mots, de ce nombre de caractères
CAPS_WORD = 24
CAPS_LISTS = ("video", "audio", "containers")           # ce que lit `probe.plan`, et rien d'autre
CAPS_NUMBERS = {"channels": 32, "height": 8640}
WORD_RE = re.compile(r"[\w.+:-]{1,%d}" % CAPS_WORD, re.ASCII)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("FxiOS", "Firefox"), ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Version", "Safari"), ("VLC", "VLC"))
SYSTEMS = (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("CrKey", "Chromecast"), ("Tizen", "Tizen"), ("Web0S", "webOS"), ("Windows", "Windows"), ("Macintosh", "macOS"), ("Linux", "Linux"))


def clean_text(value, limit):
    """Le texte sans caractère de contrôle, au plus `limit` caractères ; vide quand ce n'est pas un texte."""
    return CONTROL_RE.sub("", value).strip()[:limit] if isinstance(value, str) else ""


def short_agent(text):
    """L'agent utilisateur en quelques mots (« Firefox 120 · Windows »), borné ; un client qui n'est pas un navigateur reste tel quel, tronqué."""
    text = clean_text(text, 400)
    browser = next((f"{name} {found.group(1)}" for token, name in BROWSERS if (found := re.search(rf"\b{token}/(\d+)", text))), "")
    system = next((name for token, name in SYSTEMS if token in text), "")
    return " · ".join(part for part in (browser, system) if part)[:AGENT_MAX] or text[:AGENT_MAX]


def attempt_id(value):
    """L'identifiant de tentative tel que l'interface le tire (8 à 32 lettres ou chiffres ASCII), sinon vide : un `dl` mal formé est ignoré, jamais corrigé."""
    return value if isinstance(value, str) and ATTEMPT_RE.fullmatch(value) else ""


def clean_device(value):
    """`pc`, `phone`, `tablet` ou `tv` ; vide pour toute autre valeur : le client la déclare, on ne garde que ce qu'on connaît."""
    return value if isinstance(value, str) and value in DEVICES else ""


def clean_caps(caps):
    """Seulement les clés que lit la décision de lecture : `video`, `audio`, `containers` (des mots courts, en nombre borné), `channels`, `height` (des entiers bornés) et `hdr` (un booléen).
    Ce que le client envoie est écarté dès qu'il n'a pas le bon type ; jamais recopié tel quel."""
    caps = caps if isinstance(caps, dict) else {}
    out = {}
    for key in CAPS_LISTS:
        words = caps_words(caps.get(key))
        if words:
            out[key] = words
    for key, top in CAPS_NUMBERS.items():
        number = caps_number(caps.get(key), top)
        if number is not None:
            out[key] = number
    if isinstance(caps.get("hdr"), bool):
        out["hdr"] = caps["hdr"]
    return out


def caps_words(words):
    """Les mots courts d'une liste de `caps`, en nombre borné ; rien si ce n'est pas une liste."""
    if not isinstance(words, list):
        return []
    return [word for word in words if isinstance(word, str) and WORD_RE.fullmatch(word)][:CAPS_ITEMS]


def caps_number(number, top):
    """Un entier de `caps` entre 0 et `top` (un flottant entier compte), sinon None ; un booléen n'est pas un nombre."""
    if isinstance(number, float) and number.is_integer():
        number = int(number)
    if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= top:
        return number
    return None


def clean_why(why):
    """Les raisons de `probe.plan` (une liste de phrases), en nombre et en longueur bornés ; ce qui n'est pas un texte est écarté."""
    reasons = (clean_text(reason, WHY_TEXT) for reason in why) if isinstance(why, list) else ()
    return [reason for reason in reasons if reason][:WHY_ITEMS]


@dataclass
class Line:
    user: str                   # l'identifiant du compte
    name: str                   # son nom, tel qu'il était au dernier transfert
    ref: str                    # ce qui est transféré : un fichier de la médiathèque, ou de quoi reconnaître un envoi, un fichier de la boîte, une sauvegarde
    address: str                # jamais rendue
    kind: str
    title: str
    library: str
    origin: str
    agent: str
    started: float
    last: float
    bytes: int = 0
    connections: int = 0

    def public(self):
        return {"user": self.name, "title": self.title, "library": self.library, "kind": self.kind, "origin": self.origin, "agent": self.agent,
                "started": int(self.started), "last": int(self.last), "bytes": self.bytes, "connections": self.connections}


@dataclass
class Decision:
    at: float
    user: str
    name: str
    address: str                # jamais rendue : elle ne sert qu'à rapprocher la décision d'un transfert
    agent: str
    device: str
    entry: str
    title: str
    caps: dict
    mode: str
    why: list

    def public(self):
        return {"at": int(self.at), "user": self.name, "device": self.device, "agent": self.agent, "entry": self.entry, "title": self.title, "caps": self.caps, "mode": self.mode, "why": self.why}


@dataclass
class Attempt:
    size: int                   # la taille du fichier à la dernière requête
    last: float
    bytes: int = 0              # jusqu'où le fichier est parti, toutes requêtes confondues (une reprise par Range repart de son début)
    connections: int = 0
    done: bool = False          # une requête est allée jusqu'au dernier octet sans erreur ; ne se défait plus

    def public(self):
        state = "done" if self.done else "running" if self.connections else "stopped"
        return {"state": state, "bytes": self.bytes, "size": self.size}


class Activity:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.lines = {}                                         # (compte, ref, nature, adresse) -> Line
        self.attempts = {}                                      # (compte, dl) -> Attempt
        self.decisions = deque(maxlen=DECISIONS_KEPT)

    # --- Les transferts ---------------------------------------------------
    @contextmanager
    def transfer(self, user, name, ref, kind, address, title="", library="", agent="", leave_empty=True, *, origin):
        """Un transfert ouvert le temps du bloc, qui rend la fonction où compter les octets envoyés ; il se referme sur toute sortie, une coupure du client comprise.
        Un transfert qui échoue avant le moindre octet ne laisse pas de ligne (un envoi refusé n'a rien envoyé). `origin` (obligatoire) : celle de la requête (`origin_of` avec son `X-Forwarded-For`) ; elle ne se déduit pas de `address`, qui peut venir de cet en-tête. `leave_empty=False` : un travail ouvert avant l'envoi (ffmpeg qui démarre, archive à construire) est listé pendant qu'il dure, mais ne laisse rien s'il n'a rien envoyé, même sans erreur (un HEAD, un 416)."""
        key = (user, ref, kind, address)
        with self.lock:
            now = self.clock()
            self._prune(now)
            line = self.lines.get(key)
            if line is None:
                self._make_room()
                line = self.lines[key] = Line(user, name, ref, address, kind, clean_text(title, TITLE_MAX), clean_text(library, TITLE_MAX), origin, short_agent(agent), now, now)
            line.name, line.connections, line.last = name, line.connections + 1, now
        failed = False
        try:
            yield partial(self._count, line)
        except BaseException:
            failed = True
            raise
        finally:
            self._close(key, line, failed or not leave_empty)

    def _count(self, line, size):
        with self.lock:
            line.bytes += size
            line.last = self.clock()

    def _close(self, key, line, drop_if_empty):
        with self.lock:
            now = self.clock()
            line.connections -= 1
            line.last = now
            if drop_if_empty and not line.bytes and not line.connections and self.lines.get(key) is line:
                del self.lines[key]
            self._prune(now)

    def touch(self, user, ref):
        """Le lecteur du compte a donné sa position : les lignes de ce compte et de ce fichier sont toujours regardées. Ne crée jamais de ligne."""
        with self.lock:
            now = self.clock()
            self._prune(now)
            for line in self.lines.values():
                if line.user == user and line.ref == ref:
                    line.last = now

    def _prune(self, now):
        """Retire les lignes sans connexion ouverte dont la dernière activité est trop ancienne (verrou pris) : le registre ne grandit pas."""
        for key in [key for key, line in self.lines.items() if not line.connections and now - line.last >= ACTIVE_SECONDS]:
            del self.lines[key]

    def _make_room(self):
        """Au plafond, retire la ligne sans connexion la plus ancienne (verrou pris). Une connexion ouverte n'est jamais retirée : la ligne dit un envoi réel."""
        idle = [key for key, line in self.lines.items() if not line.connections] if len(self.lines) >= MAX_LINES else []
        if idle:
            del self.lines[min(idle, key=lambda key: self.lines[key].last)]

    # --- Les tentatives de téléchargement ---------------------------------
    @contextmanager
    def attempt(self, user, dl, start, size):
        """Une requête de la tentative `dl` du compte, qui envoie le fichier de `size` octets à partir de `start` ; rend la fonction où compter les octets envoyés.
        Elle est finie (`done`) quand cette requête atteint le dernier octet sans erreur. Au plafond, sans place à libérer, la tentative n'est pas suivie : le téléchargement se fait quand même."""
        key = (user, dl)
        with self.lock:
            now = self.clock()
            self._prune_attempts(now)
            found = self.attempts.get(key) or self._new_attempt(key, size, now)
            if found:
                found.size, found.last, found.connections = size, now, found.connections + 1
        if found is None:
            yield _ignore
            return
        position = [start]
        failed = False
        try:
            yield partial(self._reach, found, position)
        except BaseException:
            failed = True
            raise
        finally:
            with self.lock:
                found.connections -= 1
                found.last = self.clock()
                found.done = found.done or (not failed and position[0] >= size)

    def _reach(self, found, position, count):
        with self.lock:
            position[0] += count
            found.bytes = max(found.bytes, position[0])
            found.last = self.clock()

    def _new_attempt(self, key, size, now):
        """Une tentative neuve, après avoir fait la place sous les deux plafonds (verrou pris) ; None quand il n'y a pas de place sans retirer une connexion ouverte."""
        for keys, top in ((lambda: [k for k in self.attempts if k[0] == key[0]], ATTEMPTS_PER_USER), (lambda: list(self.attempts), ATTEMPTS_MAX)):
            if not self._room(keys(), top):         # relues après chaque retrait : celle du compte peut libérer la place commune
                return None
        self.attempts[key] = Attempt(size, now)
        return self.attempts[key]

    def _room(self, keys, top):
        """Vrai quand `keys` est sous `top`, au besoin en retirant la plus ancienne sans connexion."""
        return len(keys) < top or self._drop_oldest_idle(keys)

    def _drop_oldest_idle(self, keys):
        idle = [k for k in keys if not self.attempts[k].connections]
        if idle:
            del self.attempts[min(idle, key=lambda k: self.attempts[k].last)]
        return bool(idle)

    def _prune_attempts(self, now):
        for key in [key for key, found in self.attempts.items() if not found.connections and now - found.last >= KEEP_SECONDS]:
            del self.attempts[key]

    def attempts_of(self, user, ids):
        """{dl: {"state", "bytes", "size"}} pour les tentatives demandées de ce compte seulement ; `{"state": "unknown"}` pour une qu'il n'a pas (jamais vue, oubliée, ou d'un autre compte)."""
        with self.lock:
            self._prune_attempts(self.clock())
            found = {dl: self.attempts.get((user, dl)) for dl in ids}
            return {dl: attempt.public() if attempt else {"state": "unknown"} for dl, attempt in found.items()}

    # --- Les décisions ----------------------------------------------------
    def decide(self, user, name, address, agent, device, entry, title, caps, mode, why):
        """Garde une décision de lecture. Ce que le client a dit (`device`, `caps`, agent) est nettoyé ici, jamais gardé tel quel."""
        with self.lock:
            self.decisions.append(Decision(self.clock(), user, name, address, short_agent(agent), clean_device(device), entry, clean_text(title, TITLE_MAX), clean_caps(caps),
                                           clean_text(mode, 20), clean_why(why)))

    def last_context(self, user, entries):
        """Ce que la dernière décision de lecture de ce compte pour l'un de ces fichiers (`entries`) dit à un signalement : la date, le mode, les raisons, l'appareil et `caps` ; None si le registre n'en a plus.
        Ni adresse, ni agent, ni nom : seulement ce qui aide à comprendre ce qui a été joué (borné par WHY_* et CAPS_*)."""
        wanted = set(entries)
        with self.lock:
            found = next((d for d in reversed(self.decisions) if d.user == user and d.entry in wanted), None)
        return None if found is None else {"at": int(found.at), "mode": found.mode, "why": found.why, "device": found.device, "caps": found.caps}

    # --- La lecture -------------------------------------------------------
    def report(self):
        """{"counts", "transfers", "decisions"} : les lignes actives (avec `device` quand une décision du même compte, de la même entrée et du même client le donne), les décisions de la plus récente à la plus ancienne."""
        with self.lock:
            self._prune(self.clock())
            lines, decisions = list(self.lines.values()), list(reversed(self.decisions))
            devices = {}
            for d in decisions:
                devices.setdefault((d.user, d.entry, d.address), d.device)      # la plus récente d'abord : c'est elle qui compte
            return {"counts": counts(lines), "transfers": [self._shown(line, devices) for line in lines], "decisions": [d.public() for d in decisions]}

    @staticmethod
    def _shown(line, devices):
        out = line.public()
        device = devices.get((line.user, line.ref, line.address))
        if device:
            out["device"] = device
        return out


def _ignore(size):
    """Le compteur d'une tentative qui n'est pas suivie."""


def counts(lines):
    kinds = [line.kind for line in lines]
    return {"playing": sum(kind in PLAYING for kind in kinds), "downloads": kinds.count("download"), "uploads": kinds.count("upload"), "remux": kinds.count("remux"),
            "transcodes": kinds.count("transcode"), "backups": kinds.count("backup")}
