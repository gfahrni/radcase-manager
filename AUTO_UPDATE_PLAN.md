# Mise à jour automatique — étude de faisabilité

Objectif : que les postes des radiologues se mettent à jour **sans action de leur
part** et sans lecture de mail, en récupérant le `.exe` publié dans les
**releases GitHub** de `gfahrni/radcase-manager`.

## Verdict

**Faisable, et pas très difficile.** Un module d'auto-update « maison » représente
environ **200 à 280 lignes** et **~1 jour de travail**, sans nouvelle dépendance
(on reste sur la bibliothèque standard). Aucune restriction de Windows ne bloque
l'approche : elle **ne demande pas de droits administrateur** et ne touche ni au
registre ni à `Program Files`.

Il n'y a qu'**un vrai point de vigilance** : le déploiement en entreprise
(AppLocker / WDAC / antivirus). Voir §6. Le reste est de la mécanique maîtrisée.

Recommandation : commencer par un **MVP** (phase 1) qui télécharge en fond et
applique au redémarrage suivant, avec repli « notification » si l'exécutable
n'est pas modifiable sur place.

> **État : implémenté** (option B, §10/§11). Le code se trouve dans `updater.py`
> (logique, sans GUI), `version.py` (version injectée par la CI) et `main_gui.py`
> (menu « Aide », vérification de fond, téléchargement, application). Voir §13.

---

## 1. Ce dont on dispose déjà (bons points)

- Dépôt **public** → téléchargement du `.exe` sans authentification.
- Chaque release porte **un seul asset**, toujours nommé `RadCaseManager.exe` →
  l'URL de téléchargement est **constructible** :
  `https://github.com/gfahrni/radcase-manager/releases/download/<tag>/RadCaseManager.exe`
- Le workflow `build-windows.yml` crée déjà la release **au push d'un tag `v*`**
  → il suffit de taguer pour publier, exactement ce qu'il faut.
- Le binaire est un **onefile portable** → il n'y a **qu'un seul fichier** à
  remplacer. Pas d'installation, pas de dépendances externes, pas de registre.
- Les réglages vivent dans `%LOCALAPPDATA%\RadCaseManager\settings_data\` : la
  mise à jour du `.exe` **ne touche pas** aux données ni aux réglages.

## 2. Principe retenu

```
lancement de l'app
      │
      ├─ (1) y a-t-il un .exe déjà téléchargé en attente ?  ── oui ──> §4 : on l'applique
      │
      ├─ (2) sinon : afficher l'UI normalement, puis dans un THREAD de fond
      │        • au plus 1 vérification / 24 h (horodatage dans settings.json)
      │        • lire la dernière version publiée (requête légère, sans quota)
      │        • si version publiée > version locale : télécharger le .exe en fond
      │          vers  %LOCALAPPDATA%\RadCaseManager\updates\
      │        • barre de statut discrète : « Mise à jour prête, elle s'appliquera
      │          au prochain démarrage » (+ entrée de menu pour forcer)
      │
      └─ (3) au redémarrage suivant → §4 applique, puis l'app repart en version N+1
```

Le téléchargement se fait **pendant que le radiologue travaille** ; la bascule au
**redémarrage suivant**. C'est exactement le comportement que tu décris.

## 3. Détecter la dernière version (sans quota)

Deux méthodes :

| Méthode | Avantage | Inconvénient |
|---|---|---|
| **API** `GET /repos/…/releases/latest` | JSON propre (tag + assets + URL) | quota non authentifié **60 req/h/IP** → risqué si tout le service partage une IP publique |
| **Redirection du site** `HEAD /releases/latest` | **aucun quota** (ce n'est pas l'API), réponse = `302 Location: …/releases/tag/v3.12` | il faut parser une URL, et construire soi-même l'URL de l'asset |

**Recommandé : la redirection** (vérifié : répond bien un `302` renvoyant le tag),
avec l'API en repli. La version se lit donc dans le tag (`v3.12` → `3.12`).

Le téléchargement pointe ensuite directement vers l'asset de la release, avec un
`User-Agent` explicite.

> Note réseau : `urllib` (stdlib) utilise le **magasin de certificats Windows**
> pour la validation TLS — il passe donc les proxys d'entreprise qui font de
> l'inspection TLS, contrairement à une pile qui embarque `certifi`. Il respecte
> aussi `HTTP_PROXY` / `HTTPS_PROXY`. Pas besoin d'ajouter `requests`.

## 4. Le point délicat : remplacer un `.exe` en cours d'exécution

Sous Windows on **ne peut pas écraser** un exécutable en cours d'exécution… mais
on **peut le renommer**. C'est toute l'astuce.

### Variante retenue — auto-renommage au démarrage (pas de script externe)

1. Le téléchargement est écrit dans `updates\RadCaseManager.exe.new`.
2. Au lancement suivant, **avant** d'afficher la fenêtre :
   - `os.rename(exe, exe + ".old")` — autorisé même sur le `.exe` en cours ;
   - `os.rename(exe + ".new", exe)` ;
   - `subprocess.Popen([exe])` (nouvelle version) puis `sys.exit()` ;
   - le `.old` (encore verrouillé par le process courant) est supprimé au
     démarrage d'après, à titre de nettoyage ; il sert aussi de **sauvegarde de
     secours** (rollback en cas de `.new` corrompu).

Avantage **important** : aucun `.bat`/`.ps1` temporaire n'est lancé, or les
politiques d'entreprise (AppLocker/SRP) bloquent souvent l'exécution de scripts
depuis `%TEMP%`. Tout se fait **dans le processus**.

### Variante de repli — helper détaché

Un `cmd.exe` détaché qui attend la fin du PID puis fait les `move /Y` et
relance. Utile si l'auto-renommage se heurte à un antivirus tatillon, mais moins
élégant et bloquable par politique. À garder en second recours.

Dans les deux cas, si le dossier de l'exécutable **n'est pas modifiable**
(partage réseau en lecture seule, voir §6), on ne tente rien et on se contente
d'un message « nouvelle version disponible ».

## 5. Ce qu'il faut modifier / ajouter

| Fichier | Changement |
|---|---|
| `version.py` *(nouveau)* | `APP_VERSION = "0.0.0.dev"` par défaut ; **écrit par la CI** avec le tag |
| `config.py` | importer `APP_VERSION` (fallback si absent) + 3 réglages : `last_update_check`, `update_url` (proxy éventuel), `auto_update` (on/off) |
| `updater.py` *(nouveau)* | lecture de la version distante, comparaison, téléchargement, staging, application (toute la logique, testable en CLI) |
| `main_gui.py` | thread de vérification post-démarrage, entrée de menu « Aide → Vérifier les mises à jour », barre de statut |
| `.github/workflows/build-windows.yml` | injecter `version.py` depuis `GITHUB_REF_NAME` avant PyInstaller ; joindre un `.sha256` à la release |
| `RadCaseManager.spec` | rien (le nouveau module est un `.py` normal, embarqué automatiquement) |

Détail CI :

```yaml
- name: Injecter la version
  run: python -c "import os;open('version.py','w',encoding='utf-8').write('APP_VERSION=%r\n'%os.environ['GITHUB_REF_NAME'])"
- name: Empreinte SHA-256
  run: python -c "import hashlib,pathlib;p=pathlib.Path('dist/RadCaseManager.exe');print(hashlib.sha256(p.read_bytes()).hexdigest()+'  RadCaseManager.exe')" > dist/RadCaseManager.exe.sha256
```

(Ces commandes ne modifient que le clone de travail de la CI, jamais le dépôt.)

## 6. Restrictions Windows réelles — ce qu'il faut savoir

Rien de rédhibitoire, mais trois sujets à connaître :

1. **Signature de code (SmartScreen / antivirus).** L'exécutable n'est pas
   signé. Un remplacement programmatique ne déclenche **pas** SmartScreen (pas de
   « marque du web » sur un téléchargement HTTP direct, contrairement à un
   téléchargement via navigateur). En revanche, un antivirus d'entreprise peut
   trouver suspect un programme qui **remplace son propre binaire** — c'est un
   comportement typique de logiciel malveillant. C'est le principal risque
   d'échec en milieu hospitalier. Mitigations : signature de code (certificat
   OV/EV, ou **SignPath.io gratuit pour l'open source**), ou mise en liste
   blanche par l'IT.
2. **AppLocker / WDAC / SRP.** Certaines DSI n'autorisent l'exécution que
   depuis des chemins approuvés. Si c'est le cas, exécuter depuis
   `%LOCALAPPDATA%` ou `%TEMP%` peut être **refusé** → l'auto-update échoue. La
   **mise à jour sur place** (même chemin déjà autorisé) est alors la plus sûre.
   **À vérifier auprès de l'IT avant de coder** : c'est le seul point qui peut
   réellement bloquer.
3. **Dossier partagé en lecture seule.** Le `README` recommande de poser le
   `.exe` sur un partage réseau en lecture. Dans ce cas on **ne peut pas** le
   remplacer → prévoir un repli :
   - soit simple notification (« une nouvelle version est disponible ») ;
   - soit **bascule vers une copie locale** : au premier lancement, l'app copie
     l'exécutable dans `%LOCALAPPDATA%\RadCaseManager\app\` et travaille à partir
     de là (elle devient auto-modifiable). Cela suppose un raccourci qui pointe
     vers la copie locale — petite concession de déploiement, à décider.
   - Important : un `.exe` partagé par plusieurs utilisateurs sur un partage
     **ne doit pas** s'auto-modifier en place (conflits de verrou).

Bonne nouvelle : **aucun privilège administrateur n'est requis**, ce qui est
l'énorme avantage du mode portable et ce qui rend l'auto-update viable là où un
installeur MSI ne le serait pas.

## 7. Pourquoi pas un « patch » (delta binaire)

Le `.exe` pèse ~51 Mo. Un vrai delta (`bsdiff`, `zstd --patch-from`) ramènerait
le transfert à ~5-15 Mo. Mais : il faut une **version de base exacte** connue,
une dépendance supplémentaire côté client, une chaîne de génération de patches
par release, et beaucoup plus de cas d'échec pour un gain de temps négligeable
(téléchargement en tâche de fond). **Conclusion : télécharger l'exécutable
complet**, c'est plus simple et plus robuste. Le mode « patch » ne se justifie
que si la bande passante devient un problème mesuré.

## 8. Alternatives « toutes faites »

- **`tufup`** (maintenu, basé sur TUF) : plus sûr (signatures des mises à jour),
  mais plusieurs étapes de configuration (clés, dépôt de métadonnées, bootstrap
  du client). Pertinent si la sécurité de la chaîne de mise à jour devient un
  critère. **Surdimensionné pour ici.**
- **`pyupdater`** : **plus maintenu** → à écarter.

Pour ce projet, **une implémentation maison de ~250 lignes est le bon choix** :
périmètre restreint, zéro dépendance, tout est lisible et testable.

## 9. Effort et découpage

| Phase | Contenu | Effort |
|---|---|---|
| **1 — MVP** | `version.py` + CI + vérif en fond (1×/24 h) + téléchargement + application au redémarrage si l'exe est modifiable + menu manuel | ~1 jour |
| **2 — Durcissement** | vérification SHA-256, nettoyage/rollback du `.old`, bandeau de statut soigné, réglage on/off, proxy | ~0,5 jour |
| **3 — Optionnel** | signature de code, repli « copie locale » pour partage en lecture seule, canal « bêta » | selon décision |

Testabilité : `updater.py` (lecture de version, comparaison, téléchargement,
staging) est **testable sur macOS**. Seule la bascule finale du `.exe` est
spécifique à Windows ; à valider sur un poste Windows ou via la CI.

## 10. Zip, bouton manuel ou versionnage ?

**Le zip n'enlève pas la difficulté.** Le point délicat reste Windows, qui refuse
d'écraser un `.exe` en cours d'exécution (cf. §4) ; que la nouvelle version arrive
en `.exe` direct ou en `.zip`, il faut **quand même** remplacer un fichier en
train de tourner. Un zip, c'est donc l'exécutable direct **plus une étape de
décompression** : autant télécharger le `.exe` seul (c'est tout l'intérêt du
onefile).

Trois variantes, du plus simple au plus « pro » :

| Option | Mécanique | Effort | Ressenti utilisateur |
|---|---|---|---|
| **A. Fond + applique au redémarrage** | téléchargement silencieux pendant le travail, bascule au prochain lancement | ~1 j | zéro clic ; l'ancienne version tourne encore jusqu'au redémarrage |
| **B. Bouton « Mettre à jour »** *(recommandé)* | clic → télécharge → un **relais** attend la fermeture, remplace, **relance automatiquement** | ~1 j | un seul clic ; l'app se ferme et redémarre en N+1 |
| **C. Dossiers versionnés** | décompresse dans `…\app\3.13\`, un mini-lanceur pointe vers la plus récente | ~1,5 j | très robuste, **aucun écrasement**, rollback instantané ; exige un lanceur stable |

Seule l'option **C** justifie un zip : comme on installe **à côté** (jamais par
écrasement), on peut livrer un dossier par version. En contrepartie il faut un
**point d'entrée stable** (raccourci vers `…\app\current\`, à la manière de Chrome
/ VS Code), donc un petit changement de déploiement.

Recommandation : **option B**, éventuellement complétée par la vérification de
fond de l'option A (pour proposer « une mise à jour est disponible » sans rien
forcer). L'utilisateur ne relance jamais à la main : le relais s'en charge.

## 11. Contrainte de déploiement : le `.exe` sur le Bureau

Contexte réel : **seul le Bureau des utilisateurs autorise l'exécution
d'applications** ; on ne peut y déposer qu'un `.exe`. Conséquences :

| Point | Conséquence |
|---|---|
| Le Bureau est **modifiable par l'utilisateur** | l'app peut **remplacer son propre `.exe` sur place**, sans droits admin |
| **Une copie par profil Windows** | aucun conflit de verrou entre postes ; chacun se met à jour seul |
| **Le chemin ne change pas** | si AppLocker/WDAC autorise le Bureau, le remplacement sur place **reste autorisé** |
| **Rien ne doit s'exécuter ailleurs** | on évite tout `.bat`/`.ps1` dans `%TEMP%` ; le renommage se fait **dans le processus déjà autorisé** |
| L'**option C** (dossiers versionnés + lanceur dans `%LOCALAPPDATA%`) | **abandonnée** : le lanceur ne pourrait pas s'exécuter → **le zip est définitivement écarté** |

**Scénario retenu (option B, sans helper) :**

1. vérification en fond → bandeau « Mise à jour disponible » ;
2. clic « Mettre à jour » → téléchargement dans **le même dossier** (le Bureau),
   sous `RadCaseManager.new.exe` (même dossier ⇒ renommage **atomique**) ;
3. en interne : `RadCaseManager.exe` → `RadCaseManager.old.exe`, puis
   `RadCaseManager.new.exe` → `RadCaseManager.exe`, puis relance et fermeture de
   l'instance courante ;
4. le `.old` (sauvegarde de secours) est supprimé au démarrage suivant.

Avantages : **aucun script externe**, chemin d'exécution inchangé (donc conforme
aux politiques), raccourci de l'utilisateur toujours valable.

**Vigilance OneDrive.** Si le Bureau est redirigé vers OneDrive (chemin du type
`…\OneDrive - <Organisation>\Bureau\…`) :
- chaque mise à jour **re-téléverse ~51 Mo par utilisateur** vers OneDrive ;
- OneDrive peut poser un verrou bref → le renommage doit **réessayer** quelques
  fois avant d'abandonner (et reporter au démarrage suivant).
À vérifier (clic droit sur le Bureau → *Propriétés* → *Emplacement*) et, le cas
échéant, demander à l'IT d'exclure les `.exe` de la synchronisation.

## 12. Décisions à prendre

1. Le `.exe` étant **sur le Bureau** (§11) : le Bureau est-il **redirigé vers
   OneDrive** ? (impacte le volume synchronisé et le comportement du renommage)
2. L'IT tolère-t-elle un exécutable non signé qui se remplace lui-même
   (antivirus / AppLocker) ?
3. Option retenue : **A** (silencieux au redémarrage) ou **B** (bouton « Mettre à
   jour » + relance automatique — **recommandée**) ? L'option C et le zip sont
   écartés par la contrainte du Bureau (§11).

## 13. Implémentation

L'**option B** est en place. Fichiers concernés :

| Fichier | Rôle |
|---|---|
| `version.py` *(nouveau)* | `APP_VERSION`, réécrit par la CI à partir du tag |
| `updater.py` *(nouveau)* | toute la logique, **sans dépendance GUI** et testable en CLI |
| `config.py` | importe `APP_VERSION` ; réglages `auto_check_updates` et `last_update_check` |
| `main_gui.py` | workers `QThread`, menu **Aide → Vérifier les mises à jour…**, barre de statut (version), case à cocher dans les réglages |
| `.github/workflows/build-windows.yml` | injection de la version depuis le tag + asset `.sha256` |
| `README.md` / `AGENTS.md` / ce document | documentation |

Points de conception appliqués :

- **Détection sans quota** : lecture de la redirection `HEAD /releases/latest`
  (repli sur l'API GitHub en cas d'échec). Intervalle minimal de 24 h, horodaté
  dans `settings.json`.
- **Téléchargement** vers `<nom>.new.exe` **dans le dossier de l'application**
  (même volume ⇒ renommage atomique), via `<nom>.new.exe.part` pour ne jamais
  laisser un exécutable tronqué ; garde-fou de taille (≥ 5 Mo).
- **Application** : renommage `exe → .old`, puis `staged → exe`, avec quelques
  tentatives (verrou antivirus) et retour en arrière en cas d'échec ; relance
  détachée puis fermeture de l'instance courante.
- **Nettoyage** au démarrage : suppression du `.old` (verrouillé par l'instance
  précédente) et des `.part` orphelins.
- **Sûreté de threading** : les signaux des workers sont reliés à des **méthodes
  liées de la fenêtre** (jamais des lambdas) — seul moyen pour PySide6 de garantir
  l'exécution dans le thread de l'interface.
- **Mode développement** : version contenant `dev` ou exécution depuis les sources
  ⇒ vérification désactivée (aucun risque qu'un binaire de test se remplace par
  une release).

Vérifié en local (macOS) : comparaison de versions, anti-rebond 24 h, téléchargement
réel de la release `v3.12` (51 105 327 octets, identique à l'asset), échange par
renommage dans un dossier jetable, nettoyage, et flux GUI complet avec retour des
handlers dans le thread principal. Reste à valider sur un poste **Windows** : la
bascule effective du `.exe` en cours d'exécution.

**Première release équipée : `v4.0`** (13/10/2026). Le binaire publié embarque bien
`APP_VERSION = "v4.0"` — vérifié en décompressant le module `version` du `.exe`
téléchargé, empreinte SHA-256 concordante avec l'asset publié. Le test de bout en
bout se fera donc de **`v4.0` vers `v4.1`**.

Pistes non retenues pour l'instant : vérification de l'empreinte SHA-256 côté
client, retour arrière automatique si la nouvelle version ne démarre pas, canal
« bêta », signature de code.
