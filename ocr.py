"""Lecture OCR native Windows des images de cas.

Ce module s'appuie sur **Windows.Media.Ocr**, donc sur le moteur OCR intégré à
Windows 10/11 : aucun modèle, aucune dépendance lourde et aucun appel réseau.
Les paquets ``winrt-*`` ne sont importés qu'au premier travail réel, dans un
**thread dédié** (voir ci-dessous), pour ne pas ralentir le démarrage et pour
éviter l'erreur WinRT « Cannot change thread mode after it is set ».

Le thread OCR est unique et vit aussi longtemps que l'application : WinRT
initialise le mode d'appartement du thread au premier import, et cette
initialisation ne peut pas être refaite dans un autre thread. On réutilise donc
toujours le même thread de travail, via une file de tâches.

L'OCR est **best-effort** : :func:`extract_text` ne lève jamais. En cas de pack
de langue manquant, d'image illisible, de délai dépassé ou de toute autre
erreur, il retourne un texte vide et un statut explicite.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import sys
import threading
from pathlib import Path
from typing import Tuple

#: Statuts stockés dans ``case.md`` (champ ``ocr_status``).
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"

#: Délai maximal accordé à un appel OCR (secondes). Au-delà, le cas est
#: enregistré sans OCR : une lecture de texte ne doit jamais bloquer la saisie.
OCR_TIMEOUT_SECONDS = 15.0

#: Dimension maximale envoyée au moteur (garde-fou mémoire). Les captures
#: d'écran habituelles sont très en dessous ; seules les images gigantesques
#: sont réduites avant OCR.
MAX_IMAGE_DIMENSION = 10000

# File de tâches traitée par le thread OCR unique. Les éléments sont des tuples
# ``(rgba, largeur, hauteur, Future)``.
_jobs: "queue.Queue" = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False
_last_error = ""

# Moteur WinRT, créé une seule fois dans le thread OCR.
_engine = None
_engine_checked = False


def is_supported() -> bool:
    """Vrai si la plateforme cible dispose de Windows.Media.Ocr.

    On ne teste que la plateforme : importer ``winrt`` ici, dans le thread
    principal, risquerait de figer le mode d'appartement COM du thread avant
    même le premier enregistrement. L'import réel a lieu dans le thread OCR.
    """
    return sys.platform == "win32"


def last_error() -> str:
    """Dernier message d'erreur OCR (diagnostic, jamais affiché tel quel)."""
    return _last_error


def shutdown() -> None:
    """Demande l'arrêt du thread OCR (au moment de quitter l'application).

    Le thread est un thread démon : même si un appel WinRT traîne, il ne peut
    pas empêcher le processus de se terminer.
    """
    if _worker_started:
        _jobs.put(None)


def extract_text(
    image_path: str | Path,
    timeout: float = OCR_TIMEOUT_SECONDS,
) -> Tuple[str, str]:
    """Lit le texte de ``image_path`` et retourne ``(texte, statut)``.

    - ``STATUS_OK`` : du texte a été reconnu ;
    - ``STATUS_EMPTY`` : l'image ne contient pas de texte lisible ;
    - ``STATUS_UNSUPPORTED`` : plateforme non Windows, paquet ``winrt`` absent
      ou aucun pack de langue OCR installé ;
    - ``STATUS_FAILED`` : erreur de lecture ou erreur WinRT ;
    - ``STATUS_TIMEOUT`` : délai dépassé (le cas est enregistré sans OCR).

    Ne lève jamais : l'appelant peut enregistrer le cas dans tous les cas.
    """
    global _last_error

    if not is_supported():
        return "", STATUS_UNSUPPORTED

    try:
        rgba, width, height = _load_rgba(Path(image_path))
    except Exception as exc:  # noqa: BLE001 - l'OCR ne doit jamais bloquer
        _last_error = f"Image illisible pour l'OCR : {exc}"
        return "", STATUS_FAILED

    _ensure_worker()
    future: concurrent.futures.Future = concurrent.futures.Future()
    # Évite le message « Future exception was never retrieved » si le délai
    # expire avant que le thread OCR n'ait publié son résultat.
    future.add_done_callback(lambda done: done.exception())
    _jobs.put((rgba, width, height, future))

    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        _last_error = "Délai OCR dépassé."
        return "", STATUS_TIMEOUT
    except Exception as exc:  # noqa: BLE001
        _last_error = str(exc)
        return "", STATUS_FAILED


# --------------------------------------------------------------------------- #
# Thread OCR unique
# --------------------------------------------------------------------------- #
def _ensure_worker() -> None:
    """Démarre le thread OCR démon au premier besoin."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(
            target=_worker_loop,
            name="radcase-ocr",
            daemon=True,
        )
        thread.start()
        _worker_started = True


def _worker_loop() -> None:
    """Traite les demandes OCR séquentiellement, dans un seul thread."""
    global _last_error

    while True:
        job = _jobs.get()
        if job is None:
            return
        rgba, width, height, future = job
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(_recognize_sync(rgba, width, height))
            except BaseException as exc:  # noqa: BLE001 - thread pérenne
                _last_error = str(exc)
                future.set_exception(exc)
        _jobs.task_done()


def _create_engine():
    """Crée (une fois) le moteur OCR avec la meilleure langue disponible."""
    global _engine, _engine_checked

    if _engine_checked:
        return _engine
    _engine_checked = True

    from winrt.windows.media.ocr import OcrEngine

    # Langue du profil Windows (français sur un poste configuré en français),
    # puis n'importe quel pack OCR installé (l'anglais suffit pour les IPP).
    _engine = OcrEngine.try_create_from_user_profile_languages()
    if _engine is not None:
        return _engine

    try:
        languages = list(OcrEngine.available_recognizer_languages)
    except Exception:  # noqa: BLE001
        languages = []
    for language in languages:
        try:
            _engine = OcrEngine.try_create_from_language(language)
        except Exception:  # noqa: BLE001
            _engine = None
        if _engine is not None:
            return _engine
    return None


def _recognize_sync(rgba: bytes, width: int, height: int) -> Tuple[str, str]:
    """Exécute la reconnaissance WinRT dans le thread OCR."""
    global _last_error

    try:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter
    except ImportError:
        return "", STATUS_UNSUPPORTED
    except Exception as exc:  # noqa: BLE001
        _last_error = str(exc)
        return "", STATUS_FAILED

    try:
        engine = _create_engine()
        if engine is None:
            _last_error = (
                "Aucun pack de langue OCR Windows disponible. "
                "Installez « Reconnaissance optique de caractères » dans les "
                "paramètres de langue de Windows."
            )
            return "", STATUS_UNSUPPORTED

        writer = DataWriter()
        writer.write_bytes(rgba)
        buffer = writer.detach_buffer()

        bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, width, height)
        bitmap.copy_from_buffer(buffer)

        result = asyncio.run(_recognize_async(engine, bitmap))
        lines = []
        for line in result.lines:
            text = (line.text or "").strip()
            if text:
                lines.append(text)
        text = "\n".join(lines).strip()
        return (text, STATUS_OK) if text else ("", STATUS_EMPTY)
    except Exception as exc:  # noqa: BLE001
        _last_error = str(exc)
        return "", STATUS_FAILED


async def _recognize_async(engine, bitmap):
    """Adapte l'opération WinRT (awaitable) à ``asyncio.run``."""
    return await engine.recognize_async(bitmap)


# --------------------------------------------------------------------------- #
# Préparation de l'image (Qt, dans le thread appelant)
# --------------------------------------------------------------------------- #
def _load_rgba(path: Path) -> Tuple[bytes, int, int]:
    """Décode l'image et retourne ses pixels RGBA, sans canal alpha.

    Le décodage est fait côté Qt (comme l'enregistrement JPEG), pas côté WinRT :
    on évite ainsi les problèmes de chemins UNC / partage réseau et de flux
    WinRT. Les images avec transparence sont composées sur fond blanc, sinon
    les zones transparentes seraient lues comme du noir.
    """
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QImageReader, QPainter

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        largest = max(size.width(), size.height())
        if largest > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / largest
            reader.setScaledSize(
                QSize(
                    max(1, round(size.width() * ratio)),
                    max(1, round(size.height() * ratio)),
                )
            )

    image = reader.read()
    if image.isNull():
        # Repli pour les formats que le lecteur redimensionné ne gère pas.
        image = QImage(str(path))
    if image.isNull():
        raise RuntimeError("format d'image non reconnu")

    if image.hasAlphaChannel():
        canvas = QImage(image.size(), QImage.Format_RGB32)
        canvas.fill(Qt.white)
        painter = QPainter(canvas)
        painter.drawImage(0, 0, image)
        painter.end()
        image = canvas

    image = image.convertToFormat(QImage.Format_RGBA8888)
    width, height = image.width(), image.height()
    bytes_per_line = image.bytesPerLine()

    bits = image.constBits()
    try:
        raw = bits.tobytes()
    except AttributeError:  # certaines versions de PySide6
        raw = bytes(bits)

    expected = width * height * 4
    if bytes_per_line == width * 4:
        raw = raw[:expected]
    else:
        # QImage aligne chaque ligne : on retire le remplissage éventuel.
        raw = b"".join(
            raw[y * bytes_per_line:y * bytes_per_line + width * 4]
            for y in range(height)
        )

    if len(raw) != expected:
        raise RuntimeError("données d'image incomplètes")
    return raw, width, height
