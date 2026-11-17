"""Lance les tests de plusieurs modules sur N processus, les classes les plus longues d'abord.

    python -m tests.parallel [-j 6] tests.test_ui tests.test_popups ...

Chaque processus lance `unittest` sur sa part de classes (`--worker`, plus bas) et chronomètre chacune ; le lanceur
garde ces durées dans `.metrics/test-durations.json` (ignoré par git) pour mieux répartir la fois suivante, puis affiche
le total des tests, échecs, erreurs et sautés, et sort avec un code non nul au moindre échec.

Une classe de tests est l'unité de répartition : ses `setUpClass` / `tearDownClass` (serveur, navigateur) ne tournent
qu'une fois, dans un seul processus. Les tests d'interface démarrent chacun leur serveur sur un port libre dans un
dossier temporaire : plusieurs processus ne se gênent pas.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DURATIONS = ROOT / ".metrics" / "test-durations.json"
DEFAULT_JOBS = 6
DEFAULT_SECONDS = 1.0       # poids d'une classe inconnue quand aucune durée n'est connue (première exécution) : toutes pèsent pareil
MARK = "PARALLEL-RESULT "   # préfixe de la ligne que chaque processus écrit après chaque classe : {"class": ..., "run": ..., ...}
COUNTS = ("run", "failures", "errors", "skipped")


# --- Répartition (fonctions pures) ------------------------------------------------
def estimate(name: str, durations: dict[str, float]) -> float:
    """Durée supposée d'une classe : la sienne si on la connaît, sinon la médiane des durées connues (ni la plus courte
    ni la plus longue : une classe nouvelle est plus souvent ordinaire que minuscule), sinon `DEFAULT_SECONDS`."""
    if name in durations:
        return durations[name]
    return statistics.median(durations.values()) if durations else DEFAULT_SECONDS


def split(classes: list[str], durations: dict[str, float], jobs: int) -> list[list[str]]:
    """Algorithme glouton LPT : les classes par durée décroissante, chacune au processus le moins chargé.
    Pas de processus vide ; égalités tranchées par le nom, donc la même entrée donne toujours la même répartition."""
    weight = {name: estimate(name, durations) for name in classes}
    buckets: list[list[str]] = [[] for _ in range(max(1, min(jobs, len(classes))))]
    loads = [0.0] * len(buckets)
    for name in sorted(classes, key=lambda n: (-weight[n], n)):
        lightest = loads.index(min(loads))
        buckets[lightest].append(name)
        loads[lightest] += weight[name]
    return [bucket for bucket in buckets if bucket]


def totals(results: dict[str, dict]) -> dict[str, int]:
    return {key: sum(result[key] for result in results.values()) for key in COUNTS}


def exit_code(total: dict[str, int], crashed: int) -> int:
    """Non nul au moindre échec ou erreur, ou si une classe n'a pas rendu de résultat (processus mort)."""
    return 1 if total["failures"] or total["errors"] or crashed else 0


# --- Mémoire des durées ---------------------------------------------------------------
def load_durations(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: float(seconds) for name, seconds in data.items()} if isinstance(data, dict) else {}


def save_durations(path: Path, measured: dict[str, float]) -> None:
    """Les durées mesurées remplacent les anciennes ; celles des classes non lancées cette fois restent."""
    merged = {**load_durations(path), **measured}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(merged.items())), indent=1) + "\n", encoding="utf-8")


# --- Découverte -------------------------------------------------------------------------------
def discover(names: list[str]) -> list[str]:
    """Les classes de tests (`module.Classe`) qui ont des tests, pour chaque module (ou classe) demandé. Une classe que
    le module ne fait qu'importer (comme `UiCase`) est celle de son propre module, pas de celui-ci."""
    found: list[str] = []
    loader = unittest.TestLoader()
    for name in names:
        try:
            suite = loader.loadTestsFromName(name)
        except Exception as error:
            raise SystemExit(f"{name} : introuvable ({error})")
        for test in _flatten(suite):
            cls = type(test)
            if cls.__module__ == "unittest.loader":     # _FailedTest : le module ne s'importe pas
                raise SystemExit(f"{name} : import impossible ({getattr(test, '_exception', test)})")
            full = f"{cls.__module__}.{cls.__qualname__}"
            if (full == name or full.startswith(name + ".")) and full not in found:
                found.append(full)
    return found


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


# --- Un processus : `--worker` ----------------------------------------------------------------
def work(classes: list[str]) -> int:
    """Lance chaque classe avec le runner standard (sortie normale, comme `python -m unittest`) et écrit, après chacune,
    sa ligne de résultat chronométrée. Le code de sortie est celui de `unittest`."""
    loader = unittest.TestLoader()
    ok = True
    for name in classes:
        started = time.perf_counter()
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(loader.loadTestsFromName(name))
        ok = ok and result.wasSuccessful()
        line = {"class": name, "run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                "skipped": len(result.skipped), "seconds": round(time.perf_counter() - started, 2)}
        print(MARK + json.dumps(line), flush=True)
    return 0 if ok else 1


def run_worker(classes: list[str]) -> tuple[subprocess.CompletedProcess, dict[str, dict], str]:
    """Lance un processus sur ses classes ; rend sa fin, les résultats par classe et sa sortie sans les lignes de résultat."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    done = subprocess.run([sys.executable, "-m", "tests.parallel", "--worker", *classes], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    results, shown = {}, []
    for line in (done.stdout + done.stderr).splitlines():
        if line.startswith(MARK):
            entry = json.loads(line[len(MARK):])
            results[entry.pop("class")] = entry
        else:
            shown.append(line)
    return done, results, "\n".join(shown)


# --- Le lanceur ---------------------------------------------------------------------------------
def report(buckets: list[list[str]], outcomes: list) -> tuple[dict[str, dict], int]:
    """Une ligne par processus (la sortie complète seulement s'il a échoué ou est mort) ; rend les résultats par classe
    et le nombre de classes restées sans résultat."""
    results: dict[str, dict] = {}
    crashed = 0
    for number, (bucket, (done, found, output)) in enumerate(zip(buckets, outcomes), 1):
        results.update(found)
        lost = [name for name in bucket if name not in found]
        crashed += len(lost)
        mine = totals(found)
        seconds = sum(result["seconds"] for result in found.values())
        print(f"processus {number} : {len(bucket)} classes, {mine['run']} tests, {mine['failures']} échecs, {mine['errors']} erreurs, {mine['skipped']} sautés, {seconds:.0f} s", flush=True)
        if lost:
            print(f"  sans résultat (processus mort, code {done.returncode}) : {', '.join(lost)}")
        if done.returncode or lost:
            print(output)
    return results, crashed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lance les tests de modules donnés sur plusieurs processus.")
    parser.add_argument("modules", nargs="*", help="modules (tests.test_ui) ou classes (tests.test_ui.TestHome)")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS, help=f"nombre de processus (défaut {DEFAULT_JOBS})")
    parser.add_argument("--durations", type=Path, default=DURATIONS, help="fichier des durées par classe")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if args.worker:
        return work(args.modules)
    if args.jobs < 1 or not args.modules:
        parser.error("au moins un module, et -j au moins 1")

    classes = discover(args.modules)
    buckets = split(classes, load_durations(args.durations), args.jobs)
    print(f"{len(classes)} classes sur {len(buckets)} processus", flush=True)
    started = time.perf_counter()
    with ThreadPoolExecutor(len(buckets)) as pool:
        outcomes = list(pool.map(run_worker, buckets))

    results, crashed = report(buckets, outcomes)
    save_durations(args.durations, {name: result["seconds"] for name, result in results.items()})

    total = totals(results)
    print(f"Total : {total['run']} tests, {total['failures']} échecs, {total['errors']} erreurs, {total['skipped']} sautés, en {time.perf_counter() - started:.0f} s")
    if total["skipped"]:
        print(f"ATTENTION : {total['skipped']} sautés (Playwright ou ffmpeg absent ?) : ce ne sont pas des réussites.")
    return exit_code(total, crashed)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
