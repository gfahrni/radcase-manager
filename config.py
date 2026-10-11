"""Configuration locale de RadCase Manager (V3).

Les réglages propres à la machine (nom d'utilisateur, chemin du dossier partagé)
sont stockés dans ``settings_data/settings.json``, à côté de l'application ou du
binaire — jamais sur le dossier partagé.

Ce module ne dépend d'aucune bibliothèque graphique : il est utilisable en CLI.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- Emplacements locaux ----------------------------------------------------


def _app_dir() -> Path:
    """Répertoire de l'application (gère le cas PyInstaller « frozen »)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
SETTINGS_DIR = APP_DIR / "settings_data"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
INDEX_DB = SETTINGS_DIR / "index.db"

DEFAULT_SETTINGS = {
    "user_name": "",
    "shared_root": "",
    "last_units": [],
    "last_browser_unit": "",
    "feed_units": None,
    "feed_last_seen": "",
}


@dataclass
class Settings:
    """Réglages locaux persistés dans ``settings.json``."""

    user_name: str = ""
    shared_root: str = ""
    last_units: list = field(default_factory=list)
    #: Dernière unité sélectionnée dans l'onglet « Bibliothèque » ("" = toutes).
    last_browser_unit: str = ""
    #: Unités suivies dans le « Fil d'actualité » (``None`` = tout coché par défaut).
    feed_units: list | None = None
    #: Horodatage de la dernière visite du fil (soft reset du badge de nouveauté).
    feed_last_seen: str = ""

    @property
    def is_configured(self) -> bool:
        """Vrai si l'utilisateur a renseigné son nom et le dossier partagé."""
        return bool(self.user_name.strip()) and bool(self.shared_root.strip())


def load_settings() -> Settings:
    """Charge les réglages locaux (valeurs par défaut si le fichier est absent)."""
    data: dict = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    known = {k: v for k, v in data.items() if k in DEFAULT_SETTINGS}
    settings = Settings(**{**DEFAULT_SETTINGS, **known})
    if not isinstance(settings.last_units, list):
        settings.last_units = []
    if not isinstance(settings.feed_last_seen, str):
        settings.feed_last_seen = ""
    # Migration douce : ancien suivi « par unité » (feed_seen) -> filigrane global.
    if not settings.feed_last_seen and isinstance(data.get("feed_seen"), dict):
        values = [v for v in data["feed_seen"].values() if isinstance(v, str) and v]
        if values:
            settings.feed_last_seen = max(values)
    return settings


def save_settings(settings: Settings) -> None:
    """Persiste les réglages locaux."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(asdict(settings), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
