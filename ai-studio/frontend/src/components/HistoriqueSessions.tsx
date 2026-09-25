import { useState } from "react";
import { useDiscussions } from "../store/discussions";
import { useStudio } from "../store/studio";

/** Colonne gauche de l'application : historique visible en Chat comme en Edit. */
export function HistoriqueSessions() {
  const sessions = useDiscussions((s) => s.sessions);
  const activeId = useDiscussions((s) => s.activeId);
  const { nouvelle, ouvrir, renommer, supprimer } = useDiscussions();
  const setModeAgent = useStudio((s) => s.setModeAgent);
  const [enEdition, setEnEdition] = useState<string | null>(null);
  const [brouillon, setBrouillon] = useState("");

  function lancer() {
    nouvelle();
    setModeAgent("chat");
  }

  function validerRename(id: string) {
    if (brouillon.trim()) renommer(id, brouillon);
    setEnEdition(null);
  }

  return (
    <div className="historique">
      <div className="sidebar-header">
        <span className="logo" />
        <span className="sidebar-brand">Conversations</span>
      </div>
      <button className="historique-nouveau" onClick={lancer}>
        ＋ Nouvelle discussion
      </button>
      <div className="historique-liste">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={"historique-ligne" + (s.id === activeId ? " actif" : "")}
          >
            {enEdition === s.id ? (
              <input
                className="historique-rename"
                autoFocus
                value={brouillon}
                onChange={(e) => setBrouillon(e.target.value)}
                onBlur={() => validerRename(s.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") validerRename(s.id);
                  if (e.key === "Escape") setEnEdition(null);
                }}
              />
            ) : (
              <button
                className="historique-titre"
                onClick={() => ouvrir(s.id)}
                title={
                  s.messages.length
                    ? `${s.messages.length} message${s.messages.length > 1 ? "s" : ""}`
                    : undefined
                }
              >
                {s.titre}
              </button>
            )}
            <span className="historique-actions">
              <button
                title="Renommer"
                onClick={() => {
                  setEnEdition(s.id);
                  setBrouillon(s.titre);
                }}
              >
                ✎
              </button>
              <button
                title="Supprimer"
                onClick={() => supprimer(s.id)}
                disabled={s.id === activeId && useDiscussions.getState().sessions.length === 1}
              >
                ✕
              </button>
            </span>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="rien">aucune conversation pour l'instant</div>
        )}
      </div>
    </div>
  );
}
