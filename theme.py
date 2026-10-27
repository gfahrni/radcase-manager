"""Thème visuel : couleurs déduites de la palette système (Windows clair / sombre).

L'application ne propose pas de choix manuel : Qt reprend la palette de Windows
et ce module en dérive les quelques couleurs sémantiques de l'interface (texte
secondaire, bordures, survol, cartes du Fil, fond de l'onglet Édition…).

Les couleurs sont regroupées dans une feuille de style appliquée à
``QApplication`` (voir :func:`install`). Elle est régénérée quand la palette
change, si bien qu'un basculement clair ↔ sombre ouvert est suivi sans
redémarrage.

Les surfaces volontairement noires (aperçus et plein écran d'image) ne sont pas
ici : le noir est un choix d'affichage pour l'imagerie, identique dans les deux
modes.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

#: Teinte de l'onglet « Édition » : mélangée au fond de fenêtre pour donner un
#: fond chaud discret et lisible dans les deux modes.
EDIT_TINT = QColor("#b00020")

#: Ambre de l'indicateur de mise à jour, décliné pour rester lisible.
WARNING_DARK = QColor("#e6a23c")
WARNING_LIGHT = QColor("#8a5200")

#: Vert du message « image du presse-papiers prête ».
SUCCESS_DARK = QColor("#7fd08a")
SUCCESS_LIGHT = QColor("#2e7d32")


def _color(role: QPalette.ColorRole) -> QColor:
    """Couleur du rôle dans la palette courante de l'application."""
    return QApplication.palette().color(role)


def is_dark() -> bool:
    """Vrai si le thème système en cours est sombre."""
    return _color(QPalette.Window).lightness() < 128


def blend(fg: QColor, bg: QColor, bg_ratio: float) -> QColor:
    """Mélange ``bg_ratio`` de ``bg`` dans ``fg`` (0 = fg, 1 = bg)."""
    ratio = min(1.0, max(0.0, bg_ratio))
    return QColor(
        round(fg.red() * (1 - ratio) + bg.red() * ratio),
        round(fg.green() * (1 - ratio) + bg.green() * ratio),
        round(fg.blue() * (1 - ratio) + bg.blue() * ratio),
    )


def muted_text() -> QColor:
    """Texte secondaire : titres de panneaux, états vides, métadonnées."""
    return blend(_color(QPalette.WindowText), _color(QPalette.Window), 0.32)


def border() -> QColor:
    """Séparateur fin entre panneaux."""
    return blend(_color(QPalette.WindowText), _color(QPalette.Window), 0.72)


def hover_background() -> QColor:
    """Fond de survol des boutons du menu (assez soutenu pour rester lisible)."""
    return blend(_color(QPalette.WindowText), _color(QPalette.Window), 0.88)


def card_background() -> QColor:
    """Fond d'une carte du Fil : la surface de saisie de la palette."""
    return _color(QPalette.Base)


def new_card_background() -> QColor:
    """Fond d'une carte « nouvelle » : surface teintée par l'accent du système."""
    base = _color(QPalette.Base)
    return blend(_color(QPalette.Highlight), base, 0.85 if is_dark() else 0.90)


def edit_background() -> QColor:
    """Fond de l'onglet Édition : teinte chaude, pâle en clair, sombre en sombre."""
    return blend(EDIT_TINT, _color(QPalette.Window), 0.90 if is_dark() else 0.92)


def warning_text() -> QColor:
    """Couleur de l'indicateur « mise à jour disponible »."""
    return WARNING_DARK if is_dark() else WARNING_LIGHT


def success_text() -> QColor:
    """Couleur du message de confirmation du presse-papiers."""
    return SUCCESS_DARK if is_dark() else SUCCESS_LIGHT


def stylesheet() -> str:
    """Feuille de style de l'application, recalculée depuis la palette."""
    highlight = _color(QPalette.Highlight).name()
    highlighted_text = _color(QPalette.HighlightedText).name()
    muted = muted_text().name()
    # Survol de la pastille « ↓ N nouveaux cas » : accent légèrement rabattu
    # vers le fond de fenêtre, lisible dans les deux modes.
    pill_hover = blend(
        _color(QPalette.Highlight), _color(QPalette.Window), 0.18
    ).name()
    return f"""
QWidget#panelTop {{ border-top: 1px solid {border().name()}; }}
QLabel#sectionTitle {{ color: {muted}; font-weight: bold; }}
QLabel#emptyState {{ color: {muted}; padding: 24px; }}
QLabel#hintLabel {{ color: {muted}; }}
QLabel#feedMeta {{ color: {muted}; }}
QLabel#feedDate {{ color: {muted}; font-weight: bold; }}
QLabel#updateLabel {{ color: {warning_text().name()}; font-weight: bold; }}
QLabel#clipboardHint {{ color: {success_text().name()}; }}
QPushButton#menuButton {{ text-align: left; padding: 6px 8px; border: none;
                          border-radius: 4px; }}
QPushButton#menuButton:hover {{ background-color: {hover_background().name()}; }}
QPushButton#menuButton:checked {{ background-color: {highlight};
                                  color: {highlighted_text}; font-weight: bold; }}
QPushButton#toggleButton {{ border: 1px solid {border().name()}; padding: 6px 4px;
                            background: transparent; }}
QPushButton#toggleButton:hover {{ background-color: {hover_background().name()}; }}
QPushButton#toggleButton:checked {{ background-color: {highlight};
                                    color: {highlighted_text}; font-weight: bold; }}
QFrame#feedCard {{ background-color: {card_background().name()}; border-radius: 10px; }}
QFrame#feedCardNew {{ background-color: {new_card_background().name()};
                      border-radius: 10px; }}
QFrame#feedSeparator {{ background: {border().name()}; }}
QPushButton#feedNewPill {{ background-color: {highlight}; color: {highlighted_text};
                           border: none; border-radius: 12px; padding: 6px 14px;
                           font-weight: bold; }}
QPushButton#feedNewPill:hover {{ background-color: {pill_hover}; }}
QWidget#editPage {{ background-color: {edit_background().name()}; }}
"""


def install(app: QApplication) -> None:
    """Applique la feuille de style et la régénère au changement de palette."""
    app.setStyleSheet(stylesheet())
    app.paletteChanged.connect(lambda *_: app.setStyleSheet(stylesheet()))
