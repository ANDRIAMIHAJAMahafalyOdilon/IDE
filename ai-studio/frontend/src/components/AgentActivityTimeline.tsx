import { useMemo, useState } from "react";
import type { AgentActivity, AgentPermission, EvenementTache } from "../types/api";

interface Props {
  events: EvenementTache[];
  onOpenFile: (chemin: string) => void;
  onPermission: (requestId: string, reply: "once" | "always" | "reject") => Promise<void>;
}

type Item =
  | { kind: "activity"; data: AgentActivity }
  | { kind: "permission"; data: AgentPermission };

const ICONS: Record<string, string> = {
  thinking: "◌",
  file_read: "↳",
  file_search: "⌕",
  file_write: "✏️",
  file_create: "➕",
  file_delete: "🗑️",
  terminal: "⌘",
  diff: "▤",
  tool: "◆",
};

function outputText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function nomVisible(value: string): string {
  return value.replace(/OpenCode/gi, "Roch");
}

function statusLabel(status: string): string {
  if (status === "running") return "● En cours…";
  if (status === "success") return "✓ Terminé";
  if (status === "error") return "✗ Erreur";
  if (status === "cancelled") return "■ Annulé";
  if (status === "waiting_for_permission") return "! Autorisation requise";
  if (status === "rejected") return "✗ Action refusée";
  return status;
}

export function AgentActivityTimeline({ events, onOpenFile, onPermission }: Props) {
  const [ouvert, setOuvert] = useState(true);
  const [permissionsEnCours, setPermissionsEnCours] = useState<Set<string>>(new Set());
  const [permissionErreur, setPermissionErreur] = useState<string | null>(null);
  const items = useMemo<Item[]>(() => {
    const ordre: string[] = [];
    const index = new Map<string, Item>();
    for (const event of events) {
      if (event.event === "activite" || event.event === "outil") {
        const key = "activity:" + event.data.id;
        if (!index.has(key)) ordre.push(key);
        index.set(key, { kind: "activity", data: event.data });
      } else if (event.event === "permission") {
        const key = "permission:" + event.data.id;
        if (!index.has(key)) ordre.push(key);
        const previous = index.get(key);
        index.set(key, {
          kind: "permission",
          data: previous?.kind === "permission"
            ? { ...previous.data, ...event.data }
            : event.data,
        });
      }
    }
    return ordre.map((key) => index.get(key)).filter((item): item is Item => Boolean(item));
  }, [events]);

  if (!items.length) return null;

  async function repondre(permission: AgentPermission, reply: "once" | "always" | "reject") {
    setPermissionsEnCours((set) => new Set(set).add(permission.id));
    try {
      setPermissionErreur(null);
      await onPermission(permission.id, reply);
    } catch (error) {
      setPermissionErreur(error instanceof Error ? error.message : String(error));
    } finally {
      setPermissionsEnCours((set) => {
        const next = new Set(set);
        next.delete(permission.id);
        return next;
      });
    }
  }

  return (
    <section className="agent-activity">
      <button className="agent-activity-head" onClick={() => setOuvert((value) => !value)}>
        <span>{ouvert ? "▼" : "▶"} Roch · activité en direct</span>
        <span className="agent-activity-count">{items.length} étape{items.length > 1 ? "s" : ""}</span>
      </button>
      {permissionErreur && <div className="agent-permission-error">{permissionErreur}</div>}
      {ouvert && (
        <div className="agent-activity-list">
          {items.map((item) => {
            if (item.kind === "permission") {
              const permission = item.data;
              const attente = permission.status === "waiting_for_permission";
              return (
                <div className={"agent-permission " + (attente ? "permission-attente" : "")} key={"permission:" + permission.id}>
                  <div className="agent-activity-title">🔐 {statusLabel(permission.status)}</div>
                  <div className="agent-permission-action">Roch demande : <code>{permission.action}</code></div>
                  {Array.isArray(permission.resources) && permission.resources.length > 0 && (
                    <pre className="agent-activity-detail">{permission.resources.map(String).join("\n")}</pre>
                  )}
                  {attente && (
                    <div className="agent-permission-actions">
                      <button disabled={permissionsEnCours.has(permission.id)} onClick={() => void repondre(permission, "once")}>Autoriser</button>
                      <button disabled={permissionsEnCours.has(permission.id)} onClick={() => void repondre(permission, "always")}>Toujours autoriser</button>
                      <button disabled={permissionsEnCours.has(permission.id)} onClick={() => void repondre(permission, "reject")}>Refuser</button>
                    </div>
                  )}
                </div>
              );
            }
            const activity = item.data;
            const fichiers = activity.fichiers ?? (activity.fichier ? [activity.fichier] : []);
            return (
              <article className={"agent-activity-item activity-" + activity.status} key={"activity:" + activity.id}>
                <div className="agent-activity-line">
                  <span className="agent-activity-icon">{ICONS[activity.type] ?? "◆"}</span>
                  <span className="agent-activity-title">{nomVisible(activity.title)}</span>
                  <span className="agent-activity-status">{statusLabel(activity.status)}</span>
                </div>
                {activity.description && <div className="agent-activity-description">{nomVisible(activity.description)}</div>}
                {fichiers.map((fichier) => (
                  <button className="agent-file-link" key={fichier} onClick={() => onOpenFile(fichier)}>{fichier}</button>
                ))}
                {activity.type === "terminal" && activity.commande ? (
                  <div className="agent-terminal">
                    <div className="agent-terminal-question">Commande de Roch</div>
                    <div><span>Environment:</span> {activity.environnement ?? "local"}</div>
                    <div><span>Reason:</span> {nomVisible(activity.raison ?? "Roch travaille dans le projet.")}</div>
                    <code className="agent-terminal-command">$ {activity.commande}</code>
                    {activity.output && <pre className="agent-terminal-output">{activity.output}</pre>}
                  </div>
                ) : activity.commande ? (
                  <code className="agent-command">$ {activity.commande}</code>
                ) : null}
                {activity.error && <pre className="agent-activity-error">{activity.error}</pre>}
                {activity.output && activity.type !== "terminal" && (
                  <details className="agent-activity-details">
                    <summary>Sortie réelle</summary>
                    <pre>{activity.output}</pre>
                  </details>
                )}
                {activity.diff != null && (
                  <details
                    className="agent-activity-details agent-change-details"
                    open={activity.status === "running" || activity.type === "file_write" || activity.type === "file_create"}
                  >
                    <summary>Voir les changements</summary>
                    <pre>{outputText(activity.diff)}</pre>
                  </details>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
