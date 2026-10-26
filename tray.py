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
- :class:`TrayIcon` — icône + menu contextuel (Ouvrir / Réglages… / Quitter),
  notifications, et **pastille rouge** (« du nouveau dans le Fil ») pour garder
  l'application pilotable fenêtre masquée.

Choix d'implémentation (voir aussi ``ARCHITECTURE_PLAN.md``) : le verrou n'est
**pas** un fichier de verrou (``QLockFile``), mais un **canal local nommé**
(``QLocalServer`` / ``QLocalSocket``). Un fichier de verrou peut rester orphelin
après un plantage et bloquer les démarrages suivants ; ici, si aucun processus
n'écoute, le canal est simplement recréé. Le nom du canal inclut le nom
d'utilisateur système : sur un poste partagé (bureau à distance, session
multi-utilisateur), chacun a bien sa propre instance — les réglages locaux étant
eux aussi propres au compte machine, c'est la bonne granularité.

**Sous Windows**, en revanche, le canal ne peut pas servir de verrou :
``QLocalServer.listen()`` y **réussit toujours**, même si une autre instance
écoute déjà (Qt crée le pipe avec ``PIPE_UNLIMITED_INSTANCES`` et sans
``FILE_FLAG_FIRST_PIPE_INSTANCE``, et ``removeServer()`` n'y fait rien). Le test
« on écoute donc on est seuls » y serait donc inopérant et chaque lancement
créerait une instance de plus. L'exclusivité est donc assurée par un **mutex
nommé** (:meth:`SingleInstance._acquire_mutex`) : atomique, il vit dans l'espace
de la session, et l'OS le **libère à la mort du processus**, plantage compris —
sans l'inconvénient du verrou orphelin qui avait fait écarter ``QLockFile``. Le
canal local reste, lui, le support du réveil de l'instance existante.
"""
from __future__ import annotations

import ctypes
import sys
import time

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
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

#: Infobulle au repos (et préfixe des infobulles d'alerte).
BASE_TOOLTIP = "RadCase Manager"

#: Tailles produites pour l'icône « du nouveau ». Le tray Windows affiche 16 px
#: en mode compact, 24 px à 100 % et 32 px à 150 % : fournir ces tailles (et les
#: suivantes) évite un redimensionnement flou au moment de l'affichage.
ALERT_SIZES = (16, 24, 32, 48, 64)

#: Couleur de la pastille de nouveauté (rouge mat, lisible sur fond sombre comme
#: sur fond clair).
ALERT_COLOR = "#E53935"

#: Code d'erreur Windows « l'objet nommé existe déjà » (``ERROR_ALREADY_EXISTS``).
ERROR_ALREADY_EXISTS = 183

#: ``kernel32`` chargé paresseusement sous Windows (voir :func:`_kernel32`).
_KERNEL32 = None


def _kernel32():
    """Charge ``kernel32`` une seule fois et déclare les prototypes utilisés.

    Appelée uniquement sous Windows : ``ctypes.wintypes`` n'existe pas sur les
    autres plateformes (l'import lèverait ``ValueError``), et le module doit
    rester importable en développement sous macOS.
    """
    global _KERNEL32
    if _KERNEL32 is None:
        from ctypes import wintypes  # Windows uniquement

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Sans ``restype``, ctypes supposerait un entier 32 bits et tronquerait
        # le handle : ``CloseHandle`` échouerait ensuite silencieusement.
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def _channel_name() -> str:
    """Nom du canal local, propre à l'application et au compte de session.

    Le nom d'utilisateur est filtré : un canal nommé n'accepte pas n'importe quel
    caractère (espaces, barres obliques, « \\ », etc.).
    """
    user = "".join(c if c.isalnum() else "-" for c in config.session_user_name())
    return f"{config.APP_NAME}-{user or 'default'}"


class SingleInstance(QObject):
    """Garantit qu'une seule instance tourne par session utilisateur.

    **Sous Windows**, le verrou est un **mutex nommé** (voir
    :meth:`_acquire_mutex`) : ``QLocalServer.listen()`` y réussit toujours, même
    si une autre instance écoute déjà, il ne peut donc pas servir de verrou.
    Le canal local n'y est que le support du réveil.

    **Ailleurs (macOS, Linux)**, où ``listen()`` échoue bien si le fichier socket
    existe déjà, l'ordre des opérations à l'acquisition est :

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
    nettoyer, et l'OS libère le mutex Windows de lui-même.
    """

    #: Émis quand un autre lancement demande à remonter la fenêtre existante.
    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = _channel_name()
        self._server: QLocalServer | None = None
        #: Handle du mutex nommé (Windows uniquement), libéré par l'OS à la mort
        #: du processus même en cas de plantage.
        self._mutex = None

    # -- Acquisition / libération ----------------------------------------- #
    def acquire(self) -> bool:
        """Tente de devenir l'instance unique.

        Retourne ``True`` si l'application peut démarrer, ``False`` si une autre
        instance tourne déjà (elle a été réveillée si possible) : l'appelant doit
        alors s'arrêter.
        """
        if sys.platform == "win32":
            return self._acquire_windows()

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

    def _acquire_windows(self) -> bool:
        """Exclusivité sous Windows : **mutex nommé**, puis canal pour le réveil.

        Le test « on écoute donc on est seuls » est inopérant ici : Qt crée le
        pipe avec ``PIPE_UNLIMITED_INSTANCES`` et sans
        ``FILE_FLAG_FIRST_PIPE_INSTANCE``, donc ``listen()`` réussit toujours,
        même si une autre instance écoute déjà — et ``removeServer()`` n'y fait
        rien. Le mutex, lui, est **atomique** et l'OS le libère à la mort du
        processus (plantage compris) : aucun verrou orphelin.
        """
        if not self._acquire_mutex():
            # Une instance tourne déjà : on la réveille puis on rend la main.
            self._wake_existing()
            return False

        # Seuls au monde : le canal local ne sert plus qu'à recevoir les
        # prochaines demandes de réveil. Son ``listen`` ne peut pas échouer.
        server = QLocalServer(self)
        server.listen(self._name)
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def _acquire_mutex(self) -> bool:
        """Crée le mutex de session ; faux si une instance le détient déjà.

        ``bInitialOwner=False`` : on ne recherche pas la *propriété* du mutex
        (qui exigerait un ``ReleaseMutex``), seulement l'existence de l'objet
        tant que ce processus vit. Le handle n'est donc jamais « oublié ».
        Sans préfixe, le nom vit dans l'espace de la **session** : chaque
        session utilisateur (bureau à distance) garde bien son instance.
        """
        k32 = _kernel32()
        self._mutex = k32.CreateMutexW(None, False, self._name)
        if not self._mutex:
            return False  # création impossible : on refuse de démarrer
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(self._mutex)
            self._mutex = None
            return False
        return True

    def release(self) -> None:
        """Libère le canal et le mutex (fermeture propre, ou avant une relance).

        Idempotent. Sous Windows, fermer le handle libère le mutex : la nouvelle
        version peut alors prendre la main (voir ``updater``, qui libère le
        verrou avant de relancer).
        """
        if self._server is not None:
            self._server.close()
            self._server.setParent(None)  # l'objet Python reprend la propriété
            self._server = None
        # Pas de ``removeServer`` ici : fermer le canal suffit, et cela
        # supprimerait le canal d'une instance qui aurait repris la main.
        if self._mutex:
            _kernel32().CloseHandle(self._mutex)
            self._mutex = None

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


def badged_icon(base: QIcon, color: str = ALERT_COLOR) -> QIcon:
    """Dérive de ``base`` une icône portant une **pastille rouge** en bas à droite.

    Sert d'indicateur « du nouveau dans le Fil » quand la fenêtre est masquée :
    la zone de notification ne propose pas de badge, il faut donc changer
    l'icône elle-même. La pastille est peinte à chaque taille plutôt que
    redimensionnée, pour rester nette à 16 px (taille réelle du tray en mode
    compact) comme à 32 px (Windows à 150 %).

    Le dessin se fait en **pixels réels** (ratio de pixels neutralisé le temps de
    la peinture) : la pastille garde ainsi la même proportion quel que soit
    l'écran. Un liseré blanc la détache de l'icône, sombre par nature.

    Retourne une icône **nulle** si ``base`` est nulle : l'appelant garde alors
    l'icône d'origine (voir :meth:`TrayIcon.set_alert`).
    """
    result = QIcon()
    if base.isNull():
        return result
    for size in ALERT_SIZES:
        pixmap = base.pixmap(size, size)
        if pixmap.isNull():
            continue
        ratio = pixmap.devicePixelRatio() or 1.0
        image = pixmap.toImage()
        image.setDevicePixelRatio(1.0)  # peinture en pixels réels
        if image.isNull() or image.width() <= 0:
            continue

        real = float(image.width())
        dot = real * 0.46
        ring = max(1.0, real * 0.05)
        margin = max(1.0, real * 0.03)
        right = real - margin
        left = right - dot
        top = right - dot

        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(
            QRectF(left - ring, top - ring, dot + 2 * ring, dot + 2 * ring)
        )
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(left, top, dot, dot))
        painter.end()

        alert = QPixmap.fromImage(image)
        alert.setDevicePixelRatio(ratio)
        result.addPixmap(alert)
    return result


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
        on_message_clicked=None,
    ) -> None:
        super().__init__(parent)
        self._on_open = on_open

        # Icône au repos et variante « du nouveau » : la seconde est calculée une
        # seule fois, puis posée uniquement aux transitions (voir ``set_alert``).
        # Changer d'icône à chaque vérification ferait clignoter le tray et
        # laisserait des icônes fantômes dans Explorer.
        self._base_icon = icon
        self._alert_icon = badged_icon(icon)
        self._alert = False
        self._count = 0

        self._icon = QSystemTrayIcon(icon, self)
        self._icon.setToolTip(BASE_TOOLTIP)

        # Le menu doit rester référencé : ``setContextMenu`` n'en prend pas
        # possession.
        self._menu = QMenu(parent)
        self._menu.addAction("Ouvrir", on_open)
        self._menu.addAction("Réglages…", on_settings)
        self._menu.addSeparator()
        self._menu.addAction("Quitter", on_quit)
        self._icon.setContextMenu(self._menu)

        self._icon.activated.connect(self._on_activated)

        # Un clic sur la bannière (toast) doit mener quelque part : sans cela,
        # Windows la referme simplement et l'information est perdue.
        if on_message_clicked is not None:
            self._icon.messageClicked.connect(on_message_clicked)

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

    def set_alert(self, active: bool, count: int = 0) -> None:
        """Signale « du nouveau » par une **pastille rouge** sur l'icône.

        Idempotent et bon marché : l'icône n'est remplacée qu'au changement
        d'état (repos ↔ nouveau), l'infobulle est rafraîchie à chaque appel (elle
        porte le nombre). Sans variante disponible (icône de base illisible),
        seule l'infobulle change : mieux vaut cela qu'une icône nulle, qui
        ferait disparaître l'icône du tray.

        ``count`` : nombre de cas nouveaux, pour l'infobulle. ``0`` avec
        ``active=True`` reste valide (« du nouveau », sans décompte).
        """
        active = bool(active)
        count = max(0, int(count))
        self._count = count
        if active != self._alert:
            self._alert = active
            if not self._alert_icon.isNull():
                self._icon.setIcon(self._alert_icon if active else self._base_icon)
        self._icon.setToolTip(self._tooltip())

    def _tooltip(self) -> str:
        """Infobulle : nombre de nouveaux cas, ou état au repos."""
        if not self._alert:
            return BASE_TOOLTIP
        if self._count <= 0:
            return f"{BASE_TOOLTIP} — du nouveau dans le Fil"
        # Au-delà de 99, le décompte exact n'apporte rien (et l'infobulle
        # Windows est limitée en longueur).
        shown = str(self._count) if self._count < 100 else "99+"
        plural = "nouveaux cas" if self._count > 1 else "nouveau cas"
        return f"{BASE_TOOLTIP} — {shown} {plural}"

    def _on_activated(self, reason) -> None:
        """Clic sur l'icône : double-clic, ou clic simple (Windows)."""
        reasons = QSystemTrayIcon.ActivationReason
        if reason in (reasons.Trigger, reasons.DoubleClick):
            self._on_open()
