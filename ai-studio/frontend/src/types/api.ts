export interface LigneDiff {
  type: "context" | "add" | "del";
  contenu: string;
  old_no: number | null;
  new_no: number | null;
}

export interface Hunk {
  id: number;
  old_start: number;
  old_end: number;
  old_count: number;
  new_start: number;
  new_end: number;
  new_count: number;
  lignes: LigneDiff[];
}

export interface StatsDiff {
  ajouts: number;
  suppressions: number;
  nb_hunks: number;
  ancien_lignes: number;
  nouveau_lignes: number;
}

export interface Proposition {
  fichier: string;
  action: string;
  source_hash: string;
  hunks: Hunk[];
  stats: StatsDiff;
}

export interface ResultatFichier {
  fichier: string;
  statut: string;
  message: string | null;
  nouveau_sha: string | null;
}

export interface SimulationFichier {
  chemin: string;
  contenu: string;
}

export type EvenementSSE =
  | { event: "debut"; data: { session: string | null; autoris: boolean; moteur: string } }
  | { event: "texte"; data: { delta: string } }
  | { event: "proposition"; data: Proposition }
  | { event: "erreur"; data: ErreurSSE }
  | { event: "fin"; data: { session: string | null; nb_fichiers: number } };

/** Mode du panneau agent : "chat" = discussion (Gemini/Groq, sans diff) ·
 *  "edit" = édition (OpenCode + repli, propositions en hunks). */
export type ModeAgent = "chat" | "edit";

export interface ErreurSSE {
  code: string;
  message: string;
  fichier: string | null;
}

// ── Mode autonome (opencode run) : événements SSE /api/agent/tache ──

export interface DebutTache {
  session: string | null;
  moteur: string;
  mode: string;
}

export type AgentActivityStatus = "pending" | "running" | "success" | "error" | "cancelled";
export type AgentActivityType =
  | "thinking"
  | "file_read"
  | "file_search"
  | "file_write"
  | "file_create"
  | "file_delete"
  | "terminal"
  | "tool"
  | "diff";

export interface AgentActivity {
  id: string;
  type: AgentActivityType;
  status: AgentActivityStatus;
  title: string;
  description?: string;
  fichier?: string;
  commande?: string;
  environnement?: string;
  raison?: string;
  output?: string;
  error?: string;
  diff?: unknown;
  fichiers?: string[];
}

export interface AgentPermission {
  id: string;
  status: "waiting_for_permission" | "success" | "rejected";
  action: string;
  resources?: unknown;
  session?: string;
  reply?: string;
}

export type EvenementTache =
  | { event: "debut"; data: DebutTache }
  | { event: "texte"; data: { delta: string } }
  | { event: "activite"; data: AgentActivity }
  | { event: "permission"; data: AgentPermission }
  | { event: "etat"; data: { status: string } }
  | { event: "outil"; data: AgentActivity }
  | { event: "erreur"; data: ErreurSSE }
  | { event: "fin"; data: { session: string | null } };

export interface EtatFichiers {
  projet: string;
  fichiers: Record<string, string | null>;
}

export interface Modification {
  fichier: string;
  action: string;
  source_hash: string;
  acceptes: Hunk[];
}

// ── Endpoints projets (docs/cahier-des-charges-api.md) ──

export interface Projet {
  id: string;
  nom: string;
  nb_fichiers: number;
  /** "archive" = importé (copie sous data/projets) · "dossier" = ouvert en direct. */
  origine?: "archive" | "dossier";
  /** Chemin absolu sur disque (renseigné pour les deux origines). */
  chemin?: string;
}

/** Aperçu d'un dossier local (POST /api/projects/preview-local), sans ouverture. */
export interface ApercuDossier {
  nom: string;
  chemin: string;
  nb_fichiers: number;
  exemples: string[];
}

export interface NoeudArbre {
  nom: string;
  chemin: string;
  type: "dossier" | "fichier";
  taille?: number;
  enfants?: NoeudArbre[];
}

export interface ContenuFichier {
  chemin: string;
  langue: string;
  taille: number;
  modifie: number;
  contenu: string;
}

export interface ResultatEcriture {
  chemin: string;
  nouveau_sha: string;
}

export interface Correspondance {
  ligne: number;
  num: number;
  extrait: string;
}

export interface FichierRecherche {
  fichier: string;
  correspondances: Correspondance[];
}

export interface ResultatRecherche {
  q: string;
  nb_fichiers: number;
  fichiers: FichierRecherche[];
}
