"""Fabrique une petite médiathèque de démonstration pour essayer media_hub sans NAS.

    python tools/make_demo.py ./demo            # crée ./demo/Films, ./demo/Series, ...
    HUB_SRV=./demo HUB_DATA=./demo-data python -m hub

Les affiches sont dessinées (Pillow, si installé) ; les films sont des .mkv étiquetés comme le fait media_library.
Un film (« Mire de réglage ») est un vrai fichier vidéo de quelques secondes, avec une piste audio AC-3 5.1 et des sous-titres : de quoi essayer le lecteur (ffmpeg requis pour celui-là).
"""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from tests import fixtures as fx  # noqa: E402

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                       # pas de Pillow : les affiches seront de simples aplats
    Image = None

FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Helvetica.ttc"]


def poster(title, hue, size=(400, 600), square=False):
    if Image is None:
        return fx.PNG
    w, h = (500, 500) if square else size
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):                                   # dégradé vertical
        t = y / h
        c = Image.new("HSV", (1, 1), (int(hue * 255 / 360) % 256, int(150 + 60 * t), int(200 - 120 * t))).convert("RGB").getpixel((0, 0))
        draw.line([(0, y), (w, y)], fill=c)
    font = next((ImageFont.truetype(f, w // 9) for f in FONTS if Path(f).exists()), ImageFont.load_default())
    words, lines, line = title.split(), [], ""
    for word in words:
        if draw.textlength((line + " " + word).strip(), font=font) > w * 0.84 and line:
            lines.append(line); line = word
        else:
            line = (line + " " + word).strip()
    lines.append(line)
    y = h * 0.55 - len(lines) * w / 8
    for text in lines:
        draw.text(((w - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=(255, 255, 255))
        y += w / 8
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


FILMS = [
    ("Inception", 2010, "Science-Fiction, Action", "Un voleur capable de s'introduire dans les rêves reçoit une dernière mission : y planter une idée.", "Christopher Nolan", 148, 210),
    ("Interstellar", 2014, "Science-Fiction, Drame", "Des astronautes traversent un trou de ver pour sauver l'humanité.", "Christopher Nolan", 169, 230),
    ("Le Voyage de Chihiro", 2001, "Animation, Fantastique", "Une fillette est piégée dans le monde des esprits.", "Hayao Miyazaki", 125, 330),
    ("Mad Max: Fury Road", 2015, "Action, Aventure", "Une course-poursuite dans le désert.", "George Miller", 120, 20),
    ("Parasite", 2019, "Thriller, Drame", "Une famille pauvre s'infiltre chez des riches.", "Bong Joon-ho", 132, 130),
    ("Blade Runner 2049", 2017, "Science-Fiction", "Un blade runner déterre un secret enfoui.", "Denis Villeneuve", 164, 190),
    ("Amélie", 2001, "Comédie, Romance", "Une serveuse de Montmartre réenchante la vie des autres.", "Jean-Pierre Jeunet", 122, 45),
    ("Le Parrain", 1972, "Drame, Crime", "La saga d'une famille mafieuse.", "Francis Ford Coppola", 175, 5),
    ("Alien", 1979, "Horreur, Science-Fiction", "Dans l'espace, personne ne vous entend crier.", "Ridley Scott", 117, 0),
    ("Whiplash", 2014, "Drame, Musique", "Un jeune batteur face à un professeur impitoyable.", "Damien Chazelle", 107, 280),
    ("Pulp Fiction", 1994, "Crime, Thriller", "Des histoires entrelacées à Los Angeles.", "Quentin Tarantino", 154, 60),
    ("Les Évadés", 1994, "Drame", "Deux détenus se lient d'amitié.", "Frank Darabont", 142, 150),
]
SAGAS = {"Saga Le Seigneur des Anneaux": [("La Communauté de l'Anneau", 2001), ("Les Deux Tours", 2002), ("Le Retour du roi", 2003)],
         "Saga Matrix": [("Matrix", 1999), ("Matrix Reloaded", 2003), ("Matrix Revolutions", 2003)]}
SHOWS = {"Dark": (2017, "Science-Fiction, Mystère", "Dans une petite ville allemande, la disparition d'enfants révèle des liens entre quatre familles.", 170, [3, 2]),
         "Severance": (2022, "Drame, Mystère", "Des employés dont la mémoire est scindée entre travail et vie privée.", 20, [3]),
         "Breaking Bad": (2008, "Drame, Crime", "Un professeur de chimie devient fabricant de drogue.", 30, [2])}
ANIME = {"Frieren": (2023, "Fantastique, Aventure", "Après la victoire sur le roi démon, une elfe découvre ce que signifie le temps.", 120, [3]),
         "Cowboy Bebop": (1998, "Science-Fiction, Action", "Des chasseurs de primes dérivent dans le système solaire.", 200, [3])}
ALBUMS = [("Daft Punk", "Discovery", 2001, "Electronic", ["One More Time", "Aerodynamic", "Digital Love", "Harder Better Faster Stronger"]),
          ("Daft Punk", "Random Access Memories", 2013, "Electronic", ["Give Life Back to Music", "Get Lucky", "Instant Crush"]),
          ("Air", "Moon Safari", 1998, "Electronic", ["La femme d'argent", "Sexy Boy", "Kelly Watch the Stars"]),
          ("Portishead", "Dummy", 1994, "Trip Hop", ["Mysterons", "Sour Times", "Glory Box"]),
          ("Radiohead", "OK Computer", 1997, "Rock", ["Airbag", "Paranoid Android", "Karma Police"]),
          ("Nina Simone", "I Put a Spell on You", 1965, "Jazz", ["Feeling Good", "Ain't Got No - I Got Life"])]
BOOKS = [("Manga", "Naruto", 5, "Kishimoto"), ("Manga", "One Piece", 4, "Oda"), ("Comics", "Saga", 3, "Brian K. Vaughan"), ("BD", "Blacksad", 3, "Díaz Canales")]


def write_comic(path, name, number, pages, **info):
    """Un .cbz dont chaque page est dessinée (couverture, puis pages numérotées)."""
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ComicInfo.xml", fx.comicinfo_xml(Series=name, Number=str(number), **info))
        for n in range(1, pages + 1):
            z.writestr(f"{n:03d}.jpg", poster(f"{name} {number}" if n == 1 else f"page {n}", (number * 47 + n * 9) % 360, (800, 1200)))


def build(root: Path):
    films = root / "Films"
    rnd = random.Random(7)
    for title, year, genres, synopsis, director, minutes, hue in FILMS:
        fx.write_mkv(films / f"{title} ({year})" / f"{title} ({year}).mkv", fx.movie_blocks(title, synopsis, f"{year}-06-01", f"movie/{rnd.randint(100, 99999)}", genres, [director], ["Acteur Un (Premier rôle)", "Actrice Deux (Second rôle)"]), cover=poster(title, hue), minutes=minutes)
    for n, (saga, parts) in enumerate(SAGAS.items()):
        for i, (title, year) in enumerate(parts, 1):
            fx.write_mkv(films / f"_{saga.split(' ', 1)[1]}" / f"{i} - {title}.mkv", fx.movie_blocks(title, "Un épisode de la saga.", f"{year}-12-10", f"movie/{rnd.randint(100, 99999)}", "Fantastique, Aventure", ["Réalisateur"], [], collection=saga, part=i), cover=poster(title, 20 + n * 120 + i * 18), minutes=150)
    fx.write_mkv(films / "Sans Etiquette (1999)" / "film.mkv", [], minutes=90)
    if fx.FFMPEG:
        movie = films / "Mire de réglage (2024)" / "Mire de réglage (2024).mkv"
        fx.write_video(movie, seconds=20, acodec="ac3", channels=6, subtitle="1\n00:00:01,000 --> 00:00:05,000\nBonjour, ceci est un <i>sous-titre</i>.\n\n2\n00:00:06,000 --> 00:00:10,000\nIl tient sur\ndeux lignes.\n")
    for kind, folder, shows in (("series", "Series", SHOWS), ("animations", "Animations", ANIME)):
        for n, (name, (year, genres, synopsis, minutes, seasons)) in enumerate(shows.items()):
            for season in range(1, len(seasons) + 1):
                for ep in range(1, seasons[season - 1] + 1):
                    fx.write_mkv(root / folder / name / f"Saison {season}" / f"{ep:02d} - Épisode {ep}.mkv", fx.episode_blocks(name, season, ep, f"Épisode {ep}", f"Résumé de l'épisode {ep} de la saison {season}.", f"{year + season}-0{ep % 9 + 1}-01"), cover=poster(f"S{season} E{ep}", 200 + ep * 9, (640, 360)) if ep < 3 else None, minutes=minutes // 3)
            (root / folder / name / "folder.jpg").write_bytes(poster(name, 80 + n * 70 + (150 if kind == "animations" else 0)))
    for n, (artist, album, year, genre, tracks) in enumerate(ALBUMS):
        folder = root / "Musique" / artist / f"{year} - {album}"
        for i, title in enumerate(tracks, 1):
            fx.write_flac(folder / f"{i:02d} - {title}.flac", title, album, artist, i, len(tracks), year=str(year), genre=genre, mbid=f"{n:08d}-2222-3333-4444-555555555555")
        (folder / "cover.jpg").write_bytes(poster(album, n * 55, square=True))
    for section, name, count, author in BOOKS:
        for i in range(1, count + 1):
            write_comic(root / "Livres" / section / name / f"Tome {i:02d}.cbz", name, i, 6, Count=str(count + 2), Writer=author, Summary=f"Le tome {i} de {name}.", Genre="Aventure", Year=str(2000 + i))
    fx.write_epub(root / "Livres" / "Romans" / "Dumas" / "Le Comte de Monte-Cristo.epub", "Le Comte de Monte-Cristo", "Alexandre Dumas", chapters=4)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo").resolve()
    build(target)
    print(f"médiathèque de démonstration : {target}\nlance-la avec :  HUB_SRV={target} HUB_DATA={target.parent / 'demo-data'} python -m hub")
