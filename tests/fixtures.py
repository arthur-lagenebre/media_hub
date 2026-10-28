"""Une médiathèque de poche pour les tests : de vrais fichiers, minuscules.

- Les .mkv « étiquetés » sont fabriqués octet par octet (en-tête EBML, index, tags, jaquette) : c'est le seul moyen d'y mettre des tags de saga ou de saison comme le fait Metadata.py, et ça ne demande aucun outil.
- Les vidéos lisibles (pour tester la lecture) sont de vrais fichiers produits par ffmpeg, s'il est installé.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[2] / "media_library"
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

FFMPEG = shutil.which("ffmpeg")

# Trois images minuscules mais valides
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082")
JPEG = None


def _id(ident):
    return ident.to_bytes((ident.bit_length() + 7) // 8, "big")


def _size(n):
    length = next(l for l in range(1, 9) if n < (1 << (7 * l)) - 1)
    return ((1 << (7 * length)) | n).to_bytes(length, "big")


def el(ident, content):
    return _id(ident) + _size(len(content)) + content


def simple(name, value):
    return el(0x67C8, el(0x45A3, name.encode()) + el(0x4487, str(value).encode()))


def tag_block(level, pairs):
    return el(0x7373, el(0x63C0, el(0x68CA, bytes([level]))) + b"".join(simple(n, v) for n, v in pairs))


def mkv_bytes(blocks=(), cover=None, minutes=95.5):
    """Un .mkv sans image ni son, mais dont l'en-tête, les tags et la jaquette sont ceux de Metadata.py ; `blocks` = [(niveau, [(nom, valeur), ...])]."""
    header = el(0x1A45DFA3, el(0x4282, b"matroska"))
    info = el(0x1549A966, el(0x2AD7B1, (1000000).to_bytes(4, "big")) + el(0x4489, struct.pack(">d", minutes * 60_000)))
    clusters = b"".join(el(0x1F43B675, el(0xE7, bytes([n + 1]))) for n in range(2))
    tail = b""
    tags = el(0x1254C367, b"".join(tag_block(level, pairs) for level, pairs in blocks)) if blocks else b""
    attachments = el(0x1941A469, el(0x61A7, el(0x466E, b"cover.jpg") + el(0x4660, b"image/jpeg") + el(0x465C, cover))) if cover else b""
    parts = []
    if tags:
        parts.append((0x1254C367, tags))
    if attachments:
        parts.append((0x1941A469, attachments))
    seek = lambda positions: el(0x114D9B74, b"".join(el(0x4DBB, el(0x53AB, _id(i)) + el(0x53AC, p.to_bytes(8, "big"))) for i, p in positions))   # noqa: E731
    length = len(seek([(0x1549A966, 0)] + [(i, 0) for i, _ in parts]))
    at = length + len(info) + len(clusters)
    positions = [(0x1549A966, length)]
    for ident, data in parts:
        positions.append((ident, at))
        at += len(data)
        tail += data
    return header + el(0x18538067, seek(positions) + info + clusters + tail)


def write_mkv(path, blocks=(), cover=None, minutes=95.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mkv_bytes(blocks, cover, minutes))
    return path


def movie_blocks(title, synopsis="", date="", tmdb="", genres="", directors=(), actors=(), collection="", part=None):
    movie = [("TITLE", title)]
    if tmdb:
        movie.append(("TMDB", tmdb))
    if synopsis:
        movie += [("SYNOPSIS", synopsis), ("SUMMARY", synopsis)]
    if date:
        movie.append(("DATE_RELEASED", date))
    movie += [("DIRECTOR", d) for d in directors] + [("ACTOR", a) for a in actors]
    if genres:
        movie.append(("GENRE", genres))
    blocks = []
    if collection:
        blocks.append((70, [("TITLE", collection)] + ([("PART_NUMBER", part)] if part is not None else [])))
    blocks.append((50, movie))
    return blocks


def episode_blocks(series, season, number, title, synopsis="", date=""):
    block = [("TITLE", title), ("PART_NUMBER", number)]
    if synopsis:
        block += [("SYNOPSIS", synopsis)]
    if date:
        block.append(("DATE_RELEASED", date))
    return [(70, [("TITLE", series)]), (60, [("PART_NUMBER", season), ("TITLE", f"Saison {season}")]), (50, block)]


# --- Musique ---------------------------------------------------------------
def write_flac(path, title, album, artist, number, total, year="2001", genre="Electronic", seconds=1, mbid="", cover=False):
    """Un vrai .flac, avec ses tags, produit par ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={220 + number * 20}:duration={seconds}"]
    tags = {"title": title, "album": album, "artist": artist, "albumartist": artist, "track": f"{number}/{total}", "tracktotal": str(total), "date": year, "genre": genre}
    if mbid:
        tags["MUSICBRAINZ_ALBUMID"] = mbid
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-c:a", "flac", str(path)]
    subprocess.run(cmd, check=True)
    return path


# --- Livres ----------------------------------------------------------------
def comicinfo_xml(**fields):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f'<?xml version="1.0"?><ComicInfo>{body}</ComicInfo>'


def write_cbz(path, pages=3, **info):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n in range(1, pages + 1):
            z.writestr(f"{n:03d}.png", PNG)
        if info:
            z.writestr("ComicInfo.xml", comicinfo_xml(**info))
    return path


def write_epub(path, title="Un roman", author="Quelqu'un", chapters=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = "".join(f'<item id="c{n}" href="c{n}.xhtml" media-type="application/xhtml+xml"/>' for n in range(1, chapters + 1))
    spine = "".join(f'<itemref idref="c{n}"/>' for n in range(1, chapters + 1))
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:identifier id=\"id\">x</dc:identifier></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>")
    container = '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("content.opf", opf)
        for n in range(1, chapters + 1):
            z.writestr(f"c{n}.xhtml", f"<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>Chapitre {n}</h1><p>Texte du chapitre {n}.</p></body></html>")
    return path


# --- Vidéo lisible ---------------------------------------------------------
def write_video(path, seconds=3, vcodec="libx264", acodec="aac", container=None, channels=2, subtitle=None, size="320x180"):
    """Un vrai film de quelques secondes : mire et sinus."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=24:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if subtitle:
        srt = path.with_suffix(".tmp.srt")
        srt.write_text(subtitle, encoding="utf-8")
        cmd += ["-i", str(srt)]
    cmd += ["-map", "0:v", "-map", "1:a"] + (["-map", "2:s"] if subtitle else [])
    cmd += ["-c:v", vcodec, "-pix_fmt", "yuv420p", "-g", "12", "-c:a", acodec, "-ac", str(channels)]
    if subtitle:
        cmd += ["-c:s", "srt" if path.suffix == ".mkv" else "mov_text"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    if subtitle:
        path.with_suffix(".tmp.srt").unlink()
    return path


def build_library(root):
    """Les cinq bibliothèques d'essai sous `root` ; rend {id: chemin}."""
    films, series, anim, musique, livres = (root / n for n in ("Films", "Series", "Animations", "Musique", "Livres"))
    synopsis = "Un voleur s'introduit dans les rêves."
    write_mkv(films / "Inception (2010)" / "Inception (2010).mkv", movie_blocks("Inception", synopsis, "2010-07-21", "movie/27205", "Science-Fiction, Action", ["Christopher Nolan"], ["Leonardo DiCaprio (Cobb)", "Elliot Page (Ariadne)"]), cover=PNG, minutes=148)
    write_mkv(films / "_DC" / "1 - Man of Steel.mkv", movie_blocks("Man of Steel", "Superman.", "2013-06-12", "movie/49521", "Action", collection="Saga DCEU", part=1), minutes=143)
    write_mkv(films / "_DC" / "2 - Batman v Superman.mkv", movie_blocks("Batman v Superman", "Duel.", "2016-03-23", "movie/209112", "Action", collection="Saga DCEU", part=2), minutes=151)
    write_mkv(films / "Sans Etiquette (1999)" / "film.mkv", [], minutes=90)
    for n, (title, text) in enumerate((("Pilote", "Début."), ("Suite", "Après.")), 1):
        write_mkv(series / "Dark" / "Saison 1" / f"{n:02d} - {title}.mkv", episode_blocks("Dark", 1, n, title, text, f"2017-12-0{n}"), cover=PNG if n == 1 else None, minutes=50)
    write_mkv(series / "Dark" / "Saison 2" / "01 - Retour.mkv", episode_blocks("Dark", 2, 1, "Retour", "", "2019-06-21"), minutes=52)
    (series / "Dark" / "folder.png").write_bytes(PNG)
    write_mkv(anim / "Frieren" / "Saison 1" / "01 - Le voyage.mkv", episode_blocks("Frieren", 1, 1, "Le voyage", "Elfe.", "2023-09-29"), minutes=24)
    for n, title in enumerate(("Discovery", "Intro"), 1):
        pass
    for n, title in enumerate(("One More Time", "Aerodynamic", "Digital Love"), 1):
        write_flac(musique / "Daft Punk" / "2001 - Discovery" / f"{n:02d} - {title}.flac", title, "Discovery", "Daft Punk", n, 3, mbid="11111111-2222-3333-4444-555555555555")
    (musique / "Daft Punk" / "2001 - Discovery" / "cover.png").write_bytes(PNG)
    write_cbz(livres / "Manga" / "Naruto" / "Tome 01.cbz", Series="Naruto", Number="1", Count="3", Writer="Kishimoto", Summary="Un ninja.", Genre="Action", Year="1999")
    write_cbz(livres / "Manga" / "Naruto" / "Tome 02.cbz", Series="Naruto", Number="2", Count="3", Writer="Kishimoto")
    write_epub(livres / "Romans" / "Dumas" / "Le Comte de Monte-Cristo.epub", "Le Comte de Monte-Cristo", "Alexandre Dumas")
    return {"films": films, "series": series, "animations": anim, "musique": musique, "livres": livres}
