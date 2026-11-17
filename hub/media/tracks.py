"""Le choix d'office des pistes d'une vidéo d'après les réglages du compte (voir prefs.py) : la piste audio, avant le plan de lecture, et un sous-titre.

Fonctions pures. Une piste dont la langue est absente ou inconnue (« und ») n'est jamais choisie d'office ; un sous-titre en image (PGS, VobSub) non plus,
car l'extraire peut durer : il reste au choix de l'utilisateur dans le lecteur.
"""

from __future__ import annotations

from .subs import canonical

SUB_MODES = ("never", "forced", "foreign", "always")       # aucun ; les forcés seulement ; les complets quand le son n'est pas dans une langue comprise ; toujours les complets


def _of_lang(found, lang):
    """Les pistes de `found` dans la langue `lang` (code canonique), celle marquée par défaut d'abord."""
    return sorted((t for t in found if canonical(t["lang"]) == lang), key=lambda t: not t.get("default"))


def pick_audio(audio, langs):
    """L'index de la piste audio à jouer d'après `langs` (codes canoniques, par ordre de préférence) : la première langue préférée présente, hors commentaires.
    None quand rien ne correspond ou que `langs` est vide : le plan prend alors la piste par défaut du fichier (et garde la lecture directe)."""
    spoken = [t for t in audio if not t["commentary"]]
    for lang in langs:
        found = _of_lang(spoken, lang)
        if found:
            return found[0]["index"]
    return None


def _first(subs, langs, forced):
    for lang in langs:
        found = _of_lang([s for s in subs if bool(s["forced"]) == forced], lang)
        if found:
            return found[0]["id"]
    return None


def pick_sub(subs, mode, langs, audio_lang="", understood=()):
    """L'id du sous-titre à afficher d'office, ou None. `subs` : ce que rend subtitle_list ; `langs` : les langues de sous-titres préférées, dans l'ordre ;
    `audio_lang` : la langue de la piste audio jouée ; `understood` : d'autres langues que le compte comprend (ses langues audio préférées).

    `forced` : les forcés de la première langue préférée qui en a. `always` : les complets, sinon les forcés. `foreign` : comme `always` si le son est dans une
    langue connue que le compte ne comprend pas, sinon comme `forced` (un son de langue inconnue n'impose pas de sous-titres complets)."""
    text = [s for s in subs if s["text"]]
    heard = canonical(audio_lang)
    full = mode == "always" or (mode == "foreign" and bool(heard) and heard not in {*langs, *understood})
    if mode not in SUB_MODES or mode == "never":
        return None
    return (_first(text, langs, forced=False) if full else None) or _first(text, langs, forced=True)
