"""Tests des pièces jointes du Chat (services/pieces_jointe.py).

Exécutables sans réseau, sans clé LLM et sans serveur OpenCode :
     python tests/test_pieces_jointe.py

Trois décisions sont verrouillées ici, parce qu'elles sont visibles à l'écran :
le texte du PDF entre dans le PROMPT, l'image part en partie `file` et jamais
dans le texte, et une pièce refusée arrête le flux AVANT le premier token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import pathlib
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.api import agent  # noqa: E402
from app.models.chat import PieceJoine, RequeteChat  # noqa: E402
from app.services import agent_discussion, pieces_jointe  # noqa: E402


# ─────────────────────────────── Fabriques ────────────────────────────────


def _png(largeur: int, hauteur: int, couleur) -> bytes:
    """PNG minimal écrit à la main : le projet n'a ni Pillow ni dépendance de test
    d'image, et un octet modifié ici change vraiment les pixels envoyés."""

    def morceau(genre: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + genre
            + data
            + struct.pack(">I", zlib.crc32(genre + data) & 0xFFFFFFFF)
        )

    lignes = b"".join(
        b"\x00" + b"".join(bytes(couleur(x, y)) for x in range(largeur))
        for y in range(hauteur)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + morceau(b"IHDR", struct.pack(">IIBBBBB", largeur, hauteur, 8, 2, 0, 0, 0))
        + morceau(b"IDAT", zlib.compress(lignes, 9))
        + morceau(b"IEND", b"")
    )


def _pdf(texte: str) -> bytes:
    """PDF minimal à une page, construit sans reportlab (absent du venv)."""
    flux = io.BytesIO()
    contenu = f"BT /F1 24 Tf 72 700 Td ({texte}) Tj ET".encode("latin-1", "replace")
    objets = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(contenu)).encode()
        + b" >>\nstream\n"
        + contenu
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    flux.write(b"%PDF-1.4\n")
    positions = []
    for numero, corps in enumerate(objets, 1):
        positions.append(flux.tell())
        flux.write(f"{numero} 0 obj\n".encode() + corps + b"\nendobj\n")
    depart_xref = flux.tell()
    # pypdf est strict : chaque entrée doit faire exactement 20 octets.
    table = "".join(f"{p:010d} 00000 n \n" for p in positions)
    flux.write(
        f"xref\n0 {len(objets) + 1}\n".encode()
        + b"0000000000 65535 f \n"
        + table.encode()
        + f"trailer\n<< /Size {len(objets) + 1} /Root 1 0 R >>\n"
        f"startxref\n{depart_xref}\n%%EOF\n".encode()
    )
    return flux.getvalue()


def _b64(brut: bytes) -> str:
    return base64.b64encode(brut).decode()


def _image(nom: str = "v.png", couleur=lambda x, y: (255, 0, 0)) -> PieceJoine:
    return PieceJoine(nom=nom, mime="image/png", donnees=_b64(_png(8, 8, couleur)))


# ──────────────────────────────── Les tests ───────────────────────────────


def test_pdf_entere_dans_le_bloc_texte():
    bloc, images = pieces_jointe.analyser(
        [
            PieceJoine(
                nom="cours.pdf",
                mime="application/pdf",
                donnees=_b64(_pdf("Le chapitre 2 parle de photosynthese")),
            )
        ]
    )
    assert images == []
    assert "cours.pdf" in bloc
    assert "photosynthese" in bloc


def test_image_devient_une_partie_file_hors_du_texte():
    octets = _png(8, 8, lambda x, y: (255, 0, 0))
    bloc, images = pieces_jointe.analyser(
        [PieceJoine(nom="schema.png", mime="image/png", donnees=_b64(octets))]
    )
    assert len(images) == 1
    assert images[0]["type"] == "file"
    assert images[0]["mime"] == "image/png"
    assert images[0]["filename"] == "schema.png"
    # Aller-retour base64 exact : sinon le modèle reçoit une image abîmée.
    assert base64.b64decode(images[0]["url"].split(",", 1)[1]) == octets
    # Le nom est cité dans le texte, les octets jamais.
    assert "schema.png" in bloc
    assert _b64(octets) not in bloc


def test_pdf_et_image_dans_le_meme_message():
    bloc, images = pieces_jointe.analyser(
        [
            PieceJoine(nom="a.pdf", mime="application/pdf", donnees=_b64(_pdf("enonce"))),
            _image("b.png", lambda x, y: (0, 0, 255)),
        ]
    )
    assert len(images) == 1
    assert "a.pdf" in bloc and "b.png" in bloc


def test_mime_vide_tombe_sur_lextension():
    """`image/*` ou un mime absent : le navigateur n'envoie pas toujours le type."""
    _, images = pieces_jointe.analyser(
        [PieceJoine(nom="photo.JPEG", mime="", donnees=_b64(_png(4, 4, lambda x, y: (1, 2, 3))))]
    )
    assert images[0]["mime"] == "image/jpeg"


def test_prefixe_data_url_accepte():
    octets = _png(4, 4, lambda x, y: (9, 9, 9))
    _, images = pieces_jointe.analyser(
        [PieceJoine(nom="c.png", mime="image/png", donnees=f"data:image/png;base64,{_b64(octets)}")]
    )
    assert len(images) == 1


def test_format_non_gere_refuse_nomme():
    try:
        pieces_jointe.analyser([PieceJoine(nom="projet.exe", mime="", donnees=_b64(b"MZ\x90"))])
    except pieces_jointe.ErreurPiece as exc:
        assert "projet.exe" in str(exc)
    else:
        raise AssertionError("un .exe doit être refusé")


def test_base64_invalide_refuse():
    try:
        pieces_jointe.analyser(
            [PieceJoine(nom="x.png", mime="image/png", donnees="pas du base64 !!!")]
        )
    except pieces_jointe.ErreurPiece:
        pass
    else:
        raise AssertionError("du contenu illisible doit être refusé")


def test_image_trop_lourde_refuse():
    """Une grosse photo doit être refusée ici, pas faire exploser la requête."""
    try:
        pieces_jointe.analyser(
            [PieceJoine(nom="gros.png", mime="image/png", donnees=_b64(b"\x89PNG" + b"0" * 9_000_000))]
        )
    except pieces_jointe.ErreurPiece as exc:
        assert "Mo" in str(exc)
    else:
        raise AssertionError("une image de 9 Mo doit être refusée")


def test_pdf_sans_texte_extractible_refuse():
    """Un PDF scanné n'a aucun texte à donner : le dire, plutôt qu'envoyer du vide."""
    try:
        pieces_jointe.analyser(
            [PieceJoine(nom="scan.pdf", mime="application/pdf", donnees=_b64(b"%PDF-1.4\ngarbage"))]
        )
    except pieces_jointe.ErreurPiece as exc:
        assert "scan.pdf" in str(exc)
    else:
        raise AssertionError("un PDF illisible doit être refusé")


def test_trop_de_pieces_refuse():
    morceaux = [_image(f"{i}.png") for i in range(9)]
    try:
        pieces_jointe.analyser(morceaux)
    except pieces_jointe.ErreurPiece as exc:
        assert "maximum" in str(exc)
    else:
        raise AssertionError("9 pièces doivent être refusées")


def test_bloc_pieces_jamais_tronque():
    """Le PDF fait partie de la question : le tronquer répondrait sur une pièce
    jointe amputée, sans que ni l'utilisateur ni le modèle ne le voie."""
    bloc, _ = pieces_jointe.analyser(
        [PieceJoine(nom="cours.pdf", mime="application/pdf", donnees=_b64(_pdf("A" * 30_000)))]
    )
    prompt = agent_discussion.construire_prompt("Resume", "(projet vide)", "", "", bloc_pieces=bloc)
    assert "AAAAAAAAAA" in prompt
    assert len(prompt) <= agent_discussion.CONTEXTE_MAX_CAR + 40_000


def test_images_atteignent_le_moteur_opencode():
    """Le chemin qui compte : les parties image doivent arriver jusqu'à OpenCode."""
    import app.services.opencode as oc

    vu: dict[str, object] = {}
    orig = oc.repondre_chat

    def capture(racine, prompt, timeout=None, images=None):
        vu["images"] = images
        return "reponse"

    oc.repondre_chat = capture
    try:
        _, images = pieces_jointe.analyser([_image()])
        list(agent_discussion.stream_reponse("question", images=images))
    finally:
        oc.repondre_chat = orig
    assert vu["images"] and vu["images"][0]["type"] == "file"


def test_sans_piece_jointe_appel_identique():
    """Sans pièce jointe, l'appel reste celui d'avant : aucun mot-clé ajouté."""
    import app.services.opencode as oc

    orig = oc.repondre_chat
    args: dict[str, object] = {}

    def capture(*positionnels, **mots):
        args["positionnels"] = positionnels
        args["mots"] = mots
        return "reponse"

    oc.repondre_chat = capture
    try:
        list(agent_discussion.stream_reponse("question"))
    finally:
        oc.repondre_chat = orig
    assert args["mots"] == {}
    assert len(args["positionnels"]) == 2


def test_moteurs_de_secours_inchangees():
    """Gemini/Groq ne savent pas recevoir de partie `file` : leur entrée de chaîne
    doit rester INTÉGRALEMENT celle d'avant, sinon la bascule de secours casse.
    Le lien vers les images se fait par `partial`, uniquement sur OpenCode."""
    from app.services import moteurs

    sans_image = dict(agent_discussion._engins())
    avec_image = dict(agent_discussion._engins([{"type": "file"}]))
    assert sans_image["gemini"] is moteurs.gemini_flux
    assert sans_image["groq"] is moteurs.groq_flux
    assert avec_image["gemini"] is moteurs.gemini_flux
    assert avec_image["groq"] is moteurs.groq_flux
    # Sans image, OpenCode est passé tel quel (appel à un seul argument).
    assert sans_image["opencode"] is agent_discussion._flux_opencode
    assert avec_image["opencode"] is not agent_discussion._flux_opencode


def _collecte(gen):
    async def _run():
        return [evt async for evt in gen]

    return asyncio.run(_run())


def test_erreur_piece_interrompt_avant_le_premier_token():
    """Une pièce refusée produit un unique `erreur` : ni `debut`, ni `texte`."""
    agent._MEMOIRE.pop(("chat", "sess-piece"), None)
    try:
        events = _collecte(
            agent.generer_discussion(
                RequeteChat(
                    mode="chat",
                    message="salut",
                    session="sess-piece",
                    pieces=[PieceJoine(nom="x.exe", mime="", donnees=_b64(b"MZ"))],
                )
            )
        )
        assert [e["event"] for e in events] == ["erreur"]
        charge = json.loads(events[0]["data"])
        assert charge["code"] == "piece_invalide"
        assert "x.exe" in charge["message"]
    finally:
        agent._MEMOIRE.pop(("chat", "sess-piece"), None)
        nom = hashlib.sha256(b"chat:sess-piece").hexdigest() + ".json"
        (agent.MEMOIRE_DIR / nom).unlink(missing_ok=True)


def test_pieces_ignorees_en_mode_edit():
    """Le bouton « + » n'existe qu'en Chat : même envoyées par erreur, des pièces
    ne doivent jamais atteindre la consigne de l'agent d'édition."""
    vu: dict[str, object] = {}

    def propositions(racine, message, fichiers=None, memoire=None, sid=None):
        vu["message"] = message
        return {"texte_resume": "", "propositions": []}

    orig = agent.agent_chat.generer_propositions_moteur
    orig_serveur = agent.opencode.assurer_serveur
    orig_session = agent.opencode.creer_session
    agent.agent_chat.generer_propositions_moteur = propositions
    agent.opencode.assurer_serveur = lambda racine: False
    agent.opencode.creer_session = lambda racine, *a, **k: "sess-opencode"
    try:
        list(
            _collecte(
                agent.generer_evenements(
                    RequeteChat(
                        mode="edit",
                        projet=str(pathlib.Path.cwd()),
                        message="corrige",
                        session="sess-edit-piece",
                        pieces=[_image()],
                    ),
                    pathlib.Path.cwd(),
                )
            )
        )
    finally:
        agent.agent_chat.generer_propositions_moteur = orig
        agent.opencode.assurer_serveur = orig_serveur
        agent.opencode.creer_session = orig_session
        agent._SESSIONS_OPENCODE.clear()
        agent._MEMOIRE.pop(("edit", "sess-edit-piece"), None)
    consigne = str(vu.get("message", ""))
    assert consigne == "corrige"


def _tout_executer():
    tests = sorted(
        (nom, obj) for nom, obj in globals().items()
        if nom.startswith("test_") and callable(obj)
    )
    nb_ok = 0
    for nom, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [E] {nom} : {exc!r}")
        else:
            nb_ok += 1
            print(f"  [OK] {nom}")
    print(f"\n{nb_ok}/{len(tests)} tests reussis")
    return 0 if nb_ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_tout_executer())