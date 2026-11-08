"""Les films qui manquent à une saga : ce que TMDB dit d'une saga et que la médiathèque n'a pas.

L'enrichissement (enrich.enrich_sagas) interroge TMDB et garde le résultat dans `kv`, sous `sagainfo`, avec les affiches sous HUB_DATA/tmdb : un scan ne l'efface pas. Ces films ne sont pas des items (aucune liste ne les compte) : la page d'une saga les montre grisés, à leur place, et ils servent à numéroter les films qu'on a (1 et 3 quand le 2 manque).

    {"films": {"<identifiant TMDB>": {"title", "year", "date", "poster": "<fichier>" ou ""}},
     "collections": {"<nom de la saga de l'étiquette>": {"id": <collection TMDB>, "films": [identifiants dans l'ordre de sortie], "at": <horodatage>}}}

Une saga du fichier `sagas.json` n'a pas besoin de collection : sa liste dit déjà ce qu'elle contient ; l'enrichissement ne fait que décrire ses films absents.

Les films qui manquent à une personne (enrich.enrich_person) sont gardés à part, sous `personinfo:<identifiant TMDB de la personne>`, avec leurs affiches au même endroit que celles des sagas (`of_person` dit lesquels on n'a pas).
"""

from __future__ import annotations

from datetime import date

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


def today():
    return date.today().isoformat()


def released(film, now=None):
    """Le film de la fiche `film` est-il sorti ? Une date passée (ou du jour) : oui ; une date à venir, ou pas de date (un film encore à l'état de projet) : non. TMDB range les deux dans la collection, mais ce qui n'existe pas encore ne manque à personne."""
    when = (film.get("date") or "")[:10]
    return bool(when) and when <= (now or today())


def film_entry(ident, rank, film):
    """Un film manquant tel que l'API le donne : son identifiant TMDB, son titre, son année, son rang et son affiche (URL vide s'il n'en a pas). `film` : ce que TMDB en dit (titre, année, affiche)."""
    return {"tmdb": ident, "title": film.get("title") or "", "year": film.get("year") or "", "rank": rank, "cover": urls.missing_film(ident) if film.get("poster") else ""}


def entry(ident, rank, info):
    """Le film `ident` de `info["films"]` tel que l'API le donne (voir `film_entry`) ; son rang est sa place dans la saga."""
    return film_entry(ident, rank, info["films"][str(ident)])


def of_person(films, owned):
    """Les films de la fiche d'une personne (`films` : voir enrich.enrich_person, du plus récent au plus ancien) que la médiathèque n'a pas (`owned` : les identifiants TMDB qu'elle a), tels que l'API les donne : comme ceux d'une saga, avec en plus `role` (« director » ou « actor ») ; le rang est la place dans la liste, de 1 à N."""
    absent = [film for film in films if film["tmdb"] not in owned]
    return [{**film_entry(film["tmdb"], rank, film), "role": film["role"]} for rank, film in enumerate(absent, 1)]


def tagged(name, members, info, ignored):
    """(rangs, manquants) d'une saga de l'étiquette : `members` sont ses films présents (lignes de la table items).

    Les rangs ({id de l'élément: place dans la collection TMDB}) numérotent et ordonnent les films qu'on a (deux versions du même film ont le même rang) ; les manquants sont les autres films de la collection **déjà sortis**, sauf ceux qu'on a demandé de ne pas voir (`ignored`). Un film annoncé garde sa place dans la collection (il est décrit : il apparaît le jour de sa sortie, sans rien relancer) mais ne s'affiche pas. Rien de tout cela quand TMDB n'a pas été interrogé pour cette saga, ou quand elle ne concorde pas avec nos films (un film sans identifiant ou hors de la collection : le nom de la saga est alors peut-être celui de deux sagas) : (None, []), et la saga garde son ordre par date de sortie.
    """
    collection = info["collections"].get(name)
    if not collection:
        return None, []
    versions = {}
    for row in members:
        versions.setdefault(sagafile.tmdb_of(row), []).append(row)                  # deux versions d'un même film ont le même identifiant : elles partagent sa place
    ranks, absent = {}, []
    for rank, ident in enumerate(collection["films"], 1):
        if ident in versions:
            ranks.update({row["id"]: rank for row in versions[ident]})
        elif ident not in ignored and str(ident) in info["films"] and released(info["films"][str(ident)]):
            absent.append({**entry(ident, rank, info), "ignorable": True})         # la collection TMDB le compte, pas nous : on peut le cacher
    if len(ranks) != len(members):
        return None, []
    return ranks, absent


def listed(ids, owned, info):
    """Les films qu'une saga du fichier liste, qu'on n'a pas (`owned` : les identifiants TMDB qu'on a) et que l'enrichissement a décrits ; leur rang est leur place dans la liste. Un film qu'on a écrit soi-même n'est jamais « ignoré ». Un film pas encore sorti y figure aussi, marqué `coming` : il est grisé comme les autres, mais il ne manque à personne."""
    return [{**entry(ident, rank, info), **({} if released(info["films"][str(ident)]) else {"coming": True})}
            for rank, ident in enumerate(ids, 1) if ident not in owned and str(ident) in info["films"]]
