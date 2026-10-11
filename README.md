# RadCase Manager (V3)

Application de bureau légère pour cataloguer des cas cliniques radiologiques à
partir d'images collées depuis le presse-papiers, dans un **dossier réseau
partagé**.

## Principe

- **Un cas = un dossier** dans le dossier partagé :
  `cases/<AAAA-MM>/<id>/case.md` + `image.png`.
- **Cas figés** (append-only) : on n'écrit jamais deux fois le même cas, ce qui
  évite tout conflit de verrou sur le partage réseau.
- **Écriture atomique** : dossier temporaire `.tmp_<id>` puis renommage.
- **Réglages locaux** dans `settings_data/settings.json` (jamais sur le partage).

Voir `ARCHITECTURE_PLAN.md` pour la justification détaillée (et pourquoi pas SQLite).

## Installation

```bash
python -m venv .venv
source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Lancement

```bash
python main_gui.py
```

Au premier lancement, une fenêtre demande votre nom et le chemin du dossier
partagé. Ce dossier est ensuite initialisé automatiquement (`cases/`,
`units.json`, `README.md`).

## Binaire portable Windows (.exe)

PyInstaller **ne compile pas de façon croisée** : l'exécutable Windows doit être
produit *sur Windows* (ou via une machine/CI Windows). Trois voies possibles :

### 1. Sur un poste Windows (le plus simple)

Double-cliquer sur `build_windows.bat` (Python 3.10+ 64 bits requis). Le script
crée `.venv`, installe les dépendances, puis génère :

```
dist\RadCaseManager.exe
```

Ce fichier unique est autonome : le copier où bon vous semble. Les réglages
(`settings_data/settings.json`) se créent automatiquement **à côté du `.exe`**.
Pour le déployer en réseau, le placer dans un dossier accessible en lecture par
les postes (le `.exe` n'a pas besoin d'être « installé »).

### 2. Construction manuelle (Windows)

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install "pyinstaller>=6.0"
pyinstaller --clean --noconfirm RadCaseManager.spec
```

La configuration de build est décrite dans `RadCaseManager.spec` (application
fenêtrée, `console=False`, exclusion des gros modules Qt inutilisés).

### 3. Sans machine Windows (GitHub Actions)

Le workflow `.github/workflows/build-windows.yml` construit l'exécutable sur un
runner Windows GitHub et le publie comme artefact téléchargeable. Il s'exécute
manuellement (onglet *Actions* → *Run workflow*) ou au push d'un tag `v*`.

## Structure du projet

| Fichier | Rôle |
|---|---|
| `config.py` | réglages locaux et chemins |
| `bootstrap.py` | initialisation du dossier partagé |
| `storage.py` | création atomique, lecture et recherche des cas |
| `main_gui.py` | interface PySide6 |

## Tests backend (sans interface)

Le module `storage` est utilisable directement :

```python
import storage
case = storage.create_case(
    title="Scanner thoracique",
    units=["Thorax", "Collection personnelle"],
    author="Dr Martin",
    description="Exemple.",
    image=None,
    shared_root="/chemin/vers/dossier/partage",
)
print(case.folder)
```

## Feuille de route

- [x] Fondation (structure + réglages)
- [x] Moteur de données (création atomique + recherche)
- [x] Interface (saisie + consultation)
- [x] Binaire portable Windows (PyInstaller) — voir `build_windows.bat`
