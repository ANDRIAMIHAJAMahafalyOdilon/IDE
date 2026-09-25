import { useMemo, useState } from "react";
import { useDiscussions } from "../store/discussions";
import { useStudio } from "../store/studio";
import type { ModeAgent } from "../types/api";

interface HistoriqueSessionsProps {
  mode: ModeAgent;
}

/** Colonne gauche : une liste indépendante pour Chat et une autre pour Edit. */
export function HistoriqueSessions({ mode }: HistoriqueSessionsProps) {
  const toutesSessions = useDiscussions((s) => s.sessions);
  const sessions = useMemo(
    () => toutesSessions.filter((session) => session.mode === mode),
    [toutesSessions, mode],
  );
  const activeId = useDiscussions((s) => s.activeIds[mode]);
  const { nouvelle, ouvrir, renommer, supprimer } = useDiscussions();
  const setModeAgent = useStudio((s) => s.setModeAgent);
  const [enEdition, setEnEdition] = useState<string | null>(null);
  const [brouillon, setBrouillon] = useState("");

  function lancer() {
    nouvelle(mode);
    setModeAgent(mode);
  }

  function validerRename(id: string) {
    if (brouillon.trim()) renommer(id, brouillon);
    setEnEdition(null);
  }

  return (
    <div className="historique">
      <div className="sidebar-header">
        <span className="logo" />
        <span className="sidebar-brand">
          {mode === "chat" ? "Historique Chat" : "Historique Edit"}
        </span>
      </div>
      <button className="historique-nouveau" onClick={lancer}>
        {mode === "chat" ? "＋ Nouvelle discussion" : "＋ Nouvelle session Edit"}
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
                disabled={s.id === activeId && sessions.length === 1}
              >
                ✕
              </button>
            </span>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="rien">
            {mode === "chat"
              ? "aucune discussion pour l'instant"
              : "aucune session Edit pour l'instant"}
          </div>
        )}
      </div>
    </div>
  );
}
