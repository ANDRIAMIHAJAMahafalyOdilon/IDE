import type {
  ContenuFichier,
  EtatFichiers,
  EvenementSSE,
  EvenementTache,
  ApercuDossier,
  ModeAgent,
  Modification,
  NoeudArbre,
  Projet,
  ResultatEcriture,
  ResultatFichier,
  ResultatRecherche,
  SimulationFichier,
} from "../types/api";

export interface OptionsChat {
  mode?: ModeAgent;
  session?: string | null;
  fichiersContexte?: string[];
  simulation?: SimulationFichier[];
  /** RAG : ancrer la réponse sur les documents de cours indexés (mode chat). */
  documents?: boolean;
  /** Recherche web DuckDuckGo pour ancrer la réponse (mode chat). */
  web?: boolean;
  signal?: AbortSignal;
}

function parseEvenement(bloc: string): EvenementSSE | null {
  const lignes = bloc.split(/\r?\n/);
  let event = "message";
  let data = "";
  for (const ligne of lignes) {
    if (ligne.startsWith("event:")) event = ligne.slice(6).trim();
    else if (ligne.startsWith("data:")) data += ligne.slice(5).trim();
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) } as unknown as EvenementSSE;
  } catch {
    return null;
  }
}

async function* parseFluxSSE(
  res: Response,
): AsyncGenerator<{ event: string; data: unknown }> {
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error("SSE " + res.status + ": " + (detail || "réponse indisponible"));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocs = buffer.split(/\r?\n\r?\n/);
    buffer = blocs.pop() ?? "";
    for (const bloc of blocs) {
      const evt = parseEvenement(bloc);
      if (evt) yield evt as { event: string; data: unknown };
    }
  }
  const dernier = parseEvenement(buffer);
  if (dernier) yield dernier as { event: string; data: unknown };
}

export async function* chatAgent(
  projet: string | null,
  message: string,
  opts: OptionsChat = {},
): AsyncGenerator<EvenementSSE> {
  const body: Record<string, unknown> = { message, mode: opts.mode ?? "edit" };
  if (projet) body.projet = projet;
  if (opts.session) body.session = opts.session;
  if (opts.fichiersContexte?.length) body.fichiers_contexte = opts.fichiersContexte;
  if (opts.simulation?.length) body.simulation = opts.simulation;
  if (opts.documents) body.documents = true;
  if (opts.web) body.web = true;

  const res = await fetch("/api/agent/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  for await (const evt of parseFluxSSE(res)) {
    yield evt as EvenementSSE;
  }
}

export async function* tacheAgent(
  projet: string,
  message: string,
  opts: { session?: string | null; signal?: AbortSignal } = {},
): AsyncGenerator<EvenementTache> {
  const body: Record<string, unknown> = { projet, message };
  if (opts.session) body.session = opts.session;
  const res = await fetch("/api/agent/tache", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: opts.signal,
  });
  for await (const evt of parseFluxSSE(res)) {
    yield evt as EvenementTache;
  }
}

export interface EtatTachePersistante {
  existe: boolean;
  active: boolean;
  terminee: boolean;
  message?: string;
}

export async function etatTache(
  projet: string,
  session: string,
): Promise<EtatTachePersistante> {
  const params = new URLSearchParams({ projet, session });
  const res = await fetch(`/api/agent/tache/status?${params.toString()}`);
  if (!res.ok) throw new Error("status tâche " + res.status);
  return (await res.json()) as EtatTachePersistante;
}

export async function arreterTache(projet: string): Promise<void> {
  const res = await fetch("/api/agent/tache/abort", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projet }),
  });
  if (!res.ok) throw new Error("abort " + res.status + ": " + (await res.text()));
}

export async function repondrePermission(
  projet: string,
  requestId: string,
  reply: "once" | "always" | "reject",
  message?: string,
): Promise<void> {
  const res = await fetch("/api/agent/tache/permission", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projet, request_id: requestId, reply, message }),
  });
  if (!res.ok) throw new Error("permission " + res.status + ": " + (await res.text()));
}

/** Empreintes SHA-1 des fichiers ouverts : le frontend poll pendant une tâche
 * et recharge les onglets modifiés par l'agent (jamais un onglet *dirty*). */
export async function etatFichiers(
  projet: string,
  chemins: string[],
): Promise<Record<string, string | null>> {
  if (!chemins.length) return {};
  const q = chemins.map((c) => `chemins=${encodeURIComponent(c)}`).join("&");
  const r = await _json<EtatFichiers>(
    `/api/projects/${encodeURIComponent(projet)}/etat?${q}`,
  );
  return r.fichiers;
}

export async function appliquerModifs(
  projet: string,
  modifs: Modification[],
): Promise<ResultatFichier[]> {
  const res = await fetch("/api/agent/apply-changes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projet, modifications: modifs }),
  });
  if (!res.ok) throw new Error(`apply-changes ${res.status}: ${await res.text()}`);
  const json = (await res.json()) as { resultats: ResultatFichier[] };
  return json.resultats;
}

async function _json<T>(chemin: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(chemin, opts);
  if (!res.ok) throw new Error(`${chemin} ${res.status}: ${await res.text()}`);
  return (await res.json()) as T;
}

export async function listerProjets(): Promise<Projet[]> {
  const r = await _json<{ projets: Projet[] }>("/api/projects");
  return r.projets;
}

export async function importerProjet(fichier: File, nom?: string): Promise<Projet> {
  const corps = new FormData();
  corps.append("fichier", fichier);
  if (nom) corps.append("nom", nom);
  return _json<Projet>("/api/projects/import", { method: "POST", body: corps });
}

/** Aperçu (sans ouverture) d'un dossier local : validation + échantillon filtré. */
export async function apercuProjetLocal(chemin: string): Promise<ApercuDossier> {
  const res = await fetch("/api/projects/preview-local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chemin }),
  });
  if (!res.ok) {
    const corps = await res.json().catch(() => null);
    throw new Error(corps?.detail ?? `preview-local ${res.status}`);
  }
  return (await res.json()) as ApercuDossier;
}

/** Ouvre un dossier local en mode DIRECT (aucune copie) : édition sur le vrai disque. */
/** Répertoire à afficher dans l'explorateur (dossier serveur listé). */
export interface DossierExplorateur {
  nom: string;
  chemin: string;
}

/** Réponse de `GET /api/projects/browse` : dossier courant + sous-dossiers. */
export interface ParcoursDossier {
  chemin: string;
  nom: string;
  parent: string | null;
  dossiers: DossierExplorateur[];
}

export async function listerDossiersServeur(chemin = ""): Promise<ParcoursDossier> {
  const q = chemin ? `?chemin=${encodeURIComponent(chemin)}` : "";
  const res = await fetch(`/api/projects/browse${q}`);
  if (!res.ok) {
    const corps = await res.json().catch(() => null);
    throw new Error(corps?.detail ?? `browse ${res.status}`);
  }
  return (await res.json()) as ParcoursDossier;
}

export async function ouvrirProjetLocal(chemin: string): Promise<Projet> {
  const res = await fetch("/api/projects/open-local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chemin }),
  });
  if (!res.ok) {
    const corps = await res.json().catch(() => null);
    throw new Error(corps?.detail ?? `open-local ${res.status}`);
  }
  return (await res.json()) as Projet;
}

/** Ouvre la VRAIE fenêtre Windows « Sélectionner un dossier » (tkinter) et
 *  renvoie le chemin choisi, ou "" si annulé. Le backend valide le chemin
 *  avant de répondre (même garde-fou qu'open-local). */
export async function choisirDossierNatif(): Promise<string> {
  const res = await fetch("/api/system/choisir-dossier", { method: "POST" });
  if (!res.ok) {
    const corps = await res.json().catch(() => null);
    throw new Error(corps?.detail ?? `choisir-dossier ${res.status}`);
  }
  const json = (await res.json()) as { chemin: string };
  return json.chemin ?? "";
}

export function arbreProjet(projet: string): Promise<NoeudArbre[]> {
  return _json<{ racine: NoeudArbre[] }>(
    `/api/projects/${encodeURIComponent(projet)}/tree`,
  ).then((r) => r.racine);
}

export async function lireFichier(projet: string, chemin: string): Promise<ContenuFichier> {
  return _json<ContenuFichier>(
    `/api/projects/${encodeURIComponent(projet)}/file?chemin=${encodeURIComponent(chemin)}`,
  );
}

export async function ecrireFichier(
  projet: string,
  chemin: string,
  contenu: string,
): Promise<ResultatEcriture> {
  return _json<ResultatEcriture>(
    `/api/projects/${encodeURIComponent(projet)}/file`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chemin, contenu }),
    },
  );
}

export function rechercher(projet: string, q: string): Promise<ResultatRecherche> {
  return _json<ResultatRecherche>(
    `/api/projects/${encodeURIComponent(projet)}/search?q=${encodeURIComponent(q)}`,
  );
}

// ── Documents de cours (RAG) ──────────

export interface DocumentInfo {
  nom: string;
  octets: number;
  extension: string;
}

export interface EtatIndexRag {
  existe: boolean;
  nb_chunks: number;
  documents: string[];
}

export interface ReponseDocuments {
  documents: DocumentInfo[];
  index: EtatIndexRag;
}

export function listerDocuments(): Promise<ReponseDocuments> {
  return _json<ReponseDocuments>("/api/documents");
}

export async function uploaderDocument(fichier: File): Promise<DocumentInfo> {
  const corps = new FormData();
  corps.append("fichier", fichier);
  const res = await fetch("/api/documents/upload", { method: "POST", body: corps });
  if (!res.ok) {
    const corpsJson = await res.json().catch(() => null);
    throw new Error(corpsJson?.detail ?? `upload ${res.status}`);
  }
  return (await res.json()) as DocumentInfo;
}

export async function reindexerIndex(): Promise<{ nb_documents: number; nb_chunks: number; documents: string[] }> {
  const res = await fetch("/api/documents/reindex", { method: "POST" });
  if (!res.ok) {
    const corps = await res.json().catch(() => null);
    throw new Error(corps?.detail ?? `reindex ${res.status}`);
  }
  return await res.json();
}
