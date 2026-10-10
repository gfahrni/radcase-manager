"""Interface graphique PySide6 de RadCase Manager (V2).

Deux onglets :
- « Nouveau cas » : collage du presse-papiers + formulaire d'enregistrement.
- « Consulter »   : recherche multi-critères dans les cas existants.
"""
from __future__ import annotations

import csv
import sys

from PySide6.QtCore import QDate, QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QCompleter,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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
from bootstrap import PERSONAL_EMOJI, PERSONAL_UNIT

PREVIEW_W, PREVIEW_H = 400, 320


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
        return config.Settings(
            user_name=self.name_input.text().strip(),
            shared_root=self.folder_input.text().strip(),
            last_units=list(getattr(self, "_initial", config.Settings()).last_units),
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


class ImageOverlay(QLabel):
    """Superposition couvrant la fenêtre, affichant l'image du cas en grand."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: black;")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._source: QPixmap | None = None
        self.hide()

    def show_image(self, source: QPixmap) -> None:
        self._source = source
        self.setGeometry(self.parentWidget().rect())
        self._refresh()
        self.show()
        self.raise_()
        self.setFocus()

    def refresh_geometry(self) -> None:
        if self.isVisible() and self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())

    def _refresh(self) -> None:
        if self._source is None:
            return
        self.setPixmap(
            self._source.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        super().resizeEvent(event)
        self._refresh()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (nom imposé par Qt)
        self.hide()

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
                 personal_selected: bool, case_type: str = "Standard", image=None,
                 heading: str = "Confirmer l'enregistrement"):
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
        personal_label = QLabel(PERSONAL_EMOJI if personal_selected else "—")
        info.addRow("Collection personnelle :", personal_label)
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
# Fenêtre principale
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, settings: config.Settings):
        super().__init__()
        self.settings = settings
        self.current_image: QImage | None = None
        self._results: dict[int, storage.Case] = {}

        self.setWindowTitle("RadCase Manager")
        self.setGeometry(120, 120, 980, 680)

        self.tabs = QTabWidget()
        self.new_case_tab_index = self.tabs.addTab(self._build_new_case_tab(), "➕ Nouveau cas")
        self.consult_tab_index = self.tabs.addTab(self._build_consult_tab(), "🔎 Rechercher")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self.edit_page = None          # construit à la demande
        self._editing_case = None

        # Bandeau de confirmation non bloquant (créé une fois, réutilisé).
        self._toast = Toast(self)
        # Superposition plein écran (dans la fenêtre) pour agrandir une image.
        self._overlay = ImageOverlay(self)
        self.case_image.clicked.connect(self._show_fullscreen_image)

        self._build_status_bar()

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

    def _show_fullscreen_image(self) -> None:
        """Affiche l'image du cas en grand (superposition couvrant la fenêtre)."""
        if self.case_image._source is not None:
            self._overlay.show_image(self.case_image._source)

    def _on_tab_changed(self, index: int) -> None:
        if index == self.new_case_tab_index:
            self._sync_clipboard(overwrite=False)
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

    # --- Onglet « Nouveau cas » ------------------------------------------- #
    def _build_new_case_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)

        # Colonne gauche : aperçu de l'image.
        left = QVBoxLayout()
        left.addWidget(QLabel("<h3>Image du cas</h3>"))
        self.preview = QLabel("Aucune image.\nL'image du presse-papiers est importée à l'activation,\nou cliquez sur « Coller l'image du presse-papiers ».")
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
        type_row, self.case_type_buttons = self._make_case_type_row()
        units_layout.addWidget(type_row)
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
        """Liste (emoji, nom) des unités + « Collection perso » en dernier."""
        entries = [(u.get("emoji", ""), u["name"]) for u in bootstrap.load_units(self.settings.shared_root)]
        entries.append((PERSONAL_EMOJI, PERSONAL_UNIT))
        return entries

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
        self.preview.setText("Aucune image.\nL'image du presse-papiers est importée à l'activation,\nou cliquez sur « Coller l'image du presse-papiers ».")
        self.clipboard_hint.setText("")

    def selected_units(self) -> list[str]:
        return [cb.property("unit_name") for cb in self.unit_checkboxes if cb.isChecked()]

    def _units_summary(self, units: list) -> tuple:
        """Résume les unités : texte avec emoji (hors perso) + présence perso."""
        emoji_map = {u["name"]: u.get("emoji", "") for u in bootstrap.load_units(self.settings.shared_root)}
        real = [u for u in units if u != PERSONAL_UNIT]
        text = "\n".join((f"{emoji_map.get(u, '')} {u}").strip() for u in real)
        return text, (PERSONAL_UNIT in units)

    def save_case(self) -> None:
        title = self.title_input.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Le titre du cas est obligatoire.")
            return
        units = self.selected_units()
        if not any(u != PERSONAL_UNIT for u in units):
            QMessageBox.warning(
                self, "Validation",
                "Sélectionnez au moins une unité (la collection personnelle seule ne suffit pas).",
            )
            return

        description = self.description_input.toPlainText()
        units_text, personal = self._units_summary(units)
        case_type = self.selected_case_type()
        dialog = ConfirmCaseDialog(
            self, title=title, description=description,
            units_text=units_text, personal_selected=personal, case_type=case_type,
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
            )
        except Exception as exc:  # noqa: BLE001 (message clair à l'utilisateur)
            QMessageBox.critical(self, "Échec de l'enregistrement", str(exc))
            return

        self.clear_case_fields()
        self.reset_case_type()  # le type revient à « Standard » pour le prochain cas
        self._toast.show_message(f"✅ Cas « {case.title} » enregistré.")

    def clear_case_fields(self) -> None:
        """Vide titre/description/image (les unités mémorisées sont conservées)."""
        self.title_input.clear()
        self.description_input.clear()
        self.clear_image()

    def clear_form(self) -> None:
        """Vide tout le formulaire, y compris les coches d'unités mémorisées."""
        self.clear_case_fields()
        for cb in getattr(self, "unit_checkboxes", []):
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.reset_case_type()
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
        left.addWidget(QLabel("<h3>Image du cas</h3>"))
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

    def save_edit(self) -> None:
        if self._editing_case is None:
            return
        title = self.edit_title_input.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Le titre du cas est obligatoire.")
            return
        units = self.selected_edit_units()
        if not any(u != PERSONAL_UNIT for u in units):
            QMessageBox.warning(
                self, "Validation",
                "Sélectionnez au moins une unité (la collection personnelle seule ne suffit pas).",
            )
            return

        image_arg = self.edit_image_source if self.edit_image_changed else storage.KEEP_IMAGE
        description = self.edit_description_input.toPlainText()
        units_text, personal = self._units_summary(units)
        case_type = self.selected_edit_case_type()
        dialog = ConfirmCaseDialog(
            self, title=title, description=description,
            units_text=units_text, personal_selected=personal, case_type=case_type,
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

        # Grille 2×2 : Texte/Auteur puis Unités/Période ; boutons en dessous.
        grid.addWidget(labeled("Texte :", self.search_text), 0, 0)
        grid.addWidget(labeled("Auteur :", self.search_author), 0, 1)
        grid.addWidget(labeled("Unités :", self.units_button), 1, 0)
        grid.addWidget(labeled("Période :", self.period_button), 1, 1)
        grid.addWidget(buttons_row, 2, 0, 1, 2)
        grid.addWidget(self.free_date_row, 3, 0, 1, 2)
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
        authors = storage.all_authors(self.settings.shared_root)
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
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Erreur de recherche", str(exc))
            return

        self.results_list.clear()
        self.detail.clear()
        self.case_image.clear_source()
        self._results = {}
        for case in cases:
            label = f"{case.title} – {case.date_added}  ({case.author})"
            if case.units:
                label += f"  [{', '.join(case.units)}]"
            item = QListWidgetItem(label)
            self.results_list.addItem(item)
            self._results[id(item)] = case

    def reset_search(self) -> None:
        """Réinitialise tous les filtres de recherche et vide les résultats."""
        self.search_text.clear()
        self.search_author.clear()
        for cb in self.filter_unit_checkboxes:
            cb.setChecked(False)
        self._select_period(self.default_period)
        self.run_search()  # revient à la vue par défaut (« Cette semaine »)

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

        header = ["Titre", "Type", "Date d'ajout", "Date de modification",
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
            return
        case = self._results.get(id(current))
        if case is None:
            return
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
            self._update_status()


def main() -> int:
    app = QApplication(sys.argv)

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
