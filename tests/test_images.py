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


class TestConversionSlots(unittest.TestCase):
    def test_there_is_always_at_least_one_slot_whatever_the_setting(self):
        for streams, slots in ((0, 1), (1, 1), (3, 3)):
            service = images.Images(types.SimpleNamespace(data=Path(tempfile.mkdtemp()), max_streams=streams, ffmpeg=""), None)
            self.addCleanup(shutil.rmtree, service.cfg.data, True)
            taken = [service.convert.acquire(blocking=False) for _ in range(slots + 1)]
            self.assertEqual(taken, [True] * slots + [False], streams)


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
        self.assertTrue(run.call_args.kwargs["capture_output"])

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


def encoded(fmt, size, mode="RGB", **save):
    """Les octets d'une image unie de `size`, au format `fmt`."""
    out = io.BytesIO()
    images.Image.new(mode, size).save(out, fmt, **save)
    return out.getvalue()


class TestSnapWidth(unittest.TestCase):
    def test_a_width_snaps_up_to_the_next_size_that_can_be_asked(self):
        for asked, served in ((0, 0), (None, 0), (1, 160), (160, 160), (161, 320), (320, 320), (481, 720), (1280, 1280), (1281, 1280), (5000, 1280), ("320", 320)):
            self.assertEqual(images.snap_width(asked), served, asked)


class TestCropBox(unittest.TestCase):
    def test_a_rectangle_inside_the_picture_is_read_as_fractions(self):
        self.assertEqual(images.crop_box("0.1,0.2,0.3,0.4"), (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(images.crop_box("0,0,1,1"), (0, 0, 1, 1))
        self.assertEqual(images.crop_box("0.5,0.5,0.5,0.5"), (0.5, 0.5, 0.5, 0.5))

    def test_what_is_not_four_numbers_is_refused(self):
        for text in ("", "1,2,3", "a,b,c,d", "0,0,1,1,1"):
            with self.assertRaisesRegex(ValueError, "recadrage attendu"):
                images.crop_box(text)

    def test_each_edge_of_the_rectangle_has_its_own_limit(self):
        for text in ("1,0,0.00001,0.5", "0,1,0.5,0.00001", "0,0,0,0.5", "0,0,0.5,0", "-0.1,0,0.5,0.5", "0,-0.1,0.5,0.5", "0,0,1.1,0.5", "0,0,0.5,1.1",
                     "0.5,0,0.6,0.5", "0,0.5,0.5,0.6", "0.5,0,0.5002,0.5", "0,0.5,0.5,0.5002"):
            with self.assertRaisesRegex(ValueError, "sort de l'image", msg=text):
                images.crop_box(text)

    def test_a_rounding_error_at_the_far_edge_is_forgiven_but_not_more(self):
        self.assertEqual(images.crop_box("0.0001,0,1,1"), (0.0001, 0, 1, 1))
        self.assertEqual(images.crop_box("0,0.0001,1,1"), (0, 0.0001, 1, 1))
        self.assertEqual(images.crop_box("0.5,0.5,0.50005,0.50005"), (0.5, 0.5, 0.50005, 0.50005))


@needs_pillow
class TestReduced(unittest.TestCase):
    def test_a_picture_that_fits_is_returned_itself(self):
        picture = images.Image.new("RGB", (100, 50))
        self.assertIs(images._reduced(picture, 100), picture)
        self.assertIs(images._reduced(picture, 300), picture)

    def test_a_bigger_one_gets_its_long_side_to_the_limit(self):
        self.assertEqual(images._reduced(images.Image.new("RGB", (200, 100)), 100).size, (100, 50))
        self.assertEqual(images._reduced(images.Image.new("RGB", (100, 400)), 100).size, (25, 100))

    def test_a_thin_picture_keeps_one_pixel(self):
        self.assertEqual(images._reduced(images.Image.new("RGB", (1000, 1)), 10).size, (10, 1))
        self.assertEqual(images._reduced(images.Image.new("RGB", (1, 1000)), 10).size, (1, 10))


@needs_pillow
class TestPillowJpeg(unittest.TestCase):
    def size(self, data, width):
        with images.Image.open(io.BytesIO(images.Images._pillow_jpeg(data, width))) as out:
            return out.size

    def test_the_height_follows_the_shape_of_the_picture(self):
        self.assertEqual(self.size(encoded("PNG", (400, 800)), 100), (100, 200))
        self.assertEqual(self.size(encoded("PNG", (300, 100)), 150), (150, 50))

    def test_a_thin_picture_keeps_one_pixel(self):
        self.assertEqual(self.size(encoded("PNG", (1000, 1)), 10), (10, 1))

    def test_a_picture_narrower_than_the_width_is_not_enlarged(self):
        self.assertEqual(self.size(encoded("PNG", (50, 80)), 100), (50, 80))

    def test_the_jpeg_is_written_with_optimized_tables(self):
        noise = images.Image.effect_noise((200, 200), 60).convert("RGB")
        raw, plain = io.BytesIO(), io.BytesIO()
        noise.save(raw, "PNG")
        noise.save(plain, "JPEG", quality=80)
        self.assertLess(len(images.Images._pillow_jpeg(raw.getvalue(), 400)), len(plain.getvalue()))

    def test_a_jpeg_is_decoded_straight_at_a_reduced_size_but_a_png_is_not(self):
        from PIL import JpegImagePlugin
        real = JpegImagePlugin.JpegImageFile.draft
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "draft", autospec=True, side_effect=real) as draft:
            images.Images._pillow_jpeg(encoded("PNG", (400, 400)), 100)
            self.assertEqual(draft.call_count, 0)
            images.Images._pillow_jpeg(encoded("JPEG", (400, 400)), 100)
        self.assertEqual(draft.call_args.args[1:], ("RGB", (200, 300)))


def has_webp():
    from PIL import features
    return images.Image is not None and features.check("webp")


@unittest.skipUnless(has_webp(), "Pillow sans libwebp")
class TestPillowWebp(unittest.TestCase):
    def mode(self, data):
        with images.Image.open(io.BytesIO(images._pillow_webp(data, None, 100))) as out:
            return out.mode

    def test_a_picture_with_transparency_keeps_it(self):
        self.assertEqual(self.mode(encoded("PNG", (20, 20), "RGBA")), "RGBA")
        self.assertEqual(self.mode(encoded("PNG", (20, 20), "P", transparency=0)), "RGBA")

    def test_an_opaque_one_stays_opaque(self):
        self.assertEqual(self.mode(encoded("PNG", (20, 20))), "RGB")
        self.assertEqual(self.mode(encoded("JPEG", (20, 20))), "RGB")


class TestFfmpegWebp(unittest.TestCase):
    def convert(self, returncode=0, stdout=b"webp", box=None):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=returncode, stdout=stdout))
        with mock.patch.object(images.subprocess, "run", run):
            return images._ffmpeg_webp(b"octets", "ffmpeg", box, 500), run

    def test_the_box_becomes_a_crop_before_the_scale(self):
        data, run = self.convert(box=(0.1, 0.2, 0.3, 0.4))
        self.assertEqual(data, b"webp")
        self.assertIn("crop=iw*0.3:ih*0.4:iw*0.1:ih*0.2,scale='min(500,iw)':'min(500,ih)':force_original_aspect_ratio=decrease", run.call_args.args[0])

    def test_without_a_box_nothing_is_cropped(self):
        self.assertIn("scale='min(500,iw)':'min(500,ih)':force_original_aspect_ratio=decrease", self.convert()[1].call_args.args[0])
        self.assertFalse(any("crop=" in part for part in self.convert()[1].call_args.args[0]))

    def test_it_takes_both_a_success_code_and_some_bytes(self):
        for returncode, stdout in ((1, b"webp"), (0, b"")):
            with self.assertRaisesRegex(images.ConvertError, "illisible"):
                self.convert(returncode, stdout)


class TestMoreSources(ServiceCase):
    def test_a_poster_kept_by_the_enrichment_is_read_from_the_data_folder(self):
        (self.cfg.data / "tmdb").mkdir()
        (self.cfg.data / "tmdb" / "poster.png").write_bytes(fixtures.PNG)
        self.fill([item("i1", meta={"poster_file": "poster.png"})])
        row = self.db.one("SELECT * FROM items WHERE id=?", ("i1",))
        self.assertEqual(self.service.item_source(self.cfg.library("lib"), row), fixtures.PNG)

    def source(self, ident="i1"):
        row = self.db.one("SELECT * FROM items WHERE id=?", (ident,))
        return self.service.item_source(self.cfg.library("lib"), row)

    def test_an_album_has_its_cover_file_and_nothing_else(self):
        (self.root / "Artiste").mkdir()
        (self.root / "Artiste" / "cover.png").write_bytes(fixtures.PNG)
        (self.root / "Artiste" / "folder.jpg").write_bytes(b"jpeg")
        self.fill([item("i1", kind="album", meta={"folder": "Artiste", "cover_file": "cover.png"}), item("i2", kind="album", meta={"folder": "Artiste"})])
        self.assertEqual(self.source("i1"), fixtures.PNG)
        self.assertIsNone(self.source("i2"))

    def test_the_poster_comes_before_the_image_of_the_folder_which_comes_before_the_entries(self):
        (self.cfg.data / "tmdb").mkdir()
        (self.cfg.data / "tmdb" / "poster.png").write_bytes(b"affiche")
        (self.root / "folder.jpg").write_bytes(b"dossier")
        self.fill([item("i1", meta={"poster_file": "poster.png", "image": "folder.jpg"}), item("i2", meta={"poster_file": "absente.png", "image": "folder.jpg"}),
                   item("i3", meta={"poster_file": "absente.png", "image": "absente.jpg"})], [entry("e3", "i3", "ep.mkv", kind="video")])
        self.assertEqual(self.source("i1"), b"affiche")
        self.assertEqual(self.source("i2"), b"dossier")
        with mock.patch.object(images.ebml, "read_cover", return_value=(b"jaquette", "cover.jpg")) as read:
            self.assertEqual(self.source("i3"), b"jaquette")
        self.assertEqual(read.call_args.args[0], self.root / "ep.mkv")

    def test_an_item_with_no_image_anywhere_has_none(self):
        self.fill([item("i1", meta={"image": "absente.jpg"}), item("i2", meta={"image": "absente.jpg"})], [entry("e2", "i2", "ep.txt", kind="video")])
        self.assertIsNone(self.source("i1"))
        self.assertIsNone(self.source("i2"))

    def test_an_album_cover_stamp_needs_a_library_and_a_cover_file(self):
        row = {"meta": json.dumps({"cover_file": "cover.jpg"})}
        self.assertEqual(self.service._cover_stamp(None, row), "")
        self.assertEqual(self.service._cover_stamp(self.cfg.library("lib"), {"meta": "{}"}), "")
        self.assertEqual(self.service._cover_stamp(self.cfg.library("lib"), row), "")      # le fichier n'y est pas
        (self.root / "cover.jpg").write_bytes(b"jpeg")
        self.assertRegex(self.service._cover_stamp(self.cfg.library("lib"), row), r"^\|4\|\d+$")

    @needs_pillow
    def test_a_photo_without_a_width_is_served_as_it_is(self):
        photo = self.root / "photo.png"
        photo.write_bytes(encoded("PNG", (400, 400)))
        data, mime, _key = self.service.file("a", photo)
        self.assertEqual((data, mime), (photo.read_bytes(), "image/png"))


@needs_pillow
class TestToJpeg(unittest.TestCase):
    def opened(self, data):
        return images.Image.open(io.BytesIO(data))

    @unittest.skipUnless(has_webp(), "Pillow sans libwebp")
    def test_a_webp_becomes_a_jpeg_of_the_same_size(self):
        data = images.to_jpeg(encoded("WEBP", (30, 20)))
        self.assertEqual(images.sniff(data), "image/jpeg")
        with self.opened(data) as out:
            self.assertEqual((out.format, out.size), ("JPEG", (30, 20)))

    def test_a_transparent_background_becomes_white_not_black(self):
        with self.opened(images.to_jpeg(encoded("PNG", (10, 10), "RGBA"))) as out:
            self.assertTrue(all(channel > 240 for channel in out.convert("RGB").getpixel((5, 5))))

    def test_an_unreadable_image_is_a_convert_error(self):
        with self.assertRaises(images.ConvertError):
            images.to_jpeg(b"RIFF....WEBPpas une image")


class TestToJpegWithoutPillow(unittest.TestCase):
    def test_ffmpeg_does_it_when_pillow_is_not_there(self):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=b"jpeg"))
        with mock.patch.object(images, "Image", None), mock.patch.object(images.subprocess, "run", run):
            self.assertEqual(images.to_jpeg(b"octets", "ffmpeg"), b"jpeg")
        self.assertIn("mjpeg", run.call_args.args[0])

    def test_ffmpeg_takes_over_from_a_pillow_that_cannot_read_the_file(self):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=b"jpeg"))
        with mock.patch.object(images, "_pillow_to_jpeg", side_effect=ValueError("pas de libwebp")), mock.patch.object(images.subprocess, "run", run):
            self.assertEqual(images.to_jpeg(b"octets", "ffmpeg"), b"jpeg")

    def test_a_failing_ffmpeg_is_a_convert_error(self):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=1, stdout=b""))
        with mock.patch.object(images, "Image", None), mock.patch.object(images.subprocess, "run", run), self.assertRaises(images.ConvertError):
            images.to_jpeg(b"octets", "ffmpeg")

    def test_without_pillow_or_ffmpeg_there_is_no_converter(self):
        with mock.patch.object(images, "Image", None), self.assertRaises(images.NoConverter):
            images.to_jpeg(b"octets", "")


class TestCacheFolderGone(ServiceCase):
    def setUp(self):
        super().setUp()
        (self.root / "poster.png").write_bytes(fixtures.PNG)
        self.fill([item("i1", meta={"image": "poster.png"}), item("i2")])

    def test_an_image_is_still_served_and_the_folder_comes_back(self):
        shutil.rmtree(self.service.cache)
        data, mime, _key = self.service.get("item", "i1")
        self.assertEqual((data, mime), (fixtures.PNG, "image/png"))
        self.assertEqual(len(list(self.service.cache.glob("*.jpg"))), 1)

    def test_an_item_without_image_is_a_plain_miss_not_an_error(self):
        shutil.rmtree(self.service.cache)
        self.assertIsNone(self.service.get("item", "i2"))
        self.assertEqual(len(list(self.service.cache.glob("*.none"))), 1)

    def test_a_cache_that_cannot_be_written_never_breaks_a_request(self):
        shutil.rmtree(self.service.cache)
        with mock.patch.object(images, "write_atomic", side_effect=OSError("disque plein")), mock.patch.object(Path, "mkdir", side_effect=OSError("lecture seule")):
            self.assertEqual(self.service.get("item", "i1")[0], fixtures.PNG)
            self.assertIsNone(self.service.get("item", "i2"))


class TestCustomPoster(ServiceCase):
    def setUp(self):
        super().setUp()
        (self.root / "folder.jpg").write_bytes(b"dossier")
        self.fill([item("i1", meta={"image": "folder.jpg"}), item("i2", kind="album")])

    def folder(self):
        return self.cfg.data / "posters"

    def test_a_poster_chosen_comes_before_every_other_source(self):
        self.service.set_poster("i1", fixtures.PNG, ".png")
        self.assertEqual(self.service.get("item", "i1")[:2], (fixtures.PNG, "image/png"))

    def test_changing_the_poster_changes_the_image_served_and_its_stamp(self):
        before = self.service.get("item", "i1")
        self.service.set_poster("i1", fixtures.PNG, ".png")
        first = self.service.get("item", "i1")
        self.service.set_poster("i1", b"\xff\xd8\xff\xe0autre", ".jpg")
        second = self.service.get("item", "i1")
        self.assertEqual((before[0], first[0], second[0]), (b"dossier", fixtures.PNG, b"\xff\xd8\xff\xe0autre"))
        self.assertEqual(len({before[2], first[2], second[2]}), 3)

    def test_only_the_last_file_of_an_item_is_kept(self):
        self.service.set_poster("i1", fixtures.PNG, ".png")
        self.service.set_poster("i1", b"\xff\xd8\xff\xe0autre", ".jpg")
        self.service.set_poster("i2", fixtures.PNG, ".png")
        self.assertEqual(sorted(p.suffix for p in self.folder().iterdir()), [".jpg", ".png"])

    def test_the_same_image_again_changes_nothing(self):
        self.service.set_poster("i1", fixtures.PNG, ".png")
        names = sorted(p.name for p in self.folder().iterdir())
        self.service.set_poster("i1", fixtures.PNG, ".png")
        self.assertEqual(sorted(p.name for p in self.folder().iterdir()), names)
        self.assertEqual(self.service.get("item", "i1")[0], fixtures.PNG)

    def test_a_poster_file_that_went_missing_falls_back_to_the_original(self):
        self.service.set_poster("i1", fixtures.PNG, ".png")
        for path in self.folder().iterdir():
            path.unlink()
        self.assertEqual(self.service.get("item", "i1")[0], b"dossier")

    def test_a_name_that_could_be_a_path_is_never_read(self):
        (self.cfg.data / "secret.png").write_bytes(b"secret")
        self.db.kv_set("poster:i1", "../secret.png")
        self.assertEqual(self.service.get("item", "i1")[0], b"dossier")


if __name__ == "__main__":
    unittest.main()
