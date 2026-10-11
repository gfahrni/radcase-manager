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

from PySide6.QtCore import QDate, QEvent, QFileInfo, QPoint, Qt, QTimer, Signal
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
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import bootstrap
import config
import storage

PREVIEW_W, PREVIEW_H = 400, 320

#: Icône de l'onglet « Fil d'actualité » : ⚪ (blanc) = aucun nouveau cas.
#: Devient 🔵 (bleu) quand du nouveau est détecté.
FEED_DOT_EMOJI = "⚪"
FEED_DOT_NOTIFY = "🔵"

#: Intervalle de vérification du badge de nouveauté du fil (millisecondes).
FEED_BADGE_INTERVAL_MS = 5 * 60 * 1000


# --------------------------------------------------------------------------- #
# Réglages (/première exécution)
# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    """Saisie du nom d'utilisateur et du dossier partagé."""

    def __init__(self, settings: config.Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration de RadCase Manager")
        self.setMinimumWidth(520)

        self._initial = settings

        form = QFormLayout(self)

        self.name_input = QLineEdit(settings.user_name)
        self.name_input.setPlaceholderText("Ex: Dr Martin")
        form.addRow("Nom d'utilisateur :", self.name_input)

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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier partagé", self.folder_input.text() or ""
        )
        if chosen:
            self.folder_input.setText(chosen)

    def result_settings(self) -> config.Settings:
        initial = getattr(self, "_initial", config.Settings())
        return config.Settings(
            user_name=self.name_input.text().strip(),
            shared_root=self.folder_input.text().strip(),
            last_units=list(initial.last_units),
            last_browser_unit=initial.last_browser_unit,
            feed_units=initial.feed_units,
            feed_last_seen=initial.feed_last_seen,
        )

    def accept(self) -> None:  # noqa: D102
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "Champ manquant", "Le nom d'utilisateur est obligatoire.")
            return
        if not self.folder_input.text().strip():
            QMessageBox.warning(self, "Champ manquant", "Le dossier partagé est obligatoire.")
            return
        super().accept()


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


class DetailOverlay(QWidget):
    """Superposition plein fenêtre : image zoomée + titre, description et métadonnées.

    Ouverte au clic sur une image (Recherche Avancée, Bibliothèque, Fil). Le
    contenu défile si nécessaire ; fermeture par un clic n'importe où ou Échap.
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

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title.setStyleSheet("color:#fff; font-size:18px; font-weight:bold;")
        self.vbox.addWidget(self.title, 0, Qt.AlignHCenter)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.description.setStyleSheet("color:#ddd; font-size:14px;")
        self.vbox.addWidget(self.description, 0, Qt.AlignHCenter)

        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.meta.setStyleSheet("color:#888; font-size:11px;")
        self.vbox.addWidget(self.meta, 0, Qt.AlignHCenter)

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

        self.title.setText(case.title)
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

        spacing = self.vbox.spacing()
        gaps = spacing * len(labels)  # espaces entre image et blocs de texte
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

#: Dimensions du « Fil d'actualité » (cartes, type réseau social).
FEED_CARD_W = 420
FEED_IMG_W, FEED_IMG_H = 387, 267

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
        self._card_widgets: list = []   # vignettes actuellement affichées
        self._empty_label = None        # message si aucun cas
        self._columns = 0               # colonnes courantes (recalculées au resize)
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
        """Filtre instantanément les cas affichés selon le nom saisi."""
        self._quick_text = text or ""
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        """Applique le filtre de recherche rapide et reconstruit la grille."""
        needle = self._quick_text.strip().casefold()
        cases = [
            case for case in self._unit_cases
            if not needle or needle in case.title.casefold()
        ]
        cases.sort(key=lambda case: case.title.casefold())

        # Supprime les widgets précédents (vignettes ou message vide).
        self._clear_grid()
        for widget in self._card_widgets:
            widget.deleteLater()
        self._card_widgets = []
        if self._empty_label is not None:
            self._empty_label.deleteLater()
            self._empty_label = None

        if cases:
            self._card_widgets = [self._make_card(case) for case in cases]
        else:
            empty = QLabel("Aucun cas à afficher.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:#888; padding:24px;")
            self._empty_label = empty
        self._layout_cards(force=True)

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
        pixmap = QPixmap(str(case.image_path)) if case.image_path else QPixmap()
        if pixmap.isNull():
            thumb.setText("—")
            thumb.setStyleSheet("background-color:#1e1e1e; color:#777;")
        else:
            thumb.setPixmap(
                pixmap.scaled(CARD_IMG_W, CARD_IMG_H, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
            )
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


class FeedView(QWidget):
    """Fil chronologique type chat (le plus récent en bas).

    Les cas sont listés du plus ancien (haut) au plus récent (bas) ; la vue se
    positionne sur l'instant d'ancrage (par défaut : tout en bas). Un séparateur
    indique le changement de jour. Chaque carte affiche l'image en grand, le
    titre puis la description. Les cas « nouveaux » (non lus) ont un fond bleu,
    qui s'efface dès que la carte devient visible (scroll) ou est cliquée.
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
        self._cards_by_id: dict = {}        # id de cas -> carte
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
            self._mark_visible_read()  # cartes visibles à l'ouverture → lues
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

    def load(self, anchor=None, units=None, unread_ids=None, case_types=None) -> None:
        """Reconstruit le fil, du plus ancien (haut) au plus récent (bas).

        ``anchor`` : instant sur lequel se positionner à l'ouverture (tous les cas
        restent affichés : on remonte pour l'historique) ; ``None`` = le plus
        récent (bas). ``units`` : unités suivies (``None`` = toutes).
        ``unread_ids`` : identifiants des cas « nouveaux » (carte à fond bleu).
        ``case_types`` : types de cas à afficher (``None`` = tous).
        """
        self._unread_ids = set(unread_ids or ())
        self._cards_by_id = {}
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

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

        if not cases:
            empty = QLabel("Aucun cas à afficher.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("color:#888; padding:24px;")
            self.vbox.addWidget(empty)
            self.vbox.addStretch()
            self._pending_scroll_widget = None
            self._scroll_pending = False
            return

        anchor_iso = anchor.isoformat(timespec="seconds") if anchor else None
        last_day = None
        anchor_widget = None
        for case in cases:
            day = (case.date_added or "")[:10]
            if day and day != last_day:
                last_day = day
                self.vbox.addWidget(self._date_separator(case), 0, Qt.AlignHCenter)
            card = self._make_feed_card(case)
            self.vbox.addWidget(card, 0, Qt.AlignHCenter)
            self._cards_by_id[case.id] = card
            # Dernière carte au plus tard à l'instant d'ancrage = point de départ.
            if anchor_iso and (case.date_added or "") <= anchor_iso:
                anchor_widget = card
        self.vbox.addStretch()

        self._pending_scroll_widget = anchor_widget
        self._scroll_pending = True
        self._scroll_attempts = 0
        QTimer.singleShot(0, self._apply_pending_scroll)

    # --- Cas « nouveaux » (fond bleu) ------------------------------------- #
    def _on_scroll(self, _value: int) -> None:
        """À chaque défilement : marque lues les cartes entrées dans la vue."""
        self._mark_visible_read()

    def _mark_visible_read(self) -> None:
        """Marque lues les cartes « nouvelles » qui intersectent la zone visible."""
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
                self._mark_read(case_id)

    def _mark_read(self, case_id) -> None:
        """Passe une carte à « lue » (retire le fond bleu)."""
        if case_id not in self._unread_ids:
            return
        self._unread_ids.discard(case_id)
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
        """Carte du fil : grande image (cliquable) + titre + description (légende)."""
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

        image = ClickableLabel()
        image.setFixedSize(FEED_IMG_W, FEED_IMG_H)
        image.setAlignment(Qt.AlignCenter)
        image.setAttribute(Qt.WA_StyledBackground, True)
        image.setCursor(Qt.PointingHandCursor)
        image.setStyleSheet("background:black; border-radius:6px;")
        image.clicked.connect(lambda c=case: self._on_image_clicked(c))
        pixmap = QPixmap(str(case.image_path)) if case.image_path else QPixmap()
        if pixmap.isNull():
            image.setText("—")
            image.setStyleSheet("background:#111; color:#777; border-radius:6px;")
        else:
            image.setPixmap(
                pixmap.scaled(FEED_IMG_W, FEED_IMG_H, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
            )
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
class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings):
        super().__init__()
        self.settings = settings
        self.current_image: QImage | None = None
        self._results: dict[int, storage.Case] = {}

        # Fil d'actualité : état de session pour la pastille de nouveauté
        # (défini avant la construction des onglets, qui lit cet état).
        self._feed_session_watermark = settings.feed_last_seen or ""
        self._feed_read_ids: set = set()

        self.setWindowTitle("RadCase Manager")
        self.setGeometry(120, 120, 980, 680)

        self.tabs = QTabWidget()
        self.new_case_tab_index = self.tabs.addTab(self._build_new_case_tab(), "➕ Nouveau cas")
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

        # Badge de nouveauté du fil : vérification périodique (toutes les 5 min).
        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(FEED_BADGE_INTERVAL_MS)
        self._feed_timer.timeout.connect(self._update_feed_badge)
        self._feed_timer.start()
        self._update_feed_badge()

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

    def _unread_cases(self) -> list:
        """Cas du fil « nouveaux » (non lus) pour la session courante.

        Nouveau = visible, dans une unité suivie, plus récent que le filigrane de
        session, hors mes propres cas, et pas encore lu cette session.
        """
        units = self.selected_feed_units()
        if not units:
            return []
        wanted = {u.casefold() for u in units}
        boxes = getattr(self, "feed_case_type_checkboxes", None)
        types = checked_case_types(boxes) if boxes is not None else None
        since = self._feed_session_watermark or ""
        me = (self.settings.user_name or "").strip().casefold()
        try:
            cases = storage.search_cases(
                self.settings.shared_root, viewer=self.settings.user_name
            )
        except Exception:  # noqa: BLE001 (badge best-effort)
            return []
        result = []
        for case in cases:
            if not (wanted & {u.casefold() for u in case.units}):
                continue
            if types is not None and case.case_type not in types:
                continue
            if since and (case.date_added or "") <= since:
                continue
            if me and case.author.strip().casefold() == me:
                continue  # ne pas compter mes propres cas
            if case.id in self._feed_read_ids:
                continue
            result.append(case)
        return result

    def _on_feed_case_read(self, case_id) -> None:
        """Une carte vient d'être lue (scroll ou clic) : met à jour le badge."""
        if case_id and case_id not in self._feed_read_ids:
            self._feed_read_ids.add(case_id)
            self._update_feed_badge()

    def _on_feed_case_activated(self, case) -> None:
        """Clic sur l'image d'une carte : marque lu, puis ouvre le zoom détail."""
        if case is not None:
            self._on_feed_case_read(case.id)
            self._show_case_detail(case)

    def _update_feed_badge(self) -> None:
        """Pastille = nombre de cartes du fil encore « nouvelles » (non lues)."""
        count = len(self._unread_cases())
        self.set_feed_dot(FEED_DOT_NOTIFY if count else FEED_DOT_EMOJI, count)

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
            self._update_feed_badge()
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

        # Filtre « Type de cas » (3 cases à cocher), en bas de la sélection.
        feed_types_box = QWidget()
        feed_types_box.setAttribute(Qt.WA_StyledBackground, True)
        feed_types_box.setStyleSheet("border-top: 1px solid #444;")
        feed_types_layout = QVBoxLayout(feed_types_box)
        feed_types_layout.setContentsMargins(6, 6, 6, 6)
        feed_types_layout.setSpacing(2)
        feed_types_title = QLabel("Type de cas")
        feed_types_title.setStyleSheet("color:#bbb; font-weight:bold;")
        feed_types_layout.addWidget(feed_types_title)
        self.feed_case_type_checkboxes = make_case_type_checkboxes()
        for cb in self.feed_case_type_checkboxes:
            cb.toggled.connect(self._on_feed_case_type_changed)
            feed_types_layout.addWidget(cb)
        top_layout.addWidget(feed_types_box)

        left_layout.addWidget(top, 1)

        # --- Moitié basse : ancrages temporels ---
        bottom = QWidget()
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
        unread = {case.id for case in self._unread_cases()}
        self.feed_view.load(
            anchor,
            units=self.selected_feed_units(),
            unread_ids=unread,
            case_types=self.selected_feed_case_types(),
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

        try:
            case = storage.create_case(
                title=title,
                units=units,
                author=self.settings.user_name,
                description=description,
                image=self.current_image,
                shared_root=self.settings.shared_root,
                case_type=case_type,
                visibility=visibility,
            )
        except Exception as exc:  # noqa: BLE001 (message clair à l'utilisateur)
            QMessageBox.critical(self, "Échec de l'enregistrement", str(exc))
            return

        self.clear_case_fields()
        self.reset_case_type()   # le type revient à « Standard » pour le prochain cas
        self.reset_visibility()  # la visibilité revient à « Public »
        self._toast.show_message(f"✅ Cas « {case.title} » enregistré.")

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
        right.addWidget(save_btn)

        cancel_btn = QPushButton("↩️ Annuler l'édition")
        cancel_btn.clicked.connect(self.cancel_edit)
        right.addWidget(cancel_btn)

        content.addLayout(right, 2)
        self.edit_page = page

    def _fill_edit_form(self, case: storage.Case) -> None:
        self.edit_title_input.setText(case.title)
        self.edit_description_input.setPlainText(case.description)
        self._reload_edit_units(case.units)
        self._set_edit_case_type(case.case_type)
        self._set_edit_visibility(case.visibility)
        self.edit_image_source = case.image_path if case.image else None
        self.edit_image_changed = False
        self._edit_update_preview()

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
        self._edit_update_preview()

    def edit_clear_image(self) -> None:
        self.edit_image_source = None
        self.edit_image_changed = True
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
        try:
            updated = storage.update_case(
                case=self._editing_case,
                title=title,
                units=units,
                author=self.settings.user_name,
                description=description,
                image=image_arg,
                shared_root=self.settings.shared_root,
                case_type=case_type,
                visibility=visibility,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Échec de la modification", str(exc))
            return

        self._close_edit_tab()
        self._toast.show_message(f"✅ Cas « {updated.title} » modifié.")
        if self.results_list.count():
            self.run_search()

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
        self.search_text.setPlaceholderText("Texte dans le titre / description")
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

        header = ["Titre", "Type", "Visibilité", "Date d'ajout", "Date de modification",
                  "Auteur", "Unités", "Image", "Dossier", "Description"]
        try:
            # utf-8-sig : accents corrects dans Excel ; ';' : séparateur Excel FR.
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(header)
                for case in cases:
                    writer.writerow([
                        case.title,
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
        self.detail.setPlainText(f"{case.title}\n\n{description}" if description else case.title)
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
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            self.settings = dialog.result_settings()
            config.save_settings(self.settings)
            bootstrap.ensure_shared_structure(self.settings.shared_root)
            self.reload_units()
            self.reload_filter_units()
            self.reload_filter_authors()
            self.feed_view.set_settings(self.settings)
            self.reload_feed_units()
            self.browser.set_settings(self.settings)
            self._update_status()


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
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(_app_icon())

    settings = config.load_settings()
    if not settings.is_configured:
        dialog = SettingsDialog(settings)
        if dialog.exec() != QDialog.Accepted:
            return 0
        settings = dialog.result_settings()
        config.save_settings(settings)

    bootstrap.ensure_shared_structure(settings.shared_root)

    window = MainWindow(settings)
    # Bouton de reconfiguration accessible depuis la barre de menus.
    menu = window.menuBar().addMenu("Fichier")
    menu.addAction("Réglages…", window.open_settings)
    menu.addAction("Quitter", app.quit)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
