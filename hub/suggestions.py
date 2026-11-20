"""Les suggestions : une idée ou une remarque sur le hub lui-même (« le lecteur devrait… »), qu'un compte envoie à l'administrateur.

Pas des suggestions de films : celles-là sont les demandes (requests.py). Une suggestion est un texte libre rangé dans une catégorie (`evolution`, `improvement`, `bug`, `other`) ;
l'administrateur lui donne un état (`new`, `planned`, `done`, `declined`) et, s'il veut, une réponse que l'auteur lit.

Un **signalement** est une suggestion de catégorie `bug` à laquelle on rattache un titre précis (`item_id`, et `entry_id` pour un épisode ou une piste) et une cause (`cause` : l'image, le son, les sous-titres, la lecture, autre) :
mêmes auteur, états, réponse et retrait, sans table à part (voir docs/decisions.md). Il garde `subject`, le titre tel que l'auteur le voyait, et `context`, la dernière décision de lecture de l'auteur pour ce fichier
(jointe d'office, que l'administrateur seul lit). Le titre peut disparaître ou devenir invisible : la liste ne casse jamais, et le lien vers la fiche (`link`) n'est rendu que si le lecteur peut encore la voir.

Confidentialité : chaque suggestion reste entre son auteur et l'administrateur ; un compte ne lit que les siennes. Tout compte peut suggérer, sans droit dédié ni plafond de nombre
(décision du 15 novembre 2026) : seules les longueurs sont bornées.
"""

from __future__ import annotations

import json
import time

CATEGORIES = ("evolution", "improvement", "bug", "other")
STATUSES = ("new", "planned", "done", "declined")
MAX_TEXT = 1000                 # le texte de la suggestion
MAX_REPLY = 500                 # la réponse de l'administrateur
CAUSES = ("image", "sound", "subtitles", "playback", "other")
MAX_SUBJECT = 200               # le titre signalé, tel que l'auteur le voyait
# Les colonnes d'un signalement, ajoutées après coup (migration 10, hub/db.py) : la table neuve les porte aussi, une base d'avant les reçoit par ALTER TABLE. `cause` vide = pas un signalement.
REPORT_COLUMNS = {"item_id": "TEXT NOT NULL DEFAULT ''",                # items.id ; pas une clé étrangère : l'élément peut disparaître, le signalement reste
                  "entry_id": "TEXT NOT NULL DEFAULT ''",               # entries.id d'un épisode ou d'une piste, vide pour un film ou tout l'élément
                  "cause": f"TEXT NOT NULL DEFAULT '' CHECK (cause IN ('', {', '.join(repr(c) for c in CAUSES)}))",
                  "subject": "TEXT NOT NULL DEFAULT ''",
                  "context": "TEXT NOT NULL DEFAULT ''"}                # JSON : la dernière décision de lecture de l'auteur pour ce fichier (`Activity.last_context`), vide sans décision

SUGGESTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS suggestions(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,                     -- users.id, pas une clé étrangère (comme `ratings`) : un compte supprimé est nettoyé par accounts.delete
    category TEXT NOT NULL CHECK (category IN ({", ".join(repr(c) for c in CATEGORIES)})),
    text     TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    reply    TEXT NOT NULL DEFAULT '',          -- la réponse de l'administrateur, que l'auteur lit (vide sinon)
    created  INTEGER NOT NULL,
    updated  INTEGER NOT NULL,
    {",\n    ".join(f"{column:<8} {definition}" for column, definition in REPORT_COLUMNS.items())}
)"""
SUGGESTIONS_INDEX = "CREATE INDEX IF NOT EXISTS suggestions_user ON suggestions(user_id)"
TABLES = (SUGGESTIONS_TABLE, SUGGESTIONS_INDEX)

UNKNOWN_CATEGORY = "catégorie inconnue (evolution, improvement, bug ou other)"
UNKNOWN_STATUS = "statut inconnu (new, planned, done ou declined)"
CAUSE_REQUIRED = "la cause est obligatoire (image, sound, subtitles, playback ou other)"


class SuggestionError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


SELECT = ("SELECT s.*, COALESCE(u.name, '') AS name, i.lib AS item_lib, e.id AS entry_found FROM suggestions s "
          "LEFT JOIN users u ON u.id = s.user_id LEFT JOIN items i ON i.id = s.item_id LEFT JOIN entries e ON e.id = s.entry_id AND e.item_id = s.item_id ")


def _link(row, visible):
    """Le lien vers la fiche d'un signalement, `{}` quand l'élément a disparu ou que le lecteur ne le voit pas (`visible` : les bibliothèques qu'il voit, None pour tout voir) ; le fichier n'y est que s'il existe encore."""
    if row["item_lib"] is None or (visible is not None and row["item_lib"] not in visible):
        return {}
    return {"link": {"item": row["item_id"], **({"entry": row["entry_id"]} if row["entry_found"] else {})}}


def _shown(row, visible):
    return {"id": row["id"], "category": row["category"], "text": row["text"], "status": row["status"], "reply": row["reply"], "created": row["created"], "updated": row["updated"],
            "item": row["item_id"], "entry": row["entry_id"], "cause": row["cause"], "subject": row["subject"], **_link(row, visible)}


def _admin_shown(row):
    """Une suggestion pour l'administrateur : avec son auteur (`name` vide pour un compte que la base n'a plus) et, pour un signalement, son contexte (`None` sans décision de lecture)."""
    return {**_shown(row, None), "user": {"id": row["user_id"], "name": row["name"]}, "context": json.loads(row["context"]) if row["context"] else None}


# ---------------------------------------------------------------------------
# Lire
# ---------------------------------------------------------------------------
def mine(db, uid, visible):
    """Les suggestions du compte `uid`, de la plus récente à la plus ancienne ; `visible` : les bibliothèques qu'il voit (pour le lien d'un signalement)."""
    return [_shown(r, visible) for r in db.query(SELECT + "WHERE s.user_id=? ORDER BY s.created DESC, s.id DESC", (uid,))]


def everything(db, status=None, category=None, reports=False):
    """Toutes les suggestions, pour l'administrateur, chacune avec son auteur, de la plus récente à la plus ancienne ; `status` et `category` en gardent une sorte, `reports` les seuls signalements."""
    if status is not None and status not in STATUSES:
        raise SuggestionError(400, UNKNOWN_STATUS)
    if category is not None and category not in CATEGORIES:
        raise SuggestionError(400, UNKNOWN_CATEGORY)
    rows = db.query(SELECT + "WHERE (? IS NULL OR s.status = ?) AND (? IS NULL OR s.category = ?) AND (? = 0 OR s.cause != '') ORDER BY s.created DESC, s.id DESC",
                    (status, status, category, category, int(bool(reports))))
    return [_admin_shown(r) for r in rows]


def one(db, sid):
    """Une suggestion telle que l'administrateur la voit ; 404 si elle n'existe pas."""
    row = db.one(SELECT + "WHERE s.id=?", (sid,))
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


def subject_of(item_title, entry_title=""):
    """Le titre signalé tel que l'auteur le voyait : « Élément · Fichier » pour un épisode ou une piste, l'élément seul sinon ; borné."""
    return (f"{item_title} · {entry_title}" if entry_title else item_title)[:MAX_SUBJECT]


def _kind(body, target):
    """(catégorie, cause) d'une suggestion, vérifiées : celle de `body`, ou `bug` et la cause (obligatoire) d'un signalement."""
    category = "bug" if target else body.get("category")
    if category not in CATEGORIES:
        raise SuggestionError(400, UNKNOWN_CATEGORY)
    cause = body.get("cause") if target else ""
    if target and cause not in CAUSES:
        raise SuggestionError(400, CAUSE_REQUIRED)
    return category, cause


def _body_text(body, required):
    """Le texte de `body`, borné ; vide accepté quand il n'est pas `required` (un signalement dont la cause suffit)."""
    text = body.get("text")
    text = _text(text, "texte", MAX_TEXT) if text is not None else ""
    if required and not text:
        raise SuggestionError(400, "le texte est vide")
    return text


def create(db, uid, body, visible, target=None):
    """Le compte `uid` propose une amélioration (`body` : {category, text}) ; rend la suggestion telle que son auteur la lit (`visible` : les bibliothèques qu'il voit). 400 en français pour une catégorie inconnue, un texte vide ou trop long.
    `target` fait de la suggestion un signalement : `{"item", "entry", "subject", "context"}` que la route a vérifié (l'élément existe et le compte le voit, le fichier est dans l'élément) ; la catégorie est alors `bug`, la cause (`body["cause"]`) est obligatoire et le texte facultatif."""
    category, cause = _kind(body, target)
    text = _body_text(body, required=not target)
    target = target or {"item": "", "entry": "", "subject": "", "context": None}
    context = json.dumps(target["context"], ensure_ascii=False, separators=(",", ":")) if target["context"] else ""
    now = int(time.time())
    sid = db.execute("INSERT INTO suggestions(user_id, category, text, status, reply, created, updated, item_id, entry_id, cause, subject, context) VALUES(?,?,?,'new','',?,?,?,?,?,?,?)",
                     (uid, category, text, now, now, target["item"], target["entry"], cause, target["subject"], context)).lastrowid
    return _shown(db.one(SELECT + "WHERE s.id=?", (sid,)), visible)


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
