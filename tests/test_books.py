"""Lire un livre : les pages d'un .cbz, les chapitres et fichiers d'un .epub, servis un par un depuis l'archive."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests import fixtures
from hub import config

fixtures.REPO  # noqa: B018  (rend `libraries` importable)
config.use_media_library(fixtures.REPO)

from libraries.books.archive import BookError  # noqa: E402

from hub.media import books  # noqa: E402

CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestCbz(TempCase):
    def test_the_pages_are_read_by_their_number_from_the_first_to_the_last(self):
        path = fixtures.write_cbz(self.tmp / "b.cbz", pages=3)
        self.assertEqual(len(books.cbz_pages(path)), 3)
        self.assertEqual(books.cbz_page(path, 0), fixtures.PNG)
        self.assertEqual(books.cbz_page(path, 2), fixtures.PNG)

    def test_a_page_outside_the_book_is_refused(self):
        path = fixtures.write_cbz(self.tmp / "b.cbz", pages=3)
        for number in (-1, 3, 4):
            with self.assertRaisesRegex(BookError, "page inexistante"):
                books.cbz_page(path, number)


class TestEpubSpine(TempCase):
    def write(self, manifest, spine, folder="OEBPS"):
        path = self.tmp / "l.epub"
        opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:title>Mon livre</dc:title><dc:identifier id="id">x</dc:identifier></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr(f"{folder}/content.opf", opf)
        return path

    def test_the_chapters_follow_the_spine_from_the_folder_of_the_opf_with_their_names_decoded(self):
        path = self.write('<item id="a" href="un%20deux.xhtml"/><item id="b" href="texte/trois.xhtml"/>', '<itemref idref="b"/><itemref idref="a"/>')
        found = books.epub_spine(path)
        self.assertEqual(found, {"base": "OEBPS", "spine": ["OEBPS/texte/trois.xhtml", "OEBPS/un deux.xhtml"], "title": "Mon livre"})

    def test_a_reference_to_nothing_or_a_secondary_item_is_not_a_chapter(self):
        path = self.write('<item id="a" href="a.xhtml"/><item id="c" href="c.xhtml"/><item id="sans-href"/>',
                          '<itemref idref="a"/><itemref idref="absent"/><itemref idref="c" linear="no"/><itemref idref="sans-href"/><itemref idref="a" linear="yes"/>')
        self.assertEqual(books.epub_spine(path)["spine"], ["OEBPS/a.xhtml", "OEBPS/a.xhtml"])

    def test_a_book_without_manifest_or_spine_has_no_chapter(self):
        path = self.tmp / "vide.epub"
        opf = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata/></package>'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", opf)
        self.assertEqual(books.epub_spine(path), {"base": "OEBPS", "spine": [], "title": ""})


class TestEpubMember(TempCase):
    def setUp(self):
        super().setUp()
        self.path = fixtures.write_epub(self.tmp / "l.epub")

    def test_a_file_comes_with_its_type(self):
        data, mime = books.epub_member(self.path, "c1.xhtml")
        self.assertIn(b"Chapitre 1", data)
        self.assertEqual(mime, "application/xhtml+xml")
        self.assertEqual(books.epub_member(self.path, "/c2.xhtml")[0], books.epub_member(self.path, "c2.xhtml")[0])      # la barre du début ne compte pas

    def test_a_file_that_is_not_in_the_book_is_refused(self):
        with self.assertRaisesRegex(BookError, "absent de l'epub"):
            books.epub_member(self.path, "inconnu.xhtml")

    def test_a_path_that_leaves_the_book_is_refused(self):
        for name in ("../secret.txt", "a/../../secret.txt", "%2e%2e/secret.txt"):
            with self.assertRaisesRegex(BookError, "chemin refusé"):
                books.epub_member(self.path, name)

    def test_a_file_of_an_unknown_type_is_served_as_bytes(self):
        with zipfile.ZipFile(self.path, "a") as z:
            z.writestr("donnees.zzz", b"abc")
        self.assertEqual(books.epub_member(self.path, "donnees.zzz"), (b"abc", "application/octet-stream"))


class TestValidXml(unittest.TestCase):
    def test_well_formed_xml_is_valid_and_the_rest_is_not(self):
        self.assertTrue(books.valid_xml(b"<a><b/></a>"))
        self.assertFalse(books.valid_xml(b"<a><b></a>"))
        self.assertFalse(books.valid_xml(b""))


if __name__ == "__main__":
    unittest.main()
