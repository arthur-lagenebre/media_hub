"""Lire un livre : les pages d'un .cbz, les chapitres d'un .epub, servis un par un depuis l'archive.

Rien n'est extrait sur le disque : l'archive est ouverte, la page demandée est lue, c'est tout (un .cbz de 300 Mo ne se lit qu'à l'index et à la page voulue).
"""

from __future__ import annotations

import mimetypes
import posixpath
from urllib.parse import unquote
from xml.etree import ElementTree

from libraries.books import epub
from libraries.books.archive import BookError, image_names, open_zip, read_member

OPF = "{http://www.idpf.org/2007/opf}"
mimetypes.add_type("application/xhtml+xml", ".xhtml")
mimetypes.add_type("font/otf", ".otf")


def reads_right_to_left(info, section):
    """Le sens de lecture par défaut d'un tome : vrai s'il se lit de droite à gauche.

    Le champ Manga de sa fiche ComicInfo le dit ("YesAndRightToLeft" : oui ; "Yes" et "No" : non) ; sans lui - champ absent ou "Unknown" -, la section « Manga » de la série le suppose.
    """
    field = str((info or {}).get("Manga") or "").strip().casefold()
    if field in ("yesandrighttoleft", "yes", "no"):
        return field == "yesandrighttoleft"
    return section == "Manga"


def cbz_pages(path):
    with open_zip(path) as archive:
        return image_names(archive)


def cbz_page(path, number):
    with open_zip(path) as archive:
        names = image_names(archive)
        if not 0 <= number < len(names):
            raise BookError("page inexistante")
        return read_member(archive, names[number], "page")


def epub_spine(path):
    """{'base': dossier de l'.opf, 'spine': [chemins des chapitres dans l'archive], 'title'}."""
    with open_zip(path) as archive:
        root, opf_path = epub.opf_root(archive)
        base = posixpath.dirname(opf_path)
        manifest = root.find(f"{OPF}manifest")
        hrefs = {i.get("id"): i.get("href") for i in (list(manifest) if manifest is not None else [])}
        spine_el = root.find(f"{OPF}spine")
        chapters = []
        for ref in (list(spine_el) if spine_el is not None else []):
            href = hrefs.get(ref.get("idref"))
            if href and ref.get("linear") != "no":
                chapters.append(posixpath.normpath(posixpath.join(base, unquote(href))))
        return {"base": base, "spine": chapters, "title": epub.parse_opf(root).get("Title", "")}


def epub_member(path, name):
    """(octets, type MIME) d'un fichier de l'epub ; BookError s'il n'y est pas."""
    name = posixpath.normpath(unquote(name)).lstrip("/")
    if name.startswith(".."):
        raise BookError("chemin refusé")
    with open_zip(path) as archive:
        if name not in archive.namelist():
            raise BookError("fichier absent de l'epub")
        data = read_member(archive, name, name)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, mime


def valid_xml(data):
    try:
        ElementTree.fromstring(data)
        return True
    except ElementTree.ParseError:
        return False
