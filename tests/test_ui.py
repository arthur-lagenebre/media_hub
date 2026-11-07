"""L'interface dans un vrai navigateur (Chromium via Playwright) : navigation à la télécommande (flèches, Entrée, Retour), lecture, reprise, lecteurs.

Sauté si Playwright ou ffmpeg manquent. Ce que ces tests ne peuvent pas garantir : la lecture sur TON téléviseur (codecs, HDR, son) — à essayer sur place.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
import urllib.request
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

    def test_filter_letter_menu_and_back_closes_menu_only(self):
        self.open("#/l/films")
        self.view(".grid .card").first.wait_for()
        total = self.view(".grid .card").count()
        self.view("input[type=search]").fill("dceu")
        self.page.wait_for_timeout(700)
        self.assertEqual(self.view(".grid .card.saga").count(), 1)          # les films sont regroupés en sagas : une seule vignette
        self.view("input[type=search]").fill("")
        self.page.wait_for_timeout(700)
        self.assertEqual(self.view(".grid .card").count(), total)
        self.view(".tools .btn", ).first.click()                    # « Lettre »
        self.page.wait_for_selector(".veil .opt")
        url = self.page.url
        self.press("Escape", wait=400)
        self.assertEqual(self.page.locator(".veil").count(), 0)
        self.assertEqual(self.page.url, url)
        self.view(".tools .btn").first.click()
        letter = self.page.locator(".veil .opt .lbl").nth(1).inner_text()
        self.page.locator(".veil .opt").nth(1).click()
        self.page.wait_for_timeout(600)
        titles = self.view(".grid .card").evaluate_all("els => els.map(e => e.title)")
        self.assertTrue(titles and all(t.upper().startswith(letter) for t in titles), (letter, titles))
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
        entry = self.entries("livres", "Naruto")[0]["id"]
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader .page') && document.querySelector('.reader .page').complete && document.querySelector('.reader .page').naturalWidth > 0")
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 1 / 3")
        self.press("ArrowRight", wait=300)
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 2 / 3")
        self.press("ArrowLeft", wait=300)
        self.press("ArrowLeft", wait=300)
        self.assertEqual(self.page.evaluate("document.querySelector('.reader .pg').textContent"), "Page 1 / 3")
        self.press("ArrowRight", wait=300)
        self.page.wait_for_timeout(2000)
        self.assertEqual(self.api("/api/book/" + entry)["position"], 1)
        self.open("#/read/" + entry)
        self.page.wait_for_function("document.querySelector('.reader .pg').textContent === 'Page 2 / 3'")      # reprend où l'on s'était arrêté
        self.press("ArrowRight", wait=300)
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
                self.assertEqual(page.locator("#nav .link").count(), 5)
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
            alice = self.api(base, "/api/users", {"name": "alice", "password": "motdepasse1"}, token)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                admin = browser.new_page()
                self.sign_in(admin, base, "admin", "secret")
                admin.goto(base + "/#/settings?tab=comptes")
                admin.locator(f'[data-key="rights-{alice["id"]}"]').click()
                admin.locator(".rights").wait_for()
                self.assertFalse(admin.evaluate("(() => { var m = document.querySelector('.menu.rightsbox'); return m.scrollHeight > m.clientHeight; })()"))       # la fenêtre tient sans défiler
                admin.locator('[data-key="r-download-films"]').click()                    # films : voir sans télécharger
                admin.locator('[data-key="r-view-livres"]').click()                       # livres : fermés
                self.assertTrue(admin.locator('[data-key="r-download-livres"]').is_disabled())
                self.assertTrue(admin.locator('[data-key="r-play-livres"]').is_disabled())
                admin.locator('[data-key="r-play-series"]').click()                       # séries : se voient et se téléchargent, ne se lisent pas
                admin.locator(".menu .btn", has_text="Enregistrer").click()
                admin.locator(f'[data-user="{alice["id"]}"] [data-rights]', has_text="Consulter : Films").wait_for()
                self.assertIn("Télécharger : Séries", admin.locator(f'[data-rights="{alice["id"]}"]').inner_text())
                self.assertIn("Lecture : Films, Animations, Musique", admin.locator(f'[data-rights="{alice["id"]}"]').inner_text())
                self.assertEqual(admin.locator('[data-user="default"] [data-key^="rights-"]').count(), 0)       # un administrateur n'a pas de droits à régler
                saved = {u["name"]: u["rights"] for u in self.api(base, "/api/users", token=token)["users"]}["alice"]
                self.assertEqual((saved["films"], saved["series"], saved["livres"]), ({"view": True, "play": True, "download": False}, {"view": True, "play": False, "download": True},
                                                                                  {"view": False, "play": False, "download": False}))
                page = browser.new_page()
                self.sign_in(page, base, "alice", "motdepasse1")
                self.assertEqual(page.locator("#nav .link").count(), 4)                   # les livres ont disparu
                state = self.api(base, "/api/token", {"user": "alice", "password": "motdepasse1"})["token"]
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
        self.assertNotIn("t=0", self.page.url)
        self.api("/api/item/" + self.item("films", "Inception")["id"] + "/seen", {"done": True})
        self.card("films", "Inception").locator(".qbtn").click()
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
        self.assertEqual(self.view(".count").inner_text(), f"{total} artiste" + ("s" if total > 1 else ""))      # le décompte parle d'artistes, pas d'albums

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
        for lib, colour in (("films", "rgb(242, 180, 65)"), ("series", "rgb(76, 195, 176)"), ("animations", "rgb(255, 122, 168)"), ("musique", "rgb(255, 138, 92)"), ("livres", "rgb(155, 209, 107)")):
            card = self.view(f".shelf.latest .card[data-lib={lib}]").first
            card.wait_for()
            self.assertEqual(card.locator(".s .dot").evaluate("el => getComputedStyle(el).backgroundColor"), colour, lib)
            self.assertNotRegex(card.locator(".s").inner_text(), r"^(Film|Série|Animation|Album|Livre)\b", lib)               # le point dit la bibliothèque : pas de mot en plus

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
        for lib in ("films", "series", "animations"):                                                          # de quoi remplir un rayon dans chacune, au-delà des nouveautés
            items = [dict(r) for r in db.query("SELECT * FROM items WHERE lib=?", (lib,))]
            entries = [dict(r) for r in db.query("SELECT * FROM entries WHERE lib=?", (lib,))]
            old = [{**items[0], "id": f"vieux{lib}{n:03d}", "title": f"Vieux {lib} {n:03d}", "sort": f"vieux {lib} {n:03d}", "grp": "", "added": 100 + n, "meta": "{}"} for n in range(100)]
            files = [{**entries[0], "id": f"vieuxf{lib}{n:03d}", "item_id": f"vieux{lib}{n:03d}", "rel": f"vieux{lib}{n:03d}/film.mkv"} for n in range(100)]
            db.replace_library(lib, items + old, entries + files)
            self.addCleanup(db.replace_library, lib, items, entries)
        day = mock.patch("hub.server.app.today", return_value="2026-11-04")          # le serveur tourne dans ce processus : l'ordre du jour ne change pas pendant le test
        day.start()
        self.addCleanup(day.stop)
        self.addCleanup(lambda: self.api("/api/me/settings", {"home_latest": "show", "home_unseen": "show", "home_unseen_count": "12"}))

    def libraries(self):
        return self.api("/api/home")["libraries"]

    def offered(self, lib="films"):
        return [u["id"] for l in self.libraries() if l["id"] == lib for u in l["unseen"]]

    def test_each_video_library_has_its_own_shelf_after_the_new_ones(self):
        self.open()
        self.view(".shelf.unseen .card").first.wait_for()
        wanted = [l["label"] for l in self.libraries() if "unseen" in l and l["unseen"]]
        self.assertGreater(len(wanted), 1)                                                                  # films, séries, animations : pas un seul rayon pour tout
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
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse1", "role": "user"}), headers={"Content-Type": "application/json"})
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse1", headers=form)
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
        self.assertEqual(self.tabs(), ["Gestion", "Préférences", "Comptes"])
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Gestion")
        self.assertEqual(self.titles(), ["Bibliothèques", "Sagas", "Entretien", "Tâches", "Serveur"])

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
        self.assertEqual(panels.nth(2).locator(".line .what b").count(), 5)

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
        self.press("ArrowRight", "Enter", wait=300)
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
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse1", "role": "user"}), headers={"Content-Type": "application/json"})
                context.request.post(base + "/api/logout")
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse1", headers=form)
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
                context.request.post(base + "/api/users", data=json.dumps({"name": "lea", "password": "motdepasse1", "role": "user"}), headers={"Content-Type": "application/json"})
                context.request.post(base + "/api/logout")
                context.clear_cookies()
                context.request.post(base + "/api/login", data="user=lea&password=motdepasse1", headers=form)
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


if __name__ == "__main__":
    unittest.main()
