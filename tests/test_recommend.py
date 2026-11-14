"""Les recommandations « Parce que tu as aimé <titre> » : des rayons expliqués, tirés seulement des avis et de la progression du compte qui demande, dans les bibliothèques qu'il peut lire."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.rig import MiniHub, call
from tests.test_server_views import entry, item
from hub import accounts, config, recommend
from hub.server import app

PASSWORD = "un mot de passe assez long"
DAY = "2026-11-14"
FILLERS = 30            # des films sans rien en commun : de quoi qu'aucune particularité du jeu d'essai ne dépasse MAX_SHARE


def film(ident, title=None, directors=(), actors=(), genres=(), year="", grp="", lib="films", kind="movie", vote=None, count=1):
    meta = {"directors": list(directors), "actors": [{"name": a, "role": ""} for a in actors]}
    if vote is not None:
        meta["tmdb_rating"] = {"tmdb": 1, "vote": vote, "votes": 1000, "at": 0}
    return item(ident, kind, lib=lib, title=title or ident.title(), meta=meta, year=year, grp=grp, genres=json.dumps(list(genres)), count=count)


# Deux ancres possibles et leurs voisins. Pour « a1 » (Ridley, Sigourney, SF + Horreur, 1979) : un réalisateur partagé pèse plus qu'un acteur,
# qui pèse plus qu'un genre ; « Horreur » (2 films) est plus rare que « SF » (6 films).
FILMS = [
    film("a1", "Alien", ["Ridley"], ["Sigourney", "Tom"], ["SF", "Horreur"], "1979"),
    film("c_dir", "Gladiator", ["Ridley"], [], ["Péplum"], "2000"),
    film("c_act", "Ghostbusters", [], ["Sigourney"], ["Comédie"], "1984"),
    film("c_rare", "Hérédité", [], [], ["Horreur"], "2018"),
    film("c_sf1", "Dune", [], [], ["SF"], "2021"),
    film("c_sf2", "Contact", [], [], ["SF"], "1997"),
    film("c_sf3", "Solaris", [], [], ["SF"], "2002"),
    film("c_sf4", "Tenet", [], [], ["SF"], "2020"),
    film("c_dec", "Rocky", [], [], ["Sport"], "1976"),                       # seulement la décennie : jamais proposé
    film("c_down", "Prometheus", ["Ridley"], [], [], "2012"),
    film("c_seen", "Blade Runner", ["Ridley"], [], [], "1982"),
    film("a2", "Inception", ["Nolan"], ["Leo"], ["Thriller"], "2010"),
    film("n1", "Memento", ["Nolan"], [], [], "2000"),
    film("n2", "Insomnia", ["Nolan"], [], [], "2002"),
    film("n3", "Titanic", [], ["Leo"], [], "1997"),
    film("n4", "Revenant", [], ["Leo"], [], "2015"),
    film("n5", "Seven", [], [], ["Thriller"], "1995"),
    film("a3", "Seul", ["Solo"], [], [], ""),                                 # une ancre sans voisins
    film("s_solo", "Avec Solo", ["Solo"], [], [], ""),
] + [film(f"f{n:02d}", f"Remplissage {n:02d}", genres=[f"Genre {n}"]) for n in range(FILLERS)]
SHOWS = [
    film("s1", "Série Ridley", ["Ridley"], ["Sigourney"], ["SF"], "1979", lib="series", kind="show"),
    film("s2", "Série Nolan", ["Nolan"], ["Leo"], ["Thriller"], "2010", lib="series", kind="show"),
] + [film(f"sh{n}", f"Série {n}", ["Ridley"], [], ["Drame"], "2000", lib="series", kind="show") for n in range(5)] \
  + [film(f"sx{n}", f"Série Nolan {n}", ["Nolan"], [], [], "", lib="series", kind="show") for n in range(5)] \
  + [film(f"sf{n:02d}", f"Remplissage série {n}", genres=[f"G{n}"], lib="series", kind="show") for n in range(20)]
SECRET = [film("p1", "Secret Ridley", ["Ridley"], ["Sigourney"], ["SF"], "1979", lib="prive"),
          film("p2", "Secret Nolan", ["Nolan"], ["Leo"], ["Thriller"], "2010", lib="prive")]


class RecommendCase(unittest.TestCase):
    """Un hub à trois bibliothèques vidéo (films, séries, et « prive », que Bob voit mais ne peut pas lire), un administrateur et deux comptes."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        libs = [config.Library(ident, ident.title(), kind, self.root / ident) for ident, kind in (("films", "movies"), ("series", "shows"), ("prive", "movies"), ("musique", "music"))]
        for lib in libs:
            lib.path.mkdir()
        self.hub = MiniHub(self.root, libs)
        self.db = self.hub.db
        self.boss = accounts.create(self.db, "boss", PASSWORD)
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.bob = accounts.create(self.db, "bob", PASSWORD)
        accounts.set_rights(self.db, self.bob["id"], {lib.id for lib in libs}, {"prive": {"play": False}})
        for lib, rows in (("films", FILMS), ("series", SHOWS), ("prive", SECRET)):
            self.fill(lib, rows)

    def fill(self, lib, rows):
        self.db.replace_library(lib, rows, [entry("v_" + r["id"], r["id"], lib=lib) for r in rows])

    def rate(self, who, *idents, value="up"):
        for n, ident in enumerate(idents):
            self.db.set_rating(who["id"], ident, value)
            self.db.execute("UPDATE ratings SET updated=? WHERE user_id=? AND item_id=?", (1000 + n, who["id"], ident))         # l'ordre des avis : le dernier cité est le plus récent

    def watch(self, who, ident, done=True, when=None):
        self.db.save_progress(who["id"], "v_" + ident, 99 if done else 10, 100, done)
        if when is not None:
            self.db.execute("UPDATE progress SET updated=? WHERE profile=? AND entry_id=?", (when, who["id"], "v_" + ident))

    def reply(self, who, day=DAY):
        headers = self.hub.cookie(accounts.by_id(self.db, who["id"])) if who else {}
        with mock.patch("hub.server.app.today", return_value=day):
            return call(self.hub, "GET", "/api/recommendations", headers=headers)[1]

    def shelves(self, who, day=DAY):
        reply = self.reply(who, day)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.json()["shelves"]

    def by_anchor(self, who, day=DAY):
        return {s["anchor"]["id"]: [i["id"] for i in s["items"]] for s in self.shelves(who, day)}


class TestOrder(RecommendCase):
    def test_a_shared_director_beats_an_actor_which_beats_a_rare_genre_which_beats_a_common_one(self):
        self.rate(self.alice, "a1", "a2")
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertEqual(shelf[0], "p1")                                         # réalisateur, acteur, genre et décennie en commun
        self.assertLess(shelf.index("c_dir"), shelf.index("c_act"))
        self.assertLess(shelf.index("c_act"), shelf.index("c_rare"))
        self.assertLess(shelf.index("c_rare"), shelf.index("c_sf1"))
        self.assertEqual([i for i in shelf if i.startswith("c_sf")], ["c_sf2", "c_sf1", "c_sf3", "c_sf4"])          # même proximité : par titre (Contact, Dune, Solaris, Tenet)

    def test_the_answer_has_the_anchor_the_card_and_the_two_heaviest_reasons(self):
        self.rate(self.alice, "a1", "a2")
        shelf = next(s for s in self.shelves(self.alice) if s["anchor"]["id"] == "a1")
        self.assertEqual(shelf["anchor"], {"id": "a1", "title": "Alien", "year": "1979", "type": "movie"})
        cards = {c["id"]: c for c in shelf["items"]}
        self.assertEqual(cards["c_dir"]["why"], [{"kind": "director", "value": "Ridley"}])
        self.assertEqual(cards["p1"]["why"], [{"kind": "director", "value": "Ridley"}, {"kind": "actor", "value": "Sigourney"}])     # trois en commun, les deux plus lourdes
        self.assertEqual(cards["c_rare"]["why"], [{"kind": "genre", "value": "Horreur"}])
        self.assertEqual({k: cards["c_dir"][k] for k in ("title", "type", "year", "cover")}, {"title": "Gladiator", "type": "movie", "year": "2000", "cover": app.urls.item_cover("c_dir")})

    def test_a_feature_carried_by_too_many_items_is_ignored(self):
        common = [film(f"d{n:02d}", f"Drame {n:02d}", genres=["Drame"]) for n in range(30)]
        self.fill("films", FILMS + common)                       # « Drame » sur 30 films sur 81 : sous le seuil
        self.rate(self.alice, "a2", "d00")
        self.assertIn("d00", self.by_anchor(self.alice))
        drama = [film("a2", "Inception", ["Nolan"], ["Leo"], ["Thriller", "Drame"], "2010"), film("dn", "Drame Nolan", ["Nolan"], [], ["Drame"], ""),
                 film("d01", "Drame 01", genres=["Drame"], vote=9.0)]
        rows = {r["id"]: r for r in FILMS + common + [film(f"e{n:02d}", f"Encore {n:02d}", genres=["Drame"]) for n in range(30)] + drama}
        self.fill("films", list(rows.values()))                  # 61 sur 112 : au-dessus de 40 %
        shelves = self.shelves(self.alice)
        self.assertEqual([s["anchor"]["id"] for s in shelves], ["a2"])
        cards = {c["id"]: c for c in shelves[0]["items"]}
        self.assertEqual(cards["dn"]["why"], [{"kind": "director", "value": "Nolan"}])         # « Drame » n'explique rien
        self.assertNotIn("d01", cards)                                                         # ni ne rapproche, même un film bien noté

    def test_a_feature_on_exactly_forty_percent_still_counts(self):
        group = ("films", "movie")
        idx = {"items": {}, "counts": {group: {("genre", "x"): 4, ("genre", "y"): 5}}, "sizes": {group: 10}}
        scope = recommend.Scope(idx, "movie", {}, {"films"})
        self.assertGreater(scope.weight(("genre", "x")), 0)
        self.assertEqual(scope.weight(("genre", "y")), 0)

    def test_the_rating_bonus_follows_the_slope_up_to_its_cap(self):
        self.assertEqual(recommend.quality(None), 0)
        self.assertEqual(recommend.quality(recommend.QUALITY_PIVOT), 0)
        self.assertAlmostEqual(recommend.quality(recommend.QUALITY_PIVOT + 0.5), 0.5 * recommend.QUALITY_SLOPE)
        self.assertEqual(recommend.quality(10), recommend.QUALITY_CAP)
        self.assertEqual(recommend.quality(0), -recommend.QUALITY_CAP)

    def test_between_equals_the_better_rated_comes_first(self):
        self.assertEqual(self.sf_order([film("c_sf2", "Contact", genres=["SF"], year="1997", vote=9.0), film("c_sf4", "Tenet", genres=["SF"], year="2020", vote=9.5)]),
                         ["c_sf4", "c_sf2", "c_sf1", "c_sf3"])                          # bonus plafonné des deux côtés : la note départage avant le titre

    def test_a_decade_alone_is_not_enough(self):
        self.rate(self.alice, "a1", "a2")
        self.assertNotIn("c_dec", self.by_anchor(self.alice)["a1"])

    def sf_order(self, extra=()):
        rows = {r["id"]: r for r in FILMS}
        for row in extra:
            rows[row["id"]] = row
        self.fill("films", list(rows.values()))
        self.rate(self.alice, "a1", "a2")
        return [i for i in self.by_anchor(self.alice)["a1"] if i.startswith("c_sf")]

    def test_a_good_tmdb_rating_lifts_a_bad_one_lowers_but_only_among_equals(self):
        self.assertEqual(self.sf_order([film("c_sf4", "Tenet", genres=["SF"], year="2020", vote=9.0), film("c_sf2", "Contact", genres=["SF"], year="1997", vote=2.0)]),
                         ["c_sf4", "c_sf1", "c_sf3", "c_sf2"])
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertLess(shelf.index("c_rare"), shelf.index("c_sf4"))                # la note départage, elle ne fait pas la proximité

    def test_what_looks_like_a_film_not_liked_goes_down(self):
        order = self.sf_order([film("c_sf1", "Dune", ["Denis"], [], ["SF"], "2021"), film("z", "Raté", ["Denis"], [], ["Western"], "1950"),
                               film("z2", "Autre", ["Denis"], [], [], "")])
        self.assertEqual(order, ["c_sf2", "c_sf1", "c_sf3", "c_sf4"])
        self.rate(self.alice, "z", value="down")
        self.assertEqual([i for i in self.by_anchor(self.alice)["a1"] if i.startswith("c_sf")], ["c_sf2", "c_sf3", "c_sf4", "c_sf1"])

    def test_only_what_is_not_liked_and_of_the_same_kind_weighs_down_not_its_decade(self):
        expected = ["c_sf2", "c_sf1", "c_sf3", "c_sf4"]
        self.sf_order([film("c_sf1", "Dune", ["Denis"], [], ["SF"], "2021"), film("z", "Raté", ["Denis"], [], ["Western"], "1950"),
                       film("y", "Même décennie", [], [], ["Western"], "2020")])
        self.fill("series", SHOWS + [film("zs", "Série de Denis", ["Denis"], [], [], "", lib="series", kind="show")])
        self.rate(self.alice, "z", value="mid")                    # « Bof » n'est pas « Je n'aime pas »
        self.rate(self.alice, "zs", "y", value="down")            # une série, et un film qui ne partage que la décennie de Dune
        self.db.set_rating(self.alice["id"], "disparu", "down")    # un élément disparu : son avis reste, sans effet
        self.assertEqual([i for i in self.by_anchor(self.alice)["a1"] if i.startswith("c_sf")], expected)

    def test_only_the_first_five_actors_count(self):
        cast = ["Un", "Deux", "Trois", "Quatre", "Cinq", "Six"]
        extra = [film("a1", "Alien", ["Ridley"], ["Sigourney", "Tom"] + cast, ["SF", "Horreur"], "1979"),
                 film("k5", "Cinquième", [], ["Cinq"], [], ""), film("k6", "Sixième", [], ["Six"], [], "")]
        self.sf_order(extra)
        self.rate(self.alice, "a1", "a2")
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertNotIn("k5", shelf)                       # « Cinq » est le 7e acteur d'Alien
        self.assertNotIn("k6", shelf)
        extra[0] = film("a1", "Alien", ["Ridley"], cast, ["SF", "Horreur"], "1979")
        self.sf_order(extra)
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertIn("k5", shelf)
        self.assertNotIn("k6", shelf)

    def test_damaged_tags_are_ignored(self):
        broken = film("c_rare", "Hérédité", [], [], [], "2018")
        broken["genres"] = "pas du json"
        odd = film("c_odd", "Bizarre", [], [], [], "")
        odd["genres"] = json.dumps(["Horreur ", 3, "", "horreur"])
        self.sf_order([broken, odd])
        cards = {c["id"]: c for s in self.shelves(self.alice) for c in s["items"]}
        self.assertNotIn("c_rare", cards)
        self.assertEqual(cards["c_odd"]["why"], [{"kind": "genre", "value": "Horreur"}])


class TestWhatIsNeverOffered(RecommendCase):
    def test_never_what_is_not_liked_nor_started_nor_the_anchor(self):
        self.rate(self.alice, "a1", "a2")
        self.rate(self.alice, "c_down", value="down")
        self.watch(self.alice, "c_seen", done=False)
        shelves = self.by_anchor(self.alice)
        self.assertIn("c_dir", shelves["a1"])
        for shelf in shelves.values():
            self.assertNotIn("c_down", shelf)
            self.assertNotIn("c_seen", shelf)
            self.assertNotIn("a1", shelf)
            self.assertNotIn("a2", shelf)

    def test_a_film_anchor_offers_films_and_a_show_anchor_shows(self):
        self.rate(self.alice, "s1", "s2")
        shelves = self.by_anchor(self.alice)
        self.assertEqual(set(shelves), {"s1", "s2"})
        self.assertTrue(all(i.startswith("s") for shelf in shelves.values() for i in shelf), shelves)
        self.rate(self.alice, "a1", "a2")
        for anchor, shelf in self.by_anchor(self.alice).items():
            self.assertEqual({i[0] == "s" and i != "s_solo" for i in shelf}, {anchor.startswith("s")}, (anchor, shelf))

    def test_one_film_per_saga_the_best_placed(self):
        saga = [film("g1", "Saga 1", ["Ridley"], ["Sigourney"], [], "", grp="Saga"), film("g2", "Saga 2", ["Ridley"], [], [], "", grp="Saga")]
        self.fill("films", FILMS + saga)
        self.rate(self.alice, "a1", "a2")
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertIn("g1", shelf)
        self.assertNotIn("g2", shelf)

    def test_an_item_is_never_in_two_shelves(self):
        both = [film(f"b{n}", f"Les deux {n}", ["Ridley", "Nolan"], [], [], "") for n in range(3)]
        self.fill("films", FILMS + both)
        self.rate(self.alice, "a1", "a2")
        shelves = self.by_anchor(self.alice)
        self.assertEqual(len(shelves), 2)
        offered = [i for shelf in shelves.values() for i in shelf]
        self.assertEqual(len(offered), len(set(offered)))
        self.assertTrue(set(offered) >= {"b0", "b1", "b2"})


class TestLibrariesTheAccountCannotPlay(RecommendCase):
    def test_nothing_from_a_library_the_account_cannot_play(self):
        self.rate(self.bob, "a1", "a2")
        for shelf in self.by_anchor(self.bob).values():
            self.assertFalse({"p1", "p2"} & set(shelf), shelf)
        self.rate(self.alice, "a1", "a2")
        self.assertIn("p1", self.by_anchor(self.alice)["a1"])

    def test_an_anchor_from_a_library_the_account_cannot_play_does_not_leak(self):
        self.rate(self.bob, "p1", "p2", "a1")
        self.assertEqual(self.shelves(self.bob), [])                     # une seule ancre lisible
        self.rate(self.alice, "p1", "p2")
        self.assertEqual(set(self.by_anchor(self.alice)), {"p1", "p2"})
        accounts.set_rights(self.db, self.alice["id"], {"films", "series", "prive", "musique"}, {"prive": {"view": False}})
        reply = self.reply(self.alice)
        self.assertEqual(reply.json(), {"shelves": []})
        self.assertNotIn(b"Secret", reply.body)


class TestEnoughSignal(RecommendCase):
    def test_fewer_than_two_anchors_give_no_shelf(self):
        self.assertEqual(self.reply(self.alice).json(), {"shelves": []})
        self.rate(self.alice, "a1")
        self.assertEqual(self.reply(self.alice).json(), {"shelves": []})
        self.rate(self.alice, "a2", value="mid")
        self.watch(self.alice, "a2")                               # vu, mais « Bof » : pas une ancre
        self.assertEqual(self.reply(self.alice).json(), {"shelves": []})

    def test_a_finished_film_without_opinion_is_an_anchor(self):
        self.rate(self.alice, "a1")
        self.watch(self.alice, "a2")
        self.assertEqual(set(self.by_anchor(self.alice)), {"a1", "a2"})

    def test_a_shelf_of_exactly_four_items_is_kept(self):
        self.fill("films", FILMS + [film(f"s_solo{n}", f"Avec Solo {n}", ["Solo"]) for n in range(2, 5)])
        self.rate(self.alice, "a1", "a3")
        self.assertEqual(len(self.by_anchor(self.alice)["a3"]), 4)

    def test_only_the_ten_most_recent_liked_are_anchors(self):
        self.rate(self.alice, "a1", "a2", *[f"f{n:02d}" for n in range(9)])     # a1 est le plus ancien des 11 avis ; les remplissages n'ont aucun voisin
        self.assertEqual(set(self.by_anchor(self.alice)), {"a2"})

    def test_only_the_ten_most_recently_finished_are_anchors(self):
        for when, ident in enumerate(["a1", "a2"] + [f"f{n:02d}" for n in range(9)]):
            self.watch(self.alice, ident, when=1000 + when)
        self.assertEqual(set(self.by_anchor(self.alice)), {"a2"})

    def test_a_film_without_files_is_never_finished(self):
        self.fill("films", [r for r in FILMS if r["id"] != "a2"] + [film("a2", "Inception", ["Nolan"], ["Leo"], ["Thriller"], "2010", count=0)])
        self.rate(self.alice, "a1")
        self.watch(self.alice, "a2", done=False)
        self.assertEqual(self.shelves(self.alice), [])

    def test_a_shelf_under_four_items_is_dropped_and_the_next_anchor_tried(self):
        self.rate(self.alice, "a1", "a3")
        self.assertEqual(set(self.by_anchor(self.alice)), {"a1"})
        self.rate(self.alice, "a2")
        for day in ("2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17"):
            self.assertEqual(set(self.by_anchor(self.alice, day)), {"a1", "a2"}, day)

    def test_at_most_two_shelves_of_twelve(self):
        many = [film(f"r{n:02d}", f"Ridley {n:02d}", ["Ridley"]) for n in range(20)]
        self.fill("films", FILMS + many)
        self.rate(self.alice, "a1", "a2", "s1", "s2")
        shelves = self.shelves(self.alice)
        self.assertEqual(len(shelves), 2)
        self.assertTrue(all(4 <= len(s["items"]) <= 12 for s in shelves))


class TestDays(RecommendCase):
    def setUp(self):
        super().setUp()
        self.rate(self.alice, "a1", "a2", "s1", "s2")

    def test_the_same_all_day_and_another_choice_on_other_days(self):
        self.assertEqual(self.shelves(self.alice, DAY), self.shelves(self.alice, DAY))
        picks = {tuple(s["anchor"]["id"] for s in self.shelves(self.alice, f"2026-12-{d:02d}")) for d in range(1, 21)}
        self.assertGreater(len(picks), 1)


class TestRarityFollowsWhatTheAccountCanPlay(RecommendCase):
    """La rareté d'une particularité (et le seuil MAX_SHARE) se compte dans les bibliothèques que le compte peut lire : une bibliothèque qu'il ne lit pas ne change rien pour lui."""

    def setUp(self):
        super().setUp()
        extra = [film("x1", "Autre 1"), film("x2", "Autre 2")]                                # 51 films sans « Drame »
        self.drama = [film(f"d{n:02d}", f"Drame {n:02d}", genres=["Drame"]) for n in range(34)]  # 34 sur 85 : 40 % pile, encore compté
        self.fill("films", FILMS + extra + self.drama)
        for who in (self.alice, self.bob):
            self.rate(who, "a2", "d00")

    def test_a_library_the_account_cannot_play_never_moves_its_rarity(self):
        before = self.shelves(self.bob)
        self.assertEqual({s["anchor"]["id"] for s in before}, {"a2", "d00"})
        self.assertIn("d00", self.by_anchor(self.alice))
        self.fill("prive", SECRET + [film(f"q{n:02d}", f"Drame privé {n:02d}", genres=["Drame"], lib="prive") for n in range(10)])
        self.assertEqual(self.shelves(self.bob), before)                                  # Bob ne lit pas « prive » : rien ne bouge
        self.assertNotIn("d00", self.by_anchor(self.alice))                               # Alice la lit : 44 sur 97, « Drame » est ignoré
        self.fill("prive", SECRET + [film(f"q{n:02d}", f"Privé {n:02d}", lib="prive") for n in range(200)])
        self.assertEqual(self.shelves(self.bob), before)                                  # ni quand elle grossit sans « Drame »


class TestAccountsAreSeparate(RecommendCase):
    def test_another_account_never_changes_the_result(self):
        self.rate(self.alice, "a1", "a2")
        before = self.shelves(self.alice)
        self.rate(self.bob, "c_dir", "c_act", "n1", value="down")
        self.rate(self.bob, "a3", "s_solo", "c_dec")
        for ident in ("c_rare", "n2", "c_sf1", "a1"):
            self.watch(self.bob, ident)
        self.assertEqual(self.shelves(self.alice), before)
        self.assertNotEqual(self.shelves(self.bob), [])
        self.assertEqual(self.shelves(self.boss), [])

    def test_the_route_needs_an_account(self):
        reply = self.reply(None)
        self.assertEqual(reply.status, 401)


class TestCost(RecommendCase):
    def statements(self, who):
        found, conn = [], self.db.connection()
        conn.set_trace_callback(found.append)
        try:
            self.shelves(who)
        finally:
            conn.set_trace_callback(None)
        return found

    def test_opinions_and_progress_are_read_once_and_items_once_until_a_scan(self):
        self.rate(self.alice, "a1", "a2")
        first = self.statements(self.alice)
        self.assertEqual(sum("FROM items" in s for s in first), 1, first)
        self.assertEqual(sum("FROM ratings" in s for s in first), 1, first)
        self.assertEqual(sum("FROM progress" in s for s in first), 1, first)
        again = self.statements(self.alice)
        self.assertEqual(sum("FROM items" in s for s in again), 0, again)
        self.assertEqual(len([s for s in again if "FROM users" not in s]), 3, again)            # hors connexion : les droits, les avis, la progression

    def test_the_index_is_renewed_after_a_scan(self):
        self.rate(self.alice, "a1", "a2")
        self.assertNotIn("new", self.by_anchor(self.alice)["a1"])
        self.fill("films", FILMS + [film("new", "Nouveau Ridley", ["Ridley"], ["Sigourney"])])
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertLess(shelf.index("new"), shelf.index("c_dir"))

    def test_the_index_is_renewed_after_an_enrichment(self):
        self.rate(self.alice, "a1", "a2")
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertLess(shelf.index("c_act"), shelf.index("c_sf1"))
        self.db.update_item_meta("c_sf1", meta={"directors": ["Ridley"]})          # ce qu'écrit l'enrichissement TMDB
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertLess(shelf.index("c_sf1"), shelf.index("c_act"))


class TestEmptyAndDegenerate(RecommendCase):
    """Les comptes et les médiathèques sans matière : une réponse vide, jamais une erreur."""

    def assertEmpty(self, who):
        reply = self.reply(who)
        self.assertEqual((reply.status, reply.json()), (200, {"shelves": []}), reply.body)

    def test_an_account_without_any_opinion_or_progress_gets_an_empty_answer(self):
        self.assertEmpty(self.alice)

    def test_an_account_without_opinion_but_with_two_finished_films_gets_shelves_from_them(self):
        self.watch(self.alice, "a1")
        self.watch(self.alice, "a2")
        self.assertEqual(set(self.by_anchor(self.alice)), {"a1", "a2"})

    def test_an_account_that_finished_everything_has_nothing_left_to_offer(self):
        self.rate(self.alice, "a1", "a2")
        for ident in [r["id"] for r in FILMS + SHOWS + SECRET]:
            self.watch(self.alice, ident)
        self.assertEmpty(self.alice)

    def test_an_account_that_started_every_neighbour_has_nothing_left_to_offer(self):
        self.rate(self.alice, "a1", "a2")
        for ident in [r["id"] for r in FILMS + SHOWS + SECRET if r["id"] not in ("a1", "a2")]:
            self.watch(self.alice, ident, done=False)
        self.assertEmpty(self.alice)

    def test_an_empty_hub_answers_an_empty_shelf_list_even_with_opinions_left_behind(self):
        self.rate(self.alice, "a1", "a2")
        self.watch(self.alice, "s1")
        for lib in ("films", "series", "prive"):
            self.fill(lib, [])
        self.assertEmpty(self.alice)

    def test_an_empty_library_does_not_disturb_the_others(self):
        self.rate(self.alice, "s1", "s2")
        self.fill("films", [])
        shelves = self.by_anchor(self.alice)
        self.assertEqual(set(shelves), {"s1", "s2"})
        self.assertTrue(all(i.startswith("s") for shelf in shelves.values() for i in shelf), shelves)

    def test_an_account_that_cannot_play_any_video_library_gets_an_empty_answer(self):
        self.rate(self.bob, "a1", "a2", "s1", "s2")
        accounts.set_rights(self.db, self.bob["id"], {"films", "series", "prive", "musique"}, {lib: {"play": False} for lib in ("films", "series", "prive")})
        self.assertEmpty(self.bob)


class TestUnreadableTags(RecommendCase):
    """Un `meta` absent, cassé ou d'un autre type que celui qu'écrit le scan : traité comme un élément sans étiquette de personnes, sans casser le reste."""

    BASELINE = ["c_sf2", "c_sf1", "c_sf3", "c_sf4"]                 # l'ordre des films de SF d'Alien quand rien ne les distingue (voir TestOrder)
    NOT_AN_OBJECT = ["null", "[]", '"texte"', "42", "true"]          # du JSON valide, mais pas un objet
    NO_PEOPLE = ["", "pas du json", '{"directors": ["Ridley"', "{}",
                 '{"directors": "Ridley", "actors": "Sigourney"}',                    # des chaînes : on ne les découpe pas en lettres
                 '{"directors": 42, "actors": 7}',
                 '{"directors": {"name": "Ridley"}, "actors": {"Sigourney": 1}}',
                 '{"directors": null, "actors": null}',
                 '{"directors": [42, null, {"name": "Ridley"}, ["Ridley"], true, "  "], "actors": [null, 3, {"role": "x"}, {"name": 5}, [], ""]}',
                 '{"tmdb_rating": "8"}', '{"tmdb_rating": null}', '{"tmdb_rating": []}', '{"tmdb_rating": {}}']

    def with_raw_meta(self, ident, raw):
        rows = {r["id"]: r for r in FILMS}
        rows[ident] = {**rows[ident], "meta": raw}
        self.fill("films", list(rows.values()))

    def test_a_candidate_without_readable_people_still_counts_by_its_genres(self):
        self.rate(self.alice, "a1", "a2")
        for raw in self.NO_PEOPLE:
            with self.subTest(meta=raw):
                self.with_raw_meta("c_sf1", raw)
                cards = {c["id"]: c for s in self.shelves(self.alice) if s["anchor"]["id"] == "a1" for c in s["items"]}
                self.assertEqual(cards["c_sf1"]["why"], [{"kind": "genre", "value": "SF"}])
                self.assertEqual([i for i in cards if i.startswith("c_sf")], self.BASELINE)

    def test_a_candidate_whose_meta_is_not_an_object_is_still_offered(self):
        """Un `meta` JSON valide qui n'est pas un objet (`null`, liste, texte, nombre, booléen) se lit comme vide : l'élément reste proposé, sans 500."""
        self.rate(self.alice, "a1", "a2")
        for raw in self.NOT_AN_OBJECT:
            with self.subTest(meta=raw):
                self.with_raw_meta("c_sf1", raw)
                cards = {c["id"]: c for s in self.shelves(self.alice) if s["anchor"]["id"] == "a1" for c in s["items"]}
                self.assertEqual(cards["c_sf1"]["why"], [{"kind": "genre", "value": "SF"}])

    def test_an_anchor_without_readable_people_still_has_a_shelf_by_its_genres(self):
        self.rate(self.alice, "a1", "a2")
        for raw in self.NO_PEOPLE + self.NOT_AN_OBJECT:
            with self.subTest(meta=raw):
                self.with_raw_meta("a1", raw)
                shelf = self.by_anchor(self.alice)["a1"]
                self.assertTrue({"c_sf1", "c_sf2", "c_sf3", "c_sf4"} <= set(shelf), shelf)
                self.assertNotIn("c_dir", shelf)                    # Ridley n'est plus lisible chez l'ancre
                self.assertNotIn("c_act", shelf)

    def test_readable_names_among_unreadable_ones_are_kept(self):
        self.rate(self.alice, "a1", "a2")
        self.with_raw_meta("c_sf1", json.dumps({"directors": [42, None, "Ridley"], "actors": [None, 3, {"name": "Sigourney"}]}))
        cards = {c["id"]: c for s in self.shelves(self.alice) if s["anchor"]["id"] == "a1" for c in s["items"]}
        self.assertEqual(cards["c_sf1"]["why"], [{"kind": "director", "value": "Ridley"}, {"kind": "actor", "value": "Sigourney"}])

    def test_a_film_with_no_tag_at_all_is_an_anchor_without_neighbours_and_never_offered(self):
        self.fill("films", FILMS + [{**film("bare", "Sans rien"), "meta": "", "genres": "", "year": "", "grp": ""}])
        self.rate(self.alice, "a1", "a2", "bare")
        shelves = self.by_anchor(self.alice)
        self.assertEqual(set(shelves), {"a1", "a2"})
        self.assertTrue(all("bare" not in shelf for shelf in shelves.values()))

    def test_a_tmdb_rating_with_a_text_vote_does_not_break_the_answer(self):
        """Une note TMDB dont `vote` ou `votes` n'est pas un nombre (texte, liste, booléen, NaN, infini) n'est pas montrée et ne casse rien."""
        self.rate(self.alice, "a1", "a2")
        for raw in ('{"tmdb_rating": {"vote": "9.0", "votes": 1000}}', '{"tmdb_rating": {"vote": "9.0", "votes": "1000"}}', '{"tmdb_rating": {"vote": [9], "votes": 1000}}',
                    '{"tmdb_rating": {"vote": NaN, "votes": 1000}}', '{"tmdb_rating": {"vote": Infinity, "votes": 1000}}', '{"tmdb_rating": {"vote": true, "votes": 1000}}',
                    '{"tmdb_rating": {"vote": 9.0, "votes": true}}', '{"tmdb_rating": {"vote": 9.0, "votes": 1000.5}}'):
            with self.subTest(meta=raw):
                self.with_raw_meta("c_sf1", raw)
                reply = self.reply(self.alice)
                self.assertEqual(reply.status, 200, reply.body)
                cards = {c["id"]: c for s in reply.json()["shelves"] for c in s["items"]}          # NaN refusé : la réponse reste du JSON strict
                self.assertNotIn("tmdb_rating", cards["c_sf1"])                                  # une note illisible n'est pas montrée


class TestSameTitles(RecommendCase):
    """À proximité égale et titre égal, l'identifiant départage : le résultat ne dépend pas de l'ordre d'insertion."""

    def twins(self, ids):
        return [film(i, "Pareil", genres=["SF"], year="2020") for i in ids]

    def test_identical_titles_are_ordered_by_identifier(self):
        base = [r for r in FILMS if not r["id"].startswith("c_sf")]
        self.fill("films", base + self.twins(["t3", "t1", "t4", "t2"]))
        self.rate(self.alice, "a1", "a2")
        self.assertEqual([i for i in self.by_anchor(self.alice)["a1"] if i.startswith("t")], ["t1", "t2", "t3", "t4"])

    def test_the_answer_does_not_depend_on_the_order_the_items_were_stored(self):
        rows = FILMS + self.twins(["t1", "t2", "t3", "t4"]) + [film("g1", "Saga", ["Ridley"], grp="Saga"), film("g2", "Saga", ["Ridley"], grp="Saga")]
        self.rate(self.alice, "a1", "a2", "t1")
        self.fill("films", rows)
        forward = self.shelves(self.alice)
        self.fill("films", rows[::-1])
        self.assertEqual(self.shelves(self.alice), forward)

    def test_one_of_two_saga_twins_wins_by_identifier(self):
        self.fill("films", FILMS + [film("g2", "Saga", ["Ridley"], grp="Saga"), film("g1", "Saga", ["Ridley"], grp="Saga")])
        self.rate(self.alice, "a1", "a2")
        shelf = self.by_anchor(self.alice)["a1"]
        self.assertIn("g1", shelf)
        self.assertNotIn("g2", shelf)

    def test_two_anchors_with_the_same_title_are_both_offered_in_a_fixed_order(self):
        rows = [r for r in FILMS if r["id"] != "a2"] + [film("a2", "Alien", ["Nolan"], ["Leo"], ["Thriller"], "2010")]
        self.fill("films", rows)
        self.rate(self.alice, "a1", "a2")
        first = self.shelves(self.alice)
        self.assertEqual({s["anchor"]["id"] for s in first}, {"a1", "a2"})
        self.fill("films", rows[::-1])
        self.assertEqual(self.shelves(self.alice), first)


class TestVolume(RecommendCase):
    COUNT = 5000
    BUDGET = 0.5            # secondes : large (la cible est sous 0,1 s), pour qu'un poste lent ne rende pas le test instable

    def test_five_thousand_films_answer_within_budget_once_indexed(self):
        rows = [film(f"v{n:04d}", f"Film {n:04d}", [f"Réal {n % 400}"], [f"Acteur {(n * 7 + k) % 1500}" for k in range(8)], [f"Genre {n % 19}", f"Genre {n % 7 + 19}"],
                     str(1950 + n % 75), grp=f"Saga {n // 3}" if n % 5 == 0 else "", vote=4 + n % 6) for n in range(self.COUNT)]
        self.fill("films", rows)
        with self.db.write_lock:
            conn = self.db.connection()
            conn.executemany("INSERT INTO ratings(user_id, item_id, rating, updated) VALUES(?,?,?,?)",
                             [(self.alice["id"], f"v{n:04d}", ("up", "down", "mid")[n % 3], n) for n in range(0, self.COUNT, 37)])
            conn.executemany("INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES(?,?,?,?,?,?)",
                             [(self.alice["id"], f"v_v{n:04d}", 99, 100, 1, n) for n in range(0, self.COUNT, 11)])
            conn.commit()
        start = time.perf_counter()
        first = self.shelves(self.alice)
        built = time.perf_counter() - start
        start = time.perf_counter()
        again = self.shelves(self.alice)
        answered = time.perf_counter() - start
        self.assertEqual(first, again)
        self.assertEqual(len(first), 2)
        self.assertLess(answered, self.BUDGET)
        print(f"\n[recommandations, {self.COUNT} films] premier appel (index construit) : {built * 1000:.0f} ms ; appel suivant : {answered * 1000:.0f} ms")


if __name__ == "__main__":
    unittest.main()
