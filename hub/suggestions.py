"""Les suggestions : une idée ou une remarque sur le hub lui-même (« le lecteur devrait… »), qu'un compte envoie à l'administrateur.

Pas des suggestions de films : celles-là sont les demandes (requests.py). Une suggestion est un texte libre rangé dans une catégorie (`evolution`, `improvement`, `bug`, `other`) ;
l'administrateur lui donne un état (`new`, `planned`, `done`, `declined`) et, s'il veut, une réponse que l'auteur lit.

Confidentialité : chaque suggestion reste entre son auteur et l'administrateur ; un compte ne lit que les siennes. Tout compte peut suggérer, sans droit dédié ni plafond de nombre
(décision du 15 novembre 2026) : seules les longueurs sont bornées.
"""

from __future__ import annotations

import time

CATEGORIES = ("evolution", "improvement", "bug", "other")
STATUSES = ("new", "planned", "done", "declined")
MAX_TEXT = 1000                 # le texte de la suggestion
MAX_REPLY = 500                 # la réponse de l'administrateur

SUGGESTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS suggestions(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,                     -- users.id, pas une clé étrangère (comme `ratings`) : un compte supprimé est nettoyé par accounts.delete
    category TEXT NOT NULL CHECK (category IN ({", ".join(repr(c) for c in CATEGORIES)})),
    text     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    reply    TEXT NOT NULL DEFAULT '',          -- la réponse de l'administrateur, que l'auteur lit (vide sinon)
    created  INTEGER NOT NULL,
    updated  INTEGER NOT NULL
)"""
SUGGESTIONS_INDEX = "CREATE INDEX IF NOT EXISTS suggestions_user ON suggestions(user_id)"
TABLES = (SUGGESTIONS_TABLE, SUGGESTIONS_INDEX)

UNKNOWN_CATEGORY = "catégorie inconnue (evolution, improvement, bug ou other)"
UNKNOWN_STATUS = "statut inconnu (new, planned, done ou declined)"


class SuggestionError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _shown(row):
    return {"id": row["id"], "category": row["category"], "text": row["text"], "status": row["status"], "reply": row["reply"], "created": row["created"], "updated": row["updated"]}


def _admin_shown(row):
    """Une suggestion pour l'administrateur : avec son auteur (`name` vide pour un compte que la base n'a plus)."""
    return {**_shown(row), "user": {"id": row["user_id"], "name": row["name"]}}


# ---------------------------------------------------------------------------
# Lire
# ---------------------------------------------------------------------------
def mine(db, uid):
    """Les suggestions du compte `uid`, de la plus récente à la plus ancienne."""
    return [_shown(r) for r in db.query("SELECT * FROM suggestions WHERE user_id=? ORDER BY created DESC, id DESC", (uid,))]


def everything(db, status=None, category=None):
    """Toutes les suggestions, pour l'administrateur, chacune avec son auteur, de la plus récente à la plus ancienne ; `status` et `category` en gardent une sorte."""
    if status is not None and status not in STATUSES:
        raise SuggestionError(400, UNKNOWN_STATUS)
    if category is not None and category not in CATEGORIES:
        raise SuggestionError(400, UNKNOWN_CATEGORY)
    rows = db.query("SELECT s.*, COALESCE(u.name, '') AS name FROM suggestions s LEFT JOIN users u ON u.id = s.user_id "
                    "WHERE (? IS NULL OR s.status = ?) AND (? IS NULL OR s.category = ?) ORDER BY s.created DESC, s.id DESC", (status, status, category, category))
    return [_admin_shown(r) for r in rows]


def one(db, sid):
    """Une suggestion telle que l'administrateur la voit ; 404 si elle n'existe pas."""
    row = db.one("SELECT s.*, COALESCE(u.name, '') AS name FROM suggestions s LEFT JOIN users u ON u.id = s.user_id WHERE s.id=?", (sid,))
    if row is None:
        raise SuggestionError(404, "suggestion inconnue")
    return _admin_shown(row)


def new_count(db):
    """Le nombre de suggestions pas encore traitées (`new`), pour l'administrateur."""
    return db.one("SELECT COUNT(*) FROM suggestions WHERE status='new'")[0]


# ---------------------------------------------------------------------------
# Écrire
# ---------------------------------------------------------------------------
def _text(value, what, limit, feminine=False):
    """Un texte d'au plus `limit` caractères, espaces de bord retirés ; 400 sinon."""
    if not isinstance(value, str):
        raise SuggestionError(400, f"{what} : un texte est attendu")
    value = value.strip()
    if len(value) > limit:
        raise SuggestionError(400, f"{what} trop {'longue' if feminine else 'long'} ({limit} caractères au plus)")
    return value


def create(db, uid, body):
    """Le compte `uid` propose une amélioration (`body` : {category, text}) ; rend la suggestion. 400 en français pour une catégorie inconnue, un texte vide ou trop long."""
    category, text = body.get("category"), body.get("text")
    if category not in CATEGORIES:
        raise SuggestionError(400, UNKNOWN_CATEGORY)
    text = _text(text, "texte", MAX_TEXT) if text is not None else ""
    if not text:
        raise SuggestionError(400, "le texte est vide")
    now = int(time.time())
    sid = db.execute("INSERT INTO suggestions(user_id, category, text, status, reply, created, updated) VALUES(?,?,?,'new','',?,?)", (uid, category, text, now, now)).lastrowid
    return _shown(db.one("SELECT * FROM suggestions WHERE id=?", (sid,)))


def set_status(db, sid, status, reply=None):
    """L'administrateur donne un état à une suggestion et, s'il veut, une réponse : `reply` absent (None) garde la réponse d'avant, un texte la remplace (vide : l'efface)."""
    if status not in STATUSES:
        raise SuggestionError(400, UNKNOWN_STATUS)
    now = int(time.time())
    if reply is None:
        changed = db.execute("UPDATE suggestions SET status=?, updated=? WHERE id=?", (status, now, sid)).rowcount
    else:
        reply = _text(reply, "réponse", MAX_REPLY, feminine=True)
        changed = db.execute("UPDATE suggestions SET status=?, reply=?, updated=? WHERE id=?", (status, reply, now, sid)).rowcount
    if not changed:
        raise SuggestionError(404, "suggestion inconnue")


def withdraw(db, uid, sid, admin):
    """L'auteur retire sa suggestion ; l'administrateur supprime n'importe laquelle. 404 pour une suggestion inconnue ou qui n'est pas celle du compte (les deux se répondent pareil)."""
    if admin:
        gone = db.execute("DELETE FROM suggestions WHERE id=?", (sid,)).rowcount
    else:
        gone = db.execute("DELETE FROM suggestions WHERE id=? AND user_id=?", (sid, uid)).rowcount
    if not gone:
        raise SuggestionError(404, "suggestion inconnue")


def forget_user(conn, uid):
    """Dans la transaction de la suppression d'un compte (accounts.delete) : ses suggestions partent avec lui."""
    conn.execute("DELETE FROM suggestions WHERE user_id=?", (uid,))
