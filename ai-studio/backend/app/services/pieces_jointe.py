"""Pièces jointes du Chat : images et PDF choisis avec le bouton « + ».

Les pièces sont **retenues par fil**, pas consommées : ce qu'on joint une fois
reste disponible pour toutes les requêtes suivantes de la conversation, sans
avoir à le re-sélectionner. C'est le comportement attendu (« je joins le PDF,
puis j'en parle trois questions plus tard »), et il est obtenu en réinjectant le
bloc à chaque requête plutôt qu'ennandant le message qui l'a introduit.

Deux traitements, très différents, selon la nature de la pièce :

* **PDF** — le texte est extrait (`pypdf`) et injecté dans le PROMPT, tandis que
  les octets originaux sont conservés pour les moteurs multimodaux.
* **Image** — les octets sont conservés et transmis à Groq Vision et Gemini
  dans le format multimodal attendu par chacun.

Les plafonds ci-dessous sont défensifs : le navigateur envoie le fichier en base64
dans le corps de la requête, donc une photo de 40 Mo ferait exploser la requête
avant même d'atteindre le moteur.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import MEMOIRE_DIR

# Le PDF est du texte : on peut en lire beaucoup sans alourdir le contexte.
PDF_MAX_OCTETS = 25_000_000
PDF_MAX_PAGES = 120
PDF_MAX_CARACTERES = 40_000
# Une image part en base64 dans la requête ET dans la requête OpenCode.
IMAGE_MAX_OCTETS = 8_000_000
PIECES_MAX = 6

MIMES_IMAGE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# `.jpeg` est l'extension la plus répandue pour une photo : la refuser ferait
# croire que le bouton « + » n'accepte pas les images de la plupart des téléphones.
EXTENSIONS_IMAGE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MIME_PDF = "application/pdf"

PIECES_ACCEPTEES = ", ".join([*sorted(MIMES_IMAGE), MIME_PDF])
IMAGE_MODELE_MAX_OCTETS = 1_800_000
IMAGE_MODELE_COTE_MAX = 1600

logger = logging.getLogger("ai_studio.pieces_jointe")


class ErreurPiece(Exception):
    """Pièce jointe refusée (type non supporté, trop lourde, illisible)."""


def _decoder(donnees: str) -> bytes:
    """base64 strict → octets. Un padding tronqué est toléré (certains
    navigateurs envoient la dernière seventies sans `=`)."""
    donnees = donnees.strip()
    donnees = donnees.split(",", 1)[1] if donnees.startswith("data:") else donnees
    donnees += "=" * (-len(donnees) % 4)
    try:
        return base64.b64decode(donnees, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ErreurPiece("contenu illisible (base64 invalide).") from exc


def optimiser_images_pour_modeles(
    parties: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Réduit les images multimodales volumineuses sans modifier les originaux.

    Les originaux restent persistés dans la conversation. Cette copie dérivée
    plafonne chaque image à 1,8 Mo et 1600 px avant de l'envoyer aux API; cela
    évite qu'une sélection de plusieurs photos fasse dépasser la taille HTTP
    maximale du fournisseur.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # noqa: BLE001 — l'app garde son comportement si absent
        logger.error("compression_image_indisponible pillow_absent=oui")
        return parties

    optimisees: list[dict[str, str]] = []
    for partie in parties:
        mime = str(partie.get("mime") or "")
        if not mime.startswith("image/"):
            optimisees.append(partie)
            continue
        try:
            octets = _decoder(str(partie.get("url") or ""))
            with Image.open(io.BytesIO(octets)) as source:
                if (
                    len(octets) <= IMAGE_MODELE_MAX_OCTETS
                    and max(source.size) <= IMAGE_MODELE_COTE_MAX
                ):
                    optimisees.append(partie)
                    continue
                if getattr(source, "is_animated", False):
                    source.seek(0)
                image = ImageOps.exif_transpose(source).convert("RGBA")
                fond = Image.new("RGBA", image.size, "white")
                fond.alpha_composite(image)
                image = fond.convert("RGB")

            meilleur = b""
            for cote in (1600, 1280, 1024, 800):
                candidate = image.copy()
                candidate.thumbnail((cote, cote), Image.Resampling.LANCZOS)
                for qualite in (88, 82, 76, 68):
                    tampon = io.BytesIO()
                    candidate.save(
                        tampon, format="JPEG", quality=qualite, optimize=True
                    )
                    meilleur = tampon.getvalue()
                    if len(meilleur) <= IMAGE_MODELE_MAX_OCTETS:
                        break
                if len(meilleur) <= IMAGE_MODELE_MAX_OCTETS:
                    break

            fichier = str(partie.get("filename") or "image")
            optimisees.append(
                {
                    **partie,
                    "mime": "image/jpeg",
                    "filename": f"{Path(fichier).stem or 'image'}.jpg",
                    "url": "data:image/jpeg;base64," + base64.b64encode(meilleur).decode("ascii"),
                }
            )
            logger.info(
                "image_preparee octets=%d->%d",
                len(octets),
                len(meilleur),
            )
        except Exception as exc:  # noqa: BLE001 — une pièce ne disparaît jamais
            logger.warning(
                "compression_image_echouee type=%s; envoi_original_conserve=oui",
                type(exc).__name__,
            )
            optimisees.append(partie)
    return optimisees


def _deviner_mime(nom: str, mime: str) -> str:
    """`image/*` est accepté par le sélecteur du navigateur : on regarde alors
    l'extension, sinon une image sélectionnée depuis le disque serait rejetée."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in MIMES_IMAGE or mime == MIME_PDF:
        return mime
    extension = "." + nom.rsplit(".", 1)[-1].lower() if "." in nom else ""
    if extension in EXTENSIONS_IMAGE:
        return EXTENSIONS_IMAGE[extension]
    if extension == ".pdf":
        return MIME_PDF
    if mime.startswith("image/"):
        raise ErreurPiece(
            f"format d'image non pris en charge : {mime or extension or 'inconnu'} "
            f"(attendu : {PIECES_ACCEPTEES})."
        )
    return mime


def _lire_pdf(contenu: bytes, nom: str) -> tuple[str, int]:
    import io

    from pypdf import PdfReader

    try:
        lecteur = PdfReader(io.BytesIO(contenu))
        pages = list(lecteur.pages[:PDF_MAX_PAGES])
    except ErreurPiece:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf lève des exceptions variées
        raise ErreurPiece(f"PDF illisible ({nom}).") from exc
    morceaux: list[str] = []
    total = 0
    for numero, page in enumerate(pages, 1):
        try:
            texte = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — une page corrompue n'annule pas le reste
            texte = ""
        if not texte.strip():
            continue
        morceaux.append(f"--- page {numero} ---\n{texte.strip()}")
        total += len(texte)
        if total >= PDF_MAX_CARACTERES:
            break
    if not morceaux:
        return (
            f"### {nom}\n"
            "[PDF sans texte extractible : le document joint doit être analysé "
            "visuellement par un moteur multimodal.]",
            len(pages),
        )
    contenu_texte = "\n\n".join(morceaux)
    if len(contenu_texte) > PDF_MAX_CARACTERES:
        contenu_texte = contenu_texte[:PDF_MAX_CARACTERES] + "\n[…fin du PDF tronqué…]"
    return contenu_texte, len(pages)


def _texte_pdf(contenu: bytes, nom: str) -> str:
    """Compatibilité avec les appelants historiques : texte seulement."""
    return _lire_pdf(contenu, nom)[0]


def _convertir(pieces: list[Any]) -> list[dict[str, Any]]:
    """Valide une pièce et renvoie son descripteur persistable.

    Un PDF devient du texte (extrait une seule fois, ici), une image garde ses
    octets : ce sont ces descripteurs qui sont stockés par fil, ce qui permet de
    reconstruire le prompt des messages suivants sans redemander le fichier.
    """
    if len(pieces) > PIECES_MAX:
        raise ErreurPiece(f"{len(pieces)} pièces jointes : maximum {PIECES_MAX}.")

    descripteurs: list[dict[str, Any]] = []
    for piece in pieces:
        nom = (piece.nom or "fichier").strip() or "fichier"
        mime = _deviner_mime(nom, piece.mime)
        octets = _decoder(piece.donnees)
        if not octets:
            raise ErreurPiece(f"« {nom} » est vide.")
        descripteur: dict[str, Any] = {
            "cle": hashlib.sha256(f"{nom}|{mime}|{len(octets)}".encode()).hexdigest()[:16],
            "nom": nom,
            "mime": mime,
            "taille_octets": len(octets),
            "ko": max(1, len(octets) // 1000),
            "etat": "ready",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message_id": None,
        }
        if mime == MIME_PDF:
            if len(octets) > PDF_MAX_OCTETS:
                raise ErreurPiece(
                    f"« {nom} » dépasse {PDF_MAX_OCTETS // 1_000_000} Mo "
                    f"({len(octets) // 1_000_000} Mo)."
                )
            texte, pages = _lire_pdf(octets, nom)
            descripteur["texte"] = texte
            descripteur["pages"] = pages
            descripteur["document"] = base64.b64encode(octets).decode()
        elif mime in MIMES_IMAGE:
            if len(octets) > IMAGE_MAX_OCTETS:
                raise ErreurPiece(
                    f"« {nom} » dépasse {IMAGE_MAX_OCTETS // 1_000_000} Mo "
                    f"({len(octets) // 1_000_000} Mo)."
                )
            descripteur["image"] = base64.b64encode(octets).decode()
        else:
            # Aucun type deduit : liste explicite plutot qu'un mime renvoye tel quel,
            # qui aboutirait a un message d'erreur incomprehensible pour l'utilisateur.
            raise ErreurPiece(
                f"« {nom} » : type non pris en charge ({mime or 'inconnu'}). "
                f"Accepté : {PIECES_ACCEPTEES}."
            )
        descripteurs.append(descripteur)
    return descripteurs


def _blocs(descripteurs: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """(bloc texte, parties fichier multimodales) à partir des pièces d'un fil.

    Le bloc texte est NON NÉGOCIABLE : il porte le contenu du PDF et la liste des
    images. Il est donc rendu tel quel dans le prompt, hors du budget de troncature
    des blocs de contexte optionnels.
    """
    pdf = [d for d in descripteurs if d.get("texte")]
    images = [d for d in descripteurs if d.get("image")]
    documents = [d for d in descripteurs if d.get("document")]
    morceaux: list[str] = []
    if pdf:
        morceaux.append(
            "Contenu des PDF joints par l'utilisateur "
            f"({', '.join(d['nom'] for d in pdf)}) :\n\n"
            + "\n\n".join(d["texte"] for d in pdf)
        )
    if images:
        noms = ", ".join(d["nom"] for d in images)
        morceaux.append(
            f"L'utilisateur a joint {len(images)} image(s) : {noms}. "
            "Elles font partie de la conversation et restent jointes : analyse-les "
            "à chaque fois que la question porte dessus, même plus tard."
        )
    parties = [
        {
            "type": "file",
            "mime": d["mime"],
            "filename": d["nom"],
            "url": f"data:{d['mime']};base64,{d['image']}",
        }
        for d in images
    ]
    parties.extend(
        {
            "type": "file",
            "mime": d["mime"],
            "filename": d["nom"],
            "url": f"data:{d['mime']};base64,{d['document']}",
        }
        for d in documents
    )
    return "\n\n".join(morceaux), parties


def _dossier(session: str, mode: str = "chat") -> Path:
    """Retourne un chemin opaque : aucun identifiant utilisateur n'est utilisé
    comme nom de dossier et les fils Chat/Edit restent séparés."""
    nom = hashlib.sha256(f"{mode}:{session}".encode()).hexdigest()[:32]
    return MEMOIRE_DIR / "pieces" / nom


def _lire(session: str, mode: str = "chat") -> list[dict[str, Any]]:
    """Pièces retenues pour ce fil. Un manifeste corrompu ne doit pas casser le
    chat : on repart d'un fil sans pièce plutôt que de renvoyer une erreur."""
    try:
        valeur = json.loads((_dossier(session, mode) / "manifest.json").read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(valeur, list):
        return []
    return [d for d in valeur if isinstance(d, dict) and d.get("cle")]


def _ecrire(session: str, descripteurs: list[dict[str, Any]], mode: str = "chat") -> None:
    dossier = _dossier(session, mode)
    try:
        dossier.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Disque indisponible : les pièces restent valables pour cette requête.
        return
    manifeste: list[dict[str, Any]] = []
    for descripteur in descripteurs:
        cle_binaire = "image" if "image" in descripteur else "document" if "document" in descripteur else ""
        if cle_binaire:
            suffixe = "image" if cle_binaire == "image" else "document"
            fichier = f"{suffixe}-{descripteur['cle']}.bin"
            try:
                (dossier / fichier).write_bytes(base64.b64decode(descripteur[cle_binaire]))
            except OSError:
                continue
            manifeste.append({k: v for k, v in descripteur.items() if k not in {"image", "document"}} | {"fichier": fichier})
        else:
            manifeste.append(dict(descripteur))
    try:
        temporaire = dossier / "manifest.json.tmp"
        temporaire.write_text(json.dumps(manifeste, ensure_ascii=False), encoding="utf-8")
        temporaire.replace(dossier / "manifest.json")
    except OSError:
        pass


def ajouter(
    session: str,
    pieces: list[Any],
    mode: str = "chat",
    message_id: str | None = None,
) -> list[dict[str, Any]]:
    """Attache des pièces au fil et renvoie la liste complète de ce qu'il retient."""
    descripteurs = _lire(session, mode)
    if not pieces:
        if message_id:
            modifie = False
            for descripteur in descripteurs:
                if descripteur.get("message_id") is None:
                    descripteur["message_id"] = message_id
                    modifie = True
            if modifie:
                _ecrire(session, descripteurs, mode)
        return descripteurs
    connus = {d["cle"] for d in descripteurs}
    total = len(descripteurs)
    for nouveau in _convertir(pieces):
        if nouveau["cle"] in connus:
            continue
        total += 1
        if total > PIECES_MAX:
            raise ErreurPiece(
                f"{PIECES_MAX} pièces jointes maximum pour une conversation "
                f"(celle-ci en a déjà {PIECES_MAX}) : retire-en une."
            )
        descripteurs.append(nouveau)
        connus.add(nouveau["cle"])
    if message_id:
        for descripteur in descripteurs:
            if descripteur.get("message_id") is None:
                descripteur["message_id"] = message_id
    _ecrire(session, descripteurs, mode)
    return descripteurs


def retirer(session: str, cle: str, mode: str = "chat") -> list[dict[str, Any]]:
    descripteurs = [d for d in _lire(session, mode) if d["cle"] != cle]
    _ecrire(session, descripteurs, mode)
    return descripteurs


def _selectionner(descripteurs: list[dict[str, Any]], question: str) -> list[dict[str, Any]]:
    """Sélectionne les pièces utiles à la question, sans envoyer tout le fil.

    Les références naturelles (« cette image », « le PDF », « compare les deux »)
    sont traitées avant le score lexical. En cas de question vague, la pièce la
    plus récente est privilégiée : le modèle ne reçoit pas les anciens fichiers
    par défaut.
    """
    if not descripteurs:
        return []
    q = (question or "").casefold()
    # Appels internes d'inventaire/diagnostic : sans question il n'y a pas de
    # signal de pertinence, on restitue l'ensemble comme avant.
    if not q.strip():
        return descripteurs
    tous = any(x in q for x in ("compare", "comparer", "les deux", "tous les", "toutes les", "chaque fichier"))
    images = [d for d in descripteurs if str(d.get("mime", "")).startswith("image/")]
    pdfs = [d for d in descripteurs if d.get("mime") == MIME_PDF]
    if tous:
        return descripteurs
    if re.search(r"\b(premier|première|1er|1ère)\s+(fichier|document|pdf|image|photo)\b", q):
        return descripteurs[:1]
    if re.search(r"\b(deuxième|second|seconde|2e)\s+(fichier|document|pdf|image|photo)\b", q):
        return descripteurs[1:2] or descripteurs[-1:]
    if re.search(r"\b(dernier|dernière|précédent|précédente)\s+(fichier|document|pdf|image|photo)\b", q):
        return descripteurs[-1:]
    if re.search(r"\b(cette|l['’]?)?\s*(image|photo|capture)\b|image précédente|photo précédente", q):
        return images[-1:] if images else descripteurs[-1:]
    if re.search(r"\b(ce|le|la|un|mon|dans le)\s*(pdf|document|rapport|fichier)\b", q):
        candidats = pdfs or descripteurs
        mots = set(re.findall(r"[\wà-ÿ]{3,}", q))
        scores = [(sum(2 for mot in mots if mot in str(d.get("nom", "")).casefold()), i, d) for i, d in enumerate(candidats)]
        meilleur = max(score for score, _, _ in scores) if scores else 0
        if meilleur:
            return [d for score, _, d in scores if score == meilleur]
        return candidats[-1:]
    mots = set(re.findall(r"[\wà-ÿ]{3,}", q))
    scores = []
    for i, d in enumerate(descripteurs):
        nom = str(d.get("nom", "")).casefold()
        texte = str(d.get("texte", "")).casefold()[:12000]
        score = sum(3 for mot in mots if mot in nom) + sum(1 for mot in mots if mot in texte)
        scores.append((score, i, d))
    meilleur = max(score for score, _, _ in scores)
    if meilleur > 0:
        return [d for score, _, d in scores if score == meilleur]
    return descripteurs[-1:]


def etat(session: str, question: str = "", mode: str = "chat") -> tuple[str, list[dict[str, str]]]:
    """(bloc texte, parties multimodales) des pièces pertinentes du fil.

    C'est ce qui rend le PDF « mémorisé » : chaque requête du fil repart avec le
    texte du PDF et les images, sans que l'utilisateur ait à les re-joindre.
    """
    descripteurs = _selectionner(_lire(session, mode), question)
    dossier = _dossier(session, mode)
    for descripteur in descripteurs:
        if descripteur.get("fichier") and not descripteur.get("image") and not descripteur.get("document"):
            try:
                octets = (dossier / descripteur["fichier"]).read_bytes()
            except OSError:
                continue
            cle = "document" if descripteur.get("mime") == MIME_PDF else "image"
            descripteur[cle] = base64.b64encode(octets).decode()
    return _blocs(descripteurs)


def resume(descripteurs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Métadonnées affichables par l'interface (jamais les octets)."""
    return [
        {
            "cle": d["cle"], "nom": d["nom"], "mime": d["mime"],
            "ko": d["ko"], "size": d.get("taille_octets", d["ko"] * 1000),
            "etat": d.get("etat", "ready"),
            "pages": d.get("pages"), "created_at": d.get("created_at"),
            "message_id": d.get("message_id"),
        }
        for d in descripteurs
    ]


def liste(session: str, mode: str = "chat") -> list[dict[str, Any]]:
    """Pièces retenues par un fil, sans leurs octets."""
    return resume(_lire(session, mode))


def fichier(session: str, cle: str, mode: str = "chat") -> tuple[Path, str, str]:
    """Résout un fichier uniquement s'il appartient au manifeste du fil."""
    dossier = _dossier(session, mode)
    piece = next((d for d in _lire(session, mode) if d.get("cle") == cle), None)
    if not piece or not piece.get("fichier"):
        raise FileNotFoundError("Pièce jointe introuvable pour cette conversation.")
    chemin = (dossier / str(piece["fichier"])).resolve()
    if not chemin.is_relative_to(dossier.resolve()) or not chemin.is_file():
        raise FileNotFoundError("Pièce jointe inaccessible.")
    return chemin, str(piece.get("mime") or "application/octet-stream"), str(piece.get("nom") or "fichier")
