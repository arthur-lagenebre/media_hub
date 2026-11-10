"""Hook PreToolUse sur le lancement d'un agent : demande une confirmation à
l'utilisateur quand on démarre une implémentation entre 23 h et 6 h.

Seuls les agents qui écrivent des fichiers sont concernés ; hub-architect et
hub-reviewer sont en lecture seule et partent sans question. L'heure est celle
de la machine qui exécute Claude Code.
"""
import datetime
import json
import sys

# Agents qui modifient le dépôt.
IMPLEMENTERS = {"hub-server", "hub-ui", "hub-deploy", "hub-tester"}
NIGHT_START, NIGHT_END = 23, 6  # de 23 h 00 à 5 h 59


def is_night(now):
    return now.hour >= NIGHT_START or now.hour < NIGHT_END


def decide(event, now):
    """Renvoie la réponse du hook (dict) ou None pour laisser passer."""
    agent = (event.get("tool_input") or {}).get("subagent_type")
    if agent not in IMPLEMENTERS or not is_night(now):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                "Il est %s : lancer l'implémentation avec %s entre 23 h et 6 h ?"
                % (now.strftime("%H:%M"), agent)
            ),
        }
    }


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return  # entrée illisible : ne rien bloquer
    answer = decide(event, datetime.datetime.now())
    if answer:
        print(json.dumps(answer))  # ASCII : sous Windows, la sortie suit la page de code, pas l'UTF-8


if __name__ == "__main__":
    main()
