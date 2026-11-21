"""Les corrections qu'un artiste reçoit dans le hub : nom affiché, genres, résumé, pays, années d'activité et fiche MusicBrainz choisie.

Rien n'est écrit dans la médiathèque, et l'identifiant de l'artiste (tiré de son nom par media_library) ne change jamais : regroupement, albums manquants (`wanted:`) et photo en dépendent. Une correction est gardée dans `kv` sous `artistedit:<bibliothèque>:<id de l'artiste>`, avec les seuls champs corrigés, et appliquée à la lecture : un artiste n'est pas une ligne de `items`, il se calcule à chaque demande (voir app.artist_rows).

Le nom affiché corrigé remplace l'artiste de ses albums (`grp`, `meta.artist`) dans ce que le serveur rend (voir `renamed`) ; la base garde les étiquettes. Limite acceptée : si le nom change dans les étiquettes, l'identifiant change et la correction devient orpheline.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata

from .scan.common import clean_text
from .scan.edits import Invalid, genre_list, required, still_valid, text

FIELDS = ("name", "genres", "summary", "country", "active", "mbid")        # l'ordre où `edited` les rend
SHEET_FIELDS = ("summary", "country", "active")                             # ce qui corrige la fiche de MusicBrainz et Wikipédia
NAME_MAX, SUMMARY_MAX, COUNTRY_MAX, ACTIVE_MAX = 200, 5000, 100, 60
PREFIX = "artistedit:"
MBID_RE = re.compile(r"(?:https?://(?:www\.)?musicbrainz\.org/artist/)?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?", re.IGNORECASE)      # l'identifiant, ou l'adresse de la fiche
LOCK = threading.Lock()          # lire puis réécrire `artistedit:` : deux administrateurs ne s'écrasent pas


def key(lib_id, artist_id):
    return f"{PREFIX}{lib_id}:{artist_id}"


def _name(value):
    return required(value, NAME_MAX, "nom")


def _summary(value):
    """Le résumé : des paragraphes (retours à la ligne gardés), sans autre caractère de contrôle."""
    if not isinstance(value, str):
        raise Invalid("résumé : du texte, sans caractère de contrôle")
    value = value.replace("\r\n", "\n")
    if any(unicodedata.category(c) == "Cc" and c != "\n" for c in value):
        raise Invalid("résumé : du texte, sans caractère de contrôle")
    summary = clean_text(value)
    if len(summary) > SUMMARY_MAX:
        raise Invalid(f"résumé : {SUMMARY_MAX} caractères au plus")
    return summary


def _country(value):
    return text(value, COUNTRY_MAX, "pays")


def _active(value):
    return text(value, ACTIVE_MAX, "années d'activité")


def mbid(value):
    """L'identifiant MusicBrainz d'un artiste, en minuscules, tiré d'un identifiant ou de l'adresse de sa fiche ; Invalid sinon."""
    found = MBID_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not found:
        raise Invalid("mbid : l'identifiant MusicBrainz de l'artiste (8-4-4-4-12 chiffres hexadécimaux) ou l'adresse de sa fiche")
    return found.group(1).lower()


PARSERS = {"name": _name, "genres": genre_list, "summary": _summary, "country": _country, "active": _active, "mbid": mbid}


def parse(body):
    """(corrections {champ: valeur}, retours [champ]) d'une requête : `null` rétablit un champ ; `mbid` ne se rétablit que ainsi (la fiche se choisit par sa route, qui la demande à MusicBrainz). Invalid au moindre champ inconnu ou hors bornes : tout ou rien."""
    if not body:
        raise Invalid("rien à corriger")
    changes, resets = {}, []
    for field, value in body.items():
        if field not in PARSERS:
            raise Invalid("champ inconnu : un artiste se corrige par " + ", ".join(FIELDS))
        if value is None:
            resets.append(field)
        elif field == "mbid":
            raise Invalid("mbid : la fiche MusicBrainz se choisit par /api/artist/<bibliothèque>/<id>/musicbrainz ; seul null la rétablit ici")
        else:
            changes[field] = PARSERS[field](value)
    return changes, resets


def kept(stored):
    """Les corrections gardées dans `kv` qui sont encore valables : un champ abîmé (la base touchée à la main) est ignoré, jamais une erreur."""
    return still_valid(PARSERS, stored)


def listed(corrections):
    """Les champs corrigés, dans l'ordre de FIELDS."""
    return [field for field in FIELDS if field in corrections]


def of_artist(db, lib_id, artist_id):
    return kept(db.kv_get(key(lib_id, artist_id)))


def of_library(db, lib_id):
    """{id d'artiste: corrections} gardées pour une bibliothèque."""
    prefix = f"{PREFIX}{lib_id}:"
    found = {}
    for row in db.query("SELECT key, value FROM kv WHERE key LIKE ? ESCAPE '\\'", (prefix.replace("_", "\\_") + "%",)):         # `_` d'un identifiant de bibliothèque n'est pas un joker
        try:
            corrections = kept(json.loads(row["value"]))
        except ValueError:
            continue
        if corrections:
            found[row["key"][len(prefix):]] = corrections
    return found


def names(db, lib_id):
    """{id d'artiste: nom affiché corrigé} d'une bibliothèque."""
    return {artist: c["name"] for artist, c in of_library(db, lib_id).items() if "name" in c}


def pinned(db, lib_id):
    """Les fiches MusicBrainz choisies à la main dans une bibliothèque : « Fiches artistes » les tient à jour comme celles des albums."""
    return {c["mbid"] for c in of_library(db, lib_id).values() if "mbid" in c}


def save(db, lib_id, artist_id, changes, resets):
    """Garde les corrections de l'artiste : celles de `changes` posées, celles de `resets` retirées. Rend celles qui restent. À appeler sous LOCK."""
    corrections = {**of_artist(db, lib_id, artist_id), **changes}
    for field in resets:
        corrections.pop(field, None)
    if corrections:
        db.kv_set(key(lib_id, artist_id), corrections)
    else:
        db.kv_delete(key(lib_id, artist_id))
    return corrections


def renamed(row, names):
    """La ligne d'un album (une bibliothèque de musique n'a que des albums) avec le nom affiché corrigé de son artiste (`names`, voir plus haut) à la place de celui des étiquettes, dans `grp` et `meta.artist` ; la ligne telle quelle sinon."""
    meta = json.loads(row["meta"] or "{}")
    name = names.get(meta.get("artist_id")) if isinstance(meta, dict) else None
    if not name:
        return row
    meta["artist"] = name
    return {**dict(row), "grp": name, "meta": json.dumps(meta)}


def credit(artist, original, name):
    """Le crédit d'une piste : le nom affiché corrigé quand la piste était créditée du nom des étiquettes, sinon tel quel (« X feat. Y » reste)."""
    return name if name and artist == original else artist
