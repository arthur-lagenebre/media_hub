"""Les sagas que l'on compose soi-même : `sagas.json`, sous HUB_DATA.

Une saga y est une liste ordonnée de films, désignés par leur identifiant TMDB (celui que l'étiquette du fichier porte : il survit aux renommages et à une base refaite) :

    {"sagas": [{"name": "Trilogie Cornetto", "films": [747, 4638, 49051]}]}

Elles s'ajoutent à la saga que dit l'étiquette du fichier (la collection TMDB) : un film peut être dans plusieurs sagas. L'ordre de la liste est l'ordre de la saga, et la place d'un film dans la liste est son numéro, qu'on possède ou non les films qui le précèdent.

Le fichier est relu dès qu'il change ; absent ou illisible, il ne donne aucune saga.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TMDB_RE = re.compile(r"^(?:movie/)?([0-9]+)$", re.IGNORECASE)


def film_id(value):
    """L'identifiant TMDB d'un film, en nombre, d'après une étiquette (« movie/27205 ») ou un nombre ; None sinon."""
    if isinstance(value, bool):
        return None
    found = TMDB_RE.match(str(value if value is not None else "").strip())
    return int(found.group(1)) if found else None


def parse(text):
    """[(nom, [identifiants TMDB])] d'après le contenu du fichier. Une saga sans nom, un nom déjà pris ou un identifiant illisible sont écartés sans bruit : le reste du fichier compte."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    listed = data.get("sagas") if isinstance(data, dict) else None
    sagas, names = [], set()
    for saga in listed if isinstance(listed, list) else []:
        if not isinstance(saga, dict):
            continue
        name = " ".join(str(saga.get("name") or "").split())
        films = []
        for value in saga.get("films") if isinstance(saga.get("films"), list) else []:
            ident = film_id(value)
            if ident is not None and ident not in films:
                films.append(ident)
        if name and name not in names:
            names.add(name)
            sagas.append((name, films))
    return sagas


class Sagas:
    def __init__(self, path):
        self.path = Path(path)
        self._read = (None, [])          # (taille et date du fichier, sagas lues)

    def all(self):
        try:
            st = self.path.stat()
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp != self._read[0]:
            try:
                sagas = parse(self.path.read_text(encoding="utf-8")) if stamp else []
            except (OSError, UnicodeDecodeError):
                sagas = []
            self._read = (stamp, sagas)
        return self._read[1]

    def of_movies(self, movies):
        """{nom: [(film, rang)]} dans l'ordre du fichier, pour les sagas dont au moins un film est dans la bibliothèque. `movies` : lignes de la table items ; le rang est la place du film dans la liste (de 1 à N), films absents compris."""
        by_id = {}
        for row in movies:
            try:
                ident = film_id(json.loads(row["meta"] or "{}").get("tmdb"))
            except (ValueError, AttributeError):
                continue
            if ident is not None:
                by_id.setdefault(ident, row)
        out = {}
        for name, films in self.all():
            owned = [(by_id[ident], rank) for rank, ident in enumerate(films, 1) if ident in by_id]
            if owned:
                out[name] = owned
        return out
