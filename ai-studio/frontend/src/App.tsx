import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { HistoriqueSessions } from "./components/HistoriqueSessions";
import { ProjetSelect } from "./components/ProjetSelect";
import { useStudio } from "./store/studio";

export default function App() {
  const [theme, setTheme] = useState<"sombre" | "clair">(() => {
    try {
      return localStorage.getItem("roch-ai-theme") === "clair" ? "clair" : "sombre";
    } catch {
      return "sombre";
    }
  });
  const projet = useStudio((s) => s.projet);
  const modeAgent = useStudio((s) => s.modeAgent);
  const chargerProjets = useStudio((s) => s.chargerProjets);

  useEffect(() => {
    void chargerProjets();
  }, [chargerProjets]);

  function basculerTheme() {
    const suivant = theme === "sombre" ? "clair" : "sombre";
    setTheme(suivant);
    try {
      localStorage.setItem("roch-ai-theme", suivant);
    } catch {
      // Le thème reste utilisable même si le stockage local est indisponible.
    }
  }

  return (
    <div className={`coda theme-${theme}`}>
      <header className="coda-header">
        <span className="coda-brand">ROCH AI</span>
        {modeAgent === "edit" && <ProjetSelect />}
        <button
          className="theme-toggle"
          onClick={basculerTheme}
          title={theme === "sombre" ? "Passer au mode clair" : "Revenir au mode sombre"}
          aria-label={theme === "sombre" ? "Activer le mode clair" : "Activer le mode sombre"}
        >
          {theme === "sombre" ? "☀ Mode clair" : "☾ Mode sombre"}
        </button>
      </header>
      <div className="coda-corps">
        <aside className="coda-sidebar">
          <HistoriqueSessions mode={modeAgent} />
        </aside>
        <main className="coda-chat">
          <ChatPanel projet={projet ?? undefined} />
        </main>
      </div>
    </div>
  );
}
