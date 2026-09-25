# AI Studio — Cahier des charges : endpoints projets & sélection du contexte

## 1. Règle de sélection du contexte du chat (v1)

**Décision (v1) : le contexte de l'agent = les fichiers ouverts dans les onglets
de l'éditeur** (comportement Cursor par défaut). Simple, prévisible, sans
recherche ni index.

- **Où vit l'état des onglets ?** Côté frontend (store React + persistance
  `localStorage` pour survivre à un rafraîchissement). Le backend ne stocke
  **aucun** état d'onglets : il reste sans état vis-à-vis de l'UI.
- **Comment les onglets communiquent avec le chat ?** Le frontend transmet les
  chemins des onglets ouverts dans `fichiers_contexte` de
  `POST /api/agent/chat`. Le **contenu est lu côté backend** (fraîcheur +
  sécurité de chemin garanties) — jamais envoyé par le frontend.
- **Ordre de priorité :** l'ordre des onglets (onglet actif en premier).
- **Plafonds (déjà implémentés) :** 8 fichiers max, 24 000 caractères/fichier,
  budget total ~40 000 tokens, mémoire de session 8 échanges.
- **Péremption cohérente :** sauvegarder un fichier depuis l'éditeur
  (`PUT /api/files`) change son contenu sur disque ; toute proposition
  `source_hash` concernant ce fichier devient obsolète et
  `POST /api/agent/apply-changes` renverra un statut `erreur` par fichier
  (« périmé »). C'est le comportement voulu — le diff demandé reflète l'ancien
  état, l'utilisateur re-demande.

**Évolutions futurs (hors v1) :**
- v2 : sélection manuelle (cases à cocher dans l'explorateur) en complément des
  onglets.
- v3 : injection automatique des snippets de `<code>GET /api/search</code>` quand
  la requête ressemble à une recherche.

## 2. Endpoints projets

Préfixe : `/api/projects` (backup). Tous les chemins de fichiers sont relatifs,
validés avec une protection « path traversal » (`CheminHorsProjet` → 400/422).

| Méthode & route | Corps | Retour | Notes |
|---|---|---|---|
| `GET /api/projects` | — | `[{id, nom, nb_fichiers, origine, chemin?}]` | liste triée, fusion des projets `data/projets/` (`origine:"archive"`) et des dossiers réels ouverts (`origine:"dossier"`, `chemin` absolu) |
| `POST /api/projects` | JSON `{nom}` | `{id, nom, nb_fichiers: 0}` | crée un projet **vide** (pour démarrer neuf), écrase si existant |
| `POST /api/projects/import` | multipart `fichier` + `nom?` | `{id, nom, nb_fichiers}` | importe une archive `.zip`/`.tar.gz`/`.tar` ; ignore les dossiers lourds (`.git`, `node_modules`, `build`, `dist`, `.venv`, …) et les fichiers > 25 Mo ; remplace le projet du même nom |
| `POST /api/projects/preview-local` | JSON `{chemin}` | `{id, nom, chemin, nb_fichiers, exemples[]}` | **valide** un dossier réel et renvoie un échantillon filtré (≤ 20 chemins) **sans rien enregistrer ni écrire** ; `422` si chemin refusé |
| `POST /api/projects/open-local` | JSON `{chemin}` | `{id, nom, chemin}` | enregistre le dossier réel comme projet (`origine:"dossier"`) — **aucune copie** : les routes tree/file/search agissent directement sur le disque ; `422` si refusé |
| `GET /api/projects/{id}/tree` | — | `[{nom, chemin, type, taille?, enfants?}]` | arborescence **imbriquée**, dossiers d'abord puis fichiers (tri alpha) |
| `GET /api/projects/{id}/file?chemin=` | — | `{chemin, langue, taille, modifie, contenu}` | 404 si absent ; 422 `{code:"binaire"}` si binaire/hors-limite (2 Mo) |
| `PUT /api/projects/{id}/file` | JSON `{chemin, contenu}` | `{chemin, nouveau_sha}` | écrit (crée les dossiers parents) — **sauvegarde éditeur**, hors du flux `source_hash` |
| `GET /api/projects/{id}/search?q=` | — | `{q, nb_fichiers, fichiers:[{fichier, correspondances:[{ligne, num, extrait}]}]}` | grep simple : sous-chaîne insensible à la casse, contexte ±1 ligne ; plafonds 50 fichiers / 200 correspondances |

Contraintes transverses :
- `id` de projet = nom (slug) : `G`, `..`, `/`, `\` refusés (`400`).
- Projet inexistant sur les routes `GET` → `404`.
- Les fichiers binaires ne sont ni servis en contenu ni fouillés par la
  recherche (détection par extension ou sniff d'octets nuls).

## 3. Interaction avec l'agent (récapil du flux)

1. L'explorateur charge `GET /tree`, l'utilisateur ouvre un fichier
   (`GET /file`) → onglet (store frontend). Le chat reçoit les onglets dans
   `fichiers_contexte`.
2. `POST /api/agent/chat` : le backend construit le contexte
   (arborescence compacte + fichiers_contexte du disque + mémoire), appelle le
   moteur (OpenCode proposeur-seul → Gemini → Groq), adapte la sortie en hunks
   et streame les événements SSE.
3. `POST /api/agent/apply-changes` : applique uniquement les hunks validés,
   avec garde-fou `source_hash`.

### 3.1 Deux modes pour `POST /api/agent/chat` : `chat` et `edit`

Un seul endpoint, un champ `mode` (`"chat"` | `"edit"`, défaut `"edit"`) :

| | `mode: "edit"` | `mode: "chat"` |
|---|---|---|
| But | modifier le projet (propositions en hunks) | discuter (aucune écriture) |
| Moteurs | OpenCode proposeur → Gemini → Groq | Gemini → Groq (streamé token par token) |
| `projet` | **requis** (sinon `422`) | **optionnel** (discussion générale possible) |
| Contexte projet | arborescence + `fichiers_contexte` | arborescence + onglets ouverts, **lecture seule**, seulement si un projet est disponible |
| Événements | `debut → (texte \| proposition)* → (erreur \| fin)` | `debut → texte* → (erreur \| fin)` — jamais `proposition` |
| Codes d'erreur | + `parse_format`, `schema_invalide`, `hors_projet` | `auth`, `quota`, `timeout`, `moteur_indisponible`, `interne` |

- **Sessions isolées par mode** : la mémoire est indexée `(mode, session)`.
  Le fil `chat` ne pollue jamais le prompt du fil `edit` (et inversement) ;
  changer d'onglet dans l'UI réaffiche le fil correspondant
  (`sessionChat` / `sessionEdit`).
- Le mode `chat` n'importe ni `diff` ni `agent_adapter` : il ne peut produire
  ni proposition ni erreur de schéma de fichier.
- `debut.moteur` renseigne le moteur réellement utilisé (`gemini` / `groq` en
  chat ; `opencode` / `llm` / `simulation` en edit) — c'est ce que le badge UI
  affiche (`Chat · gemini`, `Edit · opencode`…).
- Dossier ouvert en **mode direct** : `mode: "edit"` impose l'aperçu
  (accept/reject) ; `mode: "chat"` est disponible sans restriction (lecture seule).

Exemples :

```jsonc
// Chat (sans projet) : debut/texte*/fin
POST /api/agent/chat
{ "mode": "chat", "message": "Explique ce qu'est un hunk." }

// Chat ancré sur les documents de cours (RAG) + recherche web :
POST /api/agent/chat
{ "mode": "chat", "message": "Qu'est-ce que la photosynthèse ?",
  "documents": true, "web": true }

// Edit (projet requis) : debut/(texte|proposition)*/fin
POST /api/agent/chat
{ "mode": "edit", "projet": "tech", "message": "ajoute un test",
  "fichiers_contexte": ["src/main.py"], "session": "sess-abc" }
```

### 3.2 Documents de cours & RAG (fusion du projet « AI »)

Le mode `chat` peut ancrer la réponse sur les cours indexés (`documents: true`,
RAG FAISS + embeddings Gemini) et/ou sur une recherche web DuckDuckGo
(`web: true`). Les passages/résultats sont injectés dans le prompt et le moteur
doit citer ses sources (« Source : <fichier>, page X » / URL exacte) — aucune
écriture, cohérent avec le mode `chat`.

Endpoints (préfixe `/api/documents`) :

| Méthode & route | Corps | Retour | Notes |
|---|---|---|---|
| `GET /api/documents` | — | `{documents:[{nom, octets, extension}], index:{existe, nb_chunks, documents[]}}` | liste des cours du dossier `data/documents/` + état de l'index |
| `POST /api/documents/upload` | multipart `fichier` | `{nom, octets}` | accepte `.pdf` / `.txt` / `.md` uniquement (400 sinon) |
| `POST /api/documents/reindex` | — | `{nb_documents, documents[], nb_chunks, echecs[]}` | **embeddings Gemini** de tous les cours → index FAISS (`data/index/`), lourd : endpoint synchrone (threadpool) |
| `DELETE /api/documents/{nom}` | — | `{supprime}` | retire le fichier (l'index n'est pas touché) |

- Pipeline : `extraire_texte` (pypdf) → chunks ~200 mots / chevauchement 30
  (champ `source` = nom du fichier + `page`) → `gemini-embedding-001` → FAISS
  (cosinus). Recherche : `k=3` (`RAG_K`).
- Index hérité d'une app antérieure : chunks sans `source` → « (documents
  archivés) » ; cliquer « Réindexer » dans l'UI reclasse proprement.
- Tout échec (réseau, index absent, ddgs injoignable) est **silencieux** : le
  Chat continue sans l'enrichissement.

## 4. Mode autonome + terminal intégré (agent qui exécute réellement)

Le chapitre 3 d�crit le mode **aper�u** (l'agent propose, l'humain valide).
Le mode **ex�cuter** inverse le contrat : OpenCode ex�cute r�ellement la
consigne dans le dossier du projet (�dition de fichiers + commandes shell).

### 4.1 POST /api/agent/tache (SSE)

- Corps : `{projet, message, session?}`.
- Ex�cution : `opencode run --format json --auto --dir <racine-projet> <message>`
  (binaire r�solu automatiquement : `OPENCODE_BIN`, exe npm, ou `opencode`
  du PATH). `--auto` l�ve les permissions (demande utilisateur explicite :
  agent autonome complet) ; `--dir` borne l'agent au dossier du projet.
- `--fork -s <sid>` : la conversation vit du c�t� OpenCode - chaque nouvelle
  t�che replie sur la session du projet (continuit�).
- S�quence SSE : `debut -> (texte|outil)* -> (erreur | fin)`.
  - `texte {delta}` : texte du mod�le, diff� par partie.
  - `outil {type, fichier?, commande?, resume?}` : une pastille par action
    r�elle (edit/write/delete/bash/read/glob/grep).
  - `erreur {code, message}` : `moteur_indisponible`, `timeout` (au-del�
    de `TACHE_TIMEOUT` s, d�faut 1200), `schema_invalide`, `interne`.
- D�l�gation testabilit� : `executer_tache(...)` accepte un faux processus
  inject� (tests unitaires sans r�seau).
- **Stop c�t� UI** : la fermeture du flux n'annulera PAS le process opencode
  (il finit en arri�re-plan, ses modifications restent visibles au prochain
  poll) - comportement document�.

### 4.2 Suivi de l'�diteur pendant une t�che

- `GET /api/projects/{projet}/etat?chemins=a.py&chemins=b.py` renvoie les
  empreintes SHA-1 des fichiers demand�s (`null` si absent) ; omettre
  `chemins` = tout le projet.
- Le frontend **poll toutes les 1,5 s** pendant une t�che et recharge les
  onglets dont l'empreinte a chang� - **sauf** les onglets marqu�s *dirty*
  (tampon �dit� main, jamais �cras�).

### 4.3 Terminal int�gr� (WebSocket)

- `WS /api/ws/terminal?projet=<id>` - un vrai `pwsh` **persistant par
  projet**, cwd = racine du projet.
- Connexion : bannire puis lignes de sortie fusionn�es (stdout+stderr) ;
  le client envoie des commandes en texte. Ctrl+C envoy� comme `\u0003`.
- Fermeture du WS : le shell survit et est r�utilis� � la reconnexion ;
  fermeture globale au shutdown uvicorn. Projet inconnu fermeture code 4404 ;
  `pwsh` absent code 4400.
- Le frontend y accde via le proxy Vite `/api` (`ws: true`).

### 4.4 S�curit� & limites

- `--auto` = l'agent peut ex�cuter n'importe quelle commande dans le dossier
  du projet (volontaire : demand� par l'utilisateur). Les chemins g�r�s par le
  backend (lecture/�criture de fichiers) restent borner par `CheminHorsProjet`.
- `TACHE_TIMEOUT` (s), `OUTIL_RESUME_MAX` (car. de r�sum�) configurables.
- Variable d'environnement `PROJETS_DIR` : surcharge utile aux tests.
- Serveur dev sur le port **8010** (le port 8000 peut �tre captur� par un
  processus syst�me persistant) ; proxy Vite `/api -> 127.0.0.1:8010`.

## 5. Mode « dossier direct » (ouvrir un projet réel sans copie)

Deux origines de projet coexistent, distinguées par `origine` :
- `archive` : importé sous `data/projets/` (bac à sable, mode actuel).
- `dossier` : un dossier réel de la machine, ouvert **en place** (édition
  directe des vrais fichiers) — c'est le mode « dossier direct ».

### 5.1 Registre des dossiers ouverts

- Persistance : `data/registre.json` → `{ "<id>": { "chemin": "<absolu>", "origine": "dossier" } }`.
  Surchargeable par `REGISTRE_FICHIER` (smoke live sans toucher au registre réel).
- `workspace.projet_existant(id)` / `racine_projet(id)` consultent le registre
  **avant** `data/projets/` : tout le reste du backend (tree, file, search,
  etat, apply-changes, terminal, agent) opère alors sur le disque réel, sans
  changement de code.
- `id` = slug du nom de dossier, suffixé si collision ; un même dossier rouvert
  réutilise le même `id`.

### 5.2 Validation d'un chemin (`valider_chemin_local`)

`resolve()` d'abord (symlinks/`..`), puis refus (`422`) si :
- chemin vide ou non absolu ; inexistant ; pas un dossier ;
- racine de lecteur (`C:\`) ;
- dossier système : `SystemRoot`, `SystemDrive`, `Program Files`/`(x86)`,
  `ProgramData`, `%USERPROFILE%\AppData` ;
- `data/`, `data/projets/` ou la racine du dépôt `ai-studio/` (ni un parent
  qui les contient) — évite de s'auto-écrire par-dessus l'IDE.

### 5.3 Respect du `.gitignore` + exclusions dures

- `services/filtres.py` : exclusions **dures** (`IGNORE_DOSSIERS` : `.git`,
  `.hg`, `.svn`, `node_modules`, `.venv`, `venv`, `dist`, `out`, `target`,
  `.tox`, `.cache`, `coverage`, `.pytest_cache`, `.mypy_cache`) appliquées à
  l'arborescence, la recherche, l'état et le comptage.
- **Secrets — exclusion dure indépendante du `.gitignore`** : `.env` et toutes
  ses variantes (`.env.*` : `.env.local`, `.env.production`, …), ainsi que
  `.envrc`, `.npmrc`, `.pypirc`, `.netrc`, `.htpasswd`, `.git-credentials`,
  `id_rsa`/`id_ed25519`/…, `credentials(.json)` et les suffixes
  `.pem`/`.key`/`.pfx`/`.p12`/`.keystore`. Ces fichiers n'apparaissent **jamais**
  dans l'arborescence, la recherche ou le contexte de l'agent, même si le projet
  n'a pas de `.gitignore` (même protection à l'import d'archive côté Streamlit).
  **Exception assumée** : les modèles d'environnement committés par convention
  (`.env.example`, `.env.sample`, `.env.template`) restent **visibles** — ils
  documentent les variables attendues sans valeurs et servent de contexte à
  l'agent.
- En complément, le `.gitignore` **racine** du dossier ouvert est appliqué via
  `pathspec` (`gitwildmatch`) : négations (`!fichier`) et ancrage (`/…`) gérés ;
  un dossier ignoré masque tout son contenu. Cache invalidé par `mtime`.

### 5.4 Garde-fous d'écriture (dossier réel)

- L'écriture passe **exclusivement** par le flux `POST /api/agent/chat`
  (proposition + hunks) → `POST /api/agent/apply-changes` avec garde-fou
  `source_hash` (accept/reject obligatoire) ; `PUT /api/projects/{id}/file`
  reste la sauvegarde éditeur explicite.
- `POST /api/agent/tache` (mode autonome `--auto`) renvoie **`403`** sur un
  dossier direct : pas d'exécution non validée.
- Anti-traversal inchangé : tout chemin relatif est borné par `CheminHorsProjet`
  (`422`), y compris en mode direct.
- Recommandation d'usage : projet réel sous **Git** (backup/undo) avant
  d'autoriser l'agent.

