"""Les images servies : le type d'après les octets, le rognage, ffmpeg à défaut de Pillow, et d'où vient l'image d'un item ou d'une entrée."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from hub.db import Database  # noqa: E402
from hub.media import images  # noqa: E402

needs_pillow = unittest.skipUnless(images.Image is not None, "Pillow absent")


class TestSniff(unittest.TestCase):
    def test_the_type_comes_from_the_first_bytes(self):
        self.assertEqual(images.sniff(b"\xff\xd8\xff\xe0abc"), "image/jpeg")
        self.assertEqual(images.sniff(fixtures.PNG), "image/png")
        self.assertEqual(images.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_anything_else_is_taken_for_a_jpeg(self):
        self.assertEqual(images.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt "), "image/jpeg")
        self.assertEqual(images.sniff(b"WEBP\x00\x00\x00\x00RIFF"), "image/jpeg")
        self.assertEqual(images.sniff(b""), "image/jpeg")


@needs_pillow
class TestCropped(unittest.TestCase):
    def crop(self, box, size=(200, 100)):
        return images._cropped(images.Image.new("RGB", size), box).size

    def test_the_box_is_a_share_of_the_width_and_the_height(self):
        self.assertEqual(self.crop((0.1, 0.2, 0.3, 0.4)), (60, 40))

    def test_what_is_outside_the_picture_is_dropped(self):
        self.assertEqual(self.crop((0.9, 0.9, 0.5, 0.5)), (20, 10))

    def test_the_result_has_at_least_one_pixel(self):
        self.assertEqual(self.crop((0.5, 0.5, 0, 0)), (1, 1))
        self.assertEqual(self.crop((1, 1, 0.5, 0.5)), (1, 1))

    def test_a_box_at_the_far_edge_keeps_the_last_column_and_the_last_row(self):
        picture = images.Image.new("RGB", (200, 100))
        picture.putpixel((199, 99), (255, 0, 0))
        self.assertEqual(images._cropped(picture, (1, 1, 0.5, 0.5)).getpixel((0, 0)), (255, 0, 0))


class TestFfmpegJpeg(unittest.TestCase):
    def convert(self, result):
        cfg = types.SimpleNamespace(data=Path(tempfile.mkdtemp()), max_streams=1, ffmpeg="ffmpeg")
        self.addCleanup(shutil.rmtree, cfg.data, True)
        service = images.Images(cfg, None)
        run = mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)
        with mock.patch.object(images.subprocess, "run", run):
            return service._ffmpeg_jpeg(b"octets", 320), run

    def test_the_bytes_ffmpeg_writes_are_returned_with_the_width_asked(self):
        data, run = self.convert(types.SimpleNamespace(returncode=0, stdout=b"jpeg"))
        self.assertEqual(data, b"jpeg")
        self.assertIn("scale='min(320,iw)':-2", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], b"octets")

    def test_a_failure_or_an_empty_answer_gives_nothing(self):
        for result in (types.SimpleNamespace(returncode=1, stdout=b"jpeg"), types.SimpleNamespace(returncode=0, stdout=b""), OSError("pas de ffmpeg"),
                       subprocess.TimeoutExpired("ffmpeg", 30)):
            self.assertIsNone(self.convert(result)[0], repr(result))


def item(ident, kind="show", lib="lib", meta=None):
    return {"id": ident, "lib": lib, "type": kind, "title": "Titre", "sort": "titre", "letter": "T", "year": "", "summary": "", "genres": "[]", "grp": "", "meta": json.dumps(meta or {}),
            "added": 0, "count": 1}


def entry(ident, item_id, rel, kind="book", meta=None):
    return {"id": ident, "item_id": item_id, "lib": "lib", "rel": rel, "kind": kind, "season": None, "disc": None, "number": 1, "title": "", "summary": "", "duration": 0.0,
            "size": 10, "mtime": 20, "meta": json.dumps(meta or {}), "probe": None}


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "lib"
        self.root.mkdir()
        self.db = Database(self.tmp / "hub.db")
        libs = {"lib": types.SimpleNamespace(path=self.root)}
        self.cfg = types.SimpleNamespace(data=self.tmp / "data", max_streams=2, ffmpeg="", library=libs.get)
        self.service = images.Images(self.cfg, self.db)

    def fill(self, items, entries=()):
        self.db.replace_library("lib", items, list(entries))


class TestGet(ServiceCase):
    def test_an_image_is_served_as_it_is_without_a_width_and_then_from_the_cache(self):
        (self.root / "poster.png").write_bytes(fixtures.PNG)
        self.fill([item("i1", meta={"image": "poster.png"})])
        data, mime, key = self.service.get("item", "i1")
        self.assertEqual((data, mime), (fixtures.PNG, "image/png"))
        (self.root / "poster.png").unlink()
        self.assertEqual(self.service.get("item", "i1"), (fixtures.PNG, "image/png", key))

    def test_an_unknown_item_or_library_has_no_image(self):
        self.fill([item("i1", lib="ailleurs")])
        self.assertIsNone(self.service.get("item", "absent"))
        self.assertIsNone(self.service.get("entry", "absent"))
        self.assertIsNone(self.service.get("item", "i1"))

    def test_an_item_without_image_is_not_searched_again_but_an_entry_is(self):
        self.fill([item("i1"), item("i2")], [entry("e1", "i2", "x.txt", kind="audio")])
        with mock.patch.object(self.service, "item_source", return_value=None) as source:
            self.assertIsNone(self.service.get("item", "i1"))
            self.assertIsNone(self.service.get("item", "i1"))
        self.assertEqual(source.call_count, 1)
        self.assertEqual(len(list(self.service.cache.glob("*.none"))), 1)
        with mock.patch.object(self.service, "entry_source", return_value=None) as source:
            self.assertIsNone(self.service.get("entry", "e1"))
            self.assertIsNone(self.service.get("entry", "e1"))
        self.assertEqual(source.call_count, 2)
        self.assertEqual(len(list(self.service.cache.glob("*.none"))), 1)


class TestBookCover(ServiceCase):
    def make(self, name, pages, meta, ident="e1"):
        with zipfile.ZipFile(self.root / name, "w") as z:
            for n, page in enumerate(pages, 1):
                z.writestr(f"{n:03d}.png", page)
        self.fill([item("i1")], [entry(ident, "i1", name, meta=meta)])

    def test_the_cover_is_the_page_the_sheet_designates_otherwise_the_first(self):
        pages = [fixtures.PNG + bytes([n]) for n in range(3)]
        self.make("b.cbz", pages, {"format": "cbz", "info": {"FrontCover": 2}})
        self.assertEqual(self.service.get("entry", "e1")[0], pages[2])
        self.make("c.cbz", pages, {"format": "cbz"}, ident="e2")
        self.assertEqual(self.service.get("entry", "e2")[0], pages[0])

    def test_a_book_that_cannot_be_read_has_no_cover(self):
        (self.root / "casse.cbz").write_bytes(b"pas une archive")
        self.fill([item("i1")], [entry("e1", "i1", "casse.cbz", meta={"format": "cbz"})])
        self.assertIsNone(self.service.get("entry", "e1"))


@needs_pillow
class TestResize(unittest.TestCase):
    def test_without_a_width_the_bytes_stay_as_they_are(self):
        service = images.Images(types.SimpleNamespace(data=Path(tempfile.mkdtemp()), max_streams=1, ffmpeg=""), None)
        self.addCleanup(shutil.rmtree, service.cfg.data, True)
        self.assertEqual(service.resize(fixtures.PNG, 0), (fixtures.PNG, "image/png"))
        data, mime = service.resize(fixtures.PNG, 160)
        self.assertEqual((mime, images.sniff(data)), ("image/jpeg", "image/jpeg"))


if __name__ == "__main__":
    unittest.main()
