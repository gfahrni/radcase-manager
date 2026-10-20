<p align="center">
  <img src="logo.png" alt="RadCase Manager" width="160">
</p>

# RadCase Manager (V3)

Application de bureau légère pour cataloguer des cas cliniques radiologiques à
partir d'images collées depuis le presse-papiers, dans un **dossier réseau
partagé**.

## Principe

- **Un cas = un dossier** dans le dossier partagé :
  `cases/<AAAA-MM>/<id>/case.md` + `image.jpg`.
- **Images compressées** : l'image collée est ré-encodée en **JPEG** (qualité
  `storage.IMAGE_QUALITY`, 98 par défaut) à l'enregistrement — typiquement 2 à
  5 fois plus léger qu'un PNG, sans perte visible. Aucune dépendance ajoutée :
  l'encodage est assuré par Qt. Les cas antérieurs (`image.png`) restent
  parfaitement lisibles (le nom du fichier est relu dans le `case.md`).
- **Cas figés** (append-only) : on n'écrit jamais deux fois le même cas, ce qui
  évite tout conflit de verrou sur le partage réseau.
- **Écriture atomique** : dossier temporaire `.tmp_<id>` puis renommage.
- **OCR natif Windows** : le texte de l'image (IPP, annotations…) est lu à
  l'enregistrement par `Windows.Media.Ocr`, sans moteur externe ni réseau. Le
  texte est stocké dans `case.md` (`ocr_text` / `ocr_status`) et ajouté à
  l'index local. La lecture et l'écriture se font dans un thread de fond : la
  fenêtre ne se fige jamais, et un échec OCR n'empêche pas l'enregistrement.
- **Numéro de demande** : extrait automatiquement du texte OCR (au moins
  10 lettres/chiffres consécutifs, préfixe `L`, `R` ou `E`, au moins 8 chiffres ;
  un `1` initial est corrigé en `L`), stocké dans `case.md`
  (`request_number`), affiché à droite du titre dans le plein écran et dans la
  Recherche Avancée, et exporté dans une colonne dédiée. Il est prérempli et
  corrigeable dans l'onglet Édition.
- **Dossier partagé par défaut (Windows)** :
  `N:/RAD/COMMUN/radcase-manager/DATABASE-RAD-CHUV`, modifiable dans les
  réglages.
- **Visibilité** : chaque cas est `public` (défaut) ou `privé`. Un cas privé
  n'apparaît dans la recherche que pour son auteur.
- **Réglages locaux** dans `settings_data/settings.json`, dans le profil de
  l'utilisateur système (`%LOCALAPPDATA%\RadCaseManager\` sous Windows) : jamais
  sur le partage, et propres à chaque compte machine.

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

Au premier lancement, une fenêtre demande le chemin du dossier partagé ; le nom
d'auteur est repris automatiquement du compte Windows de la session (ajouté à la
liste partagée s'il est inconnu). Le dossier est ensuite initialisé
automatiquement (`cases/`, `units.json`, `users.json`, `README.md`).

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
(`settings_data/settings.json`) et l'index local se créent automatiquement dans
le **profil de l'utilisateur** (`%LOCALAPPDATA%\RadCaseManager\`), et non à côté
du `.exe` : plusieurs comptes Windows peuvent donc partager le même `.exe` sans
partager leurs réglages. Le nom d'auteur est celui du compte Windows de la
session ; il est enregistré dans la liste partagée `users.json` et n'est choisi
que parmi les utilisateurs connus. Pour le déployer en réseau, le placer dans un
dossier accessible en lecture par les postes (le `.exe` n'a pas besoin d'être
« installé »).

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
manuellement (onglet *Actions* → *Run workflow*) ou au push d'un tag `v*`, auquel
cas il crée aussi une **release GitHub** avec le `.exe` attaché.

Exemple pour publier une version :

```bash
git tag -a v3.5 -m "RadCase Manager v3.5"
git push origin v3.5
```

## Mise à jour automatique

L'application installée se met à jour **d'elle-même** depuis les releases GitHub
(module `updater.py`) : les radiologues n'ont rien à télécharger ni à lire.

- Au démarrage, elle vérifie en tâche de fond la **dernière release publiée** (au
  plus une fois par jour) : le tag est lu via la redirection de
  `/releases/latest`, ce qui n'est soumis à aucun quota d'API.
- Si une version plus récente existe, elle propose de la télécharger (menu
  **Aide → Vérifier les mises à jour…** pour forcer la vérification).
- Le téléchargement **et** la sauvegarde transitent par
  `%LOCALAPPDATA%\RadCaseManager\updates\`, **hors du dossier de l'application** :
  le Bureau (ou le dossier du `.exe`) ne montre donc **jamais** plus d'une icône,
  même pendant le téléchargement.
- Le nouvel exécutable **prend la place** de l'ancien par **déplacement** (Windows
  interdit d'écraser un `.exe` en cours d'exécution, mais autorise à le renommer).
  L'ancien reste dans le dossier de transit comme **sauvegarde de retour arrière**,
  et n'est remplacé qu'à la mise à jour suivante — ce qui évite tout verrou de
  fichier. Aucun script externe n'est lancé : tout se passe dans le processus déjà
  autorisé à s'exécuter.
- Si un fichier de mise à jour traîne à côté de l'exécutable (reste des versions
  antérieures à la v4.2), il est **mis à la corbeille** au démarrage, avec des
  réessais différés : un antivirus peut tenir le fichier quelques instants.
- Si l'utilisateur répond « Plus tard », la proposition est **reportée d'un mois** ;
  un indicateur **« ⬆ Mise à jour disponible (vX) »** reste alors affiché en bas à
  droite — un clic relance la proposition. Une version encore plus récente l'emporte
  sur le report.

La version embarquée est **inscrite par la CI** dans `version.py` au moment du
build, à partir du tag. Un binaire de développement (version contenant `dev`, ou
exécution depuis les sources) ne vérifie jamais les mises à jour.

> Déploiement : l'exécutable est posé sur le **Bureau** de chaque utilisateur,
> seul emplacement où les applications sont autorisées à s'exécuter. Comme le
> Bureau est modifiable, l'application peut s'y remplacer elle-même sans droits
> administrateur. Voir `AUTO_UPDATE_PLAN.md`.

## Mode « barre d'état système » (zone de notification)

Fermer la fenêtre **n'arrête pas** l'application : comme la plupart des petites
applications portables, elle **se réduit dans la zone de notification** (le carré
à côté de l'horloge) et continue de tourner. Un clic sur son icône la rouvre ;

- **Ouvrir** — ramène la fenêtre au premier plan ;
- **Réglages…** — ouvre la configuration ;
- **Quitter** — arrête vraiment l'application (également dans **Fichier → Quitter**).

Au premier passage en zone de notification, une bulle explique que l'application
tourne toujours (l'icône peut être cachée derrière le « ^ » de Windows 11).

- Le comportement est **réglable** : *Réglages… → Fenêtre* ; décoché, fermer la
  fenêtre arrête l'application comme avant.
- **Un seul lancement à la fois** : si l'application tourne déjà, double-cliquer
  sur l'exécutable ne crée pas de second processus — il **réveille** la fenêtre
  existante. Cela évite deux icônes et deux écritures simultanées dans le dossier
  partagé. Le verrou est propre à chaque **compte machine** : sur un poste utilisé
  par plusieurs personnes (sessions simultanées), chacun a bien la sienne. Sous
  Windows, l'exclusivité est garantie par un **mutex nommé** libéré par le système
  à l'arrêt du processus (le canal local ne sert qu'au réveil : `listen()` y
  réussit toujours, il ne peut pas faire office de verrou).

## Démarrage automatique à l'ouverture de session

Pour qu'un poste soit prêt sans que personne ait à y penser, l'application peut se
**lancer toute seule après un redémarrage** — *Réglages… → Démarrage* (activée par
défaut). Elle démarre alors **réduite dans la zone de notification** : prête à
recevoir une image collée, sans imposer sa fenêtre à l'ouverture de session.

- Techniquement, l'application s'inscrit dans la clé **`Run` du compte courant**
  (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`), la même que celle listée
  par Windows dans « Applications de démarrage » : **aucun droit administrateur** ;
- l'entrée est **réécrite à chaque lancement** avec le chemin courant de
  l'exécutable : déplacer le `.exe` ne casse pas le démarrage automatique ;
- elle vaut pour le **compte Windows** concerné, pas pour tout le poste ; décocher
  l'option retire l'entrée (l'application n'est plus lancée automatiquement).

Voir `autostart.py`.

## Structure du projet

| Fichier | Rôle |
|---|---|
| `config.py` | réglages locaux et chemins |
| `bootstrap.py` | initialisation du dossier partagé |
| `storage.py` | création atomique, lecture et recherche des cas |
| `ocr.py` | OCR natif Windows, en tâche de fond (`Windows.Media.Ocr`) |
| `thumbnails.py` | cache local des vignettes d'affichage |
| `tray.py` | zone de notification (icône + menu) et verrou mono-instance |
| `autostart.py` | inscription au démarrage de Windows (clé `Run` du compte) |
| `updater.py` | mise à jour automatique depuis les releases GitHub |
| `version.py` | version de l'application (inscrite par la CI au build) |
| `main_gui.py` | interface PySide6 |

## Performances

Conçue pour rester fluide avec des **milliers de cas** sur un dossier réseau :

- les images ne sont **jamais** décodées en plein format pour l'affichage : des
  vignettes sont calculées une fois à la taille d'affichage et mises en cache
  localement (`settings_data/thumbs/`, reconstructible, hors du partage) ;
- la Bibliothèque et le Fil ne construisent que la **fenêtre visible** et
  complètent au défilement (mémoire bornée) ;
- l'index local n'est scanné qu'**une fois par rafale** de lectures (anti-rebond),
  et les auteurs/unités sont lus par requête SQL ciblée.

Ordre de grandeur (213 cas, disque local) : ouverture ~0,3 s, changement d'onglet
~0,05 s. Détails dans `ARCHITECTURE_PLAN.md`.

## Tests backend (sans interface)

Le module `storage` est utilisable directement :

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

Le module `updater` se teste également en ligne de commande :

```bash
python updater.py check       # version locale vs dernière release publiée
python updater.py download    # télécharge l'exécutable dans le dossier courant
```

## Feuille de route

- [x] Fondation (structure + réglages)
- [x] Moteur de données (création atomique + recherche)
- [x] Interface (saisie + consultation)
- [x] Binaire portable Windows (PyInstaller) — voir `build_windows.bat`
- [x] Mise à jour automatique depuis les releases GitHub — voir `updater.py` et
  `AUTO_UPDATE_PLAN.md`
- [x] Mode « barre d'état système » (réduction dans la zone de notification) et
  verrou mono-instance — voir `tray.py`
- [x] Lancement automatique à l'ouverture de session Windows (réduit dans la zone
  de notification) — voir `autostart.py`
- [x] OCR natif Windows à l'enregistrement, en tâche de fond — voir `ocr.py`
