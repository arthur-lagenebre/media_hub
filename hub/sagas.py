"""Les sagas que l'on compose soi-même : `sagas.json`, sous HUB_DATA.

Une saga y est une liste ordonnée de films, désignés par leur identifiant TMDB (celui que l'étiquette du fichier porte : il survit aux renommages et à une base refaite) :

    {"sagas": [{"name": "Trilogie Cornetto", "films": [747, 4638, 49051]}], "ignore": [1771]}

`ignore` (facultatif) : les films que l'enrichissement TMDB ne doit pas montrer comme manquants dans la saga de leur étiquette (un spin-off, un court-métrage que la collection TMDB compte et pas toi). Il ne touche pas aux films qu'une saga ci-dessus liste elle-même : les avoir écrits dit assez qu'on les veut.

Elles s'ajoutent à la saga que dit l'étiquette du fichier (la collection TMDB) : un film peut être dans plusieurs sagas. L'ordre de la liste est l'ordre de la saga, et la place d'un film dans la liste est son numéro, qu'on possède ou non les films qui le précèdent.

À la lecture, le fichier est tolérant : relu dès qu'il change ; absent ou illisible, il ne donne aucune saga ; ce qui n'y est pas lisible est écarté sans bruit. À l'écriture (l'éditeur), tout est vérifié et refusé avec un message.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

TMDB_RE = re.compile(r"^(?:movie/)?([0-9]{1,12})$", re.IGNORECASE)         # borné : int() refuse les nombres de plus de 4300 chiffres
MAX_NAME = 80
MAX_SAGAS = 200
MAX_FILMS = 2000
MAX_IGNORED = 5000


class SagaError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def film_id(value):
    """L'identifiant TMDB d'un film, en nombre, d'après une étiquette (« movie/27205 ») ou un nombre ; None sinon."""
    if isinstance(value, bool):
        return None
    found = TMDB_RE.match(str(value if value is not None else "").strip())
    return int(found.group(1)) if found else None


def clean_name(name):
    return " ".join(str(name if name is not None else "").split())


def _listed(text, key):
    """La liste `key` du fichier ; vide si le fichier est illisible ou n'a pas cette liste."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return []
    listed = data.get(key) if isinstance(data, dict) else None
    return listed if isinstance(listed, list) else []


def _readable_ids(values):
    """Les identifiants TMDB lisibles de `values`, sans doublon ; le reste est écarté."""
    ids = []
    for value in values:
        ident = film_id(value)
        if ident is not None and ident not in ids:
            ids.append(ident)
    return ids


def parse(text):
    """[(nom, [identifiants TMDB])] d'après le contenu du fichier. Une saga sans nom, un nom déjà pris ou un identifiant illisible sont écartés sans bruit : le reste du fichier compte."""
    sagas, names = [], set()
    for saga in _listed(text, "sagas"):
        if not isinstance(saga, dict):
            continue
        name = clean_name(saga.get("name"))
        films = _readable_ids(saga.get("films") if isinstance(saga.get("films"), list) else [])
        if name and name.casefold() not in names:          # comme `clean` : deux noms qui ne diffèrent que par la casse sont le même
            names.add(name.casefold())
            sagas.append((name, films))
    return sagas


def parse_ignore(text):
    """[identifiants TMDB] de `ignore`, sans doublon ; illisible, il est vide."""
    return _readable_ids(_listed(text, "ignore"))


def damaged(text):
    """Le texte est-il un sagas.json qu'on ne sait pas relire ? Vide, il ne l'est pas (rien à perdre) ; sinon il doit être un objet JSON dont `sagas` et `ignore`, s'ils y sont, sont des listes. Écrire par-dessus détruirait ce que quelqu'un a mis là, on refuse donc de le faire (voir Sagas._writable)."""
    if not text.strip():
        return False
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return True
    return not isinstance(data, dict) or any(key in data and not isinstance(data[key], list) for key in ("sagas", "ignore"))


def _checked_name(saga, names):
    """Le nom d'une saga envoyée par l'éditeur, retenu dans `names` ; SagaError s'il manque, est trop long ou déjà pris (à la casse près)."""
    if not isinstance(saga, dict):
        raise SagaError(400, "une saga est un objet {name, films}")
    name = clean_name(saga.get("name"))
    if not name:
        raise SagaError(400, "donne un nom à la saga")
    if len(name) > MAX_NAME:
        raise SagaError(400, f"nom trop long (au plus {MAX_NAME} caractères) : {name[:20]}…")
    if name.casefold() in names:
        raise SagaError(400, f"deux sagas s'appellent « {name} »")
    names.add(name.casefold())
    return name


def _checked_ids(name, values):
    """Les identifiants TMDB des films d'une saga envoyée par l'éditeur, sans doublon ; SagaError au premier qui ne se lit pas."""
    if not isinstance(values, list):
        raise SagaError(400, f"« {name} » : la liste des films est attendue")
    if len(values) > MAX_FILMS:
        raise SagaError(400, f"« {name} » : {MAX_FILMS} films au plus")
    films = []
    for value in values:
        ident = film_id(value)
        if ident is None:
            raise SagaError(400, f"« {name} » : identifiant TMDB illisible ({value!r})")
        if ident not in films:
            films.append(ident)
    return films


def clean(listed):
    """[(nom, [identifiants TMDB])] d'après ce que l'éditeur envoie, ou SagaError : ici rien n'est écarté en silence, on ne perd pas une saga sans le dire."""
    if not isinstance(listed, list):
        raise SagaError(400, "la liste des sagas est attendue")
    if len(listed) > MAX_SAGAS:
        raise SagaError(400, f"{MAX_SAGAS} sagas au plus")
    sagas, names = [], set()
    for saga in listed:
        name = _checked_name(saga, names)
        sagas.append((name, _checked_ids(name, saga.get("films", []))))
    return sagas


def tmdb_of(row):
    """L'identifiant TMDB d'une ligne de la table items (l'étiquette du fichier), ou None."""
    try:
        return film_id(json.loads(row["meta"] or "{}").get("tmdb"))
    except (ValueError, AttributeError):
        return None


def by_tmdb(movies):
    """{identifiant TMDB: ligne de la table items} pour les films qui portent cet identifiant (le premier gagne)."""
    found = {}
    for row in movies:
        ident = tmdb_of(row)
        if ident is not None:
            found.setdefault(ident, row)
    return found


class Sagas:
    def __init__(self, path):
        self.path = Path(path)
        self._read = (None, [], "", [], False)      # (taille et date du fichier, sagas lues, empreinte du contenu, films ignorés, fichier illisible)
        self._lock = threading.Lock()        # une écriture à la fois : lire, changer, écrire

    def _load(self):
        try:
            st = self.path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp != self._read[0]:
            try:
                raw = self.path.read_bytes() if stamp else b""
                text = raw.decode("utf-8")
                self._read = (stamp, parse(text), hashlib.sha1(raw).hexdigest()[:12] if stamp else "", parse_ignore(text), damaged(text))
            except (OSError, UnicodeDecodeError):
                self._read = (stamp, [], "", [], True)
        return self._read

    def all(self):
        return self._load()[1]

    def ignored(self):
        """[identifiants TMDB] des films qu'on ne veut pas voir comme manquants."""
        return self._load()[3]

    def damaged(self):
        """Vrai quand le fichier existe mais qu'on ne sait pas le relire : on ne l'écrase pas, il faut le corriger (ou le supprimer)."""
        return self._load()[4]

    def version(self):
        """Ce qui change quand le fichier change : l'éditeur la rend à l'enregistrement, et le serveur refuse d'écraser une saga qu'un autre a modifiée entre-temps. Vide quand il n'y a pas de fichier."""
        return self._load()[2]

    def of_movies(self, movies):
        """{nom: [(film, rang)]} dans l'ordre du fichier, pour les sagas dont au moins un film est dans la bibliothèque. `movies` : lignes de la table items ; le rang est la place du film dans la liste (de 1 à N), films absents compris."""
        owned = by_tmdb(movies)
        out = {}
        for name, films in self.all():
            here = [(owned[ident], rank) for rank, ident in enumerate(films, 1) if ident in owned]
            if here:
                out[name] = here
        return out

    def save(self, sagas, version=None):
        """Remplace toutes les sagas par `sagas` (déjà vérifiées par `clean`). `version` : celle que l'éditeur avait lue ; si le fichier a changé depuis, 409."""
        with self._lock:
            self._check(version)
            self._writable()
            self._write(sagas, self.ignored())

    def add(self, name, ids):
        """Ajoute des films à la fin de la saga `name` (créée si elle n'existe pas ; un nom qui ne diffère que par la casse est la même saga), sans doublon. Rend le nom de la saga."""
        name = clean_name(name)
        if not name:
            raise SagaError(400, "donne un nom à la saga")
        if len(name) > MAX_NAME:
            raise SagaError(400, f"nom trop long (au plus {MAX_NAME} caractères)")
        with self._lock:
            self._writable()
            sagas = [(n, list(films)) for n, films in self.all()]
            found = next((s for s in sagas if s[0].casefold() == name.casefold()), None)
            if found is None:
                if len(sagas) >= MAX_SAGAS:
                    raise SagaError(400, f"{MAX_SAGAS} sagas au plus")
                found = (name, [])
                sagas.append(found)
            for ident in ids:
                if ident not in found[1]:
                    found[1].append(ident)
            if len(found[1]) > MAX_FILMS:
                raise SagaError(400, f"« {found[0]} » : {MAX_FILMS} films au plus")
            self._write(sagas, self.ignored())
            return found[0]

    def ignore(self, ident, on=True):
        """Cache (ou rend à nouveau visible) un film manquant ; sans effet s'il l'est déjà ou ne l'est pas."""
        with self._lock:
            self._writable()
            ignored = list(self.ignored())
            if on == (ident in ignored):
                return
            if on:
                if len(ignored) >= MAX_IGNORED:
                    raise SagaError(400, f"{MAX_IGNORED} films cachés au plus")
                ignored.append(ident)
            else:
                ignored.remove(ident)
            self._write([(name, list(films)) for name, films in self.all()], ignored)

    def _writable(self):
        if self.damaged():
            raise SagaError(409, f"{self.path.name} n'est pas lisible (JSON invalide ?) : corrige-le, ou supprime-le, avant de modifier les sagas ici")

    def _check(self, version):
        if version is not None and version != self.version():
            raise SagaError(409, "les sagas ont changé depuis que tu les as ouvertes : recharge la page")

    def _write(self, sagas, ignored):
        data = {"sagas": [{"name": name, "films": films} for name, films in sagas]}
        if ignored:
            data["ignore"] = ignored
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8", newline="\n")
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)      # d'un bloc : un arrêt en cours d'écriture ne laisse pas un fichier tronqué
                    break
                except PermissionError:             # Windows : le fichier est ouvert en lecture à cet instant
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise SagaError(500, f"impossible d'écrire {self.path.name} : {e.strerror or e}")
        self._read = (None, [], "", [], False)
