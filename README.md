# IA — Assistant d'étude et de code

Un seul dépôt, une seule app : **AI Studio** (dossier `ai-studio/`).

L'ancien client Streamlit (app.py, etapes 1-4, opencode_client.py…) a été
fusionné dans `ai-studio/` puis supprimé : l'interface projet, le Chat
(Claude.ai-like) et le mode Édition (hunks accept/reject, Codex-like) ainsi
que la recherche sur les **documents de cours** (RAG : PDF/TXT/MD indexés en
FAISS, embeddings Gemini, recherche web DuckDuckGo) vivent maintenant dans un
seul projet backend FastAPI + frontend React.

## Architecture

```
ai-studio/
  backend/            FastAPI (port 8010) + venv
    app/
      api/            routes (agent, projets, organizer, terminal, documents)
      services/       moteurs Gemini/Groq, agent, RAG (services/rag/)
      models/         schémas pydantic (RequeteChat, Hunk, …)
    tests/            scripts autonomes (python tests/test_*.py)
  frontend/           React + Vite (localhost:5173), proxy /api → 8010
  data/
    projets/          arborescences importées (démo, archives)
    documents/        cours indexés (PDF/TXT/MD) — RAG
    index/            index FAISS + chunks.pkl (embeddings)
  archive/
    historique_global.json   archive de l'ancienne app streamlit
```

## Démarrage

```bash
# backend (Windows) — venv déjà créé
ai-studio\backend\venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8010

# frontend
cd ai-studio/frontend
npm install && npm run dev
```

Ouvrir `http://localhost:5173` (le hôte de Vite écoute sur `::1`).

## Mode edit et OpenCode

Dans le mode edit, OpenCode est sélectionné par défaut et lance le vrai agent autonome. Le sélecteur aperçu conserve en option le flux sûr par propositions de hunks à valider. La commande utilisée est opencode run --format json --auto : il peut lire le projet, enchaîner plusieurs opérations (lecture, édition, écriture, tests/commandes) et modifier plusieurs fichiers. Les onglets ouverts sont synchronisés pendant la tâche; les modifications locales non sauvegardées restent protégées.

## Clés API

Copier `GEMINI_API_KEY` et `GROQ_API_KEY` dans `ai-studio/.env`
(prioritaire) — le backend lit `ai-studio/.env` puis `ai-studio/backend/.env`.

## Tests

```bash
ai-studio\backend\venv\Scripts\python ai-studio\backend\tests\test_rag.py
# … et tous les autres test_*.py (aucun réseau requis, embeddings stubés)
```