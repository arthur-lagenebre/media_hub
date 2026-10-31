"""L'interface dans un vrai navigateur (Chromium via Playwright) : navigation à la télécommande (flèches, Entrée, Retour), lecture, reprise, lecteurs.

Sauté si Playwright ou ffmpeg manquent. Ce que ces tests ne peuvent pas garantir : la lecture sur TON téléviseur (codecs, HDR, son) — à essayer sur place.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.request
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
        self.assertEqual(self.view(".grid .card.saga").count(), 1)          # les sagas sont regroupées par défaut : une seule vignette
        self.view(".tools .btn", has_text="Sagas").click()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.view(".grid .card").count(), 2)               # sagas dépliées : les deux films
        self.view(".tools .btn", has_text="Sagas").click()
        self.page.wait_for_timeout(500)
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

    def test_admin_runs_an_update(self):
        self.open("#/admin")
        self.view(".panel").first.wait_for()
        self.view("button", has_text="Tout mettre à jour").click()
        status = self.view(".job .jt span").first
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

    def test_the_sagas_choice_is_remembered(self):
        self.open("#/l/films")
        self.view(".grid .card.saga").first.wait_for()
        self.view(".tools .btn", has_text="Sagas").click()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.view(".grid .card.saga").count(), 0)
        self.page.reload()
        self.view(".grid .card").first.wait_for()
        self.assertEqual(self.view(".grid .card.saga").count(), 0)


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
        self.view(".line").first.wait_for()                                   # `.sagaedit` est la racine de la vue : on cherche dedans, pas au-dessus
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
        self.page.locator(".menu .opt", has_text="Supprimer définitivement").wait_for()
        self.page.locator(".menu .opt", has_text="Annuler").click()
        self.page.wait_for_timeout(300)
        self.assertFalse([j for j in self.api("/api/jobs")["jobs"] if "Nettoyage" in j["label"]])          # annuler ne lance rien

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


if __name__ == "__main__":
    unittest.main()
