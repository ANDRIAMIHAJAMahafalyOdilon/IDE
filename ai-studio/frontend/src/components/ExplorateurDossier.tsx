import { useEffect, useState } from "react";
import { listerDossiersServeur } from "../api/client";
import type { ParcoursDossier } from "../api/client";

interface ExplorateurDossierProps {
  /** true = panneau ouvert : charge le point de départ à l'ouverture. */
  ouvert: boolean;
  /** Appelé avec le chemin choisi (à ouvrir) puis le panneau se ferme. */
  onOuvrir: (chemin: string) => void;
  onFermer: () => void;
}

/** Navigateur de dossiers serveur, option secondaire du « ＋ » du mode edit :
 *  le ＋ ouvre la VRAIE fenêtre Windows (endpoint /api/system/choisir-dossier) ;
 *  ici on parcourt depuis la page OU on colle un chemin, et un bouton valide le
 *  dossier courant (fix 422 : absent à l'Accueil, où le chemin est vide).
 *  Aucune copie : `onOuvrir` reçoit juste le chemin réel (open-local). */
export function ExplorateurDossier({
  ouvert,
  onOuvrir,
  onFermer,
}: ExplorateurDossierProps) {
  const [etat, setEtat] = useState<ParcoursDossier | null>(null);
  const [busy, setBusy] = useState(false);
  const [erreur, setErreur] = useState<string | null>(null);
  const [tape, setTape] = useState("");

  useEffect(() => {
    if (!ouvert || etat) return;
    setBusy(true);
    setErreur(null);
    listerDossiersServeur("")
      .then(setEtat)
      .catch((e) => setErreur(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  }, [ouvert, etat]);

  async function naviguer(chemin: string) {
    setBusy(true);
    setErreur(null);
    try {
      setEtat(await listerDossiersServeur(chemin));
    } catch (e) {
      setErreur(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function choisir() {
    if (!etat || !etat.chemin) return;
    onOuvrir(etat.chemin);
    onFermer();
  }

  function ouvrirTape() {
    const chemin = tape.trim();
    if (!chemin) return;
    onOuvrir(chemin);
    onFermer();
  }

  if (!ouvert) return null;

  return (
    <div className="pj-explorateur">
      <form
        className="pj-explorateur-saisie"
        onSubmit={(e) => {
          e.preventDefault();
          ouvrirTape();
        }}
      >
        <input
          className="pj-explorateur-champ"
          value={tape}
          onChange={(e) => setTape(e.target.value)}
          placeholder="Colle un chemin (C:\Users\…) ou parcours ci-dessous"
          spellCheck={false}
        />
        <button
          className="pj-bouton-petit"
          type="submit"
          disabled={!tape.trim()}
          title="Ouvrir ce chemin (éditions sur le disque, aucune copie)"
        >
          Ouvrir
        </button>
      </form>
      <div className="pj-explorateur-contexte">
        <button
          className="pj-bouton-petit"
          disabled={busy || !etat?.parent}
          onClick={() => etat?.parent && void naviguer(etat.parent)}
          title={etat?.parent ?? "Déjà au sommet (aucun parent valide)"}
        >
          {busy ? "…" : "↑"}
        </button>
        <span className="pj-explorateur-chemin" title={etat?.chemin}>
          {etat?.nom ? "📁 " + etat.nom : "Dossiers"}
        </span>
      </div>
      {erreur && <span className="pj-erreur">{erreur}</span>}
      {busy && !etat && <div className="pj-explorateur-vide">Chargement…</div>}
      {!busy && etat && etat.dossiers.length === 0 && (
        <div className="pj-explorateur-vide">Aucun sous-dossier navigable ici.</div>
      )}
      {etat && etat.dossiers.length > 0 && (
        <ul className="pj-explorateur-liste">
          {etat.dossiers.map((d) => (
            <li key={d.chemin}>
              <button
                className="pj-explorateur-dossier"
                onClick={() => void naviguer(d.chemin)}
                title={d.chemin}
              >
                📁 {d.nom}
              </button>
            </li>
          ))}
        </ul>
      )}
      {etat?.chemin ? (
        <button className="pj-choisir" onClick={choisir}>
          Choisir ce dossier
        </button>
      ) : null}
    </div>
  );
}