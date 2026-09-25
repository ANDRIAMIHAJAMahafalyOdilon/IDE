import { useStudio } from "../store/studio";

/** Sélecteur de projet dans l'entête : liste des projets déjà ouverts.
 *  Le choix d'un NOUVEAU dossier se fait via le bouton « ＋ » du composeur
 *  edit (référence disque : aucune copie, aucun téléchargement). */
export function ProjetSelect() {
  const projets = useStudio((s) => s.projets);
  const projet = useStudio((s) => s.projet);
  const { changerProjet } = useStudio();

  const courant = projets.find((p) => p.id === projet);

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
    </div>
  );
}
