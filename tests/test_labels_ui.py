"""Renommer une bibliothèque depuis Paramètres > Gestion : l'administrateur change le nom AFFICHÉ (jamais l'identifiant), ou rétablit le nom d'origine.

Interface dans Chromium (Playwright, comme test_ui) : la fenêtre, ses erreurs, la mise à jour de la barre, de l'accueil et des titres sans recharger, le texte jamais interprété
comme du HTML, la mise en page d'un nom de 40 caractères, la non-traduction d'un nom choisi en anglais, le compte ordinaire qui n'a pas le bouton.
Sauté sans Playwright ou ffmpeg : un test sauté n'est pas un test réussi.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.request

from tests.test_server import Running
from tests.test_ui import UiCase, sync_playwright

IDS = ("films", "series", "animations", "musique", "livres")
TRICKY = '<b>x</b> &amp; "q" \'a\''                 # du HTML, une entité, deux sortes de guillemets : tout doit s'afficher tel quel
LONG_WORD = "Supercalifragilisticexpialidocious" + "123456"         # 40 caractères sans une espace
WIDE_CAPS = "W" * 40                                         # les plus larges majuscules, 40 fois


class RenameCase(UiCase):
    """Un serveur ouvert (donc administrateur) ; chaque test rend à la fin leur nom d'origine aux bibliothèques."""

    def setUp(self):
        super().setUp()
        self.refusals = 0
        self.addCleanup(self.restore_all)

    def tearDown(self):
        """Chaque refus du serveur (400, attendu) laisse une ligne « Failed to load resource » dans la console du navigateur : on la retire, pour que les vraies erreurs restent vues."""
        for _ in range(self.refusals):
            self.errors.remove(next(e for e in self.errors if "status of 400" in e))
        super().tearDown()

    def restore_all(self):
        for lib in IDS:
            self.api(f"/api/library/{lib}/label", {"label": ""})

    def labels(self):
        return {lib["id"]: lib["label"] for lib in self.api("/api/state")["libraries"]}

    def nav_texts(self):
        return self.page.locator("#nav .link").all_inner_texts()

    def management(self, hash="#/settings?tab=gestion"):
        """La page Gestion, bibliothèques dépliées."""
        self.open(hash)
        toggle = self.view("[data-key=libs-toggle]")
        toggle.wait_for()
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        self.view("[data-key=rename-series]").wait_for()

    def dialog(self):
        return self.page.locator(".veil .menu.form")

    def open_rename(self, lib="series"):
        self.view(f"[data-key=rename-{lib}]").click()
        box = self.dialog()
        box.wait_for()
        self.page.wait_for_function("() => { var e = document.activeElement; return !!e && e.tagName === 'INPUT' && e.selectionEnd > e.selectionStart; }")      # le champ prend le focus, puis son texte est sélectionné, après un court délai
        return box

    def save_as(self, text, lib="series"):
        """Ouvre la fenêtre, tape le nom, enregistre ; la fenêtre est rendue (ouverte ou non, selon la réponse du serveur)."""
        box = self.open_rename(lib)
        box.locator("input").fill(text)
        box.locator(".btns .btn.primary").click()
        return box

    def renamed(self, lib, text):
        self.save_as(text, lib)
        self.page.wait_for_selector(".veil", state="detached")
        self.until(lambda: text in self.nav_texts())

    def hide_home_shelves(self):
        self.api("/api/me/settings", {"home_latest": "hide", "home_unseen": "hide"})
        self.addCleanup(lambda: self.api("/api/me/settings", {"home_latest": "show", "home_unseen": "show"}))


class TestRenameDialog(RenameCase):
    def test_the_button_opens_a_form_with_the_current_name_selected_and_no_restore_yet(self):
        self.management()
        button = self.view("[data-key=rename-series]")
        self.assertEqual(button.get_attribute("aria-label"), "Renommer la bibliothèque Séries")      # un nom explicite pour un lecteur d'écran, le mot visible reste « Renommer »
        self.assertEqual(button.inner_text(), "Renommer")
        box = self.open_rename()
        self.assertEqual(box.get_attribute("role"), "dialog")
        self.assertEqual(box.get_attribute("aria-label"), "Renommer la bibliothèque « Séries »")
        field = box.locator("input")
        self.assertEqual(field.input_value(), "Séries")
        self.assertEqual(field.get_attribute("maxlength"), "40")
        self.assertEqual(field.evaluate("e => [e.selectionStart, e.selectionEnd, e.value.length]"), [0, 6, 6])      # sélectionné : taper remplace le nom
        self.assertEqual(box.locator("label.flab").inner_text().strip(), "Nom affiché (40 caractères au plus)")        # un <label> pour le champ
        self.assertEqual(box.locator("[data-key=form-alt]").count(), 0)                                                # rien à rétablir : le nom est celui d'origine
        self.assertEqual(box.locator(".btns .btn").all_inner_texts(), ["Enregistrer", "Annuler"])

    def test_saving_a_new_name_updates_the_bar_the_home_and_the_page_titles_without_reloading(self):
        self.hide_home_shelves()
        self.open("#/")
        self.page.locator("#nav [data-nav='#/l/series']").click()
        self.shown("#/l/series")
        self.assertEqual(self.view("h1").inner_text(), "Séries")
        self.page.evaluate("window.sameDocument = 1")                                                        # disparaît si la page est rechargée
        self.page.locator("#nav .tool[data-nav='#/settings']").click()
        self.shown("#/settings")
        self.view("[data-key=libs-toggle]").click()
        self.view("[data-key=rename-series]").click()
        self.dialog().locator("input").fill("Feuilletons")
        self.dialog().locator(".btns .btn.primary").click()
        self.page.wait_for_selector(".veil", state="detached")
        self.page.locator("#toast.on", has_text="Bibliothèque renommée : Feuilletons").wait_for()
        self.until(lambda: "Feuilletons" in self.nav_texts())
        self.assertEqual(self.nav_texts(), ["Films", "Feuilletons", "Animations", "Musique", "Livres"])                 # la barre, au même rang
        self.assertEqual(self.api("/api/state")["libraries"][1]["id"], "series")                                       # l'identifiant, lui, n'a pas bougé
        self.assertEqual(self.view(".line[data-lib=series] .what b").inner_text(), "Feuilletons")                     # le détail de la page Gestion
        self.assertEqual(self.view("[data-key=rename-series]").get_attribute("aria-label"), "Renommer la bibliothèque Feuilletons")
        self.until(lambda: self.focused() == "Renommer la bibliothèque Feuilletons")                                    # le focus est rendu au bouton Renommer
        self.page.go_back()                                                                                            # retour à la page des séries, gardée en réserve avant le renommage : refaite avec le nouveau nom
        self.shown("#/l/series")
        self.assertEqual(self.view("h1").inner_text(), "Feuilletons")
        self.assertEqual(self.view("input[type=search]").get_attribute("placeholder"), "Filtrer Feuilletons")
        self.page.locator("#nav .brand").click()                                                                       # l'accueil : la tuile de la bibliothèque
        self.shown("#/")
        self.view(".libtile[data-lib=series] b").wait_for()
        self.assertEqual(self.view(".libtile[data-lib=series] b").inner_text(), "Feuilletons")
        self.assertEqual(self.page.evaluate("window.sameDocument"), 1)                                                 # sans recharger la page

    def test_the_search_shelf_uses_the_new_name(self):
        self.management()
        self.renamed("series", "Feuilletons")
        self.page.locator("#nav .tool[data-nav='#/search']").click()
        self.view("input[type=search]").fill("dark")
        self.view(".shelf[data-lib=series] h2").wait_for()
        self.assertEqual(self.view(".shelf[data-lib=series] h2").inner_text(), "Feuilletons")

    def test_restore_is_offered_only_after_a_change_and_gives_back_the_original_name(self):
        self.management()
        self.renamed("series", "Feuilletons")
        box = self.open_rename()
        self.assertEqual(box.locator("input").input_value(), "Feuilletons")
        alt = box.locator("[data-key=form-alt]")
        self.assertEqual(alt.inner_text(), "Rétablir « Séries »")
        self.assertEqual(box.locator(".btns .btn").all_inner_texts(), ["Enregistrer", "Rétablir « Séries »", "Annuler"])
        alt.click()
        self.page.wait_for_selector(".veil", state="detached")
        self.page.locator("#toast.on", has_text="Nom d’origine rétabli : Séries").wait_for()
        self.until(lambda: "Séries" in self.nav_texts())
        self.assertEqual(self.nav_texts(), ["Films", "Séries", "Animations", "Musique", "Livres"])
        self.assertEqual(self.labels()["series"], "Séries")
        self.assertEqual(self.open_rename().locator("[data-key=form-alt]").count(), 0)                                  # rétabli : plus rien à rétablir

    def test_saving_without_a_change_closes_the_box_and_asks_nothing(self):
        self.management()
        sent = []
        self.page.on("request", lambda r: sent.append(r.url) if r.method == "POST" else None)
        self.open_rename().locator(".btns .btn.primary").click()
        self.page.wait_for_selector(".veil", state="detached")
        self.assertEqual([url for url in sent if url.endswith("/label")], [])
        self.assertEqual(self.labels()["series"], "Séries")

    def test_an_empty_field_restores_the_original_name(self):
        self.management()
        self.renamed("series", "Feuilletons")
        box = self.open_rename()
        box.locator("input").fill("")
        box.locator(".btns .btn.primary").click()
        self.page.wait_for_selector(".veil", state="detached")
        self.until(lambda: self.labels()["series"] == "Séries")
        self.until(lambda: "Séries" in self.nav_texts())

    def test_a_name_taken_by_another_library_is_refused_with_the_servers_message_and_the_box_stays_open(self):
        self.refusals = 1                                                                                             # le serveur refuse 1 fois : 1 ligne(s) de console attendue(s)
        self.management()
        box = self.save_as("films")                                                                                    # « Films » : à la casse près, le nom d'une autre bibliothèque
        error = box.locator("p.formerr")
        self.until(lambda: error.inner_text() != "")
        self.assertEqual(error.inner_text(), "« Films » est déjà le nom d'une autre bibliothèque")                     # le message du serveur, tel quel
        self.assertEqual(error.get_attribute("role"), "alert")                                                         # annoncé par un lecteur d'écran
        self.assertTrue(box.is_visible())                                                                              # la fenêtre reste ouverte
        self.assertEqual(self.labels()["series"], "Séries")                                                            # et rien n'a changé
        self.assertEqual(self.nav_texts()[1], "Séries")
        field = box.locator("input")
        field.focus()                                                                                                  # le champ reste utilisable
        field.fill("Feuilletons")
        box.locator(".btns .btn.primary").click()
        self.page.wait_for_selector(".veil", state="detached")                                                         # corrigé : enregistré, et l'erreur n'est plus là
        self.until(lambda: self.labels()["series"] == "Feuilletons")

    def test_the_servers_message_about_a_name_too_long_for_it_is_shown_in_the_box(self):
        self.refusals = 1                                                                                             # le serveur refuse 1 fois : 1 ligne(s) de console attendue(s)
        self.management()
        box = self.open_rename()
        box.locator("input").evaluate("e => e.removeAttribute('maxlength')")                                           # le champ s'arrête à 40 ; on saute cette barrière pour voir le refus du serveur
        box.locator("input").fill("x" * 41)
        box.locator(".btns .btn.primary").click()
        error = box.locator("p.formerr")
        self.until(lambda: error.inner_text() != "")
        self.assertEqual(error.inner_text(), "le libellé fait 40 caractères au plus")
        self.assertTrue(box.is_visible())

    def test_a_double_click_on_save_sends_one_request(self):
        self.management()
        sent = []
        self.page.on("request", lambda r: sent.append(r.url) if r.method == "POST" and r.url.endswith("/label") else None)
        box = self.open_rename()
        box.locator("input").fill("Feuilletons")
        box.locator(".btns .btn.primary").dblclick()
        self.page.wait_for_selector(".veil", state="detached")
        self.assertEqual(len(sent), 1)

    def test_escape_leaves_the_field_then_closes_the_box_without_saving_and_gives_the_focus_back_to_the_button(self):
        self.management()
        box = self.open_rename()
        box.locator("input").fill("Feuilletons")
        self.page.keyboard.press("Escape")                                                                              # le comportement de tout champ : un premier Échap en sort...
        self.page.wait_for_function("document.activeElement.tagName !== 'INPUT'")
        self.assertTrue(box.is_visible())                                                                              # ...la fenêtre est toujours là
        self.page.keyboard.press("Escape")                                                                              # ...un second la ferme
        self.page.wait_for_selector(".veil", state="detached")
        self.assertEqual(self.labels()["series"], "Séries")                                                            # rien n'est enregistré
        self.assertEqual(self.nav_texts()[1], "Séries")
        self.until(lambda: self.focused() == "Renommer la bibliothèque Séries")

    def test_escape_closes_the_box_at_once_when_the_focus_is_not_in_the_field(self):
        self.management()
        box = self.open_rename()
        box.locator("input").fill("Feuilletons")
        box.locator(".btns .btn.primary").focus()                                                                      # le focus sur un bouton (comme à la télécommande, après les flèches)
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector(".veil", state="detached")
        self.assertEqual(self.labels()["series"], "Séries")
        self.until(lambda: self.focused() == "Renommer la bibliothèque Séries")

    def test_cancel_and_the_remote_back_key_close_the_box_without_saving(self):
        self.management()
        box = self.open_rename()
        box.locator("input").fill("Feuilletons")
        box.locator(".btns .btn", has_text="Annuler").click()
        self.page.wait_for_selector(".veil", state="detached")
        self.until(lambda: self.focused() == "Renommer la bibliothèque Séries")
        self.open_rename().locator("input").fill("Feuilletons")
        self.page.evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'GoBack', keyCode: 4, bubbles: true, cancelable: true }))")                                                                          # la touche Retour d'une télécommande
        self.page.wait_for_selector(".veil", state="detached")
        self.assertEqual(self.labels()["series"], "Séries")

    def test_the_arrow_keys_stay_inside_the_box(self):
        self.management()
        box = self.open_rename()
        box.locator("input").evaluate("e => e.blur()")
        for key in ("ArrowDown", "ArrowDown", "ArrowDown", "ArrowUp", "ArrowUp"):
            self.press(key)
            inside = self.page.evaluate("document.querySelector('.veil .menu').contains(document.activeElement)")
            self.assertTrue(inside, f"le focus est sorti de la fenêtre après {key}")


class TestRenamedNameIsText(RenameCase):
    """Un nom choisi s'affiche tel quel : `<b>`, `&amp;` et les guillemets ne sont ni interprétés ni échappés en double."""

    def test_html_in_a_name_is_shown_literally_in_the_bar_the_home_the_pages_and_the_settings(self):
        self.hide_home_shelves()
        self.management()
        self.renamed("series", TRICKY)
        self.assertEqual(self.labels()["series"], TRICKY)                                                              # le serveur l'a gardé tel quel
        self.assertEqual(self.nav_texts()[1], TRICKY)
        self.assertEqual(self.page.locator("#nav .link b, #nav .link i:not(.dot)").count(), 0)                         # aucun élément n'est né du texte
        self.assertEqual(self.page.locator("#nav [data-lib=series]").get_attribute("title"), TRICKY)
        self.assertEqual(self.view(".line[data-lib=series] .what b").inner_text(), TRICKY)
        self.assertEqual(self.view("[data-key=rename-series]").get_attribute("aria-label"), f"Renommer la bibliothèque {TRICKY}")
        self.page.locator("#nav .brand").click()
        self.shown("#/")
        tile = self.view(".libtile[data-lib=series]")
        tile.wait_for()
        self.assertEqual(tile.locator("b").inner_text(), TRICKY)
        self.assertEqual(tile.locator("b b, b i:not(.dot)").count(), 0)
        tile.click()
        self.shown("#/l/series")
        self.assertEqual(self.view("h1").inner_text(), TRICKY)
        self.assertEqual(self.view("h1 b").count(), 0)
        self.assertEqual(self.view("input[type=search]").get_attribute("placeholder"), f"Filtrer {TRICKY}")
        self.assertEqual(self.page.title(), "Médiathèque")                                                              # le titre du document ne porte pas de nom de bibliothèque

    def test_html_in_a_name_is_literal_in_the_dialog_the_shelves_the_search_and_the_task_titles(self):
        self.management()
        self.renamed("series", TRICKY)
        box = self.open_rename()
        self.assertEqual(box.get_attribute("aria-label"), f"Renommer la bibliothèque « {TRICKY} »")
        self.assertEqual(box.locator("h3").inner_text(), f"Renommer la bibliothèque « {TRICKY} »")
        self.assertEqual(box.locator("input").input_value(), TRICKY)
        self.assertEqual(box.locator("[data-key=form-alt]").inner_text(), "Rétablir « Séries »")
        box.locator(".btns .btn", has_text="Annuler").click()
        self.page.wait_for_selector(".veil", state="detached")
        self.view("[data-key=scan-series]").click()                                                                     # une tâche dont l'intitulé porte le nom
        self.page.locator("#toast.on", has_text=f"Mise à jour lancée : {TRICKY}").wait_for()
        self.until(lambda: any(j["label"] == f"Mise à jour · {TRICKY}" for j in self.api("/api/jobs")["jobs"]))
        job = self.view(".job .jt b", has_text="Mise à jour")
        job.first.wait_for()
        self.assertEqual(job.first.inner_text(), f"Mise à jour · {TRICKY}")
        self.assertEqual(self.view(".job .jt b b").count(), 0)
        self.page.locator("#nav .tool[data-nav='#/search']").click()
        self.view("input[type=search]").fill("dark")
        shelf = self.view(".shelf[data-lib=series] h2")
        shelf.wait_for()
        self.assertEqual(shelf.inner_text(), TRICKY)
        self.assertEqual(shelf.locator("b").count(), 0)

    def test_a_name_with_html_cited_in_the_servers_refusal_is_shown_literally(self):
        self.refusals = 1                                                                                             # le serveur refuse 1 fois : 1 ligne(s) de console attendue(s)
        self.management()
        self.renamed("series", TRICKY)
        box = self.save_as(TRICKY.upper(), "films")                                                                      # à la casse près, le nom de la bibliothèque des séries : refusé
        error = box.locator("p.formerr")
        self.until(lambda: error.inner_text() != "")
        self.assertEqual(error.inner_text(), f"« {TRICKY} » est déjà le nom d'une autre bibliothèque")
        self.assertEqual(error.locator("b").count(), 0)


class TestRenamedNameLayout(RenameCase):
    """Un nom de 40 caractères (très large, ou d'un seul mot) ne casse ni la barre du haut, ni l'accueil, ni la page de la bibliothèque, ni les réglages."""

    def overflow(self):
        return self.page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")

    def check(self, name, width):
        self.page.set_viewport_size({"width": width, "height": 800})
        self.api("/api/library/series/label", {"label": name})
        self.hide_home_shelves()
        for hash in ("#/", "#/l/series", "#/settings?tab=gestion", "#/search"):
            self.open(hash)
            self.page.wait_for_timeout(150)
            if hash.startswith("#/settings"):
                self.view("[data-key=libs-toggle]").click()
            self.assertLessEqual(self.overflow(), 0, f"{name[:6]}… défile en largeur à {width}px sur {hash}")
        self.open("#/")
        self.view(".libtile[data-lib=series]").wait_for()
        tile = self.view(".libtile[data-lib=series]").bounding_box()
        self.assertLessEqual(tile["x"] + tile["width"], width + 1)                                                         # la carte reste dans l'écran
        nav = self.page.locator("#nav").bounding_box()
        self.assertLessEqual(nav["width"], width + 1)                                                                      # la barre ne s'élargit pas : ses liens défilent
        link = self.page.locator("#nav [data-lib=series]").bounding_box()
        self.assertLess(link["width"], width)                                                                              # le nom est coupé, pas étalé sur toute la barre

    def test_a_forty_letter_word_fits_on_a_phone_and_on_a_big_screen(self):
        for width in (360, 1920):
            with self.subTest(width=width):
                self.check(LONG_WORD, width)

    def test_forty_wide_capitals_fit_on_a_phone_and_on_a_big_screen(self):
        for width in (360, 1920):
            with self.subTest(width=width):
                self.check(WIDE_CAPS, width)


class TestRenamedNameInEnglish(RenameCase):
    """Le nom d'origine se traduit (« Séries » → « TV shows ») ; un nom choisi, même s'il est une clé du dictionnaire (« Paramètres »), ne se traduit jamais.
    « Livres » ne peut pas servir d'exemple : il est refusé pour toute autre bibliothèque que celle des livres (c'est son nom d'origine)."""

    def setUp(self):
        super().setUp()
        self.context.add_cookies([{"name": "hub_lang", "value": "en", "url": self.base}])
        self.page.add_init_script("localStorage.setItem('hub.lang', JSON.stringify('en'))")

    def test_a_chosen_name_equal_to_a_dictionary_key_is_not_translated_and_the_original_is(self):
        self.hide_home_shelves()
        self.api("/api/library/films/label", {"label": "Paramètres"})                                                      # « Settings » dans le dictionnaire
        self.api("/api/library/animations/label", {"label": "Séries télé"})
        self.open("#/")
        self.assertEqual(self.nav_texts(), ["Paramètres", "TV shows", "Séries télé", "Music", "Books"])                  # films : choisi, tel quel ; séries, musique, livres : d'origine, traduits
        self.view(".libtile[data-lib=films]").wait_for()
        self.assertEqual(self.view(".libtile[data-lib=films] b").inner_text(), "Paramètres")
        self.assertEqual(self.view(".libtile[data-lib=series] b").inner_text(), "TV shows")
        self.open("#/l/films")
        self.assertEqual(self.view("h1").inner_text(), "Paramètres")
        self.assertEqual(self.view("input[type=search]").get_attribute("placeholder"), "Filter Paramètres")
        self.open("#/l/series")
        self.assertEqual(self.view("h1").inner_text(), "TV shows")
        self.assertEqual(self.page.evaluate("Hub.missing || []"), [])

    def test_the_search_shelves_and_the_settings_follow_the_same_rule(self):
        self.api("/api/library/films/label", {"label": "Paramètres"})
        self.open("#/search")
        self.view("input[type=search]").fill("incep")
        self.view(".shelf[data-lib=films] h2").wait_for()
        self.assertEqual(self.view(".shelf[data-lib=films] h2").inner_text(), "Paramètres")
        self.management()
        self.assertEqual(self.view(".line[data-lib=films] .what b").inner_text(), "Paramètres")
        self.assertEqual(self.view(".line[data-lib=series] .what b").inner_text(), "TV shows")
        self.assertEqual(self.view("[data-key=rename-films]").get_attribute("aria-label"), "Rename library Paramètres")

    def test_task_titles_keep_a_chosen_name_and_translate_an_original_one(self):
        self.api("/api/library/films/label", {"label": "Paramètres"})
        self.api("/api/scan", {"lib": "films"})
        self.api("/api/scan", {"lib": "series"})
        self.run_.wait_jobs()
        self.management()
        self.view(".job .jt b", has_text="Update").first.wait_for()
        titles = self.view(".job .jt b").all_inner_texts()
        self.assertIn("Update · Paramètres", titles)
        self.assertIn("Update · TV shows", titles)
        self.assertNotIn("Update · Settings", titles)

    def test_the_dialog_is_in_english_and_its_refusal_keeps_the_chosen_name(self):
        self.refusals = 2                                                                                             # le serveur refuse 2 fois : 2 ligne(s) de console attendue(s)
        self.api("/api/library/films/label", {"label": "Paramètres"})
        self.management()
        box = self.open_rename("films")
        self.assertEqual(box.get_attribute("aria-label"), "Rename library “Paramètres”")
        self.assertEqual(box.locator("label.flab").inner_text().strip(), "Displayed name (40 characters at most)")
        self.assertEqual(box.locator(".btns .btn").all_inner_texts(), ["Save", "Restore “Movies”", "Cancel"])            # l'origine de films est « Films », traduit « Movies »
        box.locator(".btns .btn", has_text="Cancel").click()
        self.page.wait_for_selector(".veil", state="detached")
        box = self.save_as("paramètres", "series")                                                                        # le nom choisi pour les films, à la casse près
        error = box.locator("p.formerr")
        self.until(lambda: error.inner_text() != "")
        self.assertEqual(error.inner_text(), "“Paramètres” is already the name of another library")                      # ni « Settings » ici
        box.locator("input").fill("Films")                                                                                # le nom d'origine des films, lui, est traduit dans le message
        box.locator(".btns .btn.primary").click()
        self.until(lambda: error.inner_text() == "“Movies” is already the name of another library")
        self.assertEqual(self.page.evaluate("Hub.missing || []"), [])

    def test_the_label_function_follows_the_rules(self):
        self.open("#/")
        out = self.page.evaluate("""() => {
          var L = Hub.libLabel;
          Hub.libs = [{ id: 'films', label: 'Paramètres', default_label: 'Films', kind: 'movies' }, { id: 'series', label: 'Séries', default_label: 'Séries', kind: 'shows' }];
          return [
            L({ id: 'series', label: 'Séries', default_label: 'Séries' }),          // d'origine : traduit
            L({ id: 'films', label: 'Paramètres', default_label: 'Films' }),        // choisi : tel quel
            L({ id: 'series', label: 'Séries' }),                                    // /api/home n'a pas default_label : retrouvé par l'identifiant dans H.libs
            L({ id: 'films', label: 'Paramètres' }),
            L({ id: 'inconnue', label: 'Livres' }),                                  // impossible à décider : tel quel
            L(null),
            Hub.libRenamed({ id: 'films', label: 'Paramètres' }), Hub.libRenamed({ id: 'series', label: 'Séries' }), Hub.libRenamed({ id: 'inconnue', label: 'x' }),
            Hub.isChosenLabel('Paramètres'), Hub.isChosenLabel('Séries'),
            Hub.tr('Mise à jour · Paramètres'), Hub.tr('Mise à jour · Séries')
          ];
        }""")
        self.assertEqual(out, ["TV shows", "Paramètres", "TV shows", "Paramètres", "Livres", "", True, False, False, True, False, "Update · Paramètres", "Update · TV shows"])


class TestRenameOfAnOrdinaryAccount(unittest.TestCase):
    """Avec des comptes : le bouton n'existe que pour l'administrateur ; la grille des droits et la liste des comptes portent le nom choisi, en texte."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.run_ = Running(cls.tmp, password="secret")
        cls.base = f"http://127.0.0.1:{cls.run_.port}"
        cls.token = cls.call("/api/token", {"password": "secret"})["token"]
        cls.alice = cls.call("/api/users", {"name": "alice", "password": "motdepasse12"}, cls.token)["id"]
        cls.call("/api/users", {"name": "lea", "password": "motdepasse12", "role": "user"}, cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.run_.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def call(cls, path, body=None, token=""):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        req = urllib.request.Request(cls.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return json.load(urllib.request.urlopen(req))

    def sign_in(self, page, name, password):
        page.goto(self.base + "/login")
        page.fill("input[name=user]", name)
        page.fill("input[type=password]", password)
        page.keyboard.press("Enter")
        page.wait_for_selector("#nav .link", state="attached")

    def setUp(self):
        self.addCleanup(lambda: self.call("/api/library/series/label", {"label": ""}, self.token))

    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_an_ordinary_account_has_no_rename_button_and_the_server_refuses_it(self):
        self.call("/api/library/series/label", {"label": "Feuilletons"}, self.token)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            self.sign_in(page, "lea", "motdepasse12")
            self.assertEqual(page.locator("#nav .link").all_inner_texts()[1], "Feuilletons")                                  # elle voit le nom choisi...
            for hash in ("#/settings", "#/settings?tab=gestion"):
                page.goto(self.base + "/" + hash)
                shown = page.locator("#views > .view:visible .tabs .tab[aria-selected=true]")
                shown.wait_for()
                self.assertEqual(page.locator("#views > .view:visible .tabs .tab").all_inner_texts(), ["Préférences", "Comptes"])      # ...mais pas l'onglet Gestion
                self.assertEqual(page.locator("[data-key^=rename-]").count(), 0)                                               # ...ni le bouton
            status = page.evaluate("fetch('/api/library/series/label', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: 'Pirate' }) }).then(r => r.status)")
            self.assertEqual(status, 403)                                                                                      # le serveur refuse de toute façon
            browser.close()
        self.assertEqual({lib["id"]: lib["label"] for lib in self.call("/api/state", None, self.token)["libraries"]}["series"], "Feuilletons")

    @unittest.skipUnless(sync_playwright, "Playwright absent")
    def test_the_rights_grid_and_the_account_list_show_the_chosen_name_as_text(self):
        self.call("/api/library/series/label", {"label": TRICKY}, self.token)
        rights = {lib: {"view": lib != "livres", "play": lib != "livres", "download": lib not in ("livres", "series")} for lib in IDS}
        self.call(f"/api/user/{self.alice}/rights", {"rights": rights}, self.token)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            self.sign_in(page, "admin", "secret")
            page.goto(self.base + "/#/settings?tab=comptes")
            page.locator(f'[data-key="rights-{self.alice}"]').click()
            row = page.locator(".rights .rrow[data-lib=series]")
            row.wait_for()
            self.assertEqual(row.locator("b").inner_text(), TRICKY)                                                            # la grille des droits
            self.assertEqual(row.locator("b b, b i").count(), 0)
            page.locator(".menu .btn", has_text="Annuler").click()
            summary = page.locator(f'[data-rights="{self.alice}"]')
            summary.wait_for()
            self.assertIn(f"Consulter : Films, {TRICKY}, Animations, Musique", summary.inner_text())                           # la liste des comptes
            self.assertIn("Télécharger : Films, Animations, Musique", summary.inner_text())                                    # ...sans le nom choisi quand le droit manque
            self.assertEqual(summary.locator("b").count(), 0)
            browser.close()


if __name__ == "__main__":                                                                                                    # pragma: no cover
    unittest.main()
