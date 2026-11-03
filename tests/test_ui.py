"""L'interface dans un vrai navigateur (Chromium via Playwright) : navigation à la télécommande (flèches, Entrée, Retour), lecture, reprise, lecteurs.

Sauté si Playwright ou ffmpeg manquent. Ce que ces tests ne peuvent pas garantir : la lecture sur TON téléviseur (codecs, HDR, son) — à essayer sur place.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from tests import fixtures
from tests.test_server import FakeTmdb, Running

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


@unittest.skipUnless(sync_playwright and fixtures.FFMPEG, "Playwright ou ffmpeg absent")
class UiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        import pathlib
        root = pathlib.Path(cls.tmp)
        fixtures.build_library(root / "srv")
        make_media(root)
        cls.run_ = Running.__new__(Running)           # Running reconstruit la bibliothèque : on la prépare nous-mêmes
        cls.run_.root = root
        cls.start(root)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        cls.base = f"http://127.0.0.1:{cls.run_.port}"

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
        self.assertEqual(self.view(".shelf h2 a").count(), 5)
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

    def set_track_artists(self, album, album_artist, artists):
        """Donne aux pistes de l'album les artistes `artists` (dans l'ordre) et à l'album l'artiste `album_artist` ; tout est remis après."""
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
        meta = json.loads(row["meta"])
        meta["artist"] = album_artist
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

    def test_admin_runs_an_update(self):
        self.open("#/admin")
        self.view(".panel").first.wait_for()
        self.view("button", has_text="Tout mettre à jour").click()
        self.view(".tabs .tab", has_text="Tâches").click()
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

    def test_the_films_are_always_grouped_in_sagas_and_there_is_no_button_to_undo_it(self):
        self.open("#/l/films")
        self.view(".grid .card.saga").first.wait_for()
        self.assertEqual(self.view(".tools .btn", has_text="Sagas").count(), 0)

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
        dialog = self.page.locator(".menu.form", has_text="Supprimer les fichiers parasites ?")
        dialog.locator("button.danger", has_text="Supprimer définitivement").wait_for()
        dialog.locator("button", has_text="Annuler").click()
        self.page.wait_for_timeout(300)
        self.assertFalse([j for j in self.api("/api/jobs")["jobs"] if "Nettoyage" in j["label"] and "simulation" not in j["label"]])          # annuler ne lance rien

    def test_the_dialog_can_count_again_without_closing(self):
        self.open("#/admin")
        self.view("[data-key=junk-del]").click()
        dialog = self.page.locator(".menu.form", has_text="Supprimer les fichiers parasites ?")
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
        self.addCleanup(lambda: self.api("/api/item/" + item["id"] + "/poster/delete", {}))

    def send(self, payload):
        with self.page.expect_file_chooser() as chooser:
            self.view(".actions .btn", has_text="Changer l’affiche").click()
        chooser.value.set_files(payload)

    CHOSEN = b"\xff\xd8\xff\xe0affiche choisie"

    def chosen(self):
        return {"name": "affiche.jpg", "mimeType": "image/jpeg", "buffer": self.CHOSEN}

    def test_the_poster_is_replaced_and_the_original_comes_back(self):
        item = self.item("films", "Inception")
        self.reset_later(item)
        original = self.served(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.assertEqual(self.view(".actions .btn", has_text="Affiche d’origine").count(), 0)
        self.send(self.chosen())
        self.until(lambda: self.api("/api/item/" + item["id"]).get("poster_custom") is True)
        self.assertEqual(self.served(item), self.CHOSEN)
        self.assertNotEqual(self.served(item), original)
        self.view(".actions .btn", has_text="Affiche d’origine").click()
        self.until(lambda: not self.api("/api/item/" + item["id"]).get("poster_custom"))
        self.assertEqual(self.served(item), original)
        self.assertEqual(self.view(".actions .btn", has_text="Affiche d’origine").count(), 0)

    def test_a_show_has_the_same_buttons(self):
        item = self.item("series", "Dark")
        self.reset_later(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send(self.chosen())
        self.until(lambda: self.api("/api/item/" + item["id"]).get("poster_custom") is True)
        self.assertEqual(self.served(item), self.CHOSEN)

    def test_a_file_that_is_not_an_image_is_refused_with_a_message(self):
        item = self.item("films", "Inception")
        self.reset_later(item)
        self.open("#/i/" + item["id"])
        self.view(".actions .btn", has_text="Changer l’affiche").wait_for()
        self.send({"name": "notes.txt", "mimeType": "text/plain", "buffer": b"du texte"})
        self.page.locator("#toast.on", has_text="pas une image").wait_for()
        self.assertNotIn("poster_custom", self.api("/api/item/" + item["id"]))
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


class TestSettings(UiCase):
    """La page « Paramètres » : des onglets en haut, qui restent affichés, au lieu d'une seule longue page de gestion."""

    def tabs(self):
        return self.view(".tabs .tab").all_inner_texts()

    def tab(self, name):
        self.view(".tabs .tab", has_text=name).click()

    def titles(self):
        return self.view(".panel h2").all_inner_texts()

    def test_the_page_is_called_settings_and_opens_on_the_management_tab(self):
        self.open("#/settings")
        self.view(".panel").first.wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Paramètres")
        self.assertEqual(self.page.locator("#nav .tool[data-nav='#/settings']").get_attribute("aria-label"), "Paramètres")
        self.assertEqual(self.tabs(), ["Gestion", "Lecture", "Comptes", "Tâches", "Serveur"])
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Gestion")
        self.assertEqual(self.titles(), ["Bibliothèques", "Sagas", "Entretien"])

    def test_the_old_address_still_leads_to_the_page(self):
        self.open("#/admin")
        self.view(".tabs").wait_for()
        self.assertEqual(self.view("h1").inner_text(), "Paramètres")

    def test_each_tab_has_its_own_panels(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        for name, titles in (("Lecture", ["Lecture"]), ("Comptes", ["Mon compte", "Affichage"]), ("Tâches", ["Tâches"]), ("Serveur", ["Serveur"])):
            self.tab(name)
            self.until(lambda: self.view(".tabs .tab[aria-selected=true]").inner_text() == name)
            self.assertEqual(self.titles(), titles, name)
        self.tab("Serveur")
        self.assertIn("Version", self.view("dl").inner_text())

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
        button = self.view("[data-key^='scan-']:not([data-key='scan-all'])").last
        button.click()                                  # Playwright amène le bouton à l'écran avant de cliquer : on mesure après
        before = self.page.evaluate("window.scrollY")
        self.assertGreater(before, 50)
        self.page.locator("#toast.on", has_text="Mise à jour lancée").wait_for()
        self.page.wait_for_timeout(2500)                # la page est reconstruite quand la mise à jour change l'état des bibliothèques
        self.assertEqual(self.page.evaluate("window.scrollY"), before)

    def test_the_account_chooses_how_the_music_library_opens(self):
        self.addCleanup(lambda: self.api("/api/me/settings", {"music_view": "albums"}))
        self.open("#/settings?tab=comptes")
        panel = self.view(".panel", has_text="Affichage")
        panel.wait_for()
        self.assertIn("Albums", panel.inner_text())
        panel.locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Artistes").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["music_view"] == "artists")
        self.until(lambda: "Artistes" in self.view(".panel", has_text="Affichage").inner_text())
        panel = self.view(".panel", has_text="Affichage")
        panel.locator("button", has_text="Changer").click()
        self.page.locator(".menu .opt .lbl", has_text="Albums").click()
        self.until(lambda: self.api("/api/me/settings")["settings"]["music_view"] == "albums")

    def test_the_tab_chosen_is_remembered(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.tab("Serveur")
        self.page.reload()
        self.view(".tabs").wait_for()
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Serveur")
        self.open("#/settings?tab=comptes")
        self.view(".tabs").wait_for()
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Comptes")

    def test_the_tabs_are_reached_with_the_remote(self):
        self.open("#/settings?tab=gestion")
        self.view(".tabs .tab").first.wait_for()
        self.view(".tabs .tab", has_text="Gestion").focus()
        self.press("ArrowRight", "Enter", wait=300)
        self.assertEqual(self.view(".tabs .tab[aria-selected=true]").inner_text(), "Lecture")

    def test_the_tasks_unfold_one_by_one_or_all_at_once(self):
        self.open("#/settings")
        self.view(".tabs").wait_for()
        self.tab("Tâches")
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
        self.open("#/settings?tab=taches")
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
                self.assertEqual(page.locator(".tabs .tab").all_inner_texts(), ["Lecture", "Comptes"])
                page.locator(".tabs .tab", has_text="Comptes").click()
                page.wait_for_selector(".panel h2")
                self.assertEqual(page.locator(".panel h2").all_inner_texts(), ["Mon compte", "Affichage"])
                page.goto(base + "/#/settings?tab=serveur")                       # un onglet réservé à l'administrateur ne s'ouvre pas
                shown = page.locator("#views > .view:visible .tabs .tab[aria-selected=true]")       # l'ancienne page reste, cachée, derrière la nouvelle
                shown.wait_for()
                self.assertEqual(shown.inner_text(), "Lecture")
                browser.close()
            run.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
