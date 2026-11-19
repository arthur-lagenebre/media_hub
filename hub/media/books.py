"""Lire un livre : les pages d'un .cbz, les chapitres d'un .epub, servis un par un depuis l'archive.

Rien n'est extrait sur le disque : l'archive est ouverte, la page demandée est lue, c'est tout (un .cbz de 300 Mo ne se lit qu'à l'index et à la page voulue).
"""

from __future__ import annotations

import mimetypes
import posixpath
import re
from html import unescape
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.parsers import expat

from libraries.books import epub
from libraries.books.archive import ZIP_ERRORS, BookError, image_names, open_zip, read_member

OPF = "{http://www.idpf.org/2007/opf}"
OPS_TYPE = "{http://www.idpf.org/2007/ops}type"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"
EXTERNAL_LINK_RE = re.compile(r"[a-z][a-z0-9+.-]*:|//", re.IGNORECASE)
MAX_TOC_BYTES = 8 * 1024 * 1024
MAX_TOC_ELEMENTS = 200_000            # la taille seule ne borne pas la mémoire : 8 Mio de <a/> font 2 millions d'éléments
MAX_TOC_ENTRIES = 5000
MAX_TOC_DEPTH = 10
MAX_TOC_TITLE = 200
TOC_ERRORS = (BookError, expat.ExpatError, LookupError, *ZIP_ERRORS)      # LookupError : un encodage inconnu annoncé par le fichier
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


def member_path(folder, href):
    """Le chemin dans l'archive d'un lien (sans fragment) écrit depuis `folder`."""
    return posixpath.normpath(posixpath.join(folder, unquote(href)))


def spine_of(root, opf_path):
    """{'base': dossier de l'.opf, 'spine': [chemins des chapitres dans l'archive], 'title'}."""
    base = posixpath.dirname(opf_path)
    manifest = root.find(f"{OPF}manifest")
    hrefs = {i.get("id"): i.get("href") for i in (list(manifest) if manifest is not None else [])}
    spine_el = root.find(f"{OPF}spine")
    chapters = []
    for ref in (list(spine_el) if spine_el is not None else []):
        href = hrefs.get(ref.get("idref"))
        if href and ref.get("linear") != "no":
            chapters.append(member_path(base, href))
    return {"base": base, "spine": chapters, "title": epub.parse_opf(root).get("Title", "")}


def epub_book(path):
    """`spine_of` plus 'toc', le sommaire (voir `read_toc`), en une seule ouverture de l'archive ; BookError si l'.opf est illisible, jamais à cause du sommaire."""
    with open_zip(path) as archive:
        root, opf_path = epub.opf_root(archive)
        book = spine_of(root, opf_path)
        book["toc"] = read_toc(archive, root, book["base"], book["spine"])
        return book


def read_toc(archive, root, base, spine):
    """[{title, chapter, anchor, level}] : `chapter` est l'indice dans `spine`, `level` vaut 0 au premier niveau. Le nav d'abord, le NCX s'il est absent, illisible ou ne mène à rien ; [] sinon."""
    chapters = {}
    for number, name in enumerate(spine):
        chapters.setdefault(name, number)
    for item, outline in toc_sources(root):
        name = member_path(base, item.get("href"))
        try:
            tree = parse_toc(read_toc_file(archive, name))
        except TOC_ERRORS:
            continue
        entries = toc_entries(outline(tree), name, chapters)
        if entries:
            return entries
    return []


def toc_sources(root):
    """[(élément du manifeste, lecteur de son plan)], au plus un nav puis un NCX : le nombre de fichiers analysés ne dépend pas du manifeste."""
    manifest = root.find(f"{OPF}manifest")
    items = [i for i in (list(manifest) if manifest is not None else []) if i.get("href")]
    spine = root.find(f"{OPF}spine")
    toc_id = spine.get("toc") if spine is not None else None
    navs = [i for i in items if "nav" in (i.get("properties") or "").split()]
    named = [i for i in items if toc_id is not None and i.get("id") == toc_id]
    typed = [i for i in items if i.get("media-type") == NCX_MEDIA_TYPE]
    return [(i, nav_outline) for i in navs[:1]] + [(i, ncx_outline) for i in (named + typed)[:1]]


def read_toc_file(archive, name):
    """Les octets d'un sommaire, jamais plus que la limite : `archive.read` décompresserait tout avant de voir qu'une taille annoncée est fausse."""
    info = archive.getinfo(name)
    if info.file_size > MAX_TOC_BYTES:
        raise BookError("sommaire démesuré")
    with archive.open(info) as member:
        return member.read(MAX_TOC_BYTES)


def refuse_entity(*_):
    raise BookError("entité déclarée dans le sommaire")


def qualified(name):
    return "{" + name if "}" in name else name


def parse_toc(data):
    """L'arbre d'un fichier de sommaire. Passe par expat pour refuser toute déclaration d'entité (aucun sommaire n'en a besoin, et elles servent aux bombes XML)
    et pour décoder les entités HTML employées sans être déclarées (`&eacute;`, `&nbsp;`), qu'il y ait un DOCTYPE ou non ; au-delà de MAX_TOC_ELEMENTS, le fichier est illisible."""
    builder = ElementTree.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.UseForeignDTD(True)                  # comme si une DTD externe existait : une entité inconnue est passée au gestionnaire au lieu d'être une erreur
    parser.EntityDeclHandler = refuse_entity
    parser.SkippedEntityHandler = lambda name, is_parameter: None if is_parameter else builder.data(unescape(f"&{name};"))
    elements = 0

    def start(tag, attrs):
        nonlocal elements
        elements += 1
        if elements > MAX_TOC_ELEMENTS:
            raise BookError("sommaire démesuré")
        builder.start(qualified(tag), {qualified(k): v for k, v in attrs.items()})

    parser.StartElementHandler = start
    parser.EndElementHandler = lambda tag: builder.end(qualified(tag))
    parser.CharacterDataHandler = builder.data
    parser.Parse(data, True)
    return builder.close()


def local_name(element):
    return element.tag.rpartition("}")[2]


def child(parent, name):
    """Le premier enfant direct de ce nom, quel que soit son espace de noms ; None sans lui ou sans parent."""
    return next((c for c in (list(parent) if parent is not None else []) if local_name(c) == name), None)


def toc_title(element):
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())[:MAX_TOC_TITLE]


def outline_of(root, item_name, label_name, label):
    """(profondeur, titre, lien) de chaque élément `item_name`, dans l'ordre du fichier. Une pile plutôt que la récursion : l'imbrication vient du fichier.

    Le parcours ne descend pas dans un intitulé (`label_name`) : ce qui s'y trouve appartient à son titre. Sinon une liste glissée dans chaque lien ferait relire tout le reste du fichier à chaque titre."""
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if local_name(node) == item_name:
            yield (depth, *label(node))
            depth += 1
        stack.extend((c, depth) for c in reversed(list(node)) if local_name(c) != label_name)


def nav_label(item):
    link = child(item, "a")
    return toc_title(link), link.get("href") if link is not None else None


def ncx_label(point):
    content = child(point, "content")
    return toc_title(child(child(point, "navLabel"), "text")), content.get("src") if content is not None else None


def nav_outline(root):
    """Le plan du <nav> de type toc ; à défaut, du seul <nav> sans type."""
    navs = [e for e in root.iter() if local_name(e) == "nav"]
    typed = [n for n in navs if "toc" in (n.get(OPS_TYPE) or "").split()]
    untyped = [n for n in navs if n.get(OPS_TYPE) is None]
    chosen = typed[:1] or (untyped if len(untyped) == 1 else [])
    return outline_of(chosen[0], "li", "a", nav_label) if chosen else ()


def ncx_outline(root):
    nav_map = child(root, "navMap")
    return outline_of(nav_map, "navPoint", "navLabel", ncx_label) if nav_map is not None else ()


def toc_target(href, toc_path, chapters):
    """(indice du chapitre, ancre décodée) d'un lien écrit dans le fichier de sommaire ; None s'il ne mène pas à un chapitre."""
    if not href or EXTERNAL_LINK_RE.match(href):
        return None
    name, _, anchor = href.partition("#")                  # avant de décoder : un %23 appartient au nom du fichier
    path = member_path(posixpath.dirname(toc_path), name) if name else toc_path
    if path.split("/")[0] == ".." or path not in chapters:
        return None
    return chapters[path], unquote(anchor)


def toc_entries(outline, toc_path, chapters):
    """Les entrées gardées. Le niveau compte les ancêtres gardés : les enfants d'une entrée retirée remontent d'un cran."""
    entries, kept = [], []                                 # kept : les profondeurs des ancêtres gardés
    for depth, title, href in outline:
        while kept and kept[-1] >= depth:
            kept.pop()
        target = toc_target(href, toc_path, chapters)
        if not title or target is None or len(kept) >= MAX_TOC_DEPTH:
            continue
        entries.append({"title": title, "chapter": target[0], "anchor": target[1], "level": len(kept)})
        kept.append(depth)
        if len(entries) == MAX_TOC_ENTRIES:
            break
    return entries


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
