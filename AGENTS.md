# RadCase Manager — Instructions projet

## Contexte
Application de bureau (cible **Windows, exécutable portable**) permettant aux
radiologues de cataloguer des cas cliniques à partir d'images collées depuis le
presse-papiers, dans un **dossier réseau partagé**.

## Architecture (V3)
- Source de vérité = le dossier partagé, **un dossier par cas** :
  `cases/<AAAA-MM>/<id>/case.md` + `image.png`.
- `case.md` = frontmatter **YAML** (id, title, date_added, date_modified, case_type,
  visibility, author, units, image) suivi de la description en markdown.
  `visibility` = `public` (défaut) ou `private` (masqué de la recherche des autres).
- **Pas de SQLite** (choix délibéré) : les cas sont **figés** (append-only), donc
  écriture dans un dossier temporaire `.tmp_<id>` puis `os.rename` **atomique**.
- Réglages locaux **par utilisateur système** dans `settings_data/settings.json`,
  sous `%LOCALAPPDATA%\RadCaseManager\` (Windows) ; jamais sur le partage. Le nom
  d'auteur est prérempli avec le compte de la session Windows (modifiable).
- Justification complète dans `ARCHITECTURE_PLAN.md`.

## Modules
- `config.py` : réglages locaux + chemins (gère le mode PyInstaller « frozen »).
- `bootstrap.py` : init du dossier partagé (`cases/`, `units.json`, `README.md`).
- `storage.py` : `create_case()`, `update_case()`, `delete_case()`, `list_cases()`,
  `search_cases()`, plus l'**index local** (`settings_data/index.db`, cache
  reconstructible, maj par mtime).
- `main_gui.py` : interface PySide6 (onglets « Nouveau cas » / « Consulter »).

## Conventions
- GUI en **PySide6** ; frontmatter en **PyYAML** ; capture image via **QClipboard**
  (Pillow n'est plus utilisé pour la capture) ; suppression via **send2trash**.
- L'onglet « Consulter » n'affiche **rien par défaut** (recherche à la demande),
  pour ne pas saturer avec des milliers de cas.
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
