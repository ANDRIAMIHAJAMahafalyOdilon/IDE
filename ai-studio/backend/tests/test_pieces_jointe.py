"""Tests des pièces jointes du Chat (services/pieces_jointe.py).

Exécutables sans réseau, sans clé LLM et sans serveur OpenCode :
     python tests/test_pieces_jointe.py

Les décisions visibles à l'écran sont couvertes ici : le texte du PDF entre dans
le PROMPT, les octets des images/PDF restent disponibles pour les moteurs
multimodaux, et une pièce refusée arrête le flux AVANT le premier token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import itertools
import json
import pathlib
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.api import agent  # noqa: E402
from app.models.chat import PieceJoine, RequeteChat, RequetePieces  # noqa: E402
from app.services import agent_discussion, moteurs, pieces_jointe  # noqa: E402

# Les pièces sont désormais écrites sur disque : on les confine à un dossier
# temporaire pour ne rien laisser dans les données réelles de l'utilisateur.
pieces_jointe.MEMOIRE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="aistudio-pieces-"))
_FILS = itertools.count()


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


def _analyser(pieces):
    """Conversion d'une pièce neuve, par le chemin réellement emprunté en production :
    le fil la conserve, puis chaque requête relit l'inventaire."""
    fil = f"test-{next(_FILS)}"
    pieces_jointe.ajouter(fil, pieces)
    return pieces_jointe.etat(fil)


# ──────────────────────────────── Les tests ───────────────────────────────


def test_pdf_entere_dans_le_bloc_texte():
    bloc, images = _analyser(
        [
            PieceJoine(
                nom="cours.pdf",
                mime="application/pdf",
                donnees=_b64(_pdf("Le chapitre 2 parle de photosynthese")),
            )
        ]
    )
    assert len(images) == 1
    assert images[0]["mime"] == "application/pdf"
    assert "cours.pdf" in bloc
    assert "photosynthese" in bloc


def test_image_devient_une_partie_file_hors_du_texte():
    octets = _png(8, 8, lambda x, y: (255, 0, 0))
    bloc, images = _analyser(
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


def test_image_est_formatee_pour_groq_vision_et_gemini():
    octets = _png(2, 2, lambda x, y: (12, 34, 56))
    _, parties = _analyser(
        [PieceJoine(nom="vision.png", mime="image/png", donnees=_b64(octets))]
    )
    contenu, vision = moteurs._contenu_groq("question", parties)
    assert vision is True
    assert contenu[1]["type"] == "image_url"
    assert contenu[1]["image_url"]["url"].startswith("data:image/png;base64,")

    class FauxTypes:
        class Part:
            @staticmethod
            def from_bytes(data, mime_type):
                return {"data": data, "mime_type": mime_type}

    contenus = moteurs._contenus_gemini("question", parties, FauxTypes)
    assert contenus[1]["data"] == octets
    assert contenus[1]["mime_type"] == "image/png"


def test_image_transmise_aux_moteurs_est_redimensionnee_sans_modifier_original():
    from PIL import Image

    tampon = io.BytesIO()
    Image.new("RGB", (2400, 1600), (35, 90, 160)).save(tampon, format="PNG")
    octets = tampon.getvalue()
    original = {
        "type": "file",
        "mime": "image/png",
        "filename": "photo.png",
        "url": f"data:image/png;base64,{_b64(octets)}",
    }

    preparee = pieces_jointe.optimiser_images_pour_modeles([original])

    assert original["mime"] == "image/png"
    assert original["url"].endswith(_b64(octets))
    assert preparee[0]["mime"] == "image/jpeg"
    assert len(base64.b64decode(preparee[0]["url"].split(",", 1)[1])) <= pieces_jointe.IMAGE_MODELE_MAX_OCTETS
    with Image.open(io.BytesIO(pieces_jointe._decoder(preparee[0]["url"]))) as image:
        assert max(image.size) <= pieces_jointe.IMAGE_MODELE_COTE_MAX


def test_groq_413_est_classe_comme_corps_trop_volumineux():
    erreur = moteurs._classer(Exception("413 Payload Too Large"), "groq")
    assert erreur.code == "requete_trop_volumineuse"
    assert "taille maximale" in erreur.message


def test_groq_429_reste_un_quota_meme_si_le_corps_mentionne_la_taille():
    class Reponse:
        status_code = 429
        headers = {"retry-after": "2"}

    class Erreur:
        response = Reponse()
        status_code = 429

        def __str__(self):
            return "request too large; retry after quota reset"

    erreur = moteurs._classer(Erreur(), "groq")
    assert erreur.code == "quota"
    assert erreur.retry_after == 2


def test_pdf_et_image_dans_le_meme_message():
    bloc, images = _analyser(
        [
            PieceJoine(nom="a.pdf", mime="application/pdf", donnees=_b64(_pdf("enonce"))),
            _image("b.png", lambda x, y: (0, 0, 255)),
        ]
    )
    assert len(images) == 2
    assert {partie["mime"] for partie in images} == {"application/pdf", "image/png"}
    assert "a.pdf" in bloc and "b.png" in bloc


def test_mime_vide_tombe_sur_lextension():
    """`image/*` ou un mime absent : le navigateur n'envoie pas toujours le type."""
    _, images = _analyser(
        [PieceJoine(nom="photo.JPEG", mime="", donnees=_b64(_png(4, 4, lambda x, y: (1, 2, 3))))]
    )
    assert images[0]["mime"] == "image/jpeg"


def test_prefixe_data_url_accepte():
    octets = _png(4, 4, lambda x, y: (9, 9, 9))
    _, images = _analyser(
        [PieceJoine(nom="c.png", mime="image/png", donnees=f"data:image/png;base64,{_b64(octets)}")]
    )
    assert len(images) == 1


def test_format_non_gere_refuse_nomme():
    try:
        _analyser([PieceJoine(nom="projet.exe", mime="", donnees=_b64(b"MZ\x90"))])
    except pieces_jointe.ErreurPiece as exc:
        assert "projet.exe" in str(exc)
    else:
        raise AssertionError("un .exe doit être refusé")


def test_base64_invalide_refuse():
    try:
        _analyser(
            [PieceJoine(nom="x.png", mime="image/png", donnees="pas du base64 !!!")]
        )
    except pieces_jointe.ErreurPiece:
        pass
    else:
        raise AssertionError("du contenu illisible doit être refusé")


def test_image_trop_lourde_refuse():
    """Une grosse photo doit être refusée ici, pas faire exploser la requête."""
    try:
        _analyser(
            [PieceJoine(nom="gros.png", mime="image/png", donnees=_b64(b"\x89PNG" + b"0" * 9_000_000))]
        )
    except pieces_jointe.ErreurPiece as exc:
        assert "Mo" in str(exc)
    else:
        raise AssertionError("une image de 9 Mo doit être refusée")


def test_pdf_sans_texte_extractible_reste_multimodal():
    """Un PDF scanné valide est gardé pour Gemini/OpenCode, pas rejeté."""
    bloc, parties = _analyser(
        [PieceJoine(nom="scan.pdf", mime="application/pdf", donnees=_b64(_pdf("")))]
    )
    assert "sans texte extractible" in bloc
    assert parties[0]["mime"] == "application/pdf"


def test_selection_pertinente_des_pieces_selon_la_question():
    fil = f"test-{next(_FILS)}"
    pieces_jointe.ajouter(
        fil,
        [
            _image("schema.png"),
            PieceJoine(nom="contrat.pdf", mime="application/pdf", donnees=_b64(_pdf("conditions"))),
            PieceJoine(nom="facture.pdf", mime="application/pdf", donnees=_b64(_pdf("montant 42"))),
        ],
    )
    bloc, parties = pieces_jointe.etat(fil, "quel montant dans la facture ?")
    assert "facture.pdf" in bloc and "contrat.pdf" not in bloc
    assert len(parties) == 1 and parties[0]["filename"] == "facture.pdf"


def test_trop_de_pieces_refuse():
    morceaux = [_image(f"{i}.png") for i in range(9)]
    try:
        _analyser(morceaux)
    except pieces_jointe.ErreurPiece as exc:
        assert "maximum" in str(exc)
    else:
        raise AssertionError("9 pièces doivent être refusées")


def test_bloc_pieces_jamais_tronque():
    """Le PDF fait partie de la question : le tronquer répondrait sur une pièce
    jointe amputée, sans que ni l'utilisateur ni le modèle ne le voie."""
    bloc, _ = _analyser(
        [PieceJoine(nom="cours.pdf", mime="application/pdf", donnees=_b64(_pdf("A" * 30_000)))]
    )
    prompt = agent_discussion.construire_prompt("Resume", "(projet vide)", "", "", bloc_pieces=bloc)
    assert "AAAAAAAAAA" in prompt
    assert len(prompt) <= agent_discussion.CONTEXTE_MAX_CAR + 40_000


def test_chat_utilise_opencode_en_dernier_secours_avec_image():
    """OpenCode reçoit aussi l'image si les deux moteurs Vision échouent."""
    from app.services import opencode

    def cloud_indisponible(*args, **kwargs):
        raise moteurs.ErreurMoteur("moteur_indisponible", "vision indisponible")
        yield  # pragma: no cover

    originaux = (moteurs.groq_flux, moteurs.gemini_flux)
    original_opencode = opencode.repondre_chat
    arguments: dict[str, object] = {}

    def opencode_repondre(directory, prompt, timeout=None, images=None):
        arguments["images"] = images
        return "J'ai analysé l'image."

    moteurs.groq_flux = cloud_indisponible
    moteurs.gemini_flux = cloud_indisponible
    opencode.repondre_chat = opencode_repondre
    try:
        _, images = _analyser([_image()])
        evenements = list(agent_discussion.stream_reponse("question", images=images))
        assert evenements[0] == ("moteur", "opencode")
        assert "".join(c for genre, c in evenements if genre == "delta") == "J'ai analysé l'image."
        assert arguments["images"] == images
    finally:
        moteurs.groq_flux, moteurs.gemini_flux = originaux
        opencode.repondre_chat = original_opencode


def test_sans_piece_jointe_appel_identique():
    """Sans pièce jointe, l'appel reste celui d'avant : aucun mot-clé ajouté."""
    import app.services.opencode as oc

    orig = oc.repondre_chat
    sauv = agent_discussion._engins
    args: dict[str, object] = {}

    def capture(*positionnels, **mots):
        args["positionnels"] = positionnels
        args["mots"] = mots
        return "reponse"

    oc.repondre_chat = capture
    def cloud_indisponible(c):
        from app.services.moteurs import ErreurMoteur
        raise ErreurMoteur("moteur_indisponible", "cloud indisponible")
        yield  # pragma: no cover

    orig_flux = agent_discussion._flux_opencode
    agent_discussion._engins = lambda images=None: [
        ("groq", cloud_indisponible),
        ("gemini", cloud_indisponible),
        ("opencode", orig_flux),
    ]
    try:
        list(agent_discussion.stream_reponse("question"))
    finally:
        agent_discussion._engins = sauv
        oc.repondre_chat = orig
    assert args["mots"] == {}
    assert len(args["positionnels"]) == 2


def test_chat_avec_piece_jointe_garde_opencode_en_dernier_secours():
    """L'image est transmise au secours OpenCode, le PDF reste du texte extrait."""
    from app.services import moteurs

    sans_image = dict(agent_discussion._engins())
    parties_image = [{"type": "file", "mime": "image/png"}]
    avec_image = dict(agent_discussion._engins(parties_image))
    noms_image = [nom for nom, _ in agent_discussion._engins(parties_image)]
    parties_pdf = [{"type": "file", "mime": "application/pdf"}]
    noms_pdf = [nom for nom, _ in agent_discussion._engins(parties_pdf)]

    assert sans_image["gemini"] is moteurs.gemini_flux
    assert sans_image["groq"] is moteurs.groq_flux
    assert avec_image["gemini"] is not moteurs.gemini_flux
    assert avec_image["groq"] is not moteurs.groq_flux
    assert avec_image["gemini"].keywords["pieces"] == parties_image
    assert avec_image["groq"].keywords["pieces"] == parties_image
    assert noms_image == ["groq", "gemini", "opencode"]
    assert noms_pdf == ["gemini", "groq", "opencode"]
    assert avec_image["opencode"].keywords["images"] == parties_image
    # Sans pièce, la chaîne habituelle reste inchangée.
    assert sans_image["opencode"] is agent_discussion._flux_opencode


def _collecte(gen):
    async def _run():
        return [evt async for evt in gen]

    return asyncio.run(_run())


# ─────────────── La pièce survit aux requêtes suivantes (c'est le contrat) ───


def test_pdf_reste_disponible_au_message_suivant():
    """Le reproche utilisateur : « je joins le PDF, puis à la requête suivante il ne
    s'en souvient plus ». Le fil doit donc réinjecter le PDF SANS qu'on le renvoie."""
    fil = f"test-{next(_FILS)}"
    session = f"sess-{fil}"
    agent._MEMOIRE.pop(("chat", session), None)
    vus: list[str] = []

    def _moteur(message, images=None):
        vus.append(message)
        return iter([("texte", {"delta": "ok"})])

    original = agent.agent_discussion.stream_reponse
    agent.agent_discussion.stream_reponse = _moteur
    try:
        pieces = [
            PieceJoine(nom="cours.pdf", mime="application/pdf", donnees=_b64(_pdf("ZEBRE")))
        ]
        # Requête 1 : la pièce est jointe au message.
        _collecte(
            agent.generer_discussion(
                RequeteChat(mode="chat", message="résume", session=session, pieces=pieces)
            )
        )
        # Requête 2 : plus rien n'est envoyé, et le PDF doit être là quand même.
        _collecte(
            agent.generer_discussion(
                RequeteChat(mode="chat", message="et le chapitre 3 ?", session=session)
            )
        )
        assert len(vus) == 2
        assert "ZEBRE" in vus[0]
        assert "ZEBRE" in vus[1], "le PDF a été oublié entre deux requêtes du même fil"
    finally:
        agent.agent_discussion.stream_reponse = original
        agent._MEMOIRE.pop(("chat", session), None)


def test_image_reste_transmise_au_message_suivant():
    fil = f"test-{next(_FILS)}"
    session = f"sess-{fil}"
    agent._MEMOIRE.pop(("chat", session), None)
    vues: list[int] = []

    def _moteur(message, images=None):
        vues.append(len(images or []))
        return iter([("texte", {"delta": "ok"})])

    original = agent.agent_discussion.stream_reponse
    agent.agent_discussion.stream_reponse = _moteur
    try:
        _collecte(
            agent.generer_discussion(
                RequeteChat(mode="chat", message="ça dit quoi ?", session=session, pieces=[_image()])
            )
        )
        _collecte(
            agent.generer_discussion(
                RequeteChat(mode="chat", message="et la couleur ?", session=session)
            )
        )
        assert vues == [1, 1], "l'image doit être renvoyée au moteur à chaque requête"
    finally:
        agent.agent_discussion.stream_reponse = original
        agent._MEMOIRE.pop(("chat", session), None)


def test_oublier_une_piece_la_retire_des_requets_suivants():
    fil = f"test-{next(_FILS)}"
    pieces_jointe.ajouter(fil, [_image("a.png")])
    assert len(pieces_jointe.etat(fil)[1]) == 1
    restant = pieces_jointe.retirer(fil, pieces_jointe.liste(fil)[0]["cle"])
    assert restant == []
    assert pieces_jointe.etat(fil) == ("", [])


def test_le_plafond_porte_sur_la_conversation_pas_sur_le_message():
    """6 pièces pour toute la conversation : c'est ce que voit l'utilisateur."""
    fil = f"test-{next(_FILS)}"
    for i in range(6):
        pieces_jointe.ajouter(fil, [_image(f"img{i}.png")])
    try:
        pieces_jointe.ajouter(fil, [_image("trop.png")])
    except pieces_jointe.ErreurPiece as exc:
        assert "maximum" in str(exc)
    else:
        raise AssertionError("la 7e pièce aurait dû être refusée")


def test_la_meme_piece_jointe_deux_fois_ne_compte_quune_seule():
    fil = f"test-{next(_FILS)}"
    pieces_jointe.ajouter(fil, [_image("a.png")])
    pieces_jointe.ajouter(fil, [_image("a.png")])
    assert len(pieces_jointe.liste(fil)) == 1


def test_manifeste_corrompu_ne_casse_pas_le_chat():
    fil = f"test-{next(_FILS)}"
    pieces_jointe.ajouter(fil, [_image("a.png")])
    (pieces_jointe._dossier(fil) / "manifest.json").write_text("{ tronqué", encoding="utf-8")
    assert pieces_jointe.etat(fil) == ("", [])


def test_route_attachement_puis_inventaire_puis_oubli():
    """Parcours réellement emprunté par le navigateur : POST, GET, DELETE."""
    session = f"sess-{next(_FILS)}"
    req = RequetePieces(session=session, pieces=[_image("a.png")])
    pose = asyncio.run(agent.chat_pieces(req))
    assert [p["nom"] for p in pose["pieces"]] == ["a.png"]
    assert "donnees" not in pose["pieces"][0], "les octets ne sortent jamais du backend"
    lu = asyncio.run(agent.chat_pieces_liste(session))
    assert lu["pieces"] == pose["pieces"]
    restant = asyncio.run(agent.chat_pieces_retrait(session, pose["pieces"][0]["cle"]))
    assert restant["pieces"] == []


def test_message_id_est_persiste_sur_la_piece_et_reste_stable():
    """L'envoi lie la pièce pré-téléversée au message et la réutilise ensuite."""
    session = f"sess-{next(_FILS)}"
    pieces_jointe.ajouter(session, [_image("image.png")])
    assert pieces_jointe.liste(session)[0]["size"] > 0

    vues: list[tuple[str, int]] = []

    def _moteur(message, images=None):
        vues.append((message, len(images or [])))
        return iter([("texte", {"delta": "réponse simulée"})])

    original = agent.agent_discussion.stream_reponse
    agent.agent_discussion.stream_reponse = _moteur
    try:
        _collecte(
            agent.generer_discussion(
                RequeteChat(
                    mode="chat",
                    message="Qui est cette personne ?",
                    session=session,
                    message_id="message_123",
                )
            )
        )
        assert pieces_jointe.liste(session)[0]["message_id"] == "message_123"

        # Deuxième message sans pièce envoyée : l'image reste au premier message
        # et reste utilisable dans la même conversation.
        _collecte(
            agent.generer_discussion(
                RequeteChat(
                    mode="chat",
                    message="Peux-tu analyser encore cette image ?",
                    session=session,
                    message_id="message_456",
                )
            )
        )
        assert pieces_jointe.liste(session)[0]["message_id"] == "message_123"
        assert [nombre for _, nombre in vues] == [1, 1]
    finally:
        agent.agent_discussion.stream_reponse = original


def test_route_attachement_refuse_une_piece_invalide():
    from fastapi import HTTPException

    req = RequetePieces(
        session=f"sess-{next(_FILS)}",
        pieces=[PieceJoine(nom="x.exe", mime="", donnees=_b64(b"MZ"))],
    )
    try:
        asyncio.run(agent.chat_pieces(req))
    except HTTPException as exc:
        assert exc.status_code == 422
        assert "x.exe" in str(exc.detail)
    else:
        raise AssertionError("une pièce non supportée aurait dû être refusée")


def test_erreur_piece_interrompt_avant_le_premier_token():
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


def test_pieces_persistantes_en_mode_edit():
    """Edit reçoit désormais le contexte texte/métadonnées de ses pièces."""
    vu: dict[str, object] = {}

    def propositions(racine, message, fichiers=None, memoire=None, sid=None, bloc_pieces="", pieces=None):
        vu["message"] = message
        vu["pieces"] = bloc_pieces
        vu["images"] = pieces
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
    assert "v.png" in str(vu.get("pieces", ""))


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
