import { useRef, useState } from "react";

import { importerProjet, telechargerProjet } from "../api/client";
import { useStudio } from "../store/studio";

/** Sélecteur de projet dans l'entête : liste des projets déjà ouverts.
 *
 *  Deux façons d'ajouter un projet, et le choix dépend de qui héberge :
 *  - « ＋ » (composant edit) : référence le disque de l'utilisateur, aucune
 *    copie. Serveur = machine locale uniquement, masqué ailleurs.
 *  - « ↑ zip » : le dossier est envoyé au serveur, qui le décompresse. Marche
 *    partout, y compris sur une instance cloud, et c'est le seul chemin
 *    disponible quand le serveur n'est pas votre machine. */
export function ProjetSelect() {
  const projets = useStudio((s) => s.projets);
  const projet = useStudio((s) => s.projet);
  const { changerProjet, chargerProjets } = useStudio();

  const courant = projets.find((p) => p.id === projet);

  const inputRef = useRef<HTMLInputElement>(null);
  const [importe, setImporte] = useState(false);
  const [erreur, setErreur] = useState<string | null>(null);

  async function choisirZip(fichier: FileList | null) {
    const zip = fichier?.[0];
    if (!zip) return;
    setErreur(null);
    setImporte(true);
    try {
      const cree = await importerProjet(zip);
      await chargerProjets();
      await changerProjet(cree.id);
    } catch (e) {
      setErreur(e instanceof Error ? e.message : String(e));
    } finally {
      setImporte(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="pj">
      <select
        className="pj-select"
        value={projet ?? ""}
        onChange={(e) => e.target.value && changerProjet(e.target.value)}
        title={courant?.chemin ?? "Choisir un projet"}
      >
        <option value="">— aucun projet —</option>
        {projets.map((p) => (
          <option key={p.id} value={p.id} title={p.chemin}>
            {p.nom} · {p.nb_fichiers} fichier{p.nb_fichiers > 1 ? "s" : ""}
          </option>
        ))}
      </select>

      <input
        ref={inputRef}
        type="file"
        accept=".zip,application/zip"
        hidden
        onChange={(e) => void choisirZip(e.target.files)}
      />
      <button
        className="pj-export"
        onClick={() => inputRef.current?.click()}
        disabled={importe}
        title="Importer un projet depuis un zip"
      >
        {importe ? "…" : "↑ zip"}
      </button>

      <button
        className="pj-export"
        onClick={() => projet && telechargerProjet(projet)}
        disabled={!projet}
        title="Télécharger le projet en zip"
      >
        ↓ zip
      </button>

      {erreur && <span className="pj-erreur">{erreur}</span>}
    </div>
  );
}
