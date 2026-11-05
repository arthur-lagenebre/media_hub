"""Les comptes : qui se connecte, avec quel mot de passe, et avec quels droits.

Un compte a un identifiant, un nom (unique, sans tenir compte de la casse), un mot de passe haché et un rôle (`admin` ou `user`). L'identifiant est le « profil » de la progression, des playlists et des clés `dismiss:` de `kv` ;
le premier compte créé reçoit l'identifiant `default` : c'est celui d'avant les comptes, il garde donc ce que le profil unique avait accumulé.

Le jeton de session n'est pas gardé : c'est `HMAC(secret, "session:<identifiant>:<token_version>")`, présenté sous la forme `<identifiant>.<mac>`. Changer un mot de passe, ou demander de déconnecter les autres appareils, incrémente `token_version`, ce qui invalide d'un coup tous les jetons du compte.
Le jeton de média (`?token=`) est propre au compte, sous la même forme `<identifiant>.<mac>` : il désigne donc qui demande un fichier, et les droits s'appliquent à lui comme à la session. Il est révoqué avec les sessions (même `token_version`).

Les droits : pour chaque bibliothèque, un compte peut *voir* (liste, fiches, images), *lire* (la lecture en ligne : vidéo, musique, livres, sous-titres) et *télécharger*. Une bibliothèque sans ligne dans `rights` est ouverte (tout permis) : un compte d'avant les droits n'a rien perdu. Lire et télécharger supposent voir ; ils sont indépendants l'un de l'autre. Un administrateur a tous les droits, sans ligne.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

from . import prefs

FIRST_ID = "default"
ROLES = ("admin", "user")
RIGHTS = ("view", "play", "download")         # l'ordre des valeurs de `grants`
MIN_PASSWORD = 8
MAX_PASSWORD = 200                  # scrypt coûte du temps et de la mémoire : pas de mot de passe de plusieurs Mo
MAX_NAME = 40
SCRYPT = (1 << 14, 8, 1)            # n, r, p : ~16 Mo et quelques dizaines de ms
SCRYPT_MAXMEM = 64 << 20
PBKDF2_ROUNDS = 600_000             # repli quand l'OpenSSL de Python n'a pas scrypt
TOKEN_FORM = re.compile(r"\w{1,64}\.[0-9a-f]{64}", re.ASCII)       # `<identifiant>.<mac>` : rien d'autre n'est un jeton de session


class AccountError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ---------------------------------------------------------------------------
# Mots de passe
# ---------------------------------------------------------------------------
def hash_password(password):
    """`scrypt$n$r$p$sel$hach` (ou `pbkdf2$tours$sel$hach`), en hexadécimal ; un sel neuf à chaque appel."""
    raw, salt = password.encode("utf-8", "replace"), secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        n, r, p = SCRYPT
        try:
            digest = hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)
            return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"
        except (ValueError, OSError, MemoryError):
            pass                                    # scrypt annoncé mais absent de cet OpenSSL : on se rabat sur pbkdf2
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, PBKDF2_ROUNDS, 32)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


_verifying = threading.Semaphore(2)     # au plus deux vérifications à la fois : chacune coûte ~16 Mo et du processeur, et /api/login et /api/token sont ouverts à qui n'est pas connecté


class Throttle:
    """Les essais de connexion ratés d'une même adresse : chaque échec rallonge l'attente du suivant, et au-delà de LIMIT échecs en WINDOW secondes plus rien n'est vérifié (429) : ni scrypt, ni fil retenu. Un essai réussi remet l'adresse à zéro.

    Derrière un proxy (Tailscale serve) tout vient de la même adresse : un essai de force brute bloque alors aussi les autres, pour au plus WINDOW secondes. C'est le prix d'une défense sans dépendance.
    """
    WINDOW, FREE, LIMIT, BASE, CAP = 300, 3, 10, 0.4, 5.0

    def __init__(self, clock=time.time):
        self._clock, self._fails, self._lock = clock, {}, threading.Lock()

    def _recent(self, key):
        now = self._clock()
        kept = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        if kept:
            self._fails[key] = kept
        else:
            self._fails.pop(key, None)
        return now, kept

    def delay(self, key):
        """Les secondes à attendre avant de vérifier cet essai ; AccountError 429 quand il y a eu trop d'échecs."""
        with self._lock:
            now, kept = self._recent(key)
            if len(kept) >= self.LIMIT:
                raise AccountError(429, f"trop d'essais : réessaie dans {int(self.WINDOW - (now - kept[0])) + 1} s")
            return min(self.CAP, self.BASE * 2 ** max(0, len(kept) - self.FREE))

    def failed(self, key):
        with self._lock:
            self._recent(key)
            self._fails.setdefault(key, []).append(self._clock())

    def passed(self, key):
        with self._lock:
            self._fails.pop(key, None)


def verify_password(password, stored):
    raw = password.encode("utf-8", "replace") if isinstance(password, str) else b""
    with _verifying:
        return _verify(raw, stored)


def _verify(raw, stored):
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt":
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            digest = hashlib.scrypt(raw, salt=bytes.fromhex(parts[4]), n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)
            return hmac.compare_digest(digest, bytes.fromhex(parts[5]))
        if parts[0] == "pbkdf2":
            digest = hashlib.pbkdf2_hmac("sha256", raw, bytes.fromhex(parts[2]), int(parts[1]), 32)
            return hmac.compare_digest(digest, bytes.fromhex(parts[3]))
    except (ValueError, IndexError, OSError, AttributeError, MemoryError):     # hach illisible, ou scrypt absent alors que le compte en dépend
        pass
    return False


_dummy = []


def _dummy_hash():
    """Un haché qui ne correspond à rien : un nom inconnu coûte autant de temps qu'un mauvais mot de passe."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    return _dummy[0]


def check_password(password):
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AccountError(400, f"le mot de passe doit faire de {MIN_PASSWORD} à {MAX_PASSWORD} caractères")
    return password


def clean_name(name):
    name = " ".join(str(name).split()) if isinstance(name, str) else ""
    if not name or len(name) > MAX_NAME or not name.isprintable():
        raise AccountError(400, f"le nom doit faire de 1 à {MAX_NAME} caractères")
    return name


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
def rows(db):
    return db.query("SELECT * FROM users ORDER BY created, name COLLATE NOCASE")


def exist(db):
    return db.one("SELECT 1 FROM users LIMIT 1") is not None


def by_id(db, uid):
    return db.one("SELECT * FROM users WHERE id=?", (uid,))


def by_name(db, name):
    wanted = " ".join(name.split()).casefold() if isinstance(name, str) else ""
    return next((r for r in rows(db) if r["name"].casefold() == wanted), None) if wanted else None


def public(row):
    """Ce que l'API dit d'un compte (jamais le haché ni la version du jeton)."""
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


def listing(row):
    return {**public(row), "created": row["created"]}


def authenticate(db, name, password):
    """Le compte désigné par `name` (ou le seul compte, si `name` est absent) quand le mot de passe est bon ; sinon 401. Le même message pour un nom inconnu et un mauvais mot de passe."""
    if name is None or name == "":
        everyone = rows(db)
        if len(everyone) != 1:
            raise AccountError(401, "nom d'utilisateur requis")
        row = everyone[0]
    else:
        row = by_name(db, name)
    good = verify_password(password, row["pass_hash"] if row else _dummy_hash())
    if row is None or not good:
        raise AccountError(401, "mot de passe incorrect")
    return row


# ---------------------------------------------------------------------------
# Jeton de session
# ---------------------------------------------------------------------------
def _mac(secret, uid, version, purpose="session"):
    return hmac.new(secret.encode(), f"{purpose}:{uid}:{version}".encode(), hashlib.sha256).hexdigest()


def session_token(secret, row):
    return f"{row['id']}.{_mac(secret, row['id'], row['token_version'])}"


def media_token(secret, row):
    """Le jeton de média du compte (`?token=` sur /media, /img, /book et /api/subs) : un autre domaine du même HMAC, donc un jeton de session n'y vaut rien, ni l'inverse."""
    return f"{row['id']}.{_mac(secret, row['id'], row['token_version'], 'media')}"


def user_for_token(db, secret, token, purpose="session"):
    """Le compte qui porte ce jeton (`<identifiant>.<mac>`), ou None : jeton mal formé, compte supprimé, mot de passe changé depuis. `purpose` : `session` ou `media`."""
    if not TOKEN_FORM.fullmatch(token):
        return None
    uid, _, mac = token.partition(".")
    row = by_id(db, uid)
    if row is None or not hmac.compare_digest(mac.encode("utf-8", "replace"), _mac(secret, uid, row["token_version"], purpose).encode()):
        return None
    return row


# ---------------------------------------------------------------------------
# Droits
# ---------------------------------------------------------------------------
def grants(db, uid):
    """{bibliothèque: (voir, lire, télécharger)} des lignes de ce compte ; une bibliothèque absente est ouverte (voir `allows`). Sans « voir », ni lire ni télécharger, quoi que la ligne dise."""
    return {r["lib"]: (bool(r["view"]), bool(r["view"] and r["play"]), bool(r["view"] and r["download"])) for r in db.query("SELECT lib, view, play, download FROM rights WHERE user_id=?", (uid,))}


def allows(role, granted, lib_id, perm="view"):
    """Ce compte peut-il `view`, `play` ou `download` cette bibliothèque ? `granted` vient de `grants`. Un administrateur peut tout."""
    if role == "admin":
        return True
    return granted.get(lib_id, (True, True, True))[RIGHTS.index(perm)]


def rights_json(role, granted, lib_ids):
    """Ce que l'API dit des droits d'un compte : `{bibliothèque: {"view": bool, "play": bool, "download": bool}}`, pour chacune des bibliothèques données."""
    return {lib: {perm: allows(role, granted, lib, perm) for perm in RIGHTS} for lib in lib_ids}


def set_rights(db, uid, lib_ids, changes):
    """Change les droits d'un compte pour les bibliothèques citées dans `changes` (`{bibliothèque: {"view": bool, "play": bool, "download": bool}}`, chaque droit facultatif : celui qui manque ne change pas).
    Refuse 400 une bibliothèque inconnue, un droit inconnu ou autre chose qu'un booléen, et un administrateur (il a tout). Retirer « voir » retire aussi « lire » et « télécharger ». Tout ou rien."""
    row = by_id(db, uid)
    if row is None:
        raise AccountError(404, "compte inconnu")
    if row["role"] == "admin":
        raise AccountError(400, "un administrateur a tous les droits")
    if not isinstance(changes, dict) or not changes:
        raise AccountError(400, "droits attendus : {bibliothèque: {view, play, download}}")
    current, new = grants(db, uid), {}
    for lib, perms in changes.items():
        if lib not in lib_ids:
            raise AccountError(400, f"bibliothèque inconnue : {lib}")
        if not isinstance(perms, dict) or not perms or set(perms) - set(RIGHTS) or not all(isinstance(v, bool) for v in perms.values()):
            raise AccountError(400, "chaque bibliothèque attend {view, play, download}, des booléens")
        view, play, download = current.get(lib, (True, True, True))
        view, play, download = perms.get("view", view), perms.get("play", play), perms.get("download", download)
        new[lib] = (view, view and play, view and download)
    with db.write_lock:
        conn = db.connection()
        try:
            for lib, (view, play, download) in new.items():
                if view and play and download:
                    conn.execute("DELETE FROM rights WHERE user_id=? AND lib=?", (uid, lib))        # tout autorisé : pas de ligne
                else:
                    conn.execute("INSERT INTO rights(user_id, lib, view, play, download) VALUES(?,?,?,?,?) ON CONFLICT(user_id, lib) DO UPDATE SET view=excluded.view, play=excluded.play, download=excluded.download",
                                 (uid, lib, int(view), int(play), int(download)))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return grants(db, uid)


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------
def create(db, name, password, role="user", check=True):
    """Crée un compte. Le premier de la base est administrateur et s'appelle `default` (identifiant) ; les suivants ont un identifiant aléatoire.
    `check=False` : pas de longueur minimale (le mot de passe de HUB_PASSWORD existait avant les comptes)."""
    name = clean_name(name)
    if role not in ROLES:
        raise AccountError(400, "rôle inconnu (admin ou user)")
    if check:
        check_password(password)
    elif not isinstance(password, str) or not password:
        raise AccountError(400, "mot de passe vide")
    hashed = hash_password(password)
    with db.write_lock:
        conn = db.connection()
        try:
            if any(r["name"].casefold() == name.casefold() for r in conn.execute("SELECT name FROM users")):
                raise AccountError(409, "ce nom est déjà pris")
            first = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            uid, role = (FIRST_ID, "admin") if first else (secrets.token_hex(8), role)
            now = int(time.time())
            conn.execute("INSERT INTO users(id, name, pass_hash, role, token_version, created) VALUES(?,?,?,?,1,?)", (uid, name, hashed, role, now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return by_id(db, uid)


def set_password(db, uid, password, check=True):
    """Nouveau mot de passe ; les jetons de session du compte cessent de valoir (`token_version` + 1). Renvoie le compte à jour."""
    if check:
        check_password(password)
    hashed = hash_password(password)
    with db.write_lock:
        conn = db.connection()
        cursor = conn.execute("UPDATE users SET pass_hash=?, token_version=token_version+1 WHERE id=?", (hashed, uid))
        conn.commit()
    if cursor.rowcount == 0:
        raise AccountError(404, "compte inconnu")
    return by_id(db, uid)


def revoke_sessions(db, uid):
    """Tous les jetons de session du compte cessent de valoir (`token_version` + 1), sans toucher au mot de passe. Renvoie le compte à jour : son nouveau jeton est celui de l'appareil qui a demandé."""
    with db.write_lock:
        conn = db.connection()
        cursor = conn.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (uid,))
        conn.commit()
    if cursor.rowcount == 0:
        raise AccountError(404, "compte inconnu")
    return by_id(db, uid)


def delete(db, uid):
    """Supprime le compte, ses droits, sa progression, ses playlists et ses clés `dismiss:` ; jamais le dernier administrateur. Une seule transaction."""
    with db.write_lock:
        conn = db.connection()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if row is None:
                raise AccountError(404, "compte inconnu")
            if row["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                raise AccountError(400, "impossible de supprimer le dernier administrateur")
            conn.execute("DELETE FROM progress WHERE profile=?", (uid,))
            conn.execute("DELETE FROM rights WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM playlist_tracks WHERE playlist_id IN (SELECT id FROM playlists WHERE profile=?)", (uid,))
            conn.execute("DELETE FROM playlists WHERE profile=?", (uid,))
            prefix = f"dismiss:{uid}:"
            conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
            conn.execute("DELETE FROM kv WHERE key=?", (prefs.KEY + uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return row


def seed(db, cfg):
    """Au démarrage : sans aucun compte, `HUB_PASSWORD` crée l'administrateur (`HUB_ADMIN`, « admin » par défaut) sous l'identifiant `default`. Ensuite `HUB_PASSWORD` est ignoré."""
    if cfg.password and not exist(db):
        try:
            create(db, cfg.admin, cfg.password, "admin", check=False)
        except AccountError as e:
            raise SystemExit(f"HUB_ADMIN invalide : {e.message}")
    _dummy_hash()                   # calculé ici, pas à la première connexion ratée : sinon cette première réponse se distinguerait des suivantes
