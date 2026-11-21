"""Les corrections qu'un album reçoit dans le hub : titre, année, genres, label, type de sortie (la pochette a son fichier, voir Images.set_poster).

Rien n'est écrit dans la médiathèque. Une correction est gardée dans `kv` sous `edit:<bibliothèque>:<id de l'album>`, avec les seuls champs corrigés, et appliquée à la ligne de `items` : tout de suite quand l'administrateur l'enregistre, puis à chaque scan (comme les données `enrich:`, voir scan.enriched_for). Listes, tri, recherche, filtre « Genre » et compteurs lisent donc les colonnes sans rien savoir des corrections.

Pour que « Rétablir » rende la valeur des étiquettes sans attendre un scan, la ligne corrigée garde dans `meta["original"]` ce que chaque champ corrigé valait avant (les valeurs que le dernier scan a lues). `apply` commence par les remettre : on part toujours de l'origine, qu'il y ait une correction, plusieurs ou aucune.
"""

from __future__ import annotations

import json
import threading
import time
import unicodedata

from libraries.common.text import initial

from .common import clean_text, sort_key

FIELDS = ("title", "year", "genres", "label", "type")        # l'ordre où `edited` les rend
RELEASE_TYPES = ("album", "ep", "single", "compilation", "live", "soundtrack", "remix")
TITLE_MAX, GENRES_MAX, GENRE_MAX, LABEL_MAX, YEAR_MIN = 300, 20, 60, 200, 1000
PREFIX = "edit:"
LOCK = threading.Lock()          # lire la ligne puis la réécrire (save), et lire les corrections puis écrire la bibliothèque (scan_library) : l'un après l'autre, jamais entrelacés. On ne prend que `db.write_lock` à l'intérieur, jamais l'inverse


class Invalid(ValueError):
    """Une valeur hors des bornes ; le message est celui que l'administrateur lit."""


def key(lib_id, item_id):
    return f"{PREFIX}{lib_id}:{item_id}"


def _text(value, limit, what):
    """`value` nettoyé comme un titre lu dans une étiquette (sans marques invisibles ni espaces aux bords) ; texte seul, sans caractère de contrôle, d'au plus `limit` caractères."""
    if not isinstance(value, str) or any(unicodedata.category(c) == "Cc" for c in value):
        raise Invalid(f"{what} : du texte, sans caractère de contrôle")
    text = clean_text(value)
    if len(text) > limit:
        raise Invalid(f"{what} : {limit} caractères au plus")
    return text


def _title(value):
    title = _text(value, TITLE_MAX, "titre")
    if not title:
        raise Invalid(f"titre : de 1 à {TITLE_MAX} caractères")
    return title


def _year(value):
    text = str(value)          # un nombre ou un texte de quatre chiffres ; tout autre JSON (liste, booléen, décimal) ne ressemble pas à un an une fois écrit
    if not (len(text) == 4 and text.isascii() and text.isdigit() and YEAR_MIN <= int(text) <= time.localtime().tm_year + 1):
        raise Invalid(f"année : quatre chiffres, de {YEAR_MIN} à l'an prochain")
    return text


def _genres(value):
    if not isinstance(value, list) or len(value) > GENRES_MAX:
        raise Invalid(f"genres : une liste de {GENRES_MAX} au plus")
    genres = [_text(genre, GENRE_MAX, "genre") for genre in value]
    if not all(genres):
        raise Invalid("genres : aucun genre vide")
    return list(dict.fromkeys(genres))          # une fois chacun, dans l'ordre


def _label(value):
    return _text(value, LABEL_MAX, "label")


def _type(value):
    if value not in RELEASE_TYPES:
        raise Invalid("type : " + ", ".join(RELEASE_TYPES))
    return value


PARSERS = {"title": _title, "year": _year, "genres": _genres, "label": _label, "type": _type}


def parse(body):
    """(corrections {champ: valeur}, retours [champ]) d'une requête : `null` rétablit un champ ; `poster` ne se rétablit que ainsi (l'image s'envoie à part). Invalid au moindre champ inconnu ou hors bornes : tout ou rien."""
    changes, resets = {}, []
    for field, value in body.items():
        if field not in PARSERS and field != "poster":
            raise Invalid("champ inconnu : un album se corrige par " + ", ".join((*FIELDS, "poster")))
        if value is None:
            resets.append(field)
        elif field == "poster":
            raise Invalid("poster : seul null le rétablit ; l'image s'envoie à /api/item/<id>/poster")
        else:
            changes[field] = PARSERS[field](value)
    if not body:
        raise Invalid("rien à corriger")
    return changes, resets


def kept(stored):
    """Les corrections gardées dans `kv`, celles qui sont encore valables : un champ abîmé (la base touchée à la main) est ignoré, jamais une erreur de scan."""
    found = {}
    for field, value in (stored.items() if isinstance(stored, dict) else ()):
        try:
            found[field] = PARSERS[field](value)
        except (KeyError, Invalid):
            pass
    return found


def listed(corrections):
    """Les champs corrigés, dans l'ordre de FIELDS."""
    return [field for field in FIELDS if field in corrections]


def of_library(db, lib_id):
    """{id d'album: corrections} gardées pour une bibliothèque : un scan les réapplique."""
    prefix = f"{PREFIX}{lib_id}:"
    found = {}
    for row in db.query("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)):
        try:
            corrections = kept(json.loads(row["value"]))
        except ValueError:
            continue
        if corrections:
            found[row["key"][len(prefix):]] = corrections
    return found


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _put_title(row, title):
    row.update(title=title, sort=sort_key(title), letter=initial(title))


def _put_types(meta, types):
    if types:
        meta["release_types"] = types
    else:
        meta.pop("release_types", None)             # le scan n'écrit pas de liste vide


def restore(row):
    """La ligne d'un album telle que les étiquettes la donnent : les valeurs de `meta["original"]` remises, et plus rien d'autre à retirer."""
    row = dict(row)
    meta = json.loads(row["meta"])
    original = meta.pop("original", None) or {}
    if "title" in original:
        _put_title(row, original["title"])
    if "year" in original:
        row["year"] = original["year"]
    if "genres" in original:
        row["genres"] = _dump(original["genres"])
    if "label" in original:
        meta["label"] = original["label"]
    if "type" in original:
        _put_types(meta, original["type"])
    row["meta"] = _dump(meta)
    return row


def apply(row, corrections):
    """La ligne d'un album avec ses corrections ({champ: valeur valable}, voir `kept`), les valeurs d'origine gardées dans `meta["original"]`. Sans correction, la ligne des étiquettes."""
    row = restore(row)
    if not corrections:
        return row
    meta = json.loads(row["meta"])
    types = meta.get("release_types", [])
    original = {"title": row["title"], "year": row["year"], "genres": json.loads(row["genres"]), "label": meta.get("label", ""), "type": types}
    if "title" in corrections:
        _put_title(row, corrections["title"])
    if "year" in corrections:
        row["year"] = corrections["year"]
    if "genres" in corrections:
        row["genres"] = _dump(corrections["genres"])
    if "label" in corrections:
        meta["label"] = corrections["label"]
    if "type" in corrections:
        _put_types(meta, [corrections["type"], *(t for t in types[1:] if t != corrections["type"])])          # le type principal est remplacé ; les autres types (live, compilation...) restent
    meta["original"] = {field: original[field] for field in FIELDS if field in corrections}
    row["meta"] = _dump(meta)
    return row


def apply_all(items, edits):
    """Les lignes d'un scan, avec la correction de chaque album qui en a une (`edits` : voir of_library)."""
    return [apply(row, edits[row["id"]]) if row["type"] == "album" and row["id"] in edits else row for row in items]


def save(db, row, changes, resets):
    """Garde les corrections de l'album `row` (la ligne de `items`) : celles de `changes` posées, celles de `resets` retirées ; la ligne est réécrite tout de suite. Rend les corrections qui restent."""
    corrections = {**kept(db.kv_get(key(row["lib"], row["id"]))), **changes}
    for field in resets:
        corrections.pop(field, None)
    if corrections:
        db.kv_set(key(row["lib"], row["id"]), corrections)
    else:
        db.kv_delete(key(row["lib"], row["id"]))
    db.rewrite_item(apply(row, corrections))
    return corrections
