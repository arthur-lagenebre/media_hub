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
import subprocess
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


class Images:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.cache = cfg.data / "img"
        self.cache.mkdir(parents=True, exist_ok=True)

    # --- Réduction ---------------------------------------------------------
    def resize(self, data, width):
        if not width:
            return data, sniff(data)
        if Image is not None:
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.format == "JPEG":
                        image.draft("RGB", (width * 2, width * 3))
                    image = image.convert("RGB")
                    if image.width > width:
                        image = image.resize((width, max(1, round(image.height * width / image.width))), Image.LANCZOS)
                    out = io.BytesIO()
                    image.save(out, "JPEG", quality=80, optimize=True)
                    return out.getvalue(), "image/jpeg"
            except Exception:       # image illisible : on la rend telle quelle
                return data, sniff(data)
        if self.cfg.ffmpeg:
            try:
                done = subprocess.run([self.cfg.ffmpeg, "-v", "error", "-nostdin", "-i", "pipe:0", "-vf", f"scale='min({width},iw)':-2", "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "pipe:1"],
                                      input=data, capture_output=True, timeout=30)
                if done.returncode == 0 and done.stdout:
                    return done.stdout, "image/jpeg"
            except (OSError, subprocess.SubprocessError):
                pass
        return data, sniff(data)

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
        if not source:
            return None
        data, mime = self.resize(source, width)
        try:
            hit.write_bytes(data)
        except OSError:
            pass
        return data, mime, key

    def get(self, kind, ident, width=0):
        """(octets, type MIME, étiquette) ou None. `kind` : "item" ou "entry"."""
        width = snap_width(width)
        if kind == "item":
            row = self.db.one("SELECT * FROM items WHERE id=?", (ident,))
            stamp = ""
            if row:
                stamp = f"{row['added']}|{row['count']}|{row['meta']}"
                if row["type"] == "album":
                    stamp += self._cover_stamp(self.cfg.library(row["lib"]), row)
        else:
            row = self.db.one("SELECT * FROM entries WHERE id=?", (ident,))
            stamp = f"{row['size']}|{row['mtime']}" if row else ""
        if row is None:
            return None
        lib = self.cfg.library(row["lib"])
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
        if not source and kind == "entry":
            return None
        if not source:
            miss.touch()
            return None
        data, mime = self.resize(source, width)
        try:
            hit.write_bytes(data)
        except OSError:
            pass
        return data, mime, key
