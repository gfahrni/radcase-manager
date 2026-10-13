"""Cœur de données de RadCase Manager (V3).

Source de vérité : un dossier par cas dans le dossier partagé ::

    cases/<AAAA-MM>/<id>/case.md
    cases/<AAAA-MM>/<id>/image.jpg

L'image collée est ré-encodée en **JPEG progressif** (:data:`IMAGE_QUALITY`) :
le poids baisse de plusieurs fois, sans différence visible (encodage assuré par
Qt, donc aucune dépendance ajoutée).
Les cas historiques peuvent contenir ``image.png`` : le nom réel du fichier est
toujours relu dans le frontmatter (``image:``), jamais deviné.

Un cas est écrit dans un dossier temporaire ``.tmp_<id>`` puis renommé vers son
nom final. Le renommage étant atomique sur un même volume, un cas n'existe que
lorsqu'il est complet : jamais de cas à moitié écrit.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml
from send2trash import send2trash

import config

CASES_DIRNAME = "cases"
CASE_FILENAME = "case.md"
#: Nom du fichier image des nouveaux cas (JPEG, cf. :data:`IMAGE_QUALITY`).
IMAGE_FILENAME = "image.jpg"
#: Nom historique (PNG) : encore présent dans les cas antérieurs à la
#: compression JPEG, et utilisé pour les données ``bytes`` déjà encodées.
LEGACY_IMAGE_FILENAME = "image.png"
TMP_PREFIX = ".tmp_"

#: Qualité JPEG (0-100) appliquée à l'enregistrement des images.
#: 98 = texte, cotes et flèches nets (indispensables pour lire une mesure sur
#: un screenshot) tout en restant ~2 fois plus léger qu'un PNG. Descendre à 92
#: pour privilégier le poids au détriment de la lisibilité des petits textes,
#: monter à 100 (quasi sans perte, ~1,3 fois plus lourd) si nécessaire.
IMAGE_QUALITY = 98
#: Encodage **progressif** : mêmes pixels à qualité égale, ~20 % de poids en
#: moins (mesuré ; la lecture est universellement prise en charge aujourd'hui).
IMAGE_PROGRESSIVE = True

# Sentinelle : « conserver l'image existante » lors d'une édition.
KEEP_IMAGE = object()

# --- Visibilité -------------------------------------------------------------
# Un cas « public » est visible par tous ; un cas « privé » n'est visible que
# de son auteur (caché de la recherche pour les autres utilisateurs).
VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_VALUES = (VISIBILITY_PUBLIC, VISIBILITY_PRIVATE)

# Ancienne « unité » personnelle (avant la visibilité) : conservée uniquement
# pour la nettoyer à la lecture des cas historiques.
LEGACY_PERSONAL_UNIT = "Collection perso"


def _normalize_visibility(value: Any) -> str:
    """Ramène toute valeur à ``public`` ou ``private`` (défaut : ``public``)."""
    token = str(value or "").strip().casefold()
    return VISIBILITY_PRIVATE if token in ("private", "privé", "prive") else VISIBILITY_PUBLIC


def _visible_to(case: "Case", viewer: str) -> bool:
    """Vrai si ``viewer`` peut voir ce cas (auteur des cas privés, ou tous)."""
    if case.visibility != VISIBILITY_PRIVATE:
        return True
    viewer_l = (viewer or "").strip().casefold()
    return bool(viewer_l) and case.author.strip().casefold() == viewer_l


# --- Modèle -----------------------------------------------------------------


@dataclass
class Case:
    """Un cas clinique tel que stocké sur le disque."""

    id: str
    title: str
    date_added: str
    author: str
    units: List[str] = field(default_factory=list)
    image: Optional[str] = None
    description: str = ""
    folder: Optional[Path] = None
    date_modified: Optional[str] = None
    case_type: str = "Standard"
    visibility: str = VISIBILITY_PUBLIC

    @property
    def image_path(self) -> Optional[Path]:
        if self.image and self.folder:
            return self.folder / self.image
        return None

    @property
    def datetime(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.date_added)
        except (TypeError, ValueError):
            return None


# --- Identifiants / chemins -------------------------------------------------


def new_id(when: Optional[datetime] = None) -> str:
    """Génère un identifiant unique ``YYYYMMDD_HHMMSS_xxxx``."""
    when = when or datetime.now()
    return f"{when:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"


def _month_dir(shared_root: Path, when: datetime) -> Path:
    return shared_root / CASES_DIRNAME / f"{when:%Y-%m}"


# --- Sérialisation ----------------------------------------------------------


def _render_case_md(meta: Dict[str, Any], description: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    body = (description or "").strip()
    return f"---\n{front}\n---\n\n{body}\n"


def _parse_case_md(path: Path) -> Optional[Case]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None

    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None

    body = "\n".join(lines[end + 1:]).strip()
    # Nettoyage des cas historiques : l'ancienne pseudo-unité personnelle
    # disparaît du vocabulaire (les anciens cas deviennent publics).
    units = [u for u in (meta.get("units") or []) if u and u != LEGACY_PERSONAL_UNIT]
    return Case(
        id=str(meta.get("id", path.parent.name)),
        title=str(meta.get("title", "")),
        date_added=str(meta.get("date_added", "")),
        author=str(meta.get("author", "")),
        units=units,
        image=meta.get("image") or None,
        description=body,
        folder=path.parent,
        date_modified=str(meta["date_modified"]) if meta.get("date_modified") else None,
        case_type=str(meta.get("case_type") or "Standard"),
        visibility=_normalize_visibility(meta.get("visibility")),
    )


#: En-tête permettant de reconnaître des données ``bytes`` déjà encodées en JPEG.
_JPEG_MAGIC = b"\xff\xd8"


def _is_qt_image(image: Any) -> bool:
    """Vrai pour un ``QImage`` (détection par capacités, sans importer Qt)."""
    return hasattr(image, "hasAlphaChannel") and hasattr(image, "save")


def _is_pillow_image(image: Any) -> bool:
    """Vrai pour une image Pillow (détection par capacités, sans importer PIL)."""
    return hasattr(image, "mode") and hasattr(image, "save")


def _jpeg_with_qt(image: Any, path: Path, quality: int) -> None:
    """Encode ``image`` (``QImage``) en JPEG progressif à ``quality``.

    ``QImageWriter`` est utilisé plutôt que ``QImage.save()`` pour activer le
    mode **progressif** (mêmes pixels, ~20 % de poids en moins, vérifié). Qt
    encode déjà en 4:4:4 (chroma pleine résolution, lu sur les marqueurs SOF) :
    les annotations colorées ne sont donc pas dégradées.

    JPEG ne gère pas la transparence : une image avec canal alpha est d'abord
    composée sur fond **blanc** (sinon les zones transparentes virent au noir).
    """
    from PySide6.QtGui import QImageWriter

    if image.hasAlphaChannel():
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter

        canvas = QImage(image.size(), QImage.Format_RGB32)
        canvas.fill(Qt.white)
        painter = QPainter(canvas)
        painter.drawImage(0, 0, image)
        painter.end()
        image = canvas

    writer = QImageWriter(str(path), b"JPEG")
    writer.setQuality(quality)
    writer.setProgressiveScanWrite(IMAGE_PROGRESSIVE)
    if not writer.write(image):
        raise RuntimeError(
            "Échec de l'encodage JPEG de l'image "
            f"(greffon Qt « jpeg » indisponible ? {writer.errorString()})"
        )


def _jpeg_with_pillow(image: Any, path: Path, quality: int) -> None:
    """Encode une image Pillow en JPEG (4:4:4, progressif, Huffman optimisé)."""
    if image.mode not in ("RGB", "L", "CMYK"):
        image = image.convert("RGB")
    image.save(
        str(path),
        format="JPEG",
        quality=quality,
        subsampling=0,   # 4:4:4 : préserve la chroma (Doppler, cartographies)
        optimize=True,   # tables de Huffman optimisées (~5-10 % de gain)
        progressive=True,
    )


def _save_image(image: Any, directory: Path, quality: int = IMAGE_QUALITY) -> str:
    """Écrit l'image dans ``directory`` et retourne le **nom** du fichier créé.

    - ``QImage`` ou image Pillow : ré-encodée en JPEG (qualité ``quality``) →
      ``image.jpg`` ;
    - ``bytes`` : données déjà encodées, écrites telles quelles ; l'extension
      est déduite de l'en-tête (``image.jpg`` pour du JPEG, ``image.png``
      sinon).

    Qt/Pillow ne sont importés qu'au moment de l'encodage : le module reste
    utilisable en ligne de commande, sans environnement graphique.
    """
    if isinstance(image, (bytes, bytearray)):
        data = bytes(image)
        name = IMAGE_FILENAME if data[:2] == _JPEG_MAGIC else LEGACY_IMAGE_FILENAME
        (directory / name).write_bytes(data)
        return name

    if _is_qt_image(image):
        _jpeg_with_qt(image, directory / IMAGE_FILENAME, quality)
        return IMAGE_FILENAME

    if _is_pillow_image(image):
        _jpeg_with_pillow(image, directory / IMAGE_FILENAME, quality)
        return IMAGE_FILENAME

    raise TypeError("Format d'image non supporté (attendu : bytes, QImage ou PIL.Image).")


# --- Écriture ---------------------------------------------------------------


def create_case(
    *,
    title: str,
    units: Iterable[str],
    author: str,
    description: str = "",
    image: Any = None,
    shared_root: str | Path,
    when: Optional[datetime] = None,
    case_type: str = "Standard",
    visibility: str = VISIBILITY_PUBLIC,
) -> Case:
    """Crée un cas de façon atomique et retourne l'objet :class:`Case`.

    ``image`` peut être ``None``, des ``bytes``, un ``PIL.Image`` ou un ``QImage``.
    Les images Qt/Pillow sont ré-encodées en JPEG progressif
    (:data:`IMAGE_QUALITY`) pour diviser fortement le poids du cas tout en
    gardant les textes et cotes lisibles.
    ``visibility`` vaut ``"public"`` (défaut) ou ``"private"`` : un cas privé
    n'apparaît dans la recherche que pour son auteur.
    Lève ``ValueError`` si le titre est vide ou le dossier partagé manquant.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("Le titre du cas est obligatoire.")
    if not shared_root:
        raise ValueError("Le dossier partagé n'est pas configuré.")

    if when is None:
        when = datetime.now()
    shared_path = Path(shared_root)
    month_dir = _month_dir(shared_path, when)
    month_dir.mkdir(parents=True, exist_ok=True)

    case_id = new_id(when)
    tmp_dir = month_dir / f"{TMP_PREFIX}{case_id}"
    final_dir = month_dir / case_id
    # Anti-collision : on régénère tant que l'identifiant est déjà pris.
    while final_dir.exists() or tmp_dir.exists():
        case_id = new_id(when)
        tmp_dir = month_dir / f"{TMP_PREFIX}{case_id}"
        final_dir = month_dir / case_id
    tmp_dir.mkdir(parents=True, exist_ok=False)

    try:
        image_name: Optional[str] = None
        if image is not None:
            image_name = _save_image(image, tmp_dir)

        meta = {
            "id": case_id,
            "title": title,
            "date_added": when.isoformat(timespec="seconds"),
            "author": author or "",
            "units": [u for u in (units or []) if u],
            "image": image_name,
            "case_type": case_type or "Standard",
            "visibility": _normalize_visibility(visibility),
        }
        (tmp_dir / CASE_FILENAME).write_text(
            _render_case_md(meta, description), encoding="utf-8"
        )
        os.rename(tmp_dir, final_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    result = Case(
        id=case_id,
        title=title,
        date_added=meta["date_added"],
        author=meta["author"],
        units=meta["units"],
        image=image_name,
        description=(description or "").strip(),
        folder=final_dir,
        case_type=meta["case_type"],
        visibility=meta["visibility"],
    )
    _index_touch_case(result, shared_path)
    return result


# --- Édition -----------------------------------------------------------------


def update_case(
    *,
    case: "Case",
    title: str,
    units: Iterable[str],
    author: str,
    description: str = "",
    image: Any = KEEP_IMAGE,
    shared_root: str | Path,
    case_type: str = "Standard",
    visibility: Optional[str] = None,
) -> Case:
    """Met à jour un cas existant en réécrivant son dossier « à sa place ».

    - ``id`` et ``date_added`` sont **conservés** ; ``date_modified`` est ajouté.
    - L'ancien dossier est envoyé dans la **corbeille** avant réécriture.
    - ``image`` : ``KEEP_IMAGE`` (défaut) réutilise l'image existante **telle
      quelle** (nom et encodage conservés, y compris un ancien ``image.png``),
      ``None`` la retire, toute autre valeur (bytes/PIL/QImage) la remplace par
      une version JPEG ré-encodée.
    - ``visibility`` : ``None`` (défaut) conserve la visibilité du cas.
    - Écriture atomique par fichier (dossier temporaire puis renommage).
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("Le titre du cas est obligatoire.")
    if not shared_root or case is None or case.folder is None:
        raise ValueError("Cas ou dossier partagé invalide.")

    final_dir = Path(case.folder)
    month_dir = final_dir.parent
    tmp_dir = month_dir / f"{TMP_PREFIX}{case.id}_edit"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=False)

    try:
        # 1) Image : conserver, retirer, ou remplacer.
        image_name: Optional[str] = None
        if image is KEEP_IMAGE:
            if case.image and (final_dir / case.image).exists():
                shutil.copy2(final_dir / case.image, tmp_dir / case.image)
                image_name = case.image
        elif image is not None:
            image_name = _save_image(image, tmp_dir)

        # 2) case.md : on conserve id + date_added, on ajoute date_modified.
        modified = datetime.now().isoformat(timespec="seconds")
        visibility = _normalize_visibility(
            visibility if visibility is not None else case.visibility
        )
        meta = {
            "id": case.id,
            "title": title,
            "date_added": case.date_added,
            "date_modified": modified,
            "author": author or "",
            "units": [u for u in (units or []) if u],
            "image": image_name,
            "case_type": case_type or "Standard",
            "visibility": visibility,
        }
        (tmp_dir / CASE_FILENAME).write_text(
            _render_case_md(meta, description), encoding="utf-8"
        )

        # 3) Ancien dossier -> corbeille, puis nouveau dossier à la même place.
        _trash_folder(final_dir)
        os.rename(tmp_dir, final_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    result = Case(
        id=case.id,
        title=title,
        date_added=case.date_added,
        author=meta["author"],
        units=meta["units"],
        image=image_name,
        description=(description or "").strip(),
        folder=final_dir,
        date_modified=modified,
        case_type=meta["case_type"],
        visibility=meta["visibility"],
    )
    _index_touch_case(result, Path(shared_root))
    return result


# --- Suppression -------------------------------------------------------------


def _trash_folder(folder: Path) -> None:
    """Envoie un dossier à la corbeille système (repli : suppression réelle)."""
    try:
        send2trash(str(folder))
    except Exception:
        # Corbeille indisponible (typiquement un partage réseau) : suppression réelle.
        shutil.rmtree(folder, ignore_errors=True)


def delete_case(case: "Case") -> bool:
    """Supprime définitivement un cas.

    Le dossier est envoyé dans la **corbeille du système** si possible
    (récupérable). Sur un partage réseau (Windows), la corbeille n'existe pas :
    la suppression est alors réellement définitive.
    Retourne ``False`` si le dossier est introuvable.
    """
    if case is None or case.folder is None:
        return False
    folder = Path(case.folder)
    if not folder.exists():
        return False
    _trash_folder(folder)
    _index_remove(case.id)
    return True


# --- Index local (cache reconstructible) -------------------------------------

INDEX_SCHEMA_VERSION = "2"


def _index_path() -> Path:
    """Chemin de la base d'index locale (stockée hors du dossier partagé)."""
    return Path(config.INDEX_DB)


def _index_connect() -> sqlite3.Connection:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cases_index (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            date_added    TEXT NOT NULL,
            date_modified TEXT,
            case_type     TEXT,
            author        TEXT,
            units         TEXT,
            image         TEXT,
            description   TEXT,
            rel_path      TEXT NOT NULL,
            mtime         REAL,
            size          INTEGER,
            visibility    TEXT
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # Migration douce : ajoute les colonnes si l'index a été créé avant leur ajout.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cases_index)")}
    if "date_modified" not in columns:
        conn.execute("ALTER TABLE cases_index ADD COLUMN date_modified TEXT")
    if "case_type" not in columns:
        conn.execute("ALTER TABLE cases_index ADD COLUMN case_type TEXT")
    if "visibility" not in columns:
        conn.execute("ALTER TABLE cases_index ADD COLUMN visibility TEXT")
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _row_to_case(row: tuple, root: Path) -> Case:
    (case_id, title, date_added, date_modified, case_type, author, units, image,
     description, rel_path, _mtime, _size, visibility) = row
    return Case(
        id=case_id,
        title=title or "",
        date_added=date_added or "",
        author=author or "",
        units=json.loads(units) if units else [],
        image=image or None,
        description=description or "",
        folder=root / rel_path,
        date_modified=date_modified or None,
        case_type=case_type or "Standard",
        visibility=_normalize_visibility(visibility),
    )


def _index_upsert(conn: sqlite3.Connection, case: Case, rel_path: str, mtime: float, size: int) -> None:
    conn.execute(
        """INSERT INTO cases_index
             (id, title, date_added, date_modified, case_type, author, units, image, description, rel_path, mtime, size, visibility)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, date_added=excluded.date_added, date_modified=excluded.date_modified,
             case_type=excluded.case_type, author=excluded.author, units=excluded.units,
             image=excluded.image, description=excluded.description, rel_path=excluded.rel_path,
             mtime=excluded.mtime, size=excluded.size, visibility=excluded.visibility""",
        (
            case.id, case.title, case.date_added, case.date_modified, case.case_type, case.author,
            json.dumps(case.units, ensure_ascii=False), case.image, case.description,
            rel_path, mtime, size, case.visibility,
        ),
    )


def _scan_case_files(shared_root: Path) -> Iterable[tuple]:
    """Parcourt ``cases/**/case.md`` via ``os.scandir`` (stat mis en cache).

    On expose le ``DirEntry`` pour réutiliser sa stat (évite un second appel
    système par fichier, coûteux sur un partage réseau).
    """
    base = shared_root / CASES_DIRNAME
    if not base.exists():
        return
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(TMP_PREFIX):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.name == CASE_FILENAME:
                yield Path(entry.path), entry


def refresh_index(shared_root: str | Path) -> int:
    """Met à jour l'index local et retourne le nombre de cas indexés.

    Ne relit et ne re-parse un ``case.md`` que si sa date de modification ou sa
    taille a changé. Purge les cas disparus. Reconstruit tout si le dossier
    partagé a changé ou si l'index est corrompu.
    """
    root = Path(shared_root)
    try:
        conn = _index_connect()
    except sqlite3.Error:
        try:
            _index_path().unlink(missing_ok=True)
        except OSError:
            pass
        conn = _index_connect()

    try:
        if _meta_get(conn, "shared_root") != str(root):
            conn.execute("DELETE FROM cases_index")
            _meta_set(conn, "shared_root", str(root))
        # Changement de schéma : l'index est un cache, on le vide pour forcer
        # une relecture complète des ``case.md`` (nouvelles colonnes/migrations).
        if _meta_get(conn, "schema_version") != INDEX_SCHEMA_VERSION:
            conn.execute("DELETE FROM cases_index")
        _meta_set(conn, "schema_version", INDEX_SCHEMA_VERSION)

        known = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT id, rel_path, mtime, size FROM cases_index")
        }
        seen: set[str] = set()

        for path, entry in _scan_case_files(root):
            rel_path = str(path.parent.relative_to(root))
            case_id = path.parent.name
            seen.add(case_id)
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            cached = known.get(case_id)
            if cached and cached[0] == rel_path and cached[1] == stat.st_mtime and cached[2] == stat.st_size:
                continue  # inchangé : aucun accès disque supplémentaire
            case = _parse_case_md(path)
            if case is None:
                continue
            _index_upsert(conn, case, rel_path, stat.st_mtime, stat.st_size)

        stale = [cid for cid in known if cid not in seen]
        if stale:
            conn.executemany("DELETE FROM cases_index WHERE id = ?", [(cid,) for cid in stale])

        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM cases_index").fetchone()[0]
    finally:
        conn.close()


def _index_touch_case(case: Case, shared_root: Path) -> None:
    """Met à jour l'index pour un cas tout juste créé (best-effort)."""
    try:
        conn = _index_connect()
    except sqlite3.Error:
        return
    try:
        if _meta_get(conn, "shared_root") != str(shared_root):
            return  # index d'un autre partage : sera reconstruit au prochain scan
        case_md = Path(case.folder) / CASE_FILENAME
        stat = case_md.stat()
        rel_path = str(Path(case.folder).relative_to(shared_root))
        _index_upsert(conn, case, rel_path, stat.st_mtime, stat.st_size)
        conn.commit()
    except (OSError, ValueError, sqlite3.Error):
        return
    finally:
        conn.close()


def _index_remove(case_id: str) -> None:
    """Retire un cas de l'index local (best-effort)."""
    if not _index_path().exists():
        return
    try:
        conn = _index_connect()
        conn.execute("DELETE FROM cases_index WHERE id = ?", (case_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


# --- Lecture / recherche -----------------------------------------------------


_SELECT_COLS = "id, title, date_added, date_modified, case_type, author, units, image, description, rel_path, mtime, size, visibility"


def list_cases(shared_root: str | Path, viewer: str = "") -> List[Case]:
    """Retourne tous les cas visibles (via l'index), du plus récent au plus ancien.

    ``viewer`` est le nom de l'utilisateur courant : les cas privés ne sont
    inclus que si ``viewer`` en est l'auteur. Par défaut (``""``), aucun cas
    privé n'est retourné.
    """
    root = Path(shared_root)
    refresh_index(root)
    conn = _index_connect()
    try:
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM cases_index ORDER BY date_added DESC"
        ).fetchall()
    finally:
        conn.close()
    cases = [_row_to_case(row, root) for row in rows]
    return [case for case in cases if _visible_to(case, viewer)]


def search_cases(
    shared_root: str | Path,
    *,
    text: str = "",
    author: str = "",
    units: Optional[Iterable[str]] = None,
    unit_match_all: bool = False,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    viewer: str = "",
    visibility: Optional[str] = None,
    case_types: Optional[Iterable[str]] = None,
) -> List[Case]:
    """Recherche multi-critères, exécutée sur l'index local.

    Les filtres auteur, texte et unités sont appliqués côté Python (via
    ``casefold``, correct pour les accents), la plage de dates via SQL.

    ``viewer`` est le nom de l'utilisateur courant : les cas privés ne sont
    retournés que s'il en est l'auteur (sinon ils restent cachés).

    ``visibility`` restreint à ``"public"`` ou ``"private"`` (``None`` : les deux,
    les cas privés d'autrui restant exclus).
    """
    root = Path(shared_root)
    refresh_index(root)

    wanted = {u.casefold() for u in (units or [])}
    text_l = (text or "").strip().casefold()
    author_l = (author or "").strip().casefold()

    where, params = [], []
    if date_from:
        where.append("date_added >= ?")
        params.append(date_from.isoformat(timespec="seconds"))
    if date_to:
        where.append("date_added <= ?")
        params.append(date_to.isoformat(timespec="seconds"))

    sql = f"SELECT {_SELECT_COLS} FROM cases_index"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date_added DESC"

    conn = _index_connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    results: List[Case] = []
    for row in rows:
        case = _row_to_case(row, root)
        if not _visible_to(case, viewer):
            continue
        if visibility is not None and case.visibility != visibility:
            continue
        if case_types is not None and case.case_type not in case_types:
            continue
        if author_l and author_l not in case.author.casefold():
            continue
        if wanted:
            case_units = {u.casefold() for u in case.units}
            ok = wanted.issubset(case_units) if unit_match_all else bool(wanted & case_units)
            if not ok:
                continue
        if text_l and text_l not in f"{case.title}\n{case.description}".casefold():
            continue
        results.append(case)
    return results


def all_unit_tags(shared_root: str | Path, viewer: str = "") -> List[str]:
    """Liste triée de toutes les unités effectivement utilisées dans les cas."""
    tags = {u for case in list_cases(shared_root, viewer=viewer) for u in case.units}
    return sorted(tags, key=str.casefold)


def all_authors(shared_root: str | Path, viewer: str = "") -> List[str]:
    """Liste triée des auteurs présents dans les cas (pour l'autocomplétion).

    Les cas privés d'autrui sont exclus (``viewer``), pour ne pas révéler leur
    existence via la liste des auteurs.
    """
    authors = {
        case.author.strip()
        for case in list_cases(shared_root, viewer=viewer)
        if case.author and case.author.strip()
    }
    return sorted(authors, key=str.casefold)
