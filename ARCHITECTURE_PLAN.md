# 🎯 RadCase Manager — Architecture V3

Application de bureau légère permettant aux radiologues de cataloguer rapidement
des cas cliniques à partir d'images collées depuis le presse-papiers, dans un
**dossier réseau partagé**.

## 🧠 Décision d'architecture : fichiers plutôt que SQLite

La V1 utilisait une base SQLite unique posée sur le dossier partagé. **C'est un
anti-pattern** : les base SQLite sur partage réseau (SMB/NFS) ne peuvent pas
s'appuyer sur des verrous fiables, et une écriture concurrente depuis deux postes
peut **corrompre tout le fichier**.

La V3 exploite une caractéristique clé du besoin : **les cas sont figés**
(append-only, jamais modifiés). Dans ce contexte :

- Chaque nouveau cas = **un nouveau dossier au nom unique** → deux postes qui
  créent un cas en même temps écrivent dans deux dossiers différents →
  **aucun conflit de verrou**.
- Données **lisibles par un humain** et **sauvegardables** trivialement.
- Aucune migration de schéma, aucune dépendance serveur.

### Source de vérité

Un **dossier par cas**, contenant ses métadonnées `.md` et son image :

```
[Dossier Partagé]/
├── cases/
│   └── 2026-10/                        # sharding par mois
│       └── 20261008_174301_5709/       # id = YYYYMMDD_HHMMSS_xxxx
│           ├── case.md                 # frontmatter YAML + description
│           └── image.jpg               # JPEG (compressé, qualité IMAGE_QUALITY)
├── units.json                          # vocabulaire contrôlé des unités
├── users.json                          # liste partagée des utilisateurs connus
└── README.md
```

### Réglages locaux (jamais sur le partage)

Dans le **profil de l'utilisateur système**, donc propres à chaque compte machine
(plusieurs personnes peuvent partager le même `.exe` sans partager leurs réglages) :

```
Windows : %LOCALAPPDATA%\RadCaseManager\settings_data\
macOS   : ~/Library/Application Support/RadCaseManager/settings_data/
├── settings.json     # nom d'utilisateur (compte de session) + chemin du dossier partagé
└── index.db          # index local des cas (cache reconstructible)
```

Le nom d'auteur (`user_name`) est celui du **compte de session Windows** (nom de
connexion : `USERNAME`, repli `USER`/`LOGNAME`/`getpass`). Il est ajouté
automatiquement à la liste partagée `users.json` et n'est choisi que parmi les
utilisateurs connus (pas de saisie libre).

## 📄 Format d'un cas (`case.md`)

```markdown
---
id: 20261008_174301_5709
title: Scanner thoracique de routine
date_added: 2026-10-08T17:43:01
case_type: Standard
visibility: public
author: Dr Martin
units: [Thorax]
image: image.jpg
---

Description narrative, observations clés...
```

- `id` : `YYYYMMDD_HHMMSS_xxxx` (horodatage + 4 car. aléatoires) → unicité entre postes.
- `case_type` : `Standard` (défaut) / `Typique` / `Exceptionnel` (choix exclusif).
- `visibility` : `public` (défaut) ou `private`. Un cas **privé** n'apparaît dans
  la recherche que pour son auteur (`author`) ; il reste toutefois physiquement
  présent sur le dossier partagé (filtrage d'interface, pas de contrôle d'accès).
- `units` : tags libres issus de `units.json` (au moins une unité requise).
- `image` : nom **relatif** dans le dossier du cas → cas auto-contenu.
  **Obligatoire via l'interface** : on ne peut enregistrer (création ou édition)
  qu'avec une image présente.

## 🔒 Écriture atomique

```
1. Image     ← presse-papiers (QClipboard), ré-encodée en JPEG
2. id        ← horodatage + uuid
3. mkdir     cases/<YYYY-MM>/.tmp_<id>/
4. Écrire    image.jpg + case.md (UTF-8)
5. os.rename .tmp_<id>/  →  <id>/        ← atomique sur le même partage
6. En cas d'échec → suppression du dossier temporaire
```

Un cas n'existe que lorsque son dossier au **nom final** est complet. Les dossiers
`.tmp_*` sont ignorés au scan.

## ✏️ Édition

Un cas est éditable (onglet « Édition » dédié, ouvert par clic droit → « Modifier »).

- `id` et `date_added` sont **conservés** ; une clé `date_modified` est ajoutée.
- L'ancien dossier est envoyé à la **corbeille**, puis le nouveau est réécrit
  **à la même place** (même chemin, même `id`).
- Dernier écrivain gagne (pas de verrou) : l'édition est rare et non simultanée.
- En mode édition, l'**import automatique du presse-papiers est désactivé** pour
  ne pas écraser l'image en cours.

## 🖼️ Compression des images

L'image du presse-papiers est ré-encodée en **JPEG progressif** à
l'enregistrement (`storage.IMAGE_QUALITY` = 98, `storage.IMAGE_PROGRESSIVE`),
avec :

- **Qt seul** (`QImageWriter`, greffon `qjpeg` déjà collecté avec les autres
  greffons Qt du `.exe`) : aucune dépendance ajoutée ;
- **mode progressif** : mêmes pixels à qualité égale, ~20 % de poids en moins ;
- **alpha aplati sur fond blanc** avant encodage (JPEG n'a pas de canal alpha ;
  sans ça les zones transparentes virent au noir) ;
- **rétro-compatibilité totale** : les cas antérieurs gardent leur `image.png`
  (le nom réel est relu dans le frontmatter `image:`), et une édition sans
  nouvelle image (`KEEP_IMAGE`) conserve l'octet pour octet.

Mesures sur une capture type (1024×768, speckle + texte PACS), bitmap brut du
presse-papiers = 3 072 Ko :

| Encodage | Poids | PSNR zone texte |
|---|---|---|
| PNG (sans perte) | 648 Ko | ∞ |
| JPEG q92 | 209 Ko | 42,6 dB |
| **JPEG q98 progressif (retenu)** | **325 Ko** | **51,2 dB** |
| WebP sans perte | 376 Ko | ∞ |

Le choix de **q98** (plutôt que 92) vient de la lisibilité du **texte fin** :
cotes, flèches et annotations PACS sont ce qui souffre le plus en JPEG, et
l'écart est net (51 dB contre 42 dB). Le poids reste ~2 fois meilleur que le PNG,
et « seulement » ~1,5 fois celui du q92.

Deux points vérifiés à la mesure, qui évitent d'ajouter une dépendance pour rien :

- Qt encode **déjà en 4:4:4** (chroma pleine résolution, lu sur les marqueurs
  SOF) : les annotations colorées ne souffrent pas de sous-échantillonnage, donc
  `subsampling=0` de Pillow n'apporterait rien ;
- `setOptimizedWrite` n'apporte rien en mode progressif (Huffman déjà optimisé).

**Alternatives écartées** : WebP sans perte (zéro dégradation, mais fichiers
`.webp` moins universels hors de l'app et ~15 % plus lourds que q98) ; Pillow
(aucun gain restant sur Qt) ; PNG (zéro perte mais 2× plus lourd que q98).

⚠️ JPEG est **destructif** : adapté à un catalogue visuel, à éviter comme source
de mesure ou d'archivage de référence. Le gain dépend du contenu (typiquement 2 à
5 fois le PNG ; le speckle limite le gain).

## 🔎 Recherche

La recherche s'appuie sur un **index local** (`settings_data/index.db`), jamais
posé sur le partage réseau :

- Cache des métadonnées (titre, date, auteur, unités, description, chemin relatif).
- **Mise à jour incrémentale** : un `case.md` n'est re-parsé que si son `mtime`
  ou sa taille a changé (`os.scandir` + stat mise en cache).
- **Reconstructible** : si l'index est supprimé, corrompu, ou si le dossier
  partagé change, il est reconstruit automatiquement au prochain scan.
- Ordre de grandeur mesuré (1 000 cas, disque local) : scan froid ~0,5 s,
  scan chaud ~0,05 s, recherche filtrée ~0,05 s.
- **Anti-rebond des scans** : les lectures rapprochées (plusieurs onglets, badge,
  fil…) partagent un même résultat ; un scan complet de l'arbre n'est déclenché
  qu'au plus une fois toutes les `storage._REFRESH_TTL_SECONDS` secondes. Les
  écritures (création/édition/suppression) mettent l'index à jour directement et
  ne dépendent donc pas de ce délai.

Filtres : collection (👥 publique / 👤 privé / les deux), unités, auteur,
plage de dates, texte (titre + description).

⚠️ Aucun affichage par défaut : l'onglet « Consulter » ne liste rien tant qu'un
critère n'est pas saisi (évite de déverser des milliers de lignes).

## 🖥️ Affichage à l'échelle (vignettes + chargement progressif)

Objectif : rester fluide avec des milliers de cas, y compris sur un dossier
partagé réseau (2000+ cas visés).

- **Vignettes mises en cache** (`thumbnails.py`) : jamais d'image plein format
  décodée pour l'affichage. La vignette est produite une seule fois, à la taille
  d'affichage, via `QImageReader.setScaledSize` (décodage **directement réduit**),
  puis stockée dans le profil local (`settings_data/thumbs/`), jamais sur le
  partage. La clé inclut le `mtime` et la taille du fichier source : une image
  modifiée est recalculée automatiquement. Cache **reconstructible** (purge des
  plus anciennes au-delà de `THUMB_MAX_FILES`).
- **Chargement progressif** : la Bibliothèque et le Fil ne construisent que la
  fenêtre visible (paliers `BROWSER_PAGE` / `FEED_PAGE`), complétée au fil du
  défilement (remontée de l'historique pour le fil). La mémoire reste bornée,
  indépendamment du nombre de cas.
- **Recherche rapide** (Bibliothèque) : anti-rebond `QUICK_SEARCH_DEBOUNCE_MS`,
  pour ne pas reconstruire la grille à chaque frappe.

Mesures (213 cas, disque local, cache de vignettes chaud) : ouverture ~0,3 s et
changement d'onglet ~0,01–0,05 s (contre ~5 s et ~2,3 s avant). Premier lancement
(cache de vignettes vide) : ~1 s, le temps de produire les vignettes de la
fenêtre affichée.

## 🧩 Modules

| Module | Rôle |
|---|---|
| `config.py` | réglages locaux (`settings.json`), chemins |
| `bootstrap.py` | crée `cases/`, `units.json`, `users.json`, `README.md` si absents |
| `storage.py` | `create_case()`, `list_cases()`, `search_cases()` + index local |
| `thumbnails.py` | cache local des vignettes d'affichage (dérivées, reconstructible) |
| `main_gui.py` | interface PySide6 (saisie + consultation) |

## 🛠️ Dépendances

- `PySide6` (GUI + presse-papiers + encodage JPEG des images)
- `PyYAML` (frontmatter)
- `sqlite3` (stdlib, utilisé pour l'index local `settings_data/index.db`)

## 🏁 Roadmap V3

1. **Phase 1 — Fondation** : `config.py`, `bootstrap.py`, structure partagée.
2. **Phase 2 — Moteur** : `storage.py` (création atomique + lecture + recherche).
3. **Phase 3 — Interface** : `main_gui.py` (saisie, collage presse-papiers, consultation).
4. **Phase 4 — Distribution** : binaire portable Windows via PyInstaller.
