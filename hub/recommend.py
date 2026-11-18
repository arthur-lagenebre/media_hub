"""Les recommandations « Parce que tu as aimé <titre> » : des films proches d'un film aimé, des séries proches d'une série aimée, chacun avec ce qu'ils partagent.

Contenu local et expliqué : la proximité se lit dans les étiquettes des éléments (réalisateurs, premiers acteurs, genres, saga, décennie), jamais dans un service externe. Le calcul ne lit que les données du compte qui demande (ses avis, sa progression) : aucun signal collectif (voir docs/decisions.md).

L'index des particularités de tous les films et séries est construit au premier appel puis gardé en mémoire (`Database.derived`) jusqu'à la prochaine écriture des éléments, comme celui des personnes (people.py). Ce qui dépend des droits du compte (les bibliothèques qu'il peut lire) se calcule à chaque appel, par bibliothèque : la rareté d'une particularité ne tient compte que de ce que le compte peut lire.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, namedtuple

from libraries.common.text import fold

from . import people, prefs

# ---------------------------------------------------------------------------
# Réglages : tout ce qui se règle d'après ce qu'on voit sur sa médiathèque est ici, et nulle part ailleurs.
# ---------------------------------------------------------------------------
WEIGHTS = {             # le poids d'une particularité partagée avec l'ancre, avant d'être multiplié par sa rareté (idf : log(N / nombre d'éléments qui la portent))
    "director": 3.0,    # un réalisateur en commun : le signal le plus fort
    "saga": 2.5,        # la même saga (`grp`) ; un seul élément par saga dans un rayon
    "actor": 1.5,       # un des TOP_ACTORS premiers acteurs en commun (des deux côtés)
    "genre": 1.0,
    "decade": 0.5,      # ne suffit jamais seul : il faut au moins une autre particularité en commun
}
TOP_ACTORS = 5          # les acteurs qui comptent : les premiers du casting (l'ordre des étiquettes)
MAX_SHARE = 0.40        # une particularité portée par plus de cette part des éléments (de la même sorte, dans les bibliothèques lisibles) est ignorée : « Drame » ne rapproche rien
ANCHOR_POOL = 10        # les ancres se choisissent parmi les 10 plus récentes : avis `up` d'abord (du plus récent au plus ancien), puis ce qui est terminé sans avis
MIN_ANCHORS = 2         # en dessous, pas assez de signal : aucun rayon
MAX_SHELVES = 2         # au plus deux rayons « Parce que tu as aimé »
SHELF_MIN = 4           # un rayon plus court est abandonné, et l'ancre suivante essayée
SHELF_FIRST = 12        # les premiers éléments d'un rayon, retenus avant de passer à l'ancre suivante : le rayon suivant garde de quoi exister, même si les deux ancres se disputent les mêmes films
SHELF_MAX = prefs.SHELF_CARDS         # éléments par rayon, au plus : au-delà de SHELF_FIRST, les rayons gardés se complètent chacun leur tour
WHY_MAX = 2             # les particularités partagées données en explication : les plus lourdes
QUALITY_PIVOT = 6.5     # note TMDB (sur 10, montrée seulement : assez de votes) qui ne change rien ; au-dessus un petit bonus, en dessous un petit malus
QUALITY_SLOPE = 0.3     # points de proximité par point de note d'écart au pivot
QUALITY_CAP = 0.6       # bonus ou malus de note, au plus : la note départage, elle ne fait pas la proximité
DOWN_WEIGHT = 0.3       # part de la ressemblance avec ce que le compte a noté `down` retirée à la proximité
DOWN_CAP = 1.5          # malus « ressemble à ce que je n'aime pas », au plus
MIN_SCORE = 0.0         # proximité finale (bonus et malus compris) à dépasser pour être proposé

KINDS = ("director", "saga", "actor", "genre", "decade")         # l'ordre de départage des explications de même poids
TYPES = ("movie", "show")

Item = namedtuple("Item", "row lib type sort count feats vote saga")      # `feats` : {(sorte, clé): texte affiché} ; `vote` : la note TMDB montrée, ou None ; `saga` : la clé de saga, ou ""


def people_features(meta):
    """{(rôle, clé): nom écrit} des réalisateurs et des TOP_ACTORS premiers acteurs d'un élément."""
    found, actors = {}, 0
    for role, name in people.names_in(meta):
        actors += role == "actor"
        if role == "director" or actors <= TOP_ACTORS:
            found.setdefault((role, people.key_of(name)), name)
    return found


def genre_features(row):
    """{("genre", clé): genre écrit} d'un élément ; une liste illisible n'en donne aucun."""
    try:
        genres = json.loads(row["genres"])
    except ValueError:
        return {}
    named = [g.strip() for g in genres if isinstance(g, str) and g.strip()] if isinstance(genres, list) else []
    return {("genre", fold(g)): g for g in reversed(named)}          # à clé égale, la première graphie l'emporte


def features(row, meta):
    """{(sorte, clé): texte} des particularités d'un élément : réalisateurs, TOP_ACTORS premiers acteurs, genres, saga, décennie. La clé ignore accents, casse et espaces en trop."""
    found = {**people_features(meta), **genre_features(row)}
    if row["grp"].strip():
        found[("saga", fold(row["grp"].strip()))] = row["grp"].strip()
    year = row["year"][:4]
    if year.isdigit():
        found[("decade", year[:3] + "0")] = year[:3] + "0"
    return found


def build(db, shown):
    """{"items": {id: Item}, "posting": {particularité: [id]}, "counts": {(bibliothèque, sorte d'élément): Counter(particularité)}, "sizes": Counter((bibliothèque, sorte d'élément))}.
    `shown(meta)` : la note TMDB montrée d'un élément, ou None. Une seule lecture de `items`, `meta` lu une fois par élément."""
    items, posting, counts, sizes = {}, {}, {}, Counter()
    for row in db.query("SELECT * FROM items WHERE type IN (?, ?)", TYPES):
        meta = people.meta_of(row) or {}
        feats = features(row, meta)
        group = (row["lib"], row["type"])
        items[row["id"]] = Item(row, row["lib"], row["type"], row["sort"], row["count"], feats, shown(meta), fold(row["grp"].strip()))
        sizes[group] += 1
        counts.setdefault(group, Counter()).update(feats.keys())
        for feat in feats:
            posting.setdefault(feat, []).append(row["id"])
    return {"items": items, "posting": posting, "counts": counts, "sizes": sizes}


def index(db, shown):
    return db.derived("recommend", lambda: build(db, shown))


PROGRESS_SQL = ("SELECT e.item_id AS item, SUM(p.done) AS seen, COUNT(*) AS touched, MAX(p.updated) AS last FROM progress p JOIN entries e ON e.id=p.entry_id "
                "WHERE p.profile=? AND e.kind != 'audio' GROUP BY e.item_id")


def progress_of(db, profile):
    """{id d'élément: (fichiers vus, fichiers entamés, dernière mise à jour)} de ce compte, en une lecture (les deux premiers comme `seen_counts`)."""
    return {r["item"]: (r["seen"], r["touched"], r["last"]) for r in db.query(PROGRESS_SQL, (profile,))}


class Scope:
    """Ce qui vaut pour un compte et une sorte d'élément (films ou séries) : la rareté des particularités dans les bibliothèques qu'il peut lire, et ce qu'il a noté `down`."""

    def __init__(self, idx, kind, opinions, readable):
        self.groups = [idx["counts"].get((lib, kind), {}) for lib in readable]
        self.total = sum(idx["sizes"][(lib, kind)] for lib in readable)
        self.known = {}
        items = idx["items"]
        mine = [items[i] for i, (opinion, _) in opinions.items() if opinion == "down" and i in items]
        self.disliked = {f for it in mine if it.type == kind and it.lib in readable for f in it.feats if f[0] != "decade"}

    def weight(self, feat):
        """Le poids d'une particularité (WEIGHTS × idf), 0 quand elle est trop répandue (MAX_SHARE) ou absente."""
        if feat not in self.known:
            carried = sum(group.get(feat, 0) for group in self.groups)
            self.known[feat] = 0.0 if not carried or carried > MAX_SHARE * self.total else WEIGHTS[feat[0]] * math.log(self.total / carried)
        return self.known[feat]

    def penalty(self, cand):
        """Le malus d'un candidat qui ressemble à ce que le compte n'aime pas (hors décennie), DOWN_CAP au plus."""
        return min(DOWN_CAP, DOWN_WEIGHT * sum(self.weight(f) for f in cand.feats if f in self.disliked))


def anchors(idx, opinions, progress, readable):
    """Les ancres possibles, les plus récentes d'abord, ANCHOR_POOL au plus : ce que le compte a noté `up`, puis ce qu'il a terminé (tous les fichiers vus) sans l'avoir noté. Jamais d'une bibliothèque qu'il ne peut pas lire."""
    items = idx["items"]
    usable = lambda ident: ident in items and items[ident].lib in readable
    liked = sorted((ident for ident, (opinion, _) in opinions.items() if opinion == "up" and usable(ident)), key=lambda i: (-opinions[i][1], i))
    finished = sorted((ident for ident, (seen, _, _) in progress.items() if ident not in opinions and usable(ident) and seen >= max(1, items[ident].count)),
                      key=lambda i: (-progress[i][2], i))
    return (liked + finished)[:ANCHOR_POOL]


def quality(vote):
    return 0.0 if vote is None else max(-QUALITY_CAP, min(QUALITY_CAP, (vote - QUALITY_PIVOT) * QUALITY_SLOPE))


def shared(anchor, cand, scope):
    """[(poids, sorte, texte)] des particularités que l'ancre et le candidat partagent et qui comptent, de la plus lourde à la plus légère."""
    found = [(scope.weight(feat), feat[0], text) for feat, text in anchor.feats.items() if feat in cand.feats]
    return sorted((f for f in found if f[0] > 0), key=lambda f: (-f[0], KINDS.index(f[1]), f[2]))


Breakdown = namedtuple("Breakdown", "score common bonus malus")          # `common` : voir shared ; `bonus` : la note (quality) ; `malus` : la ressemblance avec ce que le compte n'aime pas (Scope.penalty)


def breakdown(anchor, cand, scope):
    """La proximité d'un candidat à l'ancre et ce qui la compose : la somme des particularités partagées, plus la note, moins le malus."""
    common, bonus, malus = shared(anchor, cand, scope), quality(cand.vote), scope.penalty(cand)
    return Breakdown(sum(w for w, _, _ in common) + bonus - malus, common, bonus, malus)


def reached(idx, anchor, allowed, scope):
    """Les candidats permis (`allowed(id)`) qui partagent avec l'ancre au moins une particularité qui compte, autre que la décennie."""
    found = set()
    for feat in anchor.feats:
        if feat[0] != "decade" and scope.weight(feat) > 0:
            found.update(i for i in idx["posting"].get(feat, ()) if allowed(i))
    return found


def ranked(idx, anchor, allowed, scope):
    """[(Item, particularités partagées)] des candidats, du plus proche au moins proche : proximité (bonus de note et malus compris), puis note, titre, identifiant."""
    found = []
    for ident in reached(idx, anchor, allowed, scope):
        cand = idx["items"][ident]
        score, common, _, _ = breakdown(anchor, cand, scope)
        if score > MIN_SCORE:
            found.append(((-score, -(cand.vote or 0), cand.sort, ident), cand, common))
    return [(cand, common) for _, cand, common in sorted(found, key=lambda f: f[0])]


def shelf(idx, anchor, allowed, scope):
    """[(Item, explication)] : tous les candidats, du plus proche de l'ancre au moins proche, un seul par saga (le mieux placé)."""
    out, sagas = [], set()
    for cand, common in ranked(idx, anchor, allowed, scope):
        if cand.saga not in sagas:
            sagas.update({cand.saga} - {""})
            out.append((cand, [{"kind": kind, "value": text} for _, kind, text in common[:WHY_MAX]]))
    return out


def top_up(kept, used):
    """Complète les rayons `kept` ([(cartes retenues, cartes en réserve)]) jusqu'à SHELF_MAX, une carte chacun à son tour, pour qu'aucun ne prenne tout. Une carte déjà dans `used` est passée ; celles ajoutées y entrent."""
    waiting = [(shown, iter(spare)) for shown, spare in kept]
    while waiting:
        going_on = []
        for shown, spare in waiting:
            card = next((c for c in spare if c[0].row["id"] not in used), None)
            if card and len(shown) < SHELF_MAX:
                shown.append(card)
                used.add(card[0].row["id"])
                going_on.append((shown, spare))
        waiting = going_on


def candidates(items, anchor, opinions, progress, readable, used):
    """Ce qui peut figurer dans le rayon d'une ancre : de la même sorte, d'une bibliothèque lisible, à voir (aucun fichier entamé), pas noté `down`, ni l'ancre ni ce qu'un autre rayon montre déjà."""
    return lambda i: (items[i].type == anchor.type and items[i].lib in readable and i not in progress and opinions.get(i, ("",))[0] != "down"
                      and i != anchor.row["id"] and i not in used)


def attempts(idx, opinions, progress, readable, profile, day):
    """Chaque ancre essayée, dans l'ordre : (ancre, [(Item, explication)], gardé ?, Scope de sa sorte). S'arrête au MAX_SHELVES-ième rayon gardé ; rien sans assez de signal (moins de MIN_ANCHORS ancres possibles).
    C'est le calcul de `shelves`, avec les rayons abandonnés en plus : `python -m hub recommend` les montre."""
    possible = anchors(idx, opinions, progress, readable)
    if len(possible) < MIN_ANCHORS:
        return
    possible.sort(key=lambda ident: hashlib.sha1(f"{day}:{profile}:{ident}".encode()).hexdigest())
    items, used, tried, kept, scopes = idx["items"], set(), [], [], {}
    for anchor in (items[ident] for ident in possible):
        if anchor.type not in scopes:
            scopes[anchor.type] = Scope(idx, anchor.type, opinions, readable)
        found = shelf(idx, anchor, candidates(items, anchor, opinions, progress, readable, used), scopes[anchor.type])
        shown, keep = found[:SHELF_FIRST], len(found) >= SHELF_MIN
        tried.append((anchor, shown, keep, scopes[anchor.type]))
        if keep:
            used.update(cand.row["id"] for cand, _ in shown)
            kept.append((shown, found[SHELF_FIRST:]))
        if len(kept) == MAX_SHELVES:
            break
    top_up(kept, used)
    yield from tried


def shelves(idx, opinions, progress, readable, profile, day):
    """[(ancre, [(Item, explication)])] : au plus MAX_SHELVES rayons, [] sans assez de signal.

    `opinions` : {id: (avis, date)} du compte ; `progress` : voir progress_of ; `readable` : les bibliothèques vidéo qu'il peut lire. Les ancres se choisissent parmi les ANCHOR_POOL plus récentes dans un ordre qui ne dépend que du compte et du jour (le même toute la journée, un autre demain), comme « Pas encore vu ». Un rayon de moins de SHELF_MIN éléments est abandonné ; un élément n'est jamais dans deux rayons. Chaque rayon retient d'abord ses SHELF_FIRST premiers éléments, et ce n'est qu'une fois les rayons choisis qu'ils se complètent jusqu'à SHELF_MAX : un long rayon ne fait jamais disparaître le suivant."""
    return [(anchor, found) for anchor, found, keep, _ in attempts(idx, opinions, progress, readable, profile, day) if keep]
