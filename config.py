"""Configuration locale de RadCase Manager (V3).

Les réglages propres au poste (nom d'utilisateur, chemin du dossier partagé) et
l'index local sont stockés dans le **profil de l'utilisateur système** :

    Windows : %LOCALAPPDATA%\\RadCaseManager\\settings_data\\settings.json
              %LOCALAPPDATA%\\RadCaseManager\\settings_data\\index.db
    macOS   : ~/Library/Application Support/RadCaseManager/settings_data/…
    Linux   : $XDG_DATA_HOME/RadCaseManager/settings_data/…

Chaque compte machine dispose donc de ses **propres** réglages, même si le même
``.exe`` est partagé par plusieurs personnes sur un poste. Rien n'est jamais
écrit sur le dossier partagé.

Le nom de l'auteur proposé par défaut est celui de la **session en cours**
(:func:`session_user_name`), modifiable dans les réglages.

Ce module ne dépend d'aucune bibliothèque graphique : il est utilisable en CLI.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: Nom du dossier applicatif dans le profil utilisateur.
APP_NAME = "RadCaseManager"

# --- Emplacements locaux ----------------------------------------------------


def _app_dir() -> Path:
    """Répertoire de l'application (gère le cas PyInstaller « frozen »)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _user_data_dir() -> Path:
    """Répertoire de données **par utilisateur** du système d'exploitation.

    - Windows : ``%LOCALAPPDATA%\\RadCaseManager`` (profil du compte connecté) ;
    - macOS : ``~/Library/Application Support/RadCaseManager`` ;
    - autres : ``$XDG_DATA_HOME/RadCaseManager`` (ou ``~/.local/share``).

    La variable d'environnement ``RADCASE_SETTINGS_DIR`` force l'emplacement :
    pratique pour tester en CLI sans toucher au profil réel.
    """
    override = os.environ.get("RADCASE_SETTINGS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME", "").strip()
        root = Path(base) if base else Path.home() / ".local" / "share"
        return root / APP_NAME
    # Repli (rare) : aucun chemin système exploitable.
    return Path.home() / f".{APP_NAME}"


APP_DIR = _app_dir()
SETTINGS_DIR = _user_data_dir() / "settings_data"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
INDEX_DB = SETTINGS_DIR / "index.db"

# --- Identité de la session -------------------------------------------------

#: ``EXTENDED_NAME_FORMAT.NameDisplay`` (nom lisible du compte Windows).
_WINDOWS_NAME_DISPLAY = 3


def _windows_display_name() -> str:
    """Nom d'affichage du compte Windows courant (``GetUserNameExW``).

    Retourne ``""`` si l'appel échoue (hors Windows, compte sans nom d'affichage,
    contexte restreint…).
    """
    try:
        import ctypes

        getter = ctypes.windll.secur32.GetUserNameExW  # type: ignore[attr-defined]
        getter.argtypes = [
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        getter.restype = ctypes.c_ubyte
        size = ctypes.c_ulong(0)
        getter(_WINDOWS_NAME_DISPLAY, None, ctypes.byref(size))
        if size.value <= 1:
            return ""
        buffer = ctypes.create_unicode_buffer(size.value)
        if getter(_WINDOWS_NAME_DISPLAY, buffer, ctypes.byref(size)):
            return buffer.value.strip()
    except Exception:
        return ""
    return ""


def session_user_name() -> str:
    """Nom de l'utilisateur de la session en cours (``""`` si indéterminable).

    Sous Windows, on privilégie le **nom d'affichage** du compte (ex. « Dr
    Martin ») ; à défaut, le nom de connexion (``USERNAME``) puis
    ``getpass.getuser()``.
    """
    if sys.platform.startswith("win"):
        name = _windows_display_name()
        if name:
            return name
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    try:
        return getpass.getuser().strip()
    except Exception:
        return ""


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
    """Réglages locaux persistés dans ``settings.json`` (profil de l'utilisateur)."""

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
    # Nom d'auteur : à défaut de valeur enregistrée, on prend celui de la session.
    if not settings.user_name.strip():
        settings.user_name = session_user_name()
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
