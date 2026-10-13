"""Cache local de vignettes, dérivées des images de cas (V3).

Objectif : **ne jamais décoder une image plein format pour l'affichage**. Une
vignette est calculée une seule fois à la taille d'affichage, stockée dans le
profil local de l'utilisateur (jamais sur le dossier partagé) puis réutilisée.

Points clés :

- la vignette est produite via :class:`QImageReader` avec ``setScaledSize`` :
  l'image est décodée **directement à sa taille finale** (le greffon JPEG
  redimensionne pendant le décodage), donc ni le plein format ni ses ~4 Mo ne
  transitent par la mémoire ;
- le cache est **reconstructible** : la clé inclut la date de modification et la
  taille du fichier source, toute image modifiée est donc automatiquement
  recalculée ; supprimer ``thumbs/`` est sans risque ;
- le disque local évite de relire les images du dossier partagé (réseau) : la
  vignette est servie depuis ``settings_data/thumbs/``.

Ce module importe Qt à la demande : le backend reste utilisable en CLI.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Tuple

import config

#: Sous-dossier du profil local accueillant les vignettes.
THUMB_DIRNAME = "thumbs"
#: Qualité JPEG des vignettes (affichage seulement : 88 suffit largement).
THUMB_QUALITY = 88
#: Nombre maximum de fichiers conservés (purge des plus anciens au-delà).
THUMB_MAX_FILES = 20000
#: Générations entre deux purges (évite de scanner le dossier à chaque image).
PRUNE_EVERY = 300

_generated_since_prune = 0
_thumb_dir_cache: Optional[Path] = None


def _thumb_dir() -> Path:
    """Dossier des vignettes (créé au besoin), mis en cache en mémoire."""
    global _thumb_dir_cache
    if _thumb_dir_cache is None or not _thumb_dir_cache.exists():
        path = Path(config.SETTINGS_DIR) / THUMB_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        _thumb_dir_cache = path
    return _thumb_dir_cache


def _fitted(src_w: int, src_h: int, box_w: int, box_h: int) -> Tuple[int, int]:
    """Taille cible conservant les proportions (jamais 0, jamais déformée)."""
    if src_w <= 0 or src_h <= 0:
        return max(1, box_w), max(1, box_h)
    ratio = min(box_w / src_w, box_h / src_h)
    return max(1, round(src_w * ratio)), max(1, round(src_h * ratio))


def thumbnail_path(case, box_w: int, box_h: int) -> Optional[Path]:
    """Chemin de la vignette d'un cas pour la boîte ``box_w × box_h``.

    Retourne ``None`` si le cas n'a pas d'image exploitable. Le nom encode un
    condensé de (cas, taille demandée, mtime et taille de la source) : une image
    modifiée donne un nouveau nom, l'ancienne vignette devenant orpheline.
    """
    source = getattr(case, "image_path", None)
    if source is None:
        return None
    source = Path(source)
    try:
        stat = source.stat()
    except OSError:
        return None
    key = f"{case.id}|{box_w}x{box_h}|{int(stat.st_mtime)}|{stat.st_size}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return _thumb_dir() / f"{digest}.jpg"


def _generate(source: Path, out_path: Path, box_w: int, box_h: int) -> bool:
    """Décode ``source`` à la taille cible et écrit la vignette JPEG."""
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QImageReader, QPainter

    reader = QImageReader(str(source))
    reader.setAutoTransform(True)  # respecte l'orientation EXIF des photos
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        fitted = _fitted(size.width(), size.height(), box_w, box_h)
        reader.setScaledSize(QSize(*fitted))
    image = reader.read()
    if image.isNull():
        # Repli : certains formats ne supportent pas le décodage redimensionné.
        image = QImage(str(source))
        if image.isNull():
            return False
        image = image.scaled(box_w, box_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    # JPEG ne gère pas la transparence : composition sur fond noir (le cadre
    # d'affichage est noir ; les zones transparentes se fondent donc bien).
    if image.hasAlphaChannel():
        canvas = QImage(image.size(), QImage.Format_RGB32)
        canvas.fill(Qt.black)
        painter = QPainter(canvas)
        painter.drawImage(0, 0, image)
        painter.end()
        image = canvas

    tmp = out_path.with_suffix(".tmp.jpg")
    if not image.save(str(tmp), "JPEG", THUMB_QUALITY):
        tmp.unlink(missing_ok=True)
        return False
    try:
        tmp.replace(out_path)  # renommage atomique (même dossier)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def _maybe_prune() -> None:
    """Purge les vignettes les plus anciennes si le cache devient trop gros."""
    global _generated_since_prune
    _generated_since_prune += 1
    if _generated_since_prune < PRUNE_EVERY:
        return
    _generated_since_prune = 0
    prune()


def prune(max_files: int = THUMB_MAX_FILES) -> None:
    """Ne conserve que les ``max_files`` vignettes les plus récentes."""
    try:
        entries = [e for e in Path(_thumb_dir()).iterdir() if e.suffix == ".jpg"]
    except OSError:
        return
    if len(entries) <= max_files:
        return
    try:
        entries.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in entries[: len(entries) - max_files]:
        path.unlink(missing_ok=True)


def load_thumbnail(case, box_w: int, box_h: int):
    """Retourne la vignette d'un cas (``QPixmap``), calculée/mise en cache.

    ``QPixmap`` vide si le cas n'a pas d'image ou si elle est illisible.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    out_path = thumbnail_path(case, box_w, box_h)
    if out_path is None:
        return QPixmap()

    if out_path.exists():
        pixmap = QPixmap(str(out_path))
        if not pixmap.isNull():
            return pixmap

    source = Path(case.image_path)
    try:
        generated = _generate(source, out_path, box_w, box_h)
    except Exception:  # noqa: BLE001 (vignette best-effort)
        generated = False
    if generated:
        _maybe_prune()
        pixmap = QPixmap(str(out_path))
        if not pixmap.isNull():
            return pixmap

    # Dernier recours : lecture directe (format exotique, disque en lecture seule…).
    pixmap = QPixmap(str(source))
    if pixmap.isNull():
        return pixmap
    return pixmap.scaled(box_w, box_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
