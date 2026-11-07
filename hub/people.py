"""Les personnes des films et des séries : qui a réalisé ou joué dans quoi.

L'index se lit dans `meta["directors"]` (liste de chaînes) et `meta["actors"]` (liste de {name, role}, ou de chaînes pour une fiche ancienne) des items `movie` et `show`.
Il est construit au premier appel puis gardé en mémoire (`Database.derived`) jusqu'à la prochaine écriture des items : un scan ou un enrichissement le renouvelle.

Deux personnes de même nom n'en font qu'une (aucun identifiant ne les distingue dans les étiquettes) : voir « homonymes » dans docs/api.md.
"""

from __future__ import annotations

import json
from collections import Counter

from libraries.common.text import fold

ROLES = ("director", "actor")
TYPES = ("movie", "show")
MAX_NAME = 200


def key_of(name):
    """La clé d'une personne : son nom sans accents, sans casse, espaces ramenés à un seul. '' si ce n'est pas un nom."""
    return fold(" ".join(name.split())) if isinstance(name, str) else ""


def names_in(meta):
    """[(rôle, nom écrit)] d'un item : réalisateurs (chaînes) puis acteurs ({name, role} ou chaîne). Ce qui n'est pas un nom lisible est ignoré."""
    found = []
    directors, actors = meta.get("directors"), meta.get("actors")
    for name in directors if isinstance(directors, list) else ():            # une chaîne ou un dict à la place d'une liste : on ne la découpe pas en lettres
        found.append(("director", name))
    for actor in actors if isinstance(actors, list) else ():
        found.append(("actor", actor.get("name") if isinstance(actor, dict) else actor))
    return [(role, " ".join(name.split())) for role, name in found if isinstance(name, str) and name.strip()]


def build(db):
    """{clé: {"names": {bibliothèque: Counter(graphie -> nombre d'items)}, "director": [id d'item], "actor": [id d'item]}}.
    Les graphies sont comptées par bibliothèque : on ne sait ainsi dire que celles des bibliothèques qu'un compte voit."""
    index = {}
    for row in db.query("SELECT id, lib, meta FROM items WHERE type IN (?, ?)", TYPES):
        try:
            meta = json.loads(row["meta"])
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        spelled = set()
        for role, name in names_in(meta):
            key = key_of(name)
            if not key:
                continue
            person = index.setdefault(key, {"names": {}, "director": [], "actor": []})
            if row["id"] not in person[role]:           # une personne citée deux fois dans le même item n'y compte qu'une fois
                person[role].append(row["id"])
            if (key, name) not in spelled:
                spelled.add((key, name))
                person["names"].setdefault(row["lib"], Counter())[name] += 1
    return index


def index(db):
    return db.derived("people", lambda: build(db))


def find(db, name):
    """L'entrée de l'index pour ce nom (égalité exacte des clés, jamais une sous-chaîne), ou None."""
    return index(db).get(key_of(name))


def spelling(person, libs):
    """La graphie la plus fréquente dans ces bibliothèques (à égalité, la première dans l'ordre des caractères : « PENELOPE » avant « Penelope »), ou None si aucune n'y figure."""
    total = Counter()
    for lib in libs:
        total.update(person["names"].get(lib, ()))
    return min(total, key=lambda name: (-total[name], name)) if total else None
