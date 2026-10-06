import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ModeAgent } from "../types/api";

export interface MessageDisc {
  /** Identifiant stable : clé React et cible de réconciliation. */
  id: string;
  role: "user" | "assistant";
  texte: string;
  /** Fichiers du message ; les octets restent persistés côté backend. */
  attachments?: PieceMessage[];
}

export interface PieceMessage {
  id: string;
  message_id?: string;
  filename: string;
  mime_type: string;
  size: number;
  status?: "selected" | "uploading" | "processing" | "ready" | "error";
  pages?: number | null;
  previewUrl?: string;
}

export interface SessionDisc {
  id: string;
  /** Une session Chat ne doit jamais apparaître dans l'historique Edit, et inversement. */
  mode: ModeAgent;
  /** Session backend (/api/agent/chat) une fois `debut` reçu ; null avant. */
  backend: string | null;
  titre: string;
  messages: MessageDisc[];
  moteur: string | null;
}

const TITRE_PAR_DEFAUT = "nouvelle discussion";

function genererId() {
  return (
    "disc-" +
    Math.random().toString(36).slice(2, 9) +
    Date.now().toString(36)
  );
}

/** Identifiant stable d'un message. `crypto.randomUUID` exige un contexte
 *  sécurisé (localhost ou https) : on retombe sinon sur un suffixe aléatoire. */
export function genererIdMessage(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return (
    "msg-" +
    Math.random().toString(36).slice(2, 10) +
    Date.now().toString(36)
  );
}

function titrer(texte: string): string {
  const plat = texte.replace(/\s+/g, " ").trim();
  return plat.length > 48 ? plat.slice(0, 48) + "…" : plat;
}

interface EtatDiscussions {
  sessions: SessionDisc[];
  activeIds: Record<ModeAgent, string | null>;
  ouvrir: (id: string, mode?: ModeAgent) => void;
  nouvelle: (mode?: ModeAgent) => string;
  renommer: (id: string, titre: string) => void;
  supprimer: (id: string) => void;
  ajouterMessage: (
    id: string,
    role: "user" | "assistant",
    texte: string,
    attachments?: PieceMessage[],
    messageId?: string,
  ) => void;
  associerPiecesMessages: (id: string, pieces: PieceMessage[]) => void;
  fusionnerDelta: (id: string, delta: string) => void;
  /** Vide la réponse en cours d'un assistant : utilisé sur `reprise`, quand le
   *  flux a été coupé et que le moteur de secours relance la réponse. */
  viderReponseEnCours: (id: string) => void;
  fixer: (id: string, backend: string, moteur: string | null) => void;
  retirerAssistantVide: (id: string) => void;
}

export const useDiscussions = create<EtatDiscussions>()(
  persist(
    (set) => ({
      sessions: [],
      activeIds: { chat: null, edit: null },

      ouvrir: (id, mode) =>
        set((s) => {
          const session = s.sessions.find((x) => x.id === id);
          const cible = mode ?? session?.mode ?? "chat";
          return { activeIds: { ...s.activeIds, [cible]: id } };
        }),

      nouvelle: (mode = "chat") => {
        const session: SessionDisc = {
          id: genererId(),
          mode,
          backend: null,
          titre: TITRE_PAR_DEFAUT,
          messages: [],
          moteur: null,
        };
        set((s) => ({
          sessions: [session, ...s.sessions],
          activeIds: { ...s.activeIds, [mode]: session.id },
        }));
        return session.id;
      },

      renommer: (id, titre) =>
        set((s) => ({
          sessions: s.sessions.map((x) =>
            x.id === id ? { ...x, titre: titre.trim() || TITRE_PAR_DEFAUT } : x,
          ),
        })),

      supprimer: (id) =>
        set((s) => {
          const supprimee = s.sessions.find((x) => x.id === id);
          const sessions = s.sessions.filter((x) => x.id !== id);
          if (!supprimee) return { sessions };
          const activeIds = { ...s.activeIds };
          if (activeIds[supprimee.mode] === id) {
            activeIds[supprimee.mode] =
              sessions.find((x) => x.mode === supprimee.mode)?.id ?? null;
          }
          return { sessions, activeIds };
        }),

      ajouterMessage: (id, role, texte, attachments, messageId) =>
        set((s) => ({
          sessions: s.sessions.map((x) => {
            if (x.id !== id) return x;
            const idMessage = messageId ?? genererIdMessage();
            const titre =
              x.titre === TITRE_PAR_DEFAUT && role === "user" && texte.trim()
                ? titrer(texte)
                : x.titre;
            return {
              ...x,
              titre,
              messages: [
                ...x.messages,
                {
                  id: idMessage,
                  role,
                  texte,
                  ...(attachments?.length
                    ? {
                        attachments: attachments.map((piece) => ({
                          ...piece,
                          message_id: idMessage,
                          status: piece.status ?? "ready",
                        })),
                      }
                    : {}),
                },
              ],
            };
          }),
        })),

      associerPiecesMessages: (id, pieces) =>
        set((s) => {
          const parMessage = new Map<string, PieceMessage[]>();
          for (const piece of pieces) {
            if (!piece.message_id) continue;
            const associees = parMessage.get(piece.message_id) ?? [];
            associees.push(piece);
            parMessage.set(piece.message_id, associees);
          }
          if (!parMessage.size) return s;
          return {
            sessions: s.sessions.map((session) => {
              if (session.id !== id) return session;
              return {
                ...session,
                messages: session.messages.map((message) => {
                  const associees = parMessage.get(message.id);
                  return associees?.length
                    ? { ...message, attachments: associees }
                    : message;
                }),
              };
            }),
          };
        }),

      fusionnerDelta: (id, delta) =>
        set((s) => ({
          sessions: s.sessions.map((x) => {
            if (x.id !== id) return x;
            const messages = [...x.messages];
            const dernier = messages[messages.length - 1];
            if (dernier?.role === "assistant") {
              messages[messages.length - 1] = {
                ...dernier,
                texte: dernier.texte + delta,
              };
            }
            return { ...x, messages };
          }),
        })),

      viderReponseEnCours: (id) =>
        set((s) => ({
          sessions: s.sessions.map((x) => {
            if (x.id !== id) return x;
            const messages = [...x.messages];
            const dernier = messages[messages.length - 1];
            // Seul le dernier message assistant encore en cours est vidé :
            // l'historique des réponses terminées n'est jamais touché.
            if (dernier?.role === "assistant") {
              messages[messages.length - 1] = { ...dernier, texte: "" };
            }
            return { ...x, messages };
          }),
        })),

      fixer: (id, backend, moteur) =>
        set((s) => ({
          sessions: s.sessions.map((x) =>
            x.id === id ? { ...x, backend, moteur } : x,
          ),
        })),

      retirerAssistantVide: (id) =>
        set((s) => ({
          sessions: s.sessions.map((x) => {
            if (x.id !== id) return x;
            const messages = [...x.messages];
            const dernier = messages[messages.length - 1];
            if (dernier?.role === "assistant" && !dernier.texte) messages.pop();
            return { ...x, messages };
          }),
        })),
    }),
    {
      name: "ai-studio-discussions",
      version: 4,
      migrate: (etatInconnu) => {
        // Les versions précédentes n'avaient qu'un historique global. On le
        // conserve dans Chat afin qu'une mise à jour ne fasse rien disparaître.
        const ancien = etatInconnu as Partial<EtatDiscussions> & {
          activeId?: string | null;
        };
        const sessions = (ancien.sessions ?? []).map((session) => ({
          ...session,
          mode: session.mode === "edit" ? "edit" : "chat",
          // v3 : chaque message reçoit un identifiant stable (clé React).
          // Les fils sauvegardés avant la v3 n'en ont pas : on les complète.
          messages: (session.messages ?? []).map((message) => ({
            ...message,
            id: message.id ?? genererIdMessage(),
            // Les anciennes versions appelaient ce champ `pieces`.
            attachments: message.attachments ?? (message as MessageDisc & { pieces?: PieceMessage[] }).pieces,
          })),
        }));
        const anciensActifs = ancien.activeIds;
        return {
          ...ancien,
          sessions,
          activeIds: {
            chat: anciensActifs?.chat ?? ancien.activeId ?? null,
            edit: anciensActifs?.edit ?? null,
          },
        } as EtatDiscussions;
      },
    },
  ),
);
