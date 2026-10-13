"""Version de l'application.

Ce fichier est **réécrit par la CI** juste avant la construction de l'exécutable
(voir ``.github/workflows/build-windows.yml``) pour y inscrire le tag de la
release (ex. ``v3.13``). Il n'est jamais poussé avec cette valeur : la
modification n'a lieu que dans le clone de travail de la CI.

En développement, la valeur par défaut contient ``dev`` : le vérificateur de
mises à jour se désactive de lui-même (un binaire de test ne doit pas proposer
de se remplacer par une release publiée).
"""

APP_VERSION = "0.0.0.dev"
