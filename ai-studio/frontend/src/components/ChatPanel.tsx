import { memo, useEffect, useMemo, useRef, useState } from "react";
import {
  appliquerModifs,
  arreterTache,
  chatAgent,
  choisirDossierNatif,
  etatTache,
  etatFichiers,
  listerDocuments,
  reindexerIndex,
  repondrePermission as repondrePermissionApi,
  tacheAgent,
  uploaderDocument,
} from "../api/client";
import type { ReponseDocuments } from "../api/client";
import { useDiscussions } from "../store/discussions";
import type { MessageDisc } from "../store/discussions";
import { useStudio } from "../store/studio";
import type {
  EvenementTache,
  Modification,
  PieceJoine,
  Proposition,
  ResultatFichier,
} from "../types/api";
import { DiffView } from "./DiffView";
import { Markdown } from "./Markdown";
import { AgentActivityTimeline } from "./AgentActivityTimeline";

const ETIQUETTES_ERREUR: Record<string, string> = {
  timeout: "Temps de réponse dépassé",
  quota: "Quota moteur dépassé",
  palier_gratuit: "Palier gratuit bloqué (CLI)",
  auth: "Authentification du moteur",
  parse_format: "Format de sortie de l'agent non reconnu",
  schema_invalide: "Proposition invalide",
  hors_projet: "Chemin hors projet",
  moteur_indisponible: "Aucun moteur disponible",
  serveur_indisponible: "Serveur agent injoignable",
  permission_indisponible: "Permission Roch indisponible",
  opencode: "Erreur Roch",
  piece_invalide: "Pièce jointe refusée",
  interne: "Erreur interne",
};

/** Pièce jointe prête à partir : le contenu est déjà en base64. */
interface PieceUI {
  cle: string;
  nom: string;
  mime: string;
  ko: number;
  donnees: string;
}

/** Mêmes plafonds que `services/pieces_jointe.py` : inutile d'envoyer 12 Mo
 *  pour recevoir un refus côté serveur, le retour serait plus lent qu'un message. */
const PIECE_MAX = 6;
const IMAGE_MAX = 8_000_000;
const PDF_MAX = 25_000_000;
const MIMES_PIECE = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
];

function lireEnBase64(fichier: File): Promise<string> {
  return new Promise((resoudre, rejeter) => {
    const lecteur = new FileReader();
    lecteur.onerror = () => rejeter(new Error(`Lecture impossible : ${fichier.name}`));
    lecteur.onload = () => {
      const resultat = String(lecteur.result ?? "");
      const virgule = resultat.indexOf(",");
      resoudre(virgule >= 0 ? resultat.slice(virgule + 1) : resultat);
    };
    lecteur.readAsDataURL(fichier);
  });
}

/** Windows et certains sélecteurs envoient un mime vide : l'extension décide. */
function mimeDevine(nom: string): string {
  const extension = nom.slice(nom.lastIndexOf(".")).toLowerCase();
  if (extension === ".png") return "image/png";
  if (extension === ".jpg" || extension === ".jpeg" || extension === ".jfif") return "image/jpeg";
  if (extension === ".gif") return "image/gif";
  if (extension === ".webp") return "image/webp";
  if (extension === ".pdf") return "application/pdf";
  return "";
}

interface BulleChatProps {
  message: MessageDisc;
  dernier: boolean;
  busy: boolean;
}

/** Bulle de discussion mémoïsée : seuls les changements de texte re-parsent le
 * Markdown (un delta SSE ne re-rend que la dernière bulle, pas tout le fil). */
const BulleChat = memo(function BulleChat({ message, dernier, busy }: BulleChatProps) {
  return (
    <div className={"msg-chat msg-" + message.role}>
      <div className="msg-bulle">
        {message.role === "assistant" ? (
          <Markdown texte={message.texte || ""} />
        ) : (
          message.texte ||
          (busy && dernier ? "…" : "")
        )}
      </div>
    </div>
  );
});

interface TacheOpenCode {
  id: string;
  consigne: string;
  events: EvenementTache[];
}

/** Une section d'activité d'affilée, ou une réponse de Roch. */
type BlocTache =
  | { type: "activite"; cle: string; events: EvenementTache[] }
  | { type: "texte"; cle: string; texte: string };

/**
 * Découpe le flux d'une tâche en blocs ORDONNÉS : une suite d'activités, puis
 * la réponse de Roch, puis d'autres activités…
 *
 * Avant, la timeline recevait tous les événements d'un coup et les réponses
 * étaient rendues dans un secondlot, après le panneau : tout ce que Roch disait
 * atterrisson sous son dernier outil, même quand le texte était arrivé avant.
 * Le rendu suit maintenant l'ordre réel d'arrivée, et chaque réponse est une
 * vraie bulle de chat posée dans le fil, comme en mode Chat.
 */
function blocsTache(events: EvenementTache[]): BlocTache[] {
  const blocs: BlocTache[] = [];
  let activite: EvenementTache[] = [];
  const viderActivites = () => {
    if (activite.length) {
      blocs.push({ type: "activite", cle: "activite:" + blocs.length, events: activite });
      activite = [];
    }
  };
  for (const evt of events) {
    if (evt.event === "texte") {
      // Les deltas successifs d'une MÊME réponse forment un seul bloc de texte.
      // Sans cette fusion, chaque delta de 80 caractères devenait une bulle
      // Chat distincte : une seule réponse s'affichait en une dizaine de
      // bulles séparées, ce qui est précisément l'affichage « en désordre ».
      // Seul un événement d'activité entre deux deltas doit couper le bloc :
      // `activite` est vidée en différé, donc une activité en attente doit
      // aussi empêcher la fusion, sinon l'ordre texte/activité est perdu.
      const dernier = blocs[blocs.length - 1];
      if (dernier?.type === "texte" && activite.length === 0) {
        dernier.texte += evt.data.delta;
        continue;
      }
      viderActivites();
      blocs.push({
        type: "texte",
        cle: "texte:" + blocs.length,
        texte: evt.data.delta,
      });
    } else if (evt.event === "activite" || evt.event === "permission") {
      activite.push(evt);
    }
  }
  viderActivites();
  return blocs;
}

interface ChatPanelProps {
  /** Projet courant ; absent = Chat possible (sans contexte projet), Edit non. */
  projet?: string;
}

export function ChatPanel({ projet }: ChatPanelProps) {
  const modeAgent = useStudio((s) => s.modeAgent);
  const setModeAgent = useStudio((s) => s.setModeAgent);
  const projetEdit = projet ?? "demo";

  // Conversations (mode Chat) : historique multi-sessions, fil courant.
  const toutesSessions = useDiscussions((s) => s.sessions);
  const sessions = useMemo(
    () => toutesSessions.filter((session) => session.mode === "chat"),
    [toutesSessions],
  );
  const activeId = useDiscussions((s) => s.activeIds.chat);
  const activeEditId = useDiscussions((s) => s.activeIds.edit);
  const sessionEditHistorique = useDiscussions(
    (s) => s.sessions.find((session) => session.id === s.activeIds.edit && session.mode === "edit") ?? null,
  );
  const active = sessions.find((s) => s.id === activeId) ?? null;

  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [contexteDocs, setContexteDocs] = useState(false);
  const [contexteWeb, setContexteWeb] = useState(false);
  const [contexteProjet, setContexteProjet] = useState(false);
  const [menuPlus, setMenuPlus] = useState(false);
  const [docs, setDocs] = useState<ReponseDocuments | null>(null);
  const [docsErreur, setDocsErreur] = useState<string | null>(null);
  const [docsBusy, setDocsBusy] = useState(false);
  const fichierInputRef = useRef<HTMLInputElement | null>(null);
  // Pièces jointes du message en cours (Chat uniquement) : elles partent avec
  // l'envoi puis sont vidées, faute de quoi la pièce resterait attachée au
  // message suivant — l'utilisateur enverrait la photo une seconde fois.
  const [pieces, setPieces] = useState<PieceUI[]>([]);
  const pieceInputRef = useRef<HTMLInputElement | null>(null);
  // Cartes REPLIÉES, et non dépliées : le vide signifie « tout déplié ». Voir
  // `deployee` plus bas — le diff est visible dès son arrivée, c'est l'information
  // que l'utilisateur attend d'une proposition.
  const [cartesRepliees, setCartesRepliees] = useState<Set<string>>(new Set());
  const abortRef = useRef<AbortController | null>(null);
  const fluxRef = useRef<HTMLDivElement | null>(null);

  // Fil Edit (proposer + valider).
  const [sessionEdit, setSessionEdit] = useState<string | null>(null);
  const [moteurEdit, setMoteurEdit] = useState<string | null>(null);
  const [executionEdit, setExecutionEdit] = useState<"apercu" | "autonome">("autonome");
  const [texte, setTexte] = useState<string[]>([]);
  const [props, setProps] = useState<Proposition[]>([]);
  const [acceptes, setAcceptes] = useState<Map<string, Set<number>>>(new Map());
  const [resultats, setResultats] = useState<ResultatFichier[]>([]);
  const [tachesOpenCode, setTachesOpenCode] = useState<TacheOpenCode[]>([]);
  const tacheCouranteRef = useRef<string | null>(null);
  const lancementAutonomeRef = useRef(false);
  const tacheSessionRef = useRef<string | null>(null);
  const repriseTacheRef = useRef<string | null>(null);
  const [tacheActive, setTacheActive] = useState(false);
  const [erreur, setErreur] = useState<{ code: string; message: string } | null>(null);
  const [erreurChat, setErreurChat] = useState<{ code: string; message: string } | null>(null);

  const enChat = modeAgent === "chat";
  const moteurAff = enChat ? (active?.moteur ?? null) : moteurEdit;
  const messagesEdit = sessionEditHistorique?.messages ?? [];
  const tacheLocaleVisible = !enChat && executionEdit === "autonome" && tachesOpenCode.length > 0;
  const messagesEditVisibles = tacheLocaleVisible
    ? messagesEdit.slice(0, -(messagesEdit.at(-1)?.role === "assistant" ? 2 : 1))
    : messagesEdit;
  const onglets = useStudio((s) => s.onglets);
  const rechargerFichiers = useStudio((s) => s.rechargerFichiers);
  const ouvrirFichier = useStudio((s) => s.ouvrirFichier);
  const empreintesTacheRef = useRef<Record<string, string | null>>({});

  // Quand on ouvre une session Edit, réaligner tout l'état local sur cette
  // session. Les activités SSE ne doivent jamais rester visibles d'un autre fil.
  useEffect(() => {
    if (enChat) return;
    // Une nouvelle tâche vient juste de créer sa session : ne pas effacer
    // son flux local au rerender qui suit `nouvelle("edit")`.
    if (tacheSessionRef.current === activeEditId) return;
    tacheSessionRef.current = null;
    repriseTacheRef.current = null;
    abortRef.current?.abort();
    abortRef.current = null;
    setSessionEdit(sessionEditHistorique?.backend ?? null);
    setMoteurEdit(sessionEditHistorique?.moteur ?? null);
    setTachesOpenCode([]);
    setTexte([]);
    setProps([]);
    setAcceptes(new Map());
    setResultats([]);
    setErreur(null);
  }, [enChat, activeEditId]);

  // Suivi du bas de flux quand une réponse arrive.
  useEffect(() => {
    const el = fluxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [
    active?.messages.length,
    active?.messages[active.messages.length - 1]?.texte.length,
    messagesEditVisibles.length,
    messagesEditVisibles[messagesEditVisibles.length - 1]?.texte.length,
    tachesOpenCode[tachesOpenCode.length - 1]?.events.length,
    busy,
  ]);

  // Suivi des onglets modifiés par le vrai agent OpenCode.
  useEffect(() => {
    if (!tacheActive || !projet || onglets.length === 0) return;
    let vivant = true;
    const chemins = onglets.map((o) => o.chemin);
    const sonder = async () => {
      try {
        const empreintes = await etatFichiers(projet, chemins);
        if (!vivant) return;
        const precedentes = empreintesTacheRef.current;
        const dirty = useStudio.getState().dirty;
        const changes = Object.keys(empreintes).filter(
          (chemin) =>
            !dirty[chemin] &&
            Object.prototype.hasOwnProperty.call(precedentes, chemin) &&
            precedentes[chemin] !== empreintes[chemin],
        );
        // Conserver la référence précédente d'un onglet dirty : après sa
        // sauvegarde, le changement externe sera encore détecté.
        for (const [chemin, empreinte] of Object.entries(empreintes)) {
          if (!dirty[chemin]) precedentes[chemin] = empreinte;
        }
        if (changes.length) await rechargerFichiers(changes);
      } catch {
        // Le flux OpenCode reste prioritaire; le polling est seulement un miroir.
      }
    };
    void sonder();
    const timer = window.setInterval(() => void sonder(), 1500);
    return () => {
      vivant = false;
      window.clearInterval(timer);
    };
  }, [tacheActive, projet, onglets, rechargerFichiers]);

  // Après un refresh, le backend peut encore exécuter la tâche. On restaure
  // son journal SSE et on se rattache au flux sans relancer la consigne.
  useEffect(() => {
    if (enChat || executionEdit !== "autonome" || !projet || !activeEditId) return;
    const projetReprise = projet;
    const sessionReprise = activeEditId;
    const cle = `${projetReprise}:${sessionReprise}`;
    if (repriseTacheRef.current === cle) return;
    repriseTacheRef.current = cle;
    let vivant = true;

    async function reprendre() {
      try {
        const etat = await etatTache(projetReprise, sessionReprise);
        if (!vivant || !etat.existe) return;
        const executionId = "reprise-" + sessionReprise;
        const consigne =
          etat.message ??
          sessionEditHistorique?.messages.find((message) => message.role === "user")?.texte ??
          "Tâche autonome";
        setTachesOpenCode((taches) =>
          taches.some((tache) => tache.id === executionId)
            ? taches
            : [...taches, { id: executionId, consigne, events: [] }],
        );
        const controller = new AbortController();
        abortRef.current = controller;
        await consommerFluxTache(
          executionId,
          sessionReprise,
          tacheAgent(projetReprise, "", { session: sessionReprise, signal: controller.signal }),
          controller,
        );
      } catch (error) {
        if (vivant) setErreur({ code: "interne", message: error instanceof Error ? error.message : String(error) });
      }
    }

    void reprendre();
    return () => {
      vivant = false;
      abortRef.current?.abort();
    };
  }, [enChat, executionEdit, projet, activeEditId, sessionEditHistorique?.id]);

  // État des cours indexés (RAG), chargé à l'ouverture du menu « ＋ ».
  useEffect(() => {
    if (!menuPlus || !enChat || docs) return;
    listerDocuments()
      .then(setDocs)
      .catch((e) => setDocsErreur(e instanceof Error ? e.message : String(e)));
  }, [menuPlus, enChat, docs]);

  // Fermeture du menu « ＋ » : clic extérieur ou touche Échap.
  const menuPlusRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!menuPlus) return;
    function fermerAuClicExt(e: MouseEvent) {
      if (menuPlusRef.current && !menuPlusRef.current.contains(e.target as Node)) {
        setMenuPlus(false);
      }
    }
    function fermerEchap(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuPlus(false);
    }
    document.addEventListener("mousedown", fermerAuClicExt);
    document.addEventListener("keydown", fermerEchap);
    return () => {
      document.removeEventListener("mousedown", fermerAuClicExt);
      document.removeEventListener("keydown", fermerEchap);
    };
  }, [menuPlus]);

  // Ouverture du dossier à modifier (edit) : le « ＋ » ouvre la VRAIE fenêtre
  // Windows via le backend (aucune copie : open-local référence le disque tel quel).
  const [erreurDossier, setErreurDossier] = useState<string | null>(null);
  const dossierLocal = useStudio((s) => s.dossierLocal);
  const ouvrirDossierLocal = useStudio((s) => s.ouvrirDossierLocal);

  // Mode biseau de la fenêtre système : la requête reste ouverte le temps du
  // choix (backend bloqué sur la fenêtre, serveur toujours disponible).
  const [choisitDossier, setChoisitDossier] = useState(false);
  async function choisirDossierViaFenetre() {
    if (choisitDossier || busy) return;
    setErreurDossier(null);
    setChoisitDossier(true);
    try {
      const chemin = await choisirDossierNatif();
      if (chemin) await ouvrirDossierLocal(chemin);
    } catch (e) {
      setErreurDossier(e instanceof Error ? e.message : String(e));
    } finally {
      setChoisitDossier(false);
    }
  }

  async function uploaderCours(fichiers: FileList | null) {
    const fichier = fichiers?.[0];
    if (!fichier) return;
    setDocsBusy(true);
    setDocsErreur(null);
    try {
      await uploaderDocument(fichier);
      setDocs(await listerDocuments());
    } catch (e) {
      setDocsErreur(e instanceof Error ? e.message : String(e));
    } finally {
      setDocsBusy(false);
      if (fichierInputRef.current) fichierInputRef.current.value = "";
    }
  }

  async function choisirPieces(fichiers: FileList | null) {
    const choisis = Array.from(fichiers ?? []);
    if (!choisis.length) return;
    setDocsErreur(null);
    const refusees: string[] = [];
    const ajoutees: PieceUI[] = [];
    for (const fichier of choisis) {
      const mime = fichier.type || mimeDevine(fichier.name);
      if (!MIMES_PIECE.includes(mime)) {
        refusees.push(`${fichier.name} : type non pris en charge`);
        continue;
      }
      const plafond = mime === "application/pdf" ? PDF_MAX : IMAGE_MAX;
      if (fichier.size > plafond) {
        refusees.push(`${fichier.name} : trop lourd (${Math.round(fichier.size / 1_000_000)} Mo)`);
        continue;
      }
      try {
        ajoutees.push({
          cle: `${fichier.name}-${fichier.size}-${fichier.lastModified}`,
          nom: fichier.name,
          mime,
          ko: Math.max(1, Math.round(fichier.size / 1024)),
          donnees: await lireEnBase64(fichier),
        });
      } catch (e) {
        refusees.push(e instanceof Error ? e.message : String(e));
      }
    }
    setPieces((precedentes) => {
      const deja = new Set(precedentes.map((p) => p.cle));
      const fusion = [...precedentes, ...ajoutees.filter((p) => !deja.has(p.cle))];
      if (fusion.length > PIECE_MAX) {
        refusees.push(`maximum ${PIECE_MAX} pièces par message`);
        return fusion.slice(0, PIECE_MAX);
      }
      return fusion;
    });
    if (refusees.length) setDocsErreur(refusees.join(" · "));
    if (pieceInputRef.current) pieceInputRef.current.value = "";
  }

  async function reindexerCours() {
    setDocsBusy(true);
    setDocsErreur(null);
    try {
      await reindexerIndex();
      const etat = await listerDocuments();
      setDocs(etat);
      if (etat.index.existe) setContexteDocs(true);
    } catch (e) {
      setDocsErreur(e instanceof Error ? e.message : String(e));
    } finally {
      setDocsBusy(false);
    }
  }

  // ── Mode CHAT : discussion libre streamée (jamais de diff) ──
  async function envoyerChat() {
    // Une pièce jointe suffit à justifier l'envoi : l'utilisateur colle une
    // photo et valide sans écrire. Sans consigne ni pièce, il n'y a rien à demander.
    const consigne = message.trim() || (pieces.length ? "Analyse la pièce jointe." : "");
    if (!consigne || busy) return;
    const jointes: PieceJoine[] = pieces.map(({ nom, mime, donnees }) => ({ nom, mime, donnees }));
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true);
    setErreurChat(null);
    setMessage("");
    setPieces([]);

    const disc = useDiscussions.getState();
    let id = activeId;
    if (!id) id = disc.nouvelle("chat");
    disc.ajouterMessage(id, "user", consigne);
    disc.ajouterMessage(id, "assistant", "");
    const backendAvant =
      useDiscussions.getState().sessions.find((x) => x.id === id)?.backend ?? null;

    try {
      for await (const evt of chatAgent(
        contexteProjet ? projet ?? null : null,
        consigne,
        {
          mode: "chat",
          session: backendAvant,
          documents: contexteDocs,
          web: contexteWeb,
          pieces: jointes,
          signal: ctrl.signal,
        },
      )) {
        if (evt.event === "debut") {
          useDiscussions.getState().fixer(id, evt.data.session ?? id, evt.data.moteur);
        } else if (evt.event === "texte") {
          useDiscussions.getState().fusionnerDelta(id, evt.data.delta);
        } else if (evt.event === "reprise") {
          // Le flux précédent s'est coupé : on efface sa réponse partielle
          // avant que le moteur de secours ne diffuse la sienne, sinon
          // l'utilisateur verrait les deux textes collés l'un à l'autre.
          const etat = useDiscussions.getState();
          etat.viderReponseEnCours(id);
          etat.fixer(id, etat.sessions.find((x) => x.id === id)?.backend ?? id, evt.data.moteur);
        } else if (evt.event === "erreur") {
          useDiscussions.getState().retirerAssistantVide(id);
          setErreurChat({
            code: evt.data.code,
            message: evt.data.message || (ETIQUETTES_ERREUR[evt.data.code] ?? evt.data.code),
          });
          break;
        }
      }
    } catch (e) {
      if (!ctrl.signal.aborted) {
        useDiscussions.getState().retirerAssistantVide(id);
        setErreurChat({
          code: "interne",
          message: e instanceof Error ? e.message : String(e),
        });
      }
    } finally {
      setBusy(false);
    }
  }

  // ── Mode EDIT : proposer (hunks) puis appliquer à la main ──
  async function envoyer() {
    const consigne = message;
    if (!consigne.trim() || busy || !projet) return;
    const historique = useDiscussions.getState();
    const sessionId = activeEditId ?? historique.nouvelle("edit");
    historique.ajouterMessage(sessionId, "user", consigne);
    setBusy(true);
    setTexte([]);
    setProps([]);
    setAcceptes(new Map());
    setResultats([]);
    setErreur(null);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    let resume = "";
    try {
      for await (const evt of chatAgent(projetEdit, consigne, {
        mode: "edit",
        session: sessionEdit,
        signal: controller.signal,
      })) {
        if (evt.event === "debut") {
          setSessionEdit(evt.data.session);
          setMoteurEdit(evt.data.moteur);
          useDiscussions.getState().fixer(
            sessionId,
            evt.data.session ?? sessionEdit ?? sessionId,
            evt.data.moteur,
          );
        } else if (evt.event === "texte") {
          resume += evt.data.delta;
          setTexte((t) => [...t, evt.data.delta]);
        } else if (evt.event === "proposition") {
          setProps((p) => [...p, evt.data]);
        } else if (evt.event === "erreur") {
          setErreur({
            code: evt.data.code,
            message: evt.data.message || (ETIQUETTES_ERREUR[evt.data.code] ?? evt.data.code),
          });
          break;
        } else if (evt.event === "fin") {
          setSessionEdit(evt.data.session);
          const moteurSession = useDiscussions
            .getState()
            .sessions.find((session) => session.id === sessionId)?.moteur;
          useDiscussions.getState().fixer(
            sessionId,
            evt.data.session ?? sessionEdit ?? sessionId,
            moteurSession ?? moteurEdit ?? "Roch",
          );
          useDiscussions.getState().ajouterMessage(
            sessionId,
            "assistant",
            resume.trim() || "Propositions générées.",
          );
        }
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        setErreur({
          code: "interne",
          message: e instanceof Error ? e.message : String(e),
        });
      }
    } finally {
      setBusy(false);
    }
  }

  async function annulerAutonome() {
    if (!projet) return;
    try {
      await arreterTache(projet);
    } catch (e) {
      setErreur({ code: "interne", message: e instanceof Error ? e.message : String(e) });
    } finally {
      abortRef.current?.abort();
      setTacheActive(false);
      setBusy(false);
    }
  }

  async function consommerFluxTache(
    executionId: string,
    sessionId: string,
    flux: AsyncGenerator<EvenementTache>,
    controller: AbortController,
  ) {
    setTacheActive(true);
    setBusy(true);
    let resume = "";
    try {
      for await (const evt of flux) {
        setTachesOpenCode((taches) =>
          taches.map((tache) =>
            tache.id === executionId
              ? { ...tache, events: [...tache.events, evt] }
              : tache,
          ),
        );
        if (evt.event === "debut") {
          setSessionEdit(evt.data.session);
          setMoteurEdit(evt.data.moteur);
          useDiscussions.getState().fixer(
            sessionId,
            evt.data.session ?? sessionId,
            evt.data.moteur,
          );
        } else if (evt.event === "texte") {
          resume += evt.data.delta;
          setTexte((t) => [...t, evt.data.delta]);
        } else if (evt.event === "erreur") {
          setErreur({
            code: evt.data.code,
            message: evt.data.message || (ETIQUETTES_ERREUR[evt.data.code] ?? evt.data.code),
          });
          break;
        } else if (evt.event === "fin") {
          setSessionEdit(evt.data.session);
          const moteurSession = useDiscussions
            .getState()
            .sessions.find((session) => session.id === sessionId)?.moteur;
          useDiscussions.getState().fixer(
            sessionId,
            evt.data.session ?? sessionId,
            moteurSession ?? moteurEdit ?? "Roch",
          );
          useDiscussions.getState().ajouterMessage(
            sessionId,
            "assistant",
            resume.trim() || "Tâche terminée.",
          );
        }
      }
      if (onglets.length) await rechargerFichiers(onglets.map((onglet) => onglet.chemin));
    } catch (error) {
      if (!controller.signal.aborted) {
        setErreur({
          code: "interne",
          message: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      setTacheActive(false);
      setBusy(false);
    }
  }

  // Mode OpenCode réel : l'agent peut lire le projet, enchaîner plusieurs
  // outils et écrire directement plusieurs fichiers dans le dossier courant.
  async function envoyerAutonome() {
    const consigne = message;
    if (!consigne.trim() || busy || !projet || lancementAutonomeRef.current) return;
    // Le verrou synchrone ferme la petite fenêtre avant le rerender de `busy`.
    // Deux clics/Entrées rapides ne doivent pas créer deux sessions distinctes.
    lancementAutonomeRef.current = true;
    try {
      const historique = useDiscussions.getState();
      const sessionId = activeEditId ?? historique.nouvelle("edit");
      tacheSessionRef.current = sessionId;
      repriseTacheRef.current = `${projet}:${sessionId}`;
      historique.ajouterMessage(sessionId, "user", consigne);
      setBusy(true);
      setTacheActive(true);
      setTexte([]);
      const executionId = "opencode-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
      tacheCouranteRef.current = executionId;
      setTachesOpenCode([{ id: executionId, consigne, events: [] }]);
      setResultats([]);
      setProps([]);
      setErreur(null);
      empreintesTacheRef.current = {};
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setMessage("");
      await consommerFluxTache(
        executionId,
        sessionId,
        tacheAgent(projet, consigne, {
          session: sessionId,
          signal: controller.signal,
        }),
        controller,
      );
    } finally {
      lancementAutonomeRef.current = false;
    }
  }

  function basculer(fichier: string, idHunk: number) {
    setAcceptes((m) => {
      const suiv = new Map(m);
      const s = new Set(suiv.get(fichier) ?? []);
      s.has(idHunk) ? s.delete(idHunk) : s.add(idHunk);
      suiv.set(fichier, s);
      return suiv;
    });
  }

  function basculerCarte(cle: string) {
    setCartesRepliees((c) => {
      const suiv = new Set(c);
      suiv.has(cle) ? suiv.delete(cle) : suiv.add(cle);
      return suiv;
    });
  }

  async function appliquer() {
    setResultats([]);
    setErreur(null);
    const modifs: Modification[] = props
      .filter((p) => (acceptes.get(p.fichier)?.size ?? 0) > 0)
      .map((p) => ({
        fichier: p.fichier,
        action: p.action,
        source_hash: p.source_hash,
        acceptes: p.hunks.filter((h) => acceptes.get(p.fichier)?.has(h.id)),
      }));
    let res: ResultatFichier[];
    try {
      res = await appliquerModifs(projetEdit, modifs);
      setResultats(res);
    } catch (e) {
      setErreur({
        code: "interne",
        message: e instanceof Error ? e.message : String(e),
      });
      return;
    }
  }

  const nbAcceptes = props.reduce(
    (s, p) => s + (acceptes.get(p.fichier)?.size ?? 0),
    0,
  );

  const montreBubbles = enChat && active && active.messages.length > 0;

  function envoyerActif() {
    if (busy) return;
    if (!message.trim() && !(enChat && pieces.length)) return;
    if (enChat) void envoyerChat();
    else if (executionEdit === "autonome") void envoyerAutonome();
    else void envoyer();
  }

  return (
    <div className="chat-panel">
      <div className="chat-header">
        <span className="chat-modes">
          <button
            className={"mode-btn" + (enChat ? " mode-actif" : "")}
            onClick={() => setModeAgent("chat")}
            disabled={busy}
            title="Discussion libre : l'assistant répond sans modifier de fichier"
          >
            chat
          </button>
          <button
            className={"mode-btn" + (!enChat ? " mode-actif" : "")}
            onClick={() => setModeAgent("edit")}
            disabled={busy}
            title="Édition : l'agent propose des modifications, tu valides avant écriture"
          >
            edit
          </button>
        </span>
        {!enChat && (
          <span className="edit-execution">
            <button
              className={"execution-btn" + (executionEdit === "apercu" ? " execution-actif" : "")}
              onClick={() => setExecutionEdit("apercu")}
              disabled={busy}
              title="L'agent propose des hunks à valider avant écriture"
            >
              aperçu
            </button>
            <button
              className={"execution-btn" + (executionEdit === "autonome" ? " execution-actif" : "")}
              onClick={() => setExecutionEdit("autonome")}
              disabled={busy || !projet}
              title="Roch lit, modifie et teste directement le projet"
            >
              autonome
            </button>
          </span>
        )}
        {enChat && moteurAff && (
          <span
            className="badge-moteur"
            title={enChat ? `Réponse générée par ${moteurAff}` : `Moteur actif : ${moteurAff}`}
          >
            · {moteurAff === "opencode" ? "Roch" : moteurAff}
          </span>
        )}
      </div>

      <div className="chat-flux" ref={fluxRef}>
        {enChat && (
          <div className="chat-accueil">
            <span className="chat-accueil-titre">
              {active ? active.titre : "Nouvelle discussion"}
            </span>
            {!active && (
              <span className="chat-hint">
                Pose une question — l'assistant répond sans jamais modifier tes
                fichiers. Ouvre le menu ＋ pour l'ancrer sur option(s) : projet
                ouvert, documents de cours (RAG) ou web.
              </span>
            )}
          </div>
        )}

        {montreBubbles && (
          <>
            <div className="fil-titre">
              <span className="fil-titre-texte">{active!.titre}</span>
              <span className="fil-titre-hint">session en cours</span>
            </div>
            {active!.messages.map((m, i) => (
              <BulleChat
                key={m.id}
                message={m}
                dernier={i === active!.messages.length - 1}
                busy={busy}
              />
            ))}
            {busy && (
              <div className="chat-statut">
                <span className="chat-statut-pulse" />
                rédaction de la réponse…
              </div>
            )}
          </>
        )}
        {enChat && erreurChat && (
          <div className="chat-erreur">
            <strong>{ETIQUETTES_ERREUR[erreurChat.code] ?? erreurChat.code}</strong> —{" "}
            {erreurChat.message}
          </div>
        )}

        {!enChat && (
          <div className="chat-mode-info">
            {executionEdit === "autonome"
              ? "Roch travaille directement dans le projet et peut enchaîner plusieurs lectures, modifications et commandes."
              : "L'agent propose des modifications de fichiers ci-dessous — valide bloc par bloc puis applique."}
          </div>
        )}
        {!enChat && messagesEditVisibles.map((message) => (
          <BulleChat
            key={message.id}
            message={message}
            dernier={false}
            busy={false}
          />
        ))}
        {!enChat && executionEdit === "apercu" &&
          texte.map((t, i) => (
            <div key={i} className="chat-texte">{t}</div>
          ))}
        {!enChat && executionEdit === "autonome" &&
          tachesOpenCode.map((tache) => (
            <div className="agent-run" key={tache.id}>
              <BulleChat
                message={{ id: tache.id + ":consigne", role: "user", texte: tache.consigne }}
                dernier={false}
                busy={false}
              />
              {blocsTache(tache.events).map((bloc) =>
                bloc.type === "texte" ? (
                  <BulleChat
                    key={bloc.cle}
                    message={{ id: bloc.cle, role: "assistant", texte: bloc.texte }}
                    dernier={false}
                    busy={false}
                  />
                ) : (
                  <AgentActivityTimeline
                    key={bloc.cle}
                    events={bloc.events}
                    onOpenFile={(chemin) => void ouvrirFichier(chemin)}
                    onPermission={async (requestId, reply) => {
                      if (!projet) return;
                      await repondrePermissionApi(projet, requestId, reply);
                    }}
                  />
                ),
              )}
            </div>
          ))}
        {busy && !enChat && executionEdit === "autonome" &&
          (() => {
            const derniereTache = tachesOpenCode[tachesOpenCode.length - 1];
            const activites = derniereTache?.events.filter(
              (evt): evt is Extract<EvenementTache, { event: "activite" }> =>
                evt.event === "activite" && evt.data.status === "running",
            );
            // La timeline affiche déjà l'activité en cours : ne pas la
            // répéter dans un deuxième bandeau identique.
            if (activites?.length) return null;
            return <div className="chat-busy">… Roch analyse le projet</div>;
          })()}
        {busy && !enChat && executionEdit !== "autonome" && (
          <div className="chat-busy">… l'agent propose — tu peux entre-temps éditer</div>
        )}
        {!enChat &&
          props.map((p) => {
            const cle = p.fichier + ":" + p.source_hash;
            const s = acceptes.get(p.fichier) ?? new Set<number>();
            const deployee = !cartesRepliees.has(cle);
            return (
              <div className="prop-carte" key={cle}>
                <div className="prop-entete">
                  <button
                    className="prop-basculer"
                    onClick={() => basculerCarte(cle)}
                    title={deployee ? "Replier le diff" : "Afficher le diff"}
                  >
                    <span className={"prop-chevron" + (deployee ? " ouvert" : "")}>▸</span>
                  </button>
                  <code className="prop-fichier">{p.fichier}</code>
                  <span className={"badge action-" + p.action}>{p.action}</span>
                  <span className="badge-stats">
                    +{p.stats.ajouts} −{p.stats.suppressions}
                  </span>
                </div>
                {deployee && (
                  <DiffView
                    hunks={p.hunks}
                    acceptes={s}
                    onBascule={(id) => basculer(p.fichier, id)}
                  />
                )}
              </div>
            );
          })}
        {!enChat && executionEdit === "apercu" && !busy && props.length === 0 && erreur === null && (
          <div className="chat-texte chat-hint">
            Écris une consigne : « ajoute un script de test », « corrige le bug du calcul »…
            — l'agent propose les modifications, tu valides avant qu'elles ne soient écrites.
          </div>
        )}
        {!enChat && erreur && (
          <div className="chat-erreur">
            <strong>{ETIQUETTES_ERREUR[erreur.code] ?? erreur.code}</strong> — {erreur.message}
          </div>
        )}
        {!enChat &&
          resultats.map((r, i) => (
            <div key={i} className={"resultat resultat-" + r.statut}>
              {r.statut === "ok" && <>✓ {r.fichier} écrit ({r.nouveau_sha?.slice(0, 8)}…)</>}
              {r.statut === "pas_modifie" && <>— {r.fichier} inchangé</>}
              {r.statut === "erreur" && <>✗ {r.fichier} : {r.message}</>}
            </div>
          ))}
      </div>

      {enChat ? (
        <div className="chat-bar-chat">
          <div className="composer">
            <div className="composer-plus-wrap">
              <button
                className="composer-plus"
                onClick={() => setMenuPlus((o) => !o)}
                disabled={busy}
                title="Options"
              >
                ＋
              </button>
              {menuPlus && (
                <div className="composer-menu" ref={menuPlusRef}>
                  <div className="composer-menu-close">
                    <span>Sources</span>
                    <button
                      className="composer-close-btn"
                      onClick={() => setMenuPlus(false)}
                      title="Fermer (Échap)"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="composer-menu-sep">Sources supplémentaires</div>
                  <label className="composer-option">
                    <input
                      type="checkbox"
                      checked={contexteProjet}
                      disabled={!projet}
                      onChange={(e) => setContexteProjet(e.target.checked)}
                    />
                    Projet ouvert ({projet ? "oui" : "aucun ouvert"})
                  </label>
                  <label className="composer-option">
                    <input
                      type="checkbox"
                      checked={contexteDocs}
                      disabled={!docs?.index.existe}
                      onChange={(e) => setContexteDocs(e.target.checked)}
                    />
                    Documents de cours (RAG)
                    {docs && docs.index.existe
                      ? ` · ${docs.index.nb_chunks} chunk${docs.index.nb_chunks > 1 ? "s" : ""}`
                      : " · non indexé"}
                  </label>
                  <label className="composer-option">
                    <input
                      type="checkbox"
                      checked={contexteWeb}
                      onChange={(e) => setContexteWeb(e.target.checked)}
                    />
                    Recherche web (DuckDuckGo)
                  </label>
                  <div className="composer-menu-sep">Pièce jointe au message</div>
                  <div className="composer-menu-actions">
                    <button
                      className="composer-mini"
                      onClick={() => {
                        setMenuPlus(false);
                        pieceInputRef.current?.click();
                      }}
                      disabled={busy || pieces.length >= PIECE_MAX}
                      title="Image ou PDF envoyé avec le prochain message (mode Chat)"
                    >
                      Image ou PDF…
                    </button>
                  </div>
                  <div className="composer-menu-sep">Cours ({docs?.documents.length ?? 0})</div>
                  <div className="composer-menu-actions">
                    <button
                      className="composer-mini"
                      onClick={() => fichierInputRef.current?.click()}
                      disabled={docsBusy}
                    >
                      Ajouter un cours…
                    </button>
                    <button
                      className="composer-mini"
                      onClick={() => void reindexerCours()}
                      disabled={docsBusy || (docs?.documents.length ?? 0) === 0}
                      title="Extrait + indexe tous les cours (embeddings Gemini)"
                    >
                      {docsBusy ? "Indexation…" : "Réindexer"}
                    </button>
                    <input
                      ref={fichierInputRef}
                      type="file"
                      accept=".pdf,.txt,.md"
                      hidden
                      onChange={(e) => void uploaderCours(e.target.files)}
                    />
                  </div>
                  {docsErreur && <div className="composer-docs-erreur">{docsErreur}</div>}
                </div>
              )}
            </div>
            <input
              className="composer-champ"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !busy) envoyerActif();
              }}
              placeholder={pieces.length ? "Question (facultative si pièce jointe)…" : "Pose une question…"}
              disabled={busy}
            />
            <button
              className="composer-envoi"
              onClick={envoyerActif}
              disabled={busy || (!message.trim() && !pieces.length)}
            >
              {busy ? "…" : "Envoyer"}
            </button>
          </div>
          {pieces.length > 0 && (
            <div className="composer-pieces">
              {pieces.map((piece) => (
                <span className="composer-piece" key={piece.cle}>
                  <span className="composer-piece-nom" title={`${piece.nom} · ${piece.ko} Ko`}>
                    {piece.nom}
                  </span>
                  <span className="composer-piece-taille">{piece.ko} Ko</span>
                  <button
                    className="composer-piece-retirer"
                    onClick={() => setPieces((p) => p.filter((x) => x.cle !== piece.cle))}
                    title="Retirer cette pièce jointe"
                    disabled={busy}
                  >
                    ✕
                  </button>
                </span>
              ))}
            </div>
          )}
          <input
            ref={pieceInputRef}
            type="file"
            multiple
            accept=".png,.jpg,.jpeg,.jfif,.gif,.webp,.pdf,image/png,image/jpeg,image/gif,image/webp,application/pdf"
            hidden
            onChange={(e) => void choisirPieces(e.target.files)}
          />
          <div className="chat-note">
            L'assistant répond sans jamais modifier tes fichiers
            {contexteDocs || contexteWeb ? " — sources (cours + web) citées dans la réponse" : ""}.
          </div>
        </div>
      ) : (
        <div className="chat-bar-chat">
          <div className="composer">
            {dossierLocal && (
              <div className="composer-plus-wrap">
                <button
                  className="composer-plus"
                  onClick={() => void choisirDossierViaFenetre()}
                  disabled={busy || choisitDossier}
                  title="Ouvrir une fenêtre Windows pour choisir le dossier à modifier (édition directe, aucune copie)"
                >
                  {choisitDossier ? "…" : "＋"}
                </button>
              </div>
            )}
            <input
              className="composer-champ"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !busy) envoyerActif();
              }}
              placeholder={
                projet
                  ? "Demander une modification…"
                  : "Choisis un dossier puis demande une modification…"
              }
              disabled={busy}
            />
            <button
              className="composer-envoi"
              onClick={envoyerActif}
              disabled={busy || !message.trim()}
            >
              {busy ? "…" : "Envoyer"}
            </button>
            {executionEdit === "autonome" && busy && (
              <button className="composer-envoi arreter" onClick={() => void annulerAutonome()}>
                Arrêter
              </button>
            )}
            {executionEdit === "apercu" && props.length > 0 && (
              <button
                className="composer-envoi appliquer"
                onClick={appliquer}
                disabled={busy || nbAcceptes === 0}
                title="Appliquer les blocs validés"
              >
                Appliquer ({nbAcceptes})
              </button>
            )}
          </div>
          <div className="chat-note">
            {executionEdit === "autonome"
              ? "Roch écrit dans le projet courant. Utilise un dépôt Git pour pouvoir annuler ses changements."
              : "L'agent propose des modifications — rien n'est écrit tant que tu n'as pas validé et appliqué."}
          </div>
          {erreurDossier && (
            <div className="chat-erreur">{erreurDossier}</div>
          )}
        </div>
      )}
    </div>
  );
}
