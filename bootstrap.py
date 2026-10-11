"""Initialisation et gestion de la structure du dossier partagé.

Rôle :
- créer ``cases/``, ``units.json`` et ``README.md`` si absents ;
- lire/ajouter des unités dans le vocabulaire contrôlé partagé.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

CASES_DIRNAME = "cases"
UNITS_FILENAME = "units.json"
README_FILENAME = "README.md"

# Unité fourre-tout, toujours affichée en dernier.
DIVERS_NAME = "Divers"

# Types de cas (exclusifs) : le standard est le défaut.
CASE_TYPES: List[Dict[str, str]] = [
    {"name": "Standard", "emoji": "📄"},
    {"name": "Typique", "emoji": "🎓"},
    {"name": "Exceptionnel", "emoji": "🤩"},
]
DEFAULT_CASE_TYPE = "Standard"
CASE_TYPE_EMOJI = {ct["name"]: ct["emoji"] for ct in CASE_TYPES}

DEFAULT_UNITS: List[Dict[str, str]] = [
    {"name": "Cardio-vasculaire", "emoji": "🫀", "description": ""},
    {"name": "Divers", "emoji": "🧩", "description": ""},
    {"name": "Echographie", "emoji": "🌊", "description": ""},
    {"name": "Interventionnel", "emoji": "🎯", "description": ""},
    {"name": "Médecine Nucléaire", "emoji": "☢️", "description": ""},
    {"name": "Neuro", "emoji": "🧠", "description": ""},
    {"name": "Onco-dig", "emoji": "🧬", "description": ""},
    {"name": "Ostéo", "emoji": "🦴", "description": ""},
    {"name": "Pediatrie", "emoji": "🧒", "description": ""},
    {"name": "Thorax", "emoji": "🫁", "description": ""},
    {"name": "Urgences", "emoji": "🚑", "description": ""},
]

_README_CONTENT = """# Dossier partagé RadCase Manager

Ce dossier contient les cas cliniques enregistrés avec RadCase Manager.

## Structure

- `cases/AAAA-MM/<id>/case.md` : les métadonnées et la description d'un cas.
- `cases/AAAA-MM/<id>/image.png` : l'image du cas (le cas échéant).
- `units.json` : le vocabulaire contrôlé des unités (modifiable par tous).

## Règles

- Les dossiers préfixés par `.tmp_` sont des écritures inachevées : ne pas y toucher.
- Ne pas modifier la structure d'un cas une fois créé (les cas sont figés).
"""


def ensure_shared_structure(shared_root: str | Path) -> Path:
    """Crée l'arborescence minimale du dossier partagé et retourne sa racine."""
    root = Path(shared_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / CASES_DIRNAME).mkdir(parents=True, exist_ok=True)

    units_path = root / UNITS_FILENAME
    if not units_path.exists():
        units_path.write_text(
            json.dumps(
                {"units": _sorted_units([_normalize_unit(u) for u in DEFAULT_UNITS])},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    readme_path = root / README_FILENAME
    if not readme_path.exists():
        readme_path.write_text(_README_CONTENT, encoding="utf-8")

    return root


def _sorted_units(units: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Trie par ordre alphabétique, mais place « Divers » en dernier."""
    return sorted(
        units,
        key=lambda u: (u["name"].casefold() == DIVERS_NAME.casefold(), u["name"].casefold()),
    )


def _normalize_unit(unit: Dict[str, Any]) -> Dict[str, str]:
    return {
        "name": unit["name"],
        "emoji": unit.get("emoji", ""),
        "description": unit.get("description", ""),
    }


def load_units(shared_root: str | Path) -> List[Dict[str, str]]:
    """Charge les unités depuis ``units.json`` (« Divers » en dernier)."""
    units_path = Path(shared_root) / UNITS_FILENAME
    if not units_path.exists():
        return _sorted_units([_normalize_unit(u) for u in DEFAULT_UNITS])
    try:
        data: Any = json.loads(units_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _sorted_units([_normalize_unit(u) for u in DEFAULT_UNITS])
    units = data.get("units", []) if isinstance(data, dict) else []
    valid = [u for u in units if isinstance(u, dict) and u.get("name")]
    return _sorted_units([_normalize_unit(u) for u in valid])


def unit_names(shared_root: str | Path) -> List[str]:
    """Retourne simplement les noms des unités disponibles (triés)."""
    return [u["name"] for u in load_units(shared_root)]


def add_unit(shared_root: str | Path, name: str, description: str = "", emoji: str = "") -> bool:
    """Ajoute une unité au vocabulaire partagé. False si elle existe déjà."""
    name = (name or "").strip()
    if not name:
        return False
    units = load_units(shared_root)
    if any(u["name"].casefold() == name.casefold() for u in units):
        return False
    units.append({"name": name, "emoji": emoji, "description": description})
    units = _sorted_units(units)
    units_path = Path(shared_root) / UNITS_FILENAME
    units_path.write_text(
        json.dumps({"units": units}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return True
