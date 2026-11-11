"""L'interface dans un vrai navigateur (Chromium via Playwright) : navigation à la télécommande (flèches, Entrée, Retour), lecture, reprise, lecteurs.

Sauté si Playwright ou ffmpeg manquent. Ce que ces tests ne peuvent pas garantir : la lecture sur TON téléviseur (codecs, HDR, son) — à essayer sur place.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path
from unittest import mock

from tests import fixtures
from tests.test_server import FakeTmdb, Running
from hub.media import images

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                  # pragma: no cover
    sync_playwright = None

SUBS = "1\n00:00:02,000 --> 00:00:30,000\nPremier <i>sous-titre</i>\n\n2\n00:00:31,000 --> 00:00:50,000\nSecond\n"


def make_media(root):
    films, series = root / "srv" / "Films", root / "srv" / "Series"
    fixtures.write_video(films / "Long (2023)" / "Long (2023).mkv", seconds=90, size="160x90", acodec="ac3", channels=6, subtitle=SUBS)
    fixtures.write_video(films / "Direct (2022)" / "Direct (2022).mkv", seconds=60, size="160x90", acodec="aac", channels=2)
    fixtures.write_video(series / "Vidéo Série" / "Saison 1" / "01 - Un.mkv", seconds=8, size="160x90", acodec="aac", channels=2)
    fixtures.write_video(series / "Vidéo Série" / "Saison 1" / "02 - Deux.mkv", seconds=8, size="160x90", acodec="aac", channels=2)


ARTIST_MBID = "99999999-aaaa-bbbb-cccc-000000000001"
OFFICIAL = "https://daftpunk.example/"
WIKIPEDIA = "https://fr.wikipedia.org/wiki/Daft_Punk"
MUSICBRAINZ = f"https://musicbrainz.org/artist/{ARTIST_MBID}"


def give_artist_a_sheet(db):
    """Donne à Daft Punk (la seule artiste de la médiathèque factice) un identifiant MusicBrainz et une fiche gardée : un site officiel, Wikipédia, MusicBrainz. Son identifiant, et de quoi tout remettre."""
    row = db.one("SELECT id, meta FROM items WHERE lib='musique' AND type='album'")
    meta = json.loads(row["meta"])
    db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps({**meta, "artist_mbid": ARTIST_MBID}), row["id"]))
    db.kv_set(f"artist:{ARTIST_MBID}", {"fetched": "2099-01-01", "info": {"type": "Group", "members": [], "links": [{"label": "Site officiel", "url": OFFICIAL}],
                                                                           "wiki": {"lang": "fr", "title": "Daft Punk", "url": WIKIPEDIA, "extract": "Un duo."}}})

    def undo():
        db.execute("UPDATE items SET meta=? WHERE id=?", (row["meta"], row["id"]))
        db.kv_delete(f"artist:{ARTIST_MBID}")
        db.kv_delete(f"artistlinks:{ARTIST_MBID}")
    return meta["artist_id"], undo


PROSE = 70          # la largeur maximale des textes courants (--prose), en rem


def width_rem(locator):
    """La largeur réellement occupée par l'élément, en rem (pas son `max-width` : un parent plus étroit la réduit)."""
    return locator.evaluate("el => el.getBoundingClientRect().width / parseFloat(getComputedStyle(document.documentElement).fontSize)")


@unittest.skipUnless(sync_playwright and fixtures.FFMPEG, "Playwright ou ffmpeg absent")
class UiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        import pathlib
        root = pathlib.Path(cls.tmp)
        fixtures.build_library(root / "srv")
        make_media(root)
        cls.add_media(root)
        cls.run_ = Running.__new__(Running)           # Running reconstruit la bibliothèque : on la prépare nous-mêmes
        cls.run_.root = root
        cls.start(root)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        cls.base = f"http://127.0.0.1:{cls.run_.port}"

    @classmethod
    def add_media(cls, root):
        """Les films en plus qu'une classe de tests veut dans la médiathèque (les autres n'en ont pas besoin, et leurs comptes resteraient justes)."""

    @classmethod
    def start(cls, root, password=""):
        import threading
        from hub import config
        from hub.server.app import make_server
        cfg = config.load({"HUB_SRV": str(root / "srv"), "HUB_DATA": str(root / "data"), "HUB_REPO": str(fixtures.REPO), "HUB_PORT": "0", "HUB_PASSWORD": password})
        cls.run_.server = make_server(cfg)
        cls.run_.hub = cls.run_.server.hub
        cls.run_.port = cls.run_.server.server_address[1]
        cls.run_.libs = {}
        threading.Thread(target=cls.run_.server.serve_forever, daemon=True).start()
        for lib in cfg.libraries:
            cls.run_.hub.jobs.scan(lib)
        cls.run_.wait_jobs()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1920, "height": 1080})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and not any(code in m.text for code in ("404", "500", "503")) else None)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], "erreurs JavaScript")

    # --- Aides ---------------------------------------------------------------
    def api(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    def item(self, lib, title):
        return next(i for i in self.api(f"/api/library/{lib}")["items"] if i["title"] == title)

    def entries(self, lib, title):
        return self.api("/api/item/" + self.item(lib, title)["id"])["entries"]

    def open(self, hash=""):
        self.page.goto(self.base + "/" + hash)
        self.page.wait_for_selector("#nav .link", state="attached")

    def focused(self):
        return self.page.evaluate("(() => { var e = document.activeElement; return e ? (e.getAttribute('title') || e.getAttribute('aria-label') || e.textContent || e.className).trim() : '' })()")

    def press(self, *keys, wait=120):
        for key in keys:
            self.page.keyboard.press(key)
            self.page.wait_for_timeout(wait)

    def view(self, selector="", **kw):
        return self.page.locator(f"#views > .view:visible {selector}".strip(), **kw)

    def shown(self, hash):
        """Attend que la page d'adresse `hash` soit affichée. L'adresse change un instant avant que la page suive (`hashchange`) : jusque-là, `view()` désigne encore l'ancienne page, cartes et titre compris."""
        self.page.wait_for_function("h => { var c = window.Hub.current(); return !!c && c.hash === h && c.el.style.display !== 'none'; }", arg=hash)

    def until(self, condition, tries=40):
        """Attend (au plus quelques secondes) que `condition()` devienne vraie : l'écran ou le serveur ne suivent pas à l'instant du clic."""
        for _ in range(tries):
            if condition():
                return
            self.page.wait_for_timeout(100)
        self.fail("la condition n'est jamais devenue vraie")

    def video_time(self):
        return self.page.evaluate("document.querySelector('.player video').currentTime")

    def wait_playing(self, timeout=20000):
        self.page.wait_for_function("(() => { var v = document.querySelector('.player video'); return v && v.currentTime > 0.6 && !v.paused })()", timeout=timeout)


class TestNavigation(UiCase):
    def test_home_shelves_and_remote_navigation(self):
        self.open()
        self.view(".shelf").first.wait_for()
        self.assertIn("Nouveautés", self.view(".shelf h2").all_inner_texts())
        first = self.focused()
        self.assertNotIn("Médiathèque", first)                      # le focus est dans le contenu, pas sur la barre du haut
        self.press("ArrowRight")
        second = self.focused()
        self.assertNotEqual(first, second)
        self.press("ArrowDown", "ArrowDown")
        self.assertNotEqual(self.focused(), second)
        self.press("ArrowLeft", "ArrowLeft", "ArrowLeft")           # jamais bloqué dans un coin
        self.assertNotEqual(self.focused(), "")

    def test_enter_opens_detail_and_back_restores_focus(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        self.press("ArrowRight", "ArrowRight")
        title = self.focused()
        self.press("Enter", wait=600)
        self.assertIn("#/i/", self.page.url)
        self.assertEqual(self.view("h1").first.inner_text(), title)
        self.press("Escape", wait=500)
        self.assertTrue(self.page.url.endswith("#/l/films"))
        self.assertEqual(self.focused(), title)

    def test_filter_genre_menu_and_back_closes_menu_only(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        total = self.view(".grid .card").count()
        self.view("input[type=search]").fill("dceu")
        self.page.wait_for_timeout(700)
        self.assertEqual(self.view(".grid .card.saga").count(), 1)          # les films sont regroupés en sagas : une seule vignette
        self.view("input[type=search]").fill("")
        self.page.wait_for_timeout(700)
        self.assertEqual(self.view(".grid .card").count(), total)
        self.view(".tools .btn", ).first.click()                    # « Genre »
        self.page.wait_for_selector(".veil .opt")
        url = self.page.url
        self.press("Escape", wait=400)
        self.assertEqual(self.page.locator(".veil").count(), 0)
        self.assertEqual(self.page.url, url)
        self.view(".tools .btn").first.click()
        genre = self.page.locator(".veil .opt .lbl").nth(1).inner_text()
        self.page.locator(".veil .opt").nth(1).click()
        self.page.wait_for_timeout(600)
        self.assertEqual(self.view(".tools .btn.flt").first.inner_text(), "Genre : " + genre)
        self.assertTrue(0 < self.view(".grid .card").count() <= total)
        self.assertEqual(self.page.locator(".veil").count(), 0)

    def test_filter_typing_keeps_focus_in_field(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        field = self.view("input[type=search]")
        field.click()
        self.page.keyboard.type("dc", delay=150)
        self.page.wait_for_timeout(700)                                     # le filtre a relancé la liste
        self.assertTrue(field.evaluate("el => el === document.activeElement"))
        self.page.keyboard.type("eu")
        self.assertEqual(field.input_value(), "dceu")

    def test_search_page(self):
        self.open("#/search")
        self.page.fill(".view:visible input[type=search]", "dark")
        self.page.wait_for_selector(".view:visible .results .card")
        self.assertEqual(self.view(".results .card").first.get_attribute("title"), "Dark")

    def test_typing_the_digit_4_is_not_the_back_key(self):
        # Une vraie frappe (keydown) : `fill()` n'en envoie pas. « 4 » est un code de la touche Retour de certaines télécommandes, pas le caractère.
        self.open("#/search")
        field = self.page.locator(".view:visible input[type=search]")
        field.click()
        self.page.keyboard.type("4 mariages", delay=30)
        self.assertEqual(self.page.evaluate("location.hash"), "#/search")
        self.assertEqual(field.input_value(), "4 mariages")

    def test_no_horizontal_overflow_on_phone(self):
        self.context.close()
        self.context = self.browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        for hash in ("", "#/l/films", "#/l/musique", "#/admin", "#/search"):
            self.open(hash)
            self.page.wait_for_timeout(600)
            self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 391, hash)


class TestDetailPages(UiCase):
    def test_show_seasons_and_episodes(self):
        item = self.item("series", "Dark")
        self.open("#/i/" + item["id"])
        self.view(".ep").first.wait_for()
        self.assertEqual(self.view(".ep").count(), 2)
        self.view(".chip", has_text="Saison 2").click()
        self.assertEqual(self.view(".ep").count(), 1)
        self.assertIn("Retour", self.view(".ep").first.inner_text())

    def test_movie_detail_saga_and_cast(self):
        self.open("#/i/" + self.item("films", "Man of Steel")["id"])
        self.view(".hero").wait_for()
        self.assertEqual(self.view(".sagabox .card").count(), 2)     # la saga

    def test_album_plays_in_the_bar_and_moves_to_next_track(self):
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        self.view(".actions .btn").first.click()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.assertEqual(self.page.inner_text("#abar .ti"), "One More Time")
        self.assertEqual(self.page.locator(".trk.now").count(), 1)
        self.page.locator("#abar .btn").nth(2).click()               # suivant
        self.page.wait_for_function("document.querySelector('#abar .ti').textContent !== 'One More Time'")     # les pistes d'essai durent 1 s : la file avance vite
        self.assertEqual(self.page.locator(".trk.now").count(), 1)
        self.page.locator("#abar .btn").last.click()                 # fermer
        self.assertTrue(self.page.locator("#abar").is_hidden())

    def test_a_listened_track_shows_no_check_mark(self):
        track = self.entries("musique", "Discovery")[0]["id"]
        self.api("/api/progress", {"entry": track, "position": 3, "duration": 3, "done": True})        # écoutée jusqu'au bout
        self.addCleanup(lambda: self.api(f"/api/entry/{track}/seen", {"done": False}))
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        self.assertEqual(self.view(".trk .seen").count(), 0)

    def test_guests_of_a_track_come_from_its_artist_or_its_title(self):
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        guests = lambda title, artist: self.page.evaluate("([t, a]) => Hub.music.guests(t, a)", [title, artist])
        self.assertEqual(guests("Protovision", "Kavinsky feat. Havoc"), {"title": "Protovision", "guests": "Havoc"})
        self.assertEqual(guests("Alive 1997 (feat. Quelqu'un)", "Mouss"), {"title": "Alive 1997", "guests": "Quelqu'un"})
        self.assertEqual(guests("One More Time", "Daft Punk"), {"title": "One More Time", "guests": ""})
        self.assertEqual(self.view(".trk .ti small").count(), 0)           # aucune piste de l'album n'a d'invité : rien sous les titres

    def test_the_line_under_a_track_title_follows_the_album_artist(self):
        self.open("")
        credit = lambda title, artist, album: self.page.evaluate("([t, a, b]) => Hub.music.credit(t, a, b)", [title, artist, album])
        self.assertEqual(credit("One More Time", "Daft Punk", "Daft Punk"), {"title": "One More Time", "line": ""})
        self.assertEqual(credit("Protovision", "Kavinsky feat. Havoc", "Kavinsky"), {"title": "Protovision", "line": "feat. Havoc"})
        self.assertEqual(credit("Alive 1997 (feat. Quelqu'un)", "Mouss", "mouss "), {"title": "Alive 1997", "line": "feat. Quelqu'un"})
        self.assertEqual(credit("New Tristram", "Russell Brower, Laurence Juber & Matt Uelmen", "Various Artists"), {"title": "New Tristram", "line": "Russell Brower, Laurence Juber & Matt Uelmen"})
        self.assertEqual(credit("Titre (feat. Invité)", "Mouss", "Various Artists"), {"title": "Titre", "line": "Mouss feat. Invité"})
        self.assertEqual(credit("Titre", "", "Various Artists"), {"title": "Titre", "line": ""})

    def test_the_line_under_a_track_title_lists_the_guests_of_artists_without_any_link(self):
        self.open("")
        ids = {"BABYMETAL": "id-baby", "Poppy": "id-poppy", "Electric Callboy": "id-callboy", "Polyphia": "id-poly", "Bloodywood": "id-blood", "A": "id-a", "B": "id-b", "Guest": "id-guest"}
        named = lambda *names: [{"name": n, "id": ids.get(n, "")} for n in names]
        credit = lambda artist, artists, album_ids, album="BABYMETAL", title="Titre": self.page.evaluate("([t, a, b, l, i]) => Hub.music.credit(t, a, b, l, i)", [title, artist, album, artists, album_ids])
        baby = ["id-baby"]
        for artist in ("BABYMETAL feat. Poppy", "BABYMETAL x Poppy", "BABYMETAL & Poppy", "BABYMETAL / Poppy"):        # seule la liste compte, pas la liaison
            self.assertEqual(credit(artist, named("BABYMETAL", "Poppy"), baby), {"title": "Titre", "line": "Poppy"}, artist)
        self.assertEqual(credit("BABYMETAL feat. Polyphia", named("BABYMETAL", "Polyphia"), baby)["line"], "Polyphia")
        self.assertEqual(credit("BABYMETAL x Electric Callboy", named("BABYMETAL", "Electric Callboy"), baby)["line"], "Electric Callboy")
        self.assertEqual(credit("BABYMETAL feat. Poppy, Bloodywood", named("BABYMETAL", "Poppy", "Bloodywood"), baby)["line"], "Poppy, Bloodywood")
        self.assertEqual(credit("A, B", named("A", "B"), [])["line"], "A, B")                                # ni l'un ni l'autre n'est l'artiste de l'album (une compilation) : comme avant
        self.assertEqual(credit("BABYMETAL", named("BABYMETAL"), baby), {"title": "Titre", "line": ""})        # la piste de l'artiste seul : rien
        self.assertEqual(credit("A & B feat. Guest", named("A", "B", "Guest"), ["id-a", "id-b"], album="A & B")["line"], "Guest")     # un artiste d'album à deux identifiants : seul l'invité reste
        self.assertEqual(credit("A & B", named("A", "B"), ["id-a", "id-b"], album="A & B")["line"], "")
        self.assertEqual(credit("BABYMETAL feat. Poppy", named("BABYMETAL", "Poppy"), ["id-autre"], album="Autre")["line"], "BABYMETAL feat. Poppy")        # l'artiste de l'album n'est pas de la piste : comme avant

    def test_without_identifiers_the_guests_are_found_by_name(self):
        self.open("")
        credit = lambda artist, artists, album_ids, album: self.page.evaluate("([a, b, l, i]) => Hub.music.credit('Titre', a, b, l, i)", [artist, album, artists, album_ids])
        unpaired = [{"name": "BABYMETAL", "id": ""}, {"name": "Poppy", "id": ""}]          # ARTISTS et MUSICBRAINZ_ARTISTID de longueurs différentes : le hub n'en associe aucun
        self.assertEqual(credit("BABYMETAL feat. Poppy", unpaired, ["id-baby"], "babymetal ")["line"], "Poppy")
        self.assertEqual(credit("BABYMETAL feat. Poppy", unpaired, [], "BABYMETAL")["line"], "Poppy")
        self.assertEqual(credit("BABYMETAL feat. Poppy", [{"name": "BABYMETAL", "id": "id-baby"}, {"name": "Poppy", "id": ""}], ["id-baby"], "BABYMETAL")["line"], "Poppy")      # un seul identifiant manque : toute la piste se compare par les noms
        self.assertEqual(credit("Various x Poppy", unpaired, ["id-va"], "Various Artists")["line"], "Various x Poppy")      # compilation : comme avant

    def test_a_file_without_artists_keeps_the_rule_on_the_artist_tag(self):
        self.open("")
        credit = lambda title, artist, album, artists=None: self.page.evaluate("([t, a, b, l]) => Hub.music.credit(t, a, b, l, ['id-kav'])", [title, artist, album, artists])
        for artists in (None, []):
            self.assertEqual(credit("Protovision", "Kavinsky feat. Havoc", "Kavinsky", artists), {"title": "Protovision", "line": "feat. Havoc"})
            self.assertEqual(credit("New Tristram", "Russell Brower & Matt Uelmen", "Various Artists", artists), {"title": "New Tristram", "line": "Russell Brower & Matt Uelmen"})
            self.assertEqual(credit("Titre (feat. X)", "Kavinsky", "Kavinsky", artists), {"title": "Titre", "line": "feat. X"})
        solo = [{"name": "Kavinsky", "id": "id-kav"}]          # ARTISTS n'y donne aucun invité : celui du titre reste
        self.assertEqual(credit("Titre (feat. X)", "Kavinsky", "Kavinsky", solo), {"title": "Titre", "line": "feat. X"})

    def set_track_artists(self, album, album_artist, artists, credits=None, album_ids=None):
        """Donne aux pistes de l'album les artistes `artists` (dans l'ordre) et à l'album l'artiste `album_artist` ; tout est remis après.
        `credits` : la liste ARTISTS de chaque piste ([{name, id}]), `album_ids` : les identifiants de l'artiste de l'album."""
        db = self.run_.hub.db
        rows = db.query("SELECT id, meta FROM entries WHERE item_id=? ORDER BY number", (album["id"],))
        row = db.one("SELECT grp, meta FROM items WHERE id=?", (album["id"],))
        self.addCleanup(db.execute, "UPDATE items SET grp=?, meta=? WHERE id=?", (row["grp"], row["meta"], album["id"]))
        for r in rows:
            self.addCleanup(db.execute, "UPDATE entries SET meta=? WHERE id=?", (r["meta"], r["id"]))
        for r, artist in zip(rows, artists):
            meta = json.loads(r["meta"])
            meta.setdefault("info", {})["artist"] = artist
            db.execute("UPDATE entries SET meta=? WHERE id=?", (json.dumps(meta), r["id"]))
        for r, listed in zip(rows, credits or []):
            meta = json.loads(db.one("SELECT meta FROM entries WHERE id=?", (r["id"],))["meta"])
            meta["info"]["artists"] = listed
            db.execute("UPDATE entries SET meta=? WHERE id=?", (json.dumps(meta), r["id"]))
        meta = json.loads(row["meta"])
        meta["artist"] = album_artist
        if album_ids:
            meta["artist_mbids"] = album_ids
        db.execute("UPDATE items SET grp=?, meta=? WHERE id=?", (album_artist, json.dumps(meta), album["id"]))

    def test_a_compilation_shows_the_artist_of_each_track_under_its_title(self):
        album = self.item("musique", "Discovery")
        self.set_track_artists(album, "Various Artists", ["Russell Brower & Laurence Juber", "Matt Uelmen feat. Quelqu'un", "Daft Punk"])
        self.open("#/i/" + album["id"])
        self.view(".trk").first.wait_for()
        self.assertEqual(self.view(".trk .ti small").all_inner_texts()[:3], ["Russell Brower & Laurence Juber", "Matt Uelmen feat. Quelqu'un", "Daft Punk"])

    def test_an_album_of_one_artist_shows_nothing_under_the_titles_but_the_guests(self):
        album = self.item("musique", "Discovery")
        self.set_track_artists(album, "Daft Punk", ["Daft Punk", "Daft Punk feat. Quelqu'un", "daft punk"])
        self.open("#/i/" + album["id"])
        self.view(".trk").first.wait_for()
        self.assertEqual(self.view(".trk .ti small").all_inner_texts(), ["feat. Quelqu'un"])

    def test_the_guests_of_the_artists_list_show_under_the_titles_as_names_only(self):
        album = self.item("musique", "Discovery")
        named = lambda *names: [{"name": n, "id": "id-" + n.lower().replace(" ", "")} for n in names]
        self.set_track_artists(album, "BABYMETAL", ["BABYMETAL feat. Poppy", "BABYMETAL x Electric Callboy", "BABYMETAL"],
                               credits=[named("BABYMETAL", "Poppy"), named("BABYMETAL", "Electric Callboy"), named("BABYMETAL")], album_ids=["id-babymetal"])
        self.open("#/i/" + album["id"])
        self.view(".trk").first.wait_for()
        self.assertEqual(self.view(".trk .ti small").all_inner_texts(), ["Poppy", "Electric Callboy"])

    def set_discs(self, album, discs, titles=None):
        """Range les pistes de l'album sur les disques `discs` (un numéro par piste, dans l'ordre) et donne à l'album `titles` ({"2": "Bonus"}) comme titres de disques ; tout est remis après."""
        db = self.run_.hub.db
        rows = db.query("SELECT id, disc FROM entries WHERE item_id=? ORDER BY number", (album["id"],))
        row = db.one("SELECT meta FROM items WHERE id=?", (album["id"],))
        self.addCleanup(db.execute, "UPDATE items SET meta=? WHERE id=?", (row["meta"], album["id"]))
        for r in rows:
            self.addCleanup(db.execute, "UPDATE entries SET disc=? WHERE id=?", (r["disc"], r["id"]))
        for r, disc in zip(rows, discs):
            db.execute("UPDATE entries SET disc=? WHERE id=?", (disc, r["id"]))
        meta = json.loads(row["meta"])
        meta["discs"] = len(set(discs))
        if titles is not None:
            meta["disc_titles"] = titles
        db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), album["id"]))

    def disc_headings(self, album):
        self.open("#/i/" + album["id"])
        self.view(".trk").first.wait_for()
        return self.view("h3.grp").all_inner_texts()

    def test_the_title_of_a_disc_shows_after_its_number(self):
        album = self.item("musique", "Discovery")
        self.set_discs(album, [1, 1, 2], {"2": "Bonus"})
        self.assertEqual(self.disc_headings(album), ["Disque 1", "Disque 2 · Bonus"])

    def test_discs_without_a_title_keep_their_number_alone(self):
        album = self.item("musique", "Discovery")
        self.set_discs(album, [1, 1, 2])
        self.assertEqual(self.disc_headings(album), ["Disque 1", "Disque 2"])

    def test_an_album_of_one_disc_shows_no_disc_heading_even_with_a_title(self):
        album = self.item("musique", "Discovery")
        self.set_discs(album, [1, 1, 1], {"1": "Live à Paris"})
        self.assertEqual(self.disc_headings(album), [])

    def test_the_title_of_a_disc_that_is_not_in_the_album_is_ignored(self):
        album = self.item("musique", "Discovery")
        self.set_discs(album, [1, 1, 2], {"2": 7, "3": "Fantôme", "1": ""})
        self.assertEqual(self.disc_headings(album), ["Disque 1", "Disque 2"])

    def album_facts(self, album, types):
        """La ligne de faits de la fiche de l'album, quand l'album porte les types `types` (RELEASETYPE) ; l'album est remis après."""
        db = self.run_.hub.db
        row = db.one("SELECT meta FROM items WHERE id=?", (album["id"],))
        self.addCleanup(db.execute, "UPDATE items SET meta=? WHERE id=?", (row["meta"], album["id"]))
        if types is not None:
            db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps({**json.loads(row["meta"]), "release_types": types}), album["id"]))
        self.page.goto("about:blank")                                       # une même adresse à « # » près ne recharge pas la page : la fiche garderait l'ancien type
        self.open("#/i/" + album["id"])
        self.view(".trk").first.wait_for()
        return self.view(".info .meta").inner_text()

    def test_the_facts_line_of_an_album_ends_with_its_type(self):
        album = self.item("musique", "Discovery")
        self.assertEqual(self.album_facts(album, ["album"]), "2001 · 3 pistes · FLAC · Album")

    def test_the_facts_line_names_each_type_in_french(self):
        album = self.item("musique", "Discovery")
        for types, shown in ((["single"], "Single"), (["ep"], "EP"), (["album", "live"], "Album, Live"), (["soundtrack"], "Bande originale"),
                             (["compilation", "remix"], "Compilation, Remix"), (["dj-mix"], "Dj-mix")):
            with self.subTest(types):
                self.assertTrue(self.album_facts(album, types).endswith(" · FLAC · " + shown))

    def test_the_facts_line_of_an_album_without_type_is_unchanged(self):
        album = self.item("musique", "Discovery")
        self.assertEqual(self.album_facts(album, None), "2001 · 3 pistes · FLAC")
        self.assertEqual(self.album_facts(album, [7, ""]), "2001 · 3 pistes · FLAC")

    def test_admin_runs_an_update(self):
        self.open("#/admin")
        self.view(".panel").first.wait_for()
        self.view("button", has_text="Tout mettre à jour").click()
        status = self.view(".job", has_text="Mise à jour · tout").locator(".jt span")       # la liste montre d'abord les mises à jour du démarrage, terminées : c'est la nouvelle qu'on attend
        status.wait_for()
        for _ in range(60):
            if status.inner_text() == "terminée":
                break
            self.page.wait_for_timeout(500)
        self.assertEqual(status.inner_text(), "terminée")


class TestVideoPlayer(UiCase):
    def begin(self, lib, title):
        entry = self.entries(lib, title)[0]["id"]
        self.open("#/play/" + entry)
        self.page.wait_for_selector(".player video")
        return entry

    def test_ac3_file_is_converted_and_plays_with_subtitles(self):
        self.begin("films", "Long")
        self.wait_playing()
        self.assertEqual(self.page.inner_text(".player .pill"), "Son converti")
        self.page.locator(".player .btn", has_text="Sous-titres").click()
        self.page.wait_for_selector(".veil .opt")
        self.page.locator(".veil .opt").nth(1).click()
        self.page.wait_for_function("document.querySelector('.player .cue').textContent.indexOf('Premier') >= 0", timeout=15000)
        self.assertEqual(self.page.locator(".cue i").count(), 1)      # l'italique est gardé
        self.assertIsNotNone(self.page.evaluate("localStorage.getItem('hub.subLang')"))     # le choix est mémorisé

    def test_remote_seek_when_controls_hidden_and_resume(self):
        self.begin("films", "Direct")
        self.wait_playing()
        self.page.wait_for_function("document.querySelector('.player .ctl').hidden", timeout=9000)
        before = self.video_time()
        self.press("ArrowRight", "ArrowRight", "ArrowRight", wait=80)
        self.page.wait_for_timeout(1200)
        self.assertGreater(self.video_time(), before + 25)
        self.press("Enter", wait=300)                                  # pause
        self.assertTrue(self.page.evaluate("document.querySelector('.player video').paused"))
        self.page.wait_for_timeout(300)
        saved = self.api("/api/item/" + self.item("films", "Direct")["id"])["entries"][0]["progress"]
        self.assertGreater(saved["position"], 10)
        self.press("Escape", wait=600)
        self.assertIn("#/", self.page.url)
        # la fiche propose de reprendre
        self.open("#/i/" + self.item("films", "Direct")["id"])
        self.assertIn("Reprendre", self.view(".actions .btn").first.inner_text())
        self.assertGreater(self.api("/api/home")["continue"][0]["position"], 10)

    def test_audio_menu_and_quality_menu_open_and_close(self):
        self.begin("films", "Long")
        self.wait_playing()
        self.page.locator(".player .btn", has_text="Qualité").click()
        self.page.wait_for_selector(".veil .opt")
        self.assertEqual(self.page.locator(".veil .opt").count(), 4)
        self.press("Escape", wait=300)
        self.assertEqual(self.page.locator(".veil").count(), 0)
        self.assertIn("#/play/", self.page.url)                        # Retour a fermé le menu, pas le film

    def test_end_of_episode_goes_to_next_one(self):
        eps = self.entries("series", "Vidéo Série")
        self.open("#/play/" + eps[0]["id"])
        self.wait_playing()
        self.page.evaluate("(() => { var v = document.querySelector('.player video'); v.currentTime = v.duration - 0.4 })()")
        self.page.wait_for_function(f"location.hash.indexOf('{eps[1]['id']}') >= 0", timeout=15000)
        self.assertTrue(self.api("/api/item/" + self.item("series", "Vidéo Série")["id"])["entries"][0]["progress"]["done"])
        self.page.wait_for_selector(".player video")
        self.press("Escape", wait=600)
        self.assertNotIn("#/play/", self.page.url)                     # Retour ne rejoue pas l'épisode précédent

    def test_unplayable_file_shows_error_with_actions(self):
        broken = self.entries("films", "Sans Etiquette")[0]["id"]
        self.open("#/play/" + broken)
        self.page.wait_for_selector(".player .err:not([hidden])", timeout=15000)
        self.assertIn("Lecture impossible", self.page.inner_text(".player .err"))


class TestVideoResume(UiCase):
    """Reprendre un film dont le son est converti : l'image est recopiée, le flux ne peut partir que d'une image clé, et le lecteur compte sa position depuis celle-là (sinon l'horloge et les sous-titres glissent)."""

    @classmethod
    def add_media(cls, root):
        fixtures.write_video(root / "srv" / "Films" / "Reprise (2024)" / "Reprise (2024).mkv", seconds=60, size="160x90", acodec="ac3", channels=6)

    def test_the_stream_restarts_from_the_keyframe_the_server_names_and_the_clock_counts_from_there(self):
        entry = self.entries("films", "Reprise")[0]["id"]
        self.api("/api/progress", {"entry": entry, "position": 40.3, "duration": 60, "done": False})
        start = self.api(f"/api/play/{entry}/start?t=40.3")["start"]
        self.assertLess(start, 40.3)                                                   # l'image clé précède l'instant de la reprise : c'est ce que l'ancien lecteur ignorait
        self.assertGreater(start, 39.7)
        self.open("#/play/" + entry)
        self.page.wait_for_selector(".player video")
        self.wait_playing()
        self.assertEqual(self.page.inner_text(".player .pill"), "Son converti")
        self.assertIn(f"&start={start:.3f}", self.page.evaluate("document.querySelector('.player video').src"))
        shown = self.page.evaluate("document.querySelector('.player .times span').textContent")       # « 0:40 » : l'horloge part de l'image clé, pas de l'instant demandé
        self.assertEqual(shown, "0:40")


def ycc(y, cr, cb):
    """Le rouge, vert, bleu d'une entrée de palette PGS (norme BT.709, image HD), comme le décodeur les calcule."""
    l = 1.164 * (y - 16)
    return [l + 1.793 * (cr - 128), l - 0.213 * (cb - 128) - 0.533 * (cr - 128), l + 2.112 * (cb - 128)]


SUP_ROWS = [[(1, 1), (2, 70), (0, 9)], [(3, 64), (0, 16)]]                      # 80 × 2 : un rouge, 70 verts, 9 vides ; 64 bleus à moitié transparents, 16 vides
SUP_PALETTE = {1: fixtures.RED, 2: fixtures.GREEN, 3: fixtures.HALF_BLUE}


class TestPgsDecoder(UiCase):
    """pgs.js : un .sup fabriqué octet par octet, décodé et dessiné par la page."""
    DRAW = """([b64, at]) => {
      var bytes = Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); });
      var track = Hub.pgs.parse(bytes.buffer), canvas = document.createElement('canvas');
      Hub.pgs.draw(canvas, track, Hub.pgs.at(track, at));
      var ctx = canvas.getContext('2d');
      return { size: [track.width, track.height], sets: track.sets.length,
               shown: [[100, 900], [101, 900], [170, 900], [171, 900], [100, 901], [163, 901], [164, 901], [102, 900], [109, 900], [110, 900]].map(function (p) { return Array.from(ctx.getImageData(p[0], p[1], 1, 1).data); }) };
    }"""

    def draw(self, sup, at=3):
        import base64
        self.open()
        return self.page.evaluate(self.DRAW, [base64.b64encode(sup).decode(), at])

    def assertPixel(self, got, rgb, alpha):
        self.assertEqual(got[3], alpha, got)
        for channel, expected in zip(got[:3], rgb):
            self.assertAlmostEqual(channel, max(0, min(255, expected)), delta=3, msg=got)

    def test_runs_palette_and_transparency_are_decoded(self):
        sup = fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE) + fixtures.pgs_clear(15)
        out = self.draw(sup)
        self.assertEqual((out["size"], out["sets"]), ([1920, 1080], 2))
        red, green, blue = ycc(*fixtures.RED[:3]), ycc(*fixtures.GREEN[:3]), ycc(*fixtures.HALF_BLUE[:3])
        shown = out["shown"]
        self.assertPixel(shown[0], red, 255)           # l'octet seul
        self.assertPixel(shown[1], green, 255)         # le début de la répétition de 70
        self.assertPixel(shown[2], green, 255)         # sa fin
        self.assertEqual(shown[3], [0, 0, 0, 0])       # les 9 pixels vides qui suivent
        self.assertPixel(shown[4], blue, 128)          # la deuxième ligne : une répétition de 64, la transparence de la palette est gardée
        self.assertPixel(shown[5], blue, 128)
        self.assertEqual(shown[6], [0, 0, 0, 0])

    def test_an_image_sent_in_two_pieces_is_the_same_image(self):
        whole = self.draw(fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE))["shown"]
        pieces = self.draw(fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE, fragments=2))["shown"]
        self.assertEqual(pieces, whole)
        self.assertEqual(whole[0][3], 255)

    def test_a_cropped_object_shows_only_its_part_at_its_position(self):
        out = self.draw(fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE, crop=(2, 0, 10, 1)))["shown"]
        green = ycc(*fixtures.GREEN[:3])
        self.assertPixel(out[0], green, 255)                          # le pixel n° 2 de l'image, posé à l'endroit de l'objet
        self.assertPixel(out[8], green, 255)                          # (109, 900) : le dernier des 10 pixels gardés
        self.assertEqual(out[9], [0, 0, 0, 0])                        # (110, 900) : déjà hors de la découpe
        self.assertEqual(out[4], [0, 0, 0, 0])                        # la deuxième ligne est découpée

    def test_what_is_on_screen_depends_on_the_time(self):
        import base64
        self.open()
        sup = base64.b64encode(fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE) + fixtures.pgs_clear(15) + fixtures.pgs_show(20, SUP_ROWS, SUP_PALETTE)).decode()
        shown = self.page.evaluate("""(b64) => {
          var bytes = Uint8Array.from(atob(b64), function (c) { return c.charCodeAt(0); }), track = Hub.pgs.parse(bytes.buffer);
          return [0, 1.99, 2, 14.9, 15, 19, 20, 24.9, 25.1].map(function (t) { var s = Hub.pgs.at(track, t); return s ? s.s : null; });
        }""", sup)
        self.assertEqual(shown, [None, None, 2, 2, None, None, 20, 20, None])       # sans suite, un ensemble ne dure que 5 s

    def test_garbage_is_not_a_subtitle_track(self):
        self.open()
        out = self.page.evaluate("""() => [new ArrayBuffer(0), new Uint8Array([80, 71, 1, 2, 3]).buffer, new Uint8Array(200).buffer].map(function (b) { return Hub.pgs.parse(b).sets.length; })""")
        self.assertEqual(out, [0, 0, 0])


class TestPgsInThePlayer(UiCase):
    @classmethod
    def add_media(cls, root):
        sup = fixtures.pgs_show(2, SUP_ROWS, SUP_PALETTE, x=100, y=900) + fixtures.pgs_clear(15)
        fixtures.write_video(root / "srv" / "Films" / "Image (2024)" / "Image (2024).mkv", seconds=20, size="160x90", acodec="aac", channels=2, sup=sup)

    def pixel(self, x, y):
        return self.page.evaluate("(p) => Array.from(document.querySelector('.player .pgs').getContext('2d').getImageData(p[0], p[1], 1, 1).data)", [x, y])

    def test_the_picture_subtitles_of_a_blu_ray_are_drawn_over_the_film(self):
        entry = self.entries("films", "Image")[0]["id"]
        track = self.api("/api/play/" + entry, {})["subs"][0]
        self.assertEqual((track["text"], track["image"], track["url"]), (False, "pgs", f"/api/subs/{entry}/e0.sup"))
        self.open("#/play/" + entry)
        self.page.wait_for_selector(".player video")
        self.wait_playing()
        self.assertTrue(self.page.locator(".player .pgs").is_hidden())              # jamais choisie d'office : l'extraire peut durer
        self.page.locator(".player .btn", has_text="Sous-titres").click()
        self.page.wait_for_selector(".veil .opt")
        self.assertIn("image · mkv", self.page.locator(".veil .opt").nth(1).inner_text())
        self.assertTrue(self.page.locator(".veil .opt").nth(1).is_enabled())
        self.page.locator(".veil .opt").nth(1).click()
        self.page.wait_for_function("(() => { var c = document.querySelector('.player .pgs'); return !c.hidden && c.getContext('2d').getImageData(150, 900, 1, 1).data[3] > 0 })()", timeout=20000)
        red, green = self.pixel(100, 900), self.pixel(150, 900)
        self.assertGreater(red[0], 230)
        self.assertLess(red[1], 60)
        self.assertGreater(green[1], 150)                                           # le vert de la répétition, au bon endroit de l'écran du Blu-ray
        self.assertEqual(self.pixel(100, 950)[3], 0)
        self.assertEqual(self.page.evaluate("localStorage.getItem('hub.subLang')"), '"any"')     # le choix est mémorisé (la piste n'a pas de langue)
        self.page.locator(".player .btn", has_text="Sous-titres").click()
        self.page.wait_for_selector(".veil .opt")
        self.page.locator(".veil .opt").nth(0).click()                              # « Désactivés »
        self.page.wait_for_function("document.querySelector('.player .pgs').hidden")


class TestReader(UiCase):
    def test_cbz_pages_keys_and_progress(self):
        """Naruto est rangé dans « Manga » : il s'ouvre de droite à gauche, la flèche gauche avance."""
        entry = self.entries("livres", "Naruto")[0]["id"]
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader .page') && document.querySelector('.reader .page').complete && document.querySelector('.reader .page').naturalWidth > 0")
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 1 / 3")
        self.press("ArrowLeft", wait=300)
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 2 / 3")
        self.press("ArrowRight", wait=300)
        self.press("ArrowRight", wait=300)
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 1 / 3")
        self.press("ArrowLeft", wait=300)
        self.page.wait_for_timeout(2000)
        self.assertEqual(self.api("/api/book/" + entry)["position"], 1)
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader .pg').textContent === 'Page 2 / 3'")      # reprend où l'on s'était arrêté
        self.press("ArrowLeft", wait=300)
        self.page.wait_for_timeout(2000)
        self.assertTrue(self.api("/api/item/" + self.item("livres", "Naruto")["id"])["entries"][0]["progress"]["done"])   # dernière page : tome lu

    def test_epub_scrolls_and_changes_chapter(self):
        entry = self.entries("livres", "Dumas")[0]["id"]
        self.open("#/read/" + entry)
        self.page.wait_for_selector(".reader iframe")
        self.page.wait_for_function("document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.assertIn("Chapitre 1 / 2", self.page.evaluate("document.querySelector('.reader .pg').textContent"))
        self.press("ArrowRight", "ArrowRight", wait=500)
        self.assertIn("Chapitre 2", self.page.evaluate("document.querySelector('.reader .pg').textContent"))

    def test_unreadable_format_offers_download(self):
        (self.run_.root / "srv" / "Livres" / "Romans" / "Autre" / "Livre.mobi").parent.mkdir(parents=True, exist_ok=True)
        (self.run_.root / "srv" / "Livres" / "Romans" / "Autre" / "Livre.mobi").write_bytes(b"x" * 100)
        self.api("/api/scan", {"lib": "livres"})
        self.run_.wait_jobs()
        entry = self.entries("livres", "Autre")[0]["id"]
        self.open("#/read/" + entry)
        self.page.wait_for_selector(".reader .err:not([hidden])")
        self.assertIn("Télécharger", self.page.inner_text(".reader .err"))


class TestReaderGamebook(UiCase):
    """Un livre dont vous êtes le héros : les chapitres se renvoient l'un à l'autre, et Retour quitte le livre au lieu d'y reculer."""

    @classmethod
    def add_media(cls, root):
        links = {1: '<p><a href="c3.xhtml">Aller au trois</a></p><p><a href="http://example.org/dehors">Lien dehors</a></p><p><a href="notes.xhtml">Les notes</a></p>',
                 2: '<div style="height:3000px"></div><h2 id="fin">La fin</h2><div style="height:3000px"></div><p><a name="vieux">Le vieil ancrage</a></p><div style="height:2000px"></div>', 3: '<p><a href="c1.xhtml">Retour au un</a></p><p><a href="c2.xhtml#fin">Vers la fin</a></p><p><a href="c2.xhtml#vieux">Vers le vieil ancrage</a></p>'
                    '<p><a href="#bas">Aller en bas</a></p><div style="height:3000px"></div><p id="bas">En bas</p><p><a href="c3.xhtml">Au début</a></p>'}
        fixtures.write_epub(root / "srv" / "Livres" / "Romans" / "Heros" / "Livre-jeu.epub", "Livre-jeu", "Auteur", chapters=3,
                            body=lambda n: f"<h1>Chapitre {n}</h1>{links[n]}")
        fixtures.write_epub(root / "srv" / "Livres" / "Romans" / "Ancres" / "Ancres.epub", "Ancres", "Auteur", chapters=1,          # une ancre nommée (sans href) couvre tout l'écran : elle n'est pas un lien
                            body=lambda n: '<a id="nm" style="position:fixed;left:0;top:0;width:100vw;height:100vh"></a><h1>Chapitre 1</h1><div style="height:4000px"></div>')

    def book(self, title):
        """Le livre, remis au début : les tests se suivent sur le même serveur, et la liseuse reprend où l'on s'était arrêté."""
        entry = self.entries("livres", title)[0]["id"]
        self.api("/api/progress", {"entry": entry, "position": 0, "duration": 4})
        return entry

    def frame(self):
        return self.page.frame_locator(".reader iframe")

    def chapter(self):
        return self.page.evaluate("document.querySelector('.reader .pg').textContent")

    def follow(self, text, label):
        self.frame().locator("a", has_text=text).click()
        self.page.wait_for_function("l => document.querySelector('.reader .pg').textContent.indexOf(l) === 0", arg=label)
        self.page.wait_for_function("t => document.querySelector('.reader iframe').contentDocument.body.textContent.indexOf(t) >= 0", arg=label.split(" / ")[0])

    def test_internal_links_change_chapter_and_back_leaves_the_book(self):
        entry = self.book("Heros")
        self.open("#/i/" + self.item("livres", "Heros")["id"])
        self.page.evaluate("location.hash = '#/read/" + entry + "'")
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.assertIn("Chapitre 1 / 3", self.chapter())
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.follow("Retour au un", "Chapitre 1 / 3")
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.until(lambda: self.api("/api/book/" + entry)["position"] == 2)         # la progression suit le chapitre atteint par le lien
        self.page.locator(".reader .rbar button[aria-label=Retour]").click()
        self.shown("#/i/" + self.item("livres", "Heros")["id"])                     # un seul appui : on a quitté le livre

    def test_a_link_to_an_anchor_in_another_chapter_and_the_links_that_lead_nowhere(self):
        entry = self.book("Heros")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        for text in ("Lien dehors", "Les notes"):                                  # un site extérieur, un fichier du livre qui n'est pas un chapitre : on prévient, on ne bouge pas
            self.frame().locator("a", has_text=text).click()
            self.page.locator("#toast.on", has_text="Ce lien ne mène pas à un chapitre du livre.").wait_for()
            self.page.evaluate("document.getElementById('toast').classList.remove('on')")
            self.assertIn("/epub/c1.xhtml", self.page.evaluate("document.querySelector('.reader iframe').contentWindow.location.href"))
            self.assertIn("Chapitre 1 / 3", self.chapter())
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.follow("Vers la fin", "Chapitre 2 / 3")
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 1000")                # l'ancre est suivie : le chapitre est défilé jusqu'à « La fin »

    def test_a_named_anchor_without_href_does_not_swallow_the_click(self):
        entry = self.book("Ancres")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 5")
        self.frame().locator("#nm").click(position={"x": 1750, "y": 500})          # le bord droit de l'écran : tourner la page, comme partout ailleurs sur le texte
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 100")

    def test_an_anchor_survives_a_theme_change_made_while_the_chapter_loads(self):
        entry = self.book("Heros")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.page.evaluate("""() => {          // d'un seul tour : le thème change avant que le chapitre 2 soit arrivé
          var doc = document.querySelector('.reader iframe').contentDocument;
          Array.from(doc.querySelectorAll('a')).find(function (a) { return a.textContent === 'Vers la fin'; }).click();
          Array.from(document.querySelectorAll('.reader .btns button')).find(function (b) { return b.textContent.indexOf('Thème') >= 0; }).click();
        }""")
        self.page.wait_for_function("l => document.querySelector('.reader .pg').textContent.indexOf(l) === 0", arg="Chapitre 2 / 3")
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 1000")          # l'ancre « La fin » est rejointe quand même

    def test_enter_follows_the_focused_link(self):
        entry = self.book("Heros")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.frame().locator("a", has_text="Aller au trois").focus()               # Tab amène le focus sur un lien, comme sur une page web
        self.press("Enter")
        self.page.wait_for_function("l => document.querySelector('.reader .pg').textContent.indexOf(l) === 0", arg="Chapitre 3 / 3")
        self.assertFalse(self.page.evaluate("document.activeElement === document.querySelector('.reader .seekbar')"))          # Entrée a suivi le lien : elle n'a pas pris le curseur de progression

    def test_a_link_to_the_chapter_already_shown_reloads_it_from_the_top(self):
        entry = self.book("Heros")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.frame().locator("a", has_text="Aller en bas").click()
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 1000")
        self.frame().locator("a", has_text="Au début").click()                     # un lien vers le chapitre lu, sans ancre : on y revient au début
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY === 0")
        self.assertIn("Chapitre 3 / 3", self.chapter())

    def test_an_old_style_named_anchor_is_reached(self):
        entry = self.book("Heros")
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.follow("Vers le vieil ancrage", "Chapitre 2 / 3")                      # <a name="vieux"> (EPUB 2), pas un id
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 5000")

    def test_a_link_to_an_anchor_of_the_same_chapter_scrolls_without_leaving_it_or_stacking_history(self):
        entry = self.book("Heros")
        self.open("#/i/" + self.item("livres", "Heros")["id"])
        self.page.evaluate("location.hash = '#/read/" + entry + "'")
        self.page.wait_for_function("document.querySelector('.reader iframe') && document.querySelector('.reader iframe').contentDocument && document.querySelector('.reader iframe').contentDocument.body && document.querySelector('.reader iframe').contentDocument.body.textContent.length > 20")
        self.follow("Aller au trois", "Chapitre 3 / 3")
        self.frame().locator("a", has_text="Aller en bas").click()
        self.page.wait_for_function("document.querySelector('.reader iframe').contentWindow.scrollY > 1000")
        self.assertIn("Chapitre 3 / 3", self.chapter())                            # le chapitre n'a pas changé (en bas de page, le compteur ne passe pas au suivant)
        self.page.locator(".reader .rbar button[aria-label=Retour]").click()
        self.shown("#/i/" + self.item("livres", "Heros")["id"])                    # l'ancre n'a rien ajouté à l'historique


def solid_png(width, height):
    """Un vrai PNG uni de cette taille : la liseuse décide du couplage des pages d'après leurs dimensions."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\x00" + bytes((200, 90, 60)) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def write_sized_cbz(path, sizes, **info):
    """Un .cbz dont la page n a la taille sizes[n] (largeur, hauteur)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n, (width, height) in enumerate(sizes):
            z.writestr(f"{n:03d}.png", solid_png(width, height))
        z.writestr("ComicInfo.xml", fixtures.comicinfo_xml(**info))


PORTRAIT, LANDSCAPE = (200, 300), (400, 200)


class TestReaderSpreads(UiCase):
    """La liseuse en double page : le couplage des pages, le sens de lecture, la progression, la fenêtre en portrait."""

    @classmethod
    def add_media(cls, root):
        books = root / "srv" / "Livres"
        write_sized_cbz(books / "BD" / "Diptyque" / "Tome 01.cbz", [PORTRAIT] * 3 + [LANDSCAPE] + [PORTRAIT] * 3, Series="Diptyque", Number="1")   # pages 0 à 6, la 3 en paysage
        write_sized_cbz(books / "Manga" / "Droite" / "Tome 01.cbz", [PORTRAIT] * 6, Series="Droite", Number="1")
        write_sized_cbz(books / "Manga" / "Gauche" / "Tome 01.cbz", [PORTRAIT] * 5, Series="Gauche", Number="1", Manga="Yes")
        write_sized_cbz(books / "BD" / "Inverse" / "Tome 01.cbz", [PORTRAIT] * 4, Series="Inverse", Number="1", Manga="YesAndRightToLeft")

    # --- Aides ---------------------------------------------------------------
    def entry(self, title):
        return self.entries("livres", title)[0]["id"]

    def setUp(self):
        super().setUp()
        self.held, self.released = [], True

    def prefs(self, double=True, **pref):
        """Pose le mode double page et les autres réglages dans le navigateur."""
        self.open()
        self.page.evaluate("p => { localStorage.setItem('hub.spread', JSON.stringify(p.double)); Object.keys(p.more).forEach(k => localStorage.setItem('hub.' + k, JSON.stringify(p.more[k]))); }", {"double": double, "more": pref})

    def hold(self, *numbers):
        """Retient les images de ces pages (de tout tome) jusqu'à `release()` : la liseuse les attend, comme sur un réseau lent."""
        self.held, self.released = [], False
        pattern = re.compile(r".*/book/\w+/page/(%s)$" % "|".join(str(n) for n in numbers))
        self.page.route(pattern, lambda route: route.continue_() if self.released else self.held.append(route))

    def release(self):
        self.released = True
        for route in self.held:
            route.continue_()
        self.held = []

    def leave(self):
        self.page.evaluate("location.hash = '#/'")

    def read(self, title, double=True, page=0, **pref):
        """Ouvre le premier tome de la série ; le mode double page et les autres réglages sont posés dans le navigateur avant l'ouverture."""
        entry = self.entry(title)
        if page is not None:
            self.api(f"/api/entry/{entry}/seen", {"done": False})     # la progression d'un test précédent ne compte pas
        self.prefs(double, **pref)
        target = f"#/read/{entry}" + (f"?p={page}" if page is not None else "")           # à une page donnée, ou à la reprise (`page=None`)
        self.page.goto(self.base + "/" + target)
        self.shown(target)
        self.wait_pages(None)
        return entry

    def pages(self):
        """Les numéros des pages à l'écran, de gauche à droite (celles qui sont chargées)."""
        return self.page.evaluate("""Array.from(document.querySelectorAll('.reader .page')).map(i => ({n: +i.getAttribute('src').split('/').pop(), x: i.getBoundingClientRect().left, done: i.complete && i.naturalWidth > 0}))
                                     .sort((a, b) => a.x - b.x).filter(i => i.done).map(i => i.n)""")

    def wait_pages(self, expected):
        """Attend que la liseuse montre ces pages (de gauche à droite), toutes chargées ; `None` : qu'elle en montre, quelles qu'elles soient."""
        self.until(lambda: self.pages() == expected if expected is not None else bool(self.pages()))

    def label(self):
        return self.page.inner_text(".reader .pg")

    def buttons(self):
        return self.page.inner_text(".reader .rbar .btns")

    def turn(self, key):
        self.press(key, wait=250)

    def tool(self, name):
        """Appuie sur le bouton de la barre (qu'on fait apparaître d'un clic au centre si elle est masquée), puis la masque : les flèches tournent de nouveau les pages."""
        center = {"position": {"x": 960, "y": 500}}
        if not self.page.locator(".reader .rbar").is_visible():
            self.page.locator(".reader .stage").click(**center)
        self.page.locator(".reader .rbar .btn", has_text=name).click()
        self.page.locator(".reader .stage").click(**center)

    def tool_hidden(self, name):
        """Le bouton est-il retiré de la barre ? (on regarde l'attribut : la barre elle-même se masque au bout de quelques secondes)"""
        return self.page.evaluate("name => Array.from(document.querySelectorAll('.reader .rbar .btn')).find(b => b.textContent.indexOf(name) >= 0).hidden", name)

    def test_a_late_answer_of_the_home_page_does_not_take_the_focus_off_the_reader(self):
        """L'accueil charge ses rayons en arrière-plan : si la réponse arrive quand on est déjà dans la liseuse, le focus ne doit pas aller sur sa barre (les flèches n'y tournent plus les pages)."""
        home = []
        self.page.route("**/api/home", lambda route: home.append(route))             # retenue : l'accueil répondra après l'ouverture de la liseuse
        self.read("Diptyque")
        self.assertEqual(len(home), 1)
        for route in home:
            route.continue_()
        self.page.wait_for_selector(".home .greeting", state="attached")             # l'accueil a reçu sa réponse (il est masqué : la liseuse est devant)
        self.assertFalse(self.page.evaluate("!!document.activeElement.closest('.rbar')"))
        self.turn("ArrowRight")
        self.wait_pages([1, 2])

    def test_the_double_page_shows_the_cover_alone_then_pairs_of_pages(self):
        self.read("Diptyque")
        self.wait_pages([0])
        self.assertEqual(self.label(), "Page 1 / 7")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        self.assertEqual(self.label(), "Pages 2–3 / 7")
        self.assertEqual(len(self.page.query_selector_all(".reader .page")), 2)

    def test_a_landscape_page_is_shown_alone_and_the_pairs_go_on_after_it(self):
        self.read("Diptyque")
        for expected in ([1, 2], [3], [4, 5], [6]):
            self.turn("ArrowRight")
            self.wait_pages(expected)
        self.assertEqual(self.label(), "Page 7 / 7")

    def test_going_back_gives_the_same_spreads_as_going_forward(self):
        self.read("Diptyque")
        for expected in ([1, 2], [3], [4, 5], [6]):
            self.turn("ArrowRight")
            self.wait_pages(expected)
        for expected in ([4, 5], [3], [1, 2], [0]):
            self.turn("ArrowLeft")
            self.wait_pages(expected)

    def test_a_page_before_a_landscape_one_goes_alone(self):
        self.read("Diptyque", page=2)
        self.wait_pages([2])                           # la page 3 est en paysage : la 2 reste seule

    def test_the_lowest_page_is_on_the_right_when_reading_right_to_left(self):
        self.read("Droite")
        self.assertIn("droite → gauche", self.buttons())
        self.turn("ArrowLeft")                          # de droite à gauche : la flèche gauche avance
        self.wait_pages([2, 1])                         # à l'écran, de gauche à droite : la page 3 puis la page 2
        self.assertEqual(self.label(), "Pages 2–3 / 6")

    def test_the_lowest_page_is_on_the_left_when_reading_left_to_right(self):
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])

    def test_every_way_of_turning_goes_one_spread_forward(self):
        self.read("Gauche")
        self.turn("Space")
        self.wait_pages([1, 2])
        self.turn("PageDown")
        self.wait_pages([3, 4])
        self.turn("PageUp")
        self.wait_pages([1, 2])
        self.page.mouse.click(1800, 500)                # le bord droit : page suivante
        self.wait_pages([3, 4])
        self.page.mouse.click(100, 500)                 # le bord gauche : page précédente
        self.wait_pages([1, 2])

    def test_the_saved_position_is_the_first_page_of_the_spread(self):
        entry = self.read("Droite")
        self.turn("ArrowLeft")
        self.turn("ArrowLeft")
        self.wait_pages([4, 3])
        self.page.wait_for_timeout(2000)
        self.assertEqual(self.api("/api/book/" + entry)["position"], 3)

    def test_a_volume_started_in_single_mode_is_resumed_where_it_stood_in_double_mode(self):
        entry = self.read("Gauche", double=False)
        self.turn("ArrowRight")
        self.turn("ArrowRight")
        self.wait_pages([2])
        self.assertEqual(self.label(), "Page 3 / 5")
        self.tool("Double page")
        self.wait_pages([2, 3])
        self.assertEqual(self.label(), "Pages 3–4 / 5")
        self.page.wait_for_timeout(2000)
        self.assertEqual(self.api("/api/book/" + entry)["position"], 2)
        self.tool("Double page")                        # et inversement : on revient à la page seule
        self.wait_pages([2])
        self.assertEqual(self.page.evaluate("localStorage.getItem('hub.spread')"), "false")

    def test_a_volume_closed_in_one_mode_is_resumed_at_the_same_place_in_the_other(self):
        self.read("Droite", double=False)
        self.turn("ArrowLeft")                          # de droite à gauche : la flèche gauche avance
        self.wait_pages([1])
        self.page.wait_for_timeout(2000)
        self.read("Droite", double=True, page=None)
        self.wait_pages([2, 1])                         # la page 2 (rang 1) est la première de la vue : à droite
        self.assertEqual(self.label(), "Pages 2–3 / 6")
        self.turn("ArrowLeft")
        self.wait_pages([4, 3])
        self.page.wait_for_timeout(2000)
        self.read("Droite", double=False, page=None)
        self.wait_pages([3])
        self.assertEqual(self.label(), "Page 4 / 6")

    def test_the_last_spread_marks_the_volume_as_read(self):
        self.read("Gauche")
        for _ in range(2):
            self.turn("ArrowRight")
        self.wait_pages([3, 4])                         # la dernière page est à l'écran
        self.page.wait_for_timeout(2000)
        progress = self.api("/api/item/" + self.item("livres", "Gauche")["id"])["entries"][0]["progress"]
        self.assertEqual((progress["done"], progress["position"]), (True, 4))        # la page 5 (rang 4) : sans cela, un tome fini en double page ne serait jamais lu

    def test_the_slider_jumps_to_a_page_and_shows_the_spread_from_there(self):
        self.read("Gauche")
        box = self.page.locator(".reader .seekbar").bounding_box()
        self.page.mouse.click(box["x"] + box["width"] * 0.45, box["y"] + box["height"] / 2)    # 45 % de 5 pages : la page 2
        self.wait_pages([2, 3])
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .seekbar i').style.width"), "80%")     # la dernière page vue est la 4e sur 5

    def test_a_window_in_portrait_turns_the_double_page_off_and_wide_again_brings_it_back(self):
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        self.page.set_viewport_size({"width": 800, "height": 1000})
        self.wait_pages([1])
        self.assertTrue(self.tool_hidden("Double page"))
        self.assertEqual(self.label(), "Page 2 / 5")
        self.page.set_viewport_size({"width": 1920, "height": 1080})
        self.wait_pages([1, 2])
        self.assertFalse(self.tool_hidden("Double page"))

    def test_the_window_has_to_be_a_fifth_wider_than_tall(self):
        self.read("Gauche")
        self.turn("ArrowRight")
        self.page.set_viewport_size({"width": 1199, "height": 1000})
        self.wait_pages([1])
        self.page.set_viewport_size({"width": 1200, "height": 1000})
        self.wait_pages([1, 2])

    def test_the_double_page_button_says_its_state_and_starts_off(self):
        self.read("Gauche", double=None)                # aucun réglage mémorisé
        self.wait_pages([0])
        self.assertIn("Double page : non", self.buttons())
        self.tool("Double page")
        self.assertIn("Double page : oui", self.buttons())
        self.assertEqual(self.page.evaluate("localStorage.getItem('hub.spread')"), "true")

    def test_the_shift_button_moves_the_pairs_by_one_page_for_this_session_only(self):
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        self.tool("Décaler d’une page")
        self.wait_pages([0, 1])                          # la couverture se couple avec la page suivante
        self.turn("ArrowRight")
        self.wait_pages([2, 3])
        self.assertIsNone(self.page.evaluate("Object.keys(localStorage).find(k => /shift/i.test(k)) || null"))
        self.read("Gauche")                              # la liseuse rouverte : rien n'est mémorisé
        self.wait_pages([0])

    def test_the_shift_button_is_only_there_in_double_mode(self):
        self.read("Gauche", double=False)
        self.assertTrue(self.tool_hidden("Décaler"))
        self.tool("Double page")
        self.assertFalse(self.tool_hidden("Décaler"))

    def test_the_fit_in_width_shows_the_two_pages_side_by_side_each_half_the_window(self):
        self.read("Gauche", fit="width")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        widths = self.page.evaluate("Array.from(document.querySelectorAll('.reader .page')).map(i => Math.round(i.getBoundingClientRect().width))")
        self.assertEqual(widths, [960, 960])

    def test_the_fit_in_height_keeps_each_page_within_half_the_window_and_the_whole_height(self):
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        sizes = self.page.evaluate("Array.from(document.querySelectorAll('.reader .page')).map(i => { var r = i.getBoundingClientRect(); return [r.width, r.height]; })")
        for width, height in sizes:
            self.assertLessEqual(width, 960.5)
            self.assertLessEqual(height, 1080.5)

    def test_the_end_of_the_volume_is_announced_before_leaving_it(self):
        self.read("Gauche")
        for _ in range(2):
            self.turn("ArrowRight")
        self.wait_pages([3, 4])
        self.turn("ArrowRight")
        self.page.locator("text=Fin du volume").first.wait_for()
        self.wait_pages([3, 4])                         # on reste sur la dernière vue

    def test_going_back_after_a_jump_finds_the_pairs_that_end_just_before(self):
        self.read("Diptyque", page=5)                   # pas d'historique : le couplage est cherché d'après les dimensions des pages
        self.wait_pages([5, 6])
        for expected in ([4], [3], [1, 2], [0]):
            self.turn("ArrowLeft")
            self.wait_pages(expected)

    def test_the_direction_button_turns_the_spread_on_screen_over(self):
        self.read("Droite")
        self.turn("ArrowLeft")
        self.wait_pages([2, 1])
        self.tool("Lecture")
        self.wait_pages([1, 2])

    # --- Images lentes : ce qui se passe pendant qu'une vue se charge ---------
    def test_two_quick_turns_in_single_mode_are_both_counted(self):
        self.hold(1, 2)
        self.read("Gauche", double=False)
        self.press("ArrowRight", wait=100)
        self.press("ArrowRight", wait=100)
        self.release()
        self.wait_pages([2])
        self.assertEqual(self.label(), "Page 3 / 5")

    def test_a_turn_followed_at_once_by_a_turn_back_never_leaves_the_screen_empty(self):
        self.hold(3, 4)
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        self.press("ArrowRight", wait=100)              # [3, 4] se charge...
        self.press("ArrowLeft", wait=100)               # ...et on recule déjà
        self.release()
        self.page.wait_for_timeout(800)
        self.assertEqual((self.pages(), self.label()), ([1, 2], "Pages 2–3 / 5"))

    def test_turns_queued_while_loading_are_not_replayed_after_the_end_of_the_volume(self):
        self.hold(3, 4)
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        for _ in range(4):
            self.press("ArrowRight", wait=50)           # la dernière vue se charge, trois tours attendent derrière elle
        self.release()
        self.wait_pages([3, 4])
        self.page.wait_for_timeout(500)
        self.turn("ArrowLeft")
        self.wait_pages([1, 2])                         # les tours en trop sont perdus, ils ne reviennent pas défaire ce retour
        self.page.wait_for_timeout(500)
        self.assertEqual(self.pages(), [1, 2])

    def test_a_jump_on_the_slider_wins_over_a_step_back_still_looking_for_its_pairs(self):
        self.hold(3, 4)
        self.read("Diptyque", page=5)
        self.wait_pages([5, 6])
        self.press("ArrowLeft", wait=100)               # sans historique : le recul attend les pages 3 et 4
        box = self.page.locator(".reader .seekbar").bounding_box()
        self.page.mouse.click(box["x"] + 2, box["y"] + box["height"] / 2)           # le début de la barre : la page 0
        self.wait_pages([0])
        self.release()
        self.page.wait_for_timeout(600)
        self.assertEqual(self.pages(), [0])

    def test_leaving_before_the_first_view_is_loaded_keeps_the_saved_place(self):
        entry = self.entry("Gauche")
        self.api("/api/progress", {"entry": entry, "position": 3, "duration": 5})
        self.prefs()
        self.hold(3, 4)
        target = f"#/read/{entry}"
        self.page.goto(self.base + "/" + target)
        self.shown(target)
        self.page.wait_for_selector(".reader .rbar .btn")
        self.leave()
        self.release()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.api("/api/book/" + entry)["position"], 3)

    def test_a_window_resized_before_the_first_view_is_loaded_still_resumes_where_it_stood(self):
        entry = self.entry("Gauche")
        self.api("/api/progress", {"entry": entry, "position": 3, "duration": 5})
        self.prefs()
        self.hold(3, 4)
        target = f"#/read/{entry}"
        self.page.goto(self.base + "/" + target)
        self.shown(target)
        self.page.wait_for_selector(".reader .rbar .btn")
        self.page.set_viewport_size({"width": 1900, "height": 1080})
        self.release()
        self.wait_pages([3, 4])
        self.assertEqual(self.label(), "Pages 4–5 / 5")

    def test_a_view_still_loading_when_the_reader_is_closed_is_never_shown_nor_saved(self):
        self.hold(3, 4)
        self.read("Gauche")
        self.turn("ArrowRight")
        self.wait_pages([1, 2])
        self.press("ArrowRight", wait=100)              # la dernière vue se charge...
        self.leave()                                    # ...et on quitte
        self.release()
        self.page.wait_for_timeout(2500)
        progress = self.api("/api/item/" + self.item("livres", "Gauche")["id"])["entries"][0]["progress"]
        self.assertFalse(progress["done"])              # la page 5 n'a jamais été vue

    # --- Le sens de lecture, série par série ---------------------------------
    def test_the_server_gives_the_starting_direction(self):
        self.read("Droite")
        self.assertIn("droite → gauche", self.buttons())            # section Manga
        self.read("Gauche")
        self.assertIn("gauche → droite", self.buttons())            # Manga=Yes l'emporte sur la section
        self.read("Inverse")
        self.assertIn("droite → gauche", self.buttons())            # Manga=YesAndRightToLeft l'emporte sur la section BD
        self.read("Diptyque")
        self.assertIn("gauche → droite", self.buttons())

    def test_the_button_changes_the_direction_of_this_series_and_it_wins_over_the_default(self):
        item = self.item("livres", "Droite")["id"]
        self.read("Droite", double=False)
        self.tool("Lecture")
        self.assertEqual(self.page.evaluate("localStorage.getItem('hub.rtl.%s')" % item), "false")
        self.turn("ArrowRight")
        self.wait_pages([1])                             # gauche → droite : la flèche droite avance
        self.read("Droite", double=False)                # on rouvre le tome : le choix est gardé
        self.assertIn("gauche → droite", self.buttons())
        self.turn("ArrowRight")
        self.wait_pages([1])
        self.read("Inverse", double=False)               # une autre série garde son départ
        self.assertIn("droite → gauche", self.buttons())

    def test_a_series_made_to_read_right_to_left_by_hand_stays_so(self):
        item = self.item("livres", "Diptyque")["id"]
        self.read("Diptyque", double=False, **{"rtl." + item: True})
        self.assertIn("droite → gauche", self.buttons())
        self.turn("ArrowLeft")
        self.wait_pages([1])

    def test_the_old_global_setting_is_no_longer_read(self):
        self.read("Diptyque", double=False, rtl=True)
        self.assertIn("gauche → droite", self.buttons())
        self.turn("ArrowRight")
        self.wait_pages([1])


class TestSagasInLibrary(UiCase):
    def test_saga_card_opens_the_saga_page_with_every_film(self):
        self.open("#/l/films")
        card = self.view(".grid .card.saga")
        card.first.wait_for()
        self.assertIn("film", self.view(".count").inner_text())
        card.first.click()
        self.page.wait_for_selector(".sagapage .card")
        self.assertIn("#/saga/films", self.page.url)
        self.assertEqual(self.view("h1").inner_text(), "Saga DCEU")
        self.assertEqual(self.view(".card").count(), 2)
        self.press("Escape", wait=500)
        self.assertTrue(self.page.url.endswith("#/l/films"))

    def test_the_saga_page_marks_every_film_seen_and_back(self):
        films = [f for f in self.api("/api/library/films?group=Saga%20DCEU&sort=part&limit=600")["items"]]
        for film in films:
            self.addCleanup(lambda film=film: self.api("/api/item/" + film["id"] + "/seen", {"done": False}))
        self.open("#/saga/films?g=Saga%20DCEU")
        button = self.view(".actions .btn", has_text="Marquer la saga vue")
        button.wait_for()
        button.click()
        self.view(".actions .btn", has_text="Marquer la saga non vue").wait_for()
        self.assertIn("Tous vus", self.view(".meta").inner_text())
        self.assertEqual(self.view(".sagaall .badge.done").count(), len(films))
        self.assertTrue(all(self.api("/api/item/" + f["id"])["entries"][0]["progress"]["done"] for f in films))
        self.view(".actions .btn", has_text="Marquer la saga non vue").click()
        self.view(".actions .btn", has_text="Marquer la saga vue").wait_for()
        self.assertNotIn("vus", self.view(".meta").inner_text())
        self.assertEqual(self.view(".sagaall .badge.done").count(), 0)

    def choose_films(self, view):
        self.api("/api/me/settings", {"movie_view": view})
        self.addCleanup(lambda: self.api("/api/me/settings", {"movie_view": "sagas"}))

    def test_by_default_the_films_are_grouped_in_sagas_and_the_page_has_no_button_for_it(self):
        self.open("#/l/films")
        self.view(".grid .card.saga").first.wait_for()
        self.assertEqual(self.view(".tools .btn", has_text="Sagas").count(), 0)

    def test_an_account_that_chose_plain_films_sees_every_film_on_its_own(self):
        self.choose_films("films")
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        self.assertEqual(self.view(".grid .card.saga").count(), 0)
        titles = self.view(".grid .card").evaluate_all("els => els.map(e => e.title)")
        self.assertIn("Man of Steel", titles)
        self.assertIn("Batman v Superman", titles)
        self.assertEqual(self.view(".count").inner_text(), f"{len(titles)} films")

    def test_the_films_choice_made_in_the_settings_applies_when_coming_back_to_the_library(self):
        self.addCleanup(lambda: self.api("/api/me/settings", {"movie_view": "sagas"}))
        self.open("#/l/films")
        self.view(".grid .card.saga").first.wait_for()
        self.open("#/settings?tab=preferences")
        panel = self.view(".panel", has_text="Affichage")
        panel.locator(".line", has_text="films").locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Films").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["movie_view"] == "films")
        self.page.locator("#nav .link", has_text="Films").click()
        self.view(".grid .card").first.wait_for()
        self.until(lambda: self.view(".grid .card.saga").count() == 0)

    def test_the_old_choice_not_to_group_is_ignored(self):
        self.page.add_init_script("localStorage.setItem('hub.sagas.films', JSON.stringify('0'))")
        self.open("#/l/films")
        self.view(".grid .card.saga").first.wait_for()
        self.assertEqual(self.view(".grid .card.saga").count(), 1)


class TestStackedCards(UiCase):
    """Une série de livres de 2 tomes ou plus se dessine comme une saga (pile d'affiches, titre à la couleur de la bibliothèque) mais ouvre sa fiche ; une saga de films, la page de la saga."""
    ACCENT = {"livres": "rgb(155, 209, 107)", "films": "rgb(242, 180, 65)"}
    TEXT = "rgb(236, 232, 225)"

    @classmethod
    def add_media(cls, root):
        for name, count in (("Trois Tomes", 3), ("Un Seul", 1), ("Cinq Tomes", 5)):
            for n in range(1, count + 1):
                fixtures.write_cbz(root / "srv" / "Livres" / "Manga" / name / f"Tome {n:02d}.cbz", Series=name, Number=str(n))
        for n in range(1, 6):
            fixtures.write_mkv(root / "srv" / "Films" / "_Cinq" / f"{n} - Film {n}.mkv", fixtures.movie_blocks(f"Film {n}", "", f"200{n}-01-01", f"movie/9000{n}", "Action", collection="Saga Cinq", part=n), minutes=90)

    def card(self, lib, title):
        self.open("#/l/" + lib)
        card = self.view(f'.grid .card[title="{title}"]')
        card.wait_for()
        return card

    def colour(self, card):
        return card.locator(".t").evaluate("el => getComputedStyle(el).color")

    def test_a_series_of_three_volumes_is_a_stack_of_three_posters_in_the_accent_colour(self):
        card = self.card("livres", "Trois Tomes")
        self.assertIn("multi", card.get_attribute("class").split())
        self.assertNotIn("saga", card.get_attribute("class").split())
        self.assertEqual(card.locator(".stack").count(), 1)
        self.assertEqual(card.locator(".stack .poster").count(), 3)
        self.assertEqual(self.colour(card), self.ACCENT["livres"])
        self.assertEqual(card.locator(".s").inner_text(), "3 volumes")

    def test_a_series_of_one_volume_keeps_the_plain_card(self):
        card = self.card("livres", "Un Seul")
        self.assertNotIn("multi", card.get_attribute("class").split())
        self.assertEqual(card.locator(".stack").count(), 0)
        self.assertEqual(card.locator(".poster").count(), 1)
        self.assertEqual(self.colour(card), self.TEXT)

    def test_a_series_of_five_volumes_and_a_saga_of_five_films_show_three_posters(self):
        self.assertEqual(self.card("livres", "Cinq Tomes").locator(".stack .poster").count(), 3)
        saga = self.card("films", "Saga Cinq")
        self.assertIn("saga", saga.get_attribute("class").split())
        self.assertEqual(saga.locator(".stack .poster").count(), 3)
        self.assertEqual(self.colour(saga), self.ACCENT["films"])

    def test_the_series_card_opens_the_sheet_not_a_saga_page(self):
        card = self.card("livres", "Trois Tomes")
        item = self.item("livres", "Trois Tomes")["id"]
        self.assertEqual(card.get_attribute("href"), "#/i/" + item)
        card.click()
        self.shown("#/i/" + item)
        self.assertEqual(self.view("h1").first.inner_text(), "Trois Tomes")

    def test_the_stack_zooms_as_a_block_on_focus_and_on_hover_without_moving_its_posters(self):
        card = self.card("livres", "Trois Tomes")
        zoom = lambda: card.locator(".stack").evaluate("el => getComputedStyle(el).transform")          # noqa: E731
        card.focus()
        self.until(lambda: zoom() != "none")                                                                # la pile grossit d'un bloc
        self.assertEqual(card.locator(".stack .poster").first.evaluate("el => getComputedStyle(el).transform"), "none")        # sans que chaque affiche bouge de son côté
        self.page.evaluate("document.activeElement.blur()")
        self.page.mouse.move(0, 0)
        self.until(lambda: zoom() == "none")
        card.hover()
        self.until(lambda: zoom() != "none")

    def test_the_saga_page_shows_three_posters_of_five_films(self):
        self.open("#/saga/films?g=Saga%20Cinq")
        self.view(".stack-big .poster").first.wait_for()
        self.assertEqual(self.view(".stack-big .poster").count(), 3)

    def test_the_remote_reaches_the_stack_and_enter_opens_the_sheet(self):
        self.card("livres", "Trois Tomes")
        self.until(lambda: self.focused() != "")                                                            # le focus est posé dans le contenu
        for _ in range(8):
            if self.focused() == "Trois Tomes":
                break
            self.press("ArrowRight", wait=80)
        self.assertEqual(self.focused(), "Trois Tomes")
        item = self.item("livres", "Trois Tomes")["id"]
        self.press("Enter", wait=100)
        self.shown("#/i/" + item)


class TestSagaEditor(UiCase):
    """L'éditeur de sagas (Gestion > Sagas) et « Ajouter à une saga » de la fiche d'un film. Inception : 27205 ; Man of Steel : 49521 ; Batman v Superman : 209112."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: (self.run_.hub.cfg.data / "sagas.json").unlink(missing_ok=True))
        self.api("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 999999, 27205]}]})        # le film du milieu n'est pas dans la médiathèque

    def stored(self):
        return {s["name"]: [f["tmdb"] for f in s["films"]] for s in self.api("/api/sagas")["sagas"]}

    def test_gestion_leads_to_the_editor(self):
        self.open("#/admin")
        self.view("a.btn", has_text="Composer").click()
        self.page.wait_for_selector("#views > .sagaedit .line")               # l'éditeur est chargé : jusque-là, les `.line` visibles sont ceux de Gestion. `.sagaedit` est la racine de la vue : on cherche dedans
        self.assertEqual(self.view(".line b").all_inner_texts(), ["Ma saga"])

    def test_the_lead_and_the_empty_page_use_the_prose_width(self):
        self.open("#/sagas")
        self.view(".lead").first.wait_for()
        self.assertAlmostEqual(width_rem(self.view(".lead").first), PROSE, delta=0.1)
        self.open("#/sagas?g=" + urllib.parse.quote("mot " * 120))                      # un nom assez long pour que la phrase remplisse la largeur permise
        self.view(".blank p").wait_for()
        self.assertAlmostEqual(width_rem(self.view(".blank p")), PROSE, delta=0.1)      # le texte lui-même, pas seulement son plafond : le conteneur ne doit pas le rétrécir

    def test_a_saga_lists_its_films_and_moves_them(self):
        self.open("#/sagas?g=Ma%20saga")
        self.view(".sfilm").first.wait_for()
        self.assertEqual(self.view(".sfilm b").all_inner_texts(), ["Batman v Superman", "Film absent de la médiathèque", "Inception"])
        self.view(".sfilm [data-key=d0]").click()
        self.until(lambda: self.view(".sfilm b").first.inner_text() == "Film absent de la médiathèque")           # attendre l'écran : l'envoi suivant est ignoré tant que le précédent n'est pas revenu
        self.assertEqual(self.stored()["Ma saga"], [999999, 209112, 27205])
        self.view(".sfilm [data-key=r0]").click()
        self.until(lambda: self.view(".sfilm").count() == 2)
        self.assertEqual(self.stored()["Ma saga"], [209112, 27205])

    def test_the_picker_adds_a_film(self):
        self.open("#/sagas?g=Ma%20saga")
        self.view("[data-key=add]").click()
        self.page.locator(".picker .opt", has_text="Man of Steel").click()
        self.until(lambda: 49521 in self.stored()["Ma saga"])
        self.until(lambda: self.page.locator(".picker .opt.on").count() == 3)             # Batman v Superman, Inception et maintenant Man of Steel
        self.page.locator(".picker [data-key=pick-close]").click()
        self.assertEqual(self.view(".sfilm").count(), 4)

    def test_a_film_page_adds_the_film_to_a_saga(self):
        self.open("#/i/" + self.item("films", "Man of Steel")["id"])
        self.view("[data-key=saga-add]").click()
        self.page.locator(".menu .opt", has_text="Ma saga").click()
        self.until(lambda: 49521 in self.stored()["Ma saga"])
        self.view(".sagabox h2", has_text="Ma saga").wait_for()

    def test_a_film_without_tmdb_id_cannot_join_a_saga(self):
        self.open("#/i/" + self.item("films", "Sans Etiquette")["id"])
        self.view(".hero").wait_for()
        self.assertEqual(self.view("[data-key=saga-add]").count(), 0)


class TestMissingFilmsInTheUi(UiCase):
    """Les films qui manquent à une saga, grisés à leur rang (TMDB est simulé : voir FakeTmdb). « Saga DCEU » : Man of Steel et Batman v Superman, plus trois films absents."""

    def setUp(self):
        super().setUp()
        hub = self.run_.hub
        hub.cfg.tmdb_key = "fake"
        self.addCleanup(setattr, hub.cfg, "tmdb_key", "")
        self.addCleanup(hub.db.execute, "DELETE FROM kv WHERE key='sagainfo'")
        self.addCleanup(lambda: (hub.cfg.data / "sagas.json").unlink(missing_ok=True))
        with mock.patch("hub.enrich.Tmdb", FakeTmdb):
            self.api("/api/enrich", {"lib": "films"})
            self.run_.wait_jobs()

    def test_the_saga_page_greys_out_the_missing_films_at_their_rank(self):
        self.open("#/saga/films?g=Saga%20DCEU")
        self.view(".card.wanted").first.wait_for()
        self.assertEqual(self.view(".sagaall .part").all_inner_texts(), ["1", "2", "3", "4", "5"])
        self.assertEqual(self.view(".card.wanted").count(), 3)
        self.assertIn("3 manquants", self.view(".info .meta").inner_text())
        self.assertEqual(self.view(".card.wanted.f").count(), 0)                           # rien à ouvrir : pas dans la navigation à la télécommande

    def test_a_film_not_out_yet_is_greyed_as_coming_and_is_not_counted_as_missing(self):
        FakeTmdb.movies = {**FakeTmdb.movies, 3000: {"id": 3000, "title": "À venir", "release_date": "2099-06-01", "poster_path": None}}
        self.addCleanup(FakeTmdb.movies.pop, 3000, None)
        self.api("/api/sagas", {"sagas": [{"name": "Ma saga", "films": [209112, 1000, 3000]}]})
        with mock.patch("hub.enrich.Tmdb", FakeTmdb):
            self.api("/api/enrich", {"lib": "films"})
            self.run_.wait_jobs()
        self.open("#/saga/films?g=Ma%20saga")
        self.view(".card.wanted").first.wait_for()
        cards = {c.locator(".t").inner_text(): c for c in self.view(".card.wanted").all()}
        self.assertEqual(cards["À venir"].locator(".s").inner_text(), "2099 · À venir")
        self.assertEqual(cards["Film intermédiaire"].locator(".s").inner_text(), "2014 · Manquant")
        self.assertEqual(cards["À venir"].get_attribute("title"), "À venir (pas encore sorti)")
        self.assertIn("1 manquant", self.view(".info .meta").inner_text())                          # le film annoncé ne manque à personne
        self.assertNotIn("2 manquants", self.view(".info .meta").inner_text())

    def test_a_missing_film_can_be_hidden_and_shown_again(self):
        self.open("#/saga/films?g=Saga%20DCEU")
        self.view("[data-key=hide-1000]").click()
        self.until(lambda: self.view(".card.wanted").count() == 2)
        self.assertEqual(self.view(".sagaall .part").all_inner_texts(), ["1", "3", "4", "5"])      # le numéro ne bouge pas
        self.open("#/sagas")
        self.view("[data-key=show-1000]").click()
        self.until(lambda: self.view("[data-key=show-1000]").count() == 0)
        self.open("#/saga/films?g=Saga%20DCEU")
        self.view(".card.wanted").first.wait_for()
        self.assertEqual(self.view(".card.wanted").count(), 3)

    def test_the_film_page_shows_them_without_the_hide_button(self):
        self.open("#/i/" + self.item("films", "Batman v Superman")["id"])
        self.view(".sagabox").first.wait_for()
        self.assertIn("3 manquants", self.view(".sagabox h2").first.inner_text())
        self.assertEqual(self.view(".sagabox .mhide").count(), 0)


class TestCleanupConfirmation(UiCase):
    def test_deleting_junk_asks_first(self):
        self.open("#/admin")
        self.view("[data-key=junk-del]").click()
        dialog = self.page.locator(".menu.confirm", has_text="Supprimer les fichiers parasites ?")
        dialog.locator("button.danger", has_text="Supprimer définitivement").wait_for()
        dialog.locator("button", has_text="Annuler").click()
        self.page.wait_for_timeout(300)
        self.assertFalse([j for j in self.api("/api/jobs")["jobs"] if "Nettoyage" in j["label"] and "simulation" not in j["label"]])          # annuler ne lance rien

    def test_the_dialog_lists_the_first_files_it_would_delete(self):
        junk = []
        for name in ("Inception (2010)/Thumbs.db", "Inception (2010)/desktop.ini"):
            path = self.run_.root / "srv" / "Films" / name
            path.write_bytes(b"x")
            junk.append(path)
        self.addCleanup(lambda: [p.unlink() for p in junk if p.exists()])
        self.open("#/admin")
        self.view("[data-key=junk-del]").click()
        dialog = self.page.locator(".menu.confirm", has_text="Supprimer les fichiers parasites ?")
        dialog.locator("[data-key=junk-recount]").click()
        dialog.locator("p.lead", has_text="a compté").wait_for()
        names = dialog.locator(".sample li").all_inner_texts()
        self.assertEqual(sorted(names), ["Films/Inception (2010)/Thumbs.db", "Films/Inception (2010)/desktop.ini"])
        self.assertEqual(self.api("/api/jobs?log=1")["jobs"][0]["result"]["found"], 2)
        self.assertTrue(all(p.exists() for p in junk))                                           # compter n'efface rien

    def test_the_dialog_can_count_again_without_closing(self):
        self.open("#/admin")
        self.view("[data-key=junk-del]").click()
        dialog = self.page.locator(".menu.confirm", has_text="Supprimer les fichiers parasites ?")
        dialog.locator("[data-key=junk-recount]").click()
        dialog.locator("p.lead", has_text="a compté").wait_for()
        self.assertRegex(dialog.locator("p.lead").last.inner_text(), r"en a compté \d+\.")
        self.assertTrue(dialog.is_visible())                                                              # la fenêtre reste ouverte
        self.assertFalse([j for j in self.api("/api/jobs")["jobs"] if "Nettoyage" in j["label"] and "simulation" not in j["label"]])          # compter n'efface rien

class TestMusicQueueAndPlaylists(UiCase):
    def open_music(self):
        self.open("#/l/musique")
        self.view(".grid .card").first.wait_for()

    def test_card_play_button_starts_the_album(self):
        self.open_music()
        card = self.view(".grid .card", has_text="Discovery")
        card.hover()
        card.locator(".qbtn").first.click()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.assertEqual(self.page.inner_text("#abar .ti"), "One More Time")
        self.assertIn("#/l/musique", self.page.url)                  # on reste dans la liste

    def test_play_next_and_queue_panel(self):
        self.open_music()
        card = self.view(".grid .card", has_text="Discovery")
        card.hover()
        card.locator(".qbtn").first.click()
        self.page.wait_for_selector("#abar:not([hidden])")
        card.hover()
        card.locator(".qbtn").nth(1).click()                          # ⋯
        self.page.locator(".veil .opt", has_text="Jouer ensuite").click()
        self.page.wait_for_timeout(500)
        self.page.locator("#abar .qn").wait_for()
        self.assertEqual(self.page.inner_text("#abar .qn"), "1/6")
        self.page.locator("#abar [aria-label=\"File d’attente\"]").click()
        self.page.wait_for_selector(".menu.queue .qrow")
        self.assertEqual(self.page.locator(".menu.queue .qrow").count(), 6)
        self.assertEqual(self.page.locator(".menu.queue .qrow.now").count(), 1)
        self.page.locator(".menu.queue .qrow").nth(2).locator(".qt").click()          # saute à la piste 3
        self.page.wait_for_function("document.querySelector('#abar .qn').textContent === '3/6'")
        self.page.locator(".menu.queue [aria-label=\"Retirer de la file\"]").last.click()
        self.assertEqual(self.page.locator(".menu.queue .qrow").count(), 5)

    def test_queue_survives_a_reload(self):
        self.open_music()
        card = self.view(".grid .card", has_text="Discovery")
        card.hover()
        card.locator(".qbtn").first.click()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.page.reload()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.assertEqual(self.page.inner_text("#abar .ti"), "One More Time")

    def test_create_a_playlist_from_an_album_and_play_it(self):
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        self.view(".actions .btn", has_text="Playlist").click()
        self.page.locator(".veil .opt", has_text="Nouvelle playlist").click()
        self.page.fill(".menu.prompt input", "Soirée")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#toast.on, #toast:not(:empty)")
        lists = self.api("/api/playlists")["playlists"]
        self.assertEqual([(p["name"], p["count"]) for p in lists], [("Soirée", 3)])
        self.open("#/playlists")
        self.view(".card", has_text="Soirée").click()
        self.page.wait_for_selector(".playlist .trk")
        self.assertEqual(self.page.locator(".playlist .trk").count(), 3)
        self.view(".actions .btn", has_text="Lire").click()
        self.page.wait_for_selector("#abar:not([hidden])")
        self.assertEqual(self.page.inner_text("#abar .ti"), "One More Time")


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow absent")
class TestArtistPhotoCrop(UiCase):
    def two_halves(self):
        import io
        from PIL import Image

        image = Image.new("RGB", (400, 200), (220, 0, 0))
        image.paste((0, 0, 220), (200, 0, 400, 200))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    def test_the_chosen_photo_is_cropped_in_the_frame_then_saved_as_webp(self):
        from PIL import Image

        artist = self.item("musique", "Discovery")["artist_id"]
        home = Path(self.tmp) / "srv" / "Musique" / "Daft Punk" / "artist.webp"
        self.addCleanup(home.unlink, True)
        self.open(f"#/artist/musique/{artist}")
        self.view(".actions .btn", has_text="Tout lire").wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Recadrer").count(), 0)           # pas de photo, rien à recadrer
        self.page.set_input_files("input[type=file]", files={"name": "daft.png", "mimeType": "image/png", "buffer": self.two_halves()})
        self.page.wait_for_selector(".menu.crop .crop-stage")
        valider = self.page.locator(".menu.crop .btn", has_text="Valider")
        self.page.wait_for_function("!document.querySelector('.menu.crop .btn.primary').disabled")
        box = self.page.locator(".crop-stage").bounding_box()
        self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(box["x"] + box["width"] / 2 - 400, box["y"] + box["height"] / 2, steps=5)       # on pousse l'image à gauche : la moitié bleue est dans le cadre
        self.page.mouse.up()
        valider.click()
        self.page.wait_for_selector(".menu.crop", state="detached")
        self.until(home.exists)
        with Image.open(home) as stored:
            self.assertEqual(stored.format, "WEBP")
            self.assertEqual(stored.size[0], stored.size[1])                                       # carrée, comme la vignette
            self.assertLessEqual(stored.size[0], 200)                                              # le cadre ne couvre que la hauteur : 200 px au plus
            self.assertTrue(stored.convert("RGB").getpixel((stored.width // 2, stored.height // 2))[2] > 150)
        self.view(".actions .btn", has_text="Recadrer").wait_for()
        self.view(".actions .btn", has_text="Recadrer").click()                                    # la photo gardée se recadre encore
        self.page.wait_for_function("!document.querySelector('.menu.crop .btn.primary').disabled")
        self.page.locator(".menu.crop input[type=range]").fill("100")                              # zoom maximal
        self.page.locator(".menu.crop .btn", has_text="Valider").click()
        self.page.wait_for_selector(".menu.crop", state="detached")
        self.until(lambda: Image.open(home).size[0] < 100)
        self.view(".actions .btn", has_text="Recadrer").click()
        self.page.locator(".menu.crop .btn", has_text="Annuler").click()                           # annuler ne touche à rien
        self.page.wait_for_selector(".menu.crop", state="detached")


class TestArtistLinks(UiCase):
    """La section « Liens » de la fiche d'un artiste : l'administrateur retire et ajoute des liens ; une télé et un compte ordinaire ne font que les lire."""

    def setUp(self):
        super().setUp()
        self.artist, undo = give_artist_a_sheet(self.run_.hub.db)
        self.addCleanup(undo)

    def open_sheet(self):
        self.open(f"#/artist/musique/{self.artist}")
        self.view(".links-out a").first.wait_for()

    def shown(self):
        return self.view(".links-out a").all_inner_texts()

    def kept(self):
        return [link["url"] for link in self.api(f"/api/artist/musique/{self.artist}")["info"]["links"]]

    def toast(self, text):
        """Attend le message, puis l'efface : le suivant ne se confond pas avec lui."""
        self.page.locator("#toast.on", has_text=text).wait_for()
        self.page.evaluate("document.getElementById('toast').classList.remove('on')")

    def ask_to_add(self, url):
        self.view(".links-out .btn", has_text="Ajouter un lien").click()
        self.page.locator(".menu.prompt input").fill(url)
        self.page.locator(".menu.prompt .btn.primary").click()

    def remove_first(self):
        self.view(".links-out .linkx").first.click()
        self.page.locator(".menu.confirm .btn.danger", has_text="Retirer le lien").click()

    def forgive_refusals(self):
        """Chromium journalise chaque réponse 400 : ici, elles sont voulues."""
        self.errors[:] = [e for e in self.errors if "status of 400" not in e]

    def test_the_administrator_gets_a_cross_per_link_and_a_button_to_add_one(self):
        self.open_sheet()
        self.assertEqual(self.shown(), ["Site officiel", "Wikipédia", "MusicBrainz"])
        self.assertEqual(self.view(".links-out .linkx").count(), 3)
        self.assertEqual(self.view(".links-out .linkx").first.get_attribute("aria-label"), "Retirer le lien « Site officiel »")
        self.view(".links-out .btn", has_text="Ajouter un lien").wait_for()

    def test_a_link_is_removed_after_a_confirmation_without_reloading_the_page(self):
        self.open_sheet()
        self.page.evaluate("window.sameDocument = true")
        self.view(".links-out .linkx").first.click()
        self.page.locator(".menu.confirm .btn", has_text="Annuler").click()              # refuser ne retire rien
        self.page.wait_for_selector(".menu", state="detached")
        self.assertEqual(self.shown(), ["Site officiel", "Wikipédia", "MusicBrainz"])
        self.remove_first()
        self.until(lambda: self.shown() == ["Wikipédia", "MusicBrainz"])
        self.toast("Lien retiré")
        self.assertEqual(self.view(".links-out .linkx").count(), 2)
        self.assertTrue(self.page.evaluate("window.sameDocument"))
        self.assertEqual(self.kept(), [WIKIPEDIA, MUSICBRAINZ])

    def test_a_valid_address_is_added_and_appears_at_once(self):
        self.open_sheet()
        self.page.evaluate("window.sameDocument = true")
        self.ask_to_add("https://www.facebook.com/daftpunk")
        self.until(lambda: "Facebook" in self.shown())
        self.toast("Lien ajouté")
        link = self.view(".links-out a", has_text="Facebook")
        self.assertEqual((link.get_attribute("href"), link.get_attribute("rel"), link.get_attribute("target")), ("https://www.facebook.com/daftpunk", "noopener noreferrer", "_blank"))
        self.assertEqual(self.shown(), ["Site officiel", "Facebook", "Wikipédia", "MusicBrainz"])
        self.assertEqual(self.view(".links-out .linkx").count(), 4)
        self.assertTrue(self.page.evaluate("window.sameDocument"))
        self.assertIn("https://www.facebook.com/daftpunk", self.kept())

    def test_an_address_that_is_not_a_web_link_shows_the_error_of_the_server_and_adds_nothing(self):
        self.open_sheet()
        for bad in ("javascript:alert(1)", "ftp://example.org/fichier"):
            self.ask_to_add(bad)
            self.toast("l'adresse doit être un lien web (http:// ou https://)")
            self.assertEqual(self.shown(), ["Site officiel", "Wikipédia", "MusicBrainz"], bad)
        self.assertEqual(self.kept(), [OFFICIAL, WIKIPEDIA, MUSICBRAINZ])
        self.assertIsNone(self.run_.hub.db.kv_get(f"artistlinks:{ARTIST_MBID}"))
        self.forgive_refusals()

    def test_an_address_that_would_run_a_script_never_gets_into_the_page(self):
        self.open_sheet()
        for bad in ("javascript:window.scriptRan=true", "JaVaScRiPt:alert(1)", "data:text/html,<script>1</script>"):
            self.ask_to_add(bad)
            self.toast("l'adresse doit être un lien web")
        self.assertEqual(self.page.locator("a[href^=javascript i], a[href^=data i]").count(), 0)
        self.assertIsNone(self.page.evaluate("window.scriptRan"))
        self.assertEqual(self.kept(), [OFFICIAL, WIKIPEDIA, MUSICBRAINZ])
        self.forgive_refusals()

    def test_the_section_stays_with_its_button_when_every_link_has_been_removed(self):
        self.open_sheet()
        for left in (2, 1, 0):
            self.remove_first()
            self.until(lambda: self.view(".links-out .linkx").count() == left)
        self.assertEqual(self.view(".links-out h2").inner_text(), "Liens")
        self.view(".links-out .btn", has_text="Ajouter un lien").wait_for()
        self.assertEqual(self.kept(), [])
        self.open(f"#/artist/musique/{self.artist}")                                    # rouverte : toujours là, sans lien
        self.view(".links-out .btn", has_text="Ajouter un lien").wait_for()
        self.assertEqual(self.view(".links-out a").count(), 0)

    def test_a_television_shows_the_links_without_a_cross_or_a_button(self):
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")
        self.open_sheet()
        self.assertEqual(self.shown(), ["Site officiel", "Wikipédia", "MusicBrainz"])
        self.assertEqual(self.view(".links-out .linkx").count(), 0)
        self.assertEqual(self.view(".links-out .btn", has_text="Ajouter un lien").count(), 0)

    def test_a_television_shows_no_empty_section(self):
        for url in (OFFICIAL, WIKIPEDIA, MUSICBRAINZ):
            self.api(f"/api/artist/musique/{self.artist}/links/remove", {"url": url})
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")
        self.open(f"#/artist/musique/{self.artist}")
        self.view(".actions .btn", has_text="Tout lire").wait_for()
        self.assertEqual(self.view(".links-out").count(), 0)


class TestBooksQuickAccess(UiCase):
    def test_section_chips_filter_the_shelf(self):
        self.open("#/l/livres")
        self.view(".quick .chip").first.wait_for()
        names = self.view(".quick .chip").evaluate_all("els => els.map(e => e.getAttribute('data-section'))")
        self.assertEqual(names[0], "")
        self.assertTrue({"Manga", "Artbook", "Romans"} <= set(names), names)
        total = self.view(".grid .card").count()
        self.view(".quick .chip[data-section=Manga]").click()
        self.page.wait_for_timeout(500)
        titles = self.view(".grid .card").evaluate_all("els => els.map(e => e.title)")
        self.assertEqual(titles, ["Naruto"])
        self.assertEqual(self.view(".quick .chip.on").get_attribute("data-section"), "Manga")
        self.page.reload()                                            # le choix est retenu
        self.view(".quick .chip.on").wait_for()
        self.assertEqual(self.view(".quick .chip.on").get_attribute("data-section"), "Manga")
        self.view(".quick .chip[data-section=\"\"]").click()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.view(".grid .card").count(), total)

    def test_chips_are_reachable_with_the_remote(self):
        self.open("#/l/livres")
        self.view(".quick .chip").first.wait_for()
        self.press("ArrowDown", wait=200)
        for _ in range(6):
            if self.page.evaluate("document.activeElement.classList.contains('chip')"):
                break
            self.press("ArrowUp", wait=150)
        self.assertTrue(self.page.evaluate("document.activeElement.classList.contains('chip')"))
        self.press("ArrowRight", "Enter", wait=400)
        self.assertEqual(self.view(".quick .chip.on").count(), 1)
        self.assertNotEqual(self.view(".quick .chip.on").get_attribute("data-section"), "")


class TestLogin(unittest.TestCase):
    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_password_gate(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{run.port}/")
                page.wait_for_selector("input[type=password]")
                page.fill("input[type=password]", "nope")
                page.keyboard.press("Enter")
                page.wait_for_selector("text=Mot de passe incorrect")
                page.fill("input[type=password]", "secret")
                page.keyboard.press("Enter")
                page.wait_for_selector("#nav .link")
                self.assertEqual(page.locator("#nav .link").count(), 4)
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRightsInTheUi(unittest.TestCase):
    """L'administrateur règle les droits d'un compte dans Paramètres > Comptes ; le compte ne voit plus la bibliothèque fermée et n'a plus le bouton de téléchargement là où il est retiré."""

    def api(self, base, path, body=None, token=""):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return json.load(urllib.request.urlopen(req))

    def sign_in(self, page, base, name, password):
        page.goto(base + "/login")
        page.fill("input[name=user]", name)
        page.fill("input[type=password]", password)
        page.keyboard.press("Enter")
        page.wait_for_selector("#nav .link", state="attached")

    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_the_administrator_sets_the_rights_and_the_account_sees_them(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            base = f"http://127.0.0.1:{run.port}"
            token = self.api(base, "/api/token", {"password": "secret"})["token"]
            alice = self.api(base, "/api/users", {"name": "alice", "password": "motdepasse12"}, token)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                admin = browser.new_page()
                self.sign_in(admin, base, "admin", "secret")
                admin.goto(base + "/#/settings?tab=comptes")
                admin.locator(f'[data-key="rights-{alice["id"]}"]').click()
                admin.locator(".rights").wait_for()
                size = admin.evaluate("(() => { var m = document.querySelector('.menu.rightsbox'); var r = document.querySelector('.rights .rrow'); return [m.scrollHeight, m.clientHeight, m.getBoundingClientRect().height, innerHeight, r.getBoundingClientRect().height, getComputedStyle(m).fontFamily]; })()")
                self.assertLessEqual(size[0], size[1], f"la fenêtre défile : {size}")       # la fenêtre tient sans défiler
                widths = "Array.prototype.map.call(document.querySelectorAll('.rights .chip'), function (c) { return c.getBoundingClientRect().width; })"
                before = admin.evaluate(widths)
                admin.locator('[data-key="r-download-films"]').click()                    # films : voir sans télécharger
                admin.locator('[data-key="r-view-livres"]').click()                       # livres : fermés
                self.assertTrue(admin.locator('[data-key="r-download-livres"]').is_disabled())
                self.assertTrue(admin.locator('[data-key="r-play-livres"]').is_disabled())
                admin.locator('[data-key="r-play-series"]').click()                       # séries : se voient et se téléchargent, ne se lisent pas
                self.assertEqual(admin.evaluate(widths), before)                           # coché ou non, un bouton garde la même largeur
                admin.locator(".menu .btn", has_text="Enregistrer").click()
                admin.locator(f'[data-user="{alice["id"]}"] [data-rights]', has_text="Consulter : Films").wait_for()
                self.assertIn("Télécharger : Séries", admin.locator(f'[data-rights="{alice["id"]}"]').inner_text())
                self.assertIn("Lecture : Films, Musique", admin.locator(f'[data-rights="{alice["id"]}"]').inner_text())
                self.assertEqual(admin.locator('[data-user="default"] [data-key^="rights-"]').count(), 0)       # un administrateur n'a pas de droits à régler
                saved = {u["name"]: u["rights"] for u in self.api(base, "/api/users", token=token)["users"]}["alice"]
                self.assertEqual((saved["films"], saved["series"], saved["livres"]), ({"view": True, "play": True, "download": False}, {"view": True, "play": False, "download": True},
                                                                                  {"view": False, "play": False, "download": False}))
                page = browser.new_page()
                self.sign_in(page, base, "alice", "motdepasse12")
                self.assertEqual(page.locator("#nav .link").count(), 3)                   # les livres ont disparu
                state = self.api(base, "/api/token", {"user": "alice", "password": "motdepasse12"})["token"]
                film = next(i for i in self.api(base, "/api/library/films", token=state)["items"] if i["title"] == "Inception")["id"]
                dark = next(i for i in self.api(base, "/api/library/series", token=state)["items"] if i["title"] == "Dark")["id"]
                page.goto(f"{base}/#/i/{film}")
                page.locator("#views > .view:visible .actions .btn", has_text="Marquer comme vu").wait_for()
                self.assertEqual(page.locator("#views > .view:visible .actions .btn", has_text="Télécharger").count(), 0)
                self.assertEqual(page.locator("#views > .view:visible .actions .btn.primary").count(), 1)        # films : le bouton « Lire » est là
                page.goto(f"{base}/#/i/{dark}")
                page.locator("#views > .view:visible .actions .btn", has_text="Télécharger").wait_for()          # séries : télécharger est permis...
                self.assertEqual(page.locator("#views > .view:visible .actions .btn.primary").count(), 0)        # ...lire non : pas de bouton « Lire »
                page.locator(".ep").first.click()
                page.locator("#toast", has_text="Ta lecture est fermée").wait_for()
                self.assertTrue(page.url.endswith(f"/#/i/{dark}"))                        # le lecteur ne s'est pas ouvert
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestActionsMenu(UiCase):
    """Le bouton « … » : voir / revoir et vu / non vu sur les films, les sagas, les séries et chaque épisode."""

    def done(self, lib, title):
        return [bool((e.get("progress") or {}).get("done")) for e in self.entries(lib, title)]

    def forget(self, lib, title):
        self.addCleanup(lambda: self.api("/api/item/" + self.item(lib, title)["id"] + "/seen", {"done": False}))

    def choose(self, label):
        self.page.locator(".menu .opt", has_text=label).click()

    def card(self, lib, title):
        self.open("#/l/" + lib)
        card = self.view(f'.card[data-key="{self.item(lib, title)["id"]}"]')
        card.wait_for()
        return card

    def test_a_film_has_its_seen_button_and_a_show_speaks_of_every_season(self):
        self.open("#/i/" + self.item("films", "Inception")["id"])
        self.view(".actions .btn", has_text="Marquer comme vu").wait_for()
        self.open("#/i/" + self.item("series", "Dark")["id"])
        self.view(".actions .btn", has_text="Marquer toutes les saisons comme vu").wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Marquer comme vu").count(), 0)

    def test_the_show_button_marks_every_season_then_none(self):
        self.forget("series", "Dark")
        self.open("#/i/" + self.item("series", "Dark")["id"])
        self.view(".actions .btn", has_text="Marquer toutes les saisons comme vu").click()
        self.until(lambda: all(self.done("series", "Dark")))
        self.view(".actions .btn", has_text="Marquer toutes les saisons non vu").click()
        self.until(lambda: not any(self.done("series", "Dark")))

    def test_an_episode_is_marked_seen_from_its_own_menu(self):
        self.forget("series", "Dark")
        self.open("#/i/" + self.item("series", "Dark")["id"])
        self.view(".epmore").first.wait_for()
        self.view(".epmore").first.click()
        self.choose("Marquer comme vu")
        self.until(lambda: sum(self.done("series", "Dark")) == 1)
        self.view(".ep .seen").first.wait_for()
        self.view(".epmore").first.click()
        self.choose("Marquer non vu")
        self.until(lambda: sum(self.done("series", "Dark")) == 0)

    def test_a_film_card_marks_the_film_seen_and_updates_its_badge(self):
        self.forget("films", "Inception")
        card = self.card("films", "Inception")
        card.locator(".qbtn").click()
        self.assertEqual(self.page.locator(".menu h3").inner_text(), "Inception")
        self.choose("Marquer comme vu")
        self.until(lambda: all(self.done("films", "Inception")))
        self.until(lambda: self.view(".card .badge.done").count() == 1)
        card.locator(".qbtn").click()
        self.choose("Marquer non vu")
        self.until(lambda: not any(self.done("films", "Inception")))

    def test_a_film_card_plays_the_film_and_a_seen_one_is_watched_again_from_the_start(self):
        self.forget("films", "Inception")
        self.card("films", "Inception").locator(".qbtn").click()
        self.choose("Voir")
        self.page.wait_for_url("**#/play/*")
        self.page.wait_for_function("() => { var c = window.Hub.current(); return !!c && c.hash.indexOf('#/play/') === 0 && c.el.style.display !== 'none'; }")      # l'adresse change avant la page : sans attendre, le retour sur la liste peut la trouver déjà affichée et ne pas la rafraîchir
        self.assertNotIn("t=0", self.page.url)
        self.api("/api/item/" + self.item("films", "Inception")["id"] + "/seen", {"done": True})
        # De retour sur la liste (sans rechargement), la grille d'avant s'affiche d'abord, puis la réponse de /api/library la remplace : on attend la pastille « vu », sinon le clic tombe sur la carte périmée, dont le menu n'offre pas « Revoir »
        card = self.card("films", "Inception")
        card.locator(".badge.done").wait_for()
        card.locator(".qbtn").click()
        self.choose("Revoir")
        self.page.wait_for_url("**#/play/*t=0")

    def test_the_context_menu_key_or_a_right_click_opens_the_same_menu(self):
        card = self.card("films", "Inception")
        card.click(button="right")
        self.page.locator(".menu .opt", has_text="Marquer comme vu").wait_for()
        self.press("Escape", wait=300)
        card.focus()
        self.press("ContextMenu", wait=300)
        self.page.locator(".menu .opt", has_text="Marquer comme vu").wait_for()

    def test_a_film_known_to_tmdb_offers_its_page_and_a_saga_to_join(self):
        self.card("films", "Inception").locator(".qbtn").click()
        self.page.locator(".menu .opt", has_text="Ajouter à une saga").wait_for()
        self.page.evaluate("window.open = function (url) { window.opened = url; }")
        self.choose("TMDB")
        self.until(lambda: self.page.evaluate("window.opened") == "https://www.themoviedb.org/movie/27205")

    def test_the_menu_can_add_the_film_to_a_saga(self):
        self.card("films", "Inception").locator(".qbtn").click()
        self.choose("Ajouter à une saga")
        self.assertEqual(self.page.locator(".menu h3").inner_text(), "Ajouter « Inception » à une saga")
        self.page.locator(".menu .opt", has_text="Nouvelle saga").wait_for()

    def test_a_film_unknown_to_tmdb_offers_neither(self):
        self.card("films", "Sans Etiquette").locator(".qbtn").click()
        self.page.locator(".menu .opt", has_text="Marquer comme vu").wait_for()
        self.assertEqual(self.page.locator(".menu .opt").all_inner_texts(), ["Voir", "Marquer comme vu", "Télécharger"])

    def test_a_show_knows_its_tmdb_page_once_enriched_and_a_saga_never_does(self):
        item = self.item("series", "Dark")
        self.run_.hub.db.update_item_meta(item["id"], meta={"tmdb": 70523})
        self.addCleanup(self.run_.hub.db.update_item_meta, item["id"], meta={"tmdb": None})
        self.card("series", "Dark").locator(".qbtn").click()
        self.page.evaluate("window.open = function (url) { window.opened = url; }")
        self.choose("TMDB")
        self.until(lambda: self.page.evaluate("window.opened") == "https://www.themoviedb.org/tv/70523")
        self.open("#/l/films")
        self.view(".grid .card.saga .qbtn").first.click()
        self.page.locator(".menu .opt", has_text="Marquer comme vu").wait_for()
        self.assertEqual(self.page.locator(".menu .opt").all_inner_texts(), ["Voir", "Marquer comme vu"])

    def test_a_show_card_marks_every_season(self):
        self.forget("series", "Dark")
        self.card("series", "Dark").locator(".qbtn").click()
        self.choose("Marquer toutes les saisons comme vu")
        self.until(lambda: all(self.done("series", "Dark")))

    def test_a_saga_card_marks_every_film_of_the_saga(self):
        titles = ("Man of Steel", "Batman v Superman")
        for title in titles:
            self.forget("films", title)
        self.open("#/l/films")
        saga = self.view(".grid .card.saga").first
        saga.wait_for()
        saga.locator(".qbtn").click()
        self.assertEqual(self.page.locator(".menu h3").inner_text(), "Saga DCEU")
        self.choose("Marquer comme vu")
        self.until(lambda: all(all(self.done("films", title)) for title in titles))
        self.until(lambda: self.view(".grid .card.saga .badge.done").count() == 1)


class TestDownloads(UiCase):
    """Télécharger : toujours un fichier à la fois, jamais de zip ; une saison, une série ou un album sont des téléchargements enchaînés."""

    def watch(self):
        names = []
        self.page.on("download", lambda d: names.append(d.suggested_filename))
        return names

    def settled(self, names, expected):
        self.until(lambda: len(names) >= expected, tries=120)
        self.page.wait_for_timeout(1800)               # rien de plus que prévu n'arrive ensuite
        self.assertEqual(len(names), expected, names)
        return sorted(names)

    def choose(self, label):
        self.page.locator(".menu .opt", has_text=label).click()

    def count(self, lib, title):
        return len(self.entries(lib, title))

    def test_a_film_card_downloads_the_file(self):
        names = self.watch()
        self.open("#/l/films")
        card = self.view(f'.card[data-key="{self.item("films", "Inception")["id"]}"]')
        card.wait_for()
        card.locator(".qbtn").click()
        self.choose("Télécharger")
        self.assertEqual(self.settled(names, 1), ["Inception (2010).mkv"])

    def test_a_film_page_has_a_download_button(self):
        names = self.watch()
        self.open("#/i/" + self.item("films", "Inception")["id"])
        self.view(".actions .btn", has_text="Télécharger").click()
        self.assertEqual(self.settled(names, 1), ["Inception (2010).mkv"])

    def test_a_show_downloads_one_season_or_all_of_them(self):
        names = self.watch()
        self.open("#/i/" + self.item("series", "Dark")["id"])
        self.view(".actions .btn", has_text="Télécharger").click()
        self.choose("Saison 1")
        self.assertEqual(self.settled(names, 2), ["01 - Pilote.mkv", "02 - Suite.mkv"])
        del names[:]
        self.view(".actions .btn", has_text="Télécharger").click()
        self.choose("Toute la série")
        self.assertEqual(len(self.settled(names, 3)), 3)

    def test_an_episode_has_its_own_download(self):
        names = self.watch()
        self.open("#/i/" + self.item("series", "Dark")["id"])
        self.view(".epmore").first.click()
        self.choose("Télécharger")
        self.assertEqual(self.settled(names, 1), ["01 - Pilote.mkv"])

    def test_a_show_card_downloads_every_episode(self):
        names = self.watch()
        self.open("#/l/series")
        card = self.view(f'.card[data-key="{self.item("series", "Dark")["id"]}"]')
        card.wait_for()
        card.locator(".qbtn").click()
        self.choose("Télécharger")
        self.assertEqual(len(self.settled(names, self.count("series", "Dark"))), 3)

    def test_an_album_downloads_its_tracks_and_a_track_its_own_file(self):
        names = self.watch()
        tracks = self.count("musique", "Discovery")
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".trk").first.wait_for()
        self.view(".actions .btn", has_text="Télécharger").click()
        self.assertEqual(len(self.settled(names, tracks)), tracks)
        del names[:]
        self.view(".trkmore").first.click()
        self.choose("Télécharger")
        self.assertEqual(len(self.settled(names, 1)), 1)

    def test_a_book_series_downloads_its_volumes_and_a_volume_its_own_file(self):
        names = self.watch()
        volumes = self.count("livres", "Naruto")
        self.open("#/i/" + self.item("livres", "Naruto")["id"])
        self.view(".volumes .card").first.wait_for()
        self.view(".actions .btn", has_text="Télécharger").click()
        self.assertEqual(len(self.settled(names, volumes)), volumes)
        del names[:]
        self.view(".volumes .card .qbtn").first.click()
        self.choose("Télécharger")
        self.assertEqual(len(self.settled(names, 1)), 1)

    def test_the_download_is_a_whole_file_with_its_ranges_and_never_counts_as_a_conversion(self):
        entry = self.entries("films", "Inception")[0]["id"]
        req = urllib.request.Request(self.base + f"/media/{entry}/file?download=1", headers={"Range": "bytes=0-9"})
        reply = urllib.request.urlopen(req)
        self.assertEqual(reply.status, 206)
        self.assertIn("attachment", reply.headers["Content-Disposition"])
        self.assertEqual(self.api("/api/state")["streams"], 0)

    def test_a_television_is_not_offered_downloads(self):
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")
        self.open("#/i/" + self.item("films", "Inception")["id"])
        self.view(".actions").wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Télécharger").count(), 0)
        self.open("#/l/films")
        card = self.view(f'.card[data-key="{self.item("films", "Inception")["id"]}"]')
        card.wait_for()
        card.click(button="right")
        self.page.locator(".menu .opt", has_text="Marquer comme vu").wait_for()
        self.assertEqual(self.page.locator(".menu .opt", has_text="Télécharger").count(), 0)


class TestPosters(UiCase):
    """Changer l'affiche d'un film ou d'une série : l'image reste dans HUB_DATA, la médiathèque n'est pas touchée."""

    def served(self, item):
        return urllib.request.urlopen(self.base + "/img/item/" + item["id"]).read()

    def reset_later(self, item):
        """L'affiche choisie ne se retire pas depuis l'interface : le test la retire lui-même pour que les suivants retrouvent l'original."""
        def forget():
            images, key = self.run_.hub.images, "poster:" + item["id"]
            name = images.db.kv_get(key)
            images.db.kv_delete(key)
            path = images._poster_file(name)
            if path:
                path.unlink(missing_ok=True)
        self.addCleanup(forget)

    def send(self, payload):
        with self.page.expect_file_chooser() as chooser:
            self.view(".actions .btn", has_text="Changer l’affiche").click()
        chooser.value.set_files(payload)

    CHOSEN = b"\xff\xd8\xff\xe0affiche choisie"

    def chosen(self):
        return {"name": "affiche.jpg", "mimeType": "image/jpeg", "buffer": self.CHOSEN}

    def test_the_poster_is_replaced_and_there_is_no_way_back_to_the_original(self):
        item = self.item("films", "Inception")
        self.reset_later(item)
        original = self.served(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send(self.chosen())
        self.until(lambda: self.served(item) == self.CHOSEN)
        self.assertNotEqual(self.served(item), original)
        self.assertNotIn("poster_custom", self.api("/api/item/" + item["id"]))
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Affiche d’origine").count(), 0)

    @unittest.skipUnless(images.Image is not None and images.can_webp(""), "Pillow sans WebP")
    def test_a_webp_poster_is_kept_as_a_jpeg(self):
        item = self.item("films", "Inception")
        self.reset_later(item)
        buffer = io.BytesIO()
        images.Image.new("RGB", (30, 20), "red").save(buffer, "WEBP")
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send({"name": "affiche.webp", "mimeType": "image/webp", "buffer": buffer.getvalue()})
        self.until(lambda: images.sniff(self.served(item)) == "image/jpeg" and self.served(item)[:2] == bytes([255, 216]))
        self.assertEqual(images.sniff(self.served(item)), "image/jpeg")

    def test_a_show_has_the_same_buttons(self):
        item = self.item("series", "Dark")
        self.reset_later(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send(self.chosen())
        self.until(lambda: self.served(item) == self.CHOSEN)

    def test_a_file_that_is_not_an_image_is_refused_with_a_message(self):
        item = self.item("films", "Inception")
        self.reset_later(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send({"name": "notes.txt", "mimeType": "text/plain", "buffer": b"du texte"})
        self.page.locator("#toast.on", has_text="pas une image").wait_for()
        self.assertNotEqual(self.served(item), b"du texte")
        del self.errors[:]                      # le navigateur note la réponse 415, attendue ici

    def test_an_album_has_no_poster_button(self):
        self.open("#/i/" + self.item("musique", "Discovery")["id"])
        self.view(".actions .btn").first.wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Changer l’affiche").count(), 0)

    def test_a_television_has_no_poster_button(self):
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")
        self.open("#/i/" + self.item("films", "Inception")["id"])
        self.view(".actions .btn").first.wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Changer l’affiche").count(), 0)


class TestResumeMenu(UiCase):
    """La carte de « Reprendre » : un clic droit (ou ✕) propose Garder, Retirer de Reprendre et Remettre à zéro."""

    def started(self):
        entry = self.entries("films", "Direct")[0]
        item = self.item("films", "Direct")
        self.api("/api/progress", {"entry": entry["id"], "position": 40, "duration": 60})
        self.addCleanup(lambda: self.api("/api/item/" + item["id"] + "/seen", {"done": False}))
        self.addCleanup(lambda: self.run_.hub.db.execute("DELETE FROM kv WHERE key LIKE 'dismiss:%'"))
        return item, entry

    def menu(self):
        self.open()
        card = self.view(".shelf .card.wide", has_text="Direct")
        card.wait_for()
        card.click(button="right")
        self.page.locator(".menu .opt").first.wait_for()
        return self.page.locator(".menu .opt .lbl").all_inner_texts()

    def progress(self, item):
        return self.api("/api/item/" + item["id"])["entries"][0].get("progress")

    def test_the_menu_offers_three_choices_and_keeps_by_default(self):
        item, _ = self.started()
        self.assertEqual(self.menu(), ["Garder", "Retirer de Reprendre", "Remettre à zéro"])
        self.assertEqual(self.focused(), "GarderRien ne change")          # Entrée ne détruit rien
        self.press("Enter", wait=400)
        self.assertEqual(self.view(".shelf .card.wide", has_text="Direct").count(), 1)
        self.assertEqual(self.progress(item)["position"], 40)

    def test_hiding_keeps_the_progress(self):
        item, _ = self.started()
        self.menu()
        self.page.locator(".menu .opt", has_text="Retirer de Reprendre").click()
        self.until(lambda: self.view(".shelf .card.wide", has_text="Direct").count() == 0)
        self.assertEqual(self.progress(item)["position"], 40)

    def test_resetting_erases_the_progress(self):
        item, _ = self.started()
        self.menu()
        self.page.locator(".menu .opt", has_text="Remettre à zéro").click()
        self.until(lambda: self.view(".shelf .card.wide", has_text="Direct").count() == 0)
        self.assertIsNone(self.progress(item))
        self.assertEqual([c["item"]["id"] for c in self.api("/api/home")["continue"] if c["item"]["id"] == item["id"]], [])


class FakeShowTmdb:
    """TMDB pour les séries : deux séries portent le titre « Dark », la plus connue en premier."""
    found = [{"id": 70523, "name": "Dark", "first_air_date": "2017-12-01", "overview": "Série allemande", "vote_count": 5000, "popularity": 80},
             {"id": 2567, "name": "Dark", "first_air_date": "1950-01-01", "overview": "Vieille série", "vote_count": 2, "popularity": 1}]

    def __init__(self, key, language="fr-FR", **kwargs):
        pass

    def search_tv(self, name, year=None):
        return [dict(show) for show in self.found]

    def series(self, show_id):
        show = next(s for s in self.found if s["id"] == int(show_id))
        return {"name": show["name"], "overview": f"Fiche {show_id}", "first_air_date": show["first_air_date"], "poster_path": "/p.jpg", "genres": []}

    def season(self, show_id, number):
        return {}

    def aggregate_credits(self, show_id):
        return {"cast": []}

    def save_image(self, image_path, size, dest):
        Path(dest).write_bytes(fixtures.PNG)


class TestTmdbSheet(UiCase):
    """Choisir la fiche TMDB d'une série quand la recherche sur son nom tombe sur un homonyme (TMDB est simulé : voir FakeShowTmdb)."""

    def setUp(self):
        super().setUp()
        hub = self.run_.hub
        hub.cfg.tmdb_key = "fake"
        patcher = mock.patch("hub.enrich.Tmdb", FakeShowTmdb)
        patcher.start()
        self.addCleanup(self.forget, patcher)
        self.dark = self.item("series", "Dark")

    def forget(self, patcher):
        hub = self.run_.hub
        patcher.stop()
        hub.cfg.tmdb_key = ""
        hub.db.execute("DELETE FROM kv WHERE key LIKE 'enrich:series:%' OR key LIKE 'tmdb-pin:%'")
        hub.jobs.scan(hub.cfg.library("series"))                # le scan rend la série telle qu'elle était, sans fiche
        self.run_.wait_jobs()

    def open_sheet(self):
        self.open("#/i/" + self.dark["id"])
        self.view(".actions .btn", has_text="Changer la fiche TMDB").wait_for()

    def sheet(self):
        return self.api("/api/item/" + self.dark["id"])

    def test_a_sheet_is_searched_by_name_and_chosen(self):
        self.open_sheet()
        self.view(".actions .btn", has_text="Changer la fiche TMDB").click()
        self.page.locator(".prompt input").fill("Dark")
        self.page.locator(".prompt .primary").click()
        self.page.locator(".menu .opt", has_text="1950").click()
        self.until(lambda: self.sheet().get("tmdb") == 2567)
        self.assertEqual(self.sheet()["summary"], "Fiche 2567")

    def test_the_address_of_the_sheet_is_enough(self):
        self.open_sheet()
        self.view(".actions .btn", has_text="Changer la fiche TMDB").click()
        self.page.locator(".prompt input").fill("https://www.themoviedb.org/tv/2567-dark")
        self.page.locator(".prompt .primary").click()
        self.until(lambda: self.sheet().get("tmdb") == 2567)

    def test_a_search_that_hesitates_between_two_series_says_so_on_the_sheet(self):
        self.api("/api/enrich", {"lib": "series"})
        self.run_.wait_jobs()
        self.open("#/i/" + self.dark["id"])
        self.view(".info .doubt").wait_for()
        self.assertIn("titre partagé avec Dark (1950)", self.view(".info .doubt").inner_text())
        self.view(".actions .btn", has_text="Changer la fiche TMDB").click()
        self.page.locator(".prompt input").fill("Dark")
        self.page.locator(".prompt .primary").click()
        self.page.locator(".menu .opt", has_text="2017").click()
        self.until(lambda: self.view(".info .doubt").count() == 0)                # le doute est levé : on a choisi

    def test_a_film_has_no_such_button(self):
        self.open("#/i/" + self.item("films", "Inception")["id"])
        self.view(".actions .btn").first.wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Changer la fiche TMDB").count(), 0)

    def test_a_television_has_no_such_button(self):
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")
        self.open("#/i/" + self.dark["id"])
        self.view(".actions .btn").first.wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Changer la fiche TMDB").count(), 0)


class TestHeroImage(UiCase):
    """L'image de gauche de l'en-tête d'une fiche part du haut de l'encart, même quand le texte à côté est plus haut qu'elle."""

    def gap(self, target):
        self.open(target)
        self.view(".hero-in").wait_for()
        return self.view(".hero-in").evaluate("el => el.firstElementChild.getBoundingClientRect().top - el.getBoundingClientRect().top")

    def pages(self):
        album = self.item("musique", "Discovery")
        return {"film": "#/i/" + self.item("films", "Inception")["id"], "série": "#/i/" + self.item("series", "Dark")["id"], "album": "#/i/" + album["id"],
                "artiste": "#/artist/musique/" + self.api("/api/item/" + album["id"])["artist_id"], "saga": "#/saga/films?g=Saga%20DCEU"}

    def test_the_image_starts_at_the_top_on_every_kind_of_page(self):
        self.page.set_viewport_size({"width": 1000, "height": 800})            # des boutons qui passent à la ligne : le texte est plus haut que l'image
        for name, target in self.pages().items():
            self.assertAlmostEqual(self.gap(target), 0, delta=1, msg=name)

    def test_and_on_a_phone(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        for name, target in self.pages().items():
            self.assertAlmostEqual(self.gap(target), 0, delta=1, msg=name)


class TestGenreFromAddress(UiCase):
    """Un genre posé par l'adresse (`#/l/films?genre=Animation`) : filtre la bibliothèque s'il existe, retombe sur « Tous » sinon, et se retire avec les filtres."""

    @classmethod
    def add_media(cls, root):
        fixtures.write_mkv(root / "srv" / "Films" / "Coco (2017)" / "Coco (2017).mkv", fixtures.movie_blocks("Coco", "Un garçon.", "2017-10-27", "movie/354912", "Animation, Famille"), minutes=105)

    def genre_button(self):
        return self.view(".tools .btn.flt").first

    def test_a_genre_the_library_does_not_have_falls_back_to_all(self):
        self.open("#/l/films?genre=Inconnu")
        self.view(".grid .card").first.wait_for()
        self.assertEqual(self.genre_button().inner_text(), "Genre : Tous")
        total = self.api("/api/library/films?sagas=1")["total"]
        self.assertEqual(self.view(".grid .card").count(), total)                          # toute la bibliothèque (sagas regroupées), pas une liste vide
        self.assertTrue(self.view(".blank").count() == 0 or not self.view(".blank").first.is_visible())

    def test_removing_the_filters_still_clears_a_genre_that_came_from_the_address(self):
        self.open("#/l/films?genre=Animation")
        self.view(".grid .card").first.wait_for()
        self.view("input[type=search]").fill("zzzz")
        self.view("button", has_text="Retirer les filtres").click()                       # n'existe qu'une fois la liste vide relue : le clic l'attend
        self.until(lambda: self.genre_button().inner_text() == "Genre : Tous" and self.view(".grid .card").count() > 1, tries=100)      # la liste est relue, puis les boutons repeints


class TestPersonPage(UiCase):
    """Les noms d'une fiche (réalisateur, acteurs) ouvrent la page de la personne, au clavier comme à la souris."""

    @classmethod
    def add_media(cls, root):
        films = root / "srv" / "Films"
        fixtures.write_mkv(films / "Premier Essai (2001)" / "Premier Essai (2001).mkv", fixtures.movie_blocks("Premier Essai", "Un.", "2001-01-01", "movie/9001", "Drame", ["Jane Campion"], ["Holly Hunter (Ada)", "Zoé O'Brien (Flora)"]))
        fixtures.write_mkv(films / "Second Essai (2003)" / "Second Essai (2003).mkv", fixtures.movie_blocks("Second Essai", "Deux.", "2003-01-01", "movie/9002", "Drame", ["Jane Campion"], ["Holly Hunter (Mère)"]))

    def sheet(self):
        self.open("#/i/" + self.item("films", "Premier Essai")["id"])
        self.view(".cast .who").first.wait_for()

    def person(self, name):
        self.open("#/person?n=" + name)
        self.view(".shelf .card, .results > p").first.wait_for()

    def titles(self):
        return self.view(".shelf .card").evaluate_all("els => els.map(e => e.title)")

    def focused_key(self):
        return self.page.evaluate("document.activeElement && document.activeElement.getAttribute('data-key')")

    def test_clicking_an_actor_opens_the_page_with_their_titles_and_back_returns_to_the_sheet(self):
        self.sheet()
        sheet_url = self.page.url
        self.view(".cast .who", has_text="Holly Hunter").click()
        self.view(".shelf .card").first.wait_for()
        self.assertTrue(self.page.url.endswith("#/person?n=Holly%20Hunter"))
        self.assertEqual(self.view("h1").inner_text(), "Holly Hunter")
        self.assertEqual(self.view(".roles").inner_text(), "Acteur · 2 titres")           # aucun rôle de réalisateur : il n'est pas annoncé
        self.assertEqual(sorted(self.titles()), ["Premier Essai", "Second Essai"])
        self.assertTrue(self.page.evaluate("document.activeElement.classList.contains('card')"))      # le focus est sur la première carte
        self.press("Escape", wait=500)
        self.assertEqual(self.page.url, sheet_url)
        self.assertEqual(self.focused_key(), "who-a-Holly Hunter")                      # le focus retrouve le nom cliqué

    def test_the_director_line_links_each_name_and_keeps_its_sentence(self):
        self.sheet()
        self.assertEqual(self.view(".credit").first.inner_text(), "De Jane Campion")
        self.view(".credit .who").click()
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.view(".roles").inner_text(), "Réalisateur · 2 titres")

    def test_a_name_with_an_accent_and_an_apostrophe(self):
        self.sheet()
        self.view(".cast .who", has_text="O'Brien").click()
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Zoé O'Brien")
        self.assertEqual(self.titles(), ["Premier Essai"])
        self.person("zoe%20o'brien")                                                    # l'adresse écrite sans accent, en minuscules : même personne, graphie de la médiathèque
        self.assertEqual(self.view("h1").inner_text(), "Zoé O'Brien")
        self.assertEqual(self.titles(), ["Premier Essai"])

    def test_the_keyboard_goes_from_one_name_to_the_next_and_enter_opens_the_page(self):
        self.sheet()
        self.view(".cast .who").first.focus()
        self.assertEqual(self.focused_key(), "who-a-Holly Hunter")
        self.press("ArrowRight")
        self.assertEqual(self.focused_key(), "who-a-Zoé O'Brien")
        self.press("ArrowLeft")
        self.assertEqual(self.focused_key(), "who-a-Holly Hunter")
        self.press("Enter", wait=600)
        self.assertTrue(self.page.url.endswith("#/person?n=Holly%20Hunter"))

    def test_a_name_nobody_in_the_library_carries(self):
        self.person("Personne%20Inconnue")
        self.assertEqual(self.view("h1").inner_text(), "Personne Inconnue")
        self.assertEqual(self.view(".results > p").first.inner_text(), "Aucun titre pour Personne Inconnue dans ta médiathèque.")
        self.assertTrue(self.view(".roles").is_hidden())
        self.assertEqual(self.focused(), "Rechercher")                                  # une page sans carte garde de quoi viser à la télécommande
        self.press("Enter", wait=500)
        self.assertTrue(self.page.url.endswith("#/search"))

    def test_signs_that_are_part_of_the_address_survive_the_round_trip(self):
        self.person("A%26B%20%2BC%20%231%3F%20100%25%20%22x%22")                        # A&B +C #1? 100% "x"
        self.assertEqual(self.view("h1").inner_text(), 'A&B +C #1? 100% "x"')
        self.assertEqual(self.view(".results > p").first.inner_text(), 'Aucun titre pour A&B +C #1? 100% "x" dans ta médiathèque.')

    def test_coming_back_to_the_page_keeps_the_focus_on_the_card_that_was_opened(self):
        self.person("Holly%20Hunter")
        self.view(".shelf .card").first.wait_for()
        card = self.view(".shelf .card").first
        key = card.get_attribute("data-key")
        card.click()
        self.view(".hero").wait_for()
        self.press("Escape", wait=800)                                                  # la page se rafraîchit au retour
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.focused_key(), key)

    def test_a_name_is_inserted_as_text(self):
        self.person("%3Cb%3Ex%3C%2Fb%3E")                                              # <b>x</b>
        self.assertEqual(self.view("h1").inner_text(), "<b>x</b>")
        self.assertEqual(self.view("h1 b").count(), 0)


class FakePersonTmdb:
    """TMDB pour la page d'une personne : « Acteur Test » (8200) a joué deux films qu'on a (9101, 9102) et trois qu'on n'a pas ; « Réal Test » (8100) n'a réalisé que 9101."""
    calls = []
    credits = {
        "person/8200/movie_credits": {"cast": [
            {"id": 9101, "title": "Premier Film", "release_date": "2001-01-01", "order": 0, "vote_count": 900, "poster_path": "/p1.jpg"},
            {"id": 9102, "title": "Second Film", "release_date": "2003-01-01", "order": 1, "vote_count": 900, "poster_path": None},
            {"id": 9301, "title": "Absent Un", "release_date": "2010-05-05", "order": 0, "vote_count": 500, "poster_path": "/a1.jpg"},
            {"id": 9302, "title": "Absent Deux", "release_date": "2005-05-05", "order": 2, "vote_count": 500, "poster_path": None},
            {"id": 9303, "title": "<b>x</b>", "release_date": "2008-08-08", "order": 3, "vote_count": 500, "poster_path": None}]},
        "person/8100/movie_credits": {"crew": [{"id": 9101, "title": "Premier Film", "job": "Director", "release_date": "2001-01-01", "vote_count": 900, "poster_path": "/p1.jpg"}]},
        "person/8300/movie_credits": {"cast": [{"id": 9500, "title": "Film de la voix", "release_date": "2015-05-05", "order": 0, "vote_count": 400, "poster_path": None}]}}

    def __init__(self, key, language="fr-FR", **kwargs):
        pass

    def get(self, endpoint):
        from libraries.video.tmdb import TmdbError
        FakePersonTmdb.calls.append(endpoint)
        if endpoint not in self.credits:
            raise TmdbError("introuvable sur TMDB (HTTP 404)")
        return self.credits[endpoint]

    def save_image(self, image_path, size, dest):
        Path(dest).write_bytes(fixtures.PNG)


class TestPersonMissingUi(UiCase):
    """La page d'une personne dit aussi ses films qu'on n'a pas : un bouton pour l'administrateur qui les demande à TMDB, puis des cases grisées que la télécommande ne vise pas."""

    @classmethod
    def add_media(cls, root):
        films = root / "srv" / "Films"
        ids = {"Réal Test": 8100, "Acteur Test (Premier)": 8200, "Acteur Test (Second)": 8200}
        fixtures.write_mkv(films / "Premier Film (2001)" / "Premier Film (2001).mkv", fixtures.movie_blocks("Premier Film", "Un.", "2001-01-01", "movie/9101", "Drame", ["Réal Test"], ["Acteur Test (Premier)"], person_ids=ids))
        fixtures.write_mkv(films / "Second Film (2003)" / "Second Film (2003).mkv", fixtures.movie_blocks("Second Film", "Deux.", "2003-01-01", "movie/9102", "Drame", [], ["Acteur Test (Second)"], person_ids=ids))

    def setUp(self):
        super().setUp()
        hub = self.run_.hub
        FakePersonTmdb.calls = []
        hub.cfg.tmdb_key = "fake"
        self.addCleanup(setattr, hub.cfg, "tmdb_key", "")
        self.addCleanup(hub.db.execute, "DELETE FROM kv WHERE key LIKE 'personinfo:%'")

    def person(self, name="Acteur%20Test"):
        self.open("#/person?n=" + name)
        self.view(".shelf .card").first.wait_for()

    def ask(self, name="Acteur%20Test"):
        """Ouvre la page, clique « Chercher ses films manquants » (TMDB simulé) et attend que la tâche soit finie et la page rechargée."""
        self.person(name)
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
            self.view("[data-key=person-enrich]").click()
            self.until(lambda: FakePersonTmdb.calls, tries=100)
            self.run_.wait_jobs()
            self.page.locator("#toast.on", has_text="Terminé").or_(self.page.locator("#toast.on", has_text="Aucun film ne manque")).wait_for()

    def missing_titles(self):
        return self.view(".card.wanted .t").all_inner_texts()

    def test_nothing_is_missing_until_tmdb_was_asked_and_the_button_is_there(self):
        self.person()
        self.assertEqual(self.view(".card.wanted").count(), 0)
        self.assertEqual(self.view("[data-key=person-enrich]").inner_text().strip(), "Chercher ses films manquants")
        self.assertTrue(self.view("[data-key=person-enrich]").evaluate("e => e.classList.contains('f')"))          # atteignable à la télécommande

    def test_the_button_asks_tmdb_then_the_page_greys_out_what_is_missing_the_latest_first(self):
        self.ask()
        self.assertEqual(self.missing_titles(), ["Absent Un", "<b>x</b>", "Absent Deux"])          # 2010, 2008, 2005 ; les deux films qu'on a n'y sont pas
        self.assertEqual(self.view(".card.wanted .s").all_inner_texts(), ["2010 · Acteur", "2008 · Acteur", "2005 · Acteur"])
        self.assertEqual(self.view("section.missing h2").inner_text(), "Films qui manquent · 3")
        self.assertEqual(self.view("section.missing .part").count(), 0)          # le rang n'a pas de sens ici : pas de pastille
        self.assertEqual(FakePersonTmdb.calls, ["person/8200/movie_credits"])
        self.assertEqual(self.view("section.missing b").count(), 0)          # « <b>x</b> » est un titre, pas du HTML

    def test_the_greyed_films_have_nothing_to_reach_or_open(self):
        self.ask()
        self.assertEqual(self.view(".card.wanted.f").count(), 0)
        self.assertEqual(self.view(".card.wanted[href], .card.wanted [data-key]").count(), 0)
        self.view(".shelf .card.f").first.focus()
        self.press("ArrowDown", "ArrowDown", "ArrowRight", "ArrowDown")
        self.assertFalse(self.page.evaluate("document.activeElement.classList.contains('wanted')"))
        self.assertEqual(self.view(".card.wanted").first.get_attribute("title"), "Absent Un (manquant)")

    def test_the_remote_can_press_the_button_and_keeps_the_focus_on_it_after_the_page_is_redrawn(self):
        self.person()
        button = self.view("[data-key=person-enrich]")
        button.focus()
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
            self.press("Enter", wait=200)
            self.view(".card.wanted").first.wait_for()
            self.run_.wait_jobs()
        self.assertEqual(self.page.evaluate("document.activeElement && document.activeElement.getAttribute('data-key')"), "person-enrich")

    def test_a_second_press_while_the_search_runs_asks_nothing_more(self):
        self.person()
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
            self.view("[data-key=person-enrich]").dblclick()
            self.view(".card.wanted").first.wait_for()
            self.run_.wait_jobs()
        self.assertEqual(FakePersonTmdb.calls, ["person/8200/movie_credits"])

    def test_on_a_phone_the_page_does_not_scroll_sideways(self):
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.ask()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        self.assertTrue(self.view("[data-key=person-enrich]").evaluate("e => e.getBoundingClientRect().height >= 36"))          # une cible tactile assez grande

    def test_a_failed_search_is_never_presented_as_nothing_missing(self):
        with mock.patch.object(FakePersonTmdb, "credits", {}):                      # TMDB ne connaît pas cette personne : la tâche finit, comptée en échec
            self.person()
            with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
                self.view("[data-key=person-enrich]").click()
                self.page.locator("#toast.on", has_text="La recherche a échoué").wait_for(timeout=15000)
                self.run_.wait_jobs()
        self.assertNotIn("Aucun film ne manque", self.page.inner_text("#toast"))
        self.assertEqual(self.view(".card.wanted").count(), 0)
        self.until(lambda: self.view("[data-key=person-enrich]").get_attribute("aria-busy") is None)          # le bouton sert à nouveau

    def test_a_refused_key_is_a_failure_too(self):
        self.person()
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb), mock.patch.object(FakePersonTmdb, "get", side_effect=__import__("libraries.video.tmdb", fromlist=["TmdbAuthError"]).TmdbAuthError("clé refusée")):
            self.view("[data-key=person-enrich]").click()
            self.page.locator("#toast.on", has_text="La recherche a échoué").wait_for(timeout=15000)
            self.run_.wait_jobs()

    def test_a_person_with_nothing_missing_is_told_so(self):
        self.ask("R%C3%A9al%20Test")
        self.assertIn("Aucun film ne manque", self.page.inner_text("#toast"))
        self.assertEqual(self.view(".card.wanted").count(), 0)

    def test_a_person_already_known_shows_her_missing_films_without_a_click(self):
        self.run_.hub.db.kv_set("personinfo:8200", {"at": 1, "films": [{"tmdb": 9101, "title": "Premier Film", "year": "2001", "date": "2001-01-01", "role": "actor", "poster": ""},
                                                                      {"tmdb": 9400, "title": "Réalisé ailleurs", "year": "1999", "date": "1999-09-09", "role": "director", "poster": ""}]})
        self.person()
        self.assertEqual(self.missing_titles(), ["Réalisé ailleurs"])
        self.assertEqual(self.view(".card.wanted .s").inner_text(), "1999 · Réalisateur")

    def test_a_movie_library_has_a_missing_movies_button_and_the_general_one_is_gone(self):
        self.open_libraries_detail()
        self.assertEqual(self.view("[data-key=enrich-people]").count(), 0)                    # plus de bouton global dans l'en-tête du panneau
        self.assertNotIn("bibliothèques de films", self.page.inner_text("body"))
        button = self.view("[data-key=people-films]")
        self.assertEqual(button.inner_text().strip(), "Films manquants")                       # le bouton d'une bibliothèque de films
        self.assertIn("réalisateurs et des acteurs de cette bibliothèque", button.get_attribute("title"))
        self.assertIn("une heure ou plus", button.get_attribute("title"))                       # la durée du traitement se lit toujours dans l'infobulle
        self.assertTrue(button.evaluate("e => e.classList.contains('f')"))                     # atteignable à la télécommande
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
            button.click()
            self.page.locator("#toast.on", has_text="Recherche des films manquants lancée : Films").wait_for()
            self.until(lambda: len(FakePersonTmdb.calls) >= 2, tries=100)
            self.run_.wait_jobs()
        self.assertEqual(sorted(FakePersonTmdb.calls), ["person/8100/movie_credits", "person/8200/movie_credits"])
        self.person()                                                                          # sans cliquer sur la page : tout est déjà là
        self.assertEqual(self.missing_titles(), ["Absent Un", "<b>x</b>", "Absent Deux"])
        self.assertEqual(self.view("[data-key=person-enrich]").count(), 1)

    def give_a_voice_to_the_show(self):
        """La série « Vidéo Série » a un acteur dont l'enrichissement TMDB a gardé l'identifiant (8300) ; l'état d'avant est remis à la fin."""
        db = self.run_.hub.db
        row = db.one("SELECT id, meta FROM items WHERE lib='series' AND title='Vidéo Série'")
        meta = json.loads(row["meta"])
        db.update_item_meta(row["id"], meta={"actors": [{"name": "Voix Test", "role": "Narrateur"}], "person_ids": {"Voix Test": 8300}})
        self.addCleanup(db.execute, "UPDATE items SET meta=? WHERE id=?", (row["meta"], row["id"]))
        self.assertNotIn("person_ids", meta)

    def open_libraries_detail(self):
        self.open("#/admin")
        self.view("[data-key=libs-toggle]").click()
        self.view("[data-key=scan-films]").wait_for()

    def test_a_category_has_its_own_button_for_the_people_of_its_series(self):
        self.give_a_voice_to_the_show()
        self.open_libraries_detail()
        for lib in ("films", "series"):
            self.assertEqual(self.view(f"[data-key=people-{lib}]").count(), 1, lib)                  # films et séries : une catégorie chacune
        self.assertEqual(self.view("[data-key=people-musique], [data-key=people-livres]").count(), 0)
        self.assertEqual(self.view("[data-key=people-films]").inner_text().strip(), "Films manquants")                  # d'après le genre de la bibliothèque : films...
        self.assertEqual(self.view("[data-key=people-series]").inner_text().strip(), "Films des personnes (TMDB)")       # ...séries (genre « shows »)
        self.assertIn("Les identifiants viennent de", self.view("[data-key=people-series]").get_attribute("title"))
        self.assertTrue(self.view("[data-key=people-series]").evaluate("e => e.classList.contains('f')"))
        with mock.patch("hub.enrich.Tmdb", FakePersonTmdb):
            self.view("[data-key=people-series]").click()
            self.page.locator("#toast.on", has_text="Recherche des films manquants lancée").wait_for()
            self.until(lambda: FakePersonTmdb.calls, tries=100)
            self.run_.wait_jobs()
        self.assertEqual(FakePersonTmdb.calls, ["person/8300/movie_credits"])                      # les personnes de cette catégorie seulement, pas celles des films
        self.open("#/person?n=Voix%20Test")
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.missing_titles(), ["Film de la voix"])
        self.assertEqual(self.view("[data-key=person-enrich]").count(), 1)                         # et la page de cette personne a son bouton

    def test_the_category_buttons_need_a_tmdb_key_too(self):
        self.run_.hub.cfg.tmdb_key = ""
        self.open("#/admin")
        self.view("[data-key=libs-toggle]").click()
        self.view("[data-key=scan-films]").wait_for()
        self.assertEqual(self.view("[data-key^=people-]").count(), 0)

    def test_without_a_tmdb_key_there_is_no_button(self):
        self.run_.hub.cfg.tmdb_key = ""
        self.person()
        self.assertEqual(self.view("[data-key=person-enrich]").count(), 0)

    def test_a_person_without_an_id_has_no_button(self):
        self.open("#/person?n=Personne%20Inconnue")
        self.view(".results > p").first.wait_for()
        self.assertEqual(self.view("[data-key=person-enrich]").count(), 0)

    def test_an_ordinary_account_sees_the_list_but_has_no_button(self):
        self.run_.hub.db.kv_set("personinfo:8200", {"at": 1, "films": [{"tmdb": 9400, "title": "Réalisé ailleurs", "year": "1999", "date": "1999-09-09", "role": "director", "poster": ""}]})
        self.open("#/")
        self.page.evaluate("Hub.session.auth = true; Hub.session.user = { id: 'x', name: 'bob', role: 'user' }; location.hash = '#/person?n=Acteur%20Test';")
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.missing_titles(), ["Réalisé ailleurs"])
        self.assertEqual(self.view("[data-key=person-enrich]").count(), 0)


class TestPeopleInSearchUi(UiCase):
    """La recherche propose aussi les personnes : une rangée de pastilles « nom · N titres » au-dessus des résultats."""

    @classmethod
    def add_media(cls, root):
        films = root / "srv" / "Films"
        fixtures.write_mkv(films / "Premier Essai (2001)" / "Premier Essai (2001).mkv", fixtures.movie_blocks("Premier Essai", "Un.", "2001-01-01", "movie/9001", "Drame", ["Jane Campion"], ["Holly Hunter (Ada)", "Holly Berry (Zed)", "Zoé O'Brien (Flora)"]))
        fixtures.write_mkv(films / "Second Essai (2003)" / "Second Essai (2003).mkv", fixtures.movie_blocks("Second Essai", "Deux.", "2003-01-01", "movie/9002", "Drame", ["Jane Campion"], ["Holly Hunter (Mère)"]))
        fixtures.write_mkv(films / "Hunter Blues (2010)" / "Hunter Blues (2010).mkv", fixtures.movie_blocks("Hunter Blues", "Trois.", "2010-01-01", "movie/9003", "Drame"))      # un titre qui porte le même texte qu'un nom

    def search(self, text):
        self.open("#/search")
        self.view("input[type=search]").fill(text)
        self.view(".people .chip, .shelf .card, .results > p").first.wait_for()

    def chips(self):
        return self.view(".people .chip").evaluate_all("els => els.map(e => e.textContent)")

    def test_a_search_shows_the_people_with_their_number_of_titles_most_titles_first(self):
        self.search("holly")
        self.assertEqual(self.view(".people h2").inner_text(), "Personnes")
        self.assertEqual(self.chips(), ["Holly Hunter · 2 titres", "Holly Berry · 1 titre"])
        self.assertEqual(self.view(".shelf .card").count(), 0)                         # aucun titre ne porte ce texte : pas de message « aucun résultat » non plus
        self.assertEqual(self.view(".results > p").count(), 0)

    def test_clicking_a_chip_opens_the_page_of_the_person_and_back_returns_to_the_search(self):
        self.search("hunter")
        self.view(".people .chip").click()
        self.shown("#/person?n=Holly%20Hunter")                                          # l'adresse change avant la page : jusque-là, `.shelf .card` serait la recherche parquée
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Holly Hunter")
        self.press("Escape")
        self.shown("#/search")
        self.assertEqual(self.chips(), ["Holly Hunter · 2 titres"])                  # la recherche est retrouvée telle quelle

    def test_accents_case_and_an_apostrophe(self):
        self.search("ZOE O'B")
        self.assertEqual(self.chips(), ["Zoé O'Brien · 1 titre"])
        self.view(".people .chip").click()
        self.page.locator("#views > .view.person:visible").wait_for()                   # la page de la personne, pas encore la recherche
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Zoé O'Brien")

    def test_the_remote_reaches_the_chips_first_then_moves_between_them_and_opens_one(self):
        self.search("holly")
        self.view("input[type=search]").focus()
        self.press("Enter")                                                             # du champ aux résultats : la première pastille
        self.assertEqual(self.focused(), "Holly Hunter · 2 titres")
        self.press("ArrowRight")
        self.assertEqual(self.focused(), "Holly Berry · 1 titre")
        self.press("ArrowLeft", "Enter", wait=600)
        self.assertTrue(self.page.url.endswith("#/person?n=Holly%20Hunter"))

    def test_without_any_person_there_is_no_row(self):
        self.search("essai")
        self.assertEqual(self.view(".people").count(), 0)                               # personne ne porte ce texte : pas de rangée
        self.assertEqual(self.view(".shelf .card").count(), 2)

    def test_the_chips_come_before_the_titles_that_match(self):
        self.search("hunter")
        self.view(".shelf .card").first.wait_for()
        self.assertEqual(self.chips(), ["Holly Hunter · 2 titres"])
        self.assertEqual(self.view(".results > *").evaluate_all("els => els.map(e => e.className)"), ["people", "shelf"])
        self.assertEqual(self.view(".shelf .card").evaluate_all("els => els.map(e => e.title)"), ["Hunter Blues"])

    def test_nothing_found_says_so_and_a_short_search_asks_for_more(self):
        self.search("zzzz")
        self.assertEqual(self.view(".people").count(), 0)
        self.assertEqual(self.view(".results > p").inner_text(), "Aucun résultat pour « zzzz ».")
        self.view("input[type=search]").fill("z")
        self.view(".results > p", has_text="Tape au moins deux lettres.").wait_for()
        self.assertEqual(self.view(".people").count(), 0)


class TestLibraryToolbar(UiCase):
    """La barre d'une bibliothèque : « Genre : Tous » (le nom en gras, la valeur en poids normal, une largeur qui ne bouge pas) et le compteur de la musique."""

    def filters(self):
        return self.view(".tools .btn.flt")

    def widths(self):
        return self.filters().evaluate_all("els => els.map(e => e.getBoundingClientRect().width)")

    def test_the_name_of_each_filter_comes_first_in_bold_then_its_value_in_normal_weight(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        self.assertEqual(self.filters().all_inner_texts(), ["Genre : Tous", "Tri : Titre", "Statut : Tous"])
        weights = self.filters().evaluate_all("els => els.map(e => [+getComputedStyle(e.querySelector('b')).fontWeight, +getComputedStyle(e.querySelector('.val')).fontWeight])")
        self.assertTrue(all(name >= 600 and value <= 500 for name, value in weights), weights)
        self.assertEqual(self.filters().evaluate_all("els => els.map(e => e.querySelector('b').textContent)"), ["Genre :", "Tri :", "Statut :"])

    def test_the_best_rated_sort_puts_the_rated_film_first_and_is_not_offered_for_books(self):
        db = self.run_.hub.db
        inception = self.item("films", "Inception")["id"]
        db.update_item_meta(inception, meta={"tmdb_rating": {"tmdb": 27205, "vote": 8.4, "votes": 37000, "at": 0}})
        self.addCleanup(db.update_item_meta, inception, meta={"tmdb_rating": None})
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        self.filters().nth(1).click()
        self.page.locator(".veil .opt", has_text="Mieux notés").click()
        self.until(lambda: self.view(".grid .card").first.get_attribute("title") == "Inception")
        self.assertEqual(self.filters().nth(1).inner_text(), "Tri : Mieux notés")
        self.open("#/l/livres")
        self.view(".grid .card").first.wait_for()
        self.filters().nth(1).click()
        self.page.wait_for_selector(".veil .opt")
        self.assertEqual(self.page.locator(".veil .opt", has_text="Mieux notés").count(), 0)

    def test_the_buttons_keep_their_width_whatever_their_value(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        before = self.widths()
        self.filters().nth(0).click()
        self.page.locator(".veil .opt", has_text="Science-Fiction").click()
        self.page.wait_for_timeout(600)
        self.assertEqual(self.filters().nth(0).inner_text(), "Genre : Science-Fiction")
        self.filters().nth(1).click()
        self.page.locator(".veil .opt").nth(1).click()
        self.page.wait_for_timeout(600)
        self.assertNotEqual(self.filters().nth(1).inner_text(), "Tri : Titre")
        self.assertEqual(self.widths(), before)

    def test_a_value_too_long_for_its_button_is_cut_and_the_button_does_not_grow(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        before = self.widths()
        self.filters().nth(0).evaluate("el => { el.querySelector('.val').textContent = 'Un genre au nom vraiment très très long pour ce bouton'; }")
        self.assertEqual(self.widths(), before)
        self.assertTrue(self.filters().nth(0).evaluate("el => { var l = el.querySelector('.lbl'); return l.scrollWidth > l.clientWidth && getComputedStyle(l).textOverflow === 'ellipsis'; }"))

    def test_the_buttons_stay_readable_on_a_tv_and_on_a_phone(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        self.page.evaluate("document.documentElement.setAttribute('data-device', 'tv')")
        self.assertTrue(self.filters().evaluate_all("els => els.every(e => { var l = e.querySelector('.lbl'); return l.scrollWidth <= l.clientWidth && parseFloat(getComputedStyle(e).fontSize) >= 18; })"))
        self.page.evaluate("document.documentElement.setAttribute('data-device', 'phone')")
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.assertEqual(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), True)
        self.assertTrue(self.filters().evaluate_all("els => els.every(e => { var l = e.querySelector('.lbl'); return l.scrollWidth <= l.clientWidth; })"))

    def test_the_music_counter_gives_the_artists_and_the_albums_in_both_views(self):
        self.addCleanup(lambda: self.api("/api/me/settings", {"music_view": "albums"}))
        for view in ("albums", "artists"):
            self.api("/api/me/settings", {"music_view": view})
            self.open("#/l/musique")
            self.view(".grid .card").first.wait_for()
            self.assertEqual(self.view(".count").inner_text(), "1 artiste · 1 album", view)

    def test_the_counters_of_the_other_libraries_do_not_change(self):
        self.open("#/l/series")
        self.view(".grid .card").first.wait_for()
        total = self.api("/api/library/series")["total"]
        self.assertEqual(self.view(".count").inner_text(), f"{total} série" + ("s" if total > 1 else ""))


class TestArtistsView(UiCase):
    """La bibliothèque de musique par artiste : un artiste par carte, sans ses albums, quand le compte l'a choisi dans ses paramètres."""

    def choose(self, view):
        self.api("/api/me/settings", {"music_view": view})
        self.addCleanup(lambda: self.api("/api/me/settings", {"music_view": "albums"}))

    def artists(self):
        return self.view(".grid .card[data-type=artist]")

    def test_by_default_the_library_shows_the_albums_under_their_artists(self):
        self.open("#/l/musique")
        self.view(".grid .card").first.wait_for()
        self.assertGreaterEqual(self.view(".grid .card[data-type=album]").count(), 1)
        self.assertEqual(self.artists().count(), 0)
        self.assertGreaterEqual(self.view("h3.grp").count(), 1)

    def test_an_account_that_chose_artists_sees_one_card_per_artist_without_albums(self):
        self.choose("artists")
        self.open("#/l/musique")
        self.artists().first.wait_for()
        self.assertEqual(self.view(".grid .card[data-type=album]").count(), 0)
        self.assertEqual(self.view("h3.grp").count(), 0)
        total = self.api("/api/library/musique?view=artists")["total"]
        self.assertEqual(self.artists().count(), total)
        self.assertIn("album", self.artists().first.inner_text())
        out = self.api("/api/library/musique?view=artists")
        self.assertEqual(self.view(".count").inner_text(), f"{out['artists']} artiste" + ("s" if out["artists"] > 1 else "") + " · " + f"{out['albums']} album" + ("s" if out["albums"] > 1 else ""))      # les artistes et leurs albums

    def test_a_card_opens_the_page_of_its_artist(self):
        self.choose("artists")
        self.open("#/l/musique")
        card = self.artists().first
        card.wait_for()
        name = card.get_attribute("title")
        card.click()
        self.page.wait_for_url("**#/artist/musique/*")
        self.view("h1", has_text=name).wait_for()

    def test_the_card_falls_back_to_the_cover_of_the_first_album_when_there_is_no_photo(self):
        self.choose("artists")
        self.open("#/l/musique")
        self.artists().first.wait_for()
        self.until(lambda: "/img/item/" in (self.artists().first.locator("img").first.get_attribute("src") or ""))

    def test_an_album_title_in_the_filter_finds_its_artist(self):
        self.choose("artists")
        self.open("#/l/musique")
        self.artists().first.wait_for()
        self.view("input[type=search]").fill("discovery")
        self.until(lambda: self.artists().count() == 1)
        self.assertEqual(self.artists().first.get_attribute("title"), "Daft Punk")

    def test_the_choice_made_in_the_settings_applies_when_coming_back_to_the_library(self):
        self.addCleanup(lambda: self.api("/api/me/settings", {"music_view": "albums"}))
        self.open("#/l/musique")
        self.view(".grid .card[data-type=album]").first.wait_for()
        self.open("#/settings?tab=preferences")
        panel = self.view(".panel", has_text="Affichage")
        panel.locator(".line", has_text="musique").locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Artistes").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["music_view"] == "artists")
        self.page.locator("#nav .link", has_text="Musique").click()
        self.artists().first.wait_for()
        self.assertEqual(self.view(".grid .card[data-type=album]").count(), 0)


class TestTextSize(UiCase):
    def root_size(self):
        return self.page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")

    def test_the_text_is_the_size_of_the_device_until_a_smaller_one_is_chosen(self):
        self.open("#/settings?tab=preferences")
        self.assertEqual(self.page.evaluate("document.documentElement.getAttribute('data-text')"), None)
        normal = self.root_size()
        self.view('button[data-key="textSize"]').click()
        self.page.locator(".menu .opt .lbl", has_text="Petite").click()
        self.until(lambda: self.root_size() < normal)
        self.assertAlmostEqual(self.root_size() / normal, 0.88, places=2)

    def test_the_choice_is_kept_and_does_not_depend_on_the_device(self):
        for device in ("pc", "tv"):
            with self.subTest(device=device):
                page = self.context.new_page()
                page.add_init_script(f"localStorage.setItem('hub.device', JSON.stringify('{device}'))")
                page.goto(self.base + "/")
                page.evaluate("localStorage.removeItem('hub.textSize')")          # le contexte garde le choix du tour précédent
                page.reload()
                page.wait_for_selector("#nav .link", state="attached")
                size = lambda: page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
                normal = size()
                page.evaluate("localStorage.setItem('hub.textSize', JSON.stringify('small'))")
                page.reload()
                page.wait_for_selector("#nav .link", state="attached")
                self.assertAlmostEqual(size() / normal, 0.88, places=2)
                page.close()


class TestLibraryStatusList(UiCase):
    """« Statut : Tous » : une liste déroulante comme « Genre » et « Tri » (Tous · À voir · En cours · Terminés), accordée d'après le genre de la bibliothèque, pas d'après son nom."""

    def setUp(self):
        super().setUp()
        film = self.item("films", "Inception")
        self.addCleanup(self.api, f"/api/item/{film['id']}/seen", {"done": False})
        self.film = film

    def status(self):
        return self.view(".tools .btn.flt", has_text="Statut")

    def options(self):
        return self.page.locator(".veil .opt .lbl").all_inner_texts()

    def pick(self, label):
        self.status().click()
        self.page.locator(".veil .opt", has_text=label).click()
        self.page.wait_for_selector(".veil", state="detached")

    def titles(self):
        return self.view(".grid .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")

    def open_library(self, lib="films"):
        self.open("#/l/" + lib)
        self.view(".grid .card").first.wait_for()

    def test_the_list_sits_with_genre_and_sort_in_the_same_component_and_says_all_at_first(self):
        self.open_library()
        self.assertEqual(self.view(".tools .btn.flt").all_inner_texts(), ["Genre : Tous", "Tri : Titre", "Statut : Tous"])
        self.assertEqual(self.view(".tools .btn.flt").evaluate_all("els => els.map(e => e.className)"), ["btn f flt wide"] * 3)         # la même fabrique, le même style
        self.assertEqual(self.status().evaluate("e => e.querySelector('b').textContent"), "Statut :")
        self.assertEqual(self.view(".seg").count(), 0)
        self.assertEqual(self.view(".tools .mark, .tools [role=radio]").count(), 0)             # ni coche, ni fond plein : du texte, comme les deux autres

    def test_the_options_are_those_of_the_kind_of_the_library_movies_in_the_masculine_shows_and_books_in_the_feminine(self):
        for lib, want in (("films", ["Tous", "À voir", "En cours", "Terminés"]), ("series", ["Toutes", "À voir", "En cours", "Terminées"]), ("livres", ["Toutes", "À voir", "En cours", "Terminées"])):
            with self.subTest(lib=lib):
                self.open_library(lib)
                self.status().click()
                self.page.wait_for_selector(".veil .opt")
                self.assertEqual(self.options(), want)
                self.assertEqual(self.page.locator(".veil .opt.on .lbl").all_inner_texts(), [want[0]])
                self.press("Escape", wait=300)
                self.assertEqual(self.status().inner_text(), "Statut : " + want[0])

    def test_the_agreement_follows_the_kind_never_the_name_of_the_library(self):
        self.open("#/")
        self.page.evaluate("Hub.libs.forEach(l => { if (l.id === 'series') l.label = 'Films'; if (l.id === 'films') l.label = 'Séries'; })")      # les bibliothèques se renomment
        self.open_library("series")
        self.status().click()
        self.page.wait_for_selector(".veil .opt")
        self.assertEqual(self.options(), ["Toutes", "À voir", "En cours", "Terminées"])
        self.press("Escape", wait=300)
        self.open_library("films")
        self.status().click()
        self.page.wait_for_selector(".veil .opt")
        self.assertEqual(self.options(), ["Tous", "À voir", "En cours", "Terminés"])

    def test_each_choice_asks_the_server_for_its_state_and_shows_what_it_says(self):
        self.api("/api/progress", {"entry": self.entries("films", "Inception")[0]["id"], "position": 600, "duration": 8880})
        asked = []
        self.page.on("request", lambda r: asked.append(r.url) if "/api/library/films" in r.url else None)
        self.open_library()
        for label, state in (("À voir", "todo"), ("En cours", "progress"), ("Terminés", "done"), ("Tous", "")):
            del asked[:]
            self.pick(label)
            self.assertEqual(self.status().inner_text(), "Statut : " + label)
            self.until(lambda: asked)
            self.assertTrue(f"&filter={state}&" in asked[-1] if state else "filter=" not in asked[-1], asked[-1])
            wanted = self.api("/api/library/films?sagas=1&limit=240" + (f"&filter={state}" if state else ""))["total"]
            self.until(lambda wanted=wanted: self.view(".grid .card").count() == wanted or (wanted == 0 and self.view("button", has_text="Retirer les filtres").count()))
        self.pick("En cours")
        self.until(lambda: self.view(".grid .card").count() == 1)
        self.assertIn(self.film["id"], self.titles())
        self.pick("À voir")
        self.until(lambda: self.film["id"] not in self.titles() and self.view(".grid .card").count() > 1)

    def test_the_series_choice_asks_for_done_and_says_terminees(self):
        asked = []
        self.page.on("request", lambda r: asked.append(r.url) if "/api/library/series" in r.url else None)
        self.open_library("series")
        self.pick("Terminées")
        self.until(lambda: any("filter=done" in u for u in asked))
        self.assertEqual(self.status().inner_text(), "Statut : Terminées")

    def test_the_value_changes_at_the_choice_not_when_the_list_arrives(self):
        self.open_library()
        held = []
        self.page.route("**/api/library/films*", lambda route: held.append(route))                 # la réponse du serveur est retenue (un NAS lent)
        self.pick("En cours")
        self.until(lambda: held)
        self.assertEqual(self.status().inner_text(), "Statut : En cours")
        self.page.unroute("**/api/library/films*")
        self.until(lambda: self.view(".grid .card").count() == self.api("/api/library/films?sagas=1&filter=progress")["total"])
        self.assertEqual(self.status().inner_text(), "Statut : En cours")

    def test_a_film_seen_to_the_end_moves_to_done(self):
        self.api(f"/api/item/{self.film['id']}/seen", {"done": True})
        self.open_library()
        self.pick("Terminés")
        self.until(lambda: self.titles() == [self.film["id"]])

    def test_an_empty_choice_says_so_and_removing_the_filters_goes_back_to_all(self):
        self.open_library()
        total = self.view(".grid .card").count()
        self.pick("Terminés")
        self.view("button", has_text="Retirer les filtres").wait_for()                       # rien de terminé : la liste est vide
        self.assertEqual(self.view("h2", has_text="Rien ne correspond").count(), 1)
        self.view("button", has_text="Retirer les filtres").click()
        self.until(lambda: self.status().inner_text() == "Statut : Tous" and self.view(".grid .card").count() == total, tries=100)

    def test_the_music_library_has_no_status_list(self):
        self.open("#/l/musique")
        self.view(".tools").first.wait_for()
        self.assertEqual(self.view(".tools .btn.flt", has_text="Statut").count(), 0)

    def test_the_list_fits_one_clean_row_on_a_phone_without_overflowing(self):
        self.open_library()
        self.page.evaluate("document.documentElement.setAttribute('data-device', 'phone')")
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        box = self.status().bounding_box()
        self.assertTrue(0 <= box["x"] and box["x"] + box["width"] <= 390, box)
        self.assertTrue(self.status().evaluate("e => { var l = e.querySelector('.lbl'); return l.scrollWidth <= l.clientWidth; }"))
        sort = self.view(".tools .btn.flt", has_text="Tri").bounding_box()
        self.assertGreater(box["y"], sort["y"] - 1)                                          # pas par-dessus la recherche


class TestLibraryStatusListOnTv(UiCase):
    """À la télécommande (data-device="tv") : le focus se voit sur la liste, Entrée l'ouvre, les flèches choisissent, Échap la referme sans rien changer."""

    def setUp(self):
        super().setUp()
        self.page.add_init_script("localStorage.setItem('hub.device', JSON.stringify('tv'))")

    def status(self):
        return self.view(".tools .btn.flt", has_text="Statut")

    def open_library(self, lib="films"):
        self.open("#/l/" + lib)
        self.view(".grid .card").first.wait_for()

    def test_the_focus_is_visible_enter_opens_the_arrows_choose_and_escape_closes_without_changing(self):
        self.open_library()
        self.assertEqual(self.page.evaluate("document.documentElement.getAttribute('data-device')"), "tv")
        self.status().focus()
        self.assertEqual(self.focused(), "Statut : Tous")
        outline = self.status().evaluate("e => getComputedStyle(e).outlineStyle + ' ' + getComputedStyle(e).outlineWidth")
        self.assertTrue(outline.startswith("solid") and outline != "solid 0px", outline)         # le focus se voit de loin
        self.press("Enter", wait=400)
        self.page.wait_for_selector(".veil .opt")
        self.assertEqual(self.focused(), "Tous")                                                 # le focus part sur le choix en cours
        self.press("Escape", wait=400)
        self.assertEqual(self.page.locator(".veil").count(), 0)
        self.assertEqual(self.status().inner_text(), "Statut : Tous")
        self.assertEqual(self.focused(), "Statut : Tous")                                        # et revient sur la liste
        self.press("Enter", wait=400)
        self.press("ArrowDown", wait=200)
        self.assertEqual(self.focused(), "À voir")
        self.press("Enter", wait=500)
        self.until(lambda: self.status().inner_text() == "Statut : À voir")
        self.assertEqual(self.focused(), "Statut : À voir")

    def test_the_arrows_reach_the_list_from_the_other_filters(self):
        self.open_library()
        self.view(".tools .btn.flt", has_text="Tri").focus()
        self.press("ArrowRight", wait=200)
        self.assertEqual(self.focused(), "Statut : Tous")


class TestHome(UiCase):
    """L'accueil : un bonjour, ce qu'on reprend, puis les derniers ajouts de toutes les bibliothèques, mêlés."""

    def latest_from_api(self):
        libs = self.api("/api/home")["libraries"]
        return {it["id"]: it["added"] for lib in libs for it in lib["recent"]}

    def expected_latest(self):
        """Les ids que l'accueil doit montrer : ce que chaque bibliothèque rend, mêlé du plus récent au plus ancien, 24 au plus."""
        mixed = [(-it["added"], i, k, it["id"]) for i, lib in enumerate(self.api("/api/home")["libraries"]) for k, it in enumerate(lib["recent"])]
        return [row[3] for row in sorted(mixed)][:24]

    def test_the_home_says_hello_and_mixes_the_latest_additions_of_every_library(self):
        self.open()
        self.view(".shelf.latest .card").first.wait_for()
        self.assertEqual(self.view(".greeting").inner_text(), "Bonjour")                       # serveur ouvert : pas de nom à dire
        cards = self.view(".shelf.latest .card")
        known = self.latest_from_api()
        shown = cards.evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        self.assertEqual(shown, self.expected_latest())
        self.assertLessEqual(len(shown), 24)
        libs = set(cards.evaluate_all("els => els.map(e => e.getAttribute('data-lib'))"))
        self.assertGreaterEqual(len(libs), 3)
        added = [known[key] for key in shown]
        self.assertEqual(added, sorted(added, reverse=True))                                    # du plus récent au plus ancien
        self.assertEqual(self.view(".shelf[data-lib]").count(), 0)                              # plus d'étagère par bibliothèque
        self.assertEqual(self.view(".card.more").count(), 0)

    def test_each_card_has_a_dot_in_the_colour_of_its_library_and_no_word_for_its_kind(self):
        self.open()
        self.view(".shelf.latest .card").first.wait_for()
        for lib, colour in (("films", "rgb(242, 180, 65)"), ("series", "rgb(76, 195, 176)"), ("musique", "rgb(255, 138, 92)"), ("livres", "rgb(155, 209, 107)")):
            card = self.view(f".shelf.latest .card[data-lib={lib}]").first
            card.wait_for()
            self.assertEqual(card.locator(".s .dot").evaluate("el => getComputedStyle(el).backgroundColor"), colour, lib)
            self.assertNotRegex(card.locator(".s").inner_text(), r"^(Film|Série|Album|Livre)\b", lib)               # le point dit la bibliothèque : pas de mot en plus

    def test_the_mix_keeps_the_newest_first_and_stops_at_the_number_asked(self):
        self.open()
        out = self.page.evaluate("""() => {
          var mk = (id, added) => ({ id: id, added: added });
          var libs = [{ id: 'a', recent: [mk('a1', 50), mk('a2', 10)] }, { id: 'b', recent: [mk('b1', 60), mk('b2', 50)] }];
          var many = { id: 'c', recent: Array.from({ length: 24 }, (_, i) => mk('c' + i, 1000 - i)) };
          return { few: Hub.latest(libs, 24).map(x => x.item.id + '@' + x.lib.id), many: Hub.latest([many], 20).length, firstMany: Hub.latest([many], 20)[0].item.id };
        }""")
        self.assertEqual(out["few"], ["b1@b", "a1@a", "b2@b", "a2@a"])           # à date égale, l'ordre des bibliothèques puis celui de leur liste
        self.assertEqual((out["many"], out["firstMany"]), (20, "c0"))

    def test_a_row_that_scrolls_is_cut_at_the_margin_on_the_right_as_on_the_left(self):
        for lib, title in (("films", "Inception"), ("series", "Dark"), ("livres", "Naruto")):
            entry = self.entries(lib, title)[0]
            self.api("/api/progress", {"entry": entry["id"], "position": 2 if lib == "livres" else 100, "duration": 4 if lib == "livres" else 3000, "done": False})
            self.addCleanup(lambda lib=lib, title=title: self.api("/api/item/" + self.item(lib, title)["id"] + "/seen", {"done": False}))
        self.page.set_viewport_size({"width": 560, "height": 800})
        self.open()
        row = self.view(".shelf .row").first
        row.wait_for()
        self.assertGreaterEqual(row.locator(".card").count(), 3)
        edges = row.evaluate("el => { var r = el.getBoundingClientRect(); return [el.querySelector('.card').getBoundingClientRect().left, innerWidth - r.right]; }")
        self.assertGreater(edges[0], 8)                                       # la première carte part de la marge
        self.assertAlmostEqual(edges[0], edges[1], delta=1)                   # la rangée est coupée à la même marge à droite : la carte qui dépasse n'est pas coupée par le bord de l'écran
        row.evaluate("el => { el.style.scrollBehavior = 'auto'; el.scrollLeft = el.scrollWidth; }")
        self.page.wait_for_timeout(200)
        last = row.locator(".card").last.evaluate("el => el.getBoundingClientRect().right")
        self.assertLessEqual(last, self.page.evaluate("innerWidth") - edges[1] + 1)      # au bout, la dernière carte reste dans la marge


class TestHomeUnseen(UiCase):
    """« Pas encore vu » à l'accueil, après « Nouveautés » : des films et des séries que le compte n'a pas touchés ; chaque rayon se masque dans les réglages du compte."""

    def setUp(self):
        super().setUp()
        db = self.run_.hub.db
        for lib in ("films", "series"):                                                          # de quoi remplir un rayon dans chacune, au-delà des nouveautés
            items = [dict(r) for r in db.query("SELECT * FROM items WHERE lib=?", (lib,))]
            entries = [dict(r) for r in db.query("SELECT * FROM entries WHERE lib=?", (lib,))]
            old = [{**items[0], "id": f"vieux{lib}{n:03d}", "title": f"Vieux {lib} {n:03d}", "sort": f"vieux {lib} {n:03d}", "grp": "", "added": 100 + n, "meta": "{}"} for n in range(100)]
            files = [{**entries[0], "id": f"vieuxf{lib}{n:03d}", "item_id": f"vieux{lib}{n:03d}", "rel": f"vieux{lib}{n:03d}/film.mkv"} for n in range(100)]
            db.replace_library(lib, items + old, entries + files)
            self.addCleanup(db.replace_library, lib, items, entries)
        day = mock.patch("hub.server.app.today", return_value="2026-11-04")          # le serveur tourne dans ce processus : l'ordre du jour ne change pas pendant le test
        day.start()
        self.addCleanup(day.stop)
        self.addCleanup(lambda: self.api("/api/me/settings", {"home_latest": "show", "home_unseen": "show", "home_unseen_count": "12", "home_unseen_sort": "random"}))

    def libraries(self):
        return self.api("/api/home")["libraries"]

    def offered(self, lib="films"):
        return [u["id"] for l in self.libraries() if l["id"] == lib for u in l["unseen"]]

    def test_each_video_library_has_its_own_shelf_after_the_new_ones(self):
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        wanted = [l["label"] for l in self.libraries() if "unseen" in l and l["unseen"]]
        self.assertGreater(len(wanted), 1)                                                                  # films, séries : pas un seul rayon pour tout
        self.assertEqual(self.view(".shelf h2").all_inner_texts(), ["Nouveautés"] + ["Pas encore vu · " + label for label in wanted])
        self.assertEqual(self.view(".shelf.unseen").evaluate_all("els => els.map(e => e.getAttribute('data-lib'))"), [l["id"] for l in self.libraries() if l.get("unseen")])
        self.assertEqual(self.view(".shelf.unseen[data-lib=films] h2 a").get_attribute("href"), "#/l/films")      # le titre mène à la bibliothèque

    def test_the_shelf_never_repeats_a_film_the_new_ones_already_show(self):
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        latest = self.view(".shelf.latest .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        shown = self.view(".shelf.unseen[data-lib=films] .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        self.assertEqual(shown, [i for i in self.offered() if i not in latest][:12])
        self.assertEqual(len(shown), 12)
        self.assertFalse(set(shown) & set(latest))

    def test_a_film_that_is_started_leaves_the_shelf(self):
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        first = self.view(".shelf.unseen .card").first.get_attribute("data-key")
        entry = self.run_.hub.db.one("SELECT id FROM entries WHERE item_id=?", (first,))["id"]
        self.api("/api/progress", {"entry": entry, "position": 5, "duration": 100})
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        self.assertNotIn(first, self.view(".shelf.unseen .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))"))

    def test_each_shelf_is_hidden_on_its_own_by_the_settings_of_the_account(self):
        self.api("/api/me/settings", {"home_unseen": "hide"})
        self.open()
        self.view(".shelf.latest .card").first.wait_for()
        self.assertEqual(self.view(".shelf h2").all_inner_texts(), ["Nouveautés"])
        self.api("/api/me/settings", {"home_unseen": "show", "home_latest": "hide"})
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        self.assertNotIn("Nouveautés", self.view(".shelf h2").all_inner_texts())
        shown = self.view(".shelf.unseen[data-lib=films] .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        self.assertEqual(shown, self.offered()[:12])                                                       # sans nouveautés, rien à ne pas répéter
        self.api("/api/me/settings", {"home_unseen": "hide"})
        self.open()
        self.view(".greeting").wait_for()
        self.until(lambda: self.view(".shelf").count() == 0)

    def test_the_account_settings_have_a_choice_for_each_shelf(self):
        self.open("#/settings?tab=preferences")
        panel = self.view(".panel", has_text="Affichage")
        for title, key, name in (("Nouveautés", "home_latest", "Nouveautés"), ("Pas encore vu", "home_unseen", "Pas encore vu")):
            line = panel.locator(".line").filter(has=self.page.locator("b", has_text=re.compile(f"^{title}$")))
            self.assertIn("Affiché", line.inner_text())
            line.locator("button", has_text="Changer").click()
            self.page.locator(".menu .opt .lbl", has_text="Masqué").click()
            self.until(lambda key=key: self.api("/api/me/settings")["settings"][key] == "hide")
            self.until(lambda line=line: "Masqué" in line.inner_text())

    def shown(self, lib="films"):
        return self.view(f".shelf.unseen[data-lib={lib}] .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")

    def test_the_account_chooses_how_many_cards_each_shelf_shows(self):
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        latest = self.view(".shelf.latest .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        wanted = [i for i in self.offered() if i not in latest]
        self.assertEqual(self.shown(), wanted[:12])                                                        # sans réglage, 12 comme avant
        for count in (6, 24):
            self.api("/api/me/settings", {"home_unseen_count": str(count)})
            self.open()
            self.view(".shelf.unseen .card").first.wait_for()
            self.assertEqual(self.shown(), wanted[:count])
            self.assertGreater(len(self.shown("series")), count - 3)                                       # le même nombre dans chaque rayon, moins ce que les nouveautés montrent déjà

    def test_the_number_of_cards_is_chosen_in_the_settings_of_the_account(self):
        self.open("#/settings?tab=preferences")
        line = self.view(".panel", has_text="Affichage").locator(".line").filter(has=self.page.locator("b", has_text="Pas encore vu : cartes par rayon"))
        self.assertIn("12 cartes", line.inner_text())
        line.locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="6 cartes").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["home_unseen_count"] == "6")
        self.until(lambda: "6 cartes" in line.inner_text())

    def test_the_order_of_the_shelf_is_chosen_in_the_settings_of_the_account_and_the_home_follows(self):
        self.open("#/settings?tab=preferences")
        line = self.view(".panel", has_text="Affichage").locator(".line").filter(has=self.page.locator("b", has_text="Pas encore vu : trier par"))
        self.assertIn("Au hasard", line.inner_text())
        line.locator("button", has_text="Changer").click()
        self.assertEqual(self.page.locator(".menu .opt .lbl").all_inner_texts(), ["Au hasard", "Note TMDB", "Date d’ajout"])
        self.page.locator(".menu .opt .lbl", has_text="Date d’ajout").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["home_unseen_sort"] == "added")
        self.until(lambda: "Date d’ajout" in line.inner_text())
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        latest = self.view(".shelf.latest .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        self.assertEqual(self.shown(), [i for i in self.offered() if i not in latest][:12])                # l'ordre vient du serveur
        added = {r["id"]: r["added"] for r in self.run_.hub.db.query("SELECT id, added FROM items WHERE lib='films'")}
        offered = [added[i] for i in self.offered()]
        self.assertEqual(offered, sorted(offered, reverse=True))                                          # du plus récemment ajouté au plus ancien

    def add_newest_films(self, count=60, rated=False):
        """`count` films jamais vus, tous plus récents que le reste (des dates d'ajout distinctes) ; `rated` : les plus récents sont aussi les mieux notés. « Nouveautés » en prend 24 : le rayon ne doit pas s'en trouver vide. Remis en l'état par le nettoyage de `setUp` (celui qui restaure les films d'origine)."""
        db = self.run_.hub.db
        items = [dict(r) for r in db.query("SELECT * FROM items WHERE lib='films'")]
        entries = [dict(r) for r in db.query("SELECT * FROM entries WHERE lib='films'")]
        top = db.one("SELECT MAX(added) AS top FROM items")["top"]
        meta = lambda n: json.dumps({"tmdb_rating": {"vote": 5 + n / 10, "votes": 1000}} if rated else {})
        new = [{**items[0], "id": f"neuf{n:03d}", "title": f"Neuf {n:03d}", "sort": f"neuf {n:03d}", "grp": "", "added": top + 1000 + n, "meta": meta(n)} for n in range(count)]
        files = [{**entries[0], "id": f"neuff{n:03d}", "item_id": f"neuf{n:03d}", "rel": f"neuf{n:03d}/film.mkv"} for n in range(count)]
        db.replace_library("films", items + new, entries + files)

    def assert_the_shelf_keeps_twelve_cards_that_the_new_ones_do_not_show(self):
        self.open()
        self.view(".shelf.unseen[data-lib=films] .card").first.wait_for()
        latest = self.view(".shelf.latest .card").evaluate_all("els => els.map(e => e.getAttribute('data-key'))")
        self.assertEqual(sum(key.startswith("neuf") for key in latest), 24)                                 # les nouveautés ne sont faites que de ces films
        shown = self.shown()
        self.assertGreaterEqual(len(shown), 12)
        self.assertFalse(set(shown) & set(latest))
        self.assertTrue(all(key.startswith("neuf") for key in shown), shown)                                  # le tri a bien pris les plus récents / les mieux notés, pas le reste

    def test_sorted_by_date_added_the_shelf_is_not_emptied_by_the_new_ones(self):
        self.add_newest_films()
        self.api("/api/me/settings", {"home_unseen_sort": "added"})
        self.assert_the_shelf_keeps_twelve_cards_that_the_new_ones_do_not_show()

    def test_sorted_by_rating_the_shelf_is_not_emptied_when_the_best_rated_are_the_newest(self):
        self.add_newest_films(rated=True)
        self.api("/api/me/settings", {"home_unseen_sort": "rating"})
        self.assert_the_shelf_keeps_twelve_cards_that_the_new_ones_do_not_show()


class TestHomeWithNoShelf(UiCase):
    """L'accueil quand aucun rayon n'est affiché (réglages masqués, ou rien à proposer) : « Bonjour », une phrase, puis les bibliothèques en tuiles."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: self.api("/api/me/settings", {"home_latest": "show", "home_unseen": "show"}))

    def hide(self, *names):
        self.api("/api/me/settings", {name: "hide" for name in names})

    def tiles(self):
        return self.view(".libtiles a.libtile")

    def libraries(self):
        return self.api("/api/home")["libraries"]

    def test_the_libraries_appear_as_tiles_under_the_greeting_when_every_shelf_is_hidden(self):
        self.hide("home_latest", "home_unseen")
        self.open()
        self.tiles().first.wait_for()
        self.assertEqual(self.view(".shelf").count(), 0)
        libs = self.libraries()
        self.assertEqual(self.tiles().count(), len(libs))
        self.assertEqual(self.tiles().evaluate_all("els => els.map(e => e.getAttribute('href'))"), ["#/l/" + l["id"] for l in libs])
        self.assertEqual(self.view("h1.greeting").inner_text(), "Bonjour")                                          # serveur ouvert : pas de nom
        self.assertEqual(self.view(".libtiles-note").inner_text(), "Tu as masqué tous les rayons de l’accueil : Paramètres > Préférences, pour les remettre")
        for tile, lib in zip(self.tiles().all(), libs):
            self.assertEqual(tile.locator("b").inner_text(), lib["label"])
            self.assertEqual(tile.locator(".what > span").inner_text(), f"{lib['count']} élément" + ("s" if lib["count"] > 1 else ""))
        order = self.page.evaluate("(() => { var t = document.querySelector('.view:not([style*=\"none\"]) .libtiles'); var h = document.querySelector('.view:not([style*=\"none\"]) h1.greeting'); var n = document.querySelector('.view:not([style*=\"none\"]) .libtiles-note'); return [h.compareDocumentPosition(n), n.compareDocumentPosition(t)]; })()")
        self.assertEqual(order, [4, 4])                                                                              # « Bonjour », la phrase, puis les tuiles

    def test_a_tile_has_the_cover_of_the_latest_addition_or_the_colour_of_its_library(self):
        self.hide("home_latest", "home_unseen")

        def without_cover(route):
            data = route.fetch().json()
            for lib in data["libraries"]:
                if lib["id"] == "livres":
                    lib["recent"] = []
            route.fulfill(json=data)
        self.page.route("**/api/home", without_cover)
        pixel = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        self.page.route("**/img/item/**", lambda route: route.fulfill(body=pixel, content_type="image/png"))          # les médias d'essai n'ont pas de pochette : une image d'un point
        self.open()
        self.tiles().first.wait_for()
        films, books = self.view(".libtiles a[data-lib=films]"), self.view(".libtiles a[data-lib=livres]")
        self.assertEqual(films.locator("img").count(), 1)
        self.assertTrue(films.locator("img").get_attribute("src").startswith("/img/item/"))
        self.assertEqual(books.locator("img").count(), 0)                                                            # rien à montrer : la couleur de la bibliothèque
        self.assertTrue(books.locator(".thumb").evaluate("e => getComputedStyle(e).backgroundColor !== 'rgba(0, 0, 0, 0)'"))
        colours = self.tiles().evaluate_all("els => els.map(e => getComputedStyle(e.querySelector('.dot')).backgroundColor)")
        self.assertEqual(len(set(colours)), len(colours))                                                            # chaque bibliothèque a sa couleur

    def test_with_one_shelf_still_shown_the_home_keeps_its_shelves_and_has_no_tiles(self):
        self.hide("home_unseen")
        self.open()
        self.view(".shelf.latest .card").first.wait_for()
        self.assertEqual(self.view(".libtiles").count(), 0)
        self.assertEqual(self.view(".libtiles-note").count(), 0)

    def test_a_home_with_nothing_to_resume_or_discover_says_so(self):
        self.hide("home_latest")                                                                                     # « Pas encore vu » est affiché, mais tout est vu
        seen = []
        for lib in ("films", "series"):
            for item in self.api(f"/api/library/{lib}")["items"]:
                self.api(f"/api/item/{item['id']}/seen", {"done": True})
                seen.append(item["id"])
        self.addCleanup(lambda: [self.api(f"/api/item/{i}/seen", {"done": False}) for i in seen])
        self.open()
        self.tiles().first.wait_for()
        self.assertEqual(self.view(".libtiles-note").inner_text(), "Rien à reprendre ni à découvrir pour l’instant")
        self.assertEqual(self.view(".shelf").count(), 0)

    def test_the_tiles_are_real_links_the_remote_reaches_and_the_first_has_the_focus(self):
        self.hide("home_latest", "home_unseen")
        self.open()
        self.tiles().first.wait_for()
        self.assertTrue(self.tiles().evaluate_all("els => els.every(e => e.tagName === 'A' && e.classList.contains('f') && e.getAttribute('href'))"))
        self.assertEqual(self.tiles().first.get_attribute("data-autofocus"), "1")
        self.until(lambda: self.page.evaluate("document.activeElement === document.querySelector('.libtiles a.libtile')"))      # le focus part sur la première tuile
        first, seen = self.focused(), []
        for _ in range(len(self.libraries()) + 1):
            self.press("ArrowRight")
            seen.append(self.page.evaluate("document.activeElement.getAttribute('data-lib')"))
        self.assertGreaterEqual(len({s for s in seen if s}), 3)                                                       # les flèches parcourent la grille
        self.tiles().first.focus()
        self.press("Enter", wait=500)
        self.assertIn("#/l/", self.page.url)

    def test_on_a_phone_the_tiles_are_one_or_two_columns_without_sideways_scroll(self):
        self.hide("home_latest", "home_unseen")
        self.page.set_viewport_size({"width": 390, "height": 800})
        self.open()
        self.tiles().first.wait_for()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        lefts = self.tiles().evaluate_all("els => els.map(e => Math.round(e.getBoundingClientRect().left))")
        self.assertLessEqual(len(set(lefts)), 2)
        self.assertTrue(self.tiles().evaluate_all("els => els.every(e => e.getBoundingClientRect().right <= innerWidth && e.getBoundingClientRect().height >= 44)"))       # dans l'écran, assez grandes au doigt

    def test_the_text_and_the_tiles_are_in_english(self):
        self.hide("home_latest", "home_unseen")
        self.context.add_cookies([{"name": "hub_lang", "value": "en", "url": self.base}])
        self.page.add_init_script("localStorage.setItem('hub.lang', JSON.stringify('en'))")
        self.open()
        self.tiles().first.wait_for()
        self.assertEqual(self.view(".libtiles-note").inner_text(), "You have hidden every shelf on the home page: Settings > Preferences, to bring them back")
        self.assertIn("Movies", self.tiles().locator("b").all_inner_texts())
        self.assertIn(" items", self.tiles().first.locator(".what > span").inner_text())
        self.assertEqual(self.page.evaluate("Hub.missing || []"), [])


class TestHomeNewArtist(UiCase):
    def test_the_arrival_of_an_artist_is_one_card_that_opens_his_page(self):
        db = self.run_.hub.db
        items = [dict(r) for r in db.query("SELECT * FROM items WHERE lib='musique'")]
        entries = [dict(r) for r in db.query("SELECT * FROM entries WHERE lib='musique'")]
        now = max([i["added"] for i in items] + [0]) + 1000
        new = [{**items[0], "id": f"nv{n}", "title": f"Album {n}", "sort": f"album {n}", "grp": "Nouvel Artiste", "added": now - n,
                "meta": json.dumps({"artist_id": "nouvelartiste"})} for n in range(3)]
        db.replace_library("musique", items + new, entries)
        self.addCleanup(db.replace_library, "musique", items, entries)
        self.open()
        card = self.view(".shelf.latest .card[data-type=artist]")
        card.first.wait_for()
        self.assertEqual(card.count(), 1)
        self.assertEqual(card.first.get_attribute("href"), "#/artist/musique/nouvelartiste")
        self.assertEqual(card.first.locator(".t").inner_text(), "Nouvel Artiste")
        self.assertIn("3 albums", card.first.locator(".s").inner_text())
        self.assertEqual(self.view(".shelf.latest .card[data-key^=nv]").count(), 0)             # ses trois albums ne sont plus là un par un
        self.assertEqual(card.first.get_attribute("data-lib"), "musique")


class TestHomeGreeting(unittest.TestCase):
    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_the_greeting_uses_the_name_of_the_account(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context()
                base = f"http://127.0.0.1:{run.port}"
                form = {"Content-Type": "application/x-www-form-urlencoded"}
                context.request.post(base + "/api/login", data="password=secret", headers=form)
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse12", "role": "user"}), headers={"Content-Type": "application/json"})
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse12", headers=form)
                page = context.new_page()
                page.goto(base + "/")
                page.wait_for_selector(".greeting")
                self.assertEqual(page.locator(".greeting").inner_text(), "Bonjour lea")
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSignOutOthers(unittest.TestCase):
    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_the_account_panel_signs_out_the_other_devices_and_keeps_this_one(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                base = f"http://127.0.0.1:{run.port}"
                form = {"Content-Type": "application/x-www-form-urlencoded"}
                here, elsewhere = browser.new_context(), browser.new_context()
                for context in (here, elsewhere):
                    context.request.post(base + "/api/login", data="password=secret", headers=form)
                    self.assertEqual(context.request.get(base + "/api/state").status, 200)
                page = here.new_page()
                page.goto(base + "/#/settings")
                page.locator(".tabs .tab", has_text="Comptes").click()
                page.locator("[data-key=signout-others]").click()
                dialog = page.locator("[role=dialog]")
                dialog.locator("input[type=password]").fill("faux")
                dialog.get_by_role("button", name="Déconnecter").click()
                page.wait_for_function("document.querySelector('.formerr').textContent.length > 0")
                self.assertIn("incorrect", dialog.locator(".formerr").inner_text())
                self.assertEqual(elsewhere.request.get(base + "/api/state").status, 200)             # un mauvais mot de passe ne coupe rien
                dialog.locator("input[type=password]").fill("secret")
                dialog.get_by_role("button", name="Déconnecter").click()
                page.wait_for_function("document.getElementById('toast').textContent.includes('autres appareils')")
                self.assertEqual(elsewhere.request.get(base + "/api/state").status, 401)
                self.assertEqual(here.request.get(base + "/api/state").status, 200)                   # cet appareil reste connecté
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSettings(UiCase):
    """La page « Paramètres » : des onglets en haut, qui restent affichés, au lieu d'une seule longue page de gestion."""

    def tabs(self):
        return self.view(".tabs .tab").all_inner_texts()

    def tab(self, name):
        self.view(".tabs .tab", has_text=name).click()

    def titles(self):
        return self.view(".panel h2").all_inner_texts()

    def test_the_films_library_can_reread_its_covers(self):
        film = self.item("films", "Inception")
        urllib.request.urlopen(self.base + "/img/item/" + film["id"] + "?w=160").read()          # la vignette est dans le cache
        cache = self.run_.hub.images.cache
        before = len(list(cache.glob("*.jpg")))
        self.assertGreater(before, 0)
        self.open("#/settings")
        self.view("[data-key=libs-toggle]").click()                                                  # les bibliothèques sont repliées tant qu'on ne les déplie pas
        self.view("[data-key=covers-films]").click()
        self.page.locator("#toast.on", has_text="Covers à relire").wait_for()
        self.assertLess(len(list(cache.glob("*.jpg"))), before)
        self.assertEqual(self.view("[data-key^=covers-]").count(), 1)                              # seule la bibliothèque de films a ce bouton
        urllib.request.urlopen(self.base + "/img/item/" + film["id"] + "?w=160").read()          # et elle se refait à la demande

    def test_the_page_is_called_settings_and_opens_on_the_management_tab(self):
        self.open("#/settings")
        self.view(".panel").first.wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Paramètres")
        self.assertEqual(self.page.locator("#nav .tool[data-nav='#/settings']").get_attribute("aria-label"), "Paramètres")
        self.assertEqual(self.tabs(), ["Gestion", "Boîte de réception", "Préférences", "Comptes"])
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Gestion")
        self.assertEqual(self.titles(), ["Bibliothèques", "Sagas", "Entretien", "Tâches", "Serveur"])

    def test_the_server_panel_credits_tmdb_with_its_logo(self):
        """Les conditions de l'API TMDB demandent la mention et le logo."""
        self.open("#/settings")
        credit = self.view(".credit-tmdb")
        credit.wait_for()
        self.assertIn("n’est ni approuvé ni certifié par TMDB", credit.inner_text())
        logo = credit.locator("img")
        self.assertEqual(logo.get_attribute("alt"), "TMDB")
        self.until(lambda: logo.evaluate("img => img.complete && img.naturalWidth > 0"))          # le fichier est bien servi

    def test_the_old_address_still_leads_to_the_page(self):
        self.open("#/admin")
        self.view(".tabs").wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Paramètres")

    def test_each_tab_has_its_own_panels(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        for name, titles in (("Préférences", ["Interface", "Lecture", "Affichage"]), ("Comptes", ["Mon compte"]), ("Gestion", ["Bibliothèques", "Sagas", "Entretien", "Tâches", "Serveur"])):
            self.tab(name)
            self.until(lambda: self.view(".tabs .tab[aria-selected=true]").inner_text() == name)
            self.assertEqual(self.titles(), titles, name)
        self.assertIn("Version", self.view("dl").inner_text())

    def test_the_libraries_are_folded_until_unfolded(self):
        """Tout mettre à jour reste à portée ; le détail des bibliothèques se déplie à la demande, et reste déplié tant qu'on est sur la page."""
        self.open("#/settings")
        self.view(".panel").first.wait_for()
        toggle = self.view("[data-key=libs-toggle]")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertEqual(self.view("[data-lib]").count(), 0)
        self.assertEqual(self.view("[data-key^=scan-]").count(), 1)                                  # « Tout mettre à jour » seul
        total = len(self.api("/api/state")["libraries"])
        self.assertIn(str(total), toggle.inner_text())
        toggle.click()
        self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        self.assertEqual(self.view("[data-lib]").count(), total)
        self.assertEqual(self.page.evaluate("document.activeElement.getAttribute('data-key')"), "libs-toggle")
        self.view("[data-key=scan-all]").click()                                                     # la page se reconstruit pendant la mise à jour : le détail reste déplié
        self.page.wait_for_timeout(2500)
        self.assertEqual(self.view("[data-lib]").count(), total)
        self.view("[data-key=libs-toggle]").click()
        self.assertEqual(self.view("[data-lib]").count(), 0)

    def test_the_former_tabs_lead_to_their_new_place(self):
        for old, new in (("lecture", "Préférences"), ("interface", "Préférences"), ("taches", "Gestion"), ("serveur", "Gestion")):
            self.open("#/settings?tab=" + old)
            self.until(lambda: self.view(".tabs .tab[aria-selected=true]").inner_text() == new)

    def test_the_preferences_are_one_tab_in_three_panels(self):
        """Interface : l'appareil, la taille du texte et la langue. Lecture : la vidéo et le son. Affichage : les rayons de l'accueil et l'ouverture des bibliothèques."""
        self.open("#/settings?tab=preferences")
        self.view(".panel").first.wait_for()
        panels = self.view(".panel")
        self.assertEqual(panels.count(), 3)
        self.assertEqual(panels.nth(0).locator(".line .what b").all_inner_texts(), ["Appareil", "Taille du texte", "Langue"])
        self.assertEqual(panels.nth(1).locator(".line .what b").all_inner_texts(), ["Qualité", "Son surround 5.1"])
        self.assertEqual(panels.nth(2).locator(".line .what b").all_inner_texts(), ["Bibliothèque de musique", "Bibliothèque de films", "Nouveautés", "Pas encore vu", "Pas encore vu : cartes par rayon", "Pas encore vu : trier par"])

    def test_the_tabs_stay_at_the_top_when_the_page_scrolls(self):
        self.page.set_viewport_size({"width": 1280, "height": 300})
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        self.page.wait_for_timeout(200)
        self.assertGreater(self.page.evaluate("window.scrollY"), 50)
        top = self.view(".tabs").evaluate("el => el.getBoundingClientRect().top")
        self.assertTrue(0 <= top < 120, top)

    def test_a_button_of_the_management_tab_does_not_scroll_the_page_back_to_the_top(self):
        self.page.set_viewport_size({"width": 1280, "height": 300})
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.view("[data-key=libs-toggle]").click()
        button = self.view("[data-key^='scan-']:not([data-key='scan-all'])").last
        button.click()                                  # Playwright amène le bouton à l'écran avant de cliquer : on mesure après
        before = self.page.evaluate("window.scrollY")
        self.assertGreater(before, 50)
        self.page.locator("#toast.on", has_text="Mise à jour lancée").wait_for()
        self.page.wait_for_timeout(2500)                # la page est reconstruite quand la mise à jour change l'état des bibliothèques
        self.assertEqual(self.page.evaluate("window.scrollY"), before)

    def test_the_account_chooses_how_the_music_library_opens(self):
        self.addCleanup(lambda: self.api("/api/me/settings", {"music_view": "albums"}))
        self.open("#/settings?tab=preferences")
        panel = self.view(".panel", has_text="Affichage")
        panel.wait_for()
        self.assertIn("Albums", panel.inner_text())
        panel.locator(".line", has_text="musique").locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Artistes").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["music_view"] == "artists")
        self.until(lambda: "Artistes" in self.view(".panel", has_text="Affichage").inner_text())
        panel = self.view(".panel", has_text="Affichage")
        panel.locator(".line", has_text="musique").locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Albums").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["music_view"] == "albums")

    def test_the_tab_chosen_is_remembered(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.tab("Préférences")
        self.page.reload()
        self.view(".tabs").wait_for()
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Préférences")
        self.open("#/settings?tab=comptes")
        self.view(".tabs").wait_for()
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Comptes")

    def test_the_tabs_are_reached_with_the_remote(self):
        self.open("#/settings?tab=gestion")
        self.view(".tabs .tab").first.wait_for()
        self.view(".tabs .tab", has_text="Gestion").focus()
        self.press("ArrowRight", "ArrowRight", "Enter", wait=300)          # Gestion, Boîte de réception, Préférences
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Préférences")

    def test_the_tasks_unfold_one_by_one_or_all_at_once(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.view(".job").first.wait_for()
        jobs = self.view(".job .jhead")
        total = jobs.count()
        self.assertGreaterEqual(total, 2)
        self.assertEqual(self.view(".job .jhead[aria-expanded=true]").count(), 0)
        jobs.nth(1).click()
        self.assertEqual(self.view(".job .jhead[aria-expanded=true]").count(), 1)
        self.view("button", has_text="Tout déplier").click()
        self.assertEqual(self.view(".job .jhead[aria-expanded=true]").count(), total)
        self.assertEqual(self.view("button", has_text="Tout replier").count(), 1)
        self.view("button", has_text="Tout replier").click()
        self.assertEqual(self.view(".job .jhead[aria-expanded=true]").count(), 0)

    def test_a_running_task_log_keeps_its_scroll_and_the_focus_while_it_grows(self):
        import threading
        grow, bottom = threading.Event(), threading.Event()

        def work(log):
            for n in range(100):
                log(f"ligne {n}")
            grow.wait(20)
            for n in range(100, 140):
                log(f"ligne {n}")
            bottom.wait(20)
            log("ligne finale")
        self.addCleanup(lambda: (grow.set(), bottom.set(), self.run_.wait_jobs()))
        self.run_.hub.jobs._submit("Essai défilement", work)
        self.open("#/settings?tab=gestion")
        job = self.view(".job", has_text="Essai défilement")
        job.locator(".jhead").click()
        pre = job.locator("pre")
        pre.wait_for()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne 99') >= 0")
        self.assertGreater(pre.evaluate("el => el.scrollHeight - el.clientHeight"), 100)
        head = job.locator(".jhead")
        head.focus()
        head.evaluate("el => { window.__head = el; }")
        pre.evaluate("el => { el.scrollTop = 80; }")
        grow.set()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne 139') >= 0", timeout=10000)
        self.assertEqual(pre.evaluate("el => el.scrollTop"), 80)                     # le journal n'est pas revenu en haut
        self.assertTrue(self.page.evaluate("document.activeElement === window.__head"))   # le focus est resté sur l'en-tête (rien n'a été reconstruit)
        pre.evaluate("el => { el.scrollTop = el.scrollHeight; }")                    # lu jusqu'en bas : on suit la fin du journal
        bottom.set()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne finale') >= 0", timeout=10000)
        self.page.wait_for_function("(() => { var p = document.querySelector('.job pre'); return p.scrollHeight - p.scrollTop - p.clientHeight < 4; })()")

    def test_the_text_selected_in_a_running_log_survives_its_updates(self):
        import threading
        grow, finish = threading.Event(), threading.Event()

        def work(log):
            for n in range(100):
                log(f"ligne {n}")
            grow.wait(20)
            for n in range(100, 140):
                log(f"ligne {n}")
            finish.wait(20)          # la tâche reste en cours : sa fin referait toute la page
        self.addCleanup(lambda: (grow.set(), finish.set(), self.run_.wait_jobs()))
        self.run_.hub.jobs._submit("Essai sélection", work)
        self.open("#/settings?tab=gestion")
        job = self.view(".job", has_text="Essai sélection")
        job.locator(".jhead").click()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne 99') >= 0")
        job.locator("pre").evaluate("el => { var r = document.createRange(); r.setStart(el.firstChild, 0); r.setEnd(el.firstChild, 7); var s = getSelection(); s.removeAllRanges(); s.addRange(r); }")
        self.assertEqual(self.page.evaluate("getSelection().toString()"), "ligne 0")
        grow.set()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne 139') >= 0", timeout=10000)
        self.assertEqual(self.page.evaluate("getSelection().toString()"), "ligne 0")          # copier un extrait d'une tâche en cours n'est pas interrompu par la mise à jour

    def test_a_task_view_that_is_gone_stops_polling_even_when_a_reply_is_late(self):
        import threading
        release = threading.Event()
        self.addCleanup(lambda: (release.set(), self.run_.wait_jobs()))
        self.run_.hub.jobs._submit("Essai minuteur", lambda log: release.wait(30))
        self.open("#/")
        self.page.evaluate("location.hash = '#/settings?tab=gestion'")
        self.shown("#/settings?tab=gestion")
        self.view(".job", has_text="Essai minuteur").wait_for()
        self.page.evaluate("""() => {          // les réponses de /api/jobs arrivent une seconde plus tard ; on compte les appels
          var fetchNow = window.fetch; window.jobsCalls = 0;
          window.fetch = function (url) {
            if (String(url).indexOf('/api/jobs') < 0) return fetchNow.apply(this, arguments);
            var self = this, args = arguments; window.jobsCalls += 1;
            return new Promise(function (ok) { setTimeout(ok, 1000); }).then(function () { return fetchNow.apply(self, args); });
          };
        }""")
        self.page.evaluate("() => { Hub.current().view.refresh(); history.back(); }")          # une interrogation part, puis la page est quittée (la vue est détruite) avant sa réponse
        self.shown("#/")
        self.page.wait_for_timeout(3500)          # la réponse arrive, puis un tour de 1,5 s s'écoulerait si le minuteur était réarmé
        self.assertEqual(self.page.evaluate("window.jobsCalls"), 1)

    def test_an_open_log_keeps_its_scroll_when_another_task_arrives_or_is_unfolded(self):
        import threading
        release = threading.Event()

        def work(log):
            for n in range(100):
                log(f"ligne {n}")
            release.wait(20)
        self.addCleanup(lambda: (release.set(), self.run_.wait_jobs()))
        self.run_.hub.jobs._submit("Essai long", work)
        self.open("#/settings?tab=gestion")
        first = self.view(".job", has_text="Essai long")
        first.locator(".jhead").click()
        self.page.wait_for_function("document.querySelector('.job pre').textContent.indexOf('ligne 99') >= 0")
        first.locator("pre").evaluate("el => { el.scrollTop = 80; }")
        self.run_.hub.jobs._submit("Essai court", lambda log: log("fait"))          # attend derrière la première : la liste gagne une tâche, le panneau est refait
        second = self.view(".job", has_text="Essai court")
        second.wait_for(timeout=10000)
        self.assertEqual(self.view(".job", has_text="Essai long").locator("pre").evaluate("el => el.scrollTop"), 80)
        second.locator(".jhead").click()                                          # déplier une autre tâche refait le panneau aussi
        second.locator(".muted").wait_for()
        self.assertEqual(self.view(".job", has_text="Essai long").locator("pre").evaluate("el => el.scrollTop"), 80)

    def test_every_task_of_the_history_is_listed_not_only_the_last_six(self):
        self.open("#/settings?tab=gestion")
        self.view(".job").first.wait_for()
        listed = self.view(".job").count()
        self.assertEqual(listed, min(30, len(self.api("/api/jobs")["jobs"])))


class TestSettingsOfAnOrdinaryAccount(unittest.TestCase):
    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_only_the_tabs_for_everyone_are_shown(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context()
                base = f"http://127.0.0.1:{run.port}"
                form = {"Content-Type": "application/x-www-form-urlencoded"}
                context.request.post(base + "/api/login", data="password=secret", headers=form)
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse12", "role": "user"}), headers={"Content-Type": "application/json"})
                context.request.post(base + "/api/logout")
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse12", headers=form)
                page = context.new_page()
                page.goto(base + "/#/settings")
                page.wait_for_selector(".tabs .tab")
                self.assertEqual(page.locator(".tabs .tab").all_inner_texts(), ["Préférences", "Comptes"])
                page.locator(".tabs .tab", has_text="Comptes").click()
                page.wait_for_selector(".panel h2")
                self.assertEqual(page.locator(".panel h2").all_inner_texts(), ["Mon compte"])
                page.goto(base + "/#/settings?tab=serveur")                       # un onglet réservé à l'administrateur ne s'ouvre pas : l'ancien « Serveur » non plus
                shown = page.locator("#views > .view:visible .tabs .tab[aria-selected=true]")       # l'ancienne page reste, cachée, derrière la nouvelle
                shown.wait_for()
                self.assertEqual(shown.inner_text(), "Préférences")
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestArtistLinksOfAnOrdinaryAccount(unittest.TestCase):
    @unittest.skipUnless(sync_playwright and fixtures.FFMPEG, "Playwright ou ffmpeg absent")
    def test_the_links_are_read_only(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Running(tmp, password="secret")
            artist, _ = give_artist_a_sheet(run.hub.db)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context()
                base = f"http://127.0.0.1:{run.port}"
                form = {"Content-Type": "application/x-www-form-urlencoded"}
                context.request.post(base + "/api/login", data="password=secret", headers=form)
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse12", "role": "user"}), headers={"Content-Type": "application/json"})
                context.request.post(base + "/api/logout")
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse12", headers=form)
                page = context.new_page()
                page.goto(f"{base}/#/artist/musique/{artist}")
                page.wait_for_selector(".links-out a")
                self.assertEqual(page.locator(".links-out a").all_inner_texts(), ["Site officiel", "Wikipédia", "MusicBrainz"])
                self.assertEqual(page.locator(".links-out .linkx").count(), 0)
                self.assertEqual(page.locator(".links-out .btn", has_text="Ajouter un lien").count(), 0)
                refused = context.request.post(f"{base}/api/artist/musique/{artist}/links", data=json.dumps({"url": "https://example.org/"}), headers={"Content-Type": "application/json"})
                self.assertEqual(refused.status, 403)                      # le serveur refuse aussi : l'interface n'est pas la seule garde
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class InboxUiCase(unittest.TestCase):
    """La boîte de réception dans le navigateur : la page « Envoyer », le droit « Envoyer » d'un compte, l'onglet de l'administrateur.

    Un serveur avec comptes (`admin` / `secret`, `alice` a le droit d'envoyer, `bob` non) pour toute la classe ; la boîte est vidée entre deux tests.
    """
    PASSWORD = "motdepasse12"

    @classmethod
    def setUpClass(cls):
        if not sync_playwright:
            raise unittest.SkipTest("Playwright absent")
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp, password="secret")
        cls.hub = cls.run_.hub
        cls.base = f"http://127.0.0.1:{cls.run_.port}"
        from hub import accounts
        cls.alice = accounts.create(cls.hub.db, "alice", cls.PASSWORD)
        cls.bob = accounts.create(cls.hub.db, "bob", cls.PASSWORD)
        accounts.set_can_upload(cls.hub.db, cls.alice["id"], True)
        cls.admin_id = next(row["id"] for row in accounts.rows(cls.hub.db) if row["role"] == "admin")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.box = Path(cls.tmp) / "data" / "inbox"

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(self.box, ignore_errors=True)
        inbox = self.hub.inbox
        inbox.keep_free = 0                                  # le disque de la machine d'essai n'a pas à avoir 10 Go de libres
        self.errors = []
        self.pages = []

    def tearDown(self):
        for context in self.pages:
            context.close()
        self.hub.inbox.keep_free = 0
        shutil.rmtree(self.box, ignore_errors=True)
        self.assertEqual(self.errors, [], "erreurs JavaScript")

    # --- Aides ---------------------------------------------------------------
    def session(self, name, lang=None, device=None, width=1280):
        """Une page connectée en tant que `name` (l'administrateur se connecte avec `secret`)."""
        context = self.browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
        self.pages.append(context)
        if lang:
            context.add_cookies([{"name": "hub_lang", "value": lang, "url": self.base}])
            context.add_init_script(f"localStorage.setItem('hub.lang', JSON.stringify('{lang}'))")
        if device:
            context.add_init_script(f"localStorage.setItem('hub.device', JSON.stringify('{device}'))")
        page = context.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.goto(self.base + "/login")
        page.fill("input[name=user]", name)
        page.fill("input[type=password]", "secret" if name == "admin" else self.PASSWORD)
        page.keyboard.press("Enter")
        page.wait_for_selector("#nav .link", state="attached")
        return page

    def token(self, name):
        req = urllib.request.Request(self.base + "/api/token", data=json.dumps({"user": name, "password": "secret" if name == "admin" else self.PASSWORD}).encode(), headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))["token"]

    def users(self):
        req = urllib.request.Request(self.base + "/api/users", headers={"Authorization": "Bearer " + self.token("admin")})
        return {u["name"]: u for u in json.load(urllib.request.urlopen(req))["users"]}

    def put(self, name, filename, data=b"contenu", mtime=None, dir=None):
        """Un fichier dans la boîte de `name` (dans son sous-dossier `dir`, s'il y en a un), par l'API."""
        url = self.base + "/api/inbox?name=" + urllib.parse.quote(filename) + ("&dir=" + urllib.parse.quote(dir) if dir else "")
        req = urllib.request.Request(url, data=data, method="POST", headers={"Authorization": "Bearer " + self.token(name), "Content-Type": "application/octet-stream"})
        stored = json.load(urllib.request.urlopen(req))
        if mtime:
            os.utime(self.box / stored["account"] / stored["dir"] / stored["name"], (mtime, mtime))
        return stored

    def member(self, name):
        """Un compte qui a le droit d'envoyer, retiré à la fin du test (les autres tests comptent les comptes)."""
        from hub import accounts
        row = accounts.create(self.hub.db, name, self.PASSWORD)
        accounts.set_can_upload(self.hub.db, row["id"], True)
        self.addCleanup(lambda: accounts.by_id(self.hub.db, row["id"]) and accounts.delete(self.hub.db, row["id"]))
        return row

    def admin_inbox(self, **kw):
        """La liste de la boîte de réception, vue par l'administrateur."""
        page = self.session("admin", **kw)
        page.goto(self.base + "/#/settings?tab=inbox")
        page.locator("#views > .view:visible .inbox-file").first.wait_for()
        return page, page.locator("#views > .view:visible .inbox-file")

    def shown(self, page, hash):
        page.wait_for_function("h => { var c = window.Hub.current(); return !!c && c.hash === h && c.el.style.display !== 'none'; }", arg=hash)

    def upload_page(self, name="alice", **kw):
        page = self.session(name, **kw)
        page.goto(self.base + "/#/upload")
        self.shown(page, "#/upload")
        page.wait_for_selector("#views > .view:visible .used .usedline")             # la page a lu l'espace utilisé
        return page

    def send(self, page, *files):
        """Choisit des fichiers `(nom, octets)` comme le ferait la personne avec le bouton « Choisir des fichiers »."""
        page.set_input_files("#views > .view:visible input[type=file]:not([webkitdirectory])", [{"name": n, "mimeType": "application/octet-stream", "buffer": b} for n, b in files])

    def sends(self, page, state=""):
        return page.locator("#views > .view:visible .send" + (f'[data-state="{state}"]' if state else ""))

    def sending(self, page):
        """Attend qu'un envoi soit en cours avec une progression visible."""
        page.wait_for_function("(() => { var m = document.querySelector('#views > .view:not([style*=\"none\"]) .send[data-state=\"send\"] .meter'); return !!m && !m.hidden && +m.getAttribute('aria-valuenow') > 0; })()", timeout=15000)

    def throttle(self, page, bytes_per_second):
        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": bytes_per_second})

    def stored(self, folder_name="alice"):
        """Les fichiers du dossier `folder_name` de la boîte : le dossier porte le nom du compte."""
        folder = self.box / folder_name
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def wait_for(self, condition, tries=100):
        for _ in range(tries):
            if condition():
                return
            time.sleep(0.1)
        self.fail("la condition n'est jamais devenue vraie")

    # --- Le droit « Envoyer » ----------------------------------------------------
    def test_the_entry_and_the_page_exist_only_for_those_who_may_upload(self):
        for name, allowed in (("alice", True), ("admin", True), ("bob", False)):
            with self.subTest(name=name):
                page = self.session(name)
                self.assertEqual(page.locator('#nav [data-nav="#/upload"]').count(), 1 if allowed else 0)
                page.goto(self.base + "/#/upload")
                if allowed:
                    self.shown(page, "#/upload")
                    self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Envoyer")
                else:
                    page.locator("#toast", has_text="Tu n’as pas le droit d’envoyer des fichiers").wait_for()
                    self.shown(page, "#/")                                                 # l'adresse directe retombe sur l'accueil
                    self.assertEqual(page.locator("#views > .view:visible .upload").count(), 0)

    def rights_box(self, page, who):
        """La boîte « Droits de {nom} » du compte `who`, ouverte depuis Paramètres > Comptes."""
        page.goto(self.base + "/#/settings?tab=comptes")
        page.locator(f'[data-key="rights-{who["id"]}"]').click()
        page.locator(".menu.rightsbox").wait_for()
        return page.locator(".menu.rightsbox")

    def calls(self, page):
        """Les appels d'écriture de droits que la page envoie : « rights » et « upload »."""
        seen = []
        page.on("request", lambda r: seen.append(r.url.rsplit("/", 1)[1]) if r.method == "POST" and re.search(r"/api/user/\w+/(rights|upload)$", r.url) else None)
        return seen

    def reset_rights(self, row):
        """Les droits du compte `row` sont remis comme au départ à la fin du test (la classe partage ses comptes)."""
        from hub import accounts
        def undo():
            ids = [lib.id for lib in self.hub.cfg.libraries]
            accounts.set_can_upload(self.hub.db, row["id"], row["id"] == self.alice["id"])
            accounts.set_rights(self.hub.db, row["id"], set(ids), {i: {"view": True, "play": True, "download": True} for i in ids})
        self.addCleanup(undo)

    def test_the_administrator_gives_and_withdraws_the_right_in_the_rights_box(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        page.goto(self.base + "/#/settings?tab=comptes")
        line = page.locator(f'[data-user="{self.bob["id"]}"]')
        line.wait_for()
        self.assertEqual(page.locator(f'[data-key="upload-{self.bob["id"]}"]').count(), 0)               # plus de pastille à part dans la ligne du compte
        self.assertIn("Envoyer des fichiers : non", line.locator("[data-rights]").inner_text())
        box = self.rights_box(page, self.bob)
        self.assertEqual(box.locator(".rrow[data-upload-right] b").inner_text(), "Envoyer des fichiers")
        yes, no = box.locator('[data-key="r-upload-yes"]'), box.locator('[data-key="r-upload-no"]')
        self.assertEqual((yes.inner_text().strip("✓ \n"), no.inner_text().strip("✓ \n")), ("Oui", "Non"))
        self.assertEqual((yes.get_attribute("aria-pressed"), no.get_attribute("aria-pressed")), ("false", "true"))
        self.assertTrue(yes.evaluate("e => e.classList.contains('f')") and no.evaluate("e => e.classList.contains('f')"))       # atteignables à la télécommande
        yes.click()
        self.assertEqual((yes.get_attribute("aria-pressed"), no.get_attribute("aria-pressed")), ("true", "false"))
        self.assertFalse(self.users()["bob"]["can_upload"])                                              # rien n'est écrit avant « Enregistrer »
        box.locator(".btn", has_text="Enregistrer").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        line.locator("[data-rights]", has_text="Envoyer des fichiers : oui").wait_for()
        self.assertTrue(self.users()["bob"]["can_upload"])
        bob = self.session("bob")                                                                        # le droit vaut tout de suite
        self.assertEqual(bob.locator('#nav [data-nav="#/upload"]').count(), 1)
        box = self.rights_box(page, self.bob)
        self.assertEqual(box.locator('[data-key="r-upload-yes"]').get_attribute("aria-pressed"), "true")
        box.locator('[data-key="r-upload-no"]').click()
        box.locator(".btn", has_text="Enregistrer").click()
        line.locator("[data-rights]", has_text="Envoyer des fichiers : non").wait_for()
        self.assertFalse(self.users()["bob"]["can_upload"])

    def test_an_administrator_always_sends_and_has_no_box_for_it(self):
        page = self.session("admin")
        page.goto(self.base + "/#/settings?tab=comptes")
        line = page.locator(f'[data-user="{self.admin_id}"]')
        line.wait_for()
        self.assertIn("toujours", line.locator("[data-upload]").inner_text())
        self.assertEqual(line.locator('[data-key^="upload-"], [data-key^="rights-"]').count(), 0)       # ni interrupteur, ni boîte pour cela

    def test_only_what_changed_is_sent(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        calls = self.calls(page)
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-upload-yes"]').click()                                                 # le droit d'envoi seul
        box.locator(".btn", has_text="Enregistrer").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertEqual(calls, ["upload"])
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-download-films"]').click()                                             # un droit de bibliothèque seul
        box.locator(".btn", has_text="Enregistrer").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertEqual(calls, ["upload", "rights"])
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-download-films"]').click()                                             # remis comme avant, avec le droit d'envoi retiré : les deux partent
        box.locator('[data-key="r-upload-no"]').click()
        box.locator(".btn", has_text="Enregistrer").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertEqual(sorted(calls[2:]), ["rights", "upload"])

    def test_one_refusal_says_which_part_failed_and_keeps_the_box_open(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        calls = self.calls(page)
        page.route("**/api/user/*/rights", lambda route: route.fulfill(status=500, content_type="application/json", body='{"error": "disque en panne"}'))
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-upload-yes"]').click()
        box.locator('[data-key="r-download-films"]').click()
        box.locator(".btn", has_text="Enregistrer").click()
        error = box.locator(".formerr")
        error.filter(has_text="disque en panne").wait_for()
        self.assertIn("Envoyer des fichiers", error.inner_text())
        self.assertIn("enregistré", error.inner_text())                                                  # l'envoi, lui, a réussi : on le dit
        self.assertTrue(self.users()["bob"]["can_upload"])
        self.assertEqual(self.users()["bob"]["rights"]["films"]["download"], True)                       # les droits de bibliothèque, non
        self.assertEqual(page.locator(".menu.rightsbox").count(), 1)                                     # la boîte reste ouverte
        page.unroute("**/api/user/*/rights")
        del calls[:]
        box.locator(".btn", has_text="Enregistrer").click()                                              # on réessaie : seul ce qui manque est renvoyé
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertEqual(calls, ["rights"])
        self.assertEqual(self.users()["bob"]["rights"]["films"]["download"], False)
        self.assertTrue(self.users()["bob"]["can_upload"])

    def test_closing_after_a_half_saved_attempt_shows_what_was_saved(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        page.route("**/api/user/*/rights", lambda route: route.fulfill(status=500, content_type="application/json", body='{"error": "disque en panne"}'))
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-upload-yes"]').click()
        box.locator('[data-key="r-download-films"]').click()
        box.locator(".btn", has_text="Enregistrer").click()
        box.locator(".formerr", has_text="disque en panne").wait_for()
        box.locator(".btn", has_text="Annuler").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        page.locator(f'[data-user="{self.bob["id"]}"] [data-rights]', has_text="Envoyer des fichiers : oui").wait_for()      # la liste dit ce qui est enregistré
        self.assertIn("Télécharger : tout", page.locator(f'[data-rights="{self.bob["id"]}"]').inner_text())                # les droits de bibliothèque, refusés, sont restés ce qu'ils étaient

    def test_a_refused_upload_right_does_not_make_the_library_rights_look_refused(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        page.route("**/api/user/*/upload", lambda route: route.fulfill(status=500, content_type="application/json", body='{"error": "base verrouillée"}'))
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-upload-yes"]').click()
        box.locator('[data-key="r-download-series"]').click()
        box.locator(".btn", has_text="Enregistrer").click()
        error = box.locator(".formerr")
        error.filter(has_text="base verrouillée").wait_for()
        self.assertIn("droits de bibliothèque", error.inner_text())
        self.assertIn("enregistrés", error.inner_text())
        self.assertFalse(self.users()["bob"]["can_upload"])
        self.assertFalse(self.users()["bob"]["rights"]["series"]["download"])
        page.unroute("**/api/user/*/upload")
        box.locator('[data-key="r-upload-no"]').click()                                                  # on renonce à l'envoi : les droits de bibliothèque, eux, sont gardés
        box.locator(".btn", has_text="Enregistrer").click()
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertFalse(self.users()["bob"]["can_upload"])
        self.assertFalse(self.users()["bob"]["rights"]["series"]["download"])

    def test_the_upload_choice_fits_a_phone(self):
        self.reset_rights(self.bob)
        page = self.session("admin", device="phone", width=390)
        box = self.rights_box(page, self.bob)
        chips = box.locator(".rrow[data-upload-right] .chip")
        edge = box.evaluate("e => e.getBoundingClientRect().right")
        self.assertTrue(chips.evaluate_all("(els, edge) => els.every(e => e.getBoundingClientRect().right <= edge)", edge))        # rien ne dépasse de la fenêtre
        self.assertTrue(chips.evaluate_all("els => els.every(e => e.getBoundingClientRect().height >= 32)"))                       # une cible tactile assez grande

    def test_the_arrow_keys_reach_the_upload_choice_and_escape_closes_the_box(self):
        self.reset_rights(self.bob)
        page = self.session("admin")
        box = self.rights_box(page, self.bob)
        box.locator('[data-key="r-download-livres"]').focus()                                            # le dernier choix de la dernière bibliothèque
        page.keyboard.press("ArrowDown")
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-key')"), "r-upload-no")      # la colonne de droite descend sur « Non »
        page.keyboard.press("ArrowLeft")
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-key')"), "r-upload-yes")
        page.keyboard.press("Enter")
        self.assertEqual(box.locator('[data-key="r-upload-yes"]').get_attribute("aria-pressed"), "true")
        page.keyboard.press("Escape")
        page.locator(".menu.rightsbox").wait_for(state="detached")
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-key')"), f"rights-{self.bob['id']}")
        self.assertFalse(self.users()["bob"]["can_upload"])

    # --- Envoyer -------------------------------------------------------------
    def test_the_intro_uses_the_prose_width(self):
        page = self.upload_page(width=1920)
        self.assertAlmostEqual(width_rem(page.locator("#views > .view:visible .intro")), PROSE, delta=0.1)

    def test_a_file_goes_through_and_keeps_the_name_the_server_gave_it(self):
        page = self.upload_page()
        self.send(page, ("Film.mkv", b"a" * 5000))
        self.sends(page, "done").first.wait_for()
        self.assertIn("Envoyé", self.sends(page).first.inner_text())
        self.assertEqual(self.stored(), ["Film.mkv"])
        self.send(page, ("Film.mkv", b"b" * 3000))                                        # le serveur ne l'écrase pas : il le renomme
        self.wait_for(lambda: self.sends(page, "done").count() == 2)
        self.assertEqual(self.stored(), ["Film (2).mkv", "Film.mkv"])
        self.assertEqual(self.sends(page, "done").nth(1).locator("b").inner_text(), "Film (2).mkv")
        self.assertIn("Film.mkv", self.sends(page, "done").nth(1).locator(".what > span").inner_text())     # et le dit : « tu l'avais appelé… »
        page.locator("#views > .view:visible .mine-file", has_text="Film (2).mkv").wait_for()         # « Mes envois » suit
        self.assertEqual(page.locator("#views > .view:visible .mine-file").count(), 2)
        self.assertIn("Espace utilisé", page.locator("#views > .view:visible .usedline").inner_text())

    def test_files_go_one_at_a_time(self):
        page = self.upload_page()
        starts = []
        page.on("request", lambda r: starts.append(r.url) if r.method == "POST" and "/api/inbox" in r.url else None)
        self.send(page, ("Un.mkv", b"u" * 4000), ("Deux.mkv", b"d" * 4000), ("Trois.mkv", b"t" * 4000))
        self.wait_for(lambda: self.sends(page, "done").count() == 3)
        self.assertEqual(len(starts), 3)
        self.assertEqual(self.stored(), ["Deux.mkv", "Trois.mkv", "Un.mkv"])
        order = [self.sends(page).nth(i).locator("b").inner_text() for i in range(3)]
        self.assertEqual(order, ["Un.mkv", "Deux.mkv", "Trois.mkv"])

    def test_a_file_dropped_on_the_page_is_sent(self):
        page = self.upload_page()
        script = """(kind) => {
            var el = document.querySelector('#views > .view.upload:not([style*="none"])');
            var dt = new DataTransfer();
            dt.items.add(new File(['bonjour glisser'], 'Glissé.mkv', { type: 'video/x-matroska' }));
            el.dispatchEvent(new DragEvent(kind, { dataTransfer: dt, bubbles: true, cancelable: true }));
            return el.querySelector('.drop').classList.contains('over');
        }"""
        self.assertTrue(page.evaluate(script, "dragenter"))
        self.assertFalse(page.evaluate(script, "drop"))
        self.sends(page, "done").first.wait_for()
        self.assertEqual(self.stored(), ["Glissé.mkv"])

    def test_the_progress_is_shown_while_the_file_goes(self):
        page = self.upload_page()
        self.throttle(page, 150_000)
        self.send(page, ("Lent.mkv", b"x" * 450_000))
        self.sending(page)
        page.wait_for_function("(() => { var s = document.querySelector('#views > .view:not([style*=\"none\"]) .send[data-state=\"send\"] .what > span'); return !!s && /reste/.test(s.textContent) })()", timeout=15000)
        text = self.sends(page, "send").first.locator(".what > span").inner_text()
        self.assertRegex(text, r"^\d+ % · .+ sur .+ · .+/s · reste ")                     # pourcentage, octets envoyés, débit moyen, durée restante
        self.assertLess(int(self.sends(page, "send").first.locator(".meter").get_attribute("aria-valuenow")), 100)
        self.sends(page, "done").first.wait_for(timeout=30000)
        self.assertEqual(self.stored(), ["Lent.mkv"])

    def test_an_extension_the_server_refuses_shows_its_message(self):
        page = self.upload_page()
        self.send(page, ("virus.exe", b"MZ" * 100), ("Suite.mkv", b"s" * 3000))
        self.sends(page, "error").first.wait_for()
        self.assertIn("type de fichier refusé", self.sends(page, "error").first.inner_text())
        self.wait_for(lambda: self.sends(page, "done").count() == 1)                       # un refus n'arrête pas la file
        self.assertEqual(self.stored(), ["Suite.mkv"])

    def test_a_server_without_room_refuses_with_507_and_the_message_shows(self):
        page = self.upload_page()
        self.hub.inbox.keep_free = 1 << 60                                                 # HUB_INBOX_KEEP_FREE très haut : l'espace libre « tomberait en dessous »
        self.send(page, ("Plein.mkv", b"p" * 3000))
        self.sends(page, "error").first.wait_for()
        self.assertIn("plus assez de place sur le serveur", self.sends(page, "error").first.inner_text())
        self.assertEqual(self.sends(page, "error").first.locator("button").inner_text(), "Retirer de la liste")
        self.assertEqual(self.stored(), [])
        self.hub.inbox.keep_free = 0
        self.send(page, ("Suite.mkv", b"s" * 3000))                                        # un refus n'arrête pas la file : l'envoi suivant passe dès qu'il y a de la place
        self.sends(page, "done").first.wait_for()
        self.assertEqual(self.stored(), ["Suite.mkv"])

    def test_a_busy_server_gets_the_file_back_in_the_queue(self):
        page = self.upload_page()
        page.evaluate("Hub.upload.retry = 200")
        calls = []

        def busy(route):
            calls.append(1)
            if len(calls) == 1:
                route.fulfill(status=429, content_type="application/json", body=json.dumps({"error": "trop d'envois en cours : réessaie dans un moment"}))
            else:
                route.continue_()
        page.route("**/api/inbox?name=*", busy)
        self.send(page, ("Occupé.mkv", b"o" * 3000))
        page.locator("#views > .view:visible .send[data-state=wait]", has_text="occupé").wait_for()
        self.sends(page, "done").first.wait_for()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.stored(), ["Occupé.mkv"])

    def test_an_expired_session_stops_the_queue(self):
        page = self.upload_page()
        page.route("**/api/inbox?name=*", lambda route: route.fulfill(status=401, content_type="application/json", body=json.dumps({"error": "connexion requise"})))
        self.send(page, ("Un.mkv", b"1" * 3000), ("Deux.mkv", b"2" * 3000))
        self.wait_for(lambda: self.sends(page, "error").count() == 2)
        self.assertIn("la session a expiré", self.sends(page, "error").nth(1).inner_text())
        self.assertTrue(page.locator("#views > .view:visible [role=alert] a[href='/login']").is_visible())
        self.assertEqual(page.evaluate("Hub.current().hash"), "#/upload")                    # la page reste : on voit ce qui n'est pas parti
        self.assertEqual(self.stored(), [])

    def test_a_cancelled_upload_leaves_nothing_on_the_server(self):
        page = self.upload_page()
        self.throttle(page, 150_000)
        self.send(page, ("Annulé.mkv", b"x" * 600_000), ("Suivant.mkv", b"y" * 2000))
        self.sending(page)
        self.sends(page, "send").first.locator("button", has_text="Annuler").click()
        self.sends(page, "cancel").first.wait_for()
        self.assertEqual(self.sends(page, "cancel").first.locator(".what > span").inner_text(), "Annulé")
        self.assertEqual(self.sends(page).first.locator("button").inner_text(), "Retirer de la liste")
        self.throttle(page, -1)
        self.sends(page, "done").first.wait_for(timeout=20000)                               # le suivant part
        self.wait_for(lambda: self.stored() == ["Suivant.mkv"])                              # le serveur a effacé le .part
        self.sends(page, "cancel").first.locator("button").click()                           # « Retirer de la liste »
        self.assertEqual(self.sends(page, "cancel").count(), 0)
        self.assertEqual(self.sends(page).count(), 1)

    # --- Mes envois ------------------------------------------------------------
    def test_my_uploads_list_and_delete_with_confirmation(self):
        self.put("alice", "Premier.mkv", b"1" * 2048, mtime=1_700_000_000)
        self.put("alice", "Second.mkv", b"2" * 4096, mtime=1_700_100_000)
        self.put("admin", "Pas à elle.mkv", b"3")
        page = self.upload_page()
        rows = page.locator("#views > .view:visible .mine-file")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.nth(0).locator("b").inner_text(), "Second.mkv")                  # le plus récent d'abord
        self.assertIn("4 Ko", rows.nth(0).locator(".what > span").inner_text())
        self.assertRegex(rows.nth(0).locator(".what > span").inner_text(), r"\d{2}/\d{2}/\d{4}")
        rows.nth(0).locator("button", has_text="Supprimer").click()
        dialog = page.locator(".menu[role=dialog]")
        dialog.wait_for()
        self.assertIn("Second.mkv", dialog.inner_text())
        dialog.get_by_role("button", name="Annuler", exact=True).click()                                   # Annuler ne supprime rien
        self.assertEqual(self.stored(), ["Premier.mkv", "Second.mkv"])
        rows.nth(0).locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: self.stored() == ["Premier.mkv"])
        self.wait_for(lambda: rows.count() == 1)
        self.assertEqual(self.stored("admin"), ["Pas à elle.mkv"])
        rows.first.locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        page.locator("#views > .view:visible .mine .empty").wait_for()
        self.assertIn("rien envoyé", page.locator("#views > .view:visible .mine .empty").inner_text())

    # --- Quitter la page ---------------------------------------------------------
    def test_leaving_during_an_upload_asks_first(self):
        page = self.upload_page()
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/api/inbox" in r.url else None)
        self.throttle(page, 150_000)
        self.send(page, ("Long.mkv", b"x" * 600_000), ("Après.mkv", b"y" * 1000))
        self.sending(page)
        page.locator("#nav .brand").click()                                                    # une autre page du routeur
        dialog = page.locator(".menu[role=dialog]")
        dialog.wait_for()
        self.assertEqual(dialog.locator("h3").inner_text(), "Quitter la page ?")
        self.assertEqual(page.evaluate("Hub.current().hash"), "#/upload")                     # l'adresse n'a pas bougé
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        dialog.wait_for(state="detached")
        self.assertEqual(self.sends(page, "send").count(), 1)                                  # l'envoi continue
        page.locator("#nav .brand").click()
        page.locator(".menu .btn.danger", has_text="Quitter et annuler").click()
        self.shown(page, "#/")
        self.throttle(page, -1)
        self.wait_for(lambda: self.stored() == [])                                             # rien n'est resté sur le serveur, et « Après.mkv » n'est pas parti
        page.wait_for_timeout(300)
        self.assertEqual(self.stored(), [])
        self.assertEqual(len(posts), 1)                                                        # l'annulation n'a pas lancé « Après.mkv » au passage

    def test_the_browser_is_asked_before_the_tab_closes_during_an_upload(self):
        page = self.upload_page()
        self.throttle(page, 150_000)
        page.mouse.click(300, 400)                                                             # le navigateur ne pose la question qu'à une page où l'on a agi
        self.send(page, ("Long.mkv", b"x" * 600_000))
        self.sending(page)
        kinds = []
        page.on("dialog", lambda d: (kinds.append(d.type), d.dismiss()))
        page.close(run_before_unload=True)
        page.context.new_page().wait_for_timeout(500)                                           # (les rappels de Playwright ne passent que pendant un de ses appels)
        self.assertEqual(kinds, ["beforeunload"])
        self.assertFalse(page.is_closed())                                                     # « Rester sur la page » : elle reste, et l'envoi continue
        self.assertEqual(self.sends(page, "send").count(), 1)

    def test_leaving_without_an_upload_asks_nothing(self):
        page = self.upload_page()
        page.locator("#nav .brand").click()
        self.shown(page, "#/")
        self.assertEqual(page.locator(".menu[role=dialog]").count(), 0)

    # --- L'administrateur ------------------------------------------------------------
    def test_the_administrator_reads_downloads_and_deletes_the_inbox(self):
        self.put("alice", "Ancien.mkv", b"ancien contenu", mtime=1_700_000_000)
        self.put("alice", "Récent.mkv", b"recent", mtime=1_700_200_000)
        self.put("admin", "Du patron.zip", b"zip", mtime=1_700_100_000)
        page = self.session("admin")
        page.goto(self.base + "/#/settings?tab=inbox")
        rows = page.locator("#views > .view:visible .inbox-file")
        rows.first.wait_for()
        self.assertEqual(page.locator("#views > .view:visible .tabs .tab[aria-selected=true]").inner_text(), "Boîte de réception")
        self.assertEqual([rows.nth(i).locator("b").inner_text() for i in range(3)], ["Récent.mkv", "Du patron.zip", "Ancien.mkv"])        # du plus récent au plus ancien
        self.assertIn("alice", rows.nth(0).locator(".what > span").inner_text())
        self.assertIn("admin", rows.nth(1).locator(".what > span").inner_text())
        for i in range(3):
            self.assertEqual(rows.nth(i).locator(".btn").all_inner_texts(), ["Télécharger", "Supprimer"])       # aucun bouton qui range dans une bibliothèque
        with page.expect_download() as info:
            rows.nth(2).locator("a", has_text="Télécharger").click()
        download = info.value
        self.assertEqual(download.suggested_filename, "Ancien.mkv")
        self.assertEqual(Path(download.path()).read_bytes(), b"ancien contenu")
        rows.nth(0).locator("button", has_text="Supprimer").click()
        dialog = page.locator(".menu[role=dialog]")
        self.assertIn("alice", dialog.inner_text())
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        self.assertEqual(self.stored(), ["Ancien.mkv", "Récent.mkv"])
        rows.nth(0).locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: self.stored() == ["Ancien.mkv"])
        self.wait_for(lambda: rows.count() == 2)
        for expected in ([], ):
            rows.nth(1).locator("button", has_text="Supprimer").click()
            page.locator(".menu .btn.danger").click()
            self.wait_for(lambda: self.stored() == expected)
        self.wait_for(lambda: rows.count() == 1)
        rows.nth(0).locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        page.locator("#views > .view:visible .inbox .empty").wait_for()
        self.assertEqual(page.locator("#views > .view:visible .inbox .empty").inner_text(), "La boîte de réception est vide.")
        self.assertEqual(self.stored("admin"), [])

    def test_only_the_administrator_has_the_inbox_tab(self):
        page = self.session("alice")
        page.goto(self.base + "/#/settings?tab=inbox")
        page.wait_for_selector("#views > .view:visible .tabs .tab")
        self.assertEqual(page.locator("#views > .view:visible .tabs .tab").all_inner_texts(), ["Préférences", "Comptes"])
        self.assertEqual(page.context.request.get(self.base + "/api/inbox").status, 403)

    # --- Le dossier d'un compte porte son nom --------------------------------------------
    def test_an_upload_lands_in_a_folder_named_after_the_account(self):
        marmotte = self.member("Marmotte")
        page = self.upload_page("Marmotte")
        self.send(page, ("Film.mkv", b"f" * 2000), ("Été.mkv", b"e" * 2000))
        self.wait_for(lambda: self.sends(page, "done").count() == 2)
        self.assertEqual(sorted(p.name for p in self.box.iterdir()), ["Marmotte"])               # ni l'identifiant du compte, ni « default »
        self.assertNotIn(marmotte["id"], [p.name for p in self.box.iterdir()])
        self.assertEqual(self.stored("Marmotte"), ["Film.mkv", "Été.mkv"])
        page.locator("#views > .view:visible .mine-file").first.wait_for()
        self.assertEqual(sorted(page.locator("#views > .view:visible .mine-file b").all_inner_texts()), ["Film.mkv", "Été.mkv"])
        row = page.locator("#views > .view:visible .mine-file", has_text="Film.mkv")                # elle supprime le sien, par l'adresse de son dossier
        row.locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: self.stored("Marmotte") == ["Été.mkv"])

    def test_the_administrator_sees_the_name_of_the_account_and_downloads_and_deletes_whatever_its_accents(self):
        for name in ("Marmotte", "Léa", "Élodie"):
            with self.subTest(account=name):
                self.member(name)
                self.put(name, "Été à Paris (2).mkv", name.encode() * 50)
                self.assertEqual(self.stored(name), ["Été à Paris (2).mkv"])
                page, rows = self.admin_inbox()
                self.assertEqual(rows.count(), 1)
                self.assertEqual(rows.get_attribute("data-account"), name)                         # le dossier, tel que l'administrateur le lit sur le partage
                what = rows.locator(".what > span").inner_text()
                self.assertTrue(what.startswith(name + " · "), what)                               # le nom du compte, sans mention « supprimé »
                self.assertNotIn("supprimé", what)
                with page.expect_download() as info:
                    rows.locator("a", has_text="Télécharger").click()
                self.assertEqual(info.value.suggested_filename, "Été à Paris (2).mkv")
                self.assertEqual(Path(info.value.path()).read_bytes(), name.encode() * 50)
                rows.locator("button", has_text="Supprimer").click()
                self.assertIn(name, page.locator(".menu[role=dialog]").inner_text())
                page.locator(".menu .btn.danger").click()
                page.locator("#views > .view:visible .inbox .empty").wait_for()
                self.assertEqual(self.stored(name), [])

    def test_the_files_of_a_deleted_account_stay_for_the_administrator_marked_as_such(self):
        from hub import accounts
        lea = self.member("Léa")
        self.member("Marmotte")
        self.put("Léa", "Souvenir.mkv", b"souvenir", mtime=1_700_000_000)
        self.put("Marmotte", "Récent.mkv", b"recent", mtime=1_700_100_000)
        accounts.delete(self.hub.db, lea["id"])
        page, rows = self.admin_inbox()
        self.assertEqual(rows.count(), 2)
        alive, gone = rows.nth(0), rows.nth(1)
        self.assertEqual(alive.locator("b").inner_text(), "Récent.mkv")
        self.assertTrue(alive.locator(".what > span").inner_text().startswith("Marmotte · "))
        self.assertEqual(gone.locator("b").inner_text(), "Souvenir.mkv")
        self.assertTrue(gone.locator(".what > span").inner_text().startswith("Léa (compte supprimé) · "), gone.locator(".what > span").inner_text())
        self.assertEqual(self.stored("Léa"), ["Souvenir.mkv"])                                     # le compte parti, ses fichiers restent sur le disque
        with page.expect_download() as info:
            gone.locator("a", has_text="Télécharger").click()
        self.assertEqual(Path(info.value.path()).read_bytes(), b"souvenir")
        gone.locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: self.stored("Léa") == [])
        self.wait_for(lambda: rows.count() == 1)
        self.assertEqual(self.stored("Marmotte"), ["Récent.mkv"])

    def test_the_deleted_account_mention_is_in_english(self):
        from hub import accounts
        lea = self.member("Léa")
        self.put("Léa", "Souvenir.mkv")
        accounts.delete(self.hub.db, lea["id"])
        page, rows = self.admin_inbox(lang="en")
        self.assertTrue(rows.locator(".what > span").inner_text().startswith("Léa (deleted account) · "), rows.locator(".what > span").inner_text())
        self.assertEqual(page.evaluate("Hub.missing"), [])

    def test_an_account_that_comes_back_under_the_name_of_a_deleted_one_has_its_own_folder(self):
        from hub import accounts
        lea = self.member("Léa")
        self.put("Léa", "Ancien.mkv")
        accounts.delete(self.hub.db, lea["id"])
        self.member("Léa")
        self.put("Léa", "Neuf.mkv")
        self.assertEqual((self.stored("Léa"), self.stored("Léa (2)")), (["Ancien.mkv"], ["Neuf.mkv"]))
        page = self.upload_page("Léa")
        self.assertEqual(page.locator("#views > .view:visible .mine-file b").all_inner_texts(), ["Neuf.mkv"])      # elle ne voit pas les fichiers de l'ancienne
        _, rows = self.admin_inbox()
        self.assertEqual(sorted(rows.nth(i).locator(".what > span").inner_text().split(" · ")[0] for i in range(rows.count())), ["Léa", "Léa (compte supprimé)"])

    def test_an_account_that_may_upload_sees_and_touches_only_its_own_files(self):
        self.member("Marmotte")
        self.put("Marmotte", "A moi.mkv", b"moi")
        self.put("alice", "A elle.mkv", b"elle")
        self.put("admin", "Du patron.zip", b"zip")
        page = self.upload_page("Marmotte")
        self.assertEqual(page.locator("#views > .view:visible .mine-file b").all_inner_texts(), ["A moi.mkv"])
        self.assertEqual(page.context.request.get(self.base + "/api/inbox").status, 403)                       # la liste de tous : pour l'administrateur
        self.assertEqual(page.context.request.get(self.base + "/api/inbox/alice/A%20elle.mkv").status, 403)    # le téléchargement aussi
        self.assertEqual(page.context.request.get(self.base + "/api/inbox/Marmotte/A%20moi.mkv").status, 403)  # même celui de son propre dossier
        self.assertEqual(page.context.request.delete(self.base + "/api/inbox/alice/A%20elle.mkv").status, 403)
        self.assertEqual(self.stored("alice"), ["A elle.mkv"])
        self.assertEqual(page.context.request.delete(self.base + "/api/inbox/Marmotte/A%20moi.mkv").status, 200)
        self.assertEqual(self.stored("Marmotte"), [])

    # --- Télévision, anglais -------------------------------------------------------------
    def test_a_television_has_neither_the_entry_nor_the_page(self):
        page = self.session("alice", device="tv", width=1920)
        self.assertEqual(page.evaluate("Hub.device"), "tv")
        self.assertEqual(page.locator('#nav [data-nav="#/upload"]').count(), 0)
        page.goto(self.base + "/#/upload")
        page.locator("#toast", has_text="télévision").wait_for()
        self.shown(page, "#/")
        self.assertEqual(page.locator("#views > .view:visible .upload").count(), 0)
        self.assertEqual(page.locator("#views > .view:visible input[type=file]").count(), 0)

    def test_changing_the_device_in_the_settings_moves_the_entry(self):
        page = self.session("alice")
        page.goto(self.base + "/#/settings?tab=preferences")
        page.locator('[data-key="device"]').wait_for()
        self.assertEqual(page.locator('#nav [data-nav="#/upload"]').count(), 1)
        page.locator('[data-key="device"]').click()
        page.locator(".menu .opt", has_text="Télévision").click()
        page.wait_for_function("document.querySelectorAll('#nav [data-nav=\"#/upload\"]').length === 0")

    def test_the_upload_page_is_in_english(self):
        page = self.upload_page(lang="en")
        self.assertEqual(page.locator('#nav [data-nav="#/upload"]').get_attribute("aria-label"), "Upload")
        self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Upload")
        self.assertEqual(page.locator("#views > .view:visible .drop .btn").all_inner_texts(), ["Choose files", "Send a folder"])
        self.assertIn("Space used", page.locator("#views > .view:visible .usedline").inner_text())
        self.assertEqual(page.locator("#views > .view:visible .mine .empty").inner_text(), "You haven’t uploaded anything yet.")
        self.send(page, ("virus.exe", b"MZ" * 100), ("Film.mkv", b"f" * 3000))
        self.sends(page, "error").first.wait_for()
        self.assertEqual(self.sends(page, "error").first.locator(".what > span").inner_text(), "file type refused: « .exe »")      # le message du serveur, traduit
        self.wait_for(lambda: self.sends(page, "done").count() == 1)
        self.assertIn("Uploaded", self.sends(page, "done").first.inner_text())
        self.assertEqual(self.sends(page, "error").first.locator("button").inner_text(), "Remove from the list")
        page.locator("#views > .view:visible .mine-file").wait_for()
        self.assertIn("2.9 KB", page.locator("#views > .view:visible .mine-file .what > span").inner_text())                         # les tailles suivent la langue
        self.hub.inbox.keep_free = 1 << 60
        self.send(page, ("Plein.mkv", b"p" * 3000))
        page.locator("#views > .view:visible .send[data-state=error]", has_text="not enough room on the server").wait_for()
        self.assertEqual(page.evaluate("Hub.missing"), [])

    def test_the_inbox_tab_and_the_upload_choice_are_in_english(self):
        self.put("alice", "Film.mkv", b"f" * 2000)
        page = self.session("admin", lang="en")
        page.goto(self.base + "/#/settings?tab=inbox")
        page.locator("#views > .view:visible .inbox-file").wait_for()
        self.assertEqual(page.locator("#views > .view:visible .tabs .tab").all_inner_texts(), ["Management", "Inbox", "Preferences", "Accounts"])
        self.assertEqual(page.locator("#views > .view:visible .inbox-file .btn").all_inner_texts(), ["Download", "Delete"])
        self.assertEqual(page.locator("#views > .view:visible .inbox .total").inner_text(), "1 file · 2 KB")
        page.goto(self.base + "/#/settings?tab=comptes")
        page.locator(f'[data-key="rights-{self.bob["id"]}"]').click()
        page.locator(".menu.rightsbox .rrow[data-upload-right]").wait_for()
        self.assertEqual(page.locator(".menu.rightsbox .rrow[data-upload-right] b").inner_text(), "Upload files")
        self.assertEqual([t.strip("✓ \n") for t in page.locator(".menu.rightsbox .rrow[data-upload-right] .chip").all_inner_texts()], ["Yes", "No"])
        self.assertIn("Upload files: no", page.locator(f'[data-user="{self.bob["id"]}"] [data-rights]').inner_text())
        page.locator(".menu.rightsbox .btn", has_text="Cancel").click()
        self.assertIn("always", page.locator(f'[data-upload="{self.admin_id}"]').inner_text())
        self.assertEqual(page.evaluate("Hub.missing"), [])

    # --- Retours de la relecture : historique, noms de fichiers, cas limites ------------------
    def start_upload_from_home(self):
        page = self.session("alice")
        page.locator('#nav [data-nav="#/upload"]').click()
        self.shown(page, "#/upload")
        page.wait_for_selector("#views > .view:visible .used .usedline")
        self.throttle(page, 150_000)
        self.send(page, ("Long.mkv", b"x" * 600_000))
        self.sending(page)
        return page

    def test_the_back_button_asks_and_staying_keeps_the_page_and_its_address(self):
        page = self.start_upload_from_home()
        page.go_back()                                                                         # Retour du navigateur (ou de la télécommande)
        dialog = page.locator(".menu[role=dialog]")
        dialog.wait_for()
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        dialog.wait_for(state="detached")
        page.wait_for_function("location.hash === '#/upload'")                                 # on reste sur la page, adresse comprise
        self.assertEqual(self.sends(page, "send").count(), 1)                                  # et l'envoi continue
        self.throttle(page, -1)
        self.sends(page, "done").first.wait_for(timeout=20000)

    def test_the_back_button_then_leaving_cancels_the_upload(self):
        page = self.start_upload_from_home()
        page.go_back()
        page.locator(".menu .btn.danger", has_text="Quitter et annuler").click()
        self.shown(page, "#/")
        self.assertIn(page.evaluate("location.hash"), ("", "#/"))                              # l'accueil d'où l'on venait
        self.assertEqual(page.locator(".menu[role=dialog]").count(), 0)
        self.throttle(page, -1)
        page.wait_for_timeout(300)
        self.assertEqual(self.stored(), [])

    def test_after_leaving_by_a_link_one_back_returns_to_the_page(self):
        page = self.upload_page()
        self.throttle(page, 150_000)
        self.send(page, ("Long.mkv", b"x" * 600_000))
        self.sending(page)
        page.locator('#nav [data-nav="#/search"]').click()
        page.locator(".menu .btn.danger", has_text="Quitter et annuler").click()
        self.shown(page, "#/search")
        page.go_back()                                                                         # un seul Retour, pas deux : l'historique n'a pas de doublon
        self.shown(page, "#/upload")
        self.throttle(page, -1)

    def test_a_name_with_html_and_url_characters_is_text_and_survives_the_links(self):
        page = self.upload_page()
        nasty = "<img src=x onerror=window.pwned=1> a#b%41&c é'x.mkv"
        self.send(page, (nasty, b"n" * 2000))
        self.sends(page, "done").first.wait_for()
        kept = self.stored()[0]
        self.assertIn("#", kept)
        self.assertIn("%41", kept)
        self.assertEqual(self.sends(page, "done").first.locator("b").inner_text(), kept)
        self.assertEqual(self.sends(page).locator("img").count(), 0)                           # du texte, jamais du HTML
        self.assertIsNone(page.evaluate("window.pwned"))
        admin = self.session("admin")
        admin.goto(self.base + "/#/settings?tab=inbox")
        row = admin.locator("#views > .view:visible .inbox-file")
        row.first.wait_for()
        self.assertEqual(row.locator("b").inner_text(), kept)
        with admin.expect_download() as info:
            row.locator("a", has_text="Télécharger").click()
        self.assertEqual(Path(info.value.path()).read_bytes(), b"n" * 2000)                    # le nom est bien encodé dans l'adresse
        row.locator("button", has_text="Supprimer").click()
        admin.locator(".menu .btn.danger").click()
        self.wait_for(lambda: self.stored() == [])                                             # et dans celle de la suppression

    def test_a_file_waiting_for_a_new_try_can_be_cancelled(self):
        page = self.upload_page()
        page.evaluate("Hub.upload.retry = 60000")
        calls = []

        def busy(route):
            calls.append(1)
            route.fulfill(status=429, content_type="application/json", body=json.dumps({"error": "trop d'envois en cours : réessaie dans un moment"}))
        page.route("**/api/inbox?name=*", busy)
        self.send(page, ("Occupé.mkv", b"o" * 3000))
        page.locator("#views > .view:visible .send[data-state=wait]", has_text="occupé").wait_for()
        self.sends(page, "wait").first.locator("button", has_text="Annuler").click()
        self.sends(page, "cancel").first.wait_for()
        page.wait_for_timeout(300)
        self.assertEqual(len(calls), 1)                                                        # plus d'essai
        self.assertEqual(page.evaluate("document.querySelectorAll('#views > .view .send[data-state=wait]').length"), 0)

    def test_after_the_session_expires_a_new_file_is_not_left_waiting(self):
        page = self.upload_page()
        page.route("**/api/inbox?name=*", lambda route: route.fulfill(status=401, content_type="application/json", body=json.dumps({"error": "connexion requise"})))
        self.send(page, ("Un.mkv", b"1" * 3000))
        self.sends(page, "error").first.wait_for()
        self.send(page, ("Deux.mkv", b"2" * 3000))
        self.wait_for(lambda: self.sends(page, "error").count() == 2)
        self.assertIn("la session a expiré", self.sends(page, "error").nth(1).inner_text())
        self.assertEqual(self.sends(page, "wait").count(), 0)
        page.locator("#nav .brand").click()                                                    # rien n'est en cours : on part sans question
        self.shown(page, "#/")
        self.assertEqual(page.locator(".menu[role=dialog]").count(), 0)

    def test_a_connection_cut_without_an_answer_says_so(self):
        page = self.upload_page()
        page.route("**/api/inbox?name=*", lambda route: route.abort("connectionreset"))
        self.send(page, ("Coupé.mkv", b"c" * 3000))
        self.sends(page, "error").first.wait_for()
        self.assertIn("connexion a été coupée", self.sends(page, "error").first.inner_text())
        self.assertIn("occupé", self.sends(page, "error").first.inner_text())

    def test_the_focus_goes_back_to_the_page_when_a_line_is_removed(self):
        page = self.upload_page()
        self.send(page, ("virus.exe", b"MZ" * 100))
        self.sends(page, "error").first.wait_for()
        page.locator("#views > .view:visible .send button").focus()
        page.keyboard.press("Enter")                                                           # « Retirer de la liste » à la télécommande
        self.assertEqual(self.sends(page).count(), 0)
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-key')"), "choose")

    def test_a_phone_gets_no_drag_and_drop_wording(self):
        page = self.upload_page(device="phone", width=390)
        self.assertEqual(page.locator("#views > .view:visible .dropline").inner_text(), "Choisis les fichiers à envoyer depuis cet appareil.")

    # --- Envoyer un dossier --------------------------------------------------------------
    def make_folder(self, name, files):
        """Un vrai dossier sur le disque de la machine d'essai, tel que la personne le choisirait : `files` associe un chemin relatif à son contenu."""
        root = Path(tempfile.mkdtemp(dir=self.tmp)) / name
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return root

    def pick_folder(self, page, root):
        page.set_input_files("#views > .view:visible input[webkitdirectory]", str(root))

    def dialog(self, page):
        dialog = page.locator(".menu[role=dialog]")
        dialog.wait_for()
        return dialog

    def confirm(self, page, label="Envoyer"):
        """Répond « oui » à la fenêtre ouverte et attend qu'elle se ferme."""
        dialog = self.dialog(page)
        dialog.get_by_role("button", name=label, exact=True).click()
        dialog.wait_for(state="detached")

    def tree(self, account="alice"):
        """Tous les fichiers du dossier du compte dans la boîte, sous-dossiers compris, en chemins relatifs."""
        base = self.box / account
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()) if base.is_dir() else []

    def folders(self, page, state=""):
        return page.locator("#views > .view:visible .send.folder" + (f'[data-state="{state}"]' if state else ""))

    def posts(self, page):
        """Les adresses des envois (POST /api/inbox) que la page fait à partir de maintenant."""
        found = []
        page.on("request", lambda r: found.append(urllib.parse.unquote(r.url)) if r.method == "POST" and "/api/inbox" in r.url else None)
        return found

    def line(self, folder):
        """Le texte d'état de la ligne d'un dossier."""
        return folder.locator(":scope > .what > span").inner_text()

    def test_a_folder_with_a_subfolder_is_sent_one_file_at_a_time(self):
        page = self.upload_page()
        posts = self.posts(page)
        root = self.make_folder("Film", {"a.mkv": b"a" * 3000, "Bonus/b.mkv": b"b" * 2000, "Bonus/Coupes/c.srt": b"c" * 1000})
        self.pick_folder(page, root)
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Envoyer ce dossier ?")
        self.assertEqual(dialog.locator(".lead").inner_text(), "Envoyer le dossier « Film » : 3 fichiers (5,9 Ko).")
        self.assertEqual(dialog.locator(".skipped").count(), 0)                           # rien d'écarté : pas de liste à déplier
        page.wait_for_timeout(300)
        self.assertEqual(posts, [])                                                       # rien ne part avant la confirmation
        self.confirm(page)
        folder = self.folders(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(folder.count(), 1)                                               # une ligne par dossier, pas une par fichier
        self.assertEqual(self.sends(page).count(), 1)
        self.assertEqual(folder.locator(":scope > .what > b").inner_text(), "Film")
        self.assertEqual(self.line(folder), "3 / 3 fichiers · 5,9 Ko · Envoyé")
        self.assertEqual(self.tree(), sorted(["Film/a.mkv", "Film/Bonus/b.mkv", "Film/Bonus/Coupes/c.srt"]))
        self.assertEqual(self.stored(), ["Film"])
        self.assertEqual(sorted(u.split("name=")[1] for u in posts), sorted(["a.mkv&dir=Film", "b.mkv&dir=Film/Bonus", "c.srt&dir=Film/Bonus/Coupes"]))
        details = folder.locator(":scope > .sendfiles")
        self.assertTrue(details.is_hidden())                                              # replié : les fichiers se voient à la demande
        folder.locator(":scope > button[data-key^=files-]").click()
        self.assertTrue(details.is_visible())
        children = details.locator(".sendfile")
        self.assertEqual([children.nth(i).locator("b").inner_text() for i in range(3)], ["a.mkv", "Bonus/b.mkv", "Bonus/Coupes/c.srt"])
        self.assertEqual(details.locator(".sendfile[data-state=done]").count(), 3)
        rows = page.locator("#views > .view:visible .mine-file")
        self.wait_for(lambda: rows.count() == 3)                                           # « Mes envois » suit, avec le sous-dossier de chacun
        listed = sorted(rows.nth(i).locator("b").inner_text() + " @ " + rows.nth(i).get_attribute("data-dir") for i in range(3))
        self.assertEqual(listed, ["a.mkv @ Film", "b.mkv @ Film/Bonus", "c.srt @ Film/Bonus/Coupes"])
        self.assertIn("Film/Bonus/Coupes", rows.filter(has_text="c.srt").locator(".what > span").inner_text())

    def test_files_a_folder_cannot_send_are_left_out_and_counted(self):
        page = self.upload_page()
        posts = self.posts(page)
        root = self.make_folder("Saison", {
            "01.mkv": b"1" * 2000, "02.mkv": b"2" * 2000, "Un/Deux/Trois/ok.mkv": b"o",                          # 4 niveaux avec « Saison » : accepté
            "notes.txt": b"n", "virus.exe": b"MZ",                                                                 # des types que le serveur refuse
            ".cache/x.mkv": b"x", "Thumbs.db": b"t", "desktop.ini": b"d", ".DS_Store": b"d", ".cache.mkv": b"h",   # cachés
            "Un/Deux/Trois/Quatre/Cinq/profond.mkv": b"p",                                                         # trop profond
            "vide.mkv": b""})
        self.pick_folder(page, root)
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator(".lead").inner_text(),
                         "Envoyer le dossier « Saison » : 3 fichiers (3,9 Ko), 9 ignorés (types non acceptés, fichiers cachés, dossiers trop profonds, fichiers vides).")
        toggle = dialog.locator("button[data-key=skipped]")
        self.assertEqual(toggle.inner_text(), "Voir les fichiers ignorés")
        skipped = dialog.locator(".skipped")
        self.assertTrue(skipped.is_hidden())
        toggle.click()
        self.assertTrue(skipped.is_visible())
        self.assertEqual(toggle.inner_text(), "Masquer les fichiers ignorés")
        lines = skipped.locator("li").all_inner_texts()
        self.assertEqual(len(lines), 9)
        self.assertIn("Saison/notes.txt — type non accepté", lines)
        self.assertIn("Saison/virus.exe — type non accepté", lines)
        self.assertIn("Saison/.cache/x.mkv — fichier caché", lines)
        self.assertIn("Saison/Thumbs.db — fichier caché", lines)
        self.assertIn("Saison/Un/Deux/Trois/Quatre/Cinq/profond.mkv — trop profond (4 niveaux au plus)", lines)
        self.assertIn("Saison/vide.mkv — fichier vide", lines)
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.line(self.folders(page)), "3 / 3 fichiers · 3,9 Ko · Envoyé")
        self.assertEqual(self.tree(), ["Saison/01.mkv", "Saison/02.mkv", "Saison/Un/Deux/Trois/ok.mkv"])
        self.assertEqual(len(posts), 3)                                                                            # les écartés ne sont même pas tentés

    def test_a_long_list_of_left_out_files_is_cut_short(self):
        page = self.upload_page()
        self.pick_folder(page, self.make_folder("Vrac", {"a.mkv": b"a", **{f"x{n:03d}.txt": b"x" for n in range(130)}}))
        dialog = self.dialog(page)
        dialog.locator("button", has_text="Voir les fichiers ignorés").click()
        lines = dialog.locator(".skipped li").all_inner_texts()
        self.assertEqual(len(lines), 101)
        self.assertEqual(lines[-1], "… et 30 autres")

    def test_cancelling_the_confirmation_sends_nothing(self):
        page = self.upload_page()
        posts = self.posts(page)
        self.pick_folder(page, self.make_folder("Film", {"a.mkv": b"a" * 3000}))
        dialog = self.dialog(page)
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        dialog.wait_for(state="detached")
        page.wait_for_timeout(300)
        self.assertEqual(posts, [])
        self.assertEqual(self.folders(page).count(), 0)
        self.assertTrue(page.locator("#views > .view:visible .sends").locator("xpath=..").is_hidden())             # le panneau « Envois » reste caché
        self.assertEqual(self.tree(), [])
        self.pick_folder(page, self.make_folder("Film", {"a.mkv": b"a" * 3000}))                                   # et on peut choisir de nouveau (même dossier)
        self.confirm(page)
        self.folders(page, "done").wait_for()

    def test_a_folder_with_nothing_the_server_accepts_says_so(self):
        page = self.upload_page()
        posts = self.posts(page)
        self.pick_folder(page, self.make_folder("Papiers", {"notes.txt": b"n", "Scans/photo.png": b"p", ".hidden.mkv": b"h"}))
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Rien à envoyer")
        self.assertIn("Aucun fichier du dossier « Papiers » ne peut être envoyé (types non acceptés, fichiers cachés)", dialog.inner_text())
        self.assertEqual(dialog.locator(".btn.primary").inner_text(), "Compris")
        dialog.locator(".btn.primary").click()
        dialog.wait_for(state="detached")
        self.assertEqual(posts, [])
        self.assertEqual(self.folders(page).count(), 0)

    def test_cancelling_a_folder_in_progress_cancels_what_is_left(self):
        page = self.upload_page()
        posts = self.posts(page)
        self.throttle(page, 150_000)
        self.pick_folder(page, self.make_folder("Album", {f"{n}.flac": b"x" * 300_000 for n in range(1, 5)}))
        self.confirm(page)
        self.sending(page)
        folder = self.folders(page, "send")
        self.assertRegex(self.line(folder), r"^0 / 4 fichiers · 1,1 Mo · \d+ %")                                   # l'avancement global
        self.assertEqual(folder.locator(":scope > button:visible").all_inner_texts(), ["Voir les fichiers", "Annuler"])
        folder.locator(":scope > button", has_text="Annuler").click()
        self.folders(page, "cancel").wait_for()
        self.assertEqual(self.line(self.folders(page)), "0 / 4 fichiers · 1,1 Mo · Annulé")
        self.assertEqual(self.folders(page).locator(":scope > button:visible").all_inner_texts(), ["Voir les fichiers", "Retirer de la liste"])
        self.assertEqual(page.locator("#views > .view:visible .sendfile[data-state=cancel]").count(), 4)
        self.throttle(page, -1)
        page.wait_for_timeout(500)
        self.assertEqual(len(posts), 1)                                                        # l'annulation n'a pas lancé le suivant
        self.wait_for(lambda: self.tree() == [])                                               # le serveur a effacé le .part
        self.wait_for(lambda: self.stored() == [])                                             # et le dossier créé pour lui
        self.folders(page).locator(":scope > button", has_text="Retirer de la liste").click()
        self.assertEqual(self.folders(page).count(), 0)

    def test_a_file_that_fails_does_not_stop_the_folder_and_can_be_tried_again_alone(self):
        page = self.upload_page()
        calls = []

        def flaky(route):
            if "name=b.mkv" in route.request.url:
                calls.append(1)
                if len(calls) == 1:
                    route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "écriture impossible : le disque du serveur est peut-être plein"}))
                    return
            route.continue_()
        page.route("**/api/inbox?name=*", flaky)
        self.pick_folder(page, self.make_folder("Série", {"a.mkv": b"a" * 2000, "b.mkv": b"b" * 2000, "c.mkv": b"c" * 2000}))
        self.confirm(page)
        self.folders(page, "error").wait_for()
        self.assertEqual(self.tree(), ["Série/a.mkv", "Série/c.mkv"])                           # le fichier en erreur n'a pas arrêté les autres
        folder = self.folders(page)
        self.assertEqual(self.line(folder), "2 / 3 fichiers · 5,9 Ko · 1 en erreur")
        self.assertEqual(folder.locator(":scope > button:visible").all_inner_texts(), ["Voir les fichiers", "Réessayer", "Retirer de la liste"])
        folder.locator(":scope > button[data-key^=files-]").click()
        failed = folder.locator(".sendfile[data-state=error]")
        self.assertEqual(failed.count(), 1)
        self.assertEqual(failed.locator("b").inner_text(), "b.mkv")
        self.assertIn("le disque du serveur est peut-être plein", failed.locator(".what > span").inner_text())
        failed.locator("button", has_text="Réessayer").click()                                  # lui seul repart
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Série/a.mkv", "Série/b.mkv", "Série/c.mkv"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.line(folder), "3 / 3 fichiers · 5,9 Ko · Envoyé")

    def test_the_failed_files_of_a_folder_are_retried_together_from_its_line(self):
        page = self.upload_page()
        mode = {"fail": True}
        page.route("**/api/inbox?name=*", lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "écriture impossible : le disque du serveur est peut-être plein"})) if mode["fail"] else route.continue_())
        self.pick_folder(page, self.make_folder("Série", {"a.mkv": b"a" * 2000, "b.mkv": b"b" * 2000}))
        self.confirm(page)
        self.folders(page, "error").wait_for()
        self.assertEqual(self.line(self.folders(page)), "0 / 2 fichiers · 3,9 Ko · 2 en erreur")
        mode["fail"] = False
        self.folders(page).locator(":scope > button", has_text="Réessayer").click()
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Série/a.mkv", "Série/b.mkv"])

    def test_names_with_accents_go_through_and_come_back_the_same(self):
        page = self.upload_page()
        root = self.make_folder("Été à Paris", {"Épisode 1 – é.mkv": b"e" * 2000, "Bonus ça/Ça va.mkv": b"c" * 2500})
        self.pick_folder(page, root)
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Été à Paris/Bonus ça/Ça va.mkv", "Été à Paris/Épisode 1 – é.mkv"])
        self.assertEqual(self.folders(page).locator(":scope > .what > b").inner_text(), "Été à Paris")
        rows = page.locator("#views > .view:visible .mine-file")
        self.wait_for(lambda: rows.count() == 2)
        row = rows.filter(has_text="Ça va.mkv")
        self.assertEqual(row.get_attribute("data-dir"), "Été à Paris/Bonus ça")
        self.assertIn("Été à Paris/Bonus ça", row.locator(".what > span").inner_text())
        admin, inbox = self.admin_inbox()
        listed = inbox.filter(has_text="Ça va.mkv")
        with admin.expect_download() as info:
            listed.locator("a", has_text="Télécharger").click()
        self.assertEqual(info.value.suggested_filename, "Ça va.mkv")
        self.assertEqual(Path(info.value.path()).read_bytes(), b"c" * 2500)

    def test_my_uploads_show_the_subfolder_and_deleting_passes_it_along(self):
        self.put("alice", "a.mkv", b"a" * 2048, mtime=1_700_100_000, dir="Film/Bonus")
        self.put("alice", "b.mkv", b"b" * 2048, mtime=1_700_000_000, dir="Film")
        self.put("alice", "c.mkv", b"c" * 2048, mtime=1_699_000_000)
        page = self.upload_page()
        deletes = []
        page.on("request", lambda r: deletes.append(r.url) if r.method == "DELETE" else None)
        rows = page.locator("#views > .view:visible .mine-file")
        self.assertEqual(rows.count(), 3)
        self.assertEqual([rows.nth(i).locator("b").inner_text() for i in range(3)], ["a.mkv", "b.mkv", "c.mkv"])
        self.assertEqual([rows.nth(i).get_attribute("data-dir") for i in range(3)], ["Film/Bonus", "Film", ""])
        self.assertTrue(rows.nth(0).locator(".what > span").inner_text().startswith("Film/Bonus · 2 Ko · "))
        self.assertTrue(rows.nth(2).locator(".what > span").inner_text().startswith("2 Ko · "))                  # à la racine : rien devant
        rows.nth(0).locator("button", has_text="Supprimer").click()
        self.assertIn("« Film/Bonus/a.mkv »", self.dialog(page).inner_text())
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: rows.count() == 2)
        self.assertTrue(deletes[0].endswith("/api/inbox/alice/a.mkv?dir=Film%2FBonus"), deletes)                  # `?dir=` encodé par encodeURIComponent
        self.assertFalse((self.box / "alice" / "Film" / "Bonus").exists())                                         # le dossier devenu vide a disparu
        rows.nth(0).locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: rows.count() == 1)
        self.assertFalse((self.box / "alice" / "Film").exists())                                                   # et celui qui l'enfermait
        self.assertEqual(self.tree(), ["c.mkv"])
        rows.nth(0).locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()
        page.locator("#views > .view:visible .mine .empty").wait_for()
        self.assertEqual(self.stored(), [])

    def test_the_administrator_downloads_and_deletes_a_file_of_a_subfolder(self):
        self.put("alice", "Épisode.mkv", b"dans un sous-dossier", mtime=1_700_100_000, dir="Série/Saison 1")
        self.put("alice", "Épisode.mkv", b"a la racine", mtime=1_700_000_000)                                       # même nom, ailleurs
        page, rows = self.admin_inbox()
        deletes = []
        page.on("request", lambda r: deletes.append(r.url) if r.method == "DELETE" else None)
        self.assertEqual(rows.count(), 2)
        deep = rows.nth(0)
        self.assertEqual(deep.get_attribute("data-dir"), "Série/Saison 1")
        self.assertTrue(deep.locator(".what > span").inner_text().startswith("alice · Série/Saison 1 · "), deep.locator(".what > span").inner_text())
        self.assertTrue(rows.nth(1).locator(".what > span").inner_text().startswith("alice · 11 octets · "))
        with page.expect_download() as info:
            deep.locator("a", has_text="Télécharger").click()
        self.assertEqual(info.value.suggested_filename, "Épisode.mkv")
        self.assertEqual(Path(info.value.path()).read_bytes(), b"dans un sous-dossier")                              # celui du sous-dossier, pas celui de la racine
        with page.expect_download() as info:
            rows.nth(1).locator("a", has_text="Télécharger").click()
        self.assertEqual(Path(info.value.path()).read_bytes(), b"a la racine")
        deep.locator("button", has_text="Supprimer").click()
        self.assertIn("« Série/Saison 1/Épisode.mkv »", self.dialog(page).inner_text())
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: rows.count() == 1)
        self.assertEqual(self.tree(), ["Épisode.mkv"])
        self.assertTrue(deletes[0].endswith("/api/inbox/alice/%C3%89pisode.mkv?dir=S%C3%A9rie%2FSaison%201"), deletes)
        self.assertFalse((self.box / "alice" / "Série").exists())                                                    # plus rien dans « Série » : le dossier a disparu
        self.assertEqual(rows.nth(0).get_attribute("data-dir"), "")

    DROP = """(spec) => {
        var reads = window.dropReads = { files: 0, folders: {} };                      // ce que le parcours a lu : des fichiers, et des paquets d'entrées par dossier
        function leaf(name, text, broken) { return { isFile: true, isDirectory: false, name: name, file: function (ok, bad) { reads.files++; setTimeout(function () { if (broken) bad(new Error('illisible')); else ok(new File([text], name)); }, 0); } }; }
        function folder(name, children, failAfter) {
            return { isFile: false, isDirectory: true, name: name, createReader: function () {
                var at = 0, calls = 0;                         // comme le navigateur : au plus 100 entrées par appel, puis un paquet vide ; `failAfter` : le nombre d'appels qui réussissent avant une erreur
                return { readEntries: function (ok, bad) {
                    reads.folders[name] = (reads.folders[name] || 0) + 1;
                    var batch = children.slice(at, at + 100); at += 100;
                    setTimeout(function () { if (failAfter !== undefined && calls++ >= failAfter) bad(new Error('lecture')); else ok(batch); }, 0);
                } };
            } };
        }
        function build(node) { return node.children ? folder(node.name, node.children.map(build), node.failAfter) : leaf(node.name, node.text, node.broken); }
        var specs = Array.isArray(spec) ? spec : [spec], el = document.querySelector('#views > .view.upload:not([style*="none"])');
        var dt = { types: ['Files'], files: specs.map(function (s) { return new File(s.children ? [] : [s.text], s.name); }),
                   items: specs.map(function (s) { var entry = s.children ? build(s) : { isFile: true, isDirectory: false, name: s.name }; return { webkitGetAsEntry: function () { return entry; } }; }) };
        var event = new Event('drop', { bubbles: true, cancelable: true });
        Object.defineProperty(event, 'dataTransfer', { value: dt });
        el.dispatchEvent(event);
    }"""

    def test_a_folder_dropped_on_the_page_is_read_to_the_end(self):
        page = self.upload_page()
        posts = self.posts(page)
        big = {"name": "Série", "children": [{"name": f"e{n:03d}.srt", "text": "sous-titre"} for n in range(125)] + [{"name": "Extras", "children": [{"name": f"x{n}.mkv", "text": "extra"} for n in range(5)]}]}
        page.evaluate(self.DROP, big)
        dialog = self.dialog(page)
        self.assertIn("130 fichiers", dialog.locator(".lead").inner_text())                  # au-delà des 100 premiers : le parcours a relu jusqu'au paquet vide
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        dialog.wait_for(state="detached")
        self.assertEqual(posts, [])
        page.evaluate(self.DROP, {"name": "Mini", "children": [{"name": "m1.srt", "text": "un"}, {"name": "Sous", "children": [{"name": "m2.srt", "text": "deux"}]}]})
        self.assertIn("Envoyer le dossier « Mini » : 2 fichiers", self.dialog(page).locator(".lead").inner_text())
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Mini/Sous/m2.srt", "Mini/m1.srt"])

    def test_several_folders_dropped_with_a_file_are_confirmed_one_after_the_other(self):
        page = self.upload_page()
        page.evaluate(self.DROP, [{"name": "Un", "children": [{"name": "a.srt", "text": "a"}]}, {"name": "Deux", "children": [{"name": "b.srt", "text": "b"}]}, {"name": "seul.srt", "text": "s"}])
        self.sends(page, "done").first.wait_for()                                             # le fichier isolé part tout de suite
        titles = []
        for _ in range(2):
            dialog = self.dialog(page)
            titles.append(dialog.locator(".lead").inner_text().split(" : ")[0])
            dialog.get_by_role("button", name="Envoyer", exact=True).click()
            page.wait_for_timeout(300)                                                        # (la fenêtre suivante prend aussitôt la place de celle-ci)
        self.assertEqual(sorted(titles), ["Envoyer le dossier « Deux »", "Envoyer le dossier « Un »"])
        self.wait_for(lambda: self.folders(page, "done").count() == 2)
        self.assertEqual(self.tree(), ["Deux/b.srt", "Un/a.srt", "seul.srt"])

    def test_a_browser_without_folder_support_has_no_folder_button(self):
        page = self.session("alice")
        page.context.add_init_script("delete HTMLInputElement.prototype.webkitdirectory")
        page.reload()
        page.wait_for_selector("#nav .link", state="attached")
        page.goto(self.base + "/#/upload")
        self.shown(page, "#/upload")
        page.wait_for_selector("#views > .view:visible .used .usedline")
        self.assertEqual(page.evaluate("'webkitdirectory' in document.createElement('input')"), False)
        self.assertEqual(page.locator("#views > .view:visible .drop .btn").all_inner_texts(), ["Choisir des fichiers"])
        self.assertEqual(page.locator("#views > .view:visible input[type=file]").count(), 1)
        self.assertEqual(page.locator("#toast").inner_text(), "")                              # et pas un mot d'erreur
        self.send(page, ("Suite.mkv", b"s" * 3000))                                            # les fichiers un à un marchent comme avant
        self.sends(page, "done").first.wait_for()

    def test_the_server_refusals_of_a_folder_file_are_shown_on_that_file(self):
        page = self.upload_page()
        page.evaluate("Hub.upload.retry = 200")
        refusals = {
            "a.mkv": (400, "segment de dossier vide, « . » ou « .. » refusé"),
            "b.mkv": (409, "trop de sous-dossiers dans ton dossier (1000 au plus)"),
            "c.mkv": (415, "type de fichier refusé : « .exe »"),
            "e.mkv": (409, "« Série » : un fichier ou un lien porte déjà ce nom"),
        }
        busy = []

        def refuse(route):
            name = route.request.url.split("name=")[1].split("&")[0]
            if name in refusals:
                status, message = refusals[name]
                route.fulfill(status=status, content_type="application/json", body=json.dumps({"error": message}))
            elif name == "d.mkv" and not busy:                                                  # 429 : déjà deux envois en cours, la file patiente
                busy.append(1)
                route.fulfill(status=429, content_type="application/json", body=json.dumps({"error": "trop d'envois en cours : réessaie dans un moment"}))
            else:
                route.continue_()
        page.route("**/api/inbox?name=*", refuse)
        self.pick_folder(page, self.make_folder("Série", {f"{c}.mkv": c.encode() * 1000 for c in "abcde"}))
        self.confirm(page)
        self.folders(page, "error").wait_for()
        folder = self.folders(page)
        self.assertEqual(self.line(folder), "1 / 5 fichiers · 4,9 Ko · 4 en erreur")
        self.assertEqual(self.tree(), ["Série/d.mkv"])                                          # le 429 a patienté puis le fichier est passé
        self.assertEqual(busy, [1])
        folder.locator(":scope > button[data-key^=files-]").click()
        said = {folder.locator(".sendfile").nth(i).locator("b").inner_text(): folder.locator(".sendfile").nth(i).locator(".what > span").inner_text() for i in range(5)}
        self.assertEqual(said["a.mkv"], "segment de dossier vide, « . » ou « .. » refusé")
        self.assertEqual(said["b.mkv"], "trop de sous-dossiers dans ton dossier (1000 au plus)")
        self.assertEqual(said["c.mkv"], "type de fichier refusé : « .exe »")
        self.assertEqual(said["e.mkv"], "« Série » : un fichier ou un lien porte déjà ce nom")
        self.assertTrue(said["d.mkv"].startswith("Envoyé"))

    def test_a_file_in_the_place_of_a_subfolder_is_a_409_on_the_real_server(self):
        page = self.upload_page()
        self.send(page, ("Dossier.zip", b"z" * 2000))                                          # un fichier occupe déjà le nom
        self.sends(page, "done").first.wait_for()
        self.pick_folder(page, self.make_folder("Dossier.zip", {"a.mkv": b"a" * 2000}))
        self.confirm(page)
        self.folders(page, "error").wait_for()
        self.folders(page).locator(":scope > button[data-key^=files-]").click()
        self.assertIn("« Dossier.zip » : un fichier ou un lien porte déjà ce nom", self.folders(page).locator(".sendfile .what > span").inner_text())
        self.assertEqual(self.tree(), ["Dossier.zip"])

    def test_leaving_the_page_during_a_folder_asks_first_and_cancels_the_rest(self):
        page = self.upload_page()
        posts = self.posts(page)
        self.throttle(page, 150_000)
        self.pick_folder(page, self.make_folder("Album", {f"{n}.flac": b"x" * 300_000 for n in range(1, 4)}))
        self.confirm(page)
        self.sending(page)
        page.locator("#nav .brand").click()
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Quitter la page ?")
        dialog.get_by_role("button", name="Annuler", exact=True).click()
        dialog.wait_for(state="detached")
        self.assertEqual(self.folders(page, "send").count(), 1)                                 # le dossier continue
        page.locator("#nav .brand").click()
        page.locator(".menu .btn.danger", has_text="Quitter et annuler").click()
        self.shown(page, "#/")
        self.throttle(page, -1)
        self.wait_for(lambda: self.tree() == [])
        page.wait_for_timeout(300)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.tree(), [])

    def test_the_folder_dialog_and_the_folder_line_are_in_english(self):
        page = self.upload_page(lang="en")
        self.assertEqual(page.locator("#views > .view:visible .drop .btn").all_inner_texts(), ["Choose files", "Send a folder"])
        self.pick_folder(page, self.make_folder("Film", {"a.mkv": b"a" * 3000, "notes.txt": b"n", "Thumbs.db": b"t"}))
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Send this folder?")
        self.assertEqual(dialog.locator(".lead").inner_text(), "Send the folder “Film”: 1 file (2.9 KB), 2 skipped (unsupported types, hidden files).")
        dialog.locator("button", has_text="Show the skipped files").click()
        self.assertEqual(sorted(dialog.locator(".skipped li").all_inner_texts()), ["Film/Thumbs.db — hidden file", "Film/notes.txt — unsupported type"])
        self.confirm(page, "Upload")
        self.folders(page, "done").wait_for()
        self.assertEqual(self.line(self.folders(page)), "1 / 1 file · 2.9 KB · Uploaded")
        self.assertEqual(self.folders(page).locator(":scope > button:visible").all_inner_texts(), ["Show the files", "Remove from the list"])
        self.pick_folder(page, self.make_folder("Papiers", {"notes.txt": b"n"}))
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Nothing to send")
        self.assertIn("None of the files in the folder “Papiers” can be sent (unsupported types): there is nothing to send.", dialog.inner_text())
        self.assertEqual(page.evaluate("Hub.missing"), [])

    def test_the_server_refusals_of_a_folder_file_are_in_english(self):
        page = self.upload_page(lang="en")
        refusals = {
            "a.mkv": (400, "segment de dossier vide, « . » ou « .. » refusé"),
            "b.mkv": (400, "dossiers trop profonds : 4 niveaux au plus"),
            "c.mkv": (409, "trop de sous-dossiers dans ton dossier (1000 au plus)"),
            "d.mkv": (409, "« Film » : un fichier ou un lien porte déjà ce nom"),
            "e.mkv": (400, "« \\ » n'est pas un séparateur de dossiers : sépare-les par « / » (?dir=…)"),
            "f.mkv": (409, "« Film » existe déjà sous un autre nom ou n'est pas un dossier ordinaire : choisis un autre nom"),
            "g.mkv": (503, "la boîte de réception du serveur est inutilisable : préviens l'administrateur"),
        }

        def refuse(route):
            status, message = refusals[route.request.url.split("name=")[1].split("&")[0]]
            route.fulfill(status=status, content_type="application/json", body=json.dumps({"error": message}))
        page.route("**/api/inbox?name=*", refuse)
        self.pick_folder(page, self.make_folder("Film", {f"{c}.mkv": c.encode() * 100 for c in "abcdefg"}))
        self.confirm(page, "Upload")
        self.folders(page, "error").wait_for()
        folder = self.folders(page)
        self.assertEqual(self.line(folder), "0 / 7 files · 700 bytes · 7 failed")
        folder.locator(":scope > button[data-key^=files-]").click()
        said = {folder.locator(".sendfile").nth(i).locator("b").inner_text(): folder.locator(".sendfile").nth(i).locator(".what > span").inner_text() for i in range(7)}
        self.assertEqual(said, {
            "a.mkv": "empty folder segment, “.” or “..” refused",
            "b.mkv": "folders nested too deep: 4 levels at most",
            "c.mkv": "too many subfolders in your folder (1000 at most)",
            "d.mkv": "“Film”: a file or a link already has this name",
            "e.mkv": "“\\” isn’t a folder separator: separate them with “/” (?dir=…)",
            "f.mkv": "“Film” already exists under another name or isn’t an ordinary folder: pick another name",
            "g.mkv": "the server’s inbox is unusable: tell the administrator",
        })
        self.assertEqual(page.evaluate("Hub.missing"), [])

    def test_a_folder_name_with_html_and_url_characters_is_text_and_survives_the_links(self):
        page = self.upload_page()
        nasty = "a &amp; b #1 %41 'q' é"                                                            # (Windows refuse « < » dans un nom de dossier : l'entité fait le même travail)
        self.pick_folder(page, self.make_folder(nasty, {"a.mkv": b"a" * 2000, "x&y/b.mkv": b"b" * 2000}))
        dialog = self.dialog(page)
        self.assertIn(nasty, dialog.locator(".lead").inner_text())
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.folders(page).locator(":scope > .what > b").inner_text(), nasty)       # du texte, jamais du HTML
        self.assertEqual(self.tree(), sorted([nasty + "/a.mkv", nasty + "/x&y/b.mkv"]))              # `dir` bien encodé dans l'adresse
        rows = page.locator("#views > .view:visible .mine-file")
        self.wait_for(lambda: rows.count() == 2)
        rows.filter(has_text="b.mkv").locator("button", has_text="Supprimer").click()
        page.locator(".menu .btn.danger").click()                                                    # et dans celle de la suppression
        self.wait_for(lambda: self.tree() == [nasty + "/a.mkv"])


    def test_two_folders_with_the_same_name_are_two_lines_and_cancelling_one_leaves_the_other(self):
        page = self.upload_page()
        self.throttle(page, 150_000)
        self.pick_folder(page, self.make_folder("Film", {"a.mkv": b"a" * 300_000, "b.mkv": b"b" * 300_000}))
        self.confirm(page)
        self.sending(page)
        self.pick_folder(page, self.make_folder("Film", {"c.mkv": b"c" * 2000}))                      # le même nom, pendant que le premier part
        self.confirm(page)
        folders = self.folders(page)
        self.assertEqual(folders.count(), 2)
        first, second = folders.nth(0), folders.nth(1)
        self.assertEqual(first.get_attribute("data-folder"), second.get_attribute("data-folder"))
        self.assertNotEqual(first.get_attribute("data-send"), second.get_attribute("data-send"))      # deux lignes distinctes
        first.locator(":scope > button", has_text="Annuler").click()
        self.folders(page, "cancel").wait_for()
        self.throttle(page, -1)
        self.folders(page, "done").wait_for()                                                          # l'autre dossier part quand même
        self.assertEqual(self.line(self.folders(page, "done")), "1 / 1 fichier · 2 Ko · Envoyé")
        self.assertEqual(self.line(self.folders(page, "cancel")), "0 / 2 fichiers · 585,9 Ko · Annulé")
        self.wait_for(lambda: self.tree() == ["Film/c.mkv"])

    def test_the_same_folder_sent_twice_merges_and_numbers_the_files_of_the_same_name(self):
        page = self.upload_page()
        for sent in range(2):
            self.pick_folder(page, self.make_folder("Film", {"a.mkv": b"a" * 2000, "Bonus/b.mkv": b"b" * 2000}))
            self.confirm(page)
            self.wait_for(lambda: self.folders(page, "done").count() == sent + 1)
        self.assertEqual(self.folders(page).count(), 2)
        self.assertEqual(self.tree(), ["Film/Bonus/b (2).mkv", "Film/Bonus/b.mkv", "Film/a (2).mkv", "Film/a.mkv"])
        self.assertEqual(self.stored(), ["Film"])                                                      # un seul dossier, jamais « Film (2) »
        folder = self.folders(page).nth(1)
        folder.locator(":scope > button[data-key^=files-]").click()
        self.assertEqual(folder.locator(".sendfile b").all_inner_texts(), ["a (2).mkv", "Bonus/b (2).mkv"])      # la ligne dit le nom que le serveur a retenu
        self.assertIn("enregistré sous ce nom (tu l’avais appelé « a.mkv »)", folder.locator(".sendfile .what > span").first.inner_text())

    def test_cancelling_a_folder_while_a_file_waits_for_a_new_try_sends_nothing_more(self):
        page = self.upload_page()
        page.evaluate("Hub.upload.retry = 60000")
        calls = []

        def busy(route):
            calls.append(1)
            route.fulfill(status=429, content_type="application/json", body=json.dumps({"error": "trop d'envois en cours : réessaie dans un moment"}))
        page.route("**/api/inbox?name=*", busy)
        self.pick_folder(page, self.make_folder("Série", {"a.mkv": b"a" * 2000, "b.mkv": b"b" * 2000}))
        self.confirm(page)
        folder = self.folders(page, "wait")
        folder.wait_for()
        folder.locator(":scope > button[data-key^=files-]").click()
        folder.locator(".sendfile[data-state=wait]", has_text="occupé").wait_for()                     # le fichier attend son nouvel essai
        folder.locator(":scope > button", has_text="Annuler").click()
        self.folders(page, "cancel").wait_for()
        page.wait_for_timeout(300)
        self.assertEqual(len(calls), 1)                                                                # ni nouvel essai, ni fichier suivant
        self.assertEqual(self.line(self.folders(page)), "0 / 2 fichiers · 3,9 Ko · Annulé")
        self.assertEqual(page.locator("#views > .view:visible .sendfile[data-state=cancel]").count(), 2)
        self.assertEqual(self.tree(), [])

    def test_a_server_that_gives_no_extensions_leaves_the_refusals_to_the_server_file_by_file(self):
        page = self.session("alice")

        def old_server(route):                                                                         # un serveur d'avant `extensions` : la liste ne les donne pas
            reply = route.fetch()
            body = reply.json()
            body.pop("extensions", None)
            route.fulfill(response=reply, json=body)
        page.route(re.compile(r"/api/inbox\?mine=1"), old_server)
        page.goto(self.base + "/#/upload")
        self.shown(page, "#/upload")
        page.wait_for_selector("#views > .view:visible .used .usedline")
        self.pick_folder(page, self.make_folder("Papiers", {"a.mkv": b"a" * 2000, "notes.txt": b"n" * 100, ".cache/x.mkv": b"x"}))
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator(".lead").inner_text(), "Envoyer le dossier « Papiers » : 2 fichiers (2,1 Ko), 1 ignoré (fichiers cachés).")      # les types ne sont pas jugés, le reste l'est
        self.confirm(page)
        self.folders(page, "error").wait_for()
        self.assertEqual(self.line(self.folders(page)), "1 / 2 fichiers · 2,1 Ko · 1 en erreur")
        self.folders(page).locator(":scope > button[data-key^=files-]").click()
        failed = self.folders(page).locator(".sendfile[data-state=error]")
        self.assertEqual(failed.locator("b").inner_text(), "notes.txt")
        self.assertIn("type de fichier refusé", failed.locator(".what > span").inner_text())          # le message du serveur, sur ce fichier
        self.assertEqual(self.tree(), ["Papiers/a.mkv"])

    def test_a_dropped_file_the_browser_cannot_read_is_left_out_and_said(self):
        page = self.upload_page()
        page.evaluate(self.DROP, {"name": "Série", "children": [{"name": "a.srt", "text": "a"}, {"name": "b.srt", "text": "b", "broken": True}]})
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator(".lead").inner_text(), "Envoyer le dossier « Série » : 1 fichier (1 octets), 1 ignoré (fichiers illisibles).")
        dialog.locator("button", has_text="Voir les fichiers ignorés").click()
        self.assertEqual(dialog.locator(".skipped li").all_inner_texts(), ["Série/b.srt — illisible"])
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Série/a.srt"])

    def test_a_dropped_folder_whose_reading_fails_midway_does_not_freeze_the_page(self):
        page = self.upload_page()
        posts = self.posts(page)
        page.evaluate(self.DROP, [{"name": "Gros", "failAfter": 1, "children": [{"name": f"e{n:03d}.srt", "text": "s"} for n in range(125)]}, {"name": "Autre", "children": [{"name": "o.srt", "text": "o"}]}])
        first = self.dialog(page)
        self.assertEqual(first.locator("h3").inner_text(), "Envoyer ce dossier ?")                     # la fenêtre s'ouvre malgré l'erreur de lecture
        leads = first.locator(".lead").all_inner_texts()
        self.assertIn("100 fichiers", leads[0])                                                         # ce qui a été lu
        self.assertIn("La lecture du dossier « Gros » a échoué : il peut manquer des fichiers.", leads)  # et le dossier tronqué ne passe pas pour complet
        first.locator("button", has_text="Voir les fichiers ignorés").click()
        self.assertEqual(first.locator(".skipped li").all_inner_texts(), ["Gros — lecture du dossier échouée"])
        first.get_by_role("button", name="Annuler", exact=True).click()
        page.locator(".menu[role=dialog] .lead", has_text="« Autre »").wait_for()                      # et les dossiers suivants passent
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Autre/o.srt"])
        self.assertEqual(len(posts), 1)

    def test_a_dropped_folder_whose_reading_fails_at_once_says_reading_was_impossible_and_a_failed_subfolder_is_named(self):
        page = self.upload_page()
        posts = self.posts(page)
        page.evaluate(self.DROP, {"name": "Vide", "failAfter": 0, "children": [{"name": "a.srt", "text": "a"}]})
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Dossier illisible")                       # pas « Rien à envoyer » : le dossier n'est pas vide, on n'a pas pu le lire
        self.assertEqual(dialog.locator(".lead").all_inner_texts(), ["La lecture du dossier « Vide » a échoué : aucun fichier n’a pu être lu."])
        dialog.get_by_role("button", name="Compris").click()
        dialog.wait_for(state="detached")
        page.evaluate(self.DROP, {"name": "Série", "children": [{"name": "a.srt", "text": "a"}, {"name": "Extras", "failAfter": 0, "children": [{"name": "x.srt", "text": "x"}]}]})
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator("h3").inner_text(), "Envoyer ce dossier ?")                      # le reste se lit : on peut l'envoyer, en connaissance de cause
        self.assertIn("La lecture du dossier « Série/Extras » a échoué : il peut manquer des fichiers.", dialog.locator(".lead").all_inner_texts())
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Série/a.srt"])
        self.assertEqual(len(posts), 1)

    def test_a_folder_confirmation_does_not_open_once_the_page_has_been_left(self):
        page = self.session("alice")
        held, first = [], []

        def late(route):                                                                               # un serveur d'avant `extensions` : chaque dépôt relit la liste, ici une réponse que l'on retient
            if not first:
                first.append(1)
                reply = route.fetch()
                body = reply.json()
                body.pop("extensions", None)
                route.fulfill(response=reply, json=body)
            else:
                held.append(route)
        page.route(re.compile(r"/api/inbox\?mine=1"), late)
        page.goto(self.base + "/#/upload")
        self.shown(page, "#/upload")
        page.wait_for_selector("#views > .view:visible .used .usedline")
        page.evaluate(self.DROP, {"name": "Série", "children": [{"name": "a.srt", "text": "a"}]})
        for _ in range(50):                                                                             # la lecture du dossier est finie, la liste des types n'est pas arrivée
            if held:
                break
            page.wait_for_timeout(100)                                                                  # (et non time.sleep : le gestionnaire de route ne tourne que pendant les appels à la page)
        self.assertEqual(len(held), 1)
        page.locator("#nav .brand").click()
        self.shown(page, "#/")                                                                          # la page est quittée avant la réponse
        held[0].continue_()
        page.wait_for_timeout(600)
        self.assertEqual(page.locator(".menu[role=dialog], .menu.sendfolder").count(), 0)               # et aucune confirmation ne s'ouvre sur l'accueil

    def test_a_dropped_folder_does_not_enter_hidden_or_too_deep_folders(self):
        page = self.upload_page()
        posts = self.posts(page)
        junk = [{"name": f"m{n}.srt", "text": "m"} for n in range(3000)]
        page.evaluate(self.DROP, {"name": "Série", "children": [
            {"name": "a.srt", "text": "a"},
            {"name": "node_modules", "children": junk}, {"name": ".git", "children": junk},                             # des milliers d'entrées qu'on ne lit pas
            {"name": "N1", "children": [{"name": "N2", "children": [{"name": "N3", "children": [                         # « Série » compte pour un niveau : N3 est le quatrième, le dernier accepté
                {"name": "ok.srt", "text": "o"}, {"name": "N4", "children": [{"name": "N5", "children": junk}, {"name": "profond.srt", "text": "p"}]}]}]}]}]})
        dialog = self.dialog(page)
        self.assertEqual(dialog.locator(".lead").inner_text(), "Envoyer le dossier « Série » : 2 fichiers (2 octets), 3 ignorés (fichiers cachés, dossiers trop profonds).")
        dialog.locator("button", has_text="Voir les fichiers ignorés").click()
        self.assertEqual(sorted(dialog.locator(".skipped li").all_inner_texts()), sorted(["Série/.git — fichier caché", "Série/node_modules — fichier caché", "Série/N1/N2/N3/N4 — trop profond (4 niveaux au plus)"]))
        reads = page.evaluate("window.dropReads")
        self.assertEqual(reads["files"], 2)                                                                              # a.srt et ok.srt : rien d'autre n'a été ouvert
        self.assertEqual(sorted(reads["folders"]), ["N1", "N2", "N3", "Série"])                                           # ni node_modules, ni .git, ni N4 (le premier trop profond), ni N5
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Série/N1/N2/N3/ok.srt", "Série/a.srt"])
        self.assertEqual(len(posts), 2)

    def test_the_files_of_a_folder_are_sorted_in_natural_order_with_one_collator(self):
        page = self.upload_page()
        page.evaluate("window.localeCompares = 0; var compare = String.prototype.localeCompare; String.prototype.localeCompare = function () { if (arguments[2] && arguments[2].numeric) window.localeCompares++; return compare.apply(this, arguments); };")
        self.pick_folder(page, self.make_folder("Épisodes", {f"f{n}.srt": b"s" for n in (10, 2, 1, 11, 3, 20, 9)}))
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.folders(page).locator(":scope > button[data-key^=files-]").click()
        files = self.folders(page).locator(".sendfile b")
        self.assertEqual(files.all_inner_texts(), ["f1.srt", "f2.srt", "f3.srt", "f9.srt", "f10.srt", "f11.srt", "f20.srt"])        # « 2 » avant « 10 »
        self.assertEqual(page.evaluate("window.localeCompares"), 0)                                                              # un collateur créé une fois, pas un `localeCompare` (et son collateur) par comparaison

    def test_the_name_of_a_dropped_folder_is_not_judged_hidden(self):
        page = self.upload_page()
        page.evaluate(self.DROP, {"name": ".Archive", "children": [{"name": "a.srt", "text": "a"}]})
        self.assertEqual(self.dialog(page).locator(".lead").inner_text(), "Envoyer le dossier « .Archive » : 1 fichier (1 octets).")      # le dossier désigné par la personne n'est pas écarté
        self.confirm(page)
        self.folders(page, "done").wait_for()
        self.assertEqual(self.tree(), ["Archive/a.srt"])                                                                                 # (le serveur range « .Archive » sous un nom sans point)

    def test_a_backslash_in_a_dropped_folder_name_is_refused_on_that_file_and_in_a_file_name_it_keeps_the_last_part(self):
        page = self.upload_page()
        page.evaluate(self.DROP, {"name": "Film", "children": [{"name": "ok.srt", "text": "ok"}, {"name": "x\\y", "children": [{"name": "a.srt", "text": "a"}]}, {"name": "b\\c.srt", "text": "b"}]})
        self.confirm(page)
        self.folders(page, "error").wait_for()
        self.assertEqual(self.line(self.folders(page)), "2 / 3 fichiers · 4 octets · 1 en erreur")
        self.folders(page).locator(":scope > button[data-key^=files-]").click()
        failed = self.folders(page).locator(".sendfile[data-state=error]")
        self.assertEqual(failed.locator("b").inner_text(), "x\\y/a.srt")                              # le dossier au `\` : refusé, sur ce fichier seul
        self.assertIn("pas un séparateur de dossiers", failed.locator(".what > span").inner_text())
        self.assertEqual(self.tree(), ["Film/c.srt", "Film/ok.srt"])                                    # le nom à `\` garde sa dernière partie, comme le serveur le dit
        renamed = self.folders(page).locator(".sendfile[data-state=done]", has_text="c.srt")
        self.assertIn("tu l’avais appelé « b\\c.srt »", renamed.locator(".what > span").inner_text())

    def test_the_administrator_sees_the_subfolders_of_a_deleted_account_and_reaches_them(self):
        from hub import accounts
        lea = self.member("Léa")
        self.put("Léa", "Épisode.mkv", b"profond", mtime=1_700_000_000, dir="Été/Saison 1")
        self.put("Léa", "Bonus.mkv", b"bonus", mtime=1_699_000_000, dir="Été")
        accounts.delete(self.hub.db, lea["id"])
        page, rows = self.admin_inbox()
        self.assertEqual(rows.count(), 2)
        deep = rows.nth(0)
        self.assertEqual(deep.get_attribute("data-dir"), "Été/Saison 1")
        self.assertTrue(deep.locator(".what > span").inner_text().startswith("Léa (compte supprimé) · Été/Saison 1 · "), deep.locator(".what > span").inner_text())
        with page.expect_download() as info:
            deep.locator("a", has_text="Télécharger").click()
        self.assertEqual(Path(info.value.path()).read_bytes(), b"profond")
        deep.locator("button", has_text="Supprimer").click()
        self.assertIn("« Été/Saison 1/Épisode.mkv », envoyé par Léa", self.dialog(page).inner_text())
        page.locator(".menu .btn.danger").click()
        self.wait_for(lambda: rows.count() == 1)
        self.assertEqual(self.tree("Léa"), ["Été/Bonus.mkv"])
        self.assertFalse((self.box / "Léa" / "Été" / "Saison 1").exists())                              # le sous-dossier vidé a disparu, pas celui qui garde un fichier


if __name__ == "__main__":
    unittest.main()
