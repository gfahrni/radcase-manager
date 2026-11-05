"""Réactions du Fil d'actualité, anonymes et hors des dossiers de cas.

Chaque utilisateur peut poser **un seul emoji** sur un cas. Les réactions sont
stockées à la racine du dossier partagé, dans ``reactions/``, un fichier JSON
par utilisateur ::

    { "version": 1, "user": "gfahrni", "reactions": { "<id de cas>": "🔥" } }

Ce choix découle de deux contraintes du projet :

- les **cas sont figés** (``case.md`` n'est jamais réécrit) : une réaction ne
  doit donc pas toucher au dossier du cas ;
- plusieurs utilisateurs écrivent **en même temps** sur le partage : chaque
  utilisateur n'écrit que son propre fichier, si bien qu'aucune réaction n'en
  écrase une autre.

Cliquer le même emoji retire la réaction ; cliquer un autre emoji la remplace.
L'écriture est **atomique** (fichier temporaire puis ``os.replace``), avec
quelques réessais : sous Windows, un lecteur qui a le fichier ouvert au même
instant peut faire échouer le remplacement.

Le module n'importe pas Qt : il est testable en ligne de commande.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Tuple

REACTIONS_DIRNAME = "reactions"
FILE_VERSION = 1

#: Emojis proposés, dans l'ordre d'affichage. Tous sont des codets uniques
#: (aucune séquence ZWJ), donc rendus de façon fiable par Segoe UI Emoji.
EMOJIS = ("👍", "👀", "⭐", "😨", "😅", "😮", "🔥")

#: Infobulles des pastilles. Volontairement anonymes : jamais de nom d'auteur.
EMOJI_LABELS = {
    "👍": "J'aime",
    "👀": "Vu",
    "⭐": "Intéressant",
    "😨": "Inquiétant",
    "😅": "Stressant",
    "😮": "Surprenant",
    "🔥": "Excellent",
}

#: Caractères utilisables dans un nom de fichier (Windows compris).
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_SLUG = 40
#: Réessais du remplacement atomique (un lecteur concurrent peut bloquer).
_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.05


def reactions_dir(shared_root: str | Path) -> Path:
    """Dossier des réactions à la racine du dossier partagé."""
    return Path(shared_root) / REACTIONS_DIRNAME


def _user_file(shared_root: str | Path, user: str) -> Path:
    """Fichier JSON d'un utilisateur (nom lisible + empreinte anti-collision).

    Le nom de session Windows peut contenir des espaces, des accents ou des
    caractères interdits par Windows (``\\ / : * ? " < > |``) : le nom de fichier
    est donc assaini, et une empreinte courte garantit que deux noms différents
    ne se retrouvent pas dans le même fichier. Le nom complet est conservé dans
    le JSON.
    """
    clean = " ".join((user or "").split()) or "inconnu"
    digest = hashlib.sha1(clean.casefold().encode("utf-8")).hexdigest()[:8]
    slug = _UNSAFE_FILENAME_RE.sub("_", clean.casefold()).strip("_")[:_MAX_SLUG] or "user"
    return reactions_dir(shared_root) / f"{slug}_{digest}.json"


def _read_file(path: Path) -> dict:
    """Contenu JSON d'un fichier de réactions, ou ``{}`` si illisible/corrompu."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _reactions_of(data: dict) -> Dict[str, str]:
    """Réactions valides d'un fichier (emoji connu, identifiant non vide)."""
    reactions = data.get("reactions")
    if not isinstance(reactions, dict):
        return {}
    return {
        str(case_id): emoji
        for case_id, emoji in reactions.items()
        if case_id and emoji in EMOJIS
    }


def load_all(
    shared_root: str | Path, user: str = ""
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, str]]:
    """Compte les réactions de tous les utilisateurs.

    Retourne ``(comptes, miennes)`` :

    - ``comptes`` : ``{id de cas: {emoji: nombre}}``, les cas sans réaction
      n'y figurent pas ;
    - ``miennes`` : ``{id de cas: emoji}`` pour ``user`` (met en évidence sa
      propre réaction) ; vide si ``user`` est vide.

    Un fichier absent, illisible ou corrompu est simplement ignoré : une
    réaction perdue ne doit jamais empêcher le Fil de s'afficher.
    """
    counts: Dict[str, Dict[str, int]] = {}
    mine: Dict[str, str] = {}
    directory = reactions_dir(shared_root)
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return {}, {}

    own = _user_file(shared_root, user) if user else None
    for path in paths:
        if path.name.startswith("."):
            continue  # fichier temporaire d'une écriture en cours
        reactions = _reactions_of(_read_file(path))
        if not reactions:
            continue
        is_own = own is not None and path == own
        for case_id, emoji in reactions.items():
            per_case = counts.setdefault(case_id, {})
            per_case[emoji] = per_case.get(emoji, 0) + 1
            if is_own:
                mine[case_id] = emoji
    return counts, mine


def toggle(
    shared_root: str | Path, user: str, case_id: str, emoji: str
) -> str:
    """Ajoute, remplace ou retire la réaction de ``user`` sur ``case_id``.

    Retourne l'emoji désormais posé, ou ``""`` si la réaction a été retirée
    (l'utilisateur a cliqué une seconde fois sur le même emoji).
    """
    if emoji not in EMOJIS:
        raise ValueError(f"Emoji de réaction inconnu : {emoji!r}")
    case_id = str(case_id or "").strip()
    if not case_id:
        raise ValueError("Identifiant de cas manquant.")

    path = _user_file(shared_root, user)
    reactions = _reactions_of(_read_file(path))
    current = reactions.get(case_id)
    new_emoji = "" if current == emoji else emoji
    if new_emoji:
        reactions[case_id] = new_emoji
    else:
        reactions.pop(case_id, None)

    _write_atomic(
        path,
        {
            "version": FILE_VERSION,
            "user": (user or "").strip(),
            "reactions": reactions,
        },
    )
    return new_emoji


def _write_atomic(path: Path, payload: dict) -> None:
    """Écrit ``payload`` dans ``path`` sans jamais laisser de fichier partiel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".tmp_{path.stem}_{uuid.uuid4().hex[:8]}.json")
    try:
        temp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        for attempt in range(_WRITE_RETRIES):
            try:
                os.replace(temp, path)
                return
            except OSError:
                if attempt + 1 >= _WRITE_RETRIES:
                    raise
                time.sleep(_WRITE_RETRY_DELAY)
    finally:
        temp.unlink(missing_ok=True)
