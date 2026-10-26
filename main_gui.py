"""Interface graphique PySide6 de RadCase Manager (V3).

Onglets :
- « Nouveau cas »   : collage du presse-papiers + formulaire d'enregistrement.
- « Fil d'actualité » : flux des derniers cas (contenu à venir).
- « Bibliothèque »  : navigation par unités (cas publics / privés).
- « Recherche Avancée » : recherche multi-critères dans les cas existants.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QFileInfo, QObject, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileIconProvider,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import autostart
import bootstrap
import config
import ocr
import storage
import thumbnails
import tray
import updater

PREVIEW_W, PREVIEW_H = 400, 320

#: Icône de l'onglet « Fil d'actualité » : ⚪ (blanc) = aucun nouveau cas.
#: Devient 🔵 (bleu) quand du nouveau est détecté.
FEED_DOT_EMOJI = "⚪"
FEED_DOT_NOTIFY = "🔵"

#: Intervalle de vérification du badge de nouveauté du fil (millisecondes).
FEED_BADGE_INTERVAL_MS = 5 * 60 * 1000

#: Reprendre le titre du cas dans la bannière système (voir
#: ``MainWindow._notify_new_cases``). Le mettre à ``False`` donne une bannière
#: neutre (« US — un nouveau cas ») : un titre de cas peut contenir un nom de
#: patient, et Windows affiche aussi ses notifications sur l'écran verrouillé.
TRAY_TOAST_WITH_TITLE = True

#: Délai avant la recherche automatique de mise à jour (millisecondes) : on
#: laisse la fenêtre s'afficher et l'interface se stabiliser.
UPDATE_CHECK_DELAY_MS = 4000

#: Délais (ms) des tentatives de nettoyage des restes de mise à jour après le
#: démarrage : la première peut échouer (fichier encore tenu par l'antivirus).
LEFTOVER_CLEANUP_DELAYS_MS = (3000, 15000, 45000, 120000, 300000)

#: Message affiché quand la mise à jour n'est pas applicable (mode développement).
UPDATE_UNAVAILABLE = (
    "La mise à jour automatique concerne l'application installée (le fichier "
    ".exe). En mode développement, elle est désactivée."
)


# --------------------------------------------------------------------------- #
# Mise à jour : tâches de fond
# --------------------------------------------------------------------------- #
class UpdateCheckWorker(QObject):
    """Cherche en tâche de fond une version plus récente de l'application."""

    #: Tag de la version disponible ("" si l'application est à jour).
    finished = Signal(str)
    #: Message d'erreur (réseau, etc.).
    failed = Signal(str)

    def run(self) -> None:
        try:
            tag = updater.check()
        except Exception as exc:  # noqa: BLE001 - toute erreur est non fatale
            self.failed.emit(str(exc))
            return
        self.finished.emit(tag)


class UpdateDownloadWorker(QObject):
    """Télécharge en tâche de fond l'exécutable de la release demandée."""

    #: Octets reçus, octets annoncés (0 si inconnu).
    progress = Signal(int, int)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, tag: str, dest):
        super().__init__()
        self._tag = tag
        self._dest = dest
        self._cancelled = False

    def cancel(self) -> None:
        """Demande l'interruption du téléchargement (appelé depuis l'UI)."""
        self._cancelled = True

    def run(self) -> None:
        def report(received: int, total: int) -> None:
            if self._cancelled:
                raise updater.UpdateError("Téléchargement annulé.")
            self.progress.emit(received, total)

        try:
            updater.download(self._tag, dest=self._dest, progress=report)
        except Exception as exc:  # noqa: BLE001 - remonté à l'interface
            self.failed.emit(str(exc))
            return
        if self._cancelled:  # annulation tardive : le fichier complet est écarté
            self._dest.unlink(missing_ok=True)
            self.failed.emit("Téléchargement annulé.")
            return
        self.finished.emit()


# --------------------------------------------------------------------------- #
# Enregistrement : tâches de fond
# --------------------------------------------------------------------------- #
class SaveCaseWorker(QObject):
    """Crée un cas (image + OCR éventuel) sans bloquer l'interface."""

    #: Cas créé (``storage.Case``).
    finished = Signal(object)
    #: Message d'erreur remonté à l'utilisateur.
    failed = Signal(str)

    def __init__(self, payload: dict):
        super().__init__()
        self._payload = payload

    def run(self) -> None:
        try:
            case = storage.create_case(**self._payload)
        except Exception as exc:  # noqa: BLE001 - remonté à l'interface
            self.failed.emit(str(exc))
            return
        self.finished.emit(case)


class UpdateCaseWorker(QObject):
    """Met à jour un cas existant sans bloquer l'interface."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, payload: dict):
        super().__init__()
        self._payload = payload

    def run(self) -> None:
        try:
            case = storage.update_case(**self._payload)
        except Exception as exc:  # noqa: BLE001 - remonté à l'interface
            self.failed.emit(str(exc))
            return
        self.finished.emit(case)


class ClickableLabel(QLabel):
    """Étiquette cliquable — utilisée par l'indicateur de mise à jour."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        self.clicked.emit()
        super().mousePressEvent(event)


# --------------------------------------------------------------------------- #
# Réglages (/première exécution)
# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    """Choix de l'utilisateur connu et du dossier partagé."""

    def __init__(self, settings: config.Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration de RadCase Manager")
        self.setMinimumWidth(520)

        self._initial = settings
        self._session_name = config.session_user_name().strip()

        form = QFormLayout(self)

        # Nom d'auteur : liste déroulante des **utilisateurs connus** (dossier
        # partagé), pré-sélectionnée sur le compte de la session Windows. Le nom
        # n'est plus saisi librement ; il est ajouté automatiquement s'il est
        # inconnu (voir ``_ensure_user_known``).
        self.name_input = QComboBox()
        self.name_input.setEditable(False)
        form.addRow("Utilisateur :", self.name_input)
        name_hint = QLabel(
            "Compte de la session Windows, limité aux utilisateurs connus du "
            "dossier partagé (ajout automatique si inconnu)."
        )
        name_hint.setStyleSheet("color: gray;")
        name_hint.setWordWrap(True)
        form.addRow("", name_hint)

        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_input = QLineEdit(settings.shared_root)
        self.folder_input.setPlaceholderText("Chemin du dossier partagé des cas…")
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self.browse)
        folder_layout.addWidget(self.folder_input)
        folder_layout.addWidget(browse)
        form.addRow("Dossier partagé :", folder_row)

        # Recherche automatique des mises à jour (vérification silencieuse en
        # tâche de fond au démarrage, au plus une fois par jour).
        self.auto_update_checkbox = QCheckBox("Rechercher les mises à jour au démarrage")
        self.auto_update_checkbox.setChecked(
            bool(getattr(settings, "auto_check_updates", True))
        )
        form.addRow("Mises à jour :", self.auto_update_checkbox)

        # OCR natif Windows : lecture du texte de l'image au moment de
        # l'enregistrement, dans un thread dédié (voir ``ocr.py``). La case
        # n'apparaît que sur une plateforme capable de l'exécuter.
        self.ocr_checkbox = None
        if ocr.is_supported():
            self.ocr_checkbox = QCheckBox(
                "Lire le texte des images (OCR Windows) à l'enregistrement"
            )
            self.ocr_checkbox.setChecked(bool(getattr(settings, "ocr_enabled", True)))
            self.ocr_checkbox.setToolTip(
                "Le texte détecté (IPP, annotations…) est stocké dans le cas et "
                "indexé localement. La lecture se fait en tâche de fond, sans "
                "bloquer l'interface."
            )
            form.addRow("OCR :", self.ocr_checkbox)

        # Fermeture de la fenêtre : réduction dans la zone de notification, ou
        # arrêt. L'arrêt réel se fait toujours par « Quitter » (menu Fichier ou
        # menu de l'icône).
        self.close_to_tray_checkbox = QCheckBox(
            "Réduire dans la zone de notification à la fermeture "
            "(« Quitter » arrête l'application)"
        )
        self.close_to_tray_checkbox.setChecked(
            bool(getattr(settings, "close_to_tray", True))
        )
        form.addRow("Fenêtre :", self.close_to_tray_checkbox)

        # Démarrage automatique à l'ouverture de session Windows : l'application
        # se lance réduite dans la zone de notification, pour que le poste soit
        # prêt après un redémarrage sans que personne ait à y penser. La case
        # n'apparaît que là où le réglage est applicable (Windows, binaire
        # empaqueté) : ailleurs, elle serait trompeuse (voir ``autostart``).
        self.autostart_checkbox = None
        if autostart.is_supported():
            self.autostart_checkbox = QCheckBox(
                "Lancer RadCase Manager à l'ouverture de Windows "
                "(réduit dans la zone de notification)"
            )
            self.autostart_checkbox.setChecked(
                bool(getattr(settings, "start_with_windows", True))
            )
            form.addRow("Démarrage :", self.autostart_checkbox)

        # Le dossier conditionne la liste des utilisateurs connus : on la
        # rafraîchit quand il change.
        self.folder_input.editingFinished.connect(self._reload_known_users)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._reload_known_users()

    def _known_users(self, root: str) -> list:
        """Utilisateurs à proposer : connus du dossier + session + choix courant."""
        names = [self._session_name]
        if root:
            names += bootstrap.load_users(root)
        names.append(getattr(self, "_initial", config.Settings()).user_name.strip())
        seen = {}
        for name in names:
            name = (name or "").strip()
            if name:
                seen.setdefault(name.casefold(), name)
        return sorted(seen.values(), key=str.casefold)

    def _reload_known_users(self) -> None:
        """Repopule la liste déroulante d'après le dossier partagé saisi."""
        current = self.name_input.currentText().strip()
        self.name_input.clear()
        self.name_input.addItems(self._known_users(self.folder_input.text().strip()))
        self._select_user(current or self._initial.user_name.strip() or self._session_name)

    def _select_user(self, name: str) -> None:
        """Sélectionne ``name`` dans la liste (repli sur le 1er élément)."""
        target = (name or "").strip().casefold()
        for index in range(self.name_input.count()):
            if self.name_input.itemText(index).strip().casefold() == target:
                self.name_input.setCurrentIndex(index)
                return
        if self.name_input.count():
            self.name_input.setCurrentIndex(0)

    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier partagé", self.folder_input.text() or ""
        )
        if chosen:
            self.folder_input.setText(chosen)
            self._reload_known_users()

    def result_settings(self) -> config.Settings:
        initial = getattr(self, "_initial", config.Settings())
        return config.Settings(
            user_name=self.name_input.currentText().strip(),
            shared_root=self.folder_input.text().strip(),
            last_units=list(initial.last_units),
            last_browser_unit=initial.last_browser_unit,
            feed_units=initial.feed_units,
            feed_last_seen=initial.feed_last_seen,
            auto_check_updates=self.auto_update_checkbox.isChecked(),
            last_update_check=initial.last_update_check,
            available_update=initial.available_update,
            update_snooze_until=initial.update_snooze_until,
            update_snooze_version=initial.update_snooze_version,
            close_to_tray=self.close_to_tray_checkbox.isChecked(),
            start_with_windows=(
                self.autostart_checkbox.isChecked()
                if self.autostart_checkbox is not None
                else bool(getattr(initial, "start_with_windows", True))
            ),
            ocr_enabled=(
                self.ocr_checkbox.isChecked()
                if self.ocr_checkbox is not None
                else bool(getattr(initial, "ocr_enabled", True))
            ),
        )

    def accept(self) -> None:  # noqa: D102
        if not self.name_input.currentText().strip():
            QMessageBox.warning(self, "Champ manquant", "L'utilisateur est obligatoire.")
            return
        if not self.folder_input.text().strip():
            QMessageBox.warning(self, "Champ manquant", "Le dossier partagé est obligatoire.")
            return
        super().accept()


def _ensure_user_known(settings: config.Settings) -> None:
    """Enregistre le compte de session (et l'auteur courant) comme utilisateur connu.

    Le nom d'auteur n'est plus saisi librement : on garantit qu'il figure dans la
    liste partagée ``users.json``, en l'ajoutant au besoin.
    """
    if not settings.shared_root.strip():
        return
    for name in (config.session_user_name(), settings.user_name):
        if name.strip():
            bootstrap.add_user(settings.shared_root, name)


# --------------------------------------------------------------------------- #
# Toast (confirmation non bloquante)
# --------------------------------------------------------------------------- #
class Toast(QLabel):
    """Petit bandeau de confirmation qui s'efface tout seul.

    Non modal : il ne bloque rien, disparaît après ``duration_ms`` et se ferme
    si on clique dessus.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background-color: rgba(46, 125, 50, 235); color: white;"
            "border-radius: 8px; font-weight: bold; padding: 0px;"
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, duration_ms: int = 4000) -> None:
        self.setText(text)
        metrics = self.fontMetrics()
        self.setFixedSize(
            metrics.horizontalAdvance(text) + 48,
            metrics.height() + 24,
        )
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(duration_ms)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        margin = 16
        top = margin
        menubar = getattr(parent, "menuBar", None)
        if callable(menubar):
            bar = menubar()
            if bar is not None:
                top = max(top, bar.height() + margin)
        x = max(margin, parent.width() - self.width() - margin)
        y = top
        self.move(x, y)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        self.hide()


# --------------------------------------------------------------------------- #
# Cadre image (aperçu du cas sélectionné)
# --------------------------------------------------------------------------- #
class ImageFrame(QLabel):
    """Cadre noir affichant l'image du cas sélectionné (aucun texte)."""

    clicked = Signal()

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: black;")
        self.setMinimumSize(220, 220)
        self._source: QPixmap | None = None

    def set_source(self, path) -> None:
        """Affiche l'image au chemin donné (ou vide si None/introuvable)."""
        pixmap = QPixmap(str(path)) if path else QPixmap()
        self._source = None if pixmap.isNull() else pixmap
        self._refresh()

    def clear_source(self) -> None:
        self._source = None
        self._refresh()

    def _refresh(self) -> None:
        self.setCursor(Qt.PointingHandCursor if self._source is not None else Qt.ArrowCursor)
        if self._source is None:
            self.clear()
            return
        self.setPixmap(
            self._source.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        self._refresh()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        if self._source is not None:
            self.clicked.emit()


class ClickableLabel(QLabel):
    """Étiquette cliquable (ouvre le détail au clic sur une image)."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        self.clicked.emit()


class FeedCard(QFrame):
    """Carte du fil d'actualité (cliquable sur toute sa zone)."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        self.clicked.emit()


class SelectableLabel(QLabel):
    """Texte sélectionnable dont le clic ne ferme pas le plein écran.

    Sans ``event.accept()``, le clic remonterait au conteneur et déclencherait
    le ``eventFilter`` de :class:`DetailOverlay` (fermeture).
    """

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self.setCursor(Qt.IBeamCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().mousePressEvent(event)
        event.accept()


class DetailOverlay(QWidget):
    """Superposition plein fenêtre : image zoomée + titre, description et métadonnées.

    Ouverte au clic sur une image (Recherche Avancée, Bibliothèque, Fil). Le
    contenu défile si nécessaire ; un clic sur l'image ou le fond ferme, un clic
    dans le texte sélectionne sans fermer. Échap ferme toujours.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: black;")
        self.setFocusPolicy(Qt.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget"
            "{ background: black; }"
        )
        self.scroll.viewport().installEventFilter(self)

        self.container = QWidget()
        self.container.installEventFilter(self)
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(40, 40, 40, 40)
        self.vbox.setSpacing(12)
        self.vbox.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.vbox.addWidget(self.image, 0, Qt.AlignHCenter)

        # Panneau de texte : tout clic dans cette zone (y compris entre les
        # libellés) est consommé et ne ferme pas le plein écran.
        self.text_panel = QWidget()
        self.text_panel.installEventFilter(self)
        self.text_layout = QVBoxLayout(self.text_panel)
        self.text_layout.setContentsMargins(0, 0, 0, 0)
        self.text_layout.setSpacing(12)

        self.title = SelectableLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("color:#fff; font-size:18px; font-weight:bold;")
        self.text_layout.addWidget(self.title, 0, Qt.AlignHCenter)

        self.description = SelectableLabel()
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color:#ddd; font-size:14px;")
        self.text_layout.addWidget(self.description, 0, Qt.AlignHCenter)

        self.meta = SelectableLabel()
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("color:#888; font-size:11px;")
        self.text_layout.addWidget(self.meta, 0, Qt.AlignHCenter)

        self.vbox.addWidget(self.text_panel, 0, Qt.AlignHCenter)

        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)

        self._source = None
        self.hide()

    def show_case(self, case: storage.Case, source=None) -> None:
        """Affiche le détail d'un cas (image fournie ou lue depuis le disque)."""
        pixmap = source
        if pixmap is None:
            pixmap = QPixmap(str(case.image_path)) if case.image_path else QPixmap()
        self._source = None if pixmap is None or pixmap.isNull() else pixmap

        title = (case.title or "").strip()
        if case.request_number:
            title = f"{title} ({case.request_number})"
        self.title.setText(title)
        description = (case.description or "").strip()
        self.description.setText(description)
        self.description.setVisible(bool(description))
        self.meta.setText(self._metadata_text(case))

        self.setGeometry(self.parentWidget().rect())
        self._layout_content()
        self.scroll.verticalScrollBar().setValue(0)
        self.show()
        self.raise_()
        self.setFocus()

    def refresh_geometry(self) -> None:
        if self.isVisible() and self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())
            self._layout_content()

    def _layout_content(self) -> None:
        """Adapte l'image et la largeur des textes à la taille de la fenêtre.

        L'image ne prend que la hauteur **restante** une fois les textes posés
        (plafonnée à 78 %), pour éviter tout ascenseur quand la fenêtre est
        petite ; sur une grande fenêtre, le plafond s'applique et rien ne change.
        """
        margins = self.vbox.contentsMargins()
        inner_width = self.width() - margins.left() - margins.right()
        text_width = max(280, min(1000, int(self.width() * 0.9), inner_width))
        for label in (self.title, self.description, self.meta):
            label.setFixedWidth(text_width)
        self.text_panel.setFixedWidth(text_width)

        if self._source is None:
            self.image.clear()
            self.image.setVisible(False)
            return
        self.image.setVisible(True)

        labels = [self.title]
        if self.description.text().strip():
            labels.append(self.description)
        labels.append(self.meta)
        text_height = sum(self._text_height(label, text_width) for label in labels)
        text_height += self.text_layout.spacing() * max(0, len(labels) - 1)

        spacing = self.vbox.spacing()
        gaps = spacing  # espace entre l'image et le panneau de texte
        available = self.height() - margins.top() - margins.bottom() - text_height - gaps

        max_w = max(200, inner_width)
        max_h = max(120, min(int(self.height() * 0.78), available))
        self.image.setPixmap(
            self._source.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    @staticmethod
    def _text_height(label: QLabel, width: int) -> int:
        """Hauteur d'un texte sur la largeur donnée (retour à la ligne inclus)."""
        from PySide6.QtCore import QRect

        metrics = label.fontMetrics()
        rect = metrics.boundingRect(
            QRect(0, 0, width, 100000), Qt.TextWordWrap, label.text()
        )
        return rect.height()

    @staticmethod
    def _metadata_text(case: storage.Case) -> str:
        """Métadonnées utiles, compactes (petit texte discret en bas)."""
        parts = []
        if case.author:
            parts.append(case.author)
        moment = case.datetime
        if moment:
            parts.append(
                f"Ajouté le {moment.day:02d}.{moment.month:02d}.{moment.year} à {moment:%H:%M}"
            )
        if case.date_modified:
            parts.append("Modifié le " + str(case.date_modified)[:16].replace("T", " "))
        parts.append(case.case_type)
        parts.append("Privé" if case.visibility == storage.VISIBILITY_PRIVATE else "Public")
        if case.units:
            parts.append(", ".join(case.units))
        parts.append(f"#{case.id}")
        return "   ·   ".join(parts)

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        self._layout_content()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (nom imposé par Qt)
        if event.type() == QEvent.MouseButtonPress:
            if obj is getattr(self, "text_panel", None):
                return True  # zone de texte : ne pas fermer
            self.hide()
            return True
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.hide()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


class CaseTextEdit(QTextEdit):
    """Zone de description : Entrée enregistre, Maj+Entrée insère un saut de ligne."""

    submitted = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ConfirmCaseDialog(QDialog):
    """Résumé du cas (avec vignette) demandé avant l'enregistrement."""

    def __init__(self, parent, *, title: str, description: str, units_text: str,
                 visibility: str = storage.VISIBILITY_PUBLIC, case_type: str = "Standard",
                 image=None, heading: str = "Confirmer l'enregistrement"):
        super().__init__(parent)
        self.setWindowTitle(heading)
        self.setMinimumWidth(560)

        outer = QVBoxLayout(self)

        content = QHBoxLayout()
        thumb = QLabel()
        thumb.setFixedSize(200, 150)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setAttribute(Qt.WA_StyledBackground, True)
        thumb.setStyleSheet("background: #1e1e1e; color: #999; border: 1px solid #555;")
        if image is not None and not image.isNull():
            thumb.setPixmap(
                QPixmap.fromImage(image).scaled(200, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            thumb.setText("Aucune image")
        content.addWidget(thumb)

        info = QFormLayout()
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        info.addRow("Titre :", title_label)
        type_emoji = bootstrap.CASE_TYPE_EMOJI.get(case_type, "")
        info.addRow("Type :", QLabel(f"{type_emoji} {case_type}".strip()))
        units_label = QLabel(units_text or "—")
        units_label.setWordWrap(True)
        info.addRow("Unités :", units_label)
        private = visibility == storage.VISIBILITY_PRIVATE
        visibility_label = QLabel("👤 Privé (visible de moi seul)" if private else "👥 Public")
        info.addRow("Visibilité :", visibility_label)
        content.addLayout(info, 1)
        outer.addLayout(content)

        outer.addWidget(QLabel("Description :"))
        description_view = QTextEdit()
        description_view.setReadOnly(True)
        description_view.setFixedHeight(90)
        description_view.setPlainText(description or "—")
        outer.addWidget(description_view)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Enregistrer")
        buttons.button(QDialogButtonBox.Cancel).setText("Annuler")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)


# --------------------------------------------------------------------------- #
# Navigateur de cas (onglets « Bibliothèque » / « Mes cas »)
# --------------------------------------------------------------------------- #
#: Taille des vignettes (image) et largeur d'une carte.
CARD_IMG_W, CARD_IMG_H = 150, 112
CARD_W = CARD_IMG_W + 10

#: Nombre de vignettes construites par palier dans la Bibliothèque : la grille
#: se remplit au fur et à mesure du défilement (reste fluide à 2000+ cas).
BROWSER_PAGE = 60
#: Distance (px) du bas à partir de laquelle on prépare le palier suivant.
GRID_PREFETCH_PX = 600
#: Anti-rebond de la recherche rapide (ms) : évite de reconstruire la grille à
#: chaque frappe pendant la saisie.
QUICK_SEARCH_DEBOUNCE_MS = 220

#: Dimensions du « Fil d'actualité » (cartes, type réseau social).
FEED_CARD_W = 420
FEED_IMG_W, FEED_IMG_H = 387, 267

#: Nombre de cartes du fil construites par palier : le fil se remplit au fur et
#: à mesure du défilement (reste fluide avec un long historique).
FEED_PAGE = 40
#: Distance (px) du haut/bas à partir de laquelle le palier voisin est préparé.
FEED_PREFETCH_PX = 700

#: Fond d'une carte du fil : normal, ou bleu si le cas est « nouveau » (non lu).
FEED_CARD_BG = "#1e1e1e"
FEED_CARD_BG_NEW = "#1b3a5c"

#: Ancrages temporels du fil : (libellé, décalage). J = jours, M = mois, A = ans.
#: Cliquer un ancrage relance le fil à partir de cet instant (remontée vers le passé).
FEED_ANCHORS = [
    ("Aujourd'hui", {}),
    ("J-1", {"days": 1}),
    ("J-3", {"days": 3}),
    ("J-7", {"days": 7}),
    ("J-30", {"days": 30}),
    ("M-3", {"months": 3}),
    ("M-6", {"months": 6}),
    ("A-1", {"years": 1}),
    ("A-2", {"years": 2}),
    ("A-5", {"years": 5}),
]

#: Jours en français : table manuelle (évite les aléas de locale, surtout Windows).
_JOURS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def make_case_type_checkboxes() -> list:
    """3 cases à cocher (📄 Standard / 🎓 Typique / 🤩 Exceptionnel), cochées.

    Chaque case porte la propriété ``case_type`` avec le nom du type.
    """
    boxes = []
    for case_type in bootstrap.CASE_TYPES:
        cb = QCheckBox(f"{case_type['emoji']}  {case_type['name']}")
        cb.setProperty("case_type", case_type["name"])
        cb.setChecked(True)
        boxes.append(cb)
    return boxes


def checked_case_types(boxes) -> set:
    """Noms des types cochés (filtre « type de cas »)."""
    return {cb.property("case_type") for cb in boxes if cb.isChecked()}


class CaseBrowserPage(QWidget):
    """Onglet de navigation : menu d'unités (gauche) + grille de vignettes.

    Le menu de gauche liste « Tous » puis les unités partagées ; la zone
    principale affiche, triés par titre, les cas de l'unité sélectionnée.
    Une bascule en bas du menu choisit entre cas **publics** et cas **privés**
    (ces derniers limités aux cas privés de l'utilisateur courant).
    """

    #: Émis avec le :class:`storage.Case` quand on clique l'image d'une vignette.
    case_activated = Signal(object)

    def __init__(self, settings: config.Settings, visibility: str, parent: QWidget = None):
        super().__init__(parent)
        self.settings = settings
        self.visibility = visibility
        self._unit_buttons: dict = {}
        self._unit_group = QButtonGroup(self)
        self._unit_group.setExclusive(True)
        self._selected_unit = None      # None = toutes les unités
        self._unit_cases: list = []     # cas de l'unité/visibilité courantes
        self._quick_text = ""           # filtre de recherche rapide (titre)
        self._card_widgets: list = []   # vignettes construites
        self._empty_label = None        # message si aucun cas
        self._columns = 0               # colonnes courantes (recalculées au resize)
        self._filtered: list = []       # cas après recherche rapide (triés)
        self._built = 0                 # nombre de vignettes déjà construites
        # Anti-rebond : reconstruire la grille à chaque frappe saccade la saisie.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(QUICK_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._refresh_grid)
        self._build_ui()
        self.reload_units()
        self.reload_cases()

    def set_settings(self, settings: config.Settings) -> None:
        """Met à jour les réglages (dossier partagé/nom) puis rafraîchit la vue."""
        self.settings = settings
        self.reload_units()
        self.reload_cases()

    # --- Construction ----------------------------------------------------- #
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Colonne gauche (~15 %) : menu des unités (haut) + bascule (bas).
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Champ de recherche rapide (filtre instantané des noms de cas).
        search_wrap = QWidget()
        search_layout = QVBoxLayout(search_wrap)
        search_layout.setContentsMargins(6, 6, 6, 6)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Rechercher un cas…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setToolTip("Filtre instantanément les cas affichés par leur nom.")
        self.search_input.textChanged.connect(self._on_quick_search)
        search_layout.addWidget(self.search_input)
        left_layout.addWidget(search_wrap)

        self.menu_scroll = QScrollArea()
        self.menu_scroll.setWidgetResizable(True)
        self.menu_scroll.setFrameShape(QFrame.NoFrame)
        self.menu_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.menu_container = QWidget()
        self.menu_layout = QVBoxLayout(self.menu_container)
        self.menu_layout.setContentsMargins(6, 6, 6, 6)
        self.menu_layout.setSpacing(2)
        self.menu_scroll.setWidget(self.menu_container)
        left_layout.addWidget(self.menu_scroll, 1)

        # Filtre « Type de cas » (3 cases à cocher), au-dessus de la bascule.
        types_box = QWidget()
        types_box.setAttribute(Qt.WA_StyledBackground, True)
        types_box.setStyleSheet("border-top: 1px solid #444;")
        types_layout = QVBoxLayout(types_box)
        types_layout.setContentsMargins(6, 6, 6, 6)
        types_layout.setSpacing(2)
        types_title = QLabel("Type de cas")
        types_title.setStyleSheet("color:#bbb; font-weight:bold;")
        types_layout.addWidget(types_title)
        self.case_type_checkboxes = make_case_type_checkboxes()
        for cb in self.case_type_checkboxes:
            cb.toggled.connect(self._on_case_type_changed)
            types_layout.addWidget(cb)
        left_layout.addWidget(types_box)

        # Bascule « Cas publics » / « Cas privés », ancrée en bas du menu.
        switch = QWidget()
        switch.setAttribute(Qt.WA_StyledBackground, True)
        switch.setStyleSheet("border-top: 1px solid #444;")
        switch_layout = QHBoxLayout(switch)
        switch_layout.setContentsMargins(6, 6, 6, 6)
        switch_layout.setSpacing(0)
        self.visibility_group = QButtonGroup(self)
        self.visibility_group.setExclusive(True)
        self.visibility_buttons: dict = {}
        for value, label in (
            (storage.VISIBILITY_PUBLIC, "👥 Publics"),
            (storage.VISIBILITY_PRIVATE, "👤 Privés"),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton{border:1px solid #555; padding:6px 4px; background:transparent;}"
                "QPushButton:hover{background:#2f2f2f;}"
                "QPushButton:checked{background:#3a5a8c; color:white; font-weight:bold;}"
            )
            button.clicked.connect(lambda _checked, v=value: self._set_visibility(v))
            self.visibility_group.addButton(button)
            self.visibility_buttons[value] = button
            switch_layout.addWidget(button)
        self.visibility_buttons[self.visibility].setChecked(True)
        left_layout.addWidget(switch)

        outer.addWidget(left, 15)

        # Zone principale (~85 %) : grille de vignettes, défilable.
        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.NoFrame)
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setContentsMargins(12, 12, 12, 12)
        self.grid_layout.setSpacing(12)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid_scroll.setWidget(self.grid_container)
        # Chargement progressif : on construit la suite de la grille en approchant
        # du bas (avant-dernière page), pour ne jamais matérialiser tout d'un coup.
        self.grid_scroll.verticalScrollBar().valueChanged.connect(self._on_grid_scroll)
        outer.addWidget(self.grid_scroll, 85)

    def reload_units(self) -> None:
        """Reconstruit le menu des unités (« Tous » en tête) en gardant la sélection."""
        while self.menu_layout.count():
            item = self.menu_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for button in list(self._unit_group.buttons()):
            self._unit_group.removeButton(button)

        self._unit_buttons = {}
        entries = [
            (u["name"], f"{u['emoji']}  {u['name']}" if u.get("emoji") else u["name"])
            for u in bootstrap.load_units(self.settings.shared_root)
        ]
        for value, label in entries:
            button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QPushButton{text-align:left; padding:6px 8px; border:none; border-radius:4px;}"
                "QPushButton:hover{background:#2f2f2f;}"
                "QPushButton:checked{background:#3a5a8c; color:white; font-weight:bold;}"
            )
            button.clicked.connect(lambda _checked, v=value: self._select_unit(v))
            self._unit_group.addButton(button)
            self._unit_buttons[value] = button
            self.menu_layout.addWidget(button)
        self.menu_layout.addStretch()

        # Sélection : on restaure l'unité mémorisée dans les réglages locaux.
        desired = (self.settings.last_browser_unit or "").strip() or None
        if desired is not None and desired not in self._unit_buttons:
            desired = None  # unité mémorisée disparue : on revient à « toutes »
        self._selected_unit = desired
        if self._selected_unit is not None:
            self._unit_buttons[self._selected_unit].setChecked(True)

    def _select_unit(self, value) -> None:
        """Sélectionne une unité (mémorisée) ; re-cliquer revient à toutes les unités."""
        if value == self._selected_unit:
            value = None  # re-clic sur l'unité courante → toutes les unités
        self._selected_unit = value
        self.settings.last_browser_unit = value or ""
        config.save_settings(self.settings)
        # État visuel des boutons : un seul coché (ou aucun).
        self._unit_group.setExclusive(False)
        for name, button in self._unit_buttons.items():
            button.setChecked(name == value)
        self._unit_group.setExclusive(True)
        self.reload_cases()

    def _set_visibility(self, value: str) -> None:
        """Bascule entre cas publics et privés, puis recharge la grille."""
        button = self.visibility_buttons.get(value)
        if button is not None and not button.isChecked():
            button.setChecked(True)  # synchronise l'état visuel (ex : appel par code)
        if value == self.visibility:
            return
        self.visibility = value
        self.reload_cases()

    def reload_cases(self) -> None:
        """Recharge la liste (visibilité + unité) puis applique la recherche rapide."""
        try:
            cases = storage.search_cases(
                self.settings.shared_root,
                viewer=self.settings.user_name,
                visibility=self.visibility,
            )
        except Exception:  # noqa: BLE001 (vue best-effort)
            cases = []

        unit = self._selected_unit
        cases = [case for case in cases if unit in case.units] if unit is not None else cases
        types = checked_case_types(self.case_type_checkboxes)
        self._unit_cases = [case for case in cases if case.case_type in types]
        self._refresh_grid()

    def _on_case_type_changed(self) -> None:
        """Un type de cas a été (dé)coché : recharge la grille."""
        self.reload_cases()

    def _on_quick_search(self, text: str) -> None:
        """Filtre les cas affichés selon le nom saisi (après une courte pause)."""
        self._quick_text = text or ""
        self._search_timer.start()  # reconstruit la grille après l'anti-rebond

    def _refresh_grid(self) -> None:
        """Applique le filtre de recherche rapide et construit le 1er palier."""
        needle = self._quick_text.strip().casefold()
        cases = [
            case for case in self._unit_cases
            if not needle or needle in case.title.casefold()
        ]
        cases.sort(key=lambda case: case.title.casefold())
        self._filtered = cases

        # Supprime les widgets précédents (vignettes ou message vide).
        self._clear_grid()
        for widget in self._card_widgets:
            widget.deleteLater()
        self._card_widgets = []
        self._built = 0
        if self._empty_label is not None:
            self._empty_label.deleteLater()
            self._empty_label = None

        if not cases:
            empty = QLabel("Aucun cas à afficher.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:#888; padding:24px;")
            self._empty_label = empty
            self._layout_cards(force=True)
            return
        self._build_page()

    def _build_page(self) -> None:
        """Construit le palier suivant de vignettes (chargement progressif)."""
        start = self._built
        end = min(len(self._filtered), start + BROWSER_PAGE)
        if start >= end:
            return
        self._card_widgets.extend(
            self._make_card(case) for case in self._filtered[start:end]
        )
        self._built = end
        self._layout_cards(force=True)
        # Grille encore non défilable alors qu'il reste des cas : on complète,
        # sinon le défilement (qui déclenche le palier suivant) ne viendrait pas.
        if self._built < len(self._filtered):
            QTimer.singleShot(0, self._fill_viewport)

    def _fill_viewport(self) -> None:
        """Complète la grille tant qu'elle tient entièrement dans la vue.

        Utilise une estimation déterministe de la hauteur du contenu (et non la
        position de l'ascenseur, pas encore calculée) : évite tout emballement.
        """
        if self._built >= len(self._filtered):
            return
        columns = max(1, self._columns)
        rows = (self._built + columns - 1) // columns
        estimated_height = rows * (CARD_IMG_H + 56)  # image + titre + espacement
        if estimated_height < self.grid_scroll.viewport().height():
            self._build_page()

    def _on_grid_scroll(self, value: int) -> None:
        """Prépare le palier suivant en approchant du bas de la grille."""
        bar = self.grid_scroll.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - GRID_PREFETCH_PX:
            self._build_page()

    def _clear_grid(self) -> None:
        """Détache tous les widgets de la grille (sans les détruire)."""
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)

    def _columns_for_width(self) -> int:
        """Nombre de vignettes par ligne selon la largeur horizontale disponible."""
        viewport = self.grid_scroll.viewport().width()
        margins = self.grid_layout.contentsMargins()
        spacing = self.grid_layout.spacing()
        usable = viewport - margins.left() - margins.right()
        return max(1, (usable + spacing) // (CARD_W + spacing))

    def _layout_cards(self, *, force: bool = False) -> None:
        """Replace les vignettes selon le nombre de colonnes courant."""
        columns = self._columns_for_width()
        if not force and columns == self._columns and self._card_widgets:
            return  # rien à replacer : même largeur et des vignettes présentes
        self._columns = columns
        self._clear_grid()
        if self._empty_label is not None:
            self.grid_layout.addWidget(self._empty_label, 0, 0, 1, columns)
            return
        for i, widget in enumerate(self._card_widgets):
            self.grid_layout.addWidget(widget, i // columns, i % columns)

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        if hasattr(self, "grid_scroll"):
            self._layout_cards()

    def _make_card(self, case: storage.Case) -> QFrame:
        """Vignette d'un cas : image (cliquable) + titre (sous l'image)."""
        card = QFrame()
        card.setFixedWidth(CARD_W)
        card.setToolTip(case.title)
        column = QVBoxLayout(card)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        thumb = ClickableLabel()
        thumb.setFixedSize(CARD_IMG_W, CARD_IMG_H)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setAttribute(Qt.WA_StyledBackground, True)
        thumb.setCursor(Qt.PointingHandCursor)
        thumb.setStyleSheet("background-color: black;")
        thumb.clicked.connect(lambda c=case: self.case_activated.emit(c))
        pixmap = thumbnails.load_thumbnail(case, CARD_IMG_W, CARD_IMG_H)
        if pixmap.isNull():
            thumb.setText("—")
            thumb.setStyleSheet("background-color:#1e1e1e; color:#777;")
        else:
            thumb.setPixmap(pixmap)
        column.addWidget(thumb, 0, Qt.AlignHCenter)

        title = QLabel(case.title)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        title.setFixedWidth(CARD_W)
        column.addWidget(title)
        return card


# --------------------------------------------------------------------------- #
# Fil d'actualité (zone centrale)
# --------------------------------------------------------------------------- #
def _date_label(moment) -> str:
    """Libellé de jour français, ex. « jeudi 10.10.2028 »."""
    return f"{_JOURS_FR[moment.weekday()]} {moment.day:02d}.{moment.month:02d}.{moment.year}"


#: Séparateur des blocs de la ligne de contexte d'une carte du fil.
FEED_META_SEP = "\u00a0·  "


def _feed_meta_text(case: storage.Case, unit_emoji: dict) -> str:
    """Ligne de contexte d'une carte du fil, au-dessus de l'image.

    Trois blocs dans l'ordre demandé : **unités** (icône + nom), **date et
    heure**, **type de cas** (icône + nom). L'auteur n'y figure pas : la carte
    montre un cas, pas une personne.
    """
    units = []
    for name in case.units or ():
        emoji = (unit_emoji.get(name) or "").strip()
        # espace insécable : l'icône ne se sépare jamais de son libellé
        units.append(f"{emoji}\u00a0{name}".strip())
    # virgules entre unités, séparateur large entre les blocs
    unit_block = ", ".join(units) if units else "Sans unité"

    moment = case.datetime
    if moment is not None:
        date_block = (
            f"{moment.day:02d}.{moment.month:02d}.{moment.year}\u00a0{moment:%H:%M}"
        )
    else:
        date_block = (case.date_added or "")[:10]

    type_emoji = (bootstrap.CASE_TYPE_EMOJI.get(case.case_type) or "").strip()
    type_block = f"{type_emoji}\u00a0{case.case_type}".strip()

    return FEED_META_SEP.join([unit_block, date_block, type_block])


class FeedView(QWidget):
    """Fil chronologique type chat (le plus récent en bas).

    Les cas sont listés du plus ancien (haut) au plus récent (bas) ; la vue se
    positionne sur l'instant d'ancrage (par défaut : tout en bas). Un séparateur
    indique le changement de jour. Chaque carte affiche, de haut en bas : une
    ligne de contexte (**unités** avec leur icône, **date et heure**, **type de
    cas** avec son icône — jamais l'auteur), l'image en grand, le titre puis la
    description. Les cas « nouveaux » (non lus) ont un fond bleu,
    qui reste tant que la carte est visible : il s'efface quand la carte **sort
    de l'écran** (défilement passé) ou est cliquée.
    """

    #: Émis avec le :class:`storage.Case` quand on clique l'image d'une carte.
    case_activated = Signal(object)
    #: Émis avec l'identifiant d'un cas lorsqu'une carte passe à « lu ».
    case_read = Signal(object)

    def __init__(self, settings: config.Settings, parent: QWidget = None):
        super().__init__(parent)
        self.settings = settings

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.vbox = QVBoxLayout(self.container)
        self.vbox.setContentsMargins(16, 16, 16, 16)
        self.vbox.setSpacing(16)
        self.scroll.setWidget(self.container)

        self._pending_scroll_widget = None  # carte d'ancrage à rejoindre
        self._scroll_pending = False
        self._scroll_attempts = 0
        self._unread_ids: set = set()       # cas « nouveaux » (fond bleu)
        self._seen_ids: set = set()         # cas déjà passés dans la zone visible
        self._cards_by_id: dict = {}        # id de cas -> carte
        self._cases: list = []              # cas du fil (ancien → récent)
        self._unit_emoji: dict = {}         # nom d'unité -> emoji (units.json)
        self._win_start = 0                 # borne basse de la fenêtre construite
        self._win_end = 0                   # borne haute (exclue)
        self._extending = False             # garde anti-réentrance (extension)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

    def showEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        """Repositionne le fil quand l'onglet devient visible (layout prêt)."""
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_pending_scroll)

    def _apply_pending_scroll(self) -> None:
        """Applique l'ancrage ; réessaie tant que la géométrie n'est pas prête."""
        if not self._scroll_pending:
            return
        if self._try_scroll_to_anchor():
            self._scroll_pending = False
            self._mark_visible_read()  # mémorise les cartes visibles à l'ouverture
            return
        self._scroll_attempts += 1
        if self._scroll_attempts < 60:  # ~2 s max (le temps que le layout se pose)
            QTimer.singleShot(30, self._apply_pending_scroll)
        else:
            self._scroll_pending = False

    def _on_range_changed(self, _minimum: int, _maximum: int) -> None:
        """La plage de défilement vient d'être calculée : on applique l'ancrage."""
        self._apply_pending_scroll()

    def set_settings(self, settings: config.Settings) -> None:
        self.settings = settings

    def _refresh_unit_emoji(self) -> None:
        """Recharge la table unité → emoji (``units.json`` peut avoir changé)."""
        try:
            units = bootstrap.load_units(self.settings.shared_root)
        except Exception:  # noqa: BLE001 (vue best-effort)
            units = []
        self._unit_emoji = {
            unit["name"]: unit.get("emoji", "")
            for unit in units
            if unit.get("name")
        }

    def load(self, anchor=None, units=None, unread_ids=None, case_types=None,
             cases=None) -> None:
        """Reconstruit le fil autour de l'instant d'ancrage (fenêtre progressive).

        Seules les cartes de la fenêtre courante sont construites ; les plus
        anciennes sont préparées en remontant, les plus récentes en descendant.

        ``anchor`` : instant sur lequel se positionner à l'ouverture ; ``None`` =
        le plus récent (bas). ``units`` : unités suivies (``None`` = toutes).
        ``unread_ids`` : identifiants des cas « nouveaux » (carte à fond bleu).
        ``case_types`` : types de cas à afficher (``None`` = tous).
        ``cases`` : cas déjà chargés (évite un second scan) ; ``None`` les charge.
        """
        self._refresh_unit_emoji()
        self._unread_ids = set(unread_ids or ())
        self._seen_ids = set()
        self._cards_by_id = {}
        self._clear_view()

        if cases is None:
            try:
                cases = storage.search_cases(
                    self.settings.shared_root,
                    viewer=self.settings.user_name,
                )
            except Exception:  # noqa: BLE001 (vue best-effort)
                cases = []

        if units is not None:
            wanted = {u.casefold() for u in units}
            cases = [c for c in cases if wanted & {u.casefold() for u in c.units}]
        if case_types is not None:
            cases = [c for c in cases if c.case_type in case_types]

        cases.sort(key=lambda case: case.date_added)  # ancien (haut) → récent (bas)
        self._cases = cases

        if not cases:
            empty = QLabel("Aucun cas à afficher.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:#888; padding:24px;")
            self.vbox.addWidget(empty)
            self.vbox.addStretch()
            self._pending_scroll_widget = None
            self._scroll_pending = False
            self._win_start = self._win_end = 0
            return

        anchor_iso = anchor.isoformat(timespec="seconds") if anchor else None
        # Dernière carte au plus tard à l'instant d'ancrage = point de départ.
        anchor_idx = -1
        for index, case in enumerate(cases):
            if anchor_iso and (case.date_added or "") <= anchor_iso:
                anchor_idx = index
            elif anchor_iso:
                break
        if anchor_idx < 0:
            # Ancre plus ancienne que tout le fil : on montre le début.
            self._win_start, self._win_end = 0, min(len(cases), FEED_PAGE)
            anchor_widget = None
        else:
            self._win_end = anchor_idx + 1
            self._win_start = max(0, self._win_end - FEED_PAGE)
            anchor_widget = None  # renseigné ci-dessous (carte construite)

        for widget in self._build_range(self._win_start, self._win_end):
            self.vbox.addWidget(widget, 0, Qt.AlignHCenter)
        self.vbox.addStretch()

        if anchor_idx >= 0:
            anchor_widget = self._cards_by_id.get(cases[anchor_idx].id)
        self._pending_scroll_widget = anchor_widget
        self._scroll_pending = True
        self._scroll_attempts = 0
        QTimer.singleShot(0, self._apply_pending_scroll)

    def _clear_view(self) -> None:
        """Retire tous les widgets du fil (sans détruire la fenêtre)."""
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _build_range(self, start: int, end: int) -> list:
        """Construit les widgets (séparateurs + cartes) pour ``cases[start:end]``.

        Le séparateur de jour n'est posé que si la date diffère de la carte
        précédente (comparaison sur la liste complète), ce qui reste correct
        lorsqu'on ajoute un palier au-dessus ou en dessous.
        """
        widgets: list = []
        for index in range(start, end):
            case = self._cases[index]
            day = (case.date_added or "")[:10]
            previous_day = (
                (self._cases[index - 1].date_added or "")[:10] if index > 0 else None
            )
            if day and day != previous_day:
                widgets.append(self._date_separator(case))
            card = self._make_feed_card(case)
            self._cards_by_id[case.id] = card
            widgets.append(card)
        return widgets

    def _maybe_extend(self) -> None:
        """Prépare le(s) palier(s) voisin(s) quand on approche du haut ou du bas.

        Boucle protégée par une garde : les signaux de défilement déclenchés par
        l'insertion de widgets ne relancent pas la procédure.
        """
        if self._extending:
            return
        self._extending = True
        try:
            while True:
                bar = self.scroll.verticalScrollBar()
                if bar.maximum() <= 0:
                    break
                if bar.value() <= FEED_PREFETCH_PX and self._win_start > 0:
                    before = self._win_start
                    self._prepend_older()
                    if self._win_start == before:
                        break
                    continue
                if (
                    bar.value() >= bar.maximum() - FEED_PREFETCH_PX
                    and self._win_end < len(self._cases)
                ):
                    before = self._win_end
                    self._append_newer()
                    if self._win_end == before:
                        break
                    continue
                break
        finally:
            self._extending = False

    def _prepend_older(self) -> None:
        """Ajoute un palier d'historique au-dessus, en gardant la vue stable."""
        new_start = max(0, self._win_start - FEED_PAGE)
        block = self._build_range(new_start, self._win_start)
        if not block:
            return
        bar = self.scroll.verticalScrollBar()
        previous_value = bar.value()
        added_height = 0
        for offset, widget in enumerate(block):
            self.vbox.insertWidget(offset, widget, 0, Qt.AlignHCenter)
            added_height += widget.sizeHint().height() + self.vbox.spacing()
        self._win_start = new_start
        # Le contenu a grandi vers le haut : on décale d'autant pour ne pas sauter.
        bar.setValue(previous_value + added_height)

    def _append_newer(self) -> None:
        """Ajoute un palier de cas plus récents en dessous (avant l'étirement)."""
        new_end = min(len(self._cases), self._win_end + FEED_PAGE)
        block = self._build_range(self._win_end, new_end)
        if not block:
            return
        stretch_index = self.vbox.count() - 1  # dernier item = étirement final
        for offset, widget in enumerate(block):
            self.vbox.insertWidget(stretch_index + offset, widget, 0, Qt.AlignHCenter)
        self._win_end = new_end

    # --- Cas « nouveaux » (fond bleu) ------------------------------------- #
    def _on_scroll(self, _value: int) -> None:
        """À chaque défilement : marque lues les cartes vues et prépare la suite."""
        self._mark_visible_read()
        self._maybe_extend()

    def _mark_visible_read(self) -> None:
        """Marque lues les cartes « nouvelles » déjà vues puis sorties de l'écran.

        Une carte reste bleue tant qu'elle est visible ; elle passe au gris
        lorsqu'elle quitte la zone visible (défilement passé).
        """
        if not self._unread_ids:
            return
        bar = self.scroll.verticalScrollBar()
        top = bar.value()
        bottom = top + self.scroll.viewport().height()
        for case_id in list(self._unread_ids):
            card = self._cards_by_id.get(case_id)
            if card is None:
                continue
            rect = card.geometry()
            if rect.bottom() > top and rect.top() < bottom:
                self._seen_ids.add(case_id)   # carte (re)vue : on la garde bleue
            elif case_id in self._seen_ids:
                self._mark_read(case_id)      # vue puis sortie de l'écran

    def _mark_read(self, case_id) -> None:
        """Passe une carte à « lue » (retire le fond bleu)."""
        if case_id not in self._unread_ids:
            return
        self._unread_ids.discard(case_id)
        self._seen_ids.discard(case_id)
        card = self._cards_by_id.get(case_id)
        if card is not None:
            self._apply_card_style(card, unread=False)
        self.case_read.emit(case_id)

    def _try_scroll_to_anchor(self) -> bool:
        """Amène la carte d'ancrage au bas de la vue (mode chat).

        Retourne ``False`` si la géométrie n'est pas encore positionnée (l'appelant
        réessaiera).
        """
        widget = self._pending_scroll_widget
        bar = self.scroll.verticalScrollBar()
        if widget is None:
            bar.setValue(0)
            return True
        y = widget.mapTo(self.container, QPoint(0, 0)).y()
        if y == 0 or bar.maximum() <= 0:
            return False  # géométrie et/ou plage pas encore prêtes : on réessaiera
        target = y + widget.height() - self.scroll.viewport().height()
        bar.setValue(max(0, min(target, bar.maximum())))
        return True

    @staticmethod
    def _date_separator(case: storage.Case) -> QWidget:
        """Barre de séparation pleine largeur : ligne — jour — ligne."""
        moment = case.datetime
        text = _date_label(moment) if moment else (case.date_added or "")[:10]

        container = QWidget()
        container.setFixedWidth(FEED_CARD_W)
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 6, 0, 6)
        row.setSpacing(12)

        def line() -> QFrame:
            bar = QFrame()
            bar.setFrameShape(QFrame.NoFrame)
            bar.setFixedHeight(1)
            bar.setStyleSheet("background:#555;")
            return bar

        label = QLabel(text)
        label.setStyleSheet("color:#bbb; font-weight:bold;")
        row.addWidget(line(), 1)
        row.addWidget(label)
        row.addWidget(line(), 1)
        return container

    def _make_feed_card(self, case: storage.Case) -> QFrame:
        """Carte du fil : contexte (unités, date, type) + image + titre + légende."""
        card = FeedCard()
        card.setObjectName("feedCard")
        card.setAttribute(Qt.WA_StyledBackground, True)
        card.setFixedWidth(FEED_CARD_W)
        card.setCursor(Qt.PointingHandCursor)
        self._apply_card_style(card, unread=case.id in self._unread_ids)
        card.clicked.connect(lambda c=case: self._mark_read(c.id))
        column = QVBoxLayout(card)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(8)

        # Ligne de contexte, au-dessus de l'image : unités (icône + nom), date et
        # heure, type de cas (icône + nom). Pas d'auteur : la carte montre un cas.
        # Le texte peut passer sur deux lignes si les noms d'unités sont longs.
        meta = QLabel(_feed_meta_text(case, self._unit_emoji))
        meta.setWordWrap(True)
        meta.setFixedWidth(FEED_IMG_W)
        meta.setStyleSheet("color:#b3bac1; font-size:12px;")
        column.addWidget(meta)

        image = ClickableLabel()
        image.setFixedSize(FEED_IMG_W, FEED_IMG_H)
        image.setAlignment(Qt.AlignCenter)
        image.setAttribute(Qt.WA_StyledBackground, True)
        image.setCursor(Qt.PointingHandCursor)
        image.setStyleSheet("background:black; border-radius:6px;")
        image.clicked.connect(lambda c=case: self._on_image_clicked(c))
        pixmap = thumbnails.load_thumbnail(case, FEED_IMG_W, FEED_IMG_H)
        if pixmap.isNull():
            image.setText("—")
            image.setStyleSheet("background:#111; color:#777; border-radius:6px;")
        else:
            image.setPixmap(pixmap)
        column.addWidget(image, 0, Qt.AlignHCenter)

        title = QLabel(case.title)
        title.setWordWrap(True)
        title.setFixedWidth(FEED_IMG_W)
        title.setStyleSheet("font-weight:bold; font-size:15px; color:#fff;")
        column.addWidget(title)

        description = case.description.strip()
        if description:
            body = QLabel(description)
            body.setWordWrap(True)
            body.setFixedWidth(FEED_IMG_W)
            body.setStyleSheet("color:#ddd;")
            column.addWidget(body)
        return card

    def _on_image_clicked(self, case: storage.Case) -> None:
        """Clic sur l'image : marque lu, puis ouvre le zoom détail."""
        self._mark_read(case.id)
        self.case_activated.emit(case)

    @staticmethod
    def _apply_card_style(card: QFrame, *, unread: bool) -> None:
        background = FEED_CARD_BG_NEW if unread else FEED_CARD_BG
        card.setStyleSheet(f"#feedCard{{background:{background}; border-radius:10px;}}")


# --------------------------------------------------------------------------- #
# Fenêtre principale
# --------------------------------------------------------------------------- #
class _NaturalHeightPanel(QWidget):
    """Panneau qui ne grandit jamais au-delà de sa hauteur naturelle.

    Utilisé pour le panneau « Temporalité » du Fil d'actualité : il conserve sa
    hauteur naturelle (aucun espace mort sous les ancres) tout en restant le
    premier panneau à se comprimer — avec ascenseur — quand la fenêtre devient
    trop courte.
    """

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        # Fige la hauteur maximale sur la taille préférée, recalculée au besoin
        # (changement de police / d'échelle de l'écran).
        hint = self.sizeHint().height()
        if self.maximumHeight() != hint:
            self.setMaximumHeight(hint)


class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings, single_instance=None):
        super().__init__()
        self.settings = settings
        self.current_image: QImage | None = None
        self._results: dict[int, storage.Case] = {}
        #: Verrou mono-instance (``tray.SingleInstance``) : réveille cette fenêtre
        #: quand l'utilisateur relance l'exécutable alors qu'elle est masquée.
        self._single_instance = single_instance
        #: Vrai pendant un arrêt voulu : la fermeture ne doit alors pas réduire
        #: l'application dans la zone de notification.
        self._quitting = False
        #: Enregistrement (création/édition) en tâche de fond : un seul à la fois.
        self._save_thread = None
        self._save_worker = None
        self._save_kind = ""
        #: Vrai si l'utilisateur a demandé à quitter pendant un enregistrement :
        #: l'arrêt est alors différé jusqu'à la fin du thread.
        self._quit_after_save = False

        # Fil d'actualité : état de session pour la pastille de nouveauté
        # (défini avant la construction des onglets, qui lit cet état).
        self._feed_session_watermark = settings.feed_last_seen or ""
        self._feed_read_ids: set = set()
        #: État de l'alerte du tray (voir ``_update_tray_alert``) : sert à ne
        #: déclencher la bannière qu'au passage repos → nouveau, et à ne pas en
        #: afficher au démarrage (``_tray_alert_ready``).
        self._tray_alert_active = False
        self._tray_alert_ready = False

        self.setWindowTitle("RadCase Manager")
        self.setGeometry(120, 120, 980, 680)

        self.tabs = QTabWidget()
        self.new_case_page = self._build_new_case_tab()
        self.new_case_tab_index = self.tabs.addTab(self.new_case_page, "➕ Nouveau cas")
        # Onglet « Fil d'actualité » (contenu à venir), juste après la saisie.
        # La pastille (⚪ par défaut, 🔵 si du nouveau) sert d'icône d'onglet.
        self.feed_tab_index = self.tabs.addTab(
            self._build_feed_tab(), f"{FEED_DOT_EMOJI} Fil d'actualité"
        )
        # Onglet de navigation « Bibliothèque » : menu d'unités (gauche) + grille
        # de vignettes. La bascule en bas du menu choisit cas publics / privés.
        self.browser = CaseBrowserPage(settings, storage.VISIBILITY_PUBLIC)
        self.browser.case_activated.connect(self._show_case_detail)
        self.public_list_tab_index = self.tabs.addTab(self.browser, "📖 Bibliothèque")
        self.consult_tab_index = self.tabs.addTab(self._build_consult_tab(), "🔎 Recherche Avancée")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self.edit_page = None          # construit à la demande
        self._editing_case = None

        # Bandeau de confirmation non bloquant (créé une fois, réutilisé).
        self._toast = Toast(self)
        # Superposition de détail (image + titre + description + métadonnées).
        self._overlay = DetailOverlay(self)
        self._selected_case = None
        self.case_image.clicked.connect(self._show_selected_case_detail)

        self._build_status_bar()

        # Zone de notification : l'application reste joignable quand sa fenêtre
        # est fermée (réduction). ``None`` si le système n'en propose pas.
        self._tray_hint_shown = False
        self._tray = self._build_tray()
        if single_instance is not None:
            single_instance.activated.connect(self.restore_from_tray)

        # Badge de nouveauté du fil : vérification périodique (toutes les 5 min).
        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(FEED_BADGE_INTERVAL_MS)
        self._feed_timer.timeout.connect(self._update_feed_badge)
        self._feed_timer.start()
        self._update_feed_badge(getattr(self, "_feed_cases", None))

        # Mise à jour automatique : état des tâches de fond + vérification
        # différée (on laisse d'abord la fenêtre s'afficher).
        self._update_thread = None
        self._update_worker = None
        self._download_thread = None
        self._download_worker = None
        self._progress_dialog = None
        self._check_silent = False
        self._pending_tag = ""
        QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._auto_check_updates)

        # Restes d'une mise à jour précédente, à côté de l'exécutable : on retente
        # leur mise à la corbeille à intervalles espacés, la première tentative au
        # démarrage pouvant se heurter à un verrou (antivirus).
        self._cleanup_delays = list(LEFTOVER_CLEANUP_DELAYS_MS)
        QTimer.singleShot(self._cleanup_delays.pop(0), self._retry_leftover_cleanup)

    def _build_status_bar(self) -> None:
        """Barre de statut : rouage (paramètres) + nom d'utilisateur / dossier."""
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙️")
        self.settings_btn.setToolTip("Paramètres (nom, dossier partagé)")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setAutoRaise(True)
        self.settings_btn.clicked.connect(self.open_settings)

        self.user_label = QLabel()

        bar = self.statusBar()
        bar.addWidget(self.settings_btn)
        bar.addWidget(self.user_label)
        # Version affichée en bas à droite : utile pour savoir d'un coup d'œil
        # quelle version tourne sur un poste.
        self.version_label = QLabel(updater.display_version())
        self.version_label.setStyleSheet("color: gray;")
        self.version_label.setToolTip("Version de RadCase Manager")
        bar.addPermanentWidget(self.version_label)
        # Indicateur persistant quand une version plus récente est connue : il
        # reste visible même après un « Plus tard », et un clic relance la
        # proposition.
        self.update_label = ClickableLabel()
        self.update_label.setCursor(Qt.PointingHandCursor)
        self.update_label.setStyleSheet("color:#e6a23c; font-weight:bold;")
        self.update_label.clicked.connect(lambda: self.check_updates())
        self.update_label.hide()
        bar.addPermanentWidget(self.update_label)
        self._refresh_update_indicator()
        self._update_status()

    def _update_status(self) -> None:
        self.user_label.setText(
            f"Utilisateur : {self.settings.user_name}   |   Dossier : {self.settings.shared_root}"
        )

    def set_feed_dot(self, emoji: str = FEED_DOT_EMOJI, count: int = 0) -> None:
        """Met à jour l'icône de l'onglet « Fil d'actualité » (et le compteur)."""
        text = f"{emoji} Fil d'actualité"
        if count > 0:
            text += f" ({count})"
        self.tabs.setTabText(self.feed_tab_index, text)

    def _load_visible_cases(self) -> list:
        """Charge les cas visibles pour l'utilisateur courant (best-effort).

        Centralise l'accès à l'index pour qu'un même passage d'onglet ne
        déclenche qu'un seul chargement, réutilisé par le fil et son badge.
        """
        try:
            return storage.search_cases(
                self.settings.shared_root, viewer=self.settings.user_name
            )
        except Exception:  # noqa: BLE001 (vue best-effort)
            return []

    def _unread_cases(
        self,
        cases=None,
        *,
        since: str | None = None,
        include_read: bool = False,
    ) -> list:
        """Cas du fil « nouveaux » (non lus) pour la session courante.

        Nouveau = visible, dans une unité suivie, plus récent que le filigrane,
        hors mes propres cas, et — sauf ``include_read`` — pas encore lu cette
        session.
        ``cases`` : liste déjà chargée (évite un second scan) ; ``None`` la charge.
        ``since`` : filigrane à utiliser (défaut : début de session). Le tray
        passe ``feed_last_seen`` pour une sémantique « depuis la dernière visite
        du Fil » (voir :meth:`_update_tray_alert`).
        ``include_read`` : ignorer les cartes déjà vues dans cette session.
        """
        units = self.selected_feed_units()
        if not units:
            return []
        wanted = {u.casefold() for u in units}
        boxes = getattr(self, "feed_case_type_checkboxes", None)
        types = checked_case_types(boxes) if boxes is not None else None
        watermark = self._feed_session_watermark if since is None else since
        me = (self.settings.user_name or "").strip().casefold()
        if cases is None:
            cases = self._load_visible_cases()
        result = []
        for case in cases:
            if not (wanted & {u.casefold() for u in case.units}):
                continue
            if types is not None and case.case_type not in types:
                continue
            if watermark and (case.date_added or "") <= watermark:
                continue
            if me and case.author.strip().casefold() == me:
                continue  # ne pas compter mes propres cas
            if not include_read and case.id in self._feed_read_ids:
                continue
            result.append(case)
        return result

    def _on_feed_case_read(self, case_id) -> None:
        """Une carte vient d'être lue (scroll ou clic) : met à jour le badge."""
        if case_id and case_id not in self._feed_read_ids:
            self._feed_read_ids.add(case_id)
            # La liste des cas n'a pas changé : on réutilise le dernier chargement
            # (évite une recherche à chaque carte marquée lue pendant le défilement).
            self._update_feed_badge(getattr(self, "_feed_cases", None))

    def _on_feed_case_activated(self, case) -> None:
        """Clic sur l'image d'une carte : marque lu, puis ouvre le zoom détail."""
        if case is not None:
            self._on_feed_case_read(case.id)
            self._show_case_detail(case)

    def _update_feed_badge(self, cases=None) -> None:
        """Pastille = nombre de cartes du fil encore « nouvelles » (non lues).

        ``cases`` : liste déjà chargée (réutilisée) ; ``None`` la recharge (appel
        périodique, qui doit voir les cas ajoutés par les autres).
        """
        if cases is None:
            cases = self._load_visible_cases()
        count = len(self._unread_cases(cases))
        self.set_feed_dot(FEED_DOT_NOTIFY if count else FEED_DOT_EMOJI, count)
        self._update_tray_alert(cases)

    def _update_tray_alert(self, cases=None) -> None:
        """Pastille rouge de l'icône de notification : « du nouveau depuis la visite ».

        Sémantique volontairement **distincte** de l'onglet : l'icône redevient
        normale dès que le Fil est ouvert (``feed_last_seen`` est alors réécrit),
        alors que le compteur de l'onglet attend que chaque carte ait défilé à
        l'écran. Sans quoi l'icône resterait rouge après une visite rapide, ce
        qui est le pire des signaux pour un indicateur de tray.

        Les filtres restent ceux du Fil (unités suivies, types, propres cas) :
        l'icône ne signale pas « un cas de plus sur le partage », mais bien « du
        nouveau dans *ton* fil ». Tant qu'aucune visite n'est enregistrée
        (première installation), il n'y a pas de référence : pas de pastille.
        """
        tray_icon = getattr(self, "_tray", None)
        if tray_icon is None:
            return
        watermark = self.settings.feed_last_seen or ""
        if not watermark:
            # Aucune visite du Fil enregistrée : il n'y a pas de « depuis » auquel
            # comparer. Sans cette garde, une première installation allumerait
            # l'icône pour tout l'historique (« 99+ nouveaux cas »), alors que ces
            # cas ne sont pas nouveaux.
            tray_icon.set_alert(False)
            return
        if cases is None:
            cases = self._load_visible_cases()
        fresh = self._unread_cases(cases, since=watermark, include_read=True)
        active = bool(fresh)
        was_active = self._tray_alert_active
        ready = self._tray_alert_ready
        self._tray_alert_active = active
        self._tray_alert_ready = True
        tray_icon.set_alert(active, len(fresh))
        if active and not was_active and ready and self._window_hidden():
            self._notify_new_cases(fresh)

    def _window_hidden(self) -> bool:
        """Vrai si la fenêtre n'est ni affichée ni réduite dans la barre des tâches.

        Seul cas où la bannière apporte quelque chose : fenêtre visible, l'onglet
        du Fil porte déjà l'information et la bannière ne ferait que gêner.
        """
        return (not self.isVisible()) or self.isMinimized()

    def _notify_new_cases(self, fresh: list) -> None:
        """Bannière système : **une seule** par vague, sur le cas le plus récent.

        Pas une bannière par cas : plusieurs cas déposés d'un coup produiraient
        une salve de notifications, et Windows finirait par les regrouper. Le
        titre du cas est ramené sur une ligne et raccourci (une notification
        système tronque de toute façon, autant choisir où).
        """
        tray_icon = getattr(self, "_tray", None)
        if tray_icon is None:
            return
        count = len(fresh)
        newest = max(fresh, key=lambda case: case.date_added or "")
        units = " / ".join(newest.units) or "Sans unité"

        if count == 1:
            title = "Nouveau cas dans le Fil"
        else:
            title = f"{count} nouveaux cas dans le Fil"

        if TRAY_TOAST_WITH_TITLE:
            label = " ".join((newest.title or "").split()) or newest.id
            if len(label) > 70:
                label = label[:69].rstrip() + "…"
            message = f"{units} — {label}"
        else:
            message = f"{units} — un nouveau cas"
        if count > 1:
            others = count - 1
            message += f"\n+ {others} autre" + ("s" if others > 1 else "") + " cas"
        tray_icon.notify(title, message)

    def _open_feed_from_toast(self) -> None:
        """Clic sur la bannière : ramène la fenêtre **sur le Fil d'actualité**."""
        self.restore_from_tray()
        if hasattr(self, "feed_tab_index"):
            self.tabs.setCurrentIndex(self.feed_tab_index)

    # --- Synchronisation ponctuelle du presse-papiers (activation / onglet) ---
    def changeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        """Détecte le retour au premier plan de la fenêtre."""
        super().changeEvent(event)
        if not hasattr(self, "tabs"):
            return  # événement reçu pendant la construction de la fenêtre
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            # À l'activation : on resynchronise TOUJOURS avec le presse-papiers,
            # même si une image est déjà chargée (elle est remplacée).
            self._sync_clipboard(overwrite=True)

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        toast = getattr(self, "_toast", None)
        if toast is not None and toast.isVisible():
            toast._reposition()
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            overlay.refresh_geometry()

    def _show_case_detail(self, case) -> None:
        """Ouvre le détail zoomé (image + titre + description + métadonnées)."""
        if case is not None:
            self._overlay.show_case(case)

    def _show_selected_case_detail(self) -> None:
        """Détail du cas actuellement sélectionné dans « Recherche Avancée »."""
        if self._selected_case is not None:
            self._overlay.show_case(self._selected_case)

    def _on_tab_changed(self, index: int) -> None:
        if index == self.new_case_tab_index:
            self._sync_clipboard(overwrite=False)
        elif index == self.feed_tab_index:
            # Soft reset : la prochaine visite ne verra plus ces cas comme nouveaux.
            from datetime import datetime

            self.settings.feed_last_seen = datetime.now().isoformat(timespec="seconds")
            config.save_settings(self.settings)
            self.reload_feed_units()
            self._update_feed_badge(getattr(self, "_feed_cases", None))
        elif index == self.public_list_tab_index:
            self.browser.reload_units()
            self.browser.reload_cases()
        elif index == self.consult_tab_index:
            self.reload_filter_authors()
            # Affiche par défaut une recherche vide sur la période par défaut.
            self.run_search()

    def _sync_clipboard(self, *, overwrite: bool) -> None:
        """Charge l'image du presse-papiers (une fois).

        - ``overwrite=True`` (activation de la fenêtre) : remplace l'image déjà
          chargée par celle du presse-papiers.
        - ``overwrite=False`` (ouverture de l'onglet) : ne remplit que si vide.

        Dans tous les cas, on ne touche à rien si le presse-papiers ne contient
        pas d'image (un texte copié n'efface pas l'image en cours).
        """
        # Pendant un enregistrement de fond, le formulaire est figé : ne pas
        # remplacer l'image sous les pieds du worker.
        if self._save_thread is not None:
            return
        # En mode édition, on ne remplace JAMAIS l'image du cas automatiquement.
        if self.edit_page is not None and self.tabs.currentWidget() is self.edit_page:
            return
        if self.tabs.currentIndex() != self.new_case_tab_index:
            return
        if not overwrite and self.current_image is not None:
            return

        clipboard = QApplication.clipboard()
        if clipboard.ownsClipboard():
            return  # changement provoqué par nous-mêmes
        image = clipboard.image()
        if image.isNull():
            return  # pas d'image dans le presse-papiers : on garde l'existant

        self._display_image(image)
        self.clipboard_hint.setText(
            "📋 Image synchronisée avec le presse-papiers."
            if overwrite else
            "📋 Image détectée dans le presse-papiers (importée automatiquement)."
        )

    # --- Onglet « Fil d'actualité » (contenu à venir) --------------------- #
    def _build_feed_tab(self) -> QWidget:
        """Onglet « Fil d'actualité » : menu d'unités suivies (cases à cocher).

        La zone centrale reste vide pour l'instant.
        """
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Colonne gauche (~15 %) : moitié haute = unités suivies,
        # moitié basse = navigation dans le temps.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # --- Moitié haute : sélection des unités ---
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        units_title = QLabel("Sélection des unités")
        units_title.setStyleSheet("color:#bbb; font-weight:bold; padding:6px 6px 2px 6px;")
        top_layout.addWidget(units_title)

        # Boutons « tout cocher » puis « tout décocher », juste sous le titre.
        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(6, 2, 6, 6)
        actions_layout.setSpacing(4)
        check_all = QPushButton("Tout cocher")
        check_all.clicked.connect(lambda: self._toggle_feed_units(True))
        uncheck_all = QPushButton("Tout décocher")
        uncheck_all.clicked.connect(lambda: self._toggle_feed_units(False))
        actions_layout.addWidget(check_all)
        actions_layout.addWidget(uncheck_all)
        top_layout.addWidget(actions)

        self.feed_units_scroll = QScrollArea()
        self.feed_units_scroll.setWidgetResizable(True)
        self.feed_units_scroll.setFrameShape(QFrame.NoFrame)
        self.feed_units_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.feed_units_container = QWidget()
        self.feed_units_layout = QVBoxLayout(self.feed_units_container)
        self.feed_units_layout.setContentsMargins(6, 0, 6, 6)
        self.feed_units_layout.setSpacing(2)
        self.feed_units_scroll.setWidget(self.feed_units_container)
        top_layout.addWidget(self.feed_units_scroll, 1)

        left_layout.addWidget(top)

        # --- Zone basse : ancrages temporels ---
        # La temporalité est le panneau qui cède en premier : étiré dans la mise
        # en page (il absorbe le manque de place) mais plafonné à sa hauteur
        # naturelle (aucun espace mort sous les ancres). La liste des unités,
        # sans étirement, reste donc déployée au maximum et n'est comprimée —
        # avec ascenseur — qu'une fois la temporalité réduite au minimum.
        bottom = _NaturalHeightPanel()
        bottom.setAttribute(Qt.WA_StyledBackground, True)
        bottom.setStyleSheet("border-top: 1px solid #444;")
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(6, 6, 6, 6)
        bottom_layout.setSpacing(4)

        temporalite_title = QLabel("Temporalité")
        temporalite_title.setStyleSheet("color:#bbb; font-weight:bold; padding:0 0 2px 0;")
        bottom_layout.addWidget(temporalite_title)

        anchors_scroll = QScrollArea()
        anchors_scroll.setWidgetResizable(True)
        anchors_scroll.setFrameShape(QFrame.NoFrame)
        anchors_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        anchors_container = QWidget()
        anchors_layout = QVBoxLayout(anchors_container)
        anchors_layout.setContentsMargins(0, 0, 0, 0)
        anchors_layout.setSpacing(4)
        self.feed_anchor_buttons: dict = {}
        self.feed_anchor_group = QButtonGroup(self)
        self.feed_anchor_group.setExclusive(True)
        for name, _params in FEED_ANCHORS:
            button = QPushButton(name)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked, n=name: self._on_feed_anchor(n))
            self.feed_anchor_group.addButton(button)
            self.feed_anchor_buttons[name] = button
            anchors_layout.addWidget(button)
        anchors_layout.addStretch()
        anchors_scroll.setWidget(anchors_container)
        bottom_layout.addWidget(anchors_scroll)

        left_layout.addWidget(bottom, 1)

        # --- Zone basse : filtre « Type de cas » (3 cases à cocher) ---
        feed_types_box = QWidget()
        feed_types_box.setAttribute(Qt.WA_StyledBackground, True)
        feed_types_box.setStyleSheet("border-top: 1px solid #444;")
        feed_types_layout = QVBoxLayout(feed_types_box)
        feed_types_layout.setContentsMargins(6, 6, 6, 6)
        feed_types_layout.setSpacing(2)
        feed_types_title = QLabel("Type de cas")
        feed_types_title.setStyleSheet("color:#bbb; font-weight:bold; padding:0 0 2px 0;")
        feed_types_layout.addWidget(feed_types_title)
        self.feed_case_type_checkboxes = make_case_type_checkboxes()
        for cb in self.feed_case_type_checkboxes:
            cb.toggled.connect(self._on_feed_case_type_changed)
            feed_types_layout.addWidget(cb)
        # Zone compacte : uniquement la hauteur des 3 coches (pas de stretch).
        left_layout.addWidget(feed_types_box)

        layout.addWidget(left, 15)

        # Zone centrale : fil chronologique (le plus récent en bas, mode chat).
        self.feed_content = QWidget()
        feed_content_layout = QVBoxLayout(self.feed_content)
        feed_content_layout.setContentsMargins(0, 0, 0, 0)
        self.feed_view = FeedView(self.settings)
        self.feed_view.case_activated.connect(self._on_feed_case_activated)
        self.feed_view.case_read.connect(self._on_feed_case_read)
        feed_content_layout.addWidget(self.feed_view)
        layout.addWidget(self.feed_content, 85)

        self.feed_anchor_buttons[FEED_ANCHORS[0][0]].setChecked(True)  # « Aujourd'hui »
        self.reload_feed_units()
        return page

    def reload_feed_units(self) -> None:
        """Recharge les unités suivies (cases à cocher) et restaure la sélection."""
        while self.feed_units_layout.count():
            item = self.feed_units_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        remembered = self.settings.feed_units  # None = tout coché par défaut
        self.feed_unit_checkboxes = []
        for unit in bootstrap.load_units(self.settings.shared_root):
            label = f"{unit['emoji']}  {unit['name']}" if unit.get("emoji") else unit["name"]
            cb = QCheckBox(label)
            cb.setProperty("unit_name", unit["name"])
            cb.setChecked(remembered is None or unit["name"] in remembered)
            cb.toggled.connect(self._remember_feed_units)
            self.feed_unit_checkboxes.append(cb)
            self.feed_units_layout.addWidget(cb)
        self.feed_units_layout.addStretch()
        self._refresh_feed()

    def selected_feed_units(self) -> list[str]:
        """Unités suivies actuellement cochées dans le fil."""
        return [
            cb.property("unit_name")
            for cb in getattr(self, "feed_unit_checkboxes", [])
            if cb.isChecked()
        ]

    def selected_feed_case_types(self) -> set:
        """Types de cas cochés dans le fil."""
        return checked_case_types(getattr(self, "feed_case_type_checkboxes", []))

    def _on_feed_case_type_changed(self) -> None:
        """Un type de cas a été (dé)coché dans le fil : rafraîchit le fil et le badge."""
        self._refresh_feed()
        self._update_feed_badge()

    def _remember_feed_units(self) -> None:
        """Mémorise la sélection d'unités suivies, puis rafraîchit le fil."""
        self.settings.feed_units = self.selected_feed_units()
        config.save_settings(self.settings)
        self._refresh_feed()
        self._update_feed_badge()

    def _toggle_feed_units(self, checked: bool) -> None:
        """Coche ou décoche toutes les unités suivies."""
        for cb in getattr(self, "feed_unit_checkboxes", []):
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        self._remember_feed_units()

    def _active_feed_anchor(self) -> str:
        """Nom de l'ancrage temporel actif (défaut : « Aujourd'hui »)."""
        for name, button in getattr(self, "feed_anchor_buttons", {}).items():
            if button.isChecked():
                return name
        return FEED_ANCHORS[0][0]

    def _feed_anchor_datetime(self, name: str):
        """Instant d'ancrage correspondant au bouton de temporalité."""
        from datetime import datetime, timedelta

        params = dict(FEED_ANCHORS).get(name, {})
        now = datetime.now()
        months = params.get("months", 0) + params.get("years", 0) * 12
        moment = self._shift_months(now, months) if months else now
        days = params.get("days", 0)
        if days:
            moment = moment - timedelta(days=days)
        return moment

    def _on_feed_anchor(self, _name: str) -> None:
        """Relance le fil à partir de l'instant d'ancrage choisi."""
        self._refresh_feed()

    def _refresh_feed(self) -> None:
        """(Re)charge le fil selon l'ancrage actif et les unités suivies."""
        if not hasattr(self, "feed_view"):
            return
        anchor = self._feed_anchor_datetime(self._active_feed_anchor())
        cases = self._load_visible_cases()  # un seul chargement pour le fil + le badge
        self._feed_cases = cases
        unread = {case.id for case in self._unread_cases(cases)}
        self.feed_view.load(
            anchor,
            units=self.selected_feed_units(),
            unread_ids=unread,
            case_types=self.selected_feed_case_types(),
            cases=cases,
        )

    # --- Onglet « Nouveau cas » ------------------------------------------- #
    def _build_new_case_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)

        # Colonne gauche : aperçu de l'image.
        left = QVBoxLayout()
        left.addWidget(QLabel("<h3>Image du cas (obligatoire)</h3>"))
        self.preview = QLabel("Aucune image.\n⚠️ Une image est obligatoire pour enregistrer.\nL'image du presse-papiers est importée à l'activation,\nou cliquez sur « Coller l'image du presse-papiers ».")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.preview.setAttribute(Qt.WA_StyledBackground, True)
        self.preview.setStyleSheet(
            "border: 1px dashed #555; background: #1e1e1e; color: #999;"
        )
        left.addWidget(self.preview)

        paste_btn = QPushButton("📋 Coller l'image du presse-papiers")
        paste_btn.clicked.connect(self.paste_image)
        left.addWidget(paste_btn)

        clear_img_btn = QPushButton("Retirer l'image")
        clear_img_btn.clicked.connect(self.clear_image)
        left.addWidget(clear_img_btn)

        visibility_row, self.visibility_buttons = self._make_visibility_row()
        left.addWidget(visibility_row)

        type_row, self.case_type_buttons = self._make_case_type_row()
        left.addWidget(type_row)

        self.clipboard_hint = QLabel("")
        self.clipboard_hint.setWordWrap(True)
        self.clipboard_hint.setStyleSheet("color: #2e7d32;")
        left.addWidget(self.clipboard_hint)
        left.addStretch()

        layout.addLayout(left, 1)

        # Colonne droite : formulaire.
        right = QVBoxLayout()

        details = QGroupBox("Détails du cas")
        details_form = QFormLayout(details)
        # Force tous les champs (titre, description) à occuper toute la largeur.
        # Sans cela, selon le style (ex: macOS), QFormLayout laisse chaque champ
        # à sa largeur naturelle et le titre reste plus court que la description.
        details_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Ex: Scanner thoracique de routine…")
        self.title_input.returnPressed.connect(self.save_case)
        details_form.addRow("Titre (obligatoire) :", self.title_input)
        self.description_input = CaseTextEdit()
        self.description_input.setPlaceholderText("Notes, observations clés…  (Entrée = enregistrer, Maj+Entrée = nouvelle ligne)")
        self.description_input.setFixedHeight(120)
        self.description_input.submitted.connect(self.save_case)
        details_form.addRow("Description :", self.description_input)
        right.addWidget(details)

        units_box = QGroupBox("Classification (unités)")
        units_layout = QVBoxLayout(units_box)
        self.units_scroll = QScrollArea()
        self.units_scroll.setWidgetResizable(True)
        self.units_container = QWidget()
        self.units_layout = QGridLayout(self.units_container)
        self.units_scroll.setWidget(self.units_container)
        self.units_scroll.setMinimumHeight(160)
        units_layout.addWidget(self.units_scroll)
        right.addWidget(units_box)

        save_btn = QPushButton("💾 ENREGISTRER LE CAS")
        save_btn.setStyleSheet("padding: 10px; background: #2e7d32; color: white; font-weight: bold;")
        save_btn.clicked.connect(self.save_case)
        self.save_case_btn = save_btn
        right.addWidget(save_btn)

        clear_btn = QPushButton("❌ Effacer le formulaire")
        clear_btn.clicked.connect(self.clear_form)
        right.addWidget(clear_btn)

        layout.addLayout(right, 2)

        self.reload_units()
        return page

    def _unit_entries(self) -> list:
        """Liste (emoji, nom) des unités du vocabulaire partagé."""
        return [(u.get("emoji", ""), u["name"]) for u in bootstrap.load_units(self.settings.shared_root)]

    def _make_visibility_row(self):
        """Ligne de boutons radio « Public / Privé » (choix exclusif)."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Visibilité :"))
        buttons = {}
        group = QButtonGroup(container)
        group.setExclusive(True)
        choices = (
            (storage.VISIBILITY_PUBLIC, "👥 Public",
             "Visible par tous dans la recherche."),
            (storage.VISIBILITY_PRIVATE, "👤 Privé",
             "N'apparaît dans la recherche que pour son auteur."),
        )
        for value, label, tip in choices:
            radio = QRadioButton(label)
            radio.setToolTip(tip)
            radio.setProperty("visibility", value)
            buttons[value] = radio
            group.addButton(radio)
            row.addWidget(radio)
            if value == storage.VISIBILITY_PUBLIC:
                radio.setChecked(True)
        row.addStretch()
        return container, buttons

    @staticmethod
    def _radio_value(buttons: dict) -> str:
        """Valeur du bouton radio coché (défaut : public)."""
        for value, radio in buttons.items():
            if radio.isChecked():
                return value
        return storage.VISIBILITY_PUBLIC

    def selected_visibility(self) -> str:
        return self._radio_value(self.visibility_buttons)

    def reset_visibility(self) -> None:
        """Ramène le choix de visibilité à « Public » (défaut)."""
        radio = self.visibility_buttons.get(storage.VISIBILITY_PUBLIC)
        if radio is not None:
            radio.setChecked(True)

    def _make_case_type_row(self):
        """Ligne de boutons radio pour le type de cas (choix exclusif)."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Type de cas :"))
        buttons = {}
        group = QButtonGroup(container)
        group.setExclusive(True)
        for case_type in bootstrap.CASE_TYPES:
            radio = QRadioButton(f"{case_type['emoji']}  {case_type['name']}")
            buttons[case_type["name"]] = radio
            group.addButton(radio)
            row.addWidget(radio)
            if case_type["name"] == bootstrap.DEFAULT_CASE_TYPE:
                radio.setChecked(True)
        row.addStretch()
        return container, buttons

    def selected_case_type(self) -> str:
        for name, radio in self.case_type_buttons.items():
            if radio.isChecked():
                return name
        return bootstrap.DEFAULT_CASE_TYPE

    def reset_case_type(self) -> None:
        radio = self.case_type_buttons.get(bootstrap.DEFAULT_CASE_TYPE)
        if radio is not None:
            radio.setChecked(True)

    def reload_units(self) -> None:
        """Recharge les unités (2 colonnes) et restaure la dernière sélection mémorisée."""
        while self.units_layout.count():
            item = self.units_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        remembered_set = set(self.settings.last_units or [])
        self.unit_checkboxes: list[QCheckBox] = []
        entries = self._unit_entries()
        for i, (emoji, name) in enumerate(entries):
            label = f"{emoji}  {name}" if emoji else name
            cb = QCheckBox(label)
            cb.setProperty("unit_name", name)
            cb.setChecked(name in remembered_set)
            cb.toggled.connect(self._remember_units)
            self.unit_checkboxes.append(cb)
            self.units_layout.addWidget(cb, i // 2, i % 2)
        self.units_layout.setRowStretch((len(entries) + 1) // 2, 1)

    def _remember_units(self) -> None:
        """Mémorise la sélection d'unités courante dans les réglages locaux."""
        self.settings.last_units = self.selected_units()
        config.save_settings(self.settings)

    def _display_image(self, image: QImage) -> None:
        """Affiche une image dans l'aperçu et la mémorise comme image courante."""
        self.current_image = image
        pixmap = QPixmap.fromImage(image).scaled(
            PREVIEW_W, PREVIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.preview.setPixmap(pixmap)

    def paste_image(self) -> None:
        """Récupère une image depuis le presse-papiers et l'affiche (action explicite)."""
        clipboard = QApplication.clipboard()
        image = clipboard.image()
        if image.isNull():
            QMessageBox.warning(
                self, "Presse-papiers vide",
                "Aucune image n'a été trouvée dans le presse-papiers.",
            )
            return
        self._display_image(image)
        self.clipboard_hint.setText("")

    def clear_image(self) -> None:
        self.current_image = None
        self.preview.clear()
        self.preview.setText("Aucune image.\n⚠️ Une image est obligatoire pour enregistrer.\nL'image du presse-papiers est importée à l'activation,\nou cliquez sur « Coller l'image du presse-papiers ».")
        self.clipboard_hint.setText("")

    def selected_units(self) -> list[str]:
        return [cb.property("unit_name") for cb in self.unit_checkboxes if cb.isChecked()]

    def _units_summary(self, units: list) -> str:
        """Résume les unités sélectionnées, avec leur emoji."""
        emoji_map = {u["name"]: u.get("emoji", "") for u in bootstrap.load_units(self.settings.shared_root)}
        return "\n".join((f"{emoji_map.get(u, '')} {u}").strip() for u in units)

    def save_case(self) -> None:
        if self._save_thread is not None:
            self._toast.show_message("⏳ Un enregistrement est déjà en cours…")
            return
        title = self.title_input.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Le titre du cas est obligatoire.")
            return
        units = self.selected_units()
        if not units:
            QMessageBox.warning(
                self, "Validation", "Sélectionnez au moins une unité.",
            )
            return
        if self.current_image is None:
            QMessageBox.warning(
                self, "Validation",
                "Une image est obligatoire : copiez une image dans le presse-papiers "
                "(ou cliquez sur « Coller l'image du presse-papiers ») avant d'enregistrer.",
            )
            return

        description = self.description_input.toPlainText()
        units_text = self._units_summary(units)
        case_type = self.selected_case_type()
        visibility = self.selected_visibility()
        dialog = ConfirmCaseDialog(
            self, title=title, description=description,
            units_text=units_text, visibility=visibility, case_type=case_type,
            image=self.current_image,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        payload = dict(
            title=title,
            units=units,
            author=self.settings.user_name,
            description=description,
            image=self.current_image.copy(),
            shared_root=self.settings.shared_root,
            case_type=case_type,
            visibility=visibility,
            ocr=getattr(self.settings, "ocr_enabled", True) and ocr.is_supported(),
        )
        self._start_save(SaveCaseWorker(payload), kind="create")

    def clear_case_fields(self) -> None:
        """Vide titre/description/image (les unités mémorisées sont conservées)."""
        self.title_input.clear()
        self.description_input.clear()
        self.clear_image()

    def clear_form(self) -> None:
        """Vide tout le formulaire, y compris les coches d'unités mémorisées.

        Demande confirmation, car l'action réinitialise aussi la sélection
        d'unités mémorisée localement.
        """
        answer = QMessageBox.question(
            self,
            "Effacer le formulaire",
            "Effacer tout le formulaire ?\n\n"
            "Titre, description et image seront vidés, et la sélection d'unités "
            "mémorisée sera réinitialisée.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.clear_case_fields()
        for cb in getattr(self, "unit_checkboxes", []):
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.reset_case_type()
        self.reset_visibility()
        self.settings.last_units = []
        config.save_settings(self.settings)

    # --- Onglet « Édition » (cas existant) -------------------------------- #
    def start_edit(self, case: storage.Case) -> None:
        """Ouvre l'onglet d'édition pré-rempli avec le cas donné."""
        if self.edit_page is None:
            self._build_edit_page()
        self._editing_case = case
        self._fill_edit_form(case)
        if self.tabs.indexOf(self.edit_page) == -1:
            self.tabs.addTab(self.edit_page, "✏️ Édition")
        self.tabs.setCurrentWidget(self.edit_page)

    def _build_edit_page(self) -> None:
        page = QWidget()
        page.setObjectName("editPage")
        page.setAttribute(Qt.WA_StyledBackground, True)
        # Indicateur visuel : fond chaud sombre, discret (sans agressivité).
        page.setStyleSheet("#editPage { background-color: #3b2f2f; }")

        outer = QVBoxLayout(page)
        header = QLabel("✏️  Édition d'un cas existant")
        header.setStyleSheet("color: #e6cccc; font-weight: bold; font-size: 14px;")
        outer.addWidget(header)

        content = QHBoxLayout()
        outer.addLayout(content, 1)

        # Colonne gauche : aperçu de l'image.
        left = QVBoxLayout()
        left.addWidget(QLabel("<h3>Image du cas (obligatoire)</h3>"))
        self.edit_preview = QLabel("Aucune image.")
        self.edit_preview.setAlignment(Qt.AlignCenter)
        self.edit_preview.setAttribute(Qt.WA_StyledBackground, True)
        self.edit_preview.setStyleSheet("background-color: black; color: #777;")
        self.edit_preview.setFixedSize(PREVIEW_W, PREVIEW_H)
        left.addWidget(self.edit_preview)

        paste_btn = QPushButton("📋 Coller l'image du presse-papiers")
        paste_btn.clicked.connect(self.edit_paste_image)
        left.addWidget(paste_btn)

        clear_img_btn = QPushButton("Retirer l'image")
        clear_img_btn.clicked.connect(self.edit_clear_image)
        left.addWidget(clear_img_btn)

        visibility_row, self.edit_visibility_buttons = self._make_visibility_row()
        left.addWidget(visibility_row)

        left.addStretch()
        content.addLayout(left, 1)

        # Colonne droite : formulaire.
        right = QVBoxLayout()

        details = QGroupBox("Détails du cas")
        details_form = QFormLayout(details)
        details_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.edit_title_input = QLineEdit()
        self.edit_title_input.returnPressed.connect(self.save_edit)
        details_form.addRow("Titre (obligatoire) :", self.edit_title_input)

        # Numéro de demande : prérempli avec la valeur OCR (ou une correction
        # précédente). Laisser vide et ne pas y toucher conserve/déduit la
        # valeur ; une saisie explicite, même vide, fait foi.
        self.edit_request_input = QLineEdit()
        self.edit_request_input.setPlaceholderText(
            "Détecté automatiquement après OCR (modifiable)"
        )
        self.edit_request_input.textEdited.connect(self._on_edit_request_edited)
        self._edit_request_edited = False
        details_form.addRow("N° de demande :", self.edit_request_input)

        self.edit_description_input = CaseTextEdit()
        self.edit_description_input.submitted.connect(self.save_edit)
        self.edit_description_input.setFixedHeight(120)
        details_form.addRow("Description :", self.edit_description_input)
        right.addWidget(details)

        units_box = QGroupBox("Classification (unités)")
        units_layout = QVBoxLayout(units_box)
        self.edit_units_scroll = QScrollArea()
        self.edit_units_scroll.setWidgetResizable(True)
        self.edit_units_container = QWidget()
        self.edit_units_layout = QGridLayout(self.edit_units_container)
        self.edit_units_scroll.setWidget(self.edit_units_container)
        self.edit_units_scroll.setMinimumHeight(140)
        units_layout.addWidget(self.edit_units_scroll)
        type_row, self.edit_case_type_buttons = self._make_case_type_row()
        units_layout.addWidget(type_row)
        right.addWidget(units_box)

        save_btn = QPushButton("💾 ENREGISTRER LES MODIFICATIONS")
        save_btn.setStyleSheet("padding: 10px; background: #7a4040; color: white; font-weight: bold;")
        save_btn.clicked.connect(self.save_edit)
        self.save_edit_btn = save_btn
        right.addWidget(save_btn)

        cancel_btn = QPushButton("↩️ Annuler l'édition")
        cancel_btn.clicked.connect(self.cancel_edit)
        right.addWidget(cancel_btn)

        content.addLayout(right, 2)
        self.edit_page = page

    def _fill_edit_form(self, case: storage.Case) -> None:
        self.edit_title_input.setText(case.title)
        self.edit_request_input.setText(case.request_number)
        self._edit_request_edited = False
        self.edit_description_input.setPlainText(case.description)
        self._reload_edit_units(case.units)
        self._set_edit_case_type(case.case_type)
        self._set_edit_visibility(case.visibility)
        self.edit_image_source = case.image_path if case.image else None
        self.edit_image_changed = False
        self._edit_update_preview()

    def _on_edit_request_edited(self, _text: str) -> None:
        """Mémorise qu'une correction manuelle du numéro a été saisie."""
        self._edit_request_edited = True

    def _set_edit_case_type(self, name: str) -> None:
        radio = self.edit_case_type_buttons.get(name) or self.edit_case_type_buttons.get(bootstrap.DEFAULT_CASE_TYPE)
        if radio is not None:
            radio.setChecked(True)

    def selected_edit_case_type(self) -> str:
        for name, radio in self.edit_case_type_buttons.items():
            if radio.isChecked():
                return name
        return bootstrap.DEFAULT_CASE_TYPE

    def _set_edit_visibility(self, value: str) -> None:
        radio = (
            self.edit_visibility_buttons.get(value)
            or self.edit_visibility_buttons.get(storage.VISIBILITY_PUBLIC)
        )
        if radio is not None:
            radio.setChecked(True)

    def selected_edit_visibility(self) -> str:
        return self._radio_value(self.edit_visibility_buttons)

    def _reload_edit_units(self, selected: list) -> None:
        while self.edit_units_layout.count():
            widget = self.edit_units_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        selected_set = set(selected or [])
        self.edit_unit_checkboxes = []
        entries = self._unit_entries()
        for i, (emoji, name) in enumerate(entries):
            label = f"{emoji}  {name}" if emoji else name
            cb = QCheckBox(label)
            cb.setProperty("unit_name", name)
            cb.setChecked(name in selected_set)
            self.edit_unit_checkboxes.append(cb)
            self.edit_units_layout.addWidget(cb, i // 2, i % 2)
        self.edit_units_layout.setRowStretch((len(entries) + 1) // 2, 1)

    def _edit_update_preview(self) -> None:
        source = self.edit_image_source
        if source is None:
            self.edit_preview.clear()
            self.edit_preview.setText("Aucune image.")
            return
        if isinstance(source, QImage):
            pixmap = QPixmap.fromImage(source)
        else:
            pixmap = QPixmap(str(source))
        if pixmap.isNull():
            self.edit_preview.clear()
            return
        self.edit_preview.setPixmap(
            pixmap.scaled(PREVIEW_W, PREVIEW_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def edit_paste_image(self) -> None:
        image = QApplication.clipboard().image()
        if image.isNull():
            QMessageBox.warning(self, "Presse-papiers vide",
                                "Aucune image n'a été trouvée dans le presse-papiers.")
            return
        self.edit_image_source = image
        self.edit_image_changed = True
        # Le numéro de l'ancienne image n'est plus valable : on repart de l'OCR
        # de la nouvelle, sauf si l'utilisateur en saisit un juste après.
        self.edit_request_input.clear()
        self._edit_request_edited = False
        self._edit_update_preview()

    def edit_clear_image(self) -> None:
        self.edit_image_source = None
        self.edit_image_changed = True
        self.edit_request_input.clear()
        self._edit_request_edited = False
        self._edit_update_preview()

    def selected_edit_units(self) -> list[str]:
        return [cb.property("unit_name") for cb in getattr(self, "edit_unit_checkboxes", []) if cb.isChecked()]

    def _edit_has_image(self, image_arg) -> bool:
        """Vrai si le cas édité aura une image après enregistrement.

        ``KEEP_IMAGE`` → on se base sur l'image existante du cas ; ``None`` → image
        retirée ; toute autre valeur (QImage) → nouvelle image fournie.
        """
        if image_arg is storage.KEEP_IMAGE:
            return bool(self._editing_case and self._editing_case.image)
        return image_arg is not None

    def save_edit(self) -> None:
        if self._editing_case is None:
            return
        if self._save_thread is not None:
            self._toast.show_message("⏳ Un enregistrement est déjà en cours…")
            return
        title = self.edit_title_input.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Le titre du cas est obligatoire.")
            return
        units = self.selected_edit_units()
        if not units:
            QMessageBox.warning(self, "Validation", "Sélectionnez au moins une unité.")
            return

        image_arg = self.edit_image_source if self.edit_image_changed else storage.KEEP_IMAGE
        if not self._edit_has_image(image_arg):
            QMessageBox.warning(
                self, "Validation",
                "Une image est obligatoire : copiez une image dans le presse-papiers "
                "(ou cliquez sur « Coller l'image du presse-papiers ») avant d'enregistrer.",
            )
            return
        description = self.edit_description_input.toPlainText()
        units_text = self._units_summary(units)
        case_type = self.selected_edit_case_type()
        visibility = self.selected_edit_visibility()
        dialog = ConfirmCaseDialog(
            self, title=title, description=description,
            units_text=units_text, visibility=visibility, case_type=case_type,
            image=self._edit_image_qimage(), heading="Confirmer les modifications",
        )
        if dialog.exec() != QDialog.Accepted:
            return
        payload = dict(
            case=self._editing_case,
            title=title,
            units=units,
            author=self.settings.user_name,
            description=description,
            image=image_arg,
            shared_root=self.settings.shared_root,
            case_type=case_type,
            visibility=visibility,
            ocr=getattr(self.settings, "ocr_enabled", True) and ocr.is_supported(),
            request_number=(
                self.edit_request_input.text().strip()
                if self._edit_request_edited
                else None
            ),
        )
        self._start_save(UpdateCaseWorker(payload), kind="edit")

    def _edit_image_qimage(self) -> "QImage | None":
        """Image du cas en édition sous forme de QImage (pour la confirmation)."""
        source = self.edit_image_source
        if source is None:
            return None
        if isinstance(source, QImage):
            return source
        image = QImage(str(source))
        return None if image.isNull() else image

    def cancel_edit(self) -> None:
        self._close_edit_tab()

    def _close_edit_tab(self) -> None:
        index = self.tabs.indexOf(self.edit_page)
        if index != -1:
            self.tabs.removeTab(index)
        self._editing_case = None
        self.tabs.setCurrentIndex(self.consult_tab_index)

    # --- Enregistrement en tâche de fond ---------------------------------- #
    def _start_save(self, worker: QObject, *, kind: str) -> None:
        """Lance ``worker`` dans un thread et désactive le formulaire concerné.

        L'OCR natif Windows fait partie de l'appel ``storage.create_case`` /
        ``storage.update_case`` : il s'exécute donc dans le thread de travail,
        jamais dans le thread de l'interface. Le cas reste atomique : il
        n'apparaît qu'une fois l'image, l'OCR et ``case.md`` écrits.
        """
        if self._save_thread is not None:
            self._toast.show_message("⏳ Un enregistrement est déjà en cours…")
            return

        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_save_finished)
        worker.failed.connect(self._on_save_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_save_state)
        self._save_thread = thread
        self._save_worker = worker
        self._save_kind = kind

        page = self.edit_page if kind == "edit" else self.new_case_page
        if page is not None:
            page.setEnabled(False)
        self._toast.show_message("⏳ Enregistrement et lecture OCR…")
        thread.start()

    def _on_save_finished(self, case: storage.Case) -> None:
        """Sauvegarde réussie : met à jour l'écran et le formulaire."""
        if self._save_kind == "edit":
            self._close_edit_tab()
            self._toast.show_message(f"✅ Cas « {case.title} » modifié.")
            if self.results_list.count():
                self.run_search()
            return

        self.clear_case_fields()
        self.reset_case_type()   # le type revient à « Standard » pour le prochain cas
        self.reset_visibility()  # la visibilité revient à « Public »
        suffix = ""
        if case.ocr_status in (
            ocr.STATUS_FAILED, ocr.STATUS_TIMEOUT, ocr.STATUS_UNSUPPORTED,
        ):
            suffix = " (texte OCR non extrait)"
        self._toast.show_message(f"✅ Cas « {case.title} » enregistré.{suffix}")

    def _on_save_failed(self, message: str) -> None:
        """Sauvegarde en échec : la saisie reste intacte."""
        QMessageBox.critical(self, "Échec de l'enregistrement", message)

    def _clear_save_state(self) -> None:
        """Réactive les formulaires et honore un arrêt différé."""
        self._save_thread = None
        self._save_worker = None
        self._save_kind = ""
        if getattr(self, "new_case_page", None) is not None:
            self.new_case_page.setEnabled(True)
        if self.edit_page is not None:
            self.edit_page.setEnabled(True)
        if self._quit_after_save:
            self._quit_after_save = False
            self.quit_app()

    # --- Onglet « Consulter » --------------------------------------------- #
    def _build_consult_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        search_box = QGroupBox("Recherche")
        grid = QGridLayout(search_box)

        def labeled(label_text: str, widget: QWidget) -> QWidget:
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label_text))
            row.addWidget(widget, 1)
            return box

        self.search_text = QLineEdit()
        self.search_text.setPlaceholderText(
            "Texte dans le titre / description / n° de demande"
        )
        self.search_text.returnPressed.connect(self.run_search)

        self.search_author = QLineEdit()
        self.search_author.setPlaceholderText("Auteur…")
        self.search_author.returnPressed.connect(self.run_search)

        # --- Collection : publique, ma collection privée, ou les deux ---
        self.search_visibility = QComboBox()
        self.search_visibility.setToolTip("Restreindre la recherche à une collection.")
        self.search_visibility.addItem("👥 Publique", storage.VISIBILITY_PUBLIC)
        self.search_visibility.addItem("👤 Privé", storage.VISIBILITY_PRIVATE)
        self.search_visibility.addItem("👥/👤 Public + Privé", None)
        self.search_visibility.currentIndexChanged.connect(self._maybe_search_visibility)

        # --- Unités : bouton qui ouvre la liste multi-choix en popup ---
        self.units_button = QPushButton()
        self.units_button.clicked.connect(self._open_units_popup)
        self.filter_unit_checkboxes: list[QCheckBox] = []

        self.units_popup = self._make_popup()
        units_popup_layout = QVBoxLayout(self.units_popup)
        units_popup_layout.setContentsMargins(8, 8, 8, 8)
        self.filter_units_container = QWidget()
        self.filter_units_layout = QGridLayout(self.filter_units_container)
        units_popup_layout.addWidget(self.filter_units_container)
        self.toggle_units_btn = QPushButton("☑️ Tout cocher / décocher")
        self.toggle_units_btn.clicked.connect(self.toggle_all_filter_units)
        units_popup_layout.addWidget(self.toggle_units_btn)

        # --- Période : bouton qui ouvre les bornes en popup ---
        self.period_button = QPushButton()
        self.period_button.clicked.connect(self._open_period_popup)

        self.period_popup = self._make_popup()
        period_popup_layout = QVBoxLayout(self.period_popup)
        period_popup_layout.setContentsMargins(8, 8, 8, 8)
        self.period_buttons = {}
        self.period_group = QButtonGroup(self)
        self.period_group.setExclusive(True)
        periods = [
            "Aujourd'hui", "Hier", "3 derniers jours",
            "7 derniers jours", "30 derniers jours", "Cette semaine",
            "3 mois", "6 mois", "1 an",
            "2 ans", "5 ans", "Période libre",
        ]
        periods_grid = QGridLayout()
        for i, name in enumerate(periods):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, n=name: self._on_period_clicked(n))
            self.period_buttons[name] = btn
            self.period_group.addButton(btn)
            periods_grid.addWidget(btn, i // 3, i % 3)
        period_popup_layout.addLayout(periods_grid)

        # Dates « Période libre » : masquées par défaut.
        self.free_date_row = QWidget()
        free_date_layout = QHBoxLayout(self.free_date_row)
        free_date_layout.setContentsMargins(0, 0, 0, 0)
        self.date_from = QDateEdit(QDate.currentDate().addYears(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for widget in (self.date_from, self.date_to):
            widget.setCalendarPopup(True)
        self.date_from.dateChanged.connect(self._maybe_search_free)
        self.date_to.dateChanged.connect(self._maybe_search_free)
        free_date_layout.addWidget(QLabel("Du"))
        free_date_layout.addWidget(self.date_from)
        free_date_layout.addWidget(QLabel("au"))
        free_date_layout.addWidget(self.date_to)
        free_date_layout.addStretch()
        self.free_date_row.setVisible(False)

        self.default_period = "Cette semaine"
        self._select_period(self.default_period)

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        search_btn = QPushButton("🔍 Rechercher")
        search_btn.clicked.connect(self.run_search)
        buttons_layout.addWidget(search_btn)
        reset_btn = QPushButton("♻️ Réinitialiser")
        reset_btn.clicked.connect(self.reset_search)
        buttons_layout.addWidget(reset_btn)

        export_btn = QPushButton("📤 Exporter")
        export_btn.setToolTip("Exporter les résultats de la recherche en CSV")
        export_btn.clicked.connect(self.export_results)
        buttons_layout.addWidget(export_btn)
        buttons_layout.addStretch()

        # Ligne « Type de cas » (3 cases à cocher), sous Unités/Période.
        self.search_case_type_checkboxes = make_case_type_checkboxes()
        types_row = QWidget()
        types_layout = QHBoxLayout(types_row)
        types_layout.setContentsMargins(0, 0, 0, 0)
        types_layout.addWidget(QLabel("Type de cas :"))
        for cb in self.search_case_type_checkboxes:
            cb.toggled.connect(self._maybe_search_case_type)
            types_layout.addWidget(cb)
        types_layout.addStretch()

        # Texte sur toute la largeur ; puis Auteur + Collection ; puis Unités/Période.
        grid.addWidget(labeled("Texte :", self.search_text), 0, 0, 1, 2)
        grid.addWidget(labeled("Auteur :", self.search_author), 1, 0)
        grid.addWidget(labeled("Collection :", self.search_visibility), 1, 1)
        grid.addWidget(labeled("Unités :", self.units_button), 2, 0)
        grid.addWidget(labeled("Période :", self.period_button), 2, 1)
        grid.addWidget(types_row, 3, 0, 1, 2)
        grid.addWidget(buttons_row, 4, 0, 1, 2)
        grid.addWidget(self.free_date_row, 5, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Zone de filtres défilable : évite d'imposer une grande hauteur minimale
        # à la fenêtre (sinon impossible de la réduire verticalement sur petit écran).
        search_scroll = QScrollArea()
        search_scroll.setWidgetResizable(True)
        search_scroll.setFrameShape(QScrollArea.NoFrame)
        search_scroll.setWidget(search_box)

        # Détails du cas sélectionné + image, regroupés dans la moitié droite.
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.case_image = ImageFrame()

        right_top = QWidget()
        right_top_layout = QHBoxLayout(right_top)
        right_top_layout.setContentsMargins(0, 0, 0, 0)
        right_top_layout.setSpacing(0)
        self.detail.setFrameShape(QFrame.NoFrame)
        right_top_layout.addWidget(self.detail, 1)
        right_top_layout.addWidget(self.case_image, 1)  # image tout à droite

        # Bandeau supérieur : recherche (1/2) + détails&image (1/2).
        top = QHBoxLayout()
        top.addWidget(search_scroll, 1)
        top.addWidget(right_top, 1)
        layout.addLayout(top)

        self.results_list = QListWidget()
        self.results_list.currentItemChanged.connect(self.show_selected_case)
        layout.addWidget(QLabel("<h3>Résultats</h3>"))
        layout.addWidget(self.results_list, 1)  # occupe l'espace vertical restant

        # Suppression via la touche Suppr, uniquement quand la liste a le focus.
        delete_shortcut = QShortcut(QKeySequence.Delete, self.results_list)
        delete_shortcut.setContext(Qt.WidgetShortcut)
        delete_shortcut.activated.connect(self.delete_selected_case)

        # Menu contextuel (clic droit) sur la liste des résultats.
        self.results_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.results_list.customContextMenuRequested.connect(self.show_results_context_menu)

        self.reload_filter_units()
        self.reload_filter_authors()
        return page

    def reload_filter_units(self) -> None:
        """Recharge la liste multi-choix des unités (cases à cocher, 2 colonnes)."""
        while self.filter_units_layout.count():
            widget = self.filter_units_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.filter_unit_checkboxes = []
        units = bootstrap.load_units(self.settings.shared_root)
        for i, unit in enumerate(units):
            label = f"{unit['emoji']}  {unit['name']}" if unit.get("emoji") else unit["name"]
            cb = QCheckBox(label)
            cb.setProperty("unit_name", unit["name"])
            cb.toggled.connect(self._update_units_button_text)
            self.filter_unit_checkboxes.append(cb)
            self.filter_units_layout.addWidget(cb, i // 2, i % 2)
        self._update_units_button_text()

    def selected_filter_units(self) -> list[str]:
        """Unités cochées dans le filtre de recherche."""
        return [cb.property("unit_name") for cb in self.filter_unit_checkboxes if cb.isChecked()]

    def selected_search_visibility(self):
        """Collection visée : ``"public"``, ``"private"`` ou ``None`` (les deux)."""
        return self.search_visibility.currentData()

    def _maybe_search_visibility(self, _index: int) -> None:
        """Relance la recherche au changement de collection."""
        # Pendant la construction de l'onglet, la liste de résultats n'existe
        # pas encore : on ignore le signal initial.
        if hasattr(self, "results_list"):
            self.run_search()

    def selected_search_case_types(self) -> set:
        """Types de cas cochés dans les filtres de recherche."""
        return checked_case_types(getattr(self, "search_case_type_checkboxes", []))

    def _maybe_search_case_type(self) -> None:
        """Relance la recherche quand un type de cas est (dé)coché."""
        if hasattr(self, "results_list"):
            self.run_search()

    def toggle_all_filter_units(self) -> None:
        """Coche tout, ou décoche tout (selon l'état courant)."""
        boxes = self.filter_unit_checkboxes
        if not boxes:
            return
        target = not all(cb.isChecked() for cb in boxes)
        for cb in boxes:
            cb.setChecked(target)
        self._update_units_button_text()

    def reload_filter_authors(self) -> None:
        """Autocomplétion du champ Auteur à partir des auteurs existants."""
        authors = storage.all_authors(self.settings.shared_root, viewer=self.settings.user_name)
        completer = QCompleter(authors, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)          # « contient » le mot tapé
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.search_author.setCompleter(completer)

    def _active_period(self):
        for name, button in self.period_buttons.items():
            if button.isChecked():
                return name
        return None

    def _make_popup(self) -> QFrame:
        """Panneau flottant (se ferme au clic extérieur)."""
        popup = QFrame(self, Qt.Popup | Qt.FramelessWindowHint)
        popup.setFrameShape(QFrame.StyledPanel)
        popup.setMinimumWidth(260)
        return popup

    def _open_units_popup(self) -> None:
        self.units_popup.move(
            self.units_button.mapToGlobal(QPoint(0, self.units_button.height()))
        )
        self.units_popup.show()

    def _open_period_popup(self) -> None:
        self.period_popup.move(
            self.period_button.mapToGlobal(QPoint(0, self.period_button.height()))
        )
        self.period_popup.show()

    def _update_units_button_text(self) -> None:
        names = self.selected_filter_units()
        if not names:
            text = "Toutes"
        elif len(names) <= 3:
            text = ", ".join(names)
        else:
            text = f"{len(names)} sélectionnées"
        self.units_button.setText(f"{text}  ▾")

    def _update_period_button_text(self) -> None:
        self.period_button.setText(f"{self._active_period() or 'Toutes'}  ▾")

    def _maybe_search_free(self) -> None:
        if self._active_period() == "Période libre":
            self.run_search()

    def _select_period(self, name: str) -> None:
        """Sélectionne une période (sans lancer la recherche)."""
        button = self.period_buttons.get(name)
        if button is not None:
            button.setChecked(True)
        free = name == "Période libre"
        self.free_date_row.setVisible(free)
        self.date_from.setEnabled(free)
        self.date_to.setEnabled(free)
        self._update_period_button_text()

    def _on_period_clicked(self, name: str) -> None:
        self._select_period(name)
        self.period_popup.hide()
        if name != "Période libre":
            self.run_search()  # les bornes rapides lancent directement la recherche

    @staticmethod
    def _shift_months(moment, months: int):
        import calendar

        month_index = moment.month - 1 - months
        year = moment.year + month_index // 12
        month = month_index % 12 + 1
        day = min(moment.day, calendar.monthrange(year, month)[1])
        return moment.replace(year=year, month=month, day=day)

    def _period_range(self, name: str):
        from datetime import datetime, timedelta

        now = datetime.now()
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if name == "Aujourd'hui":
            return start_today, end
        if name == "Hier":
            yesterday = start_today - timedelta(days=1)
            return yesterday, yesterday.replace(hour=23, minute=59, second=59)
        if name in ("3 derniers jours", "7 derniers jours", "30 derniers jours"):
            days = {"3 derniers jours": 3, "7 derniers jours": 7, "30 derniers jours": 30}[name]
            return start_today - timedelta(days=days - 1), end
        if name == "Cette semaine":
            monday = start_today - timedelta(days=start_today.weekday())
            return monday, end
        months = {"3 mois": 3, "6 mois": 6, "1 an": 12, "2 ans": 24, "5 ans": 60}.get(name)
        if months is not None:
            return self._shift_months(start_today, months), end
        return None, None

    def run_search(self) -> None:
        from datetime import datetime

        date_from = date_to = None
        period = self._active_period()
        if period == "Période libre":
            date_from = datetime(
                self.date_from.date().year(), self.date_from.date().month(), self.date_from.date().day()
            )
            date_to = datetime(
                self.date_to.date().year(), self.date_to.date().month(),
                self.date_to.date().day(), 23, 59, 59
            )
        elif period:
            date_from, date_to = self._period_range(period)

        units = self.selected_filter_units()
        try:
            cases = storage.search_cases(
                self.settings.shared_root,
                text=self.search_text.text(),
                author=self.search_author.text(),
                units=units or None,
                date_from=date_from,
                date_to=date_to,
                viewer=self.settings.user_name,
                visibility=self.selected_search_visibility(),
                case_types=self.selected_search_case_types(),
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Erreur de recherche", str(exc))
            return

        self.results_list.clear()
        self.detail.clear()
        self.case_image.clear_source()
        self._selected_case = None
        self._results = {}
        emoji_map = {u["name"]: u.get("emoji", "") for u in bootstrap.load_units(self.settings.shared_root)}
        for case in cases:
            label = f"{case.title} – {case.date_added}  ({case.author})"
            if case.request_number:
                label += f"  ·  N° {case.request_number}"
            if case.visibility == storage.VISIBILITY_PRIVATE:
                label = "👤 " + label
            type_txt = (
                f"{bootstrap.CASE_TYPE_EMOJI.get(case.case_type, '')} {case.case_type}"
            ).strip()
            label += f"  [{type_txt}]"
            if case.units:
                units_txt = ", ".join(
                    (f"{emoji_map.get(u, '')} {u}").strip() for u in case.units
                )
                label += f"  [{units_txt}]"
            item = QListWidgetItem(label)
            self.results_list.addItem(item)
            self._results[id(item)] = case

    def reset_search(self) -> None:
        """Réinitialise tous les filtres de recherche et vide les résultats."""
        self.search_text.clear()
        self.search_author.clear()
        for cb in self.filter_unit_checkboxes:
            cb.setChecked(False)
        self.search_visibility.blockSignals(True)  # évite une recherche intermédiaire
        self.search_visibility.setCurrentIndex(0)  # « Publique »
        self.search_visibility.blockSignals(False)
        for cb in getattr(self, "search_case_type_checkboxes", []):
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self._select_period(self.default_period)
        self.run_search()  # revient à la vue par défaut (« Cette semaine », publique)

    def export_results(self) -> None:
        """Exporte les résultats affichés dans un fichier CSV."""
        cases = [
            self._results[id(self.results_list.item(i))]
            for i in range(self.results_list.count())
            if id(self.results_list.item(i)) in self._results
        ]
        if not cases:
            QMessageBox.information(self, "Export", "Aucun résultat à exporter.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Exporter les résultats", "resultats_recherche.csv", "Fichiers CSV (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        header = ["Titre", "N° de demande", "Type", "Visibilité", "Date d'ajout",
                  "Date de modification", "Auteur", "Unités", "Image", "Dossier",
                  "Description"]
        try:
            # utf-8-sig : accents corrects dans Excel ; ';' : séparateur Excel FR.
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(header)
                for case in cases:
                    writer.writerow([
                        case.title,
                        case.request_number,
                        case.case_type,
                        "Privé" if case.visibility == storage.VISIBILITY_PRIVATE else "Public",
                        case.date_added,
                        case.date_modified or "",
                        case.author,
                        ", ".join(case.units),
                        case.image or "",
                        str(case.folder or ""),
                        case.description,
                    ])
        except OSError as exc:
            QMessageBox.critical(self, "Échec de l'export", str(exc))
            return

        self._toast.show_message(f"✅ {len(cases)} cas exportés.")

    def show_selected_case(self, current: QListWidgetItem | None) -> None:
        if current is None:
            self.detail.clear()
            self.case_image.clear_source()
            self._selected_case = None
            return
        case = self._results.get(id(current))
        if case is None:
            return
        self._selected_case = case
        description = case.description.strip()
        title = case.title
        if case.request_number:
            title = f"{title} ({case.request_number})"
        self.detail.setPlainText(f"{title}\n\n{description}" if description else title)
        self.case_image.set_source(case.image_path)

    def show_results_context_menu(self, pos) -> None:
        """Menu contextuel (clic droit) sur la liste des résultats."""
        item = self.results_list.itemAt(pos)
        if item is None:
            return
        self.results_list.setCurrentItem(item)  # cible = le cas cliqué

        menu = QMenu(self)
        edit_action = menu.addAction("✏️  Modifier le cas")
        edit_action.triggered.connect(self.edit_selected_case)
        menu.addSeparator()
        delete_action = menu.addAction("🗑  Supprimer le cas")
        delete_action.triggered.connect(self.delete_selected_case)
        menu.exec(self.results_list.viewport().mapToGlobal(pos))

    def edit_selected_case(self) -> None:
        """Ouvre l'onglet d'édition pour le cas sélectionné."""
        item = self.results_list.currentItem()
        if item is None:
            return
        case = self._results.get(id(item))
        if case is None:
            return
        self.start_edit(case)

    def delete_selected_case(self) -> None:
        """Supprime le cas sélectionné (après confirmation), via le bac à sable."""
        item = self.results_list.currentItem()
        if item is None:
            return
        case = self._results.get(id(item))
        if case is None:
            return

        reply = QMessageBox.question(
            self,
            "Supprimer le cas",
            f"Supprimer définitivement le cas « {case.title} » ?\n\n"
            "Le dossier sera envoyé dans la corbeille de l'ordinateur.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            ok = storage.delete_case(case)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Échec de la suppression", str(exc))
            return

        if not ok:
            QMessageBox.warning(self, "Suppression", "Le cas n'a pas pu être supprimé (dossier introuvable ?).")
            return

        self.detail.clear()
        self.run_search()  # rafraîchit la liste avec les mêmes critères

    # --- Réglages ---------------------------------------------------------- #
    # ------------------------------------------------------------------ #
    # Zone de notification (mode « barre d'état système »)
    # ------------------------------------------------------------------ #
    def _build_tray(self):
        """Installe l'icône de la zone de notification, si disponible.

        ``None`` quand le système n'en propose pas (ou environnement de test sans
        affichage) : la fermeture quitte alors l'application, comportement
        historique.
        """
        if not tray.TrayIcon.available():
            return None
        tray_icon = tray.TrayIcon(
            _app_icon(),
            self,
            on_open=self.restore_from_tray,
            on_settings=self._open_settings_from_tray,
            on_quit=self.quit_app,
            on_message_clicked=self._open_feed_from_toast,
        )
        tray_icon.show()
        return tray_icon

    def closeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        """Fermer la fenêtre **réduit** l'application dans la zone de notification.

        L'application continue de tourner : elle reste accessible par son icône
        (ou par un nouveau lancement de l'exécutable, qui réveille cette fenêtre
        via le verrou mono-instance). L'arrêt réel passe par « Quitter ».
        """
        if self._quitting or self._tray is None or not self.settings.close_to_tray:
            if self._save_thread is not None and not self._quitting:
                # Ne pas détruire un QThread en cours d'enregistrement : on
                # diffère l'arrêt jusqu'à la fin de la sauvegarde (quelques
                # secondes au plus avec l'OCR).
                event.ignore()
                self._quit_after_save = True
                self._toast.show_message(
                    "⏳ Enregistrement en cours : l'arrêt suivra dès qu'il sera terminé…"
                )
                return
            event.accept()
            self.quit_app()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            # Une seule fois par session : sinon la bulle devient vite pénible.
            self._tray_hint_shown = True
            self._tray.notify(
                "RadCase Manager continue de tourner",
                "L'application est réduite dans la zone de notification : cliquez "
                "sur son icône pour la rouvrir, ou choisissez « Quitter » pour "
                "l'arrêter vraiment.",
            )

    def can_reduce_at_startup(self) -> bool:
        """Vrai si l'application peut démarrer **sans afficher** sa fenêtre.

        Uniquement si la zone de notification est disponible : sinon la fenêtre
        n'aurait aucun moyen d'être rouverte (voir ``main``).
        """
        return self._tray is not None

    def restore_from_tray(self) -> None:
        """Ramène la fenêtre au premier plan (icône, clic, second lancement)."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def _open_settings_from_tray(self) -> None:
        """Ouvre les réglages depuis l'icône : la fenêtre revient d'abord."""
        self.restore_from_tray()
        self.open_settings()

    def quit_app(self) -> None:
        """Arrête réellement l'application (menu « Fichier » ou icône → Quitter).

        Le canal mono-instance est libéré **avant** de quitter : c'est ce qui
        permet à un lancement immédiat (ou à la relance qui suit une mise à jour,
        voir ``updater._relaunch``) de démarrer sans se croire seconde instance.
        """
        # Un enregistrement peut être en cours (OCR compris) : on ne détruit pas
        # le thread de travail. L'arrêt est rejoué par ``_clear_save_state``.
        if self._save_thread is not None:
            self._quit_after_save = True
            self._toast.show_message(
                "⏳ Enregistrement en cours : l'arrêt suivra dès qu'il sera terminé…"
            )
            return

        self._quitting = True
        if self._tray is not None:
            self._tray.hide()
        self._release_single_instance()
        ocr.shutdown()
        QApplication.quit()

    def _release_single_instance(self) -> None:
        if self._single_instance is not None:
            self._single_instance.release()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            self.settings = dialog.result_settings()
            config.save_settings(self.settings)
            # Le réglage de démarrage automatique se reflète dans le registre :
            # c'est ici qu'on l'applique, une fois le dialogue validé.
            autostart.sync(self.settings.start_with_windows)
            bootstrap.ensure_shared_structure(self.settings.shared_root)
            _ensure_user_known(self.settings)
            self.reload_units()
            self.reload_filter_units()
            self.reload_filter_authors()
            self.feed_view.set_settings(self.settings)
            self.reload_feed_units()
            self.browser.set_settings(self.settings)
            self._update_status()

    # ------------------------------------------------------------------ #
    # Mise à jour automatique
    # ------------------------------------------------------------------ #
    def _auto_check_updates(self) -> None:
        """Vérification silencieuse au démarrage (au plus une fois par jour)."""
        if updater.pending_update() is not None:
            self._offer_pending_update()
            return
        if updater.should_auto_check(self.settings):
            self.check_updates(silent=True)

    def _retry_leftover_cleanup(self) -> None:
        """Retente de mettre à la corbeille les restes d'une mise à jour.

        Le premier passage a lieu au tout début de ``main()`` ; s'il échoue (fichier
        encore tenu par un antivirus), on réessaie à intervalles espacés.
        """
        if not updater.leftover_files():
            return
        updater.cleanup()
        if updater.leftover_files() and self._cleanup_delays:
            QTimer.singleShot(self._cleanup_delays.pop(0), self._retry_leftover_cleanup)

    def check_updates(self, silent: bool = False) -> None:
        """Cherche une version plus récente (menu « Aide » ou vérification auto).

        Les signaux du worker sont reliés à des **méthodes liées** de la fenêtre
        (jamais des lambdas) : c'est ce qui garantit leur exécution dans le thread
        de l'interface, seul autorisé à toucher aux widgets Qt.
        """
        if not updater.is_enabled():
            if not silent:
                QMessageBox.information(self, "Mises à jour", UPDATE_UNAVAILABLE)
            return
        if self._update_thread is not None:
            return  # une vérification est déjà en cours

        self._check_silent = silent
        thread = QThread(self)
        worker = UpdateCheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_check_done)
        worker.failed.connect(self._on_check_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_check_state)
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _clear_check_state(self) -> None:
        self._update_thread = None
        self._update_worker = None

    def _on_check_done(self, tag: str) -> None:
        updater.mark_checked(self.settings)         # anti-rebond : prochaine vérif demain
        updater.note_available(self.settings, tag)  # indicateur persistant
        config.save_settings(self.settings)
        self._refresh_update_indicator()
        if not tag:
            if not self._check_silent:
                QMessageBox.information(
                    self,
                    "Mises à jour",
                    "Vous utilisez la version la plus récente "
                    f"({updater.display_version()}).",
                )
            return
        # Une vérification demandée à la main propose toujours la mise à jour ;
        # la proposition automatique respecte le report du bouton « Plus tard ».
        if not self._check_silent or updater.should_prompt(self.settings, tag):
            self._prompt_update(tag)

    def _refresh_update_indicator(self) -> None:
        """Affiche (ou masque) « mise à jour disponible » dans la barre de statut."""
        tag = updater.available_update(self.settings)
        self.update_label.setVisible(bool(tag))
        if tag:
            self.update_label.setText(f"⬆  Mise à jour disponible ({tag})")
            self.update_label.setToolTip(
                f"La version {tag} est disponible — cliquez pour l'installer."
            )

    def _on_check_failed(self, message: str) -> None:
        if self._check_silent:
            return  # échec réseau silencieux : on réessaiera au prochain jour
        QMessageBox.warning(
            self, "Mises à jour", "Impossible de vérifier les mises à jour.\n\n" + message
        )

    def _prompt_update(self, tag: str) -> None:
        answer = QMessageBox.question(
            self,
            "Mise à jour disponible",
            f"Une nouvelle version est disponible : {tag}\n"
            f"Version installée : {updater.display_version()}\n\n"
            "La télécharger et l'installer maintenant ?\n"
            "(L'application redémarrera automatiquement.)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            # « Plus tard » : on ne reproposera pas avant un mois. L'indicateur en
            # bas à droite reste visible pour que l'utilisateur puisse agir avant.
            updater.snooze(self.settings, tag)
            config.save_settings(self.settings)
            return
        self._download_update(tag)

    def _download_update(self, tag: str) -> None:
        if self._download_thread is not None:
            return
        progress = QProgressDialog(f"Téléchargement de {tag}…", "Annuler", 0, 100, self)
        progress.setWindowTitle("Mise à jour")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        self._pending_tag = tag
        self._progress_dialog = progress
        thread = QThread(self)
        worker = UpdateDownloadWorker(tag, updater.staged_path())
        worker.moveToThread(thread)
        progress.canceled.connect(self._on_download_canceled)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_download_progress)
        worker.finished.connect(self._on_download_done)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_download_state)
        self._download_thread = thread
        self._download_worker = worker
        thread.start()
        progress.show()

    def _clear_download_state(self) -> None:
        self._download_thread = None
        self._download_worker = None
        self._progress_dialog = None

    def _on_download_canceled(self) -> None:
        """Annulation demandée : on lève le drapeau lu par le thread de travail."""
        if self._download_worker is not None:
            self._download_worker.cancel()

    def _on_download_progress(self, done: int, total: int) -> None:
        progress = self._progress_dialog
        if progress is None:
            return
        if total > 0:
            progress.setValue(min(100, done * 100 // total))
        else:
            progress.setLabelText(f"Téléchargement… {done // (1024 * 1024)} Mo")

    def _on_download_done(self) -> None:
        tag = self._pending_tag
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        QMessageBox.information(
            self,
            "Mise à jour prête",
            f"La version {tag} a été téléchargée.\n"
            "L'application va redémarrer pour l'installer.",
        )
        self._apply_update()

    def _on_download_failed(self, message: str) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
        if "annul" in message.lower():
            return
        QMessageBox.warning(self, "Mise à jour", "Le téléchargement a échoué.\n\n" + message)

    def _apply_update(self) -> None:
        """Remplace l'exécutable par la version téléchargée, puis relance."""
        # La relance démarre une nouvelle version pendant que celle-ci s'arrête :
        # on libère le canal mono-instance pour qu'elle prenne immédiatement la
        # main (sinon elle se croirait « seconde instance » et sortirait aussitôt).
        self._release_single_instance()
        try:
            updater.apply_update()
        except Exception as exc:  # noqa: BLE001 - remonté à l'utilisateur
            self._reacquire_single_instance()
            QMessageBox.warning(
                self,
                "Mise à jour",
                "La mise à jour n'a pas pu être appliquée.\n\n"
                f"{exc}\n\nElle sera proposée au prochain démarrage.",
            )
            return
        updater.note_available(self.settings, "")  # la version installée va changer
        config.save_settings(self.settings)
        self.quit_app()

    def _reacquire_single_instance(self) -> None:
        """Reprend le verrou mono-instance après un échec de mise à jour."""
        if self._single_instance is not None and not self._single_instance.acquire():
            # Une autre instance a profité de la libération : cas très improbable,
            # on la laisse vivre plutôt que d'écrire à deux sur le partage.
            self._quitting = True
            QApplication.quit()

    def _offer_pending_update(self) -> None:
        """Propose d'appliquer une mise à jour déjà téléchargée."""
        answer = QMessageBox.question(
            self,
            "Mise à jour prête",
            "Une mise à jour a déjà été téléchargée.\n\n"
            "Redémarrer maintenant pour l'installer ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._apply_update()


def _app_icon() -> QIcon:
    """Icône de l'application (barre de titre + barre des tâches).

    - binaire « frozen » : l'icône ``.ico`` est embarquée par PyInstaller via
      ``datas`` et extraite dans ``sys._MEIPASS`` (mode « onefile ») ;
    - en développement : fichier ``icone_radcasemanager.ico`` à la racine ;
    - en dernier recours (Windows), l'icône gravée dans l'exécutable est lue
      directement, sans dépendre d'un fichier livré à côté.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "icone_radcasemanager.ico")
    candidates.append(Path(__file__).resolve().parent / "icone_radcasemanager.ico")

    for path in candidates:
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon

    if sys.platform == "win32" and getattr(sys, "executable", ""):
        icon = QFileIconProvider().icon(QFileInfo(sys.executable))
        if not icon.isNull():
            return icon
    return QIcon()


def _set_windows_app_id() -> None:
    """Identifiant d'application Windows pour la barre des tâches.

    Sans cela, Windows peut regrouper la fenêtre sous une icône générique (ou
    sous celle d'un raccourci épinglé) plutôt que sous celle de l'exécutable.
    À appeler avant la création de toute fenêtre.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RadCaseManager.App"
        )
    except Exception:
        pass


def main() -> int:
    # L'argument « --minimized » est posé par le démarrage automatique (voir
    # ``autostart``) : il est retiré **avant** QApplication, qui analyse la ligne
    # de commande et n'a que faire de nos propres options.
    start_minimized = autostart.START_MINIMIZED_FLAG in sys.argv
    if start_minimized:
        sys.argv.remove(autostart.START_MINIMIZED_FLAG)

    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(_app_icon())
    # La fermeture de la fenêtre peut se contenter de réduire l'application dans
    # la zone de notification : c'est alors « Quitter » qui arrête le processus
    # (voir ``MainWindow.closeEvent`` et ``MainWindow.quit_app``).
    app.setQuitOnLastWindowClosed(False)

    # Une seule instance par session utilisateur. Si l'application tourne déjà
    # (fenêtre réduite dans la zone de notification), ce second lancement réveille
    # la fenêtre existante et s'arrête : le dialogue de configuration n'apparaît
    # donc jamais deux fois, et deux processus n'écrivent pas en même temps dans
    # le dossier partagé.
    single = tray.SingleInstance()
    if not single.acquire():
        return 0

    updater.cleanup()  # supprime les restes de la mise à jour précédente

    settings = config.load_settings()
    if not settings.is_configured:
        dialog = SettingsDialog(settings)
        if dialog.exec() != QDialog.Accepted:
            return 0
        settings = dialog.result_settings()
        config.save_settings(settings)

    bootstrap.ensure_shared_structure(settings.shared_root)
    _ensure_user_known(settings)

    # Démarrage automatique : on (ré)inscrit l'exécutable avec son chemin
    # courant — il a pu être déplacé depuis la dernière fois — ou on retire
    # l'entrée si l'utilisateur a décoché le réglage. Sans effet hors Windows.
    autostart.sync(settings.start_with_windows)

    window = MainWindow(settings, single_instance=single)
    # Bouton de reconfiguration accessible depuis la barre de menus.
    menu = window.menuBar().addMenu("Fichier")
    menu.addAction("Réglages…", window.open_settings)
    menu.addAction("Quitter", window.quit_app)
    # Vérification manuelle des mises à jour.
    help_menu = window.menuBar().addMenu("Aide")
    help_menu.addAction("Vérifier les mises à jour…", lambda: window.check_updates())

    # Démarrage lancé par Windows (« --minimized ») : la fenêtre reste masquée,
    # l'application est joignable par son icône. Repli sur l'affichage normal si
    # la zone de notification n'existe pas — sinon la fenêtre serait
    # inaccessible.
    reduce_at_start = start_minimized and window.can_reduce_at_startup()
    if not reduce_at_start:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
