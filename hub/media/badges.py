"""Les badges d'une vidéo, tirés de son résumé ffprobe (voir probe.summarize) : qualité d'image, HDR, langues du son et des sous-titres. Et ceux d'une saison, tirés de ses épisodes.

Fonctions pures : rien n'est lu sur le disque ici. Un fichier pas encore sondé n'a pas de badge.
"""

from __future__ import annotations

from collections import Counter

# De la meilleure à la moins bonne : (nom, largeur minimale, hauteur minimale). L'une OU l'autre suffit : un film en 2.39:1 fait 1920x800, un 4:3 fait 1440x1080.
QUALITIES = (("4k", 3200, 1800), ("fullhd", 1800, 1000), ("hd", 1200, 700))
LOWEST = "sd"
RANK = {name: rank for rank, name in enumerate([LOWEST] + [q[0] for q in reversed(QUALITIES)])}

# ISO 639-2 (les deux formes, bibliographique et terminologique) -> ISO 639-1. Un code à trois lettres absent d'ici n'a pas de badge (« und », « zxx », « mul », les langues sans code à deux lettres).
THREE_LETTERS = {
    "fre": "fr", "fra": "fr", "eng": "en", "ger": "de", "deu": "de", "spa": "es", "ita": "it", "por": "pt", "jpn": "ja", "chi": "zh", "zho": "zh", "kor": "ko", "rus": "ru",
    "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no", "nob": "nb", "nno": "nn", "dan": "da", "fin": "fi", "ice": "is", "isl": "is", "pol": "pl", "cze": "cs", "ces": "cs",
    "slo": "sk", "slk": "sk", "slv": "sl", "hun": "hu", "rum": "ro", "ron": "ro", "bul": "bg", "gre": "el", "ell": "el", "tur": "tr", "ara": "ar", "heb": "he", "hin": "hi",
    "tha": "th", "vie": "vi", "ind": "id", "may": "ms", "msa": "ms", "ukr": "uk", "hrv": "hr", "srp": "sr", "bos": "bs", "cat": "ca", "baq": "eu", "eus": "eu", "glg": "gl",
    "est": "et", "lav": "lv", "lit": "lt", "per": "fa", "fas": "fa", "ben": "bn", "tam": "ta", "tel": "te", "urd": "ur", "lat": "la", "gle": "ga", "wel": "cy", "cym": "cy",
    "mac": "mk", "mkd": "mk", "alb": "sq", "sqi": "sq", "arm": "hy", "hye": "hy", "geo": "ka", "kat": "ka", "bre": "br", "cos": "co", "oci": "oc", "fil": "tl", "tgl": "tl",
}


def quality(width, height):
    """« 4k », « fullhd », « hd » ou « sd », d'après la largeur OU la hauteur de l'image."""
    return next((name for name, min_width, min_height in QUALITIES if width >= min_width or height >= min_height), LOWEST)


def lang_code(code):
    """Le code ISO 639-1 (deux lettres, en minuscules) de ce qu'une piste donne pour langue : « fre », « FRA », « fr-FR » -> « fr ». Vide quand il n'y en a pas (« », « und », code inconnu)."""
    code = (code or "").strip().lower().replace("_", "-").partition("-")[0]
    if len(code) == 2 and code.isalpha():
        return code
    return THREE_LETTERS.get(code, "")


def languages(codes):
    """Les langues de ces pistes, une fois chacune, dans l'ordre des pistes."""
    return list(dict.fromkeys(lang for lang in map(lang_code, codes) if lang))


def of_probe(probe, side_subs=()):
    """{quality, hdr, audio, subs} d'un fichier sondé ; vide s'il n'a pas d'image. `side_subs` : les sous-titres posés à côté de la vidéo (voir subs.side_files), comptés après ceux du fichier.

    Un commentaire n'est pas une langue du film ; des sous-titres forcés (les seuls passages en langue étrangère) ne sont pas des sous-titres du film."""
    video = probe.get("video")
    if not video:
        return {}
    return {"quality": quality(video["width"], video["height"]), "hdr": bool(video["hdr"]),
            "audio": languages(t["lang"] for t in probe["audio"] if not t["commentary"]),
            "subs": languages(s["lang"] for s in [*probe["subs"], *side_subs] if not s["forced"])}


def of_season(episodes):
    """Les badges d'une saison d'après ceux de ses épisodes sondés (`episodes` : ce que rend of_probe, sans les vides) ; vide si aucun ne l'est.

    La qualité est celle du plus grand nombre d'épisodes (la meilleure en cas d'égalité) ; HDR si tous le sont ; les langues sont celles que tous ont, dans l'ordre du premier."""
    if not episodes:
        return {}
    counts = Counter(e["quality"] for e in episodes)
    return {"quality": max(counts, key=lambda name: (counts[name], RANK[name])), "hdr": all(e["hdr"] for e in episodes),
            "audio": shared(e["audio"] for e in episodes), "subs": shared(e["subs"] for e in episodes)}


def shared(lists):
    """Ce que toutes ces listes ont en commun, dans l'ordre de la première."""
    first, *others = lists
    return [x for x in first if all(x in other for other in others)]
