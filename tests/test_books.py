"""Lire un livre : les pages d'un .cbz, les chapitres et fichiers d'un .epub, servis un par un depuis l'archive."""

from __future__ import annotations

import shutil
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

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
        found = books.epub_book(path)
        self.assertEqual(found, {"base": "OEBPS", "spine": ["OEBPS/texte/trois.xhtml", "OEBPS/un deux.xhtml"], "title": "Mon livre", "toc": []})

    def test_a_reference_to_nothing_or_a_secondary_item_is_not_a_chapter(self):
        path = self.write('<item id="a" href="a.xhtml"/><item id="c" href="c.xhtml"/><item id="sans-href"/>',
                          '<itemref idref="a"/><itemref idref="absent"/><itemref idref="c" linear="no"/><itemref idref="sans-href"/><itemref idref="a" linear="yes"/>')
        self.assertEqual(books.epub_book(path)["spine"], ["OEBPS/a.xhtml", "OEBPS/a.xhtml"])

    def test_a_book_without_manifest_or_spine_has_no_chapter(self):
        path = self.tmp / "vide.epub"
        opf = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata/></package>'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", CONTAINER)
            z.writestr("OEBPS/content.opf", opf)
        self.assertEqual(books.epub_book(path), {"base": "OEBPS", "spine": [], "title": "", "toc": []})


NAV_ITEM = '<item id="nav" href="{}" properties="nav" media-type="application/xhtml+xml"/>'
NCX_ITEM = '<item id="ncx" href="{}" media-type="application/x-dtbncx+xml"/>'


def nav_file(*navs):
    return f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>{"".join(navs)}</body></html>'


def toc_nav(items, kind="toc"):
    return f'<nav epub:type="{kind}"><ol>{items}</ol></nav>'


def li(href, title, children=""):
    return f'<li><a href="{href}">{title}</a>{children}</li>'


def point(src, title, children=""):
    return f'<navPoint><navLabel><text>{title}</text></navLabel><content src="{src}"/>{children}</navPoint>'


def ncx_file(points):
    return f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>{points}</navMap></ncx>'


def mark(title, chapter, anchor="", level=0):
    return {"title": title, "chapter": chapter, "anchor": anchor, "level": level}


class TocCase(TempCase):
    def book(self, nav=None, ncx=None, nav_path="nav/nav.xhtml", ncx_path="toc.ncx", manifest="", spine="", spine_attrs=""):
        """Un epub de trois chapitres (c1 à c3.xhtml, à la racine) avec ce sommaire ; le rend tel que le lit le hub."""
        files, items = {}, manifest
        if nav is not None:
            files[nav_path] = nav
            items += NAV_ITEM.format(nav_path)
        if ncx is not None:
            files[ncx_path] = ncx
            items += NCX_ITEM.format(ncx_path)
        return fixtures.write_epub(self.tmp / "t.epub", chapters=3, manifest_extra=items, spine_extra=spine, spine_attrs=spine_attrs, files=files)

    def toc(self, *args, **kwargs):
        return books.epub_book(self.book(*args, **kwargs))["toc"]


class TestEpubTocSources(TocCase):
    def test_an_epub3_nav_in_a_subfolder_gives_chapters_relative_to_itself_with_their_levels(self):
        nested = "<ol>" + li("../c2.xhtml", "Deux") + "</ol>"
        found = self.toc(nav_file(toc_nav(li("../c1.xhtml", "Un", nested) + li("../c3.xhtml", "Trois"))))
        self.assertEqual(found, [mark("Un", 0), mark("Deux", 1, level=1), mark("Trois", 2)])

    def test_landmarks_and_the_page_list_are_not_the_table_of_contents(self):
        found = self.toc(nav_file(toc_nav(li("../c3.xhtml", "Repère"), "landmarks"), toc_nav(li("../c1.xhtml", "Un")), toc_nav(li("../c2.xhtml", "p. 2"), "page-list")))
        self.assertEqual(found, [mark("Un", 0)])

    def test_a_single_nav_without_type_is_taken_but_two_are_ambiguous(self):
        plain = "<nav><ol>" + li("../c1.xhtml", "Un") + "</ol></nav>"
        self.assertEqual(self.toc(nav_file(plain)), [mark("Un", 0)])
        self.assertEqual(self.toc(nav_file(plain, plain)), [])

    def test_a_file_with_only_landmarks_gives_nothing(self):
        self.assertEqual(self.toc(nav_file(toc_nav(li("../c1.xhtml", "Repère"), "landmarks"))), [])

    def test_an_epub2_ncx_named_by_the_spine_gives_its_nested_points(self):
        ncx = ncx_file(point("c1.xhtml", "Un", point("c2.xhtml#s", "Deux", point("c3.xhtml", "Trois"))) + point("c3.xhtml", "Fin"))
        found = self.toc(ncx=ncx, spine_attrs=' toc="ncx"')
        self.assertEqual(found, [mark("Un", 0), mark("Deux", 1, "s", 1), mark("Trois", 2, level=2), mark("Fin", 2)])

    def test_an_entry_after_a_deeper_branch_returns_to_its_own_level(self):
        deep = "<ol>" + li("../c2.xhtml", "Deux", "<ol>" + li("../c3.xhtml", "Trois") + "</ol>") + li("../c3.xhtml", "Quatre") + "</ol>"
        found = self.toc(nav_file(toc_nav(li("../c1.xhtml", "Un", deep))))
        self.assertEqual([(e["title"], e["level"]) for e in found], [("Un", 0), ("Deux", 1), ("Trois", 2), ("Quatre", 1)])

    def test_the_ncx_is_found_by_its_type_without_a_usable_spine_attribute(self):
        ncx = ncx_file(point("c1.xhtml", "Un"))
        self.assertEqual(self.toc(ncx=ncx), [mark("Un", 0)])
        self.assertEqual(self.toc(ncx=ncx, spine_attrs=' toc="inconnu"'), [mark("Un", 0)])

    def test_the_links_of_an_ncx_are_relative_to_it(self):
        self.assertEqual(self.toc(ncx=ncx_file(point("../c2.xhtml", "Deux")), ncx_path="meta/toc.ncx"), [mark("Deux", 1)])

    def test_the_nav_comes_before_the_ncx(self):
        found = self.toc(nav_file(toc_nav(li("../c1.xhtml", "Nav"))), ncx_file(point("c2.xhtml", "Ncx")))
        self.assertEqual(found, [mark("Nav", 0)])

    def test_a_book_without_table_of_contents_has_an_empty_one(self):
        self.assertEqual(self.toc(), [])

    def test_a_table_of_contents_declared_but_missing_from_the_archive_is_empty(self):
        self.assertEqual(self.toc(manifest=NAV_ITEM.format("absent.xhtml") + NCX_ITEM.format("absent.ncx")), [])


class TestEpubTocLinks(TocCase):
    def links(self, *items, **kwargs):
        return self.toc(nav_file(toc_nav("".join(items))), **kwargs)

    def test_an_anchor_is_kept_decoded(self):
        self.assertEqual(self.links(li("../c2.xhtml#partie%20deux", "Deux")), [mark("Deux", 1, "partie deux")])

    def test_encoded_file_names_are_decoded_and_a_hash_in_a_name_is_not_an_anchor(self):
        manifest = '<item id="e" href="un%20deux.xhtml"/><item id="d" href="n%23.xhtml"/>'
        found = self.links(li("../un%20deux.xhtml", "Espace"), li("../n%23.xhtml#a", "Dièse"), manifest=manifest, spine='<itemref idref="e"/><itemref idref="d"/>')
        self.assertEqual(found, [mark("Espace", 3), mark("Dièse", 4, "a")])

    def test_an_anchor_alone_points_into_the_table_of_contents_itself(self):
        found = self.links(li("#debut", "Début"), nav_path="nav.xhtml", spine='<itemref idref="nav"/>')
        self.assertEqual(found, [mark("Début", 3, "debut")])

    def test_a_chapter_read_twice_is_found_at_its_first_place(self):
        self.assertEqual(self.links(li("../c1.xhtml", "Un"), spine='<itemref idref="c1"/>'), [mark("Un", 0)])

    def test_a_target_outside_the_spine_is_removed_and_its_children_move_up(self):
        children = "<ol>" + li("../c2.xhtml", "Deux", "<ol>" + li("../c3.xhtml", "Trois") + "</ol>") + "</ol>"
        expected = [mark("Deux", 1), mark("Trois", 2, level=1), mark("Après", 0)]
        self.assertEqual(self.links(li("../annexe.xhtml", "Annexe", children), li("../c1.xhtml", "Après")), expected)

    def test_a_chapter_marked_not_linear_is_outside_the_spine(self):
        found = self.links(li("../n.xhtml", "Note"), li("../c1.xhtml", "Un"), manifest='<item id="n" href="n.xhtml"/>', spine='<itemref idref="n" linear="no"/>')
        self.assertEqual(found, [mark("Un", 0)])

    def test_external_absolute_and_escaping_links_are_removed_with_their_children_kept(self):
        child = "<ol>" + li("../c1.xhtml", "Un") + "</ol>"
        for href in ("https://exemple.org/c1.xhtml", "mailto:a@b.c", "//exemple.org/c1.xhtml", "/c1.xhtml", "../../c1.xhtml", "../../../x/../c1.xhtml", ""):
            self.assertEqual(self.links(li(href, "Dehors", child)), [mark("Un", 0)], href)

    def test_a_spine_that_leaves_the_archive_does_not_make_an_escaping_link_valid(self):
        found = self.links(li("../../dehors.xhtml", "Dehors"), manifest='<item id="x" href="../dehors.xhtml"/>', spine='<itemref idref="x"/>')
        self.assertEqual(found, [])

    def test_a_label_without_link_gives_no_entry_and_its_children_stay_at_a_coherent_level(self):
        part = "<li><span>Partie</span><ol>" + li("../c1.xhtml", "Un", "<ol>" + li("../c2.xhtml", "Deux") + "</ol>") + "</ol></li>"
        self.assertEqual(self.links(part, li("../c3.xhtml", "Trois")), [mark("Un", 0), mark("Deux", 1, level=1), mark("Trois", 2)])

    def test_a_point_without_content_keeps_its_children(self):
        ncx = ncx_file("<navPoint><navLabel><text>Partie</text></navLabel>" + point("c1.xhtml", "Un") + "</navPoint>")
        self.assertEqual(self.toc(ncx=ncx), [mark("Un", 0)])


class TestEpubTocTitles(TocCase):
    def title(self, inner):
        return self.toc(nav_file(toc_nav(li("../c1.xhtml", inner))))

    def test_the_whole_text_is_taken_with_its_spaces_normalized(self):
        self.assertEqual(self.title("\n  Le <em>grand\n  </em>  <b>départ</b> "), [mark("Le grand départ", 0)])

    def test_the_label_of_an_ncx_is_normalized_too(self):
        self.assertEqual(self.toc(ncx=ncx_file(point("c1.xhtml", " Un\n deux "))), [mark("Un deux", 0)])

    def test_a_long_title_is_cut(self):
        self.assertEqual(self.title("x" * 5000)[0]["title"], "x" * books.MAX_TOC_TITLE)

    def test_an_entry_without_title_is_removed_and_its_children_stay(self):
        self.assertEqual(self.title("  <img src='a.png'/> "), [])
        child = "<ol>" + li("../c2.xhtml", "Deux") + "</ol>"
        self.assertEqual(self.toc(nav_file(toc_nav(li("../c1.xhtml", " ", child)))), [mark("Deux", 1)])


class TestEpubTocHostile(TocCase):
    NCX = ncx_file(point("c2.xhtml", "Secours"))
    LAUGHS = ('<?xml version="1.0"?><!DOCTYPE html [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
              + nav_file(toc_nav(li("../c1.xhtml", "&c;"))))

    def test_an_unreadable_nav_falls_back_on_the_ncx(self):
        for nav in ("<html><nav>", "", '<?xml version="1.0" encoding="inconnu"?><html/>', nav_file('<nav xmlns:x="">' + "</nav>"), '<html><a x:y="1"/></html>'):
            self.assertEqual(self.toc(nav, self.NCX), [mark("Secours", 1)], nav)

    def test_a_nav_that_leads_nowhere_falls_back_on_the_ncx(self):
        self.assertEqual(self.toc(nav_file(toc_nav(li("../absent.xhtml", "Rien"))), self.NCX), [mark("Secours", 1)])

    def test_a_nav_missing_from_the_archive_falls_back_on_the_ncx(self):
        self.assertEqual(self.toc(ncx=self.NCX, manifest=NAV_ITEM.format("absent.xhtml")), [mark("Secours", 1)])

    def test_broken_files_give_an_empty_table_and_the_book_still_opens(self):
        book = books.epub_book(self.book("<html><nav>", "<ncx><navMap>"))
        self.assertEqual((book["toc"], book["spine"], book["title"]), ([], ["c1.xhtml", "c2.xhtml", "c3.xhtml"], "Un roman"))

    def test_entity_declarations_are_refused_and_never_expanded(self):
        self.assertEqual(self.toc(self.LAUGHS), [])
        self.assertEqual(self.toc(self.LAUGHS, self.NCX), [mark("Secours", 1)])

    def test_an_oversized_file_is_not_read(self):
        with mock.patch.object(books, "MAX_TOC_BYTES", len(self.NCX)):
            self.assertEqual(self.toc(nav_file(toc_nav(li("../c1.xhtml", "Un"))), self.NCX), [mark("Secours", 1)])

    def test_a_compressed_file_lying_about_its_size_is_not_inflated(self):
        path = self.book(ncx=self.NCX, manifest=NAV_ITEM.format("nav.xhtml"))
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr("nav.xhtml", b" " * 50_000_000)
        raw = bytearray(path.read_bytes())
        size_field = raw.rfind(b"nav.xhtml") - 46 + 24                              # la taille annoncée, dans le répertoire central
        raw[size_field:size_field + 4] = (100).to_bytes(4, "little")
        path.write_bytes(raw)
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        self.assertEqual(books.epub_book(path)["toc"], [mark("Secours", 1)])
        self.assertLess(tracemalloc.get_traced_memory()[1], 3 * books.MAX_TOC_BYTES)

    def test_a_file_of_exactly_the_limit_is_read(self):
        nav = nav_file(toc_nav(li("../c1.xhtml", "Un")))
        with mock.patch.object(books, "MAX_TOC_BYTES", len(nav)):
            self.assertEqual(self.toc(nav), [mark("Un", 0)])

    def test_a_very_deep_nesting_is_bounded_without_failing(self):
        depth = 20000
        nav = nav_file(toc_nav('<li><a href="../c1.xhtml">N</a><ol>' * depth + "</ol></li>" * depth))
        found = self.toc(nav)
        self.assertEqual([e["level"] for e in found], list(range(books.MAX_TOC_DEPTH)))

    def test_deep_levels_of_removed_entries_do_not_use_up_the_depth(self):
        depth = books.MAX_TOC_DEPTH + 5
        nav = nav_file(toc_nav('<li><span>P</span><ol>' * depth + li("../c1.xhtml", "Un") + "</ol></li>" * depth))
        self.assertEqual(self.toc(nav), [mark("Un", 0)])

    def test_a_sibling_after_too_deep_a_branch_is_kept(self):
        depth = books.MAX_TOC_DEPTH + 2
        nav = nav_file(toc_nav('<li><a href="../c1.xhtml">N</a><ol>' * depth + "</ol></li>" * depth + li("../c2.xhtml", "Suite")))
        found = self.toc(nav)
        self.assertEqual(len(found), books.MAX_TOC_DEPTH + 1)
        self.assertEqual(found[-1], mark("Suite", 1))

    def test_the_number_of_entries_is_bounded(self):
        nav = nav_file(toc_nav(li("../c1.xhtml", "N") * (books.MAX_TOC_ENTRIES + 10)))
        self.assertEqual(len(self.toc(nav)), books.MAX_TOC_ENTRIES)


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
