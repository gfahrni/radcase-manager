# -*- mode: python ; coding: utf-8 -*-
"""Spécification PyInstaller — RadCase Manager (binaire portable Windows).

Produit un exécutable autonome (mode « onefile ») à partir de ``main_gui.py``.
Les réglages locaux (``settings_data/``) sont créés dans le **profil de
l'utilisateur** (``%LOCALAPPDATA%\\RadCaseManager\\`` sous Windows), et non à
côté de l'exécutable : ``config.py`` résout ce chemin via ``os.environ``.

Génération (Windows) :

    pyinstaller --clean --noconfirm RadCaseManager.spec

Le plus simple est de passer par ``build_windows.bat``, qui installe d'abord
les dépendances dans un environnement virtuel.
"""

# ``SPECPATH`` est injecté par PyInstaller : racine du projet (là où se trouve
# ce fichier .spec), pour que l'analyse trouve les modules locaux quel que soit
# le répertoire courant.
pathex = [SPECPATH]

# OCR natif Windows : les paquets ``winrt-*`` contiennent des extensions
# natives (.pyd). On les collecte explicitement, car l'analyse statique de
# PyInstaller ne les voit pas toujours à travers les paquets-namespaces.
from PyInstaller.utils.hooks import collect_dynamic_libs

WINRT_BINARIES = collect_dynamic_libs("winrt")
WINRT_HIDDENIMPORTS = [
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.storage.streams",
]

# Modules embarqués par PySide6 mais inutilisés par l'application : on les
# exclut pour alléger nettement le binaire (WebEngine, Quick/QML et 3D pèsent
# à eux seuls plusieurs centaines de Mo). L'application n'utilise que
# QtCore / QtGui / QtWidgets (et QtNetwork pour le mode « barre d'état système »
# et le verrou mono-instance, voir ``tray.py``).
# En cas d'exécutable qui ne démarre plus après une évolution, commencer par
# vider cette liste avant de chercher ailleurs.
EXCLUDES = [
    "tkinter",
    # Web (QtWebEngine pèse ~150 Mo à lui seul)
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    # QML / Quick
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    # 3D
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    # Multimédia
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    # Graphiques / visualisation
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    # Divers non utilisés
    "PySide6.QtBluetooth",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtNfc",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
]

a = Analysis(
    ["main_gui.py"],
    pathex=pathex,
    binaries=WINRT_BINARIES,
    # L'icône est embarquée dans le binaire : en mode « onefile », elle est
    # extraite dans ``sys._MEIPASS`` et chargée au démarrage par
    # ``main_gui._app_icon()`` (barre de titre + barre des tâches).
    datas=[("icone_radcasemanager.ico", ".")],
    hiddenimports=WINRT_HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RadCaseManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # application fenêtrée : pas de console noire
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icone_radcasemanager.ico",
)
