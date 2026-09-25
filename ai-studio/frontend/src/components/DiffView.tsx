import type { Hunk } from "../types/api";

interface DiffViewProps {
  hunks: Hunk[];
  acceptes: Set<number>;
  onBascule: (id: number) => void;
}

function teteHunk(h: Hunk): string {
  const anc = h.old_count > 0 ? `${h.old_start + 1},${h.old_count}` : `${h.old_start},0`;
  const nv = h.new_count > 0 ? `${h.new_start + 1},${h.new_count}` : `${h.new_start},0`;
  return `@@ -${anc} +${nv} @@`;
}

export function DiffView({ hunks, acceptes, onBascule }: DiffViewProps) {
  return (
    <div className="diff-view">
      {hunks.map((h) => (
        <div className="diff-hunk" key={h.id}>
          <div className="diff-hunk-head">
            <label>
              <input
                type="checkbox"
                checked={acceptes.has(h.id)}
                onChange={() => onBascule(h.id)}
              />
              {" "}accepter le bloc
            </label>
            <code>{teteHunk(h)}</code>
          </div>
          <pre className="diff-lignes">
            {h.lignes.map((l, k) => (
              <span key={k} className={"diff-ligne diff-" + l.type}>
                <span className="diff-num">{l.old_no ?? l.new_no ?? ""}</span>
                <span className="diff-texte">{l.contenu || " "}</span>
              </span>
            ))}
          </pre>
        </div>
      ))}
    </div>
  );
}