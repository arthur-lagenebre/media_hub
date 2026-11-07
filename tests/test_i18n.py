"""Les deux langues de l'interface : le français (celui du code) et l'anglais (hub/web/js/lang-en.js).

Statique : chaque texte que le code passe à `H.t` / `H.plural` a sa traduction, avec les mêmes `{noms}` ; les modèles de messages du serveur existent bien dans le code Python.
Navigateur (Chromium, comme test_ui) : l'anglais s'affiche, le réglage « Langue » le choisit et le retient, un texte du serveur est traduit.
"""

from __future__ import annotations

import re
import unittest
import urllib.request
from pathlib import Path

from tests.test_ui import UiCase

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "hub" / "web" / "js"
BS = chr(92)


def tokens(source):
    """[(type, valeur, profondeur)] : les chaînes ('str'), identifiants ('id') et signes ('p') d'un fichier JavaScript, sans commentaires ni expressions régulières."""
    out, i, n, depth = [], 0, len(source), 0
    regex_after = set("(,=:[!&|?{;+-*<>")          # après eux, une barre ouvre une expression régulière (après un identifiant, un nombre ou une parenthèse fermée, c'est une division)
    while i < n:
        c = source[i]
        if c.isspace():
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif source.startswith("/*", i):
            i = source.index("*/", i + 2) + 2
        elif c in "'\"":
            j, value = i + 1, []
            while source[j] != c:
                if source[j] == BS:
                    value.append({"n": "\n"}.get(source[j + 1], source[j + 1]))
                    j += 2
                else:
                    value.append(source[j])
                    j += 1
            out.append(("str", "".join(value), depth))
            i = j + 1
        elif c == "`":
            i = source.index("`", i + 1) + 1
        elif c == "/" and (not out or (out[-1][0] == "p" and out[-1][1] in regex_after) or (out[-1][0] == "id" and out[-1][1] in ("return", "typeof"))):
            j, in_class = i + 1, False
            while source[j] != "/" or in_class:
                if source[j] == BS:
                    j += 1
                elif source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                j += 1
            i = j + 1
            while i < n and source[i].isalpha():
                i += 1
        elif c.isalpha() or c in "_$":
            j = i
            while j < n and (source[j].isalnum() or source[j] in "_$"):
                j += 1
            out.append(("id", source[i:j], depth))
            i = j
        elif c.isdigit():
            j = i
            while j < n and (source[j].isalnum() or source[j] == "."):
                j += 1
            out.append(("num", source[i:j], depth))
            i = j
        else:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            out.append(("p", c, depth))
            i += 1
    return out


def call_strings(toks, name):
    """Les chaînes qui sont des arguments directs d'`H.<name>(...)` : {(rang de l'argument, texte)}."""
    found = set()
    for k in range(len(toks) - 3):
        if toks[k][1] == "H" and toks[k + 1][1] == "." and toks[k + 2][1] == name and toks[k + 3][1] == "(":
            level, arg, j = toks[k + 3][2], 0, k + 4
            while j < len(toks) and not (toks[j][0] == "p" and toks[j][1] == ")" and toks[j][2] == level - 1):
                kind, value, depth = toks[j]
                if depth == level:
                    if kind == "p" and value == ",":
                        arg += 1
                    elif kind == "str":
                        found.add((arg, value))
                j += 1
    return found


def read_dictionaries():
    """({'dict': {clé: texte}, 'srv': {...}}, doublons) d'après lang-en.js."""
    toks = tokens((JS / "lang-en.js").read_text(encoding="utf-8"))
    tables, duplicates, current = {"dict": {}, "srv": {}}, [], None
    for k, (kind, value, _) in enumerate(toks):
        if kind == "id" and value in tables and toks[k + 1][1] == "." and toks[k + 2][1] == "en" and toks[k + 3][1] == "=":
            current = value
        elif kind == "str" and current and toks[k + 1][1] == ":" and toks[k + 2][0] == "str":
            if value in tables[current]:
                duplicates.append((current, value))
            tables[current][value] = toks[k + 2][1]
    return tables, duplicates


def names(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


class TestDictionary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables, cls.duplicates = read_dictionaries()
        cls.keys, cls.words = set(), set()
        for path in sorted(JS.glob("*.js")):
            if path.name in ("lang-en.js", "i18n.js"):
                continue
            toks = tokens(path.read_text(encoding="utf-8"))
            cls.keys |= {(path.name, text) for arg, text in call_strings(toks, "t") if arg == 0}
            for arg, text in call_strings(toks, "plural"):
                if arg >= 1:
                    cls.words |= {text, text + "s"} if arg == 1 else {text}

    def test_the_dictionary_is_read(self):
        self.assertGreater(len(self.tables["dict"]), 400)
        self.assertGreater(len(self.tables["srv"]), 80)

    def test_no_key_is_written_twice(self):
        """Deux fois la même clé dans un objet : une erreur pour les vieux navigateurs de télé (ES5 strict)."""
        self.assertEqual(self.duplicates, [])

    def test_every_text_given_to_t_has_an_english_version(self):
        missing = sorted((f, k) for f, k in self.keys if k not in self.tables["dict"])
        # les chaînes d'une condition (`info.format === 'cbz' ? ... : ...`) sont vues comme des arguments : ce ne sont pas des textes
        missing = [(f, k) for f, k in missing if k not in ("cbz", "epub")]
        self.assertEqual(missing, [])

    def test_every_counted_word_has_both_forms(self):
        self.assertEqual(sorted(w for w in self.words if w not in self.tables["dict"]), [])

    def test_the_translation_keeps_the_same_placeholders(self):
        for table in self.tables.values():
            for key, text in table.items():
                with self.subTest(key=key):
                    self.assertEqual(names(key), names(text))

    def test_no_english_text_is_empty(self):
        for key, text in self.tables["dict"].items():
            with self.subTest(key=key):
                self.assertTrue(text.strip())

    def test_the_server_templates_come_from_the_server_code(self):
        """Un modèle de H.srv.en doit être le texte exact du serveur : si le message français change dans le code, la traduction ne s'applique plus sans que rien ne le dise."""
        source = "\n".join(p.read_text(encoding="utf-8").replace(BS * 2, BS) for p in (ROOT / "hub").rglob("*.py"))
        from_media_library = {"Romans", "Livre dont vous êtes héros", "Manuels", "Autres", "BD", "Site officiel", "Groupe", "Artiste solo", "Orchestre", "Chœur", "Personnage",
                              "depuis {year}", "né en {year}", "jusqu'en {year}", "Wikipédia"}
        assembled = {                 # écrits en plusieurs morceaux dans le code : on vérifie les morceaux
            "Nettoyage · fichiers parasites (simulation)": ["Nettoyage · fichiers parasites", " (simulation)"],
            "  {name} : {n} film(s), {absent} absent(s), {coming} pas encore sorti(s)": ["film(s), ", "absent(s)", "pas encore sorti(s)"],
            "  {name} : fiche vide": ["fiche vide"],
            "{found} fichier(s) de ce genre, {removed} effacé(s).": ["fichier(s) de ce genre", "effacé(s)."],
            "{found} fichier(s) de ce genre (simulation : rien n'a été effacé).": ["fichier(s) de ce genre", " (simulation : rien n'a été effacé)."],
        }
        stale = []
        for key in self.tables["srv"]:
            if key in from_media_library:
                continue
            fragments = assembled.get(key) or [part for part in re.split(r"\{\w+\}", key) if part.strip()]
            if not all(part in source for part in fragments):
                stale.append(key)
        self.assertEqual(stale, [])


class TestPageOrder(unittest.TestCase):
    def test_the_languages_are_loaded_before_the_rest(self):
        """Les textes sont écrits dès le chargement des fichiers (les tables de core.js) : la langue et le dictionnaire doivent déjà être là."""
        page = (ROOT / "hub" / "web" / "index.html").read_text(encoding="utf-8")
        order = [m.group(1) for m in re.finditer(r'<script src="/js/([\w-]+)\.js">', page)]
        self.assertEqual(order[:3], ["i18n", "lang-en", "core"])


class TestLabels(unittest.TestCase):
    """Les libellés que le serveur compose lui-même (pistes audio, sous-titres) suivent la langue de la requête."""

    def setUp(self):
        from hub.server import app
        self.app = app

    def test_audio_tracks(self):
        track = {"lang": "", "title": "", "index": 2, "codec": "aac", "channels": 2}
        self.assertEqual(self.app.track_label(track), "Piste 3 (AAC stéréo)")
        self.assertEqual(self.app.track_label(track, "en"), "Track 3 (AAC stereo)")
        self.assertEqual(self.app.track_label({**track, "lang": "fre"}, "en"), "Français (AAC stereo)")

    def test_subtitles(self):
        sub = {"lang": "", "title": "", "index": 3, "forced": True}
        self.assertEqual(self.app.sub_label(sub), "Piste 4 · Forcé")
        self.assertEqual(self.app.sub_label(sub, "en"), "Track 4 · Forced")

    def test_subtitle_files_next_to_the_video(self):
        self.assertEqual(self.app.side_label("Français · Forcé · SDH"), "Français · Forcé · SDH")
        self.assertEqual(self.app.side_label("Français · Forcé · SDH", "en"), "Français · Forced · SDH")
        self.assertEqual(self.app.side_label("Sous-titres", "en"), "Subtitles")

    def test_an_unknown_language_is_french(self):
        self.assertEqual(self.app.track_label({"lang": "", "title": "", "index": 0}, "de"), "Piste 1")

    def test_the_country_of_an_artist(self):
        info = {"type": "Group", "area": "Sweden", "begin": "1998", "end": "2016"}
        self.assertEqual(self.app.artist_sheet(info)["facts"], "Groupe · Suède · 1998 – 2016")
        self.assertEqual(self.app.artist_sheet(info, "", "en")["facts"], "Groupe · Sweden · 1998 – 2016")       # les mots « Groupe », « depuis »... sont traduits par l'interface (H.tr)


class TestEnglishInTheBrowser(UiCase):
    """La page ouverte en anglais : tout ce qui est écrit par l'interface, et ce que le serveur lui dit."""

    def setUp(self):
        super().setUp()
        self.context.add_cookies([{"name": "hub_lang", "value": "en", "url": self.base}])
        self.page.add_init_script("localStorage.setItem('hub.lang', JSON.stringify('en'))")

    def missing(self):
        return self.page.evaluate("Hub.missing || []")

    def texts(self, selector="body"):
        return self.page.locator(selector).inner_text()

    def test_the_page_is_in_english(self):
        self.open()
        self.assertEqual(self.page.evaluate("document.documentElement.lang"), "en")
        self.assertEqual(self.page.title(), "Media library")
        self.assertEqual(self.page.locator("#nav .brand .name").inner_text(), "Media library")
        self.assertEqual(self.page.locator("#nav [data-nav='#/search']").get_attribute("aria-label"), "Search")
        self.assertEqual(self.page.locator("#nav [data-nav='#/settings']").get_attribute("aria-label"), "Settings")

    def test_the_library_names_the_server_gives_are_translated(self):
        self.open()
        links = self.page.locator("#nav .link").all_inner_texts()
        self.assertIn("Movies", links)
        self.assertIn("Music", links)
        self.assertNotIn("Films", links)
        self.assertNotIn("Musique", links)
        self.open("#/l/films")
        self.assertEqual(self.view("h1").inner_text(), "Movies")

    def test_counts_follow_the_english_plural_rule(self):
        self.open()
        self.assertEqual(self.page.evaluate("[Hub.plural(0, 'film'), Hub.plural(1, 'film'), Hub.plural(2, 'film'), Hub.plural(1, 'série'), Hub.plural(3, 'série')]"),
                         ["0 movies", "1 movie", "2 movies", "1 series", "3 series"])

    def test_numbers_and_dates_follow_the_language(self):
        self.open()
        self.assertEqual(self.page.evaluate("Hub.number(1234567)"), "1,234,567")
        self.assertEqual(self.page.evaluate("Hub.date(0)"), "01/01/1970")

    def test_a_text_the_server_wrote_is_translated(self):
        self.open()
        tr = lambda text: self.page.evaluate("t => Hub.tr(t)", text)
        self.assertEqual(tr("fichier trop gros (au plus 20 Mo)"), "file too large (20 MB at most)")
        self.assertEqual(tr("élément inconnu"), "unknown item")
        self.assertEqual(tr("Mise à jour · Musique"), "Update · Music")
        self.assertEqual(tr("Enrichissement TMDB · Séries"), "TMDB enrichment · TV shows")
        self.assertEqual(tr("  Dune : série introuvable sur TMDB"), "  Dune: series not found on TMDB")
        self.assertEqual(tr("12 fichier(s) de ce genre (simulation : rien n'a été effacé)."), "12 file(s) of this kind (dry run: nothing was deleted).")
        self.assertEqual(tr("depuis 1993"), "since 1993")
        self.assertEqual(tr("Un message que le serveur est seul à connaître"), "Un message que le serveur est seul à connaître")
        self.assertEqual(self.page.evaluate("Hub.tr('')"), "")

    def test_the_most_specific_server_template_wins(self):
        self.open()
        tr = lambda text: self.page.evaluate("t => Hub.tr(t)", text)
        self.assertEqual(tr("  Dune : 3 film(s), 1 absent(s)"), "  Dune: 3 movie(s), 1 absent")
        self.assertEqual(tr("  Dune : 3 film(s), 1 absent(s), 2 pas encore sorti(s)"), "  Dune: 3 movie(s), 1 absent, 2 not released yet")

    def test_an_error_from_the_server_reaches_the_screen_in_english(self):
        self.open("#/i/unknown")
        self.view(".blank p").wait_for()
        self.assertEqual(self.view(".blank h1").inner_text(), "Not found")
        self.assertEqual(self.view(".blank p").inner_text(), "unknown item")

    def test_the_main_pages_have_every_text(self):
        for path in ("#/", "#/l/films", "#/l/series", "#/l/animations", "#/l/musique", "#/l/livres", "#/search", "#/playlists", "#/sagas",
                     "#/settings?tab=gestion", "#/settings?tab=preferences", "#/settings?tab=comptes"):
            with self.subTest(path=path):
                self.open(path)
                self.page.wait_for_timeout(250)
                self.assertEqual(self.missing(), [])
        lib = self.api("/api/state")["libraries"]
        for library in lib:
            first = self.api(f"/api/library/{library['id']}")["items"]
            if first:
                with self.subTest(item=first[0]["title"]):
                    self.open("#/i/" + first[0]["id"])
                    self.page.wait_for_timeout(250)
                    self.assertEqual(self.missing(), [])

    def test_the_settings_page_is_in_english(self):
        self.open("#/settings")
        self.assertEqual(self.view("h1").inner_text(), "Settings")
        self.assertEqual(self.view(".tabs .tab").all_inner_texts(), ["Management", "Preferences", "Accounts"])
        self.assertIn("Libraries", self.view(".panel h2").all_inner_texts())

    def test_the_language_is_chosen_in_the_settings_and_kept(self):
        context = self.browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        try:
            page.goto(self.base + "/#/settings?tab=preferences")
            page.wait_for_selector("#nav .link", state="attached")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "fr")
            self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Paramètres")
            page.locator("#views > .view:visible button[data-key=lang]").click()
            self.assertEqual(page.locator(".menu .opt .lbl").all_inner_texts(), ["Français", "English"])
            with page.expect_navigation():
                page.locator(".menu .opt .lbl", has_text="English").click()
            page.wait_for_selector("#nav .link", state="attached")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
            self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Settings")
            self.assertIn("hub_lang=en", page.evaluate("document.cookie"))
            page.reload()
            page.wait_for_selector("#nav .link", state="attached")
            self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Settings")
            page.locator("#views > .view:visible button[data-key=lang]").click()
            with page.expect_navigation():
                page.locator(".menu .opt .lbl", has_text="Français").click()
            page.wait_for_selector("#nav .link", state="attached")
            self.assertEqual(page.locator("#views > .view:visible h1").inner_text(), "Paramètres")
        finally:
            context.close()

    def test_the_login_page_follows_the_cookie(self):
        for cookie, lang, word, button in (("hub_lang=en", "en", "Password", "Sign in"), ("hub_lang=fr", "fr", "Mot de passe", "Entrer"), ("", "fr", "Mot de passe", "Entrer")):
            with self.subTest(cookie=cookie):
                req = urllib.request.Request(self.base + "/login", headers={"Cookie": cookie} if cookie else {})
                html = urllib.request.urlopen(req).read().decode()
                self.assertIn(f'<html lang="{lang}">', html)
                self.assertIn(f'placeholder="{word}"', html)
                self.assertIn(f"<button>{button}</button>", html)

    def test_the_login_page_switches_language_with_a_link(self):
        response = urllib.request.urlopen(self.base + "/login?lang=en")
        self.assertIn("hub_lang=en", response.headers["Set-Cookie"])
        self.assertIn("Media library", response.read().decode())
        response = urllib.request.urlopen(self.base + "/login?lang=klingon")
        self.assertIsNone(response.headers["Set-Cookie"])
        self.assertIn("Médiathèque", response.read().decode())

    def test_the_login_errors_are_in_english(self):
        req = urllib.request.Request(self.base + "/login?error=1", headers={"Cookie": "hub_lang=en"})
        self.assertIn("Wrong name or password.", urllib.request.urlopen(req).read().decode())
        req = urllib.request.Request(self.base + "/login?error=2", headers={"Cookie": "hub_lang=en"})
        self.assertIn("Too many attempts", urllib.request.urlopen(req).read().decode())


if __name__ == "__main__":
    unittest.main()
