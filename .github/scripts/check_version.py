"""Contrôle de version d'une pull request.

Compare `__version__`, `API_VERSION`, `SCHEMA_VERSION` et `MIGRATIONS` de la PR à ceux de la branche de base, selon la règle de
`.claude/skills/ship/SKILL.md`. Une erreur est une incohérence (le numéro baisse, n'est pas de la forme X.Y.Z) ; le reste est un
avertissement, parce que choisir MINOR ou PATCH, ou laisser la version telle quelle, est un jugement de l'utilisateur.

Usage : python check_version.py <commit de base>   (à lancer à la racine du dépôt, avec l'historique complet)
"""
import os
import re
import subprocess
import sys
from pathlib import Path

CONSTANT_FILES = ("hub/__init__.py", "hub/server/app.py", "hub/db.py")      # __version__, API_VERSION, (MIGRATIONS, SCHEMA_VERSION)
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def _find(pattern, text, name):
    found = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    if not found:
        raise ValueError(f"{name} introuvable")
    return found.group(1)


def read_constants(init, app, db):
    """Les quatre valeurs que la règle surveille, lues dans le texte des trois fichiers."""
    migrations = _find(r"^MIGRATIONS\s*=\s*\{([^}]*)\}", db, "MIGRATIONS")
    return {
        "version": _find(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", init, "__version__"),
        "api": int(_find(r"^API_VERSION\s*=\s*(\d+)", app, "API_VERSION")),
        "schema": int(_find(r"^SCHEMA_VERSION\s*=\s*(\d+)", db, "SCHEMA_VERSION")),
        "migrations": {int(key) for key in re.findall(r"(?:^|,)\s*(\d+)\s*:", migrations)},
    }


def touches_hub(files):
    """Vrai si la PR change le paquet `hub/` : tout ce qu'il contient est visible de l'utilisateur (serveur, interface)."""
    return any(name.startswith("hub/") for name in files)


def _numbers(version):
    return tuple(int(part) for part in version.split(".")) if SEMVER.fullmatch(version) else None


def _version_messages(files, before, after):
    """Erreurs et avertissements qui ne regardent que `__version__`."""
    old, new = _numbers(before["version"]), _numbers(after["version"])
    if new is None:
        return [f"__version__ = {after['version']!r} n'est pas de la forme X.Y.Z"], []
    if new < old:
        return [f"__version__ baisse : {before['version']} → {after['version']}"], []
    warnings = []
    if new == old and touches_hub(files):
        warnings.append(f"hub/ change et __version__ reste {after['version']} : MINOR pour une fonction visible, PATCH pour une correction (voir /ship)")
    if new > old and not touches_hub(files):
        warnings.append(f"__version__ passe à {after['version']} sans que hub/ change : la règle la laisse alors inchangée")
    if new[0] > old[0]:
        warnings.append(f"__version__ change de version majeure ({after['version']}) : passer en 1.0.0 est ta décision, pas celle de /ship")
    return [], warnings


def _contract_messages(before, after):
    """Erreurs et avertissements sur `SCHEMA_VERSION` et `API_VERSION`."""
    errors, warnings = [], []
    if after["schema"] < before["schema"]:
        errors.append(f"SCHEMA_VERSION baisse : {before['schema']} → {after['schema']}")
    elif after["schema"] > before["schema"] and not any(v in after["migrations"] for v in range(before["schema"] + 1, after["schema"] + 1)):
        warnings.append(f"SCHEMA_VERSION passe à {after['schema']} sans entrée dans MIGRATIONS : normal pour une simple table nouvelle (CREATE IF NOT EXISTS), pas pour une colonne")
    return errors, warnings


def _api_message(before, after):
    """Un contrat d'API qui change demande au moins un MINOR."""
    old, new = _numbers(before["version"]), _numbers(after["version"])
    if after["api"] > before["api"] and (new is None or new[:2] <= old[:2]):
        return [f"API_VERSION passe à {after['api']} : __version__ doit monter d'un MINOR au moins"]
    return []


def judge(files, before, after):
    """(erreurs, avertissements) d'une PR qui change `files` et fait passer les constantes de `before` à `after`."""
    errors, warnings = _version_messages(files, before, after)
    contract_errors, contract_warnings = _contract_messages(before, after)
    return errors + contract_errors, warnings + contract_warnings + _api_message(before, after)


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def report(errors, warnings):
    for message in errors:
        print(f"::error title=Version::{message}")
    for message in warnings:
        print(f"::warning title=Version::{message}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"- ❌ {message}" for message in errors] + [f"- ⚠️ {message}" for message in warnings] or ["- ✅ la version est cohérente avec ce que la PR change"]
        with open(summary, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    base = argv[1]
    files = git("diff", "--name-only", f"{base}...HEAD").split()
    before = read_constants(*(git("show", f"{base}:{name}") for name in CONSTANT_FILES))
    after = read_constants(*(read_text(name) for name in CONSTANT_FILES))
    errors, warnings = judge(files, before, after)
    report(errors, warnings)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
