"""Ce que le hub fait en ce moment pour qui (lectures, téléchargements, envois, sauvegarde), et la trace des dernières décisions de lecture : de quoi savoir si on peut relancer le serveur sans couper quelqu'un.

Tout est en mémoire, sous un verrou (un fil par connexion) : ni table ni migration, et rien ne survit au redémarrage. L'horloge s'injecte pour les tests.

Un transfert est une ligne par (compte, élément transféré, nature, adresse du client) : la lecture directe d'un film se fait en nombreuses requêtes Range courtes, qui ne font qu'une ligne.
L'adresse sert à séparer deux appareils d'un même compte ; elle n'est jamais rendue (`origin` en dit seulement `local` ou `remote`).
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial

ACTIVE_SECONDS = 60             # une ligne reste tant qu'une connexion est ouverte, puis ce temps après sa dernière activité (le navigateur lit un moment depuis sa mémoire tampon sans rien demander)
MAX_LINES = 500                 # au-delà, une nouvelle ligne remplace la ligne sans connexion la plus ancienne : demander un octet de milliers de fichiers ne gonfle pas le registre
DECISIONS_KEPT = 100            # les décisions de lecture gardées, les plus récentes
KINDS = ("direct", "audio", "remux", "transcode", "download", "upload", "backup")
PLAYING = ("direct", "audio", "remux", "transcode")
DEVICES = ("pc", "phone", "tablet", "tv")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")    # les appareils du réseau privé Tailscale comptent comme locaux
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
BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("FxiOS", "Firefox"), ("Firefox", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Version", "Safari"))
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


def origin_of(address):
    """`local` pour la boucle locale, les adresses privées (réseau de la maison, IPv6 locale) et Tailscale, `remote` pour le reste, adresse illisible comprise. `address` est celui de `client_ip` : une adresse, ou, pour l'IPv6 derrière le proxy, un réseau /64.
    Tailscale en IPv6 (`fd7a:115c:a1e0::/48`) est déjà un réseau local unique ; en IPv4, sa plage n'est pas privée pour Python, d'où `TAILSCALE_V4`."""
    try:
        found = ipaddress.ip_network(str(address).partition("%")[0], strict=False).network_address
    except ValueError:
        return "remote"
    found = getattr(found, "ipv4_mapped", None) or found
    tailscale = found.version == 4 and found in TAILSCALE_V4
    return "local" if found.is_loopback or found.is_private or tailscale else "remote"


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


class Activity:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.lines = {}                                         # (compte, ref, nature, adresse) -> Line
        self.decisions = deque(maxlen=DECISIONS_KEPT)

    # --- Les transferts ---------------------------------------------------
    @contextmanager
    def transfer(self, user, name, ref, kind, address, title="", library="", agent="", leave_empty=True):
        """Un transfert ouvert le temps du bloc, qui rend la fonction où compter les octets envoyés ; il se referme sur toute sortie, une coupure du client comprise.
        Un transfert qui échoue avant le moindre octet ne laisse pas de ligne (un envoi refusé n'a rien envoyé). `leave_empty=False` : un travail ouvert avant l'envoi (ffmpeg qui démarre, archive à construire) est listé pendant qu'il dure, mais ne laisse rien s'il n'a rien envoyé, même sans erreur (un HEAD, un 416)."""
        key = (user, ref, kind, address)
        with self.lock:
            now = self.clock()
            self._prune(now)
            line = self.lines.get(key)
            if line is None:
                self._make_room()
                line = self.lines[key] = Line(user, name, ref, address, kind, clean_text(title, TITLE_MAX), clean_text(library, TITLE_MAX), origin_of(address), short_agent(agent), now, now)
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

    # --- Les décisions ----------------------------------------------------
    def decide(self, user, name, address, agent, device, entry, title, caps, mode, why):
        """Garde une décision de lecture. Ce que le client a dit (`device`, `caps`, agent) est nettoyé ici, jamais gardé tel quel."""
        with self.lock:
            self.decisions.append(Decision(self.clock(), user, name, address, short_agent(agent), clean_device(device), entry, clean_text(title, TITLE_MAX), clean_caps(caps),
                                           clean_text(mode, 20), clean_why(why)))

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


def counts(lines):
    kinds = [line.kind for line in lines]
    return {"playing": sum(kind in PLAYING for kind in kinds), "downloads": kinds.count("download"), "uploads": kinds.count("upload"), "remux": kinds.count("remux"),
            "transcodes": kinds.count("transcode"), "backups": kinds.count("backup")}
