"""L'enrichissement, sans réseau : le casting d'une série, les photos d'artistes (fichiers, sauvegarde)."""

from __future__ import annotations

import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.video.tmdb import TmdbAuthError, TmdbError  # noqa: E402

from hub import enrich  # noqa: E402
from hub.media import images  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 20


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = self.tmp / "artists"
        self.folder.mkdir()

    def put(self, folder, name, data=b"x"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return folder / name


class FakeTmdb:
    def __init__(self, credits=None, error=None):
        self.credits, self.error = credits, error

    def aggregate_credits(self, show_id):
        if self.error:
            raise self.error
        return self.credits


class TestCast(unittest.TestCase):
    def test_the_most_present_actors_come_first_with_their_main_role(self):
        people = [{"name": "Rare", "total_episode_count": 1, "order": 0, "roles": [{"character": "Passant", "episode_count": 1}]},
                  {"name": "Star", "total_episode_count": 40, "order": 5, "roles": [{"character": "Second", "episode_count": 3}, {"character": "Héros", "episode_count": 37}]},
                  {"name": "Égal", "total_episode_count": 40, "order": 2, "roles": [{"character": "Ami", "episode_count": 40}]}]
        self.assertEqual(enrich.cast(FakeTmdb({"cast": people}), 1),
                         [{"name": "Égal", "role": "Ami"}, {"name": "Star", "role": "Héros"}, {"name": "Rare", "role": "Passant"}])      # à égalité : l'ordre du générique

    def test_someone_without_a_name_or_a_role_is_handled(self):
        people = [{"name": "", "total_episode_count": 99}, {"name": "Sans rôle", "roles": []}, {"name": "Sans personnage", "roles": [{"episode_count": 2}]}, {"name": "Muet"}]
        self.assertEqual(enrich.cast(FakeTmdb({"cast": people}), 1), [{"name": "Sans rôle", "role": ""}, {"name": "Sans personnage", "role": ""}, {"name": "Muet", "role": ""}])

    def test_only_the_first_twenty_are_kept(self):
        people = [{"name": f"Acteur {n:02d}", "total_episode_count": 100 - n} for n in range(30)]
        self.assertEqual([p["name"] for p in enrich.cast(FakeTmdb({"cast": people}), 1)], [f"Acteur {n:02d}" for n in range(20)])

    def test_a_show_tmdb_does_not_know_has_no_cast_but_a_refused_key_is_an_error(self):
        self.assertEqual(enrich.cast(FakeTmdb(error=TmdbError("introuvable")), 1), [])
        with self.assertRaises(TmdbAuthError):
            enrich.cast(FakeTmdb(error=TmdbAuthError("clé refusée")), 1)
        self.assertEqual(enrich.cast(FakeTmdb({}), 1), [])


class TestImageExt(unittest.TestCase):
    def test_the_extension_comes_from_the_first_bytes(self):
        self.assertEqual([enrich.image_ext(d) for d in (JPEG, PNG, WEBP)], [".jpg", ".png", ".webp"])

    def test_something_that_is_not_an_image_has_none(self):
        self.assertIsNone(enrich.image_ext(b"<html>"))
        self.assertIsNone(enrich.image_ext(b"RIFF\x00\x00\x00\x00WAVEfmt "))                 # un RIFF qui n'est pas du WebP
        self.assertIsNone(enrich.image_ext(b""))


class TestPhotoFile(TempCase):
    def test_the_photo_in_the_artist_folder_wins(self):
        home = self.tmp / "Musique" / "Daft Punk"
        self.put(home, "artist.webp")
        self.put(self.folder, "a1.jpg")
        self.assertEqual(enrich.photo_file(self.folder, "a1", home=home), home / "artist.webp")

    def test_otherwise_the_one_under_the_data_folder_with_any_extension(self):
        self.put(self.folder, "a1.png")
        self.assertEqual(enrich.photo_file(self.folder, "a1", home=self.tmp / "vide"), self.folder / "a1.png")
        self.assertEqual(enrich.photo_file(self.folder, "a1"), self.folder / "a1.png")

    def test_no_photo_is_none(self):
        self.assertIsNone(enrich.photo_file(self.folder, "a1", "mbid-1"))

    def test_the_old_names_are_moved_to_the_current_one_the_uploaded_first(self):
        self.put(self.folder, "custom-a1.jpg", b"envoyee")
        self.put(self.folder, "mbid-1.png", b"wikipedia")
        found = enrich.photo_file(self.folder, "a1", "mbid-1")
        self.assertEqual((found, found.read_bytes()), (self.folder / "a1.jpg", b"envoyee"))
        self.assertFalse((self.folder / "custom-a1.jpg").exists())

    def test_the_musicbrainz_name_is_moved_when_nothing_else_is_there(self):
        self.put(self.folder, "mbid-1.webp")
        self.assertEqual(enrich.photo_file(self.folder, "a1", "mbid-1"), self.folder / "a1.webp")

    def test_a_name_that_could_be_a_path_is_never_looked_up(self):
        self.put(self.tmp, "secret.jpg")
        self.assertIsNone(enrich.photo_file(self.folder, "a1", "../secret"))

    def test_when_the_move_fails_the_old_file_is_still_served(self):
        old = self.put(self.folder, "mbid-1.jpg")
        with mock.patch.object(Path, "replace", side_effect=OSError("lecture seule")):
            self.assertEqual(enrich.photo_file(self.folder, "a1", "mbid-1"), old)


class TestSavePhoto(TempCase):
    def cfg(self):
        return types.SimpleNamespace(data=self.tmp, ffmpeg="")

    def save(self, data, converter=False, **kw):
        patches = [mock.patch.object(images, "can_webp", return_value=converter)]
        if converter:
            patches.append(mock.patch.object(images, "to_webp", return_value=WEBP))
        with patches[0], (patches[1] if converter else mock.MagicMock()):
            return enrich.save_photo(self.cfg(), "a1", data, **kw)

    def test_without_a_converter_the_photo_is_kept_as_it_is_under_the_data_folder(self):
        self.assertEqual(self.save(PNG), self.folder / "a1.png")
        self.assertEqual((self.folder / "a1.png").read_bytes(), PNG)

    def test_a_new_photo_replaces_the_old_one_whatever_its_extension(self):
        self.put(self.folder, "a1.jpg")
        self.save(PNG)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a1.png"])

    def test_the_old_names_of_the_same_artist_are_removed(self):
        for name in ("custom-a1.jpg", "mbid-1.png", "a1.webp"):
            self.put(self.folder, name)
        self.put(self.folder, "autre.jpg")
        self.save(JPEG, mbid="mbid-1")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a1.jpg", "autre.jpg"])

    def test_a_crop_needs_a_converter(self):
        with self.assertRaises(images.NoConverter):
            self.save(PNG, box=(0, 0, 1, 1))

    def test_with_a_converter_the_photo_goes_to_the_artist_folder_as_webp(self):
        home = self.tmp / "Musique" / "Daft Punk"
        self.put(self.folder, "a1.jpg")
        self.assertEqual(self.save(PNG, converter=True, home=home), home / "artist.webp")
        self.assertEqual((home / "artist.webp").read_bytes(), WEBP)
        self.assertEqual(list(self.folder.iterdir()), [])                                 # ni l'ancienne d'artists/, ni l'autre dossier

    def test_with_a_converter_and_no_folder_it_stays_under_the_data_folder(self):
        self.assertEqual(self.save(PNG, converter=True), self.folder / "a1.webp")

    def test_a_new_photo_in_the_data_folder_removes_the_one_in_the_artist_folder(self):
        home = self.tmp / "Musique" / "Daft Punk"
        self.put(home, "artist.webp")
        self.assertEqual(self.save(PNG, converter=False, home=home), self.folder / "a1.png")        # sans WebP : gardée telle quelle, hors du dossier de l'artiste
        self.assertFalse((home / "artist.webp").exists())

    def test_a_file_that_cannot_be_removed_does_not_stop_the_save(self):
        self.put(self.folder, "custom-a1.jpg")
        real = Path.unlink

        def locked(path, missing_ok=False):
            if path.name.startswith("custom-"):
                raise OSError("verrouillé")
            return real(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", locked):
            self.assertEqual(self.save(JPEG).name, "a1.jpg")
        self.assertTrue((self.folder / "custom-a1.jpg").exists())


if __name__ == "__main__":
    unittest.main()
