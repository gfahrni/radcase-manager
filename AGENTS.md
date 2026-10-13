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
  visibility, author, units, image) suivi de la description en markdown.
  `visibility` = `public` (défaut) ou `private` (masqué de la recherche des autres).
- **Pas de SQLite** (choix délibéré) : les cas sont **figés** (append-only), donc
  écriture dans un dossier temporaire `.tmp_<id>` puis `os.rename` **atomique**.
- Réglages locaux **par utilisateur système** dans `settings_data/settings.json`,
  sous `%LOCALAPPDATA%\RadCaseManager\` (Windows) ; jamais sur le partage. Le nom
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

## Conventions
- GUI en **PySide6** ; frontmatter en **PyYAML** ; capture image via **QClipboard**
  (Pillow n'est plus utilisé) ; encodage JPEG à l'enregistrement via Qt
  (`QImageWriter`, qualité `storage.IMAGE_QUALITY` = 98 + progressif) ;
  suppression via **send2trash**.
- L'onglet « Consulter » n'affiche **rien par défaut** (recherche à la demande),
  pour ne pas saturer avec des milliers de cas.
- Fermer la fenêtre **réduit** l'application dans la zone de notification
  (`close_to_tray`) ; un vrai arrêt se fait par « Quitter » (menu Fichier ou
  icône). Toute nouvelle voie de sortie doit passer par `MainWindow.quit_app`
  (qui libère le verrou mono-instance), pas par `app.quit` directement.
- Textes de l'UI et commentaires en **français**.
- Ne pas modifier `archive/` : ce sont des instantanés par version (`v1/`, `v2.1/`…).
  Le travail actif se fait à la racine du projet.

## Commandes
- Installer : `pip install -r requirements.txt` (environnement `.venv`).
- Lancer : `python main_gui.py`.
- Tester le backend sans GUI : voir `README.md`.

## Publication GitHub
- Dépôt distant : **`gfahrni/radcase-manager`** (branche `main`), public. La
  connexion passe par la CLI **`gh`** (compte `gfahrni`) : `gh auth setup-git`
  installe l'aide-mémoire d'identifiants, ensuite `git clone` / `git push`
  s'authentifient seuls.
- **Pas de `.git` local** dans le dossier du projet : on pousse la version
  actuelle depuis un **clone temporaire** du dépôt (synchroniser la racine
  dedans, puis commit + push sur `main`).
- Pousser **la racine seule** (fichiers du projet : `*.py`, `*.md`, `.github/`,
  etc.) ; **jamais `archive/`** (instantanés conservés en local uniquement).
- **Pas de tag** sauf demande explicite : un tag `v*` déclenche le build Windows
  (GitHub Actions) et crée une release.

## Vérifications
- Après modification d'un `*.py` : `python -m py_compile <fichier>`.
- PySide6 peut ne pas être installé sur la machine : garder la logique backend
  (`storage.py`, `bootstrap.py`, `config.py`) testable en CLI, sans GUI.
