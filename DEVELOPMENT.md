# Développement

Documentation technique de RadCase Manager. Pour l'utilisation, voir le
[README](README.md).

## Prérequis

- **Python 3.10+** (64 bits) pour exécuter depuis les sources.
- **Windows** pour l'OCR natif (`Windows.Media.Ocr`) et pour produire
  l'exécutable : PyInstaller ne compile pas en croisé.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Lancer

```bash
python main_gui.py
```

Au premier lancement, une fenêtre demande le dossier partagé ; le nom d'auteur
est repris du compte de la session (ajouté à `users.json` s'il est inconnu). Le
dossier est ensuite initialisé automatiquement (`cases/`, `units.json`,
`users.json`, `README.md`).

## Tests backend (sans interface)

Le module `storage` s'utilise directement :

```python
import storage

case = storage.create_case(
    title="Scanner thoracique",
    units=["Thorax"],
    author="Dr Martin",
    description="Exemple.",
    image=None,
    shared_root="/chemin/vers/dossier/partage",
    visibility="public",   # ou "private" : visible du seul auteur
    ocr=True,              # Windows uniquement ; échec non bloquant
)
print(case.folder, case.ocr_status)
```

L'OCR est désactivé hors Windows (statut `unsupported`). Sur Windows, il dépend
du pack de langue OCR installé (`Paramètres → Langue → Reconnaissance optique de
caractères`) ; le français est utilisé lorsqu'il est disponible, sinon le
premier pack installé.

`updater` se teste aussi en ligne de commande :

```bash
python updater.py check       # version locale vs dernière release publiée
python updater.py download    # télécharge l'exécutable dans le dossier courant
```

## Construire l'exécutable Windows

### 1. Sur un poste Windows (le plus simple)

Double-cliquer sur `build_windows.bat` (Python 3.10+ 64 bits requis). Le script
crée `.venv`, installe les dépendances, puis génère `dist\RadCaseManager.exe`.

Ce fichier unique est autonome : le copier où bon vous semble. Les réglages, la
base d'index local et les vignettes se créent dans le profil de l'utilisateur
(`%LOCALAPPDATA%\RadCaseManager\`), jamais à côté du `.exe`.

### 2. Construction manuelle

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install "pyinstaller>=6.0"
pyinstaller --clean --noconfirm RadCaseManager.spec
```

La configuration de build est décrite dans `RadCaseManager.spec` (application
fenêtrée, `console=False`, exclusion des gros modules Qt inutilisés).

### 3. GitHub Actions

Le workflow `.github/workflows/build-windows.yml` construit l'exécutable sur un
runner Windows et le publie comme **release** au push d'un tag `v*` (ou comme
artefact téléchargeable en déclenchement manuel).

## Publier une version

```bash
git tag -a v6.11 -m "RadCase Manager v6.11"
git push origin v6.11
```

La CI inscrit le tag dans `version.py`, construit le `.exe`, calcule son
empreinte SHA-256, crée la release et y attache les deux fichiers. Un binaire de
développement (version contenant `dev`) ne cherche jamais de mise à jour.

## Structure du projet

| Fichier | Rôle |
|---|---|
| `config.py` | réglages locaux et chemins |
| `deployment.py` | défaut de site du dossier partagé (premier lancement) |
| `bootstrap.py` | initialisation du dossier partagé |
| `storage.py` | création atomique, lecture, recherche et index local |
| `reactions.py` | réactions anonymes du Fil (hors des dossiers de cas) |
| `ocr.py` | OCR natif Windows, en tâche de fond |
| `thumbnails.py` | cache local des vignettes d'affichage |
| `theme.py` | couleurs dérivées du thème Windows (clair / sombre) |
| `tray.py` | zone de notification et verrou mono-instance |
| `autostart.py` | démarrage automatique à l'ouverture de session |
| `updater.py` | mise à jour automatique depuis les releases GitHub |
| `version.py` | version de l'application (inscrite par la CI) |
| `main_gui.py` | interface PySide6 |

Les règles de maintenance (pièges Qt, verrou mono-instance, mise à jour, OCR…)
sont dans [AGENTS.md](AGENTS.md).

## Performances

Conçue pour rester fluide avec des milliers de cas sur un dossier réseau :

- le **scan de l'index** (parcours de `cases/`) tourne dans un thread dédié et
  toutes les lectures de l'interface interrogent l'index sans I/O réseau : la
  fenêtre ne se fige pas, la vue se rafraîchit à la fin du scan ;
- la **Bibliothèque** est une liste sans vignette : aucune image n'est décodée à
  l'ouverture (remplir 1 000 lignes prend quelques millisecondes) ;
- les images ne sont jamais décodées en plein format pour l'affichage : les
  vignettes sont calculées une fois et mises en cache localement
  (`settings_data/thumbs/`, reconstructible) ;
- le **Fil** ne construit que la fenêtre visible et complète au défilement ;
- l'index local n'est scanné qu'une fois par rafale de lectures (anti-rebond).

Ordre de grandeur (213 cas, disque local) : ouverture ~0,3 s, changement
d'onglet ~0,05 s.

## Documents

- [ARCHITECTURE_PLAN.md](ARCHITECTURE_PLAN.md) — architecture, formats, choix
  techniques (et pourquoi pas SQLite).
- [AUTO_UPDATE_PLAN.md](AUTO_UPDATE_PLAN.md) — mécanisme de mise à jour.
- [AGENTS.md](AGENTS.md) — consignes de maintenance.
