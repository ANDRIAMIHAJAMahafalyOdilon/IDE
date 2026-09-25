import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface MessageDisc {
  role: "user" | "assistant";
  texte: string;
}

export interface SessionDisc {
  id: string;
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

function titrer(texte: string): string {
  const plat = texte.replace(/\s+/g, " ").trim();
  return plat.length > 48 ? plat.slice(0, 48) + "…" : plat;
}

interface EtatDiscussions {
  sessions: SessionDisc[];
  activeId: string | null;
  ouvrir: (id: string) => void;
  nouvelle: () => string;
  renommer: (id: string, titre: string) => void;
  supprimer: (id: string) => void;
  ajouterMessage: (id: string, role: "user" | "assistant", texte: string) => void;
  fusionnerDelta: (id: string, delta: string) => void;
  fixer: (id: string, backend: string, moteur: string) => void;
  retirerAssistantVide: (id: string) => void;
}

export const useDiscussions = create<EtatDiscussions>()(
  persist(
    (set) => ({
      sessions: [],
      activeId: null,

      ouvrir: (id) => set({ activeId: id }),

      nouvelle: () => {
        const session: SessionDisc = {
          id: genererId(),
          backend: null,
          titre: TITRE_PAR_DEFAUT,
          messages: [],
          moteur: null,
        };
        set((s) => ({ sessions: [session, ...s.sessions], activeId: session.id }));
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
          const sessions = s.sessions.filter((x) => x.id !== id);
          const activeId =
            s.activeId === id ? (sessions[0]?.id ?? null) : s.activeId;
          return { sessions, activeId };
        }),

      ajouterMessage: (id, role, texte) =>
        set((s) => ({
          sessions: s.sessions.map((x) => {
            if (x.id !== id) return x;
            const titre =
              x.titre === TITRE_PAR_DEFAUT && role === "user" && texte.trim()
                ? titrer(texte)
                : x.titre;
            return { ...x, titre, messages: [...x.messages, { role, texte }] };
          }),
        })),

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
    { name: "ai-studio-discussions" },
  ),
);