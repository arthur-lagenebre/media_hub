"""Le libellé affiché d'une bibliothèque, que l'administrateur peut changer.

L'identifiant d'une bibliothèque (`films`, `series`…) ne change jamais : droits, `items.lib`, clés `kv` et préférences s'y accrochent. Seul le libellé, ce que l'interface écrit,
se renomme. Les surcharges sont gardées dans la clé `library-labels` de `kv` : `{identifiant: libellé}`. Une surcharge dont la bibliothèque n'est plus configurée est ignorée
(sans être effacée : si la bibliothèque revient, son nom choisi revient avec elle). Le libellé d'origine est celui de la configuration (`config.Library.label`).

`shown` est LA fonction qui décide du libellé affiché : tout ce que le serveur écrit avec le nom d'une bibliothèque (réponses, tâches, journaux) passe par elle.
"""

from __future__ import annotations

import threading
import unicodedata

KEY = "library-labels"
MAX_LENGTH = 40

_write = threading.Lock()          # lire puis réécrire le dictionnaire : deux renommages simultanés ne s'écrasent pas


class LabelError(ValueError):
    """Un libellé refusé ; le message est en français et part tel quel dans la réponse (400)."""


def _usable(value):
    """Vrai si `value` est un libellé que `clean` laisserait passer tel quel : un texte, non vide, de 40 caractères au plus, sans caractère de contrôle ni espace de bout, déjà en NFC.
    La base peut avoir été écrite à la main (ou par une version qui contrôlait moins) : ce qui n'est pas sûr à afficher est ignoré, et le nom d'origine s'affiche."""
    if not isinstance(value, str) or not value:
        return False
    try:
        return clean(value) == value
    except LabelError:
        return False


def stored(db):
    """{identifiant: libellé} tel que gardé, réduit à ce qui est sûr à afficher (voir `_usable`)."""
    kept = db.kv_get(KEY)
    return {k: v for k, v in kept.items() if isinstance(k, str) and _usable(v)} if isinstance(kept, dict) else {}


def shown(db, lib):
    """Le libellé affiché de `lib` : celui que l'administrateur a choisi, sinon celui de la configuration."""
    return stored(db).get(lib.id) or lib.label


def describe(db, lib):
    """`{"id", "label", "default_label", "kind"}` : la bibliothèque telle que les routes la montrent (`default_label` : le nom d'origine, celui où revenir)."""
    return {"id": lib.id, "label": shown(db, lib), "default_label": lib.label, "kind": lib.kind}


def clean(text):
    """Le libellé nettoyé (en NFC, espaces de bout retirés), ou LabelError s'il porte un caractère de contrôle ou de mise en forme, ou s'il fait plus de MAX_LENGTH caractères.
    NFC d'abord : « é » écrit en deux caractères (e + accent combinant) compte pour un et se garde sous sa forme composée."""
    if not isinstance(text, str):
        raise LabelError("le libellé doit être un texte")
    text = unicodedata.normalize("NFC", text).strip()
    if any(unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp") for c in text):
        raise LabelError("le libellé ne doit contenir ni caractère de contrôle ni caractère de mise en forme")
    if len(text) > MAX_LENGTH:
        raise LabelError(f"le libellé fait {MAX_LENGTH} caractères au plus")
    return text


def _comparable(text):
    """Le texte tel qu'on compare deux noms : formes de compatibilité (pleine largeur, ligatures) ramenées à la normale, sans casse ni accents, espaces (insécables compris) réduits à un seul.
    Les homoglyphes d'un autre alphabet (« Ｆ » est repéré, pas le « Ϝ » grec ni le « Ѕ » cyrillique) ne sont pas détectés."""
    from libraries.common.text import fold          # ici et pas en tête : seul un renommage s'en sert, et ce module est importé (par hub.inbox, hub.__main__) avant que media_library soit sur le chemin
    return " ".join(fold(unicodedata.normalize("NFKC", text)).split())


def _taken_by_another(db, libraries, lib, text):
    """Le nom (affiché ou d'origine) d'une AUTRE bibliothèque que `text` reprend, ou celui qu'une bibliothèque absente de la configuration (dossier non monté) s'est choisi et retrouvera
    à son retour ; None sinon. Comparaison à la casse, aux accents et à la forme Unicode près."""
    wanted = _comparable(text)
    kept = stored(db)
    names = [name for other in libraries if other.id != lib.id for name in (shown(db, other), other.label)]
    names += [name for lib_id, name in kept.items() if lib_id not in {other.id for other in libraries}]
    return next((name for name in names if _comparable(name) == wanted), None)


def rename(db, libraries, lib, text):
    """Donne à `lib` le libellé `text` ; un texte vide (ou absent : None) rétablit le nom d'origine. Rend le libellé affiché. LabelError, sans rien changer, si le texte est refusé.
    Un libellé d'origine retapé tel quel ne laisse pas de surcharge. Les surcharges des bibliothèques absentes de `libraries` sont gardées."""
    text = clean("" if text is None else text)
    custom = bool(text) and text != unicodedata.normalize("NFC", lib.label)
    with _write:
        if custom:
            name = _taken_by_another(db, libraries, lib, text)
            if name is not None:
                raise LabelError(f"« {name} » est déjà le nom d'une autre bibliothèque")
        kept = db.kv_get(KEY)
        kept = dict(kept) if isinstance(kept, dict) else {}
        if custom:
            kept[lib.id] = text
        else:
            kept.pop(lib.id, None)
        db.kv_set(KEY, kept)
    return shown(db, lib)
