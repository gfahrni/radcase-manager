"""Défaut de site pour le dossier partagé (déploiement).

Certains sites installent RadCase Manager sur des postes qui ont tous accès au
même dossier partagé. Ce module fournit ce chemin **au premier lancement
uniquement**, pour éviter à l'utilisateur de chercher le dossier à la main :

- il n'est **jamais** appliqué si un chemin est déjà enregistré dans les
  réglages (le réglage par utilisateur reste la seule source de vérité) ;
- il n'est proposé que si le dossier existe réellement sur le poste : un lecteur
  réseau peut ne pas être monté, ou le poste être hors du réseau du site. Dans
  ce cas on rend ``""`` et le dialogue de configuration s'affiche vide, comme
  avant.

La détection ne sert qu'au **confort**. Elle n'accorde aucun droit et ne doit
jamais servir de contrôle d'accès : elle est locale, donc falsifiable.
"""
from __future__ import annotations

import os
import subprocess
import sys

#: Domaine Active Directory du site (nom DNS).
SITE_DNS_DOMAIN = "intranet.chuv"
#: Même domaine, tel qu'il apparaît dans le DN renvoyé par ``whoami /fqdn``.
SITE_DN_MARKER = "DC=INTRANET,DC=CHUV"
#: Dossier partagé du site, tel qu'il est monté sur ses postes.
SITE_SHARED_ROOT = "N:/RAD/COMMUN/radcase-manager/DATABASE-RAD-CHUV"

#: Délai maximal accordé à ``whoami`` (secondes) : au-delà, on renonce.
_WHOAMI_TIMEOUT = 3


def matches_site(dns_domain: str, fqdn: str) -> bool:
    """Vrai si le domaine DNS ou le DN du compte désigne le site (fonction pure).

    ``dns_domain`` vient de ``USERDNSDOMAIN`` (absent pour un compte local) ;
    ``fqdn`` est la sortie de ``whoami /fqdn``, par exemple
    ``CN=utilisateur,OU=…,DC=intranet,DC=chuv``.
    """
    if (dns_domain or "").strip().casefold() == SITE_DNS_DOMAIN:
        return True
    return SITE_DN_MARKER in (fqdn or "").upper()


def _dns_domain() -> str:
    """Domaine DNS de la session (``USERDNSDOMAIN``), ou ``""``."""
    return os.environ.get("USERDNSDOMAIN", "").strip()


def _fqdn() -> str:
    """DN complet du compte courant, ou ``""`` (commande absente, échec, délai)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # pas de console clignotante
    try:
        result = subprocess.run(
            ["whoami", "/fqdn"],
            capture_output=True,
            text=True,
            timeout=_WHOAMI_TIMEOUT,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()


def _existing(path: str, check=os.path.exists) -> str:
    """``path`` s'il existe, sinon ``""`` (jamais de chemin mort prérempli)."""
    try:
        return path if check(path) else ""
    except OSError:
        return ""


def resolve_default_shared_root(dns_domain: str, fqdn: str, check=os.path.exists) -> str:
    """Chemin du site si le poste en fait partie **et** que le dossier existe.

    ``check`` est injectable pour les tests (existence simulée).
    """
    if not matches_site(dns_domain, fqdn):
        return ""
    return _existing(SITE_SHARED_ROOT, check)


def default_shared_root() -> str:
    """Chemin du dossier partagé proposé sur ce poste, ou ``""``.

    Hors Windows (développement), retourne toujours ``""`` : aucun lecteur
    réseau de site n'existe sur un poste de développement.

    ``USERDNSDOMAIN`` suffit quand il est renseigné ; ``whoami /fqdn`` n'est
    appelé qu'en repli (une seule fois, au premier lancement).
    """
    if sys.platform != "win32":
        return ""
    if matches_site(_dns_domain(), ""):
        return _existing(SITE_SHARED_ROOT)
    return resolve_default_shared_root("", _fqdn())
