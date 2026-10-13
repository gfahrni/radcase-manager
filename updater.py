"""Mise à jour automatique depuis les releases GitHub (V3).

Principe (détails et justification dans ``AUTO_UPDATE_PLAN.md``) :

1. l'application vérifie **en tâche de fond** la dernière release publiée, au plus
   une fois toutes les 24 h ;
2. si une version plus récente existe, l'utilisateur peut la télécharger ;
3. le nouvel exécutable est téléchargé dans un **dossier de transit**
   (``%LOCALAPPDATA%\\RadCaseManager\\updates\\``, hors du Bureau), puis prend la
   place de l'ancien par **déplacement** : Windows interdit d'écraser un ``.exe``
   en cours d'exécution, mais autorise à le renommer. L'ancien reste dans le
   transit comme **sauvegarde de retour arrière**, de sorte que le Bureau ne
   montre jamais plus d'une icône de l'application.

Aucune dépendance externe (bibliothèque standard uniquement), et **aucun script
externe** n'est lancé : tout se fait dans le processus déjà autorisé à s'exécuter.
Le nom de l'asset est stable (``RadCaseManager.exe``), ce qui rend l'URL de
téléchargement constructible à partir du tag.

Utilisable en ligne de commande (sans GUI) pour tester ::

    python updater.py check          # version locale vs dernière release
    python updater.py download [tag] # télécharge l'exécutable dans le dossier courant
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from send2trash import send2trash

import config

#: Dépôt GitHub qui publie les releases.
REPO = "gfahrni/radcase-manager"

#: Nom de l'asset attaché à chaque release (stable).
ASSET_NAME = "RadCaseManager.exe"

#: Page « dernière release » : sa redirection HTTP donne le tag **sans** subir le
#: quota de l'API GitHub (60 requêtes/heure/IP en non authentifié).
LATEST_PAGE = f"https://github.com/{REPO}/releases/latest"

#: Repli : API GitHub (JSON), soumise au quota.
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"

#: Gabarit de l'URL de l'asset d'une release.
ASSET_URL = f"https://github.com/{REPO}/releases/download/{{tag}}/{ASSET_NAME}"

#: En-tête envoyé aux serveurs GitHub.
USER_AGENT = "RadCaseManager-Updater"

#: Délai réseau (secondes).
TIMEOUT = 20

#: Intervalle minimal entre deux vérifications automatiques (heures).
CHECK_INTERVAL_HOURS = 24

#: Report d'une proposition de mise à jour refusée (« Plus tard »), en jours.
SNOOZE_DAYS = 30

#: Taille plancher d'un exécutable plausible : garde-fou contre un téléchargement
#: tronqué ou une page d'erreur enregistrée par erreur (l'exécutable fait ~50 Mo).
MIN_EXE_SIZE = 5 * 1024 * 1024


class UpdateError(RuntimeError):
    """Erreur de mise à jour (réseau, intégrité, verrou de fichier…)."""


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #
def app_version() -> str:
    """Version de l'application en cours d'exécution (ex. ``v3.13``)."""
    return str(getattr(config, "APP_VERSION", "0.0.0.dev"))


def display_version() -> str:
    """Version prête à afficher, avec **un seul** ``v`` initial (ex. ``v4.3``).

    La CI inscrit le tag tel quel (``v4.3``), mais une version de développement
    n'a pas de préfixe : on normalise ici pour ne pas afficher « vv4.3 ».
    """
    value = app_version().strip()
    return value if value[:1] in ("v", "V") else f"v{value}"


def is_dev_build() -> bool:
    """Vrai pour un binaire de développement (version contenant ``dev``)."""
    return "dev" in app_version().lower()


def is_enabled() -> bool:
    """La mise à jour est-elle exploitable ici (binaire figé, version réelle) ?"""
    return bool(getattr(sys, "frozen", False)) and not is_dev_build()


def parse_version(text: str) -> tuple:
    """Convertit un tag en tuple comparable (``v3.13`` → ``(3, 13)``)."""
    return tuple(int(part) for part in re.findall(r"\d+", str(text))) or (0,)


def is_newer(remote: str, local: str) -> bool:
    """Vrai si ``remote`` est strictement plus récent que ``local``."""
    return parse_version(remote) > parse_version(local)


# --------------------------------------------------------------------------- #
# Chemins
# --------------------------------------------------------------------------- #
def exe_path() -> Path:
    """Chemin de l'exécutable courant."""
    return Path(sys.executable).resolve()


def updates_dir() -> Path:
    """Dossier de transit des mises à jour (hors du Bureau, jamais visible)."""
    return config.UPDATES_DIR


def _transit_dir() -> Path | None:
    """Dossier de transit utilisable, sinon ``None`` (repli à côté de l'exe).

    Tout le transit vit ici — la nouvelle version en cours de téléchargement comme
    la sauvegarde de l'ancienne — pour que le Bureau ne montre **jamais** plus
    d'une icône de l'application.
    """
    try:
        directory = updates_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except OSError:
        return None


def _in_transit(filename: str) -> Path:
    """Chemin d'un fichier de mise à jour : transit si possible, sinon voisin de l'exe."""
    directory = _transit_dir()
    return (directory if directory is not None else exe_path().parent) / filename


def staged_path() -> Path:
    """Nouvelle version en attente d'application (dans le dossier de transit)."""
    exe = exe_path()
    return _in_transit(f"{exe.stem}.new{exe.suffix}")


def backup_path() -> Path:
    """Sauvegarde de l'exécutable remplacé (retour arrière possible)."""
    exe = exe_path()
    return _in_transit(f"{exe.stem}.old{exe.suffix}")


def _staged_candidates() -> list:
    """Emplacements possibles de la nouvelle version (transit, puis voisin de l'exe)."""
    exe = exe_path()
    paths = [staged_path()]
    sibling = exe.with_name(f"{exe.stem}.new{exe.suffix}")
    if sibling not in paths:
        paths.append(sibling)
    return paths


def _backup_candidates() -> list:
    """Emplacements possibles de la sauvegarde (transit, puis voisin de l'exe)."""
    exe = exe_path()
    paths = [backup_path()]
    sibling = exe.with_name(f"{exe.stem}.old{exe.suffix}")
    if sibling not in paths:
        paths.append(sibling)
    return paths


def _silent_unlink(path: Path) -> None:
    """Supprime un fichier sans jamais échouer (absent, verrou…)."""
    try:
        path.unlink()
    except OSError:
        pass


def _discard(path: Path) -> None:
    """Écarte un fichier visible : **corbeille** si possible, suppression sinon.

    La corbeille est préférée pour un fichier que l'utilisateur peut voir : rien
    n'est perdu, et un fichier encore verrouillé échoue proprement au lieu d'être
    effacé à moitié.
    """
    try:
        send2trash(str(path))
    except OSError:
        _silent_unlink(path)


def leftover_files() -> list:
    """Fichiers laissés par une mise à jour dans le dossier de l'exécutable.

    On reconnaît nos fichiers au préfixe ``RadCaseManager`` et au marqueur
    ``.new`` / ``.old``, **y compris si le fichier a été renommé** par le
    navigateur (``RadCaseManager (1).old.exe``). L'exécutable courant et la
    sauvegarde officielle (dossier de transit) sont exclus.
    """
    exe = exe_path()
    canonical = backup_path()
    marker = config.APP_NAME.lower()
    found = []
    for path in exe.parent.iterdir():
        if path == exe or path == canonical or not path.is_file():
            continue
        name = path.name.lower()
        if not name.startswith(marker):
            continue
        if ".new." in name or ".old." in name or name.endswith((".new", ".old")):
            found.append(path)
    return found


def _move_into_place(source: Path, target: Path) -> None:
    """Déplace ``source`` sur ``target`` (qui ne doit pas être en cours d'exécution).

    ``os.replace`` est atomique sur un même volume ; entre deux volumes il faut
    recopier, on passe alors par un fichier temporaire voisin avant la bascule.
    """
    try:
        os.replace(source, target)
        return
    except OSError:
        pass
    temp = target.with_name(f"{target.name}.incoming")
    _silent_unlink(temp)
    shutil.copy2(source, temp)
    os.replace(temp, target)
    _silent_unlink(source)


# --------------------------------------------------------------------------- #
# Détection de la dernière release
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Empêche le suivi d'une redirection (pour lire l'en-tête ``Location``)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def latest_release_tag(timeout: int = TIMEOUT) -> str:
    """Tag de la dernière release publiée (ex. ``v3.13``), ou ``""``.

    On lit d'abord la **redirection** de ``/releases/latest`` : c'est la voie la
    plus légère et la seule à ne pas dépendre du quota de l'API GitHub. En cas
    d'échec, on retombe sur l'API officielle.
    """
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(
            LATEST_PAGE, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            location = (exc.headers.get("Location") if exc.headers else "") or ""
            match = re.search(r"/releases/tag/([^/?#]+)", location)
            if match:
                return match.group(1)
    except Exception:  # noqa: BLE001 - on bascule sur l'API
        pass

    request = urllib.request.Request(
        LATEST_API,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("draft") or data.get("prerelease"):
        return ""
    return str(data.get("tag_name") or "")


def check(timeout: int = TIMEOUT) -> str:
    """Tag d'une version **plus récente** que la version locale, sinon ``""``."""
    tag = latest_release_tag(timeout=timeout)
    return tag if tag and is_newer(tag, app_version()) else ""


def download_url(tag: str) -> str:
    """URL de l'exécutable attaché à la release ``tag``."""
    return ASSET_URL.format(tag=tag)


# --------------------------------------------------------------------------- #
# Téléchargement
# --------------------------------------------------------------------------- #
def download(tag: str, dest: Path | None = None, progress=None) -> Path:
    """Télécharge l'exécutable de la release ``tag`` vers ``dest``.

    ``progress(reçus, total)`` est appelé au fil de l'eau (``total`` vaut 0 si le
    serveur ne l'annonce pas). Lève ``UpdateError`` si le fichier reçu est
    anormalement petit. Le fichier est écrit sous ``<dest>.part`` puis renommé
    d'un bloc, afin qu'un téléchargement interrompu ne laisse jamais un ``.exe``
    tronqué à la place de l'application.
    """
    dest = Path(dest) if dest else staged_path()
    part = Path(f"{dest}.part")
    request = urllib.request.Request(download_url(tag), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
        if part.stat().st_size < MIN_EXE_SIZE:
            raise UpdateError("Téléchargement invalide ou incomplet.")
    except Exception:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return dest


# --------------------------------------------------------------------------- #
# Application (remplacement de l'exécutable)
# --------------------------------------------------------------------------- #
def _relaunch(exe: Path) -> None:
    """Démarre la nouvelle version, détachée et avec un environnement **propre**.

    Point crucial : l'instance relancée doit **réinitialiser l'environnement
    PyInstaller** (`PYINSTALLER_RESET_ENVIRONMENT`, prévu par PyInstaller pour ce
    cas). Sinon le nouveau processus hérite de `_PYI_APPLICATION_HOME_DIR` et
    `_PYI_PARENT_PROCESS_LEVEL` de l'instance qui se remplace.

    Or PyInstaller décide d'hériter l'environnement en comparant `_PYI_ARCHIVE_FILE`
    au chemin de l'exécutable courant. Comme notre mécanique **conserve le même
    chemin** (on remplace le fichier au même endroit), il croit avoir affaire au
    même programme et n'extrait pas son propre dossier temporaire : la nouvelle
    version tourne alors dans le `_MEI` de l'ancienne. Conséquences observées : les
    images ne s'affichent plus (les plugins Qt du dossier temporaire sont supprimés
    au passage) et l'ancien processus affiche « Failed to remove temporary
    directory » en tentant de nettoyer un dossier encore utilisé.
    """
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in [k for k in env if k.startswith(("_PYI_", "_MEI"))]:
        del env[key]  # ceinture et bretelles (versions de bootloader plus anciennes)
    kwargs: dict = {"cwd": str(exe.parent), "close_fds": True, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(exe)], **kwargs)


def apply_update() -> None:
    """Remplace l'exécutable courant par la version téléchargée, puis relance.

    Windows interdit d'**écraser** un ``.exe`` en cours d'exécution, mais autorise
    à le **déplacer** : on déplace donc l'ancien vers le dossier de transit (il y
    reste comme sauvegarde de retour arrière, invisible sur le Bureau), on met le
    nouveau à sa place, puis on relance l'application. Quelques tentatives
    couvrent un verrou transitoire (antivirus). Si le dossier de transit n'est pas
    utilisable, on retombe sur des fichiers voisins de l'exécutable.
    """
    if not getattr(sys, "frozen", False):
        raise UpdateError("La mise à jour n'est possible que pour l'application installée.")
    exe = exe_path()
    staged = next((path for path in _staged_candidates() if path.exists()), None)
    if staged is None:
        raise UpdateError("Aucune mise à jour téléchargée.")

    last_error: Exception | None = None
    for backup in _backup_candidates():
        _silent_unlink(backup)  # sauvegarde d'une mise à jour précédente
        for _ in range(4):
            try:
                os.rename(exe, backup)
            except OSError as exc:  # verrou (antivirus) ou volume différent
                last_error = exc
                time.sleep(0.5)
                continue
            try:
                _move_into_place(staged, exe)
            except OSError as exc:
                try:
                    os.rename(backup, exe)  # on remet l'ancien en place
                except OSError:
                    pass
                last_error = exc
                time.sleep(0.5)
                continue
            last_error = None
            break
        if last_error is None:
            break

    if last_error is not None:
        raise UpdateError(f"Remplacement impossible : {last_error}")

    try:
        _relaunch(exe)
    except Exception:
        # Relance impossible : on tente de revenir à l'état précédent.
        try:
            _move_into_place(exe, staged)
            os.rename(backup, exe)
        except OSError:
            pass
        raise


def pending_update() -> Path | None:
    """Exécutable déjà téléchargé et en attente d'application, s'il existe."""
    if not is_enabled():
        return None
    for candidate in _staged_candidates():
        try:
            if candidate.exists() and candidate.stat().st_size >= MIN_EXE_SIZE:
                return candidate
        except OSError:
            continue
    return None


def cleanup() -> None:
    """Nettoie les restes de mise à jour (au démarrage, puis en différé).

    - ``.part`` : téléchargement interrompu → **supprimé** ;
    - ``.new`` posé à côté de l'exécutable : reste de l'ancienne mécanique (la
      mise à jour en attente vit désormais dans le dossier de transit) → corbeille ;
    - ``.old`` du dossier de transit : sauvegarde de retour arrière → **conservée**,
      elle n'est remplacée qu'au moment de la mise à jour suivante, ce qui évite
      tout verrou de fichier.

    Les fichiers visibles à côté de l'exécutable partent à la **corbeille**, pour
    que le dossier de l'application ne montre jamais qu'une seule icône. À
    rappeler plus tard (voir ``leftover_files()``) : un antivirus peut tenir le
    fichier au démarrage et faire échouer la première tentative.
    """
    if not getattr(sys, "frozen", False):
        return
    exe = exe_path()
    for leftover in leftover_files():
        _discard(leftover)
    for directory in {path for path in (_transit_dir(), exe.parent) if path is not None}:
        for leftover in directory.glob(f"{exe.stem}*.part"):
            _silent_unlink(leftover)


# --------------------------------------------------------------------------- #
# Déclenchement périodique (anti-rebond 24 h, stocké dans les réglages locaux)
# --------------------------------------------------------------------------- #
def should_auto_check(settings) -> bool:
    """Faut-il lancer la vérification automatique (réglage + délai de 24 h) ?"""
    if not is_enabled() or not getattr(settings, "auto_check_updates", True):
        return False
    last = (getattr(settings, "last_update_check", "") or "").strip()
    if not last:
        return True
    try:
        moment = datetime.fromisoformat(last)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - moment >= timedelta(hours=CHECK_INTERVAL_HOURS)


def mark_checked(settings) -> None:
    """Horodate la vérification dans les réglages locaux (objet passé)."""
    settings.last_update_check = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config.save_settings(settings)


# --------------------------------------------------------------------------- #
# Version disponible : mémorisation, indicateur, report
# --------------------------------------------------------------------------- #
def available_update(settings) -> str:
    """Version mémorisée comme disponible, si elle est **encore** plus récente.

    Une version devenue obsolète (l'application a été mise à jour entre-temps)
    renvoie ``""``, ce qui masque l'indicateur.
    """
    tag = (getattr(settings, "available_update", "") or "").strip()
    return tag if tag and is_newer(tag, app_version()) else ""


def note_available(settings, tag: str) -> None:
    """Mémorise la version disponible (``""`` = plus rien de nouveau).

    Effacer la version efface aussi le report : il n'a plus d'objet.
    """
    settings.available_update = tag or ""
    if not settings.available_update:
        settings.update_snooze_until = ""
        settings.update_snooze_version = ""


def should_prompt(settings, tag: str) -> bool:
    """Faut-il proposer la mise à jour ``tag`` maintenant ?

    Un « Plus tard » reporte la proposition de ``SNOOZE_DAYS`` jours, **sauf** si
    une version encore plus récente apparaît entre-temps (l'information est
    nouvelle, on la remonte tout de suite).
    """
    until = (getattr(settings, "update_snooze_until", "") or "").strip()
    if not until:
        return True
    snoozed = (getattr(settings, "update_snooze_version", "") or "").strip()
    if snoozed and is_newer(tag, snoozed):
        return True
    try:
        moment = datetime.fromisoformat(until)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= moment


def snooze(settings, tag: str) -> None:
    """Reporte la proposition de ``SNOOZE_DAYS`` jours (bouton « Plus tard »)."""
    now = datetime.now(timezone.utc)
    settings.update_snooze_until = (
        now + timedelta(days=SNOOZE_DAYS)
    ).isoformat(timespec="seconds")
    settings.update_snooze_version = tag or ""


# --------------------------------------------------------------------------- #
# Test en ligne de commande
# --------------------------------------------------------------------------- #
def _main(argv: list) -> int:
    action = argv[0] if argv else "check"
    if action == "check":
        print(f"version locale   : {app_version()}")
        print(f"version figée    : {'oui' if getattr(sys, 'frozen', False) else 'non'}")
        tag = latest_release_tag()
        print(f"dernière release : {tag or '(inconnue)'}")
        print(f"mise à jour      : {check() or 'aucune'}")
        return 0
    if action == "download":
        tag = argv[1] if len(argv) > 1 else check()
        if not tag:
            print("aucune version plus récente à télécharger")
            return 1
        dest = Path.cwd() / "RadCaseManager.new.exe"

        def report(received: int, total: int) -> None:
            if total:
                print(f"\r{received * 100 // total} %", end="", flush=True)
            else:
                print(f"\r{received // 1024} Kio", end="", flush=True)

        path = download(tag, dest=dest, progress=report)
        print(f"\ntéléchargé : {path}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
