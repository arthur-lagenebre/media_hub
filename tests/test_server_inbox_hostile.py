"""Les routes de la boîte de réception face à des chemins hostiles : traversée écrite de toutes les façons (simple, double, majuscules, surcodée, `%u`, octet nul) dans le dossier, le nom ou `?dir=` ; points et espaces de fin, flux NTFS (`nom::$DATA`) ;
Unicode décomposé ; paramètre `dir` répété ; puis sur un vrai serveur, de vraies connexions : les mêmes traversées sur le fil, et plusieurs clients qui envoient ensemble dans un même nouveau sous-dossier."""

from __future__ import annotations

import json
import threading
import unittest
from urllib.parse import quote

from tests.rig import call
from tests.test_server import AccountsCase
from tests.test_server_inbox_subfolders import SubRoutes

# « .. » écrit de toutes les façons qu'un client ou un attaquant peut essayer, déjà prêt à être collé dans une URL
TRAVERSALS = (
    "..", "%2e%2e", "%2E%2E", ".%2e", "%2e.", "%252e%252e", "%252E%252E", "%25252e%25252e",
    "..%2f", "%2e%2e%2f", "%252e%252e%252f", "..%5c", "%2e%2e%5c", "..%2fboss", "x%2f..%2f..%2fboss", "x%252f..%252f..%252fboss",
    "%c0%ae%c0%ae", "%e0%80%ae%e0%80%ae", "%u002e%u002e", "..;", "....//", "%00", "..%00", "%2e%2e%00", "%ef%bc%8e%ef%bc%8e",
)
# où le placer : dans le dossier du compte, dans le nom, dans `?dir=`, après un vrai sous-dossier
POSITIONS = (
    "/api/inbox/{f}/s.mkv", "/api/inbox/boss/{f}", "/api/inbox/alice/a.mkv?dir={f}", "/api/inbox/alice/s.mkv?dir={f}", "/api/inbox/boss/s.mkv?dir={f}",
    "/api/inbox/alice/a.mkv?dir=x%2F{f}", "/api/inbox/alice/a.mkv?dir={f}%2Fx", "/api/inbox/alice/a.mkv?dir=x&dir={f}",
)


class Hostile(SubRoutes):
    def setUp(self):
        super().setUp()
        self.put(self.alice, "a.mkv", "x", b"alice")
        self.put(self.boss, "s.mkv", None, b"secret")
        self.before = self.everything()

    def outside_alice(self):
        """Tout fichier posé quelque part sous la racine de test hors de `inbox/alice` (les fichiers de `boss` y étaient déjà)."""
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*.mkv") if self.inbox / "alice" not in p.parents and p.name != "s.mkv")


class TestTraversalsAreNever200(Hostile):
    def test_every_written_form_in_every_place_is_a_404_for_the_administrator_reading_and_deleting(self):
        for form in TRAVERSALS:
            for place in POSITIONS:
                target = place.format(f=form)
                self.assertEqual(self.get(self.boss, target)[1].status, 404, target)
                self.assertEqual(self.delete(self.boss, target)[1].status, 404, target)
        self.assertEqual(self.everything(), self.before)

    def test_an_account_deleting_in_its_own_folder_with_such_a_dir_removes_nothing(self):
        for form in TRAVERSALS:
            for place in ("/api/inbox/alice/a.mkv?dir={f}", "/api/inbox/alice/s.mkv?dir={f}", "/api/inbox/alice/a.mkv?dir=x%2F{f}"):
                target = place.format(f=form)
                self.assertEqual(self.delete(self.alice, target)[1].status, 404, target)
        self.assertEqual(self.everything(), self.before)

    def test_an_upload_with_such_a_dir_is_refused_or_kept_under_the_folder_of_the_sender_as_an_ordinary_name(self):
        for form in TRAVERSALS:
            for sub in (form, "x%2F" + form):
                handler, reply = call(self.hub, "POST", "/api/inbox?name=" + quote("n.mkv") + "&dir=" + sub, b"zz", self.cookie(self.alice))
                self.assertIn(reply.status, (201, 400), sub)
        self.assertEqual(self.outside_alice(), [])
        self.assertTrue(all(f.startswith(("alice/", "boss/")) for f in self.everything()), self.everything())
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["alice", "boss"])

    def test_the_forms_that_decode_once_to_the_parent_or_to_a_separator_are_a_400(self):
        for form in ("%2e%2e", "%2E%2E", ".%2e", "%2e.", "..%2f", "%2e%2e%2f", "..%5c", "%2e%2e%5c", "..%2fboss", "x%2f..%2f..%2fboss", "....//"):
            reply = call(self.hub, "POST", "/api/inbox?name=n.mkv&dir=" + form, b"zz", self.cookie(self.alice))[1]
            self.assertEqual(reply.status, 400, form)

    def test_the_forms_that_decode_once_to_plain_text_are_ordinary_folders_never_decoded_again(self):
        for form, folder in (("%252e%252e", "%2e%2e"), ("%25252e%25252e", "%252e%252e"), ("%252f", "%2f"), ("%u002e%u002e", "%u002e%u002e"), ("..%3B", ";")):
            reply = call(self.hub, "POST", "/api/inbox?name=n.mkv&dir=" + form, b"zz", self.cookie(self.alice))[1]
            self.assertEqual((reply.status, reply.json()["dir"]), (201, folder), form)
        self.assertEqual(self.outside_alice(), [])

    def test_a_folder_named_after_the_encoded_text_is_read_only_by_that_exact_text(self):
        self.put(self.alice, "e.mkv", "%2e%2e", b"literal")
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/e.mkv?dir=%252e%252e")[1].body, b"literal")
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/e.mkv?dir=%2e%2e")[1].status, 404)                # une seule fois décodé : « .. »
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/e.mkv?dir=%25252e%25252e")[1].status, 404)         # deux fois : toujours pas le même texte
        self.assertEqual(self.delete(self.boss, "/api/inbox/alice/e.mkv?dir=%2e%2e")[1].status, 404)
        self.assertEqual(self.delete(self.boss, "/api/inbox/alice/e.mkv?dir=%252e%252e")[1].status, 200)

    def test_a_repeated_dir_parameter_is_checked_as_the_one_that_is_used(self):
        reply = call(self.hub, "POST", "/api/inbox?name=n.mkv&dir=..&dir=bon", b"zz", self.cookie(self.alice))[1]
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "bon"))
        reply = call(self.hub, "POST", "/api/inbox?name=n.mkv&dir=bon&dir=..", b"zz", self.cookie(self.alice))[1]
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/a.mkv?dir=..&dir=x")[1].body, b"alice")          # la dernière valeur est celle qui compte
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/a.mkv?dir=x&dir=..")[1].status, 404)


class TestNothingAliasedIsServed(Hostile):
    """Windows ouvre `x.mkv.`, `x.mkv ` et `x.mkv::$DATA` comme `x.mkv`, `Dossier.` comme `Dossier` : aucune de ces écritures ne doit livrer ni supprimer le fichier."""

    def test_the_file_is_not_served_nor_deleted_under_a_name_the_system_would_alias(self):
        for target in ("/api/inbox/alice/a.mkv.?dir=x", "/api/inbox/alice/a.mkv%20?dir=x", "/api/inbox/alice/a.mkv.%20.?dir=x", "/api/inbox/alice/a.mkv%3A%3A%24DATA?dir=x",
                       "/api/inbox/alice/a.mkv%3Aflux?dir=x", "/api/inbox/alice/a.mkv%3A?dir=x", "/api/inbox/alice/a.mkv.part?dir=x",
                       "/api/inbox/alice/a.mkv?dir=x.", "/api/inbox/alice/a.mkv?dir=x%20", "/api/inbox/alice/a.mkv?dir=%20x", "/api/inbox/alice/a.mkv?dir=x%3A%3A%24INDEX_ALLOCATION",
                       "/api/inbox/alice./a.mkv?dir=x", "/api/inbox/alice%20/a.mkv?dir=x", "/api/inbox/alice%3A%3A%24INDEX_ALLOCATION/a.mkv?dir=x"):
            got = self.get(self.boss, target)[1]
            self.assertEqual((got.status, b"alice" in got.body), (404, False), target)
            self.assertEqual(self.delete(self.boss, target)[1].status, 404, target)
        self.assertEqual(self.everything(), self.before)
        self.assertEqual(self.get(self.boss, "/api/inbox/alice/a.mkv?dir=x")[1].body, b"alice")                 # sans cela tout marche

    def test_an_upload_in_progress_is_not_served_nor_deleted_whatever_the_case_of_part(self):
        (self.inbox / "alice" / "x" / "en cours.mkv.part").write_bytes(b"secret en cours")
        before = self.everything()
        for suffix in (".part", ".PART", ".Part", ".pArT"):
            target = "/api/inbox/alice/" + quote("en cours.mkv" + suffix) + "?dir=x"
            got = self.get(self.boss, target)[1]
            self.assertEqual((got.status, b"secret" in got.body), (404, False), target)
            self.assertEqual(self.delete(self.boss, target)[1].status, 404, target)
            self.assertEqual(self.delete(self.alice, target)[1].status, 404, target)
        self.assertEqual(self.everything(), before)
        self.assertEqual((self.inbox / "alice" / "x" / "en cours.mkv.part").read_bytes(), b"secret en cours")

    def test_a_stream_in_the_sent_name_or_folder_is_a_plain_underscore_and_no_second_file_appears(self):
        reply = call(self.hub, "POST", "/api/inbox?name=a.mkv%3Aflux.mkv&dir=x%3A%3A%24INDEX_ALLOCATION", b"autre", self.cookie(self.alice))[1]
        self.assertEqual((reply.status, reply.json()["name"], reply.json()["dir"]), (201, "a.mkv_flux.mkv", "x__$INDEX_ALLOCATION"))
        reply = call(self.hub, "POST", "/api/inbox?name=a.mkv%3A%3A%24DATA&dir=x", b"autre", self.cookie(self.alice))[1]
        self.assertEqual(reply.status, 415)
        self.assertEqual(self.everything(), sorted(self.before + ["alice/x__$INDEX_ALLOCATION/a.mkv_flux.mkv"]))
        self.assertEqual((self.inbox / "alice" / "x" / "a.mkv").read_bytes(), b"alice")

    def test_a_dir_with_a_final_dot_or_space_sent_is_the_same_folder_as_without(self):
        reply = call(self.hub, "POST", "/api/inbox?name=b.mkv&dir=x.%2F", b"2", self.cookie(self.alice))[1]
        self.assertEqual(reply.status, 400)                                                                    # « x./ » : un segment vide
        reply = call(self.hub, "POST", "/api/inbox?name=b.mkv&dir=x.", b"2", self.cookie(self.alice))[1]
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "x"))
        reply = call(self.hub, "POST", "/api/inbox?name=c.mkv&dir=x%20", b"2", self.cookie(self.alice))[1]
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "x"))
        self.assertEqual(sorted(p.name for p in (self.inbox / "alice").iterdir()), ["x"])


class TestDecomposedUnicode(Hostile):
    def test_a_decomposed_name_and_folder_are_stored_composed_and_listed_and_read_by_what_the_reply_says(self):
        reply = call(self.hub, "POST", "/api/inbox?name=Cafe%CC%81.mkv&dir=Cafe%CC%81%2FLe%CC%81a", b"zz", self.cookie(self.alice))[1]
        self.assertEqual(reply.json(), {"name": "Café.mkv", "size": 2, "account": "alice", "dir": "Café/Léa"})
        listed = [f for f in self.get(self.boss, "/api/inbox")[1].json()["files"] if f["name"] == "Café.mkv"]
        self.assertEqual([f["dir"] for f in listed], ["Café/Léa"])
        got = self.get(self.boss, "/api/inbox/alice/Caf%C3%A9.mkv?dir=" + quote(listed[0]["dir"], safe=""))[1]
        self.assertEqual((got.status, got.body), (200, b"zz"))
        reply = call(self.hub, "POST", "/api/inbox?name=Caf%C3%A9.mkv&dir=CAF%C3%89%2Fl%C3%A9a", b"y", self.cookie(self.alice))[1]
        self.assertEqual((reply.json()["name"], reply.json()["dir"]), ("Café (2).mkv", "Café/Léa"))        # même dossier, composé ou non, à la casse près

    def test_a_dir_that_is_not_valid_utf8_is_one_ordinary_folder_named_with_the_replacement_character(self):
        reply = call(self.hub, "POST", "/api/inbox?name=n.mkv&dir=%c0%ae%c0%ae%2F%ff", b"zz", self.cookie(self.alice))[1]
        self.assertEqual((reply.status, reply.json()["dir"]), (201, "\ufffd" * 4 + "/\ufffd"))
        got = self.get(self.boss, "/api/inbox/alice/n.mkv?dir=" + "%EF%BF%BD" * 4 + "%2F%EF%BF%BD")[1]
        self.assertEqual((got.status, got.body), (200, b"zz"))
        self.assertEqual(self.outside_alice(), [])


class TestOnTheWire(AccountsCase):
    """Un vrai serveur : la ligne de requête passe par `http.server`, pas par le gestionnaire en mémoire des tests ci-dessus."""

    def setUp(self):
        super().setUp()
        self.alice = self.new_user("alice")
        self.assertEqual(self.admin.json(f"/api/user/{self.alice.account['id']}/upload", {"upload": True})[0], 200)
        self.hub.inbox.max_uploads = 16
        self.inbox = self.hub.cfg.data / "inbox"

    def files(self):
        return sorted(p.relative_to(self.inbox).as_posix() for p in self.inbox.rglob("*") if p.is_file()) if self.inbox.is_dir() else []

    def test_traversals_written_on_the_wire_reach_nothing(self):
        self.assertEqual(self.admin.request("POST", "/api/inbox?name=s.mkv", body=b"secret")[0], 201)
        self.assertEqual(self.alice.request("POST", "/api/inbox?name=a.mkv&dir=x", body=b"alice")[0], 201)
        before = self.files()
        admin_folder = quote("admin", safe="")
        for form in ("..", "%2e%2e", "%2E%2E", "%252e%252e", "..%2f..%2fadmin", "%2e%2e%2fadmin", "..%5cadmin", "%c0%ae%c0%ae", "%u002e%u002e", "%00", "....//"):
            for method in ("GET", "DELETE"):
                for target in (f"/api/inbox/{form}/s.mkv", f"/api/inbox/{admin_folder}/{form}", f"/api/inbox/alice/a.mkv?dir={form}", f"/api/inbox/alice/s.mkv?dir={form}",
                               f"/api/inbox/alice/a.mkv?dir=x%2F{form}", f"//api/inbox/alice/a.mkv?dir={form}", f"/api/inbox/alice/../{admin_folder}/s.mkv"):
                    status, _, body = self.admin.request(method, target)
                    self.assertIn(status, (400, 404), f"{method} {target}")
                    self.assertNotIn(b"secret", body, f"{method} {target}")
            status = self.alice.request("POST", "/api/inbox?name=n.mkv&dir=" + form, body=b"zz")[0]
            self.assertIn(status, (201, 400), form)
        self.assertTrue(all(f.startswith("alice/") for f in self.files() if f not in before), self.files())
        self.assertEqual(self.admin.request("GET", f"/api/inbox/{admin_folder}/s.mkv")[2], b"secret")
        self.assertEqual(self.admin.request("GET", "/api/inbox/alice/a.mkv?dir=x")[2], b"alice")
        self.assertTrue(set(before) <= set(self.files()))

    def test_clients_sending_together_into_the_same_new_folder_end_up_in_one_folder_without_losing_a_file(self):
        results = []

        def send(n):
            sub = "Vacances%2F2025" if n % 2 else "vacances%2F2025"
            results.append(self.alice.request("POST", f"/api/inbox?name=f{n}.mkv&dir={sub}", body=bytes([n]) * (n + 1)))

        threads = [threading.Thread(target=send, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(sorted(r[0] for r in results), [201] * 10)
        self.assertEqual(len({json.loads(r[2])["dir"] for r in results}), 1)                                    # une seule écriture du dossier, celle du disque
        folders = [p.relative_to(self.inbox).as_posix() for p in self.inbox.rglob("*") if p.is_dir()]
        self.assertEqual(len(folders), 3, folders)                                                              # alice, alice/<Vacances>, alice/<Vacances>/2025
        listed = self.admin.json("/api/inbox")[1]["files"]
        self.assertEqual(sorted(f["name"] for f in listed), sorted(f"f{n}.mkv" for n in range(10)))
        self.assertEqual({f["dir"] for f in listed}, {json.loads(results[0][2])["dir"]})
        self.assertEqual(sorted(f["size"] for f in listed), list(range(1, 11)))
        self.assertFalse([f for f in self.files() if f.endswith(".part")])


if __name__ == "__main__":
    unittest.main()
