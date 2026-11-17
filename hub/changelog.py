"""Le journal des nouveautés, écrit à la main pour les gens de la maison (`hub/CHANGELOG.md`), lu pour Réglages › À propos.

Format réduit : `## X.Y.Z — AAAA-MM-JJ`, puis des lignes `- texte`, la version la plus récente en haut. Toute autre ligne est ignorée."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

FILE = Path(__file__).resolve().parent / "CHANGELOG.md"
HEADING = re.compile(r"##\s+(\d+\.\d+\.\d+)\s+[—–-]\s+(\d{4}-\d{2}-\d{2})\s*")      # tiret cadratin, demi-cadratin ou trait d'union : il se tape mal
ITEM = re.compile(r"-\s+(\S.*)")


def heading(line):
    """(version, date) d'un titre de version, ou None : hors format, ou date qui n'existe pas (2026-13-45)."""
    found = HEADING.fullmatch(line)
    try:
        return (found[1], datetime.date.fromisoformat(found[2]).isoformat()) if found else None
    except ValueError:
        return None


def parse(text):
    """`[{"version", "date", "items"}]` dans l'ordre du fichier, sans les versions qui n'ont aucun point. Une ligne hors format ne compte pas,
    et un titre `#` hors format ferme la version en cours (ses points n'iraient pas à la précédente)."""
    versions, current = [], None
    for line in text.splitlines():
        title, item = heading(line), ITEM.fullmatch(line.rstrip())
        if title:
            current = {"version": title[0], "date": title[1], "items": []}
            versions.append(current)
        elif line.startswith("#"):
            current = None
        elif item and current is not None:
            current["items"].append(item[1])
    return [version for version in versions if version["items"]]


def load(path=None):
    """Les versions du fichier (`FILE` par défaut) ; un fichier absent ou illisible n'en donne aucune. `utf-8-sig` : un éditeur Windows ajoute une marque en tête."""
    try:
        return parse(Path(path or FILE).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return []
