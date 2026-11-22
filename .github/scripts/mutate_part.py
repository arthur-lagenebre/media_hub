"""Lance mutator sur une partie de `hub/` pour la passe de nuit (`nightly.yml`).

Un passage par fichier, avec les seuls tests de ce fichier, et la couverture mesurée une fois.
Un long fichier passe par tranches de lignes : mutator garde les résultats de chaque tranche, donc
une partie arrêtée à la limite du job reprend à la nuit suivante, au lieu de tout perdre.

Une partie trop longue pour un job (`hub/server` : `app.py` refait en entier dès qu'une ligne s'y ajoute,
car mutator repère un mutant par sa position dans le fichier) se répartit sur N jobs avec `--share K/N` :
chacun lance une unité sur N (un fichier, ou une tranche d'un long fichier).

Un mutant survivant ne fait pas échouer ce script : il est signalé dans `survived` (sortie de
l'étape), pour que la règle de saut distingue « traité, avec survivants » de « pas allé au bout ».
Un survivant listé dans `.github/mutants-equivalents.json` (sans effet observable, la raison écrite)
n'y compte pas : mutator n'a pas de marque « à ignorer » pour Python.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EQUIVALENTS = ".github/mutants-equivalents.json"     # les survivants acceptés, chacun avec sa raison : ils ne rendent pas la partie rouge
BATCH_LINES = 400    # hub/server/app.py : 3300 lignes et 850 mutations, soit une centaine par tranche
SURVIVED = 3          # code de sortie de mutator quand un mutant survit
# Modules dont les tests ne portent pas (tous) le nom : sans cela, toute la suite pour chaque mutant, ou de faux survivants.
OWN_TESTS = {
    "__main__": "tests/test_cli.py",
    "enrich": "tests/test_enrich*.py tests/test_person_films.py",     # person_films et fetch_person vivent dans enrich.py
    "people": "tests/test_enrich_people.py tests/test_person_films.py",
}


def python_files(root: Path, paths: list[str]) -> list[str]:
    files: list[str] = []
    for name in paths:
        path = root / name
        if path.is_dir():
            files.extend(found.relative_to(root).as_posix() for found in sorted(path.rglob("*.py")))
        else:
            files.append(name)
    return files


def test_command(root: Path, patterns: str, python: str, memory_mb: int | None) -> str:
    found = {path.stem for pattern in patterns.split() for path in root.glob(pattern)}
    target = " ".join(f"tests.{name}" for name in sorted(found)) or "discover"
    command = f"{python} -m unittest {target}"
    return f"ulimit -v {memory_mb * 1024}; {command}" if memory_mb else command


def line_batches(total: int) -> list[str | None]:
    if total <= BATCH_LINES:
        return [None]
    return [",".join(str(n) for n in range(start, min(start + BATCH_LINES, total + 1)))
            for start in range(1, total + 1, BATCH_LINES)]


def file_patterns(name: str, tests: str) -> str:
    stem = Path(name).stem
    return tests or OWN_TESTS.get(stem, f"tests/test_{stem}*.py")


def parse_share(text: str) -> tuple[int, int] | None:
    """« 3/10 » : la troisième part sur dix ; rien : tout."""
    if not text:
        return None
    k, n = (int(value) for value in text.split("/"))     # ValueError si ce n'est pas « K/N »
    if not 1 <= k <= n:
        raise ValueError(f"part {text} : K doit aller de 1 à N")
    return k, n


def plan_part(root: Path, paths: list[str], tests: str, *, python: str, mutator: str,
              workers: int | None = None, memory_mb: int | None = None,
              share: tuple[int, int] | None = None) -> list[list[str]]:
    units = [(name, lines) for name in python_files(root, paths)
             for lines in line_batches(len((root / name).read_text(encoding="utf-8").splitlines()))]
    if share:
        k, n = share
        units = units[k - 1::n]
    commands: list[list[str]] = []
    for name, lines in units:
        command = [mutator, "--test-command", test_command(root, file_patterns(name, tests), python, memory_mb)]
        if commands:
            command.append("--reuse-coverage")     # la première mesure vaut pour tout hub/
        if workers:
            command += ["--max-workers", str(workers)]
        if lines:
            command += ["--lines", lines]
        commands.append(command + [name])
    return commands


Survivor = tuple[str, int, str]      # (fichier, ligne, mutation), tel que mutator l'écrit : « SURVIVED  hub/x.py:12 True -> False »
SURVIVOR_LINE = re.compile(r"SURVIVED\s+(\S+):(\d+) (.+)")


def run_plan(commands: list[list[str]], execute) -> tuple[int, bool, list[Survivor]]:
    """Le code de sortie de la partie, si mutator a dit qu'un mutant survit, et les survivants qu'il a écrits."""
    failure = 0
    survived = False
    found: list[Survivor] = []
    for index, command in enumerate(commands):
        code, survivors = execute(command)
        found += survivors
        if code == SURVIVED:
            survived = True
        elif code:
            failure = failure or code
            if index == 0:     # il mesurait la couverture : les suivants n'auraient rien à lire
                break
    return failure, survived, found


def survivors(lines) -> list[Survivor]:
    found = []
    for line in lines:
        match = SURVIVOR_LINE.fullmatch(line.strip())
        if match:
            found.append((match[1], int(match[2]), match[3]))
    return found


def load_equivalents(path: Path) -> list[dict]:
    """Les mutants acceptés comme équivalents (sans effet observable), chacun avec sa raison : voir .github/mutants-equivalents.json."""
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def unaccepted(found: list[Survivor], root: Path, accepted: list[dict]) -> list[Survivor]:
    """Les survivants qui ne sont pas dans `accepted`. Un mutant accepté se reconnaît au fichier, au texte de sa ligne et à la mutation,
    pas au numéro de ligne : il le reste quand des lignes s'ajoutent au-dessus, et ne l'est plus si sa ligne change."""
    keys = {(entry["file"], entry["line"], entry["mutation"]) for entry in accepted}
    texts: dict[str, list[str]] = {}

    def text(name: str, number: int) -> str:
        if name not in texts:
            path = root / name
            texts[name] = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        lines = texts[name]
        return lines[number - 1].strip() if 0 < number <= len(lines) else ""

    return [s for s in found if (s[0], text(s[0], s[1]), s[2]) not in keys]


def conclude(outcome: tuple[int, bool, list[Survivor]], root: Path, accepted: list[dict]) -> tuple[int, bool, list[Survivor]]:
    """(code de sortie, la partie reste-t-elle rouge, les survivants non acceptés). Un « survivant » de mutator dont on n'a lu
    aucune ligne (format changé ?) laisse la partie rouge : mieux vaut un faux rouge qu'un survivant passé sous silence."""
    failure, survived, found = outcome
    remaining = unaccepted(found, root, accepted)
    return failure, survived and (bool(remaining) or not found), remaining


def execute(command: list[str]) -> tuple[int, list[Survivor]]:
    """Lance mutator en recopiant sa sortie au fur et à mesure, et rend son code de sortie avec les survivants qu'il a écrits."""
    lines = "--lines" in command
    print(f"::group::mutator {command[-1]}{' (tranche)' if lines else ''} : {command[2]}", flush=True)
    printed = []
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") as process:
        for line in process.stdout:
            print(line, end="", flush=True)
            printed.append(line)
    print("::endgroup::", flush=True)
    return process.returncode, survivors(printed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", default="", help="motifs des modules de tests ; sinon, ceux de chaque fichier")
    parser.add_argument("--workers", type=lambda v: int(v) if v else None, help="mutants d'un fichier à la fois")
    parser.add_argument("--memory-mb", type=lambda v: int(v) if v else None, help="mémoire de chaque lancement de tests")
    parser.add_argument("--share", type=parse_share, default=None, help="« K/N » : une unité sur N, à partir de la K-ième")
    parser.add_argument("--mutator", default="../mutator/mutator")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    # Chemin absolu sans résoudre le lien du .venv : mutator lance les mutants dans des copies qui n'en ont pas.
    python = os.path.abspath(".venv/bin/python")
    commands = plan_part(Path.cwd(), args.paths, args.tests, python=python, mutator=args.mutator,
                         workers=args.workers, memory_mb=args.memory_mb, share=args.share)
    root = Path.cwd()
    accepted = load_equivalents(root / EQUIVALENTS)
    failure, survived, remaining = conclude(run_plan(commands, execute), root, accepted)
    print(f"Survivants hors de {EQUIVALENTS} : {len(remaining)}", flush=True)
    for name, number, mutation in remaining:
        print(f"  {name}:{number} {mutation}", flush=True)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"survived={'true' if survived else 'false'}\n")
    return failure


if __name__ == "__main__":
    sys.exit(main())
