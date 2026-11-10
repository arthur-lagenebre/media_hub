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


def stored(db):
    """{identifiant: libellé} tel que gardé ; ce qui n'est pas une chaîne non vide est ignoré."""
    kept = db.kv_get(KEY) if db is not None else None
    return {k: v for k, v in kept.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(kept, dict) else {}


def shown(db, lib):
    """Le libellé affiché de `lib` : celui que l'administrateur a choisi, sinon celui de la configuration."""
    return stored(db).get(lib.id) or lib.label


def describe(db, lib):
    """`{"id", "label", "default_label", "kind"}` : la bibliothèque telle que les routes la montrent (`default_label` : le nom d'origine, celui où revenir)."""
    return {"id": lib.id, "label": shown(db, lib), "default_label": lib.label, "kind": lib.kind}


def clean(text):
    """Le libellé nettoyé (espaces de bout retirés), ou LabelError s'il porte un caractère de contrôle ou de mise en forme, ou s'il fait plus de MAX_LENGTH caractères."""
    if not isinstance(text, str):
        raise LabelError("le libellé doit être un texte")
    text = text.strip()
    if any(unicodedata.category(c).startswith("C") or unicodedata.category(c) in ("Zl", "Zp") for c in text):
        raise LabelError("le libellé ne doit contenir ni caractère de contrôle ni caractère de mise en forme")
    if len(text) > MAX_LENGTH:
        raise LabelError(f"le libellé fait {MAX_LENGTH} caractères au plus")
    return text


def _taken_by_another(db, libraries, lib, text):
    """Le libellé (affiché ou d'origine) d'une AUTRE bibliothèque que `text` reprend, à la casse et aux accents près ; None sinon."""
    from libraries.common.text import fold          # ici et pas en tête : `shown` sert aussi là où media_library n'est pas encore sur le chemin (boîte de réception, annonce du démarrage)
    wanted = fold(text)
    for other in libraries:
        if other.id != lib.id:
            for name in (shown(db, other), other.label):
                if fold(name) == wanted:
                    return name
    return None


def rename(db, libraries, lib, text):
    """Donne à `lib` le libellé `text` ; un texte vide (ou absent : None) rétablit le nom d'origine. Rend le libellé affiché. LabelError, sans rien changer, si le texte est refusé.
    Un libellé d'origine retapé tel quel ne laisse pas de surcharge. Les surcharges des bibliothèques absentes de `libraries` sont gardées."""
    text = clean("" if text is None else text)
    custom = bool(text) and text != lib.label
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
