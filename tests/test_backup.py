"""La sauvegarde et la restauration des données propres au hub (hub.backup) : ce que contient l'archive, sa cohérence pendant que le serveur écrit, la restauration sur une installation vide ou existante, et le refus d'une archive incompatible ou abîmée sans rien toucher. Aussi les commandes `python -m hub backup` / `restore` et la route GET /api/backup."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from hub import __main__ as cli
from hub import __version__, accounts, backup, runlock
from hub.db import SCHEMA_VERSION, Database
from tests.rig import MiniHub, call

PASSWORD = "un-mot-de-passe-long"


def contents(folder):
    """Ce que contient `folder`, sans le fichier du verrou (hub.lock), qui reste après usage."""
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.name != runlock.NAME)


def undeletable(prefix):
    """Un dossier de travail dont le nom commence par `prefix` ne peut pas être supprimé (fichier ouvert ailleurs, droits) : le nettoyage échoue, le reste a réussi."""
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        if Path(path).name.startswith(prefix):
            raise PermissionError("occupé")
        return real(path, *args, **kwargs)
    return mock.patch("os.rmdir", rmdir)


def names_in(archive):
    with zipfile.ZipFile(archive) as z:
        return sorted(z.namelist())


def manifest_of(archive):
    with zipfile.ZipFile(archive) as z:
        return json.loads(z.read("manifest.json"))


def rewrite(archive, change):
    """Récrit l'archive en passant chaque (nom, octets) par `change`, qui rend la nouvelle liste de (nom, octets) pour ce membre."""
    with zipfile.ZipFile(archive) as z:
        members = [(name, z.read(name)) for name in z.namelist()]
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in members:
            for new_name, new_data in change(name, data):
                z.writestr(new_name, new_data)


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = Database(self.data / "hub.db")
        self.alice = accounts.create(self.db, "alice", PASSWORD)
        self.db.kv_set("secret", "cle-des-sessions")
        self.db.execute("INSERT INTO progress(profile, entry_id, position, duration, done, updated) VALUES(?, ?, ?, ?, ?, ?)", (self.alice["id"], "e1", 120.0, 3600.0, 0, 1))
        (self.data / "sagas.json").write_text('{"sagas": []}', encoding="utf-8")
        for folder, name, body in (("posters", "film.jpg", b"affiche"), ("artists", "a1.webp", b"photo"), ("img", "cache.jpg", b"cache"), ("tmdb", "film-1.jpg", b"tmdb"),
                                   ("tmdb-cache", "r.json", b"{}"), ("subs", "s.vtt", b"WEBVTT"), ("inbox", "alice/film.mkv", b"envoi")):
            path = self.data / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self.archive = self.root / "sauvegarde.zip"

    def make(self):
        return backup.create(self.data / "hub.db", self.data, self.archive)

    def empty_install(self):
        target = self.root / "neuf"
        target.mkdir()
        return target


class TestCreate(BackupCase):
    def test_an_archive_holds_the_base_the_sagas_the_chosen_posters_and_the_artist_photos_and_nothing_else(self):
        self.make()
        self.assertEqual(names_in(self.archive), ["artists/a1.webp", "hub.db", "manifest.json", "posters/film.jpg", "sagas.json"])

    def test_the_manifest_says_the_hub_version_the_schema_and_each_file_with_its_size_and_checksum(self):
        self.make()
        manifest = manifest_of(self.archive)
        self.assertEqual((manifest["format"], manifest["hub"], manifest["schema"]), (backup.FORMAT, __version__, SCHEMA_VERSION))
        files = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(sorted(files), ["artists/a1.webp", "hub.db", "posters/film.jpg", "sagas.json"])
        self.assertEqual(files["posters/film.jpg"]["size"], len(b"affiche"))
        self.assertEqual(len(files["hub.db"]["sha256"]), 64)

    def test_the_base_is_copied_with_what_the_server_wrote_even_before_a_checkpoint(self):
        self.db.kv_set("tout-juste-ecrit", 1)            # encore dans le journal WAL : une copie du seul fichier hub.db le perdrait
        self.make()
        out = self.root / "copie.db"
        with zipfile.ZipFile(self.archive) as z:
            out.write_bytes(z.read("hub.db"))
        conn = sqlite3.connect(out)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM kv WHERE key='tout-juste-ecrit'").fetchone(), ("1",))

    def test_a_missing_optional_file_is_simply_left_out(self):
        (self.data / "sagas.json").unlink()
        shutil.rmtree(self.data / "posters")
        self.make()
        self.assertEqual(names_in(self.archive), ["artists/a1.webp", "hub.db", "manifest.json"])

    def test_hidden_files_and_writes_in_progress_are_left_out(self):
        for name in (".DS_Store", "film.jpg.123.456.tmp", "a2.webp.part"):
            (self.data / "posters" / name).write_bytes(b"x")
        self.make()
        self.assertEqual(names_in(self.archive), ["artists/a1.webp", "hub.db", "manifest.json", "posters/film.jpg", "sagas.json"])
        target = self.empty_install()
        backup.restore(self.archive, target / "hub.db", target)          # ce que la sauvegarde prend, la restauration l'accepte

    def test_the_folder_of_the_archive_is_created_with_its_parents(self):
        out = self.root / "a" / "b" / "sauvegarde.zip"
        backup.create(self.data / "hub.db", self.data, out)
        self.assertIn("hub.db", names_in(out))

    def test_a_file_name_with_accents_is_written_as_is_in_the_manifest(self):
        (self.data / "posters" / "épée.jpg").write_bytes(b"x")
        self.make()
        with zipfile.ZipFile(self.archive) as z:
            self.assertIn("épée.jpg".encode("utf-8"), z.read("manifest.json"))          # lisible tel quel, pas « épée »

    def test_the_manifest_is_indented_so_that_a_person_can_read_it(self):
        self.make()
        with zipfile.ZipFile(self.archive) as z:
            self.assertTrue(z.read("manifest.json").decode("utf-8").startswith('{\n "format": 1,\n'))

    def test_a_working_folder_that_cannot_be_removed_does_not_fail_a_finished_backup(self):
        with undeletable(".sauvegarde-"):
            manifest = self.make()
        self.assertEqual(manifest["format"], backup.FORMAT)
        self.assertIn("hub.db", names_in(self.archive))

    def test_an_installation_without_a_base_cannot_be_saved(self):
        with self.assertRaises(backup.BackupError):
            backup.create(self.root / "absente.db", self.data, self.archive)
        self.assertFalse(self.archive.exists())


class TestRestore(BackupCase):
    def test_restoring_on_an_empty_installation_gives_back_the_accounts_the_progress_and_the_session_key(self):
        self.make()
        target = self.empty_install()
        backup.restore(self.archive, target / "hub.db", target)
        db = Database(target / "hub.db")
        self.assertEqual([r["name"] for r in accounts.rows(db)], ["alice"])
        self.assertIsNotNone(accounts.authenticate(db, "alice", PASSWORD))
        self.assertEqual(db.kv_get("secret"), "cle-des-sessions")
        self.assertEqual(db.one("SELECT position FROM progress WHERE entry_id='e1'")["position"], 120.0)
        self.assertEqual((target / "posters" / "film.jpg").read_bytes(), b"affiche")
        self.assertEqual((target / "artists" / "a1.webp").read_bytes(), b"photo")
        self.assertEqual((target / "sagas.json").read_text(encoding="utf-8"), '{"sagas": []}')

    def test_the_base_goes_where_it_is_asked_even_in_folders_that_do_not_exist_yet(self):
        self.make()
        target = self.empty_install()
        backup.restore(self.archive, target / "a" / "b" / "hub.db", target)
        self.assertEqual([r["name"] for r in accounts.rows(Database(target / "a" / "b" / "hub.db"))], ["alice"])

    def test_a_working_folder_that_cannot_be_removed_does_not_fail_a_finished_restoration(self):
        self.make()
        target = self.empty_install()
        with undeletable(".restauration-"):
            done = backup.restore(self.archive, target / "hub.db", target)
        self.assertEqual(done["files"], 4)
        self.assertEqual((target / "posters" / "film.jpg").read_bytes(), b"affiche")

    def test_restoring_leaves_no_working_folder_behind(self):
        self.make()
        target = self.empty_install()
        backup.restore(self.archive, target / "hub.db", target)
        self.assertEqual([p for p in contents(target) if "/" not in p], ["artists", "hub.db", "posters", "sagas.json"])

    def test_an_existing_installation_is_not_replaced_without_being_asked(self):
        self.make()
        before = (self.data / "hub.db").read_bytes()
        with self.assertRaises(backup.BackupError) as said:
            backup.restore(self.archive, self.data / "hub.db", self.data)
        self.assertIn("--replace", said.exception.message)
        self.assertEqual((self.data / "hub.db").read_bytes(), before)

    def test_replacing_an_installation_keeps_the_old_files_aside(self):
        self.make()
        accounts.create(self.db, "bob", PASSWORD)              # après la sauvegarde : disparaît à la restauration, reste dans la copie mise de côté
        (self.data / "posters" / "film.jpg").write_bytes(b"nouvelle affiche")
        self.db.connection().close()
        done = backup.restore(self.archive, self.data / "hub.db", self.data, replace=True)
        aside = Path(done["aside"])
        self.assertEqual(aside.parent, self.data)
        self.assertEqual((aside / "posters" / "film.jpg").read_bytes(), b"nouvelle affiche")
        self.assertEqual([r["name"] for r in accounts.rows(Database(aside / "hub.db"))], ["alice", "bob"])
        self.assertEqual([r["name"] for r in accounts.rows(Database(self.data / "hub.db"))], ["alice"])
        self.assertEqual((self.data / "posters" / "film.jpg").read_bytes(), b"affiche")
        self.assertTrue((self.data / "img" / "cache.jpg").exists())             # les caches ne sont pas touchés
        self.assertTrue((self.data / "inbox" / "alice" / "film.mkv").exists())

    def test_a_restoration_is_refused_while_the_server_holds_the_data(self):
        self.make()
        server = runlock.hold(self.data)
        self.addCleanup(runlock.release, server)
        before, base = contents(self.data), (self.data / "hub.db").read_bytes()
        with self.assertRaises(backup.BackupError) as said:
            backup.restore(self.archive, self.data / "hub.db", self.data, replace=True)
        self.assertIn("serveur", said.exception.message)
        self.assertEqual(contents(self.data), before)
        self.assertEqual((self.data / "hub.db").read_bytes(), base)

    def test_the_restoration_holds_the_lock_while_it_works_and_frees_it_after(self):
        self.make()
        self.db.connection().close()
        seen = []
        real = backup._put_in_place
        def watched(*args):
            seen.append(runlock.hold(self.data))          # un serveur qui démarrerait pendant la restauration ne l'aurait pas
            return real(*args)
        with mock.patch.object(backup, "_put_in_place", watched):
            backup.restore(self.archive, self.data / "hub.db", self.data, replace=True)
        self.assertEqual(seen, [None])
        after = runlock.hold(self.data)
        self.addCleanup(runlock.release, after)
        self.assertIsNotNone(after)

    def test_the_journal_files_of_the_old_base_do_not_survive_a_restoration(self):
        self.make()
        self.db.connection().close()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.data / 'hub.db'}{suffix}").write_bytes(b"ancien journal")
        backup.restore(self.archive, self.data / "hub.db", self.data, replace=True)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(Path(f"{self.data / 'hub.db'}{suffix}").exists())

    def test_an_older_schema_is_restored_then_brought_up_to_date_at_the_next_start(self):
        self.db.execute("UPDATE kv SET value=? WHERE key='schema'", (str(SCHEMA_VERSION - 1),))
        self.make()
        self.assertEqual(manifest_of(self.archive)["schema"], SCHEMA_VERSION - 1)
        target = self.empty_install()
        backup.restore(self.archive, target / "hub.db", target)
        self.assertEqual(Database(target / "hub.db").kv_get("schema"), SCHEMA_VERSION)


class TestRefusal(BackupCase):
    """Une archive refusée ne touche à rien : l'installation reste telle qu'elle était, aucun dossier de travail ne traîne."""

    def setUp(self):
        super().setUp()
        self.make()
        self.target = self.empty_install()

    def refused(self, *words):
        with self.assertRaises(backup.BackupError) as said:
            backup.restore(self.archive, self.target / "hub.db", self.target)
        for word in words:
            self.assertIn(word, said.exception.message)
        self.assertEqual(contents(self.target), [])

    def edit_manifest(self, **fields):
        def change(name, data):
            if name != "manifest.json":
                return [(name, data)]
            return [(name, json.dumps({**json.loads(data), **fields}).encode())]
        rewrite(self.archive, change)

    def test_an_archive_from_a_newer_hub_is_refused(self):
        self.edit_manifest(schema=SCHEMA_VERSION + 1)
        self.refused("version", str(SCHEMA_VERSION + 1))

    def test_a_base_newer_than_the_hub_is_refused_even_if_the_manifest_says_otherwise(self):
        def change(name, data):
            if name != "hub.db":
                return [(name, data)]
            path = self.root / "plus-recente.db"
            path.write_bytes(data)
            conn = sqlite3.connect(path)
            conn.execute("UPDATE kv SET value=? WHERE key='schema'", (str(SCHEMA_VERSION + 1),))
            conn.commit()
            conn.close()
            return [(name, path.read_bytes())]
        rewrite(self.archive, change)
        manifest = manifest_of(self.archive)
        for f in manifest["files"]:
            if f["path"] == "hub.db":
                with zipfile.ZipFile(self.archive) as z:
                    import hashlib
                    f["sha256"], f["size"] = hashlib.sha256(z.read("hub.db")).hexdigest(), z.getinfo("hub.db").file_size
        self.edit_manifest(files=manifest["files"])
        self.refused(str(SCHEMA_VERSION + 1))

    def test_an_unknown_archive_format_is_refused(self):
        self.edit_manifest(format=backup.FORMAT + 1)
        self.refused("format")

    def test_a_file_that_is_not_an_archive_is_refused(self):
        self.archive.write_bytes(b"pas une archive")
        self.refused("archive")

    def test_an_archive_without_a_manifest_is_refused(self):
        rewrite(self.archive, lambda name, data: [] if name == "manifest.json" else [(name, data)])
        self.refused("manifest")

    def test_a_manifest_that_is_not_json_is_refused(self):
        rewrite(self.archive, lambda name, data: [(name, b"{pas du json" if name == "manifest.json" else data)])
        self.refused("manifest.json", "illisible")

    def test_a_manifest_that_is_not_an_object_is_refused(self):
        rewrite(self.archive, lambda name, data: [(name, b"[]" if name == "manifest.json" else data)])
        self.refused("manifest.json", "illisible")

    def test_a_manifest_without_a_list_of_files_is_refused(self):
        self.edit_manifest(files="hub.db")
        self.refused("liste de fichiers")

    def test_a_manifest_whose_files_are_missing_or_not_all_objects_is_refused(self):
        for files in (None, 12, ["hub.db"], [1]):
            with self.subTest(files=files):
                self.edit_manifest(files=files)
                self.refused("liste de fichiers")

    def test_a_listed_file_without_a_text_path_is_refused(self):
        for entry in ({"size": 1, "sha256": "x"}, {"path": 5, "size": 1, "sha256": "x"}):
            with self.subTest(entry=entry):
                self.edit_manifest(files=manifest_of(self.archive)["files"] + [entry])
                self.refused("chemin refusé")

    def test_a_file_whose_size_differs_from_the_manifest_is_refused(self):
        self.edit_manifest(files=[{**f, "size": f["size"] + 1} if f["path"] == "posters/film.jpg" else f for f in manifest_of(self.archive)["files"]])
        self.refused("posters/film.jpg", "taille")

    def test_a_listed_file_missing_from_the_archive_is_refused(self):
        rewrite(self.archive, lambda name, data: [] if name == "posters/film.jpg" else [(name, data)])
        self.refused("posters/film.jpg", "absent")

    def test_a_damaged_file_of_the_same_size_is_refused_by_its_checksum(self):
        rewrite(self.archive, lambda name, data: [(name, b"AFFICHE" if name == "posters/film.jpg" else data)])          # même taille que « affiche » : seule l'empreinte le voit
        self.refused("posters/film.jpg", "empreinte")

    def test_a_file_the_manifest_does_not_list_is_refused(self):
        rewrite(self.archive, lambda name, data: [(name, data)] + ([("posters/intrus.jpg", b"x")] if name == "sagas.json" else []))
        self.refused("posters/intrus.jpg")

    def test_a_path_that_leaves_the_data_folder_is_refused(self):
        for bad in ("../dehors.txt", "posters/../../dehors.txt", "/etc/dehors", "img/cache.jpg", "posters\\..\\x", "C:/x",
                    "posters/a:b.jpg", "posters/a\\b.jpg", "posters/.cache", "artists/.cache", "posters/..", "posters/a/b.jpg"):
            with self.subTest(bad=bad):
                self.setUp()
                rewrite(self.archive, lambda name, data: [(name, data)] + ([(bad, b"x")] if name == "sagas.json" else []))
                files = manifest_of(self.archive)["files"] + [{"path": bad, "size": 1, "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"}]
                self.edit_manifest(files=files)
                self.refused()
                self.assertFalse((self.root / "dehors.txt").exists())

    def test_an_archive_without_a_base_is_refused(self):
        rewrite(self.archive, lambda name, data: [] if name == "hub.db" else [(name, data)])
        self.edit_manifest(files=[f for f in manifest_of(self.archive)["files"] if f["path"] != "hub.db"])
        self.refused("hub.db")

    def test_a_base_that_is_not_sqlite_is_refused(self):
        import hashlib
        rewrite(self.archive, lambda name, data: [(name, b"pas une base" if name == "hub.db" else data)])
        files = [{**f, "size": 12, "sha256": hashlib.sha256(b"pas une base").hexdigest()} if f["path"] == "hub.db" else f for f in manifest_of(self.archive)["files"]]
        self.edit_manifest(files=files)
        self.refused("hub.db")

    def test_a_refused_archive_leaves_an_existing_installation_as_it_was(self):
        self.edit_manifest(schema=SCHEMA_VERSION + 1)
        before = contents(self.data)
        base = (self.data / "hub.db").read_bytes()
        with self.assertRaises(backup.BackupError):
            backup.restore(self.archive, self.data / "hub.db", self.data, replace=True)
        self.assertEqual(contents(self.data), before)
        self.assertEqual((self.data / "hub.db").read_bytes(), base)


class TestCommands(BackupCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HUB_DATA": str(self.data)}), contextlib.redirect_stdout(out):
            cli.main_with(list(argv))
        return out.getvalue()

    def test_backup_writes_an_archive_where_asked_and_says_where(self):
        said = self.run_cli("backup", str(self.archive))
        self.assertIn(str(self.archive), said)
        self.assertIn("hub.db", names_in(self.archive))

    def test_backup_without_a_path_writes_into_the_backups_folder_of_the_data(self):
        said = self.run_cli("backup")
        made = list((self.data / "sauvegardes").glob("media_hub-*.zip"))
        self.assertEqual(len(made), 1)
        self.assertIn(made[0].name, said)

    def test_a_backup_is_never_saved_inside_the_next_backup(self):
        self.run_cli("backup")
        self.run_cli("backup", str(self.archive))
        self.assertFalse(any(name.startswith("sauvegardes/") for name in names_in(self.archive)))

    def test_restore_refuses_an_existing_installation_then_replaces_it_when_asked(self):
        self.run_cli("backup", str(self.archive))
        self.db.connection().close()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.run_cli("restore", str(self.archive))
        self.assertIn("--replace", str(stop.exception.code))
        said = self.run_cli("restore", str(self.archive), "--replace")
        self.assertIn("avant-restauration", said)

    def test_restore_of_a_missing_archive_says_so(self):
        with self.assertRaises(SystemExit) as stop:
            self.run_cli("restore", str(self.root / "absente.zip"))
        self.assertIn("absente.zip", str(stop.exception.code))


class TestRoute(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "web").mkdir()
        self.hub = MiniHub(self.root)
        self.boss = accounts.create(self.hub.db, "boss", PASSWORD)
        self.bob = accounts.create(self.hub.db, "bob", PASSWORD)
        (self.hub.cfg.data / "posters").mkdir()
        (self.hub.cfg.data / "posters" / "film.jpg").write_bytes(b"affiche")

    def get(self, row):
        return call(self.hub, "GET", "/api/backup", headers=self.hub.cookie(accounts.by_id(self.hub.db, row["id"])))[1]

    def test_the_administrator_downloads_an_archive_as_an_attachment(self):
        reply = self.get(self.boss)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers["content-type"], "application/zip")
        self.assertIn("attachment", reply.headers["content-disposition"])
        self.assertIn(f"media_hub-{__version__}-", reply.headers["content-disposition"])
        self.assertEqual(reply.headers["cache-control"], "private, no-store")
        archive = self.root / "recue.zip"
        archive.write_bytes(reply.body)
        self.assertEqual(names_in(archive), ["hub.db", "manifest.json", "posters/film.jpg"])

    def test_the_archive_built_for_the_download_does_not_stay_on_the_server(self):
        self.get(self.boss)
        self.assertEqual(sorted(p.name for p in self.hub.cfg.data.iterdir()), ["inbox", "posters"] if (self.hub.cfg.data / "inbox").exists() else ["posters"])

    def test_an_account_that_is_not_administrator_gets_a_403(self):
        reply = self.get(self.bob)
        self.assertEqual((reply.status, reply.json()), (403, {"error": "réservé à l'administrateur"}))


if __name__ == "__main__":
    unittest.main()
