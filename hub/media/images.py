"""Images : affiches, jaquettes, pochettes, couvertures. Trouvées dans les fichiers eux-mêmes, réduites, gardées sur le disque.

D'où vient l'image d'un item :
  film    folder.jpg à côté du film, sinon la jaquette que Metadata.py a embarquée dans le .mkv ;
  série   l'affiche TMDB gardée par l'enrichissement, sinon folder.jpg, sinon la vignette d'un épisode ;
  album   cover.jpg (ou .png...) du dossier, et rien d'autre : ni folder.jpg, ni la pochette embarquée dans les pistes ;
  livre   la couverture désignée par ComicInfo.xml, sinon la première page / l'image de l'epub.

Réduire se fait avec Pillow s'il est là, sinon avec ffmpeg, sinon on rend l'image telle quelle. Une couverture de comics fait 4 Mo : sur une télé, avec 40 couvertures à l'écran, la réduction n'est pas un luxe.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
import warnings
from pathlib import Path

from libraries.books import shelf
from libraries.video import ebml

try:
    from PIL import Image
except ImportError:       # facultatif
    Image = None

warnings.filterwarnings("ignore", module="PIL")

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
WIDTHS = (160, 320, 480, 720, 1280)      # tailles demandables : le cache ne grossit pas au gré des paramètres


def sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def snap_width(width):
    width = int(width or 0)
    return next((w for w in WIDTHS if width <= w), WIDTHS[-1]) if width else 0


def write_atomic(path, data):
    """Écrit `data` d'un bloc (fichier temporaire, puis remplacement) : une requête qui arrive en même temps ne lit jamais un fichier à moitié écrit, que le navigateur garderait avec une étiquette stable."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


PHOTO_SIDE = 1280      # la photo d'un artiste ne dépasse pas ce côté : c'est la plus grande taille que le serveur sert (WIDTHS)


class ConvertError(Exception):
    """L'image ne se convertit pas : elle est illisible."""


class NoConverter(ConvertError):
    """Ni Pillow (avec libwebp) ni ffmpeg : rien ne sait écrire du WebP."""


def crop_box(text):
    """(x, y, largeur, hauteur) en fractions de l'image, d'après "x,y,l,h" ; ValueError si ce n'est pas un rectangle dans l'image."""
    try:
        x, y, w, h = (float(part) for part in text.split(","))
    except ValueError:
        raise ValueError("recadrage attendu : x,y,largeur,hauteur") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.0001 and y + h <= 1.0001):
        raise ValueError("le recadrage sort de l'image")
    return x, y, w, h


def can_webp(ffmpeg=""):
    """Peut-on écrire du WebP ? Pillow l'écrit s'il a été compilé avec libwebp ; sinon ffmpeg, s'il est là."""
    if Image is not None:
        try:
            from PIL import features
            if features.check("webp"):
                return True
        except Exception:
            pass
    return bool(ffmpeg)


def _cropped(image, box):
    """`image` rognée à `box` (x, y, largeur, hauteur en fractions), d'au moins un pixel."""
    x, y, w, h = box
    left, top = min(image.width - 1, round(x * image.width)), min(image.height - 1, round(y * image.height))
    return image.crop((left, top, max(left + 1, min(image.width, round((x + w) * image.width))), max(top + 1, min(image.height, round((y + h) * image.height)))))


def _reduced(image, side):
    """`image` réduite pour que son grand côté ne dépasse pas `side` ; telle quelle si elle y tient."""
    if max(image.size) <= side:
        return image
    scale = side / max(image.size)
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)


def _pillow_webp(data, box, side):
    """Les octets WebP écrits par Pillow ; None si Pillow n'a pas libwebp."""
    from PIL import ImageOps, features
    if not features.check("webp"):
        return None
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        image = _reduced(_cropped(image, box) if box else image, side)
        out = io.BytesIO()
        image.save(out, "WEBP", quality=85, method=4)
        return out.getvalue()


def _ffmpeg_webp(data, ffmpeg, box, side):
    crop = f"crop=iw*{box[2]}:ih*{box[3]}:iw*{box[0]}:ih*{box[1]}," if box else ""
    try:
        done = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", "pipe:0", "-vf", f"{crop}scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
                               "-frames:v", "1", "-c:v", "libwebp", "-q:v", "85", "-f", "webp", "pipe:1"], input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ConvertError(f"ffmpeg n'a pas répondu ({e})") from e
    if done.returncode == 0 and done.stdout:
        return done.stdout
    raise ConvertError("image illisible")


def to_webp(data, ffmpeg="", box=None, side=PHOTO_SIDE):
    """Les octets WebP de `data`, rognés à `box` (fractions, voir crop_box) puis réduits à `side` px au plus ; ce qui est hors du cadre disparaît. ConvertError si l'image ne se lit pas ou si rien ne sait écrire du WebP."""
    if Image is not None:
        try:
            out = _pillow_webp(data, box, side)
        except Exception as e:
            raise ConvertError(f"image illisible ({e})") from e
        if out is not None:
            return out
    if ffmpeg:
        return _ffmpeg_webp(data, ffmpeg, box, side)
    raise NoConverter("ni Pillow ni ffmpeg : pas de conversion en WebP possible")


class Images:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.cache = cfg.data / "img"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.convert = threading.BoundedSemaphore(max(1, cfg.max_streams))      # sans Pillow, chaque réduction lance un ffmpeg : 40 pochettes à l'écran n'en lancent pas 40 d'un coup

    # --- Réduction ---------------------------------------------------------
    def resize(self, data, width):
        if not width:
            return data, sniff(data)
        if Image is not None:
            try:
                return self._pillow_jpeg(data, width), "image/jpeg"
            except Exception:       # image illisible : on la rend telle quelle
                return data, sniff(data)
        small = self._ffmpeg_jpeg(data, width) if self.cfg.ffmpeg else None
        return (small, "image/jpeg") if small else (data, sniff(data))

    @staticmethod
    def _pillow_jpeg(data, width):
        with Image.open(io.BytesIO(data)) as image:
            if image.format == "JPEG":
                image.draft("RGB", (width * 2, width * 3))
            image = image.convert("RGB")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))), Image.LANCZOS)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=80, optimize=True)
            return out.getvalue()

    def _ffmpeg_jpeg(self, data, width):
        """Les octets JPEG que ffmpeg écrit ; None s'il échoue."""
        try:
            with self.convert:
                done = subprocess.run([self.cfg.ffmpeg, "-v", "error", "-nostdin", "-i", "pipe:0", "-vf", f"scale='min({width},iw)':-2", "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "pipe:1"],
                                      input=data, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 and done.stdout else None

    # --- Sources -----------------------------------------------------------
    def _file(self, lib, rel):
        return Path(lib.path) / rel

    def _from_image_file(self, path):
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def _mkv_cover(self, path):
        found = ebml.read_cover(path)
        return found[0] if found else None

    def _book_cover(self, path, meta):
        kind = meta.get("format", "")
        volume = shelf.Volume(path, "", "", "", "", 0, 0, kind, meta.get("info") or {})
        try:
            return shelf.read_cover(volume)
        except Exception:
            return None

    def entry_source(self, lib, entry):
        """Octets de l'image propre à un fichier (vignette d'épisode, couverture d'un tome) ; None s'il n'en a pas."""
        path = self._file(lib, entry["rel"])
        meta = json.loads(entry["meta"])
        if entry["kind"] == "video":
            return self._mkv_cover(path) if path.suffix.lower() == ".mkv" else None
        if entry["kind"] == "book":
            return self._book_cover(path, meta)
        return None

    def item_source(self, lib, item):
        meta = json.loads(item["meta"])
        entries = self.db.query("SELECT * FROM entries WHERE item_id=? ORDER BY season, disc, number, rel LIMIT 4", (item["id"],))
        base = Path(lib.path)
        if item["type"] == "album":       # cover.{ext} du dossier, rien d'autre : ni folder.jpg, ni pochette embarquée dans les pistes
            return self._from_image_file(base / meta.get("folder", "") / meta["cover_file"]) if meta.get("cover_file") else None
        if meta.get("poster_file"):
            data = self._from_image_file(self.cfg.data / "tmdb" / meta["poster_file"])
            if data:
                return data
        if meta.get("image"):
            data = self._from_image_file(base / meta["image"])
            if data:
                return data
        for entry in entries:
            data = self.entry_source(lib, entry)
            if data:
                return data
        return None

    # --- Accès -------------------------------------------------------------
    def _key(self, ref, stamp, width):
        return hashlib.sha1(f"{ref}|{stamp}|{width}".encode()).hexdigest()[:24]

    def _cover_stamp(self, lib, row):
        """Taille et date de cover.{ext} d'un album : remplacer l'image par une autre du même nom doit invalider le cache."""
        meta = json.loads(row["meta"])
        if lib is None or not meta.get("cover_file"):
            return ""
        try:
            info = (Path(lib.path) / meta.get("folder", "") / meta["cover_file"]).stat()
        except OSError:
            return ""
        return f"|{info.st_size}|{info.st_mtime_ns}"

    def file(self, ref, path, width=0):
        """(octets, type MIME, étiquette) d'une image posée sur le disque (la photo d'un artiste), réduite et gardée comme les autres ; None si elle n'y est plus."""
        try:
            info = Path(path).stat()
        except OSError:
            return None
        width = snap_width(width)
        key = self._key(f"file:{ref}", f"{info.st_size}|{info.st_mtime_ns}", width)
        hit = self.cache / f"{key}.jpg"
        if hit.is_file():
            data = hit.read_bytes()
            return data, sniff(data), key
        source = self._from_image_file(Path(path))
        return self._store(hit, source, width, key) if source else None

    def _store(self, hit, source, width, key):
        """(octets, type MIME, étiquette) de `source` réduite à `width`, gardée dans le cache sous `hit` (sans bruit si le disque refuse)."""
        data, mime = self.resize(source, width)
        try:
            write_atomic(hit, data)
        except OSError:
            pass
        return data, mime, key

    def _row_and_stamp(self, kind, ident):
        """(ligne de la table, ce qui change quand l'image doit changer) d'un item ou d'une entrée ; (None, "") s'ils n'existent pas."""
        if kind == "item":
            row = self.db.one("SELECT * FROM items WHERE id=?", (ident,))
            if row is None:
                return None, ""
            stamp = f"{row['added']}|{row['count']}|{row['meta']}"
            return row, stamp + (self._cover_stamp(self.cfg.library(row["lib"]), row) if row["type"] == "album" else "")
        row = self.db.one("SELECT * FROM entries WHERE id=?", (ident,))
        return row, f"{row['size']}|{row['mtime']}" if row else ""

    def get(self, kind, ident, width=0):
        """(octets, type MIME, étiquette) ou None. `kind` : "item" ou "entry"."""
        width = snap_width(width)
        row, stamp = self._row_and_stamp(kind, ident)
        lib = self.cfg.library(row["lib"]) if row is not None else None
        if lib is None:
            return None
        key = self._key(f"{kind}:{ident}", stamp, width)
        hit, miss = self.cache / f"{key}.jpg", self.cache / f"{key}.none"
        if hit.is_file():
            data = hit.read_bytes()
            return data, sniff(data), key
        if miss.is_file():
            return None
        source = self.item_source(lib, row) if kind == "item" else self.entry_source(lib, row)
        if not source:
            if kind == "item":
                miss.touch()          # un item sans image : on ne la cherche plus tant que son étiquette ne change pas (pas pour une entrée)
            return None
        return self._store(hit, source, width, key)
