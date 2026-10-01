"""Services des projets : import d'archive, arborescence imbriquée, fichiers
(lecture/écriture) et recherche textuelle.

Règle d'état : l'empreinte SHA-1 du contenu de chaque fichier. Le frontend
poll `etat` pendant une tâche agent et recharge les onglets dont l'empreinte
change — l'éditeur suit la main de l'agent en direct.
Contrat décrit dans docs/cahier-des-charges-api.md (§1 et §2).
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

from ..config import PROJETS_DIR
from . import registre, workspace
from .filtres import IGNORE_DOSSIERS, filtres_pour
from .workspace import CheminHorsProjet, chemin_securise, projet_existant

# Extensions considérées binaires : jamais servies ni fouillées.
EXTENSIONS_BINAIRES = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".ico", ".svgz",
    ".pdf", ".zip", ".gz", ".7z", ".rar", ".tar", ".jar", ".class", ".apk",
    ".aab", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".bin", ".o", ".a", ".pyc", ".pyo", ".dat", ".db", ".sqlite",
    ".sqlite3", ".whl", ".xlsx", ".docx", ".pptx", ".ipynb_checkpoints",
}

# Taille au-delà de laquelle un fichier texte n'est plus servi (protection).
TAILLE_MAX_FICHIER = 2 * 1024 * 1024  # 2 Mo

# Taille au-delà de laquelle un fichier n'est pas importé (déchets).
TAILLE_MAX_IMPORT_OCTETS = 25 * 1024 * 1024  # 25 Mo

# Aperçu d'un dossier local : nombre d'exemples renvoyés avant ouverture.
MAX_EXEMPLES_APERCU = 20

SEARCH_MAX_FICHIERS = 50
SEARCH_MAX_CORRESPONDANCES = 200

# Mappage minimal extension -> langue pour l'éditeur / la coloration.
LANGUES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".jsonc": "json", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".txt": "plaintext", ".sql": "sql", ".sh": "shell",
    ".bash": "shell", ".ps1": "powershell", ".bat": "bat", ".toml": "ini",
    ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".java": "java", ".kt": "kotlin",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".dockerfile": "dockerfile", ".vue": "vue", ".astro": "astro", ".r": "r",
}


def _slug(nom: str) -> str:
    """Normalise un nom de projet : minuscules, non alphanumériques -> '-'.
    Vide ou dangereux => remis à "projet" (le routeur validera ensuite)."""
    import re

    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", nom).strip(".-_") or "projet"
    return slug


def _est_binaire(chemin: Path) -> bool:
    """Binaire par extension, ou sniff d'octets nuls sur les premiers Ko."""
    if chemin.suffix.lower() in EXTENSIONS_BINAIRES:
        return True
    try:
        with chemin.open("rb") as f:
            tete = f.read(8192)
    except OSError:
        return True
    return b"\x00" in tete


def _fichiers_lisibles(racine: Path, flt) -> Iterator[tuple[str, Path]]:
    """Itère (chemin relatif, Path) des fichiers réguliers non filtrés.

    Ignore silencieusement toute entrée inaccessible. Un projet ouvert en mode
    direct peut pointer n'importe où sur le disque : sockets Docker (WinError
    1920), tubes, permissions refusées, fichier supprimé pendant le parcours.
    Une seule entrée aberrante ne doit jamais invalider le parcours complet.
    """
    try:
        iterateur = racine.rglob("*")
        for p in iterateur:
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(racine).as_posix()
                if flt.ignore(rel):
                    continue
            except OSError:
                continue
            yield rel, p
    except OSError:
        return


def compter_fichiers(racine: Path) -> int:
    return sum(1 for _ in _fichiers_lisibles(racine, filtres_pour(racine)))


def lister_projets() -> list[dict]:
    """Liste triée des projets (\"archive\" importée + \"dossier\" direct).

    Pour chaque projet : {id, nom, origine, nb_fichiers, chemin}. Les racines
    cachées (préfixe '.') de PROJETS_DIR sont ignorées comme avant.

    NB performance : le compte des projets « dossier » direct vient du REGISTRE
    (mémorisé à l'ouverture) — on ne rescanne jamais le disque à chaque listing,
    sinon le threadpool saturerait dès qu'un dossier contient des dizaines de
    milliers de fichiers (tout le serveur répondrait au compte-gouttes).
    """
    projets = []
    if PROJETS_DIR.exists():
        for d in sorted(p for p in PROJETS_DIR.iterdir() if p.is_dir()):
            if d.name.startswith("."):
                continue
            projets.append({
                "id": d.name,
                "nom": d.name,
                "origine": "archive",
                "nb_fichiers": compter_fichiers(d),
                "chemin": str(d),
            })
    projets.extend(registre.projets_directs())
    _rattraper_comptes_manquants(projets)
    return sorted(projets, key=lambda p: p["nom"].lower())


def _rattraper_comptes_manquants(projets: list[dict]) -> None:
    """Compte les projets « dossier » ouverts avant l'existence de `nb_fichiers`.

    Ces entrées-là n'ont pas la clé : le listing faisait `or 0` et affichait donc
    « 0 fichiers » pour des dossiers pourtant pleins. On ne scanne QUE ces
    entrées (une fois), puis on persiste — le temps de calcul est donc payé une
    seule fois, jamais à chaque listing, comme le veut la règle de performance
    ci-dessus. Un dossier devenu introuvable reste à 0 sans faire échouer le
    listing.
    """
    a_scanner = [
        p for p in projets
        if p.get("origine") == "dossier" and not p.get("nb_fichiers")
        and p.get("chemin")
    ]
    for projet in a_scanner:
        racine = Path(projet["chemin"])
        if not racine.is_dir():
            continue
        try:
            compte = sum(1 for _ in _fichiers_lisibles(racine, filtres_pour(racine)))
        except OSError:
            continue
        projet["nb_fichiers"] = compte
        try:
            # API publique du registre : idempotente, elle met à jour l'entrée
            # existante au lieu d'en créer une seconde.
            registre.ajouter_dossier_direct(str(racine), nb_fichiers=compte)
        except (OSError, ValueError):
            pass


def _normaliser_archive(binaire: bytes, nom_archive: str | None = None) -> tuple[str, bytes]:
    """Détecte zip / tar.gz / tar ; retourne (format, binaire normalisé)."""
    if binaire[:4] == b"PK\x03\x04":
        return "zip", binaire
    if binaire.startswith(b"\x1f\x8b"):
        return "tar.gz", binaire
    if binaire[257:262] == b"ustar":
        return "tar", binaire
    raise ValueError(f"format d'archive non supporté : {nom_archive or ''!r} "
                     "(zip, tar.gz ou tar attendus)")


def _extraire(binaire: bytes) -> Path:
    """Extrait l'archive dans un dossier temporaire, en filtrant lourd/binaire."""
    format_, binaire = _normaliser_archive(binaire)
    tmp = Path(tempfile.mkdtemp())
    if format_ == "zip":
        with zipfile.ZipFile(io.BytesIO(binaire)) as z:
            z.extractall(tmp)
    elif format_ in ("tar", "tar.gz"):
        mode = "r:gz" if format_ == "tar.gz" else "r:"
        with tarfile.open(fileobj=io.BytesIO(binaire), mode=mode) as t:
            t.extractall(tmp, filter="data")

    # Retient la racine réelle si l'archive enveloppe un unique dossier.
    entites = [p for p in tmp.iterdir() if p.name not in IGNORE_DOSSIERS]
    if len(entites) == 1 and entites[0].is_dir():
        return entites[0]
    return tmp


def importer_archive(binaire: bytes, nom: str | None = None) -> dict:
    """Importe une archive (zip/tar*/tar.gz) dans data/projets/.

    Remplace un projet du même nom. Retourne {id, nom, nb_fichiers}.
    """
    tmp = _extraire(binaire)
    try:
        # Nom du projet : explicite, sinon dérivé du nom affiché du fichier.
        total = compter_fichiers(tmp)
        if not nom:
            # sans nom réel, on tombe sur l'id technique (le frontend demande le nom).
            nom = "projet"
        nom = _slug(nom)
        cible = PROJETS_DIR / nom
        cible.mkdir(parents=True, exist_ok=True)
        # Vidage préalable (remplacement propre du projet existant).
        for enfant in cible.iterdir():
            if enfant.is_dir():
                shutil.rmtree(enfant, ignore_errors=True)
            else:
                enfant.unlink()
        copies = 0
        for src in tmp.rglob("*"):
            if src.is_dir():
                continue
            if TAILLE_MAX_IMPORT_OCTETS and src.stat().st_size > TAILLE_MAX_IMPORT_OCTETS:
                continue
            rel = src.relative_to(tmp)
            if any(part in IGNORE_DOSSIERS for part in rel.parts):
                continue
            dst = cible / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copies += 1
        return {"id": nom, "nom": nom, "nb_fichiers": copies}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def exporter_archive(nom: str) -> tuple[Path, str]:
    """Zippe le projet dans un fichier temporaire.

    Retourne (chemin_du_zip, nom_telechargeable). C'est le pendant de
    `importer_archive` : sans lui, un projet modifié par l'agent dans le cloud
    resterait prisonnier du serveur, faute de pouvoir le récupérer.

    L'archive est écrite sur disque (et non en mémoire) pour ne pas peser sur
    le Workers : le fichier est supprimé par l'appelant via `supprimer_temporaire`.
    """
    # `projet_existant` et non `racine_projet` : ce dernier *crée* le dossier
    # s'il manque, et exporter un projet inexistant ne doit pas laisser de
    # répertoire vide derrière soi — il doit répondre 404.
    racine = projet_existant(nom)
    descripteur, brut = tempfile.mkstemp(suffix=".zip")
    os.close(descripteur)
    chemin = Path(brut)
    dossier = _slug(nom)
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        for src in racine.rglob("*"):
            # Mêmes exclusions que l'arborescence : un socket, un tube ou un
            # fichier devenu inaccessible ferait échouer tout l'export.
            try:
                if src.is_dir() or not src.is_file():
                    continue
                rel = src.relative_to(racine)
                if any(part in IGNORE_DOSSIERS for part in rel.parts):
                    continue
                z.write(src, Path(dossier) / rel)
            except OSError:
                continue
    return chemin, f"{dossier}.zip"


def supprimer_temporaire(chemin: Path) -> None:
    """Supprime le zip d'export après envoi (tâche de fond du FileResponse)."""
    try:
        chemin.unlink(missing_ok=True)
    except OSError:
        pass


def _noeuds(racine: Path, dossier: Path, flt) -> list[dict]:
    """Construit l'arborescence imbriquée (dossiers d'abord, tri alpha).

    Applique les filtres (dossiers lourds + .gitignore) AVANT toute descente
    récursive : un dossier ignoré est sauté sans être listé ni parcouru.

    Une entrée illisible est IGNORÉE, jamais fatale. Un projet ouvert en mode
    direct peut être n'importe quel dossier du disque : sockets Docker
    (`.sock`, WinError 1920 sous Windows), tubes, liens cassés, fichiers
    supprimés entre le listage et le `stat`, dossiers sans permission. Faire
    échouer toute l'arborescence pour une entrée aberrante rendrait
    l'éditeur inutilisable sur un dossier de travail réaliste.
    """
    entrees = []
    try:
        fils = sorted(dossier.iterdir())
    except OSError:
        return entrees
    for p in fils:
        try:
            rel = p.relative_to(racine).as_posix()
            est_dossier = p.is_dir()
            if flt.ignore(rel, est_dossier=est_dossier):
                continue
            if est_dossier:
                entrees.append({"nom": p.name, "chemin": rel, "type": "dossier",
                                "enfants": _noeuds(racine, p, flt)})
            else:
                infos = p.stat()
                # Socket, tube, périphérique : rien à afficher ni à ouvrir.
                if not stat.S_ISREG(infos.st_mode):
                    continue
                entrees.append({"nom": p.name, "chemin": rel, "type": "fichier",
                                "taille": infos.st_size})
        except OSError:
            continue
    return sorted(entrees, key=lambda e: (e["type"] != "dossier", e["nom"].lower()))


def arborescence(nom: str) -> list[dict]:
    """Arborescence imbriquée du projet (racine d'abord)."""
    racine = projet_existant(nom)
    return _noeuds(racine, racine, filtres_pour(racine))


def langue_pour(rel: str) -> str:
    return LANGUES.get(Path(rel).suffix.lower(), "plaintext")


def lire_fichier(nom: str, rel: str) -> dict:
    """Retourne {chemin, langue, taille, modifie, contenu} ; 404/422 côté route."""
    racine = projet_existant(nom)
    chemin = chemin_securise(racine, rel)
    if not chemin.is_file():
        raise FileNotFoundError(rel)
    if _est_binaire(chemin) or chemin.stat().st_size > TAILLE_MAX_FICHIER:
        raise ValueError(f"fichier binaire ou trop volumineux : {rel}")
    return {
        "chemin": rel,
        "langue": langue_pour(rel),
        "taille": chemin.stat().st_size,
        "modifie": int(chemin.stat().st_mtime),
        "contenu": workspace.lire_fichier(chemin),
    }


def ecrire_fichier(nom: str, rel: str, contenu: str) -> dict:
    """Écrit *contenu* (crée les dossiers parents). Retourne {chemin, nouveau_sha}."""
    racine = projet_existant(nom)
    chemin = chemin_securise(racine, rel)
    if chemin.is_dir():
        raise IsADirectoryError(rel)
    workspace.ecrire_fichier(chemin, contenu)
    return {"chemin": rel, "nouveau_sha": _sha(contenu)}


def _sha(contenu: str) -> str:
    import hashlib

    return hashlib.sha1(contenu.encode("utf-8")).hexdigest()


def rechercher(nom: str, q: str) -> dict:
    """Grep simple : sous-chaîne insensible à la casse, contexte ±1 ligne."""
    racine = projet_existant(nom)
    flt = filtres_pour(racine)
    motif = q.strip().lower()
    if not motif:
        return {"q": q, "nb_fichiers": 0, "fichiers": []}
    resultats: list[dict] = []
    total_corr = 0
    for p in racine.rglob("*"):
        try:
            if p.is_dir():
                continue
            rel = p.relative_to(racine).as_posix() if p.is_file() else p.name
            if not p.is_file() or flt.ignore(rel):
                continue
            if _est_binaire(p) or p.stat().st_size > TAILLE_MAX_FICHIER:
                continue
            lignes = workspace.lire_fichier(p).splitlines()
        except OSError:
            continue
        except UnicodeDecodeError:
            continue
        correspondances = []
        for i, ligne in enumerate(lignes):
            if motif in ligne.lower():
                debut = max(0, i - 1)
                extrait = "\n".join(lignes[debut:i + 2])
                correspondances.append({"ligne": i + 1, "num": i, "extrait": extrait})
                total_corr += 1
                if total_corr >= SEARCH_MAX_CORRESPONDANCES:
                    break
        if correspondances:
            resultats.append({"fichier": p.relative_to(racine).as_posix(),
                              "correspondances": correspondances[:SEARCH_MAX_CORRESPONDANCES]})
        if len(resultats) >= SEARCH_MAX_FICHIERS or total_corr >= SEARCH_MAX_CORRESPONDANCES:
            break
    return {"q": q, "nb_fichiers": len(resultats), "fichiers": resultats}


def etat_fichiers(nom: str, chemins: list[str] | None = None) -> dict:
    """Empreintes SHA-1 des fichiers *chemins* (tous si None).

    Chemin inexistant -> sha None (le fichier a été créé ou supprimé). C'est la
    brique d'accompagnement du mode autonome : le frontend poll et recharge les
    onglets qui changent pendant que l'agent travaille.
    """
    racine = projet_existant(nom)
    flt = filtres_pour(racine)
    cibles = chemins or ["."]
    etats: dict[str, str | None] = {}
    for rel in cibles:
        rel = rel.strip()
        if not rel:
            continue
        if rel == ".":
            for p in racine.rglob("*"):
                # Endpoint de polling de l'agent : une entrée illisible ne doit
                # pas interrompre le relevé des autres fichiers.
                try:
                    if p.is_file() and not flt.ignore(p.relative_to(racine).as_posix()):
                        etats[p.relative_to(racine).as_posix()] = _sha_file(p)
                except OSError:
                    continue
            continue
        try:
            chemin = chemin_securise(racine, rel)
        except CheminHorsProjet:
            continue
        if chemin.is_file() and not _est_binaire(chemin) and chemin.stat().st_size <= TAILLE_MAX_FICHIER:
            etats[rel] = _sha_file(chemin)
        else:
            etats[rel] = None
    return {"projet": nom, "fichiers": etats}


def _sha_file(chemin: Path) -> str:
    return hashlib.sha1(chemin.read_bytes()).hexdigest()


# ─────────────────── Dossier local (ouverture directe VS Code-like) ─────────

# Racines de départ de l'explorateur « Parcourir… » : le dossier personnel et
# quelques sous-dossiers usuels — tout ce qui passe déjà `valider_chemin_local`.
_DEPART_NAVIGATION = (
    (None, ""),                      # USERPROFILE
    ("USERPROFILE", "Documents"),
    ("USERPROFILE", "Desktop"),
    ("USERPROFILE", "Downloads"),
    ("USERPROFILE", "Videos"),
    ("USERPROFILE", "Pictures"),
)


def _dossier_navigable(dossier: Path, nom: str | None = None) -> dict:
    """Entrée d'explorateur (dossier déjà validé) : {nom, chemin}."""
    return {"nom": nom or dossier.name or "dossier", "chemin": str(dossier)}


def parcourir_dossier(texte: str = "") -> dict:
    """Liste les sous-dossiers de premier niveau de *texte* (explorateur serveur).

    Tout entrée listée — sous-dossier comme dossier parent — a déjà passé
    `valider_chemin_local`, donc l'explorateur ne peut jamais exposer un chemin
    que l'ouverture refuserait (AppData, racines système, racine de lecteur…).

    - texte vide  -> {chemin:"", nom:"", parent:null, dossiers:[départ usuels]}
    - sinon       -> {chemin, nom, parent|null, dossiers:[sous-dossiers triés]}
    """
    if not (texte or "").strip():
        dossiers: list[dict] = []
        for variable, sous in _DEPART_NAVIGATION:
            valeur = os.environ.get(variable) if variable else None
            dossier = _profil_utilisateur() if valeur is None and not variable else None
            base = Path(valeur) if valeur else (dossier or Path.home())
            if sous:
                base = base / sous
            try:
                chemin, nom = registre.valider_chemin_local(str(base))
            except (ValueError, OSError):
                continue
            dossiers.append(_dossier_navigable(chemin, nom))
        return {"chemin": "", "nom": "", "parent": None, "dossiers": dossiers}

    try:
        chemin, nom = registre.valider_chemin_local(texte)
    except ValueError as exc:
        raise ValueError(
            f"{exc}\n\n(Choisis un dossier dans l'explorateur ou corrige le chemin.)"
        ) from exc

    sous_dossiers: list[dict] = []
    for enfant in sorted((p for p in chemin.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
        try:
            sous_chemin, _ = registre.valider_chemin_local(str(enfant))
        except ValueError:
            continue
        sous_dossiers.append(_dossier_navigable(sous_chemin))

    parent = None
    if chemin.parent != chemin and _est_parent_valide(chemin.parent):
        parent = _dossier_navigable(chemin.parent)

    return {"chemin": str(chemin), "nom": nom, "parent": parent, "dossiers": sous_dossiers}


def _est_parent_valide(parent: Path) -> bool:
    """Le *parent* est-il lui-même ouvrable (pour proposer la remontée) ?"""
    try:
        registre.valider_chemin_local(str(parent))
        return True
    except ValueError:
        return False


def _profil_utilisateur() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()


def apercu_dossier_local(texte: str) -> dict:
    """Aperçu (sans ouvrir) d'un dossier local : validation + échantillon filtré.

    Aucune écriture, aucun enregistrement : {nom, chemin, nb_fichiers, exemples}.
    Lève ValueError si le chemin est refusé par la validation.
    """
    chemin, nom = registre.valider_chemin_local(texte)
    exemples: list[str] = []
    total = 0
    for rel, _ in _fichiers_lisibles(chemin, filtres_pour(chemin)):
        total += 1
        if len(exemples) < MAX_EXEMPLES_APERCU:
            exemples.append(rel)
    return {
        "nom": nom,
        "chemin": str(chemin),
        "nb_fichiers": total,
        "exemples": exemples,
    }


def ouvrir_dossier_local(texte: str) -> dict:
    """Ouvre un dossier local en mode direct (référence, sans copie).

    Enregistre l'entrée dans le registre puis retourne {id, nom, chemin, origine,
    nb_fichiers}. Le contenu réel du disque n'est JAMAIS copié. Le compte de
    fichiers est mémorisé au registre (rafraîchit le listing sans re-scanner).
    """
    racine, _ = registre.valider_chemin_local(texte)
    nb = sum(1 for _ in _fichiers_lisibles(racine, filtres_pour(racine)))
    entree = registre.ajouter_dossier_direct(str(racine), nb_fichiers=nb)
    return {**entree, "origine": "dossier", "nb_fichiers": nb}