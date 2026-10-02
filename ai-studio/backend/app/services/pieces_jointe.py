"""Pièces jointes du Chat : images et PDF choisis avec le bouton « + ».

Deux traitements, très différents, selon la nature de la pièce :

* **PDF** — le texte est extrait (`pyppdf`) et injecté dans le PROMPT. Tous les
  moteurs en profitent, y compris les secours cloud qui n'acceptent que du texte.
* **Image** — les octets sont transmis au moteur comme partie `file` du message
  OpenCode. C'est le seul chemin qui donne réellement l'image au modèle : il n'y
  a ni OCR ni vision locale dans l'application, et un moteur texte seul ignorera
  silencieusement la pièce. Le bloc texte dit alors explicitement à ces moteurs ce
  qu'ils ne verront pas, plutôt que de faire croire que l'image est analysée.

Les plafonds ci-dessous sont défensifs : le navigateur envoie le fichier en base64
dans le corps de la requête, donc une photo de 40 Mo ferait exploser la requête
avant même d'atteindre le moteur.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

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


def _texte_pdf(contenu: bytes, nom: str) -> str:
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
        raise ErreurPiece(
            f"« {nom} » ne contient aucun texte extractible (PDF scanné/image ?)."
        )
    contenu_texte = "\n\n".join(morceaux)
    if len(contenu_texte) > PDF_MAX_CARACTERES:
        contenu_texte = contenu_texte[:PDF_MAX_CARACTERES] + "\n[…fin du PDF tronqué…]"
    return contenu_texte


def analyser(pieces: list[Any]) -> tuple[str, list[dict[str, str]]]:
    """Convertit les pièces jointes en (bloc texte, parties image OpenCode).

    Le bloc texte est NON NÉGOCIABLE : il porte le contenu du PDF et la liste des
    images. Il est donc rendu tel quel dans le prompt, hors du budget de troncature
    des blocs de contexte optionnels.
    """
    if not pieces:
        return "", []
    if len(pieces) > PIECES_MAX:
        raise ErreurPiece(f"{len(pieces)} pièces jointes : maximum {PIECES_MAX}.")

    blocs_pdf: list[str] = []
    images: list[dict[str, str]] = []
    noms_images: list[str] = []
    total_octets = 0

    for piece in pieces:
        nom = (piece.nom or "fichier").strip() or "fichier"
        mime = _deviner_mime(nom, piece.mime)
        octets = _decoder(piece.donnees)
        if not octets:
            raise ErreurPiece(f"« {nom} » est vide.")
        total_octets += len(octets)

        if mime == MIME_PDF:
            if len(octets) > PDF_MAX_OCTETS:
                raise ErreurPiece(
                    f"« {nom} » dépasse {PDF_MAX_OCTETS // 1_000_000} Mo "
                    f"({len(octets) // 1_000_000} Mo)."
                )
            blocs_pdf.append(f"### {nom}\n{_texte_pdf(octets, nom)}")
        elif mime in MIMES_IMAGE:
            if len(octets) > IMAGE_MAX_OCTETS:
                raise ErreurPiece(
                    f"« {nom} » dépasse {IMAGE_MAX_OCTETS // 1_000_000} Mo "
                    f"({len(octets) // 1_000_000} Mo)."
                )
            images.append(
                {
                    "type": "file",
                    "mime": mime,
                    "filename": nom,
                    "url": f"data:{mime};base64,{base64.b64encode(octets).decode()}",
                }
            )
            noms_images.append(nom)
        else:
            # Aucun type deduit : liste explicite plutot qu'un mime renvoye tel quel,
            # qui aboutirait a un message d'erreur incomprehensible pour l'utilisateur.
            raise ErreurPiece(
                f"« {nom} » : type non pris en charge ({mime or 'inconnu'}). "
                f"Accepté : {PIECES_ACCEPTEES}."
            )

    morceaux: list[str] = []
    if blocs_pdf:
        morceaux.append(
            "Contenu des PDF joints par l'utilisateur "
            f"({', '.join(b.splitlines()[0].removeprefix('### ') for b in blocs_pdf)}) :\n\n"
            + "\n\n".join(blocs_pdf)
        )
    if noms_images:
        liste = ", ".join(noms_images)
        morceaux.append(
            f"L'utilisateur a joint {len(noms_images)} image(s) : {liste}. "
            "Elles sont transmises à côté de ce message : regarde-les si la question "
            "porte dessus. Si tu ne reçois pas d'image (tu ne sais que lire du texte), "
            "dis-le explicitement plutôt que de deviner ce qu'elles montrent."
        )
    return "\n\n".join(morceaux), images