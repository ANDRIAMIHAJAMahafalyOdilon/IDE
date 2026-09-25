import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ReactNode } from "react";

// Coloration syntaxique minimale : tokens communs, détectés par expression.
const MOTS_CLES =
  /\b(def|return|if|elif|else|for|while|import|from|class|function|const|let|var|async|await|try|except|finally|lambda|yield|with|as|in|not|and|or|pass|raise|break|continue|global|nonlocal|True|False|None|null|undefined|true|false|int|str|float|bool|void|this|new|typeof|instanceof|print|range|len|self|nullptr)\b/;

const TOKENS = new RegExp(
  [
    "(\\/\\*[\\s\\S]*?\\*\\/|\\/\\/[^\\n]*|#[^\\n]*)", // commentaire
    "(\"\"\"[\\s\\S]*?\"\"\"|'''(?:[^']|'')*?'''|\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')", // chaîne
    "(\\b\\d+(?:\\.\\d+)?\\b)", // nombre
    `(${MOTS_CLES.source})`, // mot-clé
  ].join("|"),
  "gm",
);

function coloriser(texte: string): ReactNode[] {
  const segments: ReactNode[] = [];
  let dernier = 0;
  let i = 0;
  for (const m of texte.matchAll(TOKENS)) {
    const idx = m.index ?? 0;
    if (idx > dernier) segments.push(texte.slice(dernier, idx));
    let classe = "md-tok-plain";
    if (m[1]) classe = "md-tok-com";
    else if (m[2]) classe = "md-tok-str";
    else if (m[3]) classe = "md-tok-num";
    else if (m[4]) classe = "md-tok-kw";
    segments.push(
      <span key={i++} className={classe}>
        {m[0]}
      </span>,
    );
    dernier = idx + m[0].length;
  }
  if (dernier < texte.length) segments.push(texte.slice(dernier));
  return segments;
}

/** Rendu Markdown léger (gras, listes, code minimalement coloré…). */
export function Markdown({ texte }: { texte: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children }) {
            const langue = /language-([\w-]+)/.exec(className ?? "")?.[1] ?? null;
            const brut = String(children ?? "").replace(/\n$/, "");
            return (
              <code className={"md-code" + (langue ? " md-code-bloc" : " md-code-inline")}>
                {coloriser(brut)}
              </code>
            );
          },
          pre({ children }) {
            return <pre className="md-pre">{children}</pre>;
          },
          a({ children, href }) {
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {texte}
      </ReactMarkdown>
    </div>
  );
}