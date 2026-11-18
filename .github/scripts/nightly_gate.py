"""Décide si la passe de nuit (`nightly.yml`) doit tourner : écrit `run=true` ou `run=false`.

La nuit, elle est sautée si ce commit de main a déjà été entièrement traité. « Traité » ne veut pas
dire « vert » : un survivant laisse le job rouge à sa dernière étape, mais l'étape Mutator a réussi.
Une partie annulée à la limite, ou dont la machine a disparu, n'est pas traitée : elle reprendra.
Un lancement à la main tourne toujours.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

STEP = "Mutator"


def is_treated(jobs: list[dict]) -> bool:
    crappers = [job for job in jobs if job["name"] == "crapper"]
    parts = [job for job in jobs if job["name"].startswith("mutator (")]
    return (len(crappers) == 1 and crappers[0]["conclusion"] == "success" and bool(parts)
            and all(any(step["name"] == STEP and step["conclusion"] == "success" for step in job["steps"])
                    for job in parts))


def treating_run(repository: str, sha: str, api) -> int | None:
    """La passe qui a entièrement traité ce commit, s'il y en a une."""
    runs = api(f"repos/{repository}/actions/workflows/nightly.yml/runs"
               f"?branch=main&head_sha={sha}&status=completed&per_page=30")
    for run in runs["workflow_runs"]:
        if is_treated(api(f"repos/{repository}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]):
            return run["id"]
    return None


def github_api(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def should_run(event: str, repository: str, sha: str, api) -> bool:
    if event != "schedule":
        return True
    try:
        run = treating_run(repository, sha, api)
    except (subprocess.CalledProcessError, KeyError, ValueError) as error:
        # Mieux vaut une passe de trop qu'une nuit perdue sur un incident de l'API.
        print(f"::warning title=Nuit::passes précédentes illisibles ({error}) : la passe tourne")
        return True
    if run is None:
        return True
    print(f"::notice title=Nuit::commit {sha} déjà entièrement traité (run {run}) : rien à faire")
    return False


def main(api=github_api) -> int:
    run = should_run(os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"], api)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"run={'true' if run else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
