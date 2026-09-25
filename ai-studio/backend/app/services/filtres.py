"""Filtres d'arborescence : exclut les dossiers lourds systématiques ET respecte
le .gitignore du projet ouvert.

- IGNORE_DOSSIERS : exclusion dure (node_modules, .git, build…), jamais re-incluse.
- Le .gitignore de la RACINE du projet est appliqué avec `pathspec`
  (grammaire gitwildmatch : motifs, `!` négations, `/` ancrages, `dir/` dossiers).

Les opérations (tree, search, etat, contextes agent…) utilisent ces filtres pour
ne jamais voir des dizaines de milliers de fichiers inutiles.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pathspec

# Dossiers génériques exclus de l'import et de l'arborescence (bruit).
# Reste la source unique (projets.py ré-exporte pour compat).
IGNORE_DOSSIERS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode", ".tox", ".gradle", "node_modules",
    ".venv", "venv", "env", "__pycache__", "build", "dist", ".next", ".cache",
    "target", ".pytest_cache", "coverage", ".mypy_cache", ".ruff_cache",
})

# Fichiers sensibles (secrets) : exclusion DURE, jamais visibles dans l'arborescence,
# la recherche ou le contexte de l'agent — même sans .gitignore.
# `.env*` est couvert par préfixe (`.env`, `.env.local`, `.env.production`, …).
IGNORE_FICHIERS = frozenset({
    ".envrc", ".npmrc", ".pypirc", ".netrc", ".htpasswd", ".git-credentials",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
})

# Exception : modèles d'environnement faits pour être committés/visibles. Ils
# documentent les variables attendues SANS valeurs — contexte utile à l'agent.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


def _fichier_sensible(nom: str) -> bool:
    """True pour un nom de fichier contenant des secrets (`.env*`, clés, …)."""
    if nom in ENV_TEMPLATES:
        return False
    return (
        nom == ".env"
        or nom.startswith(".env.")
        or nom in IGNORE_FICHIERS
        or nom.endswith((".pem", ".key", ".pfx", ".p12", ".keystore"))
    )


class Filtres:
    """Résout si un chemin relatif est ignoré (dossiers lourds + .gitignore)."""

    __slots__ = ("_spec",)

    def __init__(self, spec: pathspec.PathSpec | None):
        self._spec = spec

    def ignore(self, rel: str, est_dossier: bool = False) -> bool:
        rel = rel.replace("\\", "/").strip("/")
        # Retire un éventuel préfixe "./" SANS toucher aux points des dotfiles
        # (un `lstrip("./")` transformerait « .env » en « env », « .git » en « git »).
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel == ".":
            return True
        parties = rel.split("/")
        # Dossiers lourds : exclusion dure, indépendante du .gitignore.
        if any(p in IGNORE_DOSSIERS for p in parties):
            return True
        # Fichiers sensibles (.env*, clés…) : exclusion dure, indépendante du .gitignore.
        if _fichier_sensible(parties[-1]):
            return True
        spec = self._spec
        if spec is None:
            return False
        # 1) le chemin complet lui-même (gère les négations `!…`). Slash final
        #    pour rendre les motifs `<dossier>/` effectifs sur les dossiers.
        if spec.match_file(rel + ("/" if est_dossier else "")):
            return True
        # 2) chacun des dossiers parents (motifs `<dossier>/` couvrent tout leur
        #    contenu). Évalués du plus profond au plus haut.
        for i in range(len(parties) - 1, 0, -1):
            if spec.match_file("/".join(parties[:i]) + "/"):
                return True
        return False


@functools.lru_cache(maxsize=128)
def _spec_pour(racine: str, _mtime: int) -> Filtres:
    """construit le Filtres d'un dossier racine (mise en cache sur racine+mtime)."""
    fichier = Path(racine) / ".gitignore"
    if not fichier.is_file():
        return Filtres(None)
    try:
        lignes = fichier.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return Filtres(None)
    return Filtres(pathspec.PathSpec.from_lines("gitwildmatch", lignes))


def filtres_pour(racine: Path) -> Filtres:
    """Filtres du projet *racine* (cache invalidé par horodatage du .gitignore)."""
    racine = racine.resolve()
    mtime = 0
    chemin_gi = racine / ".gitignore"
    try:
        mtime = int(chemin_gi.stat().st_mtime_ns) if chemin_gi.is_file() else 0
    except OSError:
        mtime = 0
    return _spec_pour(str(racine), mtime)