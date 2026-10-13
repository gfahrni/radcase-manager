"""Mode « barre d'état système » (zone de notification) et verrou mono-instance.

L'application peut continuer de tourner quand sa fenêtre est fermée : elle reste
accessible par une **icône dans la zone de notification** (le petit carré à côté
de l'horloge, à droite de la barre des tâches). C'est le comportement des
applications portables qui « se réduisent dans le tray » au lieu de s'arrêter.

Deux mécanismes, tous deux sans dépendance externe (PySide6 uniquement) :

- :class:`SingleInstance` — une **seule instance par session utilisateur**. Sans
  cela, un double-clic sur l'exécutable alors que l'application est déjà réduite
  lancerait un second processus : deux icônes, deux index locaux, deux écritures
  concurrentes sur le dossier partagé. Un second lancement **réveille** donc
  l'instance existante, puis rend la main.
- :class:`TrayIcon` — icône + menu contextuel (Ouvrir / Réglages… / Quitter) et
  notifications, pour garder l'application pilotable fenêtre masquée.

Choix d'implémentation (voir aussi ``ARCHITECTURE_PLAN.md``) : le verrou n'est
**pas** un fichier de verrou (``QLockFile``), mais un **canal local nommé**
(``QLocalServer`` / ``QLocalSocket``). Un fichier de verrou peut rester orphelin
après un plantage et bloquer les démarrages suivants ; ici, si aucun processus
n'écoute, le canal est simplement recréé. Le nom du canal inclut le nom
d'utilisateur système : sur un poste partagé (bureau à distance, session
multi-utilisateur), chacun a bien sa propre instance — les réglages locaux étant
eux aussi propres au compte machine, c'est la bonne granularité.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import config

#: Requête envoyée par un second lancement, et accusé de réception attendu.
_WAKE_REQUEST = b"raise"
_WAKE_ACK = b"ok"

#: Délais d'attente du canal local (millisecondes). Généreux à dessein : le
#: processus qui répond peut être occupé à peindre une grande bibliothèque.
CONNECT_TIMEOUT_MS = 700
#: Attente de l'accusé de réception. Sans réponse, le canal est jugé orphelin.
ACK_TIMEOUT_MS = 2500

#: Nombre de tentatives d'écoute et pause entre deux (secondes).
ACQUIRE_ATTEMPTS = 3
ACQUIRE_DELAY_S = 0.4

#: Durée d'affichage des notifications (millisecondes).
NOTIFY_MS = 6000


def _channel_name() -> str:
    """Nom du canal local, propre à l'application et au compte de session.

    Le nom d'utilisateur est filtré : un canal nommé n'accepte pas n'importe quel
    caractère (espaces, barres obliques, « \\ », etc.).
    """
    user = "".join(c if c.isalnum() else "-" for c in config.session_user_name())
    return f"{config.APP_NAME}-{user or 'default'}"


class SingleInstance(QObject):
    """Garantit qu'une seule instance tourne par session utilisateur.

    Ordre des opérations à l'acquisition :

    1. **Écouter** sur le canal nommé. Si cela réussit, nous sommes seuls.
    2. Sinon, un autre processus écoute : on s'y **connecte** et on attend son
       **accusé de réception**. S'il répond, l'instance vivante est réveillée
       (:attr:`activated`) et le nouveau lancement doit s'arrêter.
    3. Si aucune réponse n'arrive, le canal est **orphelin** (le processus
       précédent s'est terminé brutalement) : on le retire et on recommence.

    L'accusé de réception est ce qui rend le mécanisme sûr vis-à-vis de la mise à
    jour automatique : au redémarrage, la nouvelle version démarre pendant que
    l'ancienne achève de s'arrêter. Une ancienne instance en cours de fermeture
    n'accuse pas réception, la nouvelle prend donc la main au lieu de se croire
    « seconde instance » et de sortir aussitôt.

    Le verrou est **libéré** par :meth:`release` (fermeture propre) ou par la fin
    du processus — la fermeture du canal suffit, il n'y a aucun fichier à
    nettoyer.
    """

    #: Émis quand un autre lancement demande à remonter la fenêtre existante.
    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = _channel_name()
        self._server: QLocalServer | None = None

    # -- Acquisition / libération ----------------------------------------- #
    def acquire(self) -> bool:
        """Tente de devenir l'instance unique.

        Retourne ``True`` si l'application peut démarrer, ``False`` si une autre
        instance tourne déjà (elle a été réveillée si possible) : l'appelant doit
        alors s'arrêter.
        """
        for attempt in range(ACQUIRE_ATTEMPTS):
            server = QLocalServer(self)
            # Volontairement SANS ``setSocketOptions`` : sur Unix (Qt 6.12), poser
            # une option d'accès fait répondre « succès » au second ``listen()``
            # sur le même nom — les deux instances croient alors être seules. Le
            # cloisonnement par utilisateur est assuré par le nom du canal, qui
            # contient le compte de session (voir ``_channel_name``).
            if server.listen(self._name):
                server.newConnection.connect(self._on_connection)
                self._server = server
                return True
            # Écoute impossible : on relâche franchement l'objet temporaire (pas de
            # ``deleteLater``, qui ne serait traité qu'à la boucle d'événements,
            # pas encore démarrée à ce stade).
            server.close()
            server.setParent(None)

            if self._wake_existing():
                return False

            # Personne n'écoute : canal orphelin laissé par un plantage.
            QLocalServer.removeServer(self._name)
            if attempt < ACQUIRE_ATTEMPTS - 1:
                time.sleep(ACQUIRE_DELAY_S)

        # Ni écoute ni réveil : situation anormale. On refuse de démarrer une
        # seconde instance plutôt que de risquer des écritures concurrentes.
        return False

    def release(self) -> None:
        """Libère le canal (fermeture propre, ou avant une relance). Idempotent."""
        if self._server is not None:
            self._server.close()
            self._server.setParent(None)  # l'objet Python reprend la propriété
            self._server = None
        # Pas de ``removeServer`` ici : fermer le canal suffit, et cela
        # supprimerait le canal d'une instance qui aurait repris la main.

    # -- Interne ---------------------------------------------------------- #
    def _wake_existing(self) -> bool:
        """Réveille l'instance qui écoute ; vrai si elle a accusé réception."""
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        socket.write(_WAKE_REQUEST)
        socket.flush()
        if not socket.waitForReadyRead(ACK_TIMEOUT_MS):
            socket.abort()
            return False
        reply = bytes(socket.readAll()).strip()
        socket.disconnectFromServer()
        return reply == _WAKE_ACK

    def _on_connection(self) -> None:
        """Requête reçue : on accuse réception puis on demande la remontée."""
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            # Lecture brève et bloquante : l'événement est rare et l'appelant
            # attend explicitement notre réponse (voir ``_wake_existing``).
            if socket.waitForReadyRead(CONNECT_TIMEOUT_MS):
                socket.readAll()
            socket.write(_WAKE_ACK)
            socket.flush()
            socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
            socket.disconnectFromServer()
            self.activated.emit()


class TrayIcon(QObject):
    """Icône de la zone de notification + menu contextuel.

    ``parent`` doit être la fenêtre principale : l'icône disparaît avec elle.
    Les actions sont fournies sous forme de fonctions (pas de dépendance à
    ``main_gui`` depuis ce module).
    """

    def __init__(
        self,
        icon: QIcon,
        parent,
        *,
        on_open,
        on_settings,
        on_quit,
    ) -> None:
        super().__init__(parent)
        self._on_open = on_open

        self._icon = QSystemTrayIcon(icon, self)
        self._icon.setToolTip("RadCase Manager")

        # Le menu doit rester référencé : ``setContextMenu`` n'en prend pas
        # possession.
        self._menu = QMenu(parent)
        self._menu.addAction("Ouvrir", on_open)
        self._menu.addAction("Réglages…", on_settings)
        self._menu.addSeparator()
        self._menu.addAction("Quitter", on_quit)
        self._icon.setContextMenu(self._menu)

        self._icon.activated.connect(self._on_activated)

    @staticmethod
    def available() -> bool:
        """Vrai si le système propose une zone de notification."""
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def notify(self, title: str, message: str, msecs: int = NOTIFY_MS) -> None:
        """Notification système (bulle/toast), affichée à côté de l'icône."""
        self._icon.showMessage(title, message, QSystemTrayIcon.Information, msecs)

    def _on_activated(self, reason) -> None:
        """Clic sur l'icône : double-clic, ou clic simple (Windows)."""
        reasons = QSystemTrayIcon.ActivationReason
        if reason in (reasons.Trigger, reasons.DoubleClick):
            self._on_open()
