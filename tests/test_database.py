"""La base SQLite : sa création et ses migrations, le remplacement d'une bibliothèque, l'enrichissement d'un item et la progression."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hub import db as database
from hub.db import Database


def item(ident, title="Titre", lib="lib", **over):
    return {"id": ident, "lib": lib, "type": "album", "title": title, "sort": title.casefold(), "letter": title[:1].upper(), "year": "", "summary": "", "genres": "[]", "grp": "",
            "meta": "{}", "added": 0, "count": 1, **over}


def entry(ident, item_id, rel=None, lib="lib", **over):
    return {"id": ident, "item_id": item_id, "lib": lib, "rel": rel or ident, "kind": "audio", "season": None, "disc": None, "number": None, "title": "", "summary": "",
            "duration": 0.0, "size": 10, "mtime": 20, "meta": "{}", "probe": None, **over}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = self.root / "hub.db"
        self.db = Database(self.path)


class TestOpening(TempCase):
    def test_the_folders_are_created_and_may_already_exist(self):
        deep = self.root / "a" / "b" / "hub.db"
        Database(deep)
        Database(deep)
        self.assertTrue(deep.is_file())

    def test_a_new_base_knows_its_version(self):
        self.assertEqual(self.db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertEqual(Database(self.path).kv_get("schema"), database.SCHEMA_VERSION)

    def test_a_base_that_is_newer_than_the_hub_is_left_alone_and_the_user_is_told(self):
        self.db.kv_set("schema", database.SCHEMA_VERSION + 5)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = Database(self.path)
        self.assertIn(f"la base est en version {database.SCHEMA_VERSION + 5}", err.getvalue())
        self.assertEqual(again.kv_get("schema"), database.SCHEMA_VERSION + 5)

    def test_each_missing_version_is_migrated_in_order_and_remembered(self):
        self.db.kv_set("schema", database.SCHEMA_VERSION - 1)
        done = []
        with mock.patch.object(database, "MIGRATIONS", {n: (lambda conn, n=n: done.append(n)) for n in range(1, database.SCHEMA_VERSION + 1)}):
            Database(self.path)
        self.assertEqual(done, [database.SCHEMA_VERSION])                  # celles d'avant sont déjà faites
        self.assertEqual(Database(self.path).kv_get("schema"), database.SCHEMA_VERSION)

    def test_a_number_that_cannot_be_read_starts_the_migrations_from_the_beginning(self):
        self.db.execute("UPDATE kv SET value=? WHERE key='schema'", ("pas un nombre",))
        done = []
        with mock.patch.object(database, "MIGRATIONS", {n: (lambda conn, n=n: done.append(n)) for n in range(1, database.SCHEMA_VERSION + 1)}):
            Database(self.path)
        self.assertEqual(done, list(range(1, database.SCHEMA_VERSION + 1)))

    def test_a_migration_that_fails_leaves_the_base_as_it_was_and_says_so(self):
        self.db.kv_set("schema", database.SCHEMA_VERSION - 1)

        def boom(conn):
            conn.execute("CREATE TABLE essai(x)")
            raise ValueError("oups")

        with mock.patch.object(database, "MIGRATIONS", {database.SCHEMA_VERSION: boom}), self.assertRaises(RuntimeError) as caught:
            Database(self.path)
        message = str(caught.exception)
        self.assertIn(f"vers la version {database.SCHEMA_VERSION} a échoué (oups)", message)
        self.assertIn(f"la base est restée en version {database.SCHEMA_VERSION - 1}", message)
        self.assertEqual(self.db.kv_get("schema"), database.SCHEMA_VERSION - 1)
        self.assertIsNone(self.db.one("SELECT name FROM sqlite_master WHERE name='essai'"))                    # annulée avec le reste


class TestUploadMigration(TempCase):
    def old_base(self):
        """Une base en version 4 : une table `users` sans `can_upload`, des comptes, de la progression, des droits."""
        path = self.root / "v4.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, pass_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', token_version INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL);
            CREATE TABLE rights(user_id TEXT NOT NULL, lib TEXT NOT NULL, view INTEGER NOT NULL DEFAULT 1, play INTEGER NOT NULL DEFAULT 1, download INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, lib));
            CREATE TABLE progress(profile TEXT NOT NULL, entry_id TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL, PRIMARY KEY(profile, entry_id));
            CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO users VALUES('default', 'boss', 'h1', 'admin', 3, 100), ('abc', 'alice', 'h2', 'user', 1, 200);
            INSERT INTO rights VALUES('abc', 'films', 1, 0, 0);
            INSERT INTO progress VALUES('abc', 'e1', 12.5, 100, 0, 300);
            INSERT INTO kv VALUES('schema', '4'), ('secret', '"s"');""")
        conn.commit()
        conn.close()
        return path

    def test_a_version_4_base_gets_the_column_and_keeps_every_account_and_what_they_watched(self):
        db = Database(self.old_base())
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertEqual([tuple(r) for r in db.query("SELECT id, name, pass_hash, role, token_version, created, can_upload, inbox_dir FROM users ORDER BY id")],
                         [("abc", "alice", "h2", "user", 1, 200, 0, None), ("default", "boss", "h1", "admin", 3, 100, 0, None)])
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [("abc", "e1", 12.5)])
        self.assertEqual([tuple(r) for r in db.query("SELECT user_id, lib, play FROM rights")], [("abc", "films", 0)])
        self.assertEqual(db.kv_get("secret"), "s")

    def test_opening_it_again_changes_nothing(self):
        path = self.old_base()
        Database(path).execute("UPDATE users SET can_upload=1 WHERE id='abc'")
        again = Database(path)
        self.assertEqual(again.one("SELECT can_upload FROM users WHERE id='abc'")["can_upload"], 1)
        self.assertEqual(again.kv_get("schema"), database.SCHEMA_VERSION)

    def test_a_version_5_base_gets_the_folder_column_and_keeps_the_upload_right(self):
        path = self.old_base()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE users ADD COLUMN can_upload INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET can_upload=1 WHERE id='abc'")
        conn.execute("UPDATE kv SET value='5' WHERE key='schema'")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertEqual([tuple(r) for r in db.query("SELECT id, can_upload, inbox_dir FROM users ORDER BY id")], [("abc", 1, None), ("default", 0, None)])
        self.assertEqual(db.kv_get("secret"), "s")

    def test_a_name_that_is_not_an_identifier_is_refused_before_any_sql_runs(self):
        conn = self.db.connection()
        for table, column in (("users; DROP TABLE users", "x"), ("users", "x y"), ("", "x"), ("users", "x) --")):
            with self.assertRaises(ValueError, msg=(table, column)):
                database._add_column(conn, table, column, "INTEGER")
        self.assertIsNotNone(self.db.one("SELECT name FROM sqlite_master WHERE name='users'"))

    def test_a_new_base_has_the_columns_too_and_the_version_is_7(self):
        self.assertEqual(database.SCHEMA_VERSION, 7)
        self.db.execute("INSERT INTO users(id, name, pass_hash, created) VALUES('x', 'x', 'h', 1)")
        self.assertEqual(tuple(self.db.one("SELECT can_upload, inbox_dir FROM users WHERE id='x'")), (0, None))

    def test_a_base_in_version_3_reaches_the_last_version_through_the_other_migrations_too(self):
        path = self.old_base()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE kv SET value='3' WHERE key='schema'")
        conn.execute("DROP TABLE rights")
        conn.execute("CREATE TABLE rights(user_id TEXT NOT NULL, lib TEXT NOT NULL, view INTEGER NOT NULL DEFAULT 1, download INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, lib))")
        conn.commit()
        conn.close()
        db = Database(path)
        self.assertEqual(db.kv_get("schema"), database.SCHEMA_VERSION)
        self.assertIn("inbox_dir", [r["name"] for r in db.query("PRAGMA table_info(users)")])
        self.assertIn("can_upload", [r["name"] for r in db.query("PRAGMA table_info(users)")])
        self.assertIn("play", [r["name"] for r in db.query("PRAGMA table_info(rights)")])


class TestRatingsMigration(TempCase):
    def v6_base(self):
        """Une base en version 6, d'avant les avis : des comptes, de la progression, une playlist, des droits."""
        path = self.root / "v6.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, pass_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', token_version INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL, can_upload INTEGER NOT NULL DEFAULT 0, inbox_dir TEXT);
            CREATE TABLE rights(user_id TEXT NOT NULL, lib TEXT NOT NULL, view INTEGER NOT NULL DEFAULT 1, play INTEGER NOT NULL DEFAULT 1, download INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user_id, lib));
            CREATE TABLE progress(profile TEXT NOT NULL, entry_id TEXT NOT NULL, position REAL NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL, PRIMARY KEY(profile, entry_id));
            CREATE TABLE playlists(id TEXT PRIMARY KEY, profile TEXT NOT NULL, name TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
            CREATE TABLE playlist_tracks(playlist_id TEXT NOT NULL, pos INTEGER NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY(playlist_id, pos));
            CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO users VALUES('default', 'boss', 'h1', 'admin', 3, 100, 0, NULL), ('abc', 'alice', 'h2', 'user', 1, 200, 1, 'alice');
            INSERT INTO rights VALUES('abc', 'films', 1, 0, 0);
            INSERT INTO progress VALUES('abc', 'e1', 12.5, 100, 0, 300);
            INSERT INTO playlists VALUES('p1', 'abc', 'Ma liste', 400, 500);
            INSERT INTO playlist_tracks VALUES('p1', 0, 't1'), ('p1', 1, 't2');
            INSERT INTO kv VALUES('schema', '6'), ('secret', '"s"'), ('dismiss:abc:m1', '300');""")
        conn.commit()
        conn.close()
        return path

    def test_a_version_6_base_gets_the_ratings_table_and_loses_nothing(self):
        db = Database(self.v6_base())
        self.assertEqual(db.kv_get("schema"), 7)
        self.assertEqual(db.query("SELECT * FROM ratings"), [])
        self.assertEqual([tuple(r) for r in db.query("SELECT id, name, can_upload, inbox_dir FROM users ORDER BY id")], [("abc", "alice", 1, "alice"), ("default", "boss", 0, None)])
        self.assertEqual([tuple(r) for r in db.query("SELECT profile, entry_id, position FROM progress")], [("abc", "e1", 12.5)])
        self.assertEqual([tuple(r) for r in db.query("SELECT id, profile, name FROM playlists")], [("p1", "abc", "Ma liste")])
        self.assertEqual([tuple(r) for r in db.query("SELECT pos, entry_id FROM playlist_tracks ORDER BY pos")], [(0, "t1"), (1, "t2")])
        self.assertEqual([tuple(r) for r in db.query("SELECT user_id, lib, play FROM rights")], [("abc", "films", 0)])
        self.assertEqual((db.kv_get("secret"), db.kv_get("dismiss:abc:m1")), ("s", 300))

    def test_the_migration_alone_creates_the_table_and_a_second_opening_keeps_the_ratings(self):
        path = self.v6_base()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        database._create_ratings(conn)
        database._create_ratings(conn)                  # rien ne casse quand la table est déjà là
        conn.close()
        db = Database(path)
        db.set_rating("abc", "m1", "up")
        self.assertEqual(Database(path).ratings_for("abc"), {"m1": "up"})


class TestRatings(TempCase):
    def test_a_rating_is_set_replaced_and_cleared_per_account(self):
        self.db.set_rating("a", "m1", "up")
        self.db.set_rating("b", "m1", "down")
        self.db.set_rating("a", "m2", "mid")
        self.assertEqual((self.db.ratings_for("a"), self.db.ratings_for("b"), self.db.ratings_for("c")), ({"m1": "up", "m2": "mid"}, {"m1": "down"}, {}))
        self.db.set_rating("a", "m1", "down")
        self.assertEqual(self.db.ratings_for("a"), {"m1": "down", "m2": "mid"})
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM ratings WHERE user_id='a' AND item_id='m1'")["n"], 1)
        self.db.set_rating("a", "m1", None)
        self.db.set_rating("a", "nope", None)
        self.assertEqual((self.db.ratings_for("a"), self.db.ratings_for("b")), ({"m2": "mid"}, {"m1": "down"}))

    def test_only_the_three_values_are_kept_by_the_code_and_by_the_base(self):
        for value in ("excellent", "", "UP", 1):
            with self.assertRaises(ValueError, msg=value):
                self.db.set_rating("a", "m1", value)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO ratings(user_id, item_id, rating, updated) VALUES('a', 'm1', 'super', 1)")
        self.assertEqual(self.db.ratings_for("a"), {})

    def test_a_scan_keeps_the_ratings_even_of_an_item_that_is_gone(self):
        self.db.set_rating("a", "m1", "up")
        self.db.replace_library("lib", [], [])
        self.assertEqual(self.db.ratings_for("a"), {"m1": "up"})


class TestKeyValue(TempCase):
    def test_a_value_comes_back_as_it_was_stored(self):
        self.db.kv_set("a", {"x": [1, "é"]})
        self.assertEqual(self.db.kv_get("a"), {"x": [1, "é"]})
        self.db.kv_set("a", 2)
        self.assertEqual(self.db.kv_get("a"), 2)

    def test_a_missing_key_gives_the_default(self):
        self.assertIsNone(self.db.kv_get("absent"))
        self.assertEqual(self.db.kv_get("absent", 7), 7)

    def test_accents_are_stored_as_they_are(self):
        self.db.kv_set("a", "é")
        self.assertEqual(self.db.one("SELECT value FROM kv WHERE key='a'")["value"], '"é"')


class TestReplaceLibrary(TempCase):
    def fill(self, lib="lib", **over):
        self.db.replace_library(lib, [item("i1", lib=lib)], [entry("e1", "i1", lib=lib, size=10, mtime=20, **over)])

    def test_the_library_is_replaced_and_the_others_are_not(self):
        self.db.replace_library("a", [item("a1", lib="a")], [entry("ea1", "a1", lib="a")])
        self.db.replace_library("b", [item("b1", lib="b")], [entry("eb1", "b1", lib="b")])
        self.db.replace_library("a", [item("a2", lib="a")], [entry("ea2", "a2", "x.flac", lib="a")])
        self.assertEqual(list(self.db.known_items("a")), ["a2"])
        self.assertEqual(list(self.db.known_entries("a")), ["x.flac"])
        self.assertEqual(list(self.db.known_items("b")), ["b1"])

    def test_the_probe_is_kept_when_neither_the_size_nor_the_date_changed(self):
        self.fill()
        self.db.set_probe("e1", {"duration": 5})
        self.db.replace_library("lib", [item("i1")], [entry("e1", "i1", size=10, mtime=20)])
        self.assertEqual(json.loads(self.db.known_entries("lib")["e1"]["probe"]), {"duration": 5})

    def test_the_probe_is_dropped_when_the_size_or_the_date_changed(self):
        for change in ({"size": 11, "mtime": 20}, {"size": 10, "mtime": 21}, {"size": 20, "mtime": 10}):
            self.fill()
            self.db.set_probe("e1", {"duration": 5})
            self.db.replace_library("lib", [item("i1")], [entry("e1", "i1", **change)])
            self.assertIsNone(self.db.known_entries("lib")["e1"]["probe"], change)

    def test_a_new_file_has_no_probe(self):
        self.fill()
        self.db.replace_library("lib", [item("i1")], [entry("e9", "i1")])
        self.assertIsNone(self.db.known_entries("lib")["e9"]["probe"])

    def test_a_failure_leaves_the_library_as_it_was(self):
        self.fill()
        with self.assertRaises(Exception):
            self.db.replace_library("lib", [item("i2")], [{"id": "incomplet"}])
        self.assertEqual(list(self.db.known_items("lib")), ["i1"])

    def test_progress_of_files_that_disappeared_is_kept_for_a_while_then_removed(self):
        self.fill()
        now = int(time.time())
        day = 86400
        for entry_id, age in (("gone-recent", 89), ("gone-old", 91), ("e1", 365)):
            self.db.execute("INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES('p', ?, 1, 2, 0, ?)", (entry_id, now - age * day))
        self.db.replace_library("lib", [item("i1")], [entry("e1", "i1")])
        self.assertEqual(sorted(self.db.progress_for("p", ["gone-recent", "gone-old", "e1"])), ["e1", "gone-recent"])


class TestUpdateItemMeta(TempCase):
    def setUp(self):
        super().setUp()
        self.db.replace_library("lib", [item("i1", meta='{"a": 1, "b": 2}')], [entry("e1", "i1")])

    def row(self):
        return self.db.known_items("lib")["i1"]

    def test_the_meta_is_merged_and_the_other_columns_replaced(self):
        self.db.update_item_meta("i1", meta={"b": 3, "c": "é"}, summary="Résumé", year="2001", genres=["Rock", "Électro"])
        row = self.row()
        self.assertEqual(json.loads(row["meta"]), {"a": 1, "b": 3, "c": "é"})
        self.assertEqual((row["summary"], row["year"], json.loads(row["genres"])), ("Résumé", "2001", ["Rock", "Électro"]))

    def test_accents_are_stored_as_they_are(self):
        self.db.update_item_meta("i1", meta={"c": "é"}, genres=["É"])
        row = self.row()
        self.assertIn("é", row["meta"])
        self.assertIn("É", row["genres"])

    def test_a_column_that_is_not_allowed_is_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(ValueError, "colonne inconnue : id, lib"):
            self.db.update_item_meta("i1", lib="autre", id="x", title="Nouveau")
        self.assertEqual(self.row()["title"], "Titre")

    def test_an_unknown_item_is_ignored(self):
        self.db.update_item_meta("absent", title="x")
        self.assertEqual(list(self.db.known_items("lib")), ["i1"])

    def test_nothing_to_change_changes_nothing(self):
        self.db.update_item_meta("i1")
        self.assertEqual(self.row()["meta"], '{"a": 1, "b": 2}')


class TestProgress(TempCase):
    def test_a_position_is_saved_and_replaced(self):
        self.db.save_progress("p", "e1", 12.5, 100, False)
        self.db.save_progress("p", "e1", 40, 100, True)
        row = self.db.progress_for("p", ["e1"])["e1"]
        self.assertEqual((row["position"], row["duration"], row["done"]), (40, 100, 1))
        self.assertAlmostEqual(row["updated"], time.time(), delta=5)

    def test_done_is_stored_as_one_or_zero(self):
        self.db.save_progress("p", "a", 1, 2, True)
        self.db.save_progress("p", "b", 1, 2, False)
        found = self.db.progress_for("p", ["a", "b"])
        self.assertEqual((found["a"]["done"], found["b"]["done"]), (1, 0))

    def test_each_profile_has_its_own(self):
        self.db.save_progress("p", "e1", 1, 2, False)
        self.assertEqual(self.db.progress_for("q", ["e1"]), {})

    def test_a_long_list_of_files_is_read_in_pieces(self):
        ids = [f"e{n}" for n in range(1201)]
        for n in (0, 499, 500, 1200):
            self.db.save_progress("p", ids[n], n, 2, False)
        found = self.db.progress_for("p", ids)
        self.assertEqual(sorted(found, key=lambda k: int(k[1:])), ["e0", "e499", "e500", "e1200"])
        self.assertEqual(self.db.progress_for("p", []), {})


class TestKeyValueDelete(TempCase):
    def test_a_deleted_key_is_gone_and_the_others_stay(self):
        self.db.kv_set("a", 1)
        self.db.kv_set("b", 2)
        self.db.kv_delete("a")
        self.db.kv_delete("absente")
        self.assertEqual((self.db.kv_get("a"), self.db.kv_get("b")), (None, 2))


class TestDerived(TempCase):
    """Ce que le hub tire de la base et garde en mémoire : refait après une écriture de ce dont il dépend, et seulement alors."""

    def setUp(self):
        super().setUp()
        self.db.replace_library("lib", [item("a")], [entry("e1", "a", kind="video")])
        self.builds = []

    def build(self):
        self.builds.append(1)
        return len(self.builds)

    def test_kept_until_the_items_change(self):
        self.assertEqual([self.db.derived("x", self.build) for _ in range(3)], [1, 1, 1])
        self.db.update_item_meta("a", title="Autre")
        self.assertEqual(self.db.derived("x", self.build), 2)

    def test_a_new_probe_renews_only_what_reads_the_probes(self):
        self.assertEqual((self.db.derived("items", self.build), self.db.derived("probes", self.build, probes=True)), (1, 2))
        self.db.set_probe("e1", {"video": None})
        self.assertEqual(self.db.derived("items", self.build), 1)                 # ce qui ne lit que les items n'est pas refait à chaque fichier sondé
        self.assertEqual(self.db.derived("probes", self.build, probes=True), 3)
        self.db.update_item_meta("a", title="Autre")
        self.assertEqual(self.db.derived("probes", self.build, probes=True), 4)  # un scan ou un enrichissement le renouvelle aussi


if __name__ == "__main__":
    unittest.main()
