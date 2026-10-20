# RadCase Manager — Instructions projet

## Contexte
Application de bureau (cible **Windows, exécutable portable**) permettant aux
radiologues de cataloguer des cas cliniques à partir d'images collées depuis le
presse-papiers, dans un **dossier réseau partagé**.

## Architecture (V3)
- Source de vérité = le dossier partagé, **un dossier par cas** :
  `cases/<AAAA-MM>/<id>/case.md` + `image.jpg` (JPEG ré-encodé à
  l'enregistrement, qualité `storage.IMAGE_QUALITY` ; les cas historiques
  peuvent contenir un `image.png`).
- `case.md` = frontmatter **YAML** (id, title, date_added, date_modified, case_type,
  visibility, author, units, image, `request_number`, et si disponible
  `ocr_text` / `ocr_status`) suivi de la description en markdown.
  `visibility` = `public` (défaut) ou `private` (masqué de la recherche des autres).
  Le texte OCR (IPP, annotations…) est écrit **avant** le renommage atomique : un
  cas visible est toujours complet. Le **numéro de demande** est déduit de ce
  texte (au moins 10 lettres/chiffres consécutifs, commençant par `L`, `R` ou
  `E`, avec au moins 8 chiffres ; un `1` initial est corrigé en `L`) et peut
  être corrigé dans l'onglet Édition.
- **Pas de SQLite** (choix délibéré) : les cas sont **figés** (append-only), donc
  écriture dans un dossier temporaire `.tmp_<id>` puis `os.rename` **atomique**.
- Réglages locaux **par utilisateur système** dans `settings_data/settings.json`,
  sous `%LOCALAPPDATA%\RadCaseManager\` (Windows) ; jamais sur le partage. Le
  dossier partagé proposé par défaut est
  `N:/RAD/COMMUN/radcase-manager/DATABASE-RAD-CHUV` (`config.DEFAULT_SHARED_ROOT`),
  modifiable dans les réglages. Le nom
  d'auteur est celui du **compte de session Windows** (nom de connexion) ; il est
  ajouté automatiquement à `users.json` et choisi uniquement parmi les
  utilisateurs connus (pas de saisie libre).
- Justification complète dans `ARCHITECTURE_PLAN.md`.

## Modules
- `config.py` : réglages locaux + chemins (gère le mode PyInstaller « frozen »).
- `bootstrap.py` : init du dossier partagé (`cases/`, `units.json`, `users.json`,
  `README.md`).
- `storage.py` : `create_case()`, `update_case()`, `delete_case()`, `list_cases()`,
  `search_cases()`, plus l'**index local** (`settings_data/index.db`, cache
  reconstructible, maj par mtime). Scan de l'arbre **anti-rebondi** (TTL) pour
  qu'une rafale de lectures ne déclenche qu'un seul scan.
- `ocr.py` : **OCR natif Windows** (`Windows.Media.Ocr`, paquets `winrt-*`),
  sans modèle ni appel réseau. Import paresseux, dans un **thread dédié et
  persistant** (WinRT fige le mode d'appartement du premier thread : ne pas
  créer un thread par enregistrement). L'API est *best-effort* : statut
  `ok` / `empty` / `unsupported` / `failed` / `timeout`, jamais d'exception.
  Le texte est calculé pendant `storage.create_case()` / `update_case()`, donc
  dans le worker de sauvegarde, jamais dans le thread de l'interface.
- `thumbnails.py` : cache local des **vignettes d'affichage**
  (`settings_data/thumbs/`, reconstructible, jamais sur le partage). Décodage à
  la taille cible via `QImageReader.setScaledSize` : le plein format n'est jamais
  décodé pour l'affichage.
- `tray.py` : **zone de notification** (icône + menu Ouvrir / Réglages / Quitter)
  et **verrou mono-instance**. Fermer la fenêtre **réduit** l'application
  (réglage `close_to_tray`, défaut `True`) ; l'arrêt réel passe par « Quitter ».
  Le verrou est un **canal local nommé** (`QLocalServer`/`QLocalSocket`, nom
  portant le compte de session) : un second lancement réveille la fenêtre
  existante (accusé de réception ⇒ elle est vivante) puis s'arrête. **Pas de
  `QLockFile`** (verrou orphelin après plantage) et **pas de
  `setSocketOptions()`** : sur Unix (Qt 6.12), poser une option fait répondre
  « succès » au second `listen()` — les deux instances se croient seules.
  **Sous Windows**, le canal ne peut pas servir de verrou : `QLocalServer.listen()`
  y **réussit toujours** (Qt crée le pipe avec `PIPE_UNLIMITED_INSTANCES`, sans
  `FILE_FLAG_FIRST_PIPE_INSTANCE`, et `removeServer()` y est un no-op) — chaque
  lancement créait donc une instance de plus (deux icônes). L'exclusivité y est
  assurée par un **mutex nommé** (`CreateMutexW`, `_acquire_windows`) : atomique,
  dans l'espace de la session, **libéré par l'OS à la mort du processus** (donc
  sans le verrou orphelin qui avait fait écarter `QLockFile`). Le canal n'y sert
  plus qu'au réveil.
- `autostart.py` : **démarrage automatique à l'ouverture de session** (réglage
  `start_with_windows`, défaut `True`), via la clé `Run` du **compte courant**
  (`HKCU\…\CurrentVersion\Run`, aucun droit administrateur). L'entrée est
  **réécrite à chaque démarrage** (`sync`) : déplacer l'exécutable portable ne
  casse pas le lancement. Elle porte l'argument `--minimized` — `main_gui.main`
  le retire avant `QApplication` et démarre alors **réduit dans la zone de
  notification**. Sans effet hors Windows ou pour un binaire non « frozen »
  (`is_supported`).
- `updater.py` : **mise à jour automatique** depuis les releases GitHub (vérif en
  fond 1×/24 h, téléchargement, remplacement de l'exécutable par **renommage** —
  jamais d'écrasement d'un `.exe` en cours). Tout le transit (`.new`, `.part`,
  `.old`) vit dans `%LOCALAPPDATA%\RadCaseManager\updates\` (`config.UPDATES_DIR`),
  **jamais** dans le dossier de l'exécutable, pour qu'une seule icône soit visible.
  Les restes visibles à côté de l'exécutable partent à la **corbeille**
  (`send2trash`), avec réessais différés après le démarrage.
  Réglages associés : `auto_check_updates`, `last_update_check` (anti-rebond 24 h),
  `available_update` + `update_snooze_until` / `update_snooze_version` (rapport
  d'un mois après « Plus tard », indicateur persistant en barre de statut).
  **La relance pose `PYINSTALLER_RESET_ENVIRONMENT=1` et purge `_PYI_*` / `_MEI*`** :
  sans cela PyInstaller fait hériter son environnement (même chemin d'exécutable ⇒
  il croit au même programme) et la nouvelle version tourne dans le `_MEI` de
  l'ancienne — images introuvables et « Failed to remove temporary directory ».
  Ne pas retirer ce nettoyage.
  Aucune dépendance externe (hors `send2trash`, déjà requis).
  **La mise à jour libère le verrou mono-instance avant de relancer**
  (`MainWindow._apply_update`) : sinon la nouvelle version se croirait « seconde
  instance » et sortirait aussitôt. Ne pas retirer cette libération.
- `version.py` : version de l'application, **inscrite par la CI** au build à partir
  du tag ; un binaire de développement (``…dev``) désactive la mise à jour.
- `main_gui.py` : interface PySide6 (onglets « Nouveau cas » / « Consulter »).
  Bibliothèque et Fil en **chargement progressif** (seule la fenêtre visible est
  construite) ; recherche rapide avec anti-rebond.
  Création et édition passent par `SaveCaseWorker` / `UpdateCaseWorker` dans un
  `QThread` : l'OCR et l'écriture disque ne figent pas l'interface. Un seul
  enregistrement à la fois ; l'arrêt est différé si un worker tourne.

## Conventions
- GUI en **PySide6** ; frontmatter en **PyYAML** ; capture image via **QClipboard**
  (Pillow n'est plus utilisé) ; encodage JPEG à l'enregistrement via Qt
  (`QImageWriter`, qualité `storage.IMAGE_QUALITY` = 98 + progressif) ;
  suppression via **send2trash**.
- L'onglet « Consulter » n'affiche **rien par défaut** (recherche à la demande),
  pour ne pas saturer avec des milliers de cas.
- OCR : réglage local `ocr_enabled` (défaut `True`). Le texte est dans
  `case.md` et dans l'index local ; `search_cases(..., include_ocr=True)` permet
  de l'inclure dans la recherche, désactivé par défaut. Aucun OCR dans l'export
  des résultats. Les anciens cas restent sans texte jusqu'à un rattrapage.
- Numéro de demande : `request_number` dans `case.md` et l'index. Règle :
  10+ caractères alphanumériques consécutifs, préfixe `L`/`R`/`E`, au moins
  8 chiffres, `1` initial corrigé en `L`. Affiché à droite du titre dans le
  plein écran et dans la Recherche Avancée ; colonne **« N° de demande »** dans
  l'export CSV/Excel. Le champ est prérempli et modifiable dans l'onglet
  Édition ; une valeur vide explicite fait foi.
- Fermer la fenêtre **réduit** l'application dans la zone de notification
  (`close_to_tray`) ; un vrai arrêt se fait par « Quitter » (menu Fichier ou
  icône). Toute nouvelle voie de sortie doit passer par `MainWindow.quit_app`
  (qui libère le verrou mono-instance), pas par `app.quit` directement.
- Textes de l'UI et commentaires en **français**.
- `archive/` ne contient plus d'instantanés de code : les versions antérieures sont
  récupérables par leurs **tags** et **releases** GitHub (voir `archive/README.md`,
  qui conserve le changelog par version). Ne pas y redéposer de copies du projet ;
  `archive/` est désormais dans `.gitignore`. Le travail actif se fait à la racine.

## Commandes
- Installer : `pip install -r requirements.txt` (environnement `.venv`).
- Lancer : `python main_gui.py`.
- Tester le backend sans GUI : voir `README.md`.

## Publication GitHub
- Dépôt distant : **`gfahrni/radcase-manager`** (branche `main`), public.
- **Deux moyens de s'authentifier, par ordre de préférence :**
  1. **`gh`** (compte `gfahrni`) : `gh auth setup-git` installe l'aide-mémoire
     d'identifiants, ensuite `git clone` / `git push` s'authentifient seuls.
     Vérifier au préalable `gh auth status` : le jeton expire, et git échoue
     alors sur `could not read Username for 'https://github.com'` (la config
     locale fait passer HTTPS par `!gh auth git-credential`).
  2. **SSH** (aucun jeton, aucun secret à manipuler) : la machine a une clé
     `~/.ssh/id_ed25519` enregistrée sur le compte GitHub. Vérifier avec
     `ssh -T git@github.com` → doit répondre *« Hi gfahrni! »*, puis passer le
     dépôt en URL SSH :
     `git remote set-url origin git@github.com:gfahrni/radcase-manager.git`.
     C'est la solution de repli quand le jeton `gh` est invalide.
  La **création de la release** ne demande pas d'authentification locale : le
  workflow utilise son propre `GITHUB_TOKEN` (voir `.github/workflows/`).
- **Pas de `.git` local** dans le dossier du projet : on pousse la version
  actuelle depuis un **clone temporaire** du dépôt (synchroniser la racine
  dedans, puis commit + push sur `main`).
- Pousser **la racine seule** (fichiers du projet : `*.py`, `*.md`, `.github/`,
  etc.) ; **jamais `archive/`** (plus d'instantanés sur le disque : les versions
  antérieures vivent sur GitHub, récupérables par tag ou release. Le dossier est
  inscrit dans `.gitignore` et ne contient plus que le changelog
  `archive/README.md`, conservé en local).
- **Pas de tag** sauf demande explicite : un tag `v*` déclenche le build Windows
  (GitHub Actions) et crée une release.

## Vérifications
- Après modification d'un `*.py` : `python -m py_compile <fichier>`.
- PySide6 peut ne pas être installé sur la machine : garder la logique backend
  (`storage.py`, `bootstrap.py`, `config.py`) testable en CLI, sans GUI.
