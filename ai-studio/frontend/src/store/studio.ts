import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  arbreProjet,
  ecrireFichier,
  lireFichier,
  listerProjets,
  ouvrirProjetLocal,
} from "../api/client";
import type { ModeAgent, NoeudArbre, Projet } from "../types/api";

export interface Onglet {
  chemin: string;
  langue: string;
}

interface EtatStudio {
  projets: Projet[];
  projet: string | null;
  arbre: NoeudArbre[];
  onglets: Onglet[];
  ongletActif: string | null;
  contenus: Record<string, string>;
  projection: Record<string, string>;
  dirty: Record<string, boolean>;
  erreur: string | null;
  /** Mode courant du panneau agent : drive aussi la liste historique séparée. */
  modeAgent: ModeAgent;
  setModeAgent: (mode: ModeAgent) => void;
  chargerProjets: () => Promise<void>;
  changerProjet: (projet: string) => Promise<void>;
  ouvrirDossierLocal: (chemin: string) => Promise<Projet>;
  ouvrirFichier: (chemin: string, langue?: string) => Promise<void>;
  activerOnglet: (chemin: string) => void;
  fermerOnglet: (chemin: string) => void;
  majContenu: (chemin: string, texte: string) => void;
  sauverFichier: (chemin: string) => Promise<void>;
  rechargerFichiers: (chemins: string[]) => Promise<void>;
}

export const useStudio = create<EtatStudio>()(
  persist(
    (set, get) => ({
      projets: [],
      projet: null,
      arbre: [],
      onglets: [],
      ongletActif: null,
      contenus: {},
      projection: {},
      dirty: {},
      erreur: null,
      modeAgent: "chat",
      setModeAgent: (modeAgent) => set({ modeAgent }),

      chargerProjets: async () => {
        try {
          const projets = await listerProjets();
          set({ projets });
          if (!get().projet && projets.length) await get().changerProjet(projets[0].id);
        } catch (e) {
          set({ erreur: e instanceof Error ? e.message : String(e) });
        }
      },

      changerProjet: async (projet) => {
        set({ projet, erreur: null });
        try {
          const arbre = await arbreProjet(projet);
          set({ arbre, onglets: [], ongletActif: null, contenus: {}, projection: {}, dirty: {} });
        } catch (e) {
          set({ erreur: e instanceof Error ? e.message : String(e) });
        }
      },

      ouvrirDossierLocal: async (chemin) => {
        // Ouvre un dossier réel en mode direct (référence le disque, sans copie).
        const projet = await ouvrirProjetLocal(chemin);
        set({ projets: await listerProjets(), erreur: null });
        await get().changerProjet(projet.id);
        return projet;
      },

      ouvrirFichier: async (chemin, langue) => {
        const { projet } = get();
        if (!projet) return;
        if (get().onglets.some((o) => o.chemin === chemin)) {
          set({ ongletActif: chemin });
          return;
        }
        try {
          const contenu = await lireFichier(projet, chemin);
          set((s) => ({
            onglets: [...s.onglets, { chemin, langue: langue ?? contenu.langue }],
            ongletActif: chemin,
            contenus: { ...s.contenus, [chemin]: contenu.contenu },
            projection: { ...s.projection, [chemin]: contenu.contenu },
            dirty: { ...s.dirty, [chemin]: false },
          }));
        } catch (e) {
          set({ erreur: e instanceof Error ? e.message : String(e) });
        }
      },

      activerOnglet: (chemin) => set({ ongletActif: chemin }),

      fermerOnglet: (chemin) =>
        set((s) => {
          const onglets = s.onglets.filter((o) => o.chemin !== chemin);
          const contenus = { ...s.contenus };
          const projection = { ...s.projection };
          const dirty = { ...s.dirty };
          delete contenus[chemin];
          delete projection[chemin];
          delete dirty[chemin];
          return {
            onglets,
            contenus,
            projection,
            dirty,
            // Tab suivant = le voisin de droite, sinon dernier restant.
            ongletActif:
              s.ongletActif !== chemin
                ? s.ongletActif
                : (onglets[onglets.findIndex((o) => o.chemin === chemin)]?.chemin ??
                  onglets[onglets.length - 1]?.chemin ??
                  null),
          };
        }),

      majContenu: (chemin, texte) =>
        set((s) => ({
          contenus: { ...s.contenus, [chemin]: texte },
          dirty: { ...s.dirty, [chemin]: texte !== (s.projection[chemin] ?? "") },
        })),

      sauverFichier: async (chemin) => {
        const { projet, contenus } = get();
        if (!projet) return;
        try {
          const ecrit = await ecrireFichier(projet, chemin, contenus[chemin] ?? "");
          set((s) => ({
            projection: { ...s.projection, [chemin]: contenus[chemin] ?? "" },
            dirty: { ...s.dirty, [chemin]: false },
          }));
          void ecrit; // nouveau_sha utile plus tard (rafraîchir les propositions)
        } catch (e) {
          set({ erreur: e instanceof Error ? e.message : String(e) });
        }
      },

      rechargerFichiers: async (chemins) => {
        const { projet } = get();
        if (!projet) return;
        for (const chemin of chemins) {
          if (!get().onglets.some((o) => o.chemin === chemin)) continue;
          // Ne jamais écraser une saisie locale non sauvegardée pendant le polling.
          if (get().dirty[chemin]) continue;
          try {
            const contenu = await lireFichier(projet, chemin);
            set((s) => ({
              contenus: { ...s.contenus, [chemin]: contenu.contenu },
              projection: { ...s.projection, [chemin]: contenu.contenu },
              dirty: { ...s.dirty, [chemin]: false },
            }));
          } catch {
            /* laisse l'onglet tel quel si le fichier a disparu */
          }
        }
      },
    }),
    {
      name: "ai-studio-onglets",
      partialize: (s) => ({
        projet: s.projet,
        onglets: s.onglets,
        ongletActif: s.ongletActif,
        modeAgent: s.modeAgent,
      }),
    },
  ),
);
