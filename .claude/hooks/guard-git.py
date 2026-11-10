"""Hook PreToolUse sur Bash : refuse les commandes git que les garde-fous de
/ship et /merged interdisent déjà en toutes lettres.

Refusé : push forcé, push vers main, suppression d'une branche distante,
--no-verify, `git branch -D`, `git add -A` / `git add .`, commit sur main,
`gh pr merge`. Le refus dit quoi faire : l'utilisateur peut lancer la commande
lui-même s'il la veut vraiment.

Les mots entre guillemets et dans un heredoc (messages de commit, description
de PR) ne comptent pas : seule la commande elle-même est examinée.
"""
import json
import re
import subprocess
import sys

PROTECTED = {"main", "master"}
QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)(\w+)\1")
SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")
TAIL = " Si tu en as vraiment besoin, demande à l'utilisateur de le lancer lui-même."


def drop_heredocs(command):
    kept, end = [], None
    for line in command.split("\n"):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        kept.append(line)
        found = HEREDOC.search(line)
        if found:
            end = found.group(2)
    return "\n".join(kept)


def segments(command):
    """Les commandes d'une ligne Bash, sans leurs textes entre guillemets, découpées par token."""
    bare = QUOTED.sub('""', drop_heredocs(command))
    for part in SEPARATORS.split(bare):
        tokens = part.split()
        while tokens and re.match(r"\w+=", tokens[0]):
            tokens.pop(0)  # VAR=valeur git ...
        if tokens:
            yield tokens


def git_call(tokens):
    """(sous-commande, arguments, a_change_de_depot) d'un appel git, options globales mises de côté."""
    i, elsewhere = 1, False
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] in ("-C", "-c"):
            elsewhere = elsewhere or tokens[i] == "-C"
            i += 1
        i += 1
    return (tokens[i] if i < len(tokens) else ""), tokens[i + 1:], elsewhere


def refuse_push(args):
    for arg in args:
        if re.match(r"^-[a-zA-Z]*f", arg) or arg.startswith("--force") or arg.startswith("+"):
            return "Pas de push forcé."
        if arg in ("--delete", "-d") or (arg.startswith(":") and len(arg) > 1):
            return "Pas de suppression d'une branche distante."
        if not arg.startswith("-") and arg.split(":")[-1].removeprefix("refs/heads/") in PROTECTED:
            return "Pas de push vers main : on passe par une branche et une pull request."
    return None


def refuse_git(sub, args, on_protected):
    if "--no-verify" in args or (sub == "commit" and "-n" in args):
        return "Pas de --no-verify : les vérifications existent pour servir."
    if sub == "push":
        return refuse_push(args)
    if sub == "branch" and any(re.match(r"^-[a-zA-Z]*D", arg) for arg in args):
        return "Pas de `git branch -D` : `-d` refuse ce qui n'est pas fusionné, et c'est voulu."
    if sub == "add" and any(arg in (".", "--all") or re.match(r"^-[a-zA-Z]*A", arg) for arg in args):
        return "Pas de `git add -A` ni `git add .` : ajoute les fichiers un par un, après avoir relu `git status`."
    if sub == "commit" and on_protected():
        return "Jamais de commit direct sur main : crée d'abord une branche."
    return None


def decide(event, branch_of):
    """Renvoie la réponse du hook (dict) ou None pour laisser passer. `branch_of(cwd)` donne la branche courante."""
    if event.get("tool_name") != "Bash":
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    moved = False  # un changement de branche ou de dossier plus tôt : la branche d'avant ne vaut plus
    for tokens in segments(command):
        if tokens[0] == "cd":
            moved = True
            continue
        if tokens[:3] == ["gh", "pr", "merge"]:
            reason = "Ne fusionne jamais une pull request : c'est à l'utilisateur de le faire."
        elif tokens[0] == "git":
            sub, args, elsewhere = git_call(tokens)
            moved = moved or elsewhere
            reason = refuse_git(sub, args, lambda: not moved and branch_of(event.get("cwd") or ".") in PROTECTED)
            moved = moved or sub in ("switch", "checkout")
        else:
            continue
        if reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason + TAIL,
                }
            }
    return None


def current_branch(cwd):
    try:
        done = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return  # entrée illisible : ne rien bloquer
    answer = decide(event, current_branch)
    if answer:
        print(json.dumps(answer))  # ASCII : sous Windows, la sortie suit la page de code, pas l'UTF-8


if __name__ == "__main__":
    main()
