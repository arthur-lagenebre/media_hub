"""Les films qui manquent à une saga : ce que TMDB dit d'une saga et que la médiathèque n'a pas.

L'enrichissement (enrich.enrich_sagas) interroge TMDB et garde le résultat dans `kv`, sous `sagainfo`, avec les affiches sous HUB_DATA/tmdb : un scan ne l'efface pas. Ces films ne sont pas des items (aucune liste ne les compte) : la page d'une saga les montre grisés, à leur place, et ils servent à numéroter les films qu'on a (1 et 3 quand le 2 manque).

    {"films": {"<identifiant TMDB>": {"title", "year", "date", "poster": "<fichier>" ou ""}},
     "collections": {"<nom de la saga de l'étiquette>": {"id": <collection TMDB>, "films": [identifiants dans l'ordre de sortie], "at": <horodatage>}}}

Une saga du fichier `sagas.json` n'a pas besoin de collection : sa liste dit déjà ce qu'elle contient ; l'enrichissement ne fait que décrire ses films absents.
"""

from __future__ import annotations

from . import sagas as sagafile
from . import urls

KEY = "sagainfo"
STALE = 30 * 24 * 3600          # au-delà, une collection est redemandée à TMDB (un nouveau film y est peut-être entré)


def load(db):
    info = db.kv_get(KEY) or {}
    return {"films": info.get("films") or {}, "collections": info.get("collections") or {}}


def save(db, info):
    db.kv_set(KEY, info)


def poster_name(ident):
    return f"film-{ident}.jpg"


def entry(ident, rank, info):
    """Un film manquant tel que l'API le donne : son identifiant TMDB, son titre, son année, son rang dans la saga et son affiche (URL vide s'il n'en a pas)."""
    film = info["films"][str(ident)]
    return {"tmdb": ident, "title": film.get("title") or "", "year": film.get("year") or "", "rank": rank, "cover": urls.missing_film(ident) if film.get("poster") else ""}


def tagged(name, members, info, ignored):
    """(rangs, manquants) d'une saga de l'étiquette : `members` sont ses films présents (lignes de la table items).

    Les rangs ({id de l'élément: place dans la collection TMDB}) numérotent et ordonnent les films qu'on a ; les manquants sont les autres films de la collection, sauf ceux qu'on a demandé de ne pas voir (`ignored`). Rien de tout cela quand TMDB n'a pas été interrogé pour cette saga, ou quand elle ne concorde pas avec nos films (un film sans identifiant ou hors de la collection : le nom de la saga est alors peut-être celui de deux sagas) : (None, []), et la saga garde son ordre par date de sortie.
    """
    collection = info["collections"].get(name)
    if not collection:
        return None, []
    owned = sagafile.by_tmdb(members)
    ranks, absent = {}, []
    for rank, ident in enumerate(collection["films"], 1):
        row = owned.get(ident)
        if row is not None:
            ranks[row["id"]] = rank
        elif ident not in ignored and str(ident) in info["films"]:
            absent.append({**entry(ident, rank, info), "ignorable": True})         # la collection TMDB le compte, pas nous : on peut le cacher
    if len(ranks) != len(members):
        return None, []
    return ranks, absent


def listed(ids, owned, info):
    """Les films qu'une saga du fichier liste, qu'on n'a pas (`owned` : les identifiants TMDB qu'on a) et que l'enrichissement a décrits ; leur rang est leur place dans la liste. Un film qu'on a écrit soi-même n'est jamais « ignoré »."""
    return [entry(ident, rank, info) for rank, ident in enumerate(ids, 1) if ident not in owned and str(ident) in info["films"]]
