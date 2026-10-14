"""Démarrage automatique à l'ouverture de session (Windows).

Le réglage « Lancer au démarrage de Windows » n'est pas seulement noté dans
``settings.json`` : pour être **effectif**, il est reflété dans le **registre du
compte de session** :

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
        RadCaseManager = "C:\\…\\RadCaseManager.exe" --minimized

C'est la clé ``Run`` du compte courant (``HKCU``) — celle que Windows liste dans
« Applications de démarrage ». Aucun privilège administrateur n'est requis, et
chaque compte machine garde ses propres réglages : c'est la même granularité que
``settings.json``, lui aussi par utilisateur (voir ``config.py``).

Deux précautions, propres à une application **portable** :

- l'entrée est **réécrite à chaque démarrage** avec le chemin courant de
  l'exécutable (:func:`sync`) : si le dossier a été déplacé, une entrée obsolète
  pointerait dans le vide ;
- l'argument ``--minimized`` inscrit dans la clé fait démarrer l'application
  **réduite dans la zone de notification** : elle est prête à l'emploi sans
  imposer sa fenêtre à chaque ouverture de session (voir ``main_gui.main``).

Limite connue : si l'utilisateur désactive l'élément depuis le Gestionnaire des
tâches, Windows conserve la clé ``Run`` mais pose un drapeau dans
``…\\Explorer\\StartupApproved\\Run`` — le réglage reste alors coché sans que
l'application démarre. Cohérent avec le reste : l'application ne force jamais ce
que l'utilisateur a explicitement désactivé.

Aucun accès au registre hors de Windows, ni pour un binaire de développement
(non « frozen ») : dans ces cas, tout est inerte et :func:`is_supported` retourne
``False``. Le module n'importe pas Qt et reste utilisable en CLI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import config

try:  # Windows uniquement : le module est absent des autres plateformes.
    import winreg
except ImportError:  # pragma: no cover - macOS / Linux
    winreg = None  # type: ignore[assignment]

#: Nom de la valeur dans la clé « Run », et donc de la ligne visible dans le
#: gestionnaire des tâches / les Paramètres Windows.
VALUE_NAME = config.APP_NAME

#: Clé « Run » du compte courant (chemin relatif à ``HKEY_CURRENT_USER``).
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: Argument ajouté à la ligne de commande inscrite au démarrage : l'application
#: se lance alors **réduite** dans la zone de notification.
START_MINIMIZED_FLAG = "--minimized"


def is_supported() -> bool:
    """Vrai si le démarrage automatique peut être piloté sur ce poste.

    Windows uniquement, et **binaire empaqueté** seulement : en développement,
    ``sys.executable`` est l'interpréteur Python — inscrire son chemin au
    démarrage n'aurait aucun sens.
    """
    return sys.platform.startswith("win") and bool(getattr(sys, "frozen", False))


def _exe_command() -> str:
    """Ligne de commande à inscrire (chemin entre guillemets + ``--minimized``).

    Les guillemets sont indispensables : le dossier peut contenir des espaces
    (l'exécutable vit souvent sur le Bureau, ou dans un dossier au nom composé).
    """
    return f'"{Path(sys.executable).resolve()}" {START_MINIMIZED_FLAG}'


def _registered_command() -> str:
    """Ligne actuellement inscrite, ou ``""`` si l'entrée est absente/illisible."""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except (FileNotFoundError, OSError):
        return ""
    return value if isinstance(value, str) else ""


def is_enabled() -> bool:
    """Vrai si l'application est inscrite au démarrage de la session."""
    return bool(_registered_command().strip())


def enable() -> bool:
    """Inscrit l'application au démarrage. Vrai si l'écriture a réussi."""
    if not is_supported():
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _exe_command())
    except OSError:
        # Registre verrouillé par une stratégie d'entreprise : on n'échoue pas
        # bruyamment, le réglage reste simplement sans effet.
        return False
    return True


def disable() -> bool:
    """Retire l'application du démarrage. Vrai si l'entrée est bien absente."""
    if not is_supported():
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True  # rien à retirer : déjà désactivé
    except OSError:
        return False
    return True


def sync(enabled: bool) -> bool:
    """Aligne le registre sur le réglage ``enabled``.

    Appelé à l'enregistrement des réglages (l'utilisateur vient de cocher ou
    décocher) **et** à chaque démarrage : dans ce second cas, il répare une
    entrée périmée si l'utilisateur a déplacé l'exécutable. L'écriture est
    évitée quand rien n'a changé.

    Retourne vrai si, au sortir, l'état souhaité est bien celui du registre.
    """
    if not is_supported():
        return False
    if enabled:
        return True if _registered_command() == _exe_command() else enable()
    return disable()


if __name__ == "__main__":  # pragma: no cover - diagnostic en ligne de commande
    print(f"Démarrage automatique pris en charge : {is_supported()}")
    print(f"Actuellement activé : {is_enabled()}")
    print(f"Ligne inscrite : {_registered_command() or '—'}")
