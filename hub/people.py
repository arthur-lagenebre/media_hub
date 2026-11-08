"""Les personnes des films et des séries : qui a réalisé ou joué dans quoi.

L'index se lit dans `meta["directors"]` (liste de chaînes) et `meta["actors"]` (liste de {name, role}, ou de chaînes pour une fiche ancienne) des items `movie` et `show`.
Il est construit au premier appel puis gardé en mémoire (`Database.derived`) jusqu'à la prochaine écriture des items : un scan ou un enrichissement le renouvelle.

Deux personnes de même nom n'en font qu'une : voir « homonymes » dans docs/api.md. Les étiquettes des films portent l'identifiant TMDB de chaque personne (`meta["person_ids"]`, que l'enrichissement TMDB d'une série écrit aussi pour son casting) : une personne en a un quand les étiquettes des bibliothèques qu'un compte voit n'en disent qu'un (`tmdb_id`) ; avec deux ou plus, ce sont des homonymes et elle n'en a aucun.
"""

from __future__ import annotations

import json
from collections import Counter

from libraries.common.text import fold

ROLES = ("director", "actor")
TYPES = ("movie", "show")
MAX_NAME = 200
MAX_HITS = 8                    # personnes rendues par la recherche


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


def meta_of(row):
    """Le `meta` d'une ligne de la table items ; None s'il n'est pas lisible ou n'est pas un objet."""
    try:
        meta = json.loads(row["meta"])
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def ids_in(meta):
    """{clé: identifiant TMDB} des personnes d'un item (`meta["person_ids"]` : écrit par le scan des films, voir scan/video.py, ou par l'enrichissement TMDB d'une série, voir enrich.cast_with_ids). Ce qui n'est pas un nombre entier positif est ignoré."""
    listed = meta.get("person_ids")
    return {key_of(name): ident for name, ident in (listed.items() if isinstance(listed, dict) else ())
            if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0 and key_of(name)}


def ids_seen(person, libs):
    """Counter(identifiant TMDB -> nombre d'items) de cette personne dans ces bibliothèques."""
    total = Counter()
    for lib in libs:
        total.update(person["ids"].get(lib, ()))
    return total


def tmdb_id(person, libs):
    """L'identifiant TMDB d'une personne de l'index, d'après les étiquettes des films de ces bibliothèques : celui qu'elles portent, quand elles n'en disent qu'un. None si aucune ne l'écrit, si elles en disent plusieurs (deux personnes de même nom), ou pour un nom inconnu (`person` None)."""
    found = ids_seen(person, libs) if person else ()
    return next(iter(found)) if len(found) == 1 else None


def build(db):
    """{clé: {"names": {bibliothèque: Counter(graphie -> nombre d'items)}, "titles": {bibliothèque: nombre d'items}, "director": [id d'item], "actor": [id d'item], "ids": {bibliothèque: Counter(identifiant TMDB -> nombre d'items)}}}.
    Les graphies, les titres et les identifiants sont comptés par bibliothèque : on ne sait ainsi dire que ceux des bibliothèques qu'un compte voit."""
    index = {}
    for row in db.query("SELECT id, lib, meta FROM items WHERE type IN (?, ?)", TYPES):
        meta = meta_of(row)
        if meta is None:
            continue
        spelled, here, ids = set(), set(), ids_in(meta)
        for role, name in names_in(meta):
            key = key_of(name)
            if not key:
                continue
            person = index.setdefault(key, {"names": {}, "titles": {}, "director": [], "actor": [], "ids": {}})
            if key not in here:             # un titre compte une fois pour une personne, quel que soit son rôle
                here.add(key)
                person["titles"][row["lib"]] = person["titles"].get(row["lib"], 0) + 1
                if key in ids:
                    person["ids"].setdefault(row["lib"], Counter())[ids[key]] += 1
            if row["id"] not in person[role]:           # une personne citée deux fois dans le même item n'y compte qu'une fois
                person[role].append(row["id"])
            if (key, name) not in spelled:
                spelled.add((key, name))
                person["names"].setdefault(row["lib"], Counter())[name] += 1
    return index


def index(db):
    return db.derived("people", lambda: build(db))


def billed(meta, top):
    """Les clés des personnes qui comptent dans un film : ses réalisateurs, et ses `top` premiers acteurs (l'ordre du casting dans les étiquettes)."""
    keys, actors = [], 0
    for role, name in names_in(meta):
        if role == "actor":
            actors += 1
            if actors > top:
                continue
        keys.append(key_of(name))
    return [key for key in keys if key]


def bulk_ids(db, libs, top):
    """[identifiant TMDB] de tout le monde dans ces bibliothèques `libs` (identifiants de bibliothèques) : les réalisateurs et les `top` premiers acteurs de chaque film ou série, quand leur nom désigne **une seule** personne (voir `tmdb_id`). Chacun une fois, dans l'ordre des éléments (par titre) : les réalisateurs puis les acteurs de chacun.
    Une série n'a que des acteurs, et seulement ceux dont l'enrichissement TMDB a gardé l'identifiant (`person_ids`, voir enrich.cast_with_ids) : une série pas encore (re)enrichie ne donne personne."""
    found, known = {}, index(db)
    for lib in libs:
        for row in db.query("SELECT meta FROM items WHERE lib=? AND type IN ('movie', 'show') ORDER BY sort", (lib,)):
            meta = meta_of(row)
            for key in billed(meta, top) if meta is not None else ():
                ident = tmdb_id(known.get(key), libs)
                if ident is not None:
                    found[ident] = None
    return list(found)


def find(db, name):
    """L'entrée de l'index pour ce nom (égalité exacte des clés, jamais une sous-chaîne), ou None."""
    return index(db).get(key_of(name))


def spelling(person, libs):
    """La graphie la plus fréquente dans ces bibliothèques (à égalité, la première dans l'ordre des caractères : « PENELOPE » avant « Penelope »), ou None si aucune n'y figure."""
    total = Counter()
    for lib in libs:
        total.update(person["names"].get(lib, ()))
    return min(total, key=lambda name: (-total[name], name)) if total else None


def search(db, needle, libs, limit=MAX_HITS):
    """[(nom, nombre de titres)] des personnes dont la clé contient `needle` (déjà passé par `key_of`), et qui ont au moins un titre dans ces bibliothèques.
    Le nom exact d'abord, puis ceux qui commencent par le texte, puis les autres ; à rang égal, le plus de titres, puis l'ordre des clés."""
    found = []
    for key, person in index(db).items():
        if needle not in key:
            continue
        titles = sum(person["titles"].get(lib, 0) for lib in libs)
        if titles:
            found.append((0 if key == needle else 1 if key.startswith(needle) else 2, -titles, key, person))
    found.sort(key=lambda hit: hit[:3])
    return [(spelling(person, libs), -negative) for _, negative, _, person in found[:limit]]
