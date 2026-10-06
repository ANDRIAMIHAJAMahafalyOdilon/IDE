"""Force en arrière-plan les commandes de serveur qui ne se terminent jamais.

Pourquoi un plugin OpenCode et pas un filtre dans notre boucle de lecture :
`opencode run` n'expose AUCUN événement avant exécution d'un outil. La demande
de permission part sur stderr en texte brut (« ! permission requested: bash … ;
auto-rejecting ») et, sans TTY, elle est auto-refusée. Le backend ne voit donc
la commande qu'*après* — trop tard pour l'empêcher de bloquer.

Le seul accroche qui précède l'exécution est le hook `tool.execute.before` d'un
plugin, qui reçoit `output.args.command` et peut la réécrire. C'est ici.

Deux garde-fous :
  * le plugin ne réécrit QUE si `AISTUDIO_AGENT=1`, variable que l'executable pose
    sur le processus `opencode` qu'il lance. Sans elle, la session interactive
    de l'utilisateur garde un `npm run dev` au premier plan, comme attendu ;
  * la détection est une liste de motifs explicites, testée. `npm test`,
    `npm install`, `git status` ne doivent RIEN_MATCHER.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Marqueurs de "ceci ne rend jamais la main".
COMMANDES_LONGUES: tuple[str, ...] = (
    r"\bnpm\s+(run|run-script)\s+(dev|start|serve|watch|preview)\b",
    r"\byarn\s+(dev|start|serve)\b",
    r"\bpnpm\s+(dev|start|serve)\b",
    r"\bnpm\s+(exec|x)\s+.*\b(serve|dev|watch)\b",
    r"\b(next|vite|ng|nuxt|svelte-kit|astro)\s+(dev|start|serve|preview)\b",
    # `vite` seul suffit : sans sous-commande il démarre le serveur de dev.
    # `vite build` reste fini, d'où la négation et l'entrée courte correspondante.
    r"\b(npx\s+)?vite\b(?!\s+build\b)",
    r"\bnodemon\b",
    r"\btsx\s+watch\b",
    r"\bflask\s+run\b",
    r"python\w*\s+-m\s+flask\s+run\b",
    r"\bmanage\.py\s+runserver\b",
    r"\buvicorn\b",
    r"\bpython\w*\s+-m\s+http\.server\b",
)

# Doit rester Finie. Un faux positif ici casserait un agent sur `npm test`.
COMMANDES_COURTES: tuple[str, ...] = (
    r"\bnpm\s+(test|install|ci|run\s+build|run\s+lint)\b",
    r"\bgit\s+(status|log|diff|add|commit)\b",
    r"\bpip\s+install\b",
    r"\bvite\s+build\b",
    r"\bnext\s+build\b",
)

_TOLERANCES = tuple(re.compile(p, re.IGNORECASE) for p in COMMANDES_LONGUES)
_TOLERANCES_COURTES = tuple(re.compile(p, re.IGNORECASE) for p in COMMANDES_COURTES)


def commande_bloquante(commande: str) -> str | None:
    """Motif long trouvé dans la commande, ou None.

    Les motifs courts priment : `npm run build` contient « run » mais finit, et
    doit rester exécuté au premier plan.
    """
    if not commande or any(m.search(commande) for m in _TOLERANCES_COURTES):
        return None
    for motif in _TOLERANCES:
        trouve = motif.search(commande)
        if trouve:
            return trouve.group(0)
    return None


def _js_commandes() -> str:
    return json.dumps(list(COMMANDES_LONGUES), ensure_ascii=False)


_PLUGIN = r"""// Plugin AI Studio — genere. Ne pas editer a la main.
// But : une commande de serveur (`npm run dev`, `flask run`…) ne rend jamais la
// main et bloque la tache. Le modele ne pense pas a l'arriere-plan, on le fait
// pour lui, AVANT execution, via le hook `tool.execute.before`.
const MOTIFS = %s;
const RE = MOTIFS.map((m) => new RegExp(m, "i"));

export const ArrierePlanPlugin = async (ctx) => {
  const lanceur = process.env.AISTUDIO_LANCEUR;
  const dossier = process.env.AISTUDIO_LOG_DIR;
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return;
      // Reserve a l'agent de l'executable : la session interactive de
      // l'utilisateur garde ses serveurs au premier plan.
      if (!process.env.AISTUDIO_AGENT) return;
      if (!lanceur || !dossier) return;
      const commande = output?.args?.command;
      if (typeof commande !== "string" || !commande.trim()) return;
      if (!RE.some((r) => r.test(commande))) return;
      // Ne jamais emballer une commande déjà préparée : sinon un appel au
      // lanceur contenant `npm run dev` devient un lanceur imbriqué.
      if (/AISTUDIO_FOND|lancer_arriere_plan\.cmd|AISTUDIO_LANCEUR/i.test(commande)) return;
      // Le lanceur est un .cmd. Un encodage base64 évite que les guillemets de
      // la commande soient massacrés par les couches PowerShell/cmd imbriquées.
      const jeton = "AISTUDIO_FOND_" + Date.now();
      const journal = dossier + "/" + jeton + ".log";
      const encodee = Buffer.from(commande.replace(/\s+/g, " ").trim(), "utf8").toString("base64");
      output.args.command =
        'Set-Content -Path "' + journal + '" -Value "' + jeton
        + '" -Encoding utf8; & "' + lanceur + '" "-Base64" "' + encodee + '" "' + journal
        + '" "' + (ctx?.directory || "") + '"';
    },
  };
};
"""

# Lanceur Windows : demarre la commande dans une fenetre cachee, rend la main
# immediatement. Le serveur survit a la fin de l'appel ; le journal indique ce
# qui s'est passe, y compris un echec au demarrage.
#
# ASCII strict, nom compris : `cmd.exe` lit un .cmd dans la page de codes de la
# console, ou un accent mal interprete casse jusqu'au quoting. Un nom accents
# echoue de la meme facon des que l'appel est passe par un autre jeu de
# caracteres.
_LANCEUR = """@echo off
rem Generated by AI Studio. Starts a server command in the background.
rem `%~dp0` keeps this next to the .ps1 it calls.
setlocal
set "MODE=%~1"
if /I "%MODE%"=="-Base64" (
  set "CMD_B64=%~2"
  set "LOG=%~3"
  set "DIR=%~4"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0demarrer_arriere_plan.ps1" -CommandeBase64 "%CMD_B64%" -Journal "%LOG%" -Repertoire "%DIR%"
) else (
  set "CMD=%~1"
  set "LOG=%~2"
  set "DIR=%~3"
  if "%CMD%"=="" ( echo usage: lancer_arriere_plan.cmd "<commande>" "<journal>" "<repertoire>" & exit /b 2 )
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0demarrer_arriere_plan.ps1" -Commande "%CMD%" -Journal "%LOG%" -Repertoire "%DIR%"
)
endlocal
exit /b 0
"""

# Le fichier .ps1 est passe a `-File`, jamais en ligne : `-Command "..."`
# imbrique dans PowerShell se fait mang par les guillemets.
_DETACHEUR = """param(
    [string]$Commande,
    [string]$CommandeBase64,
    [Parameter(Mandatory = $true)][string]$Journal,
    [string]$Repertoire
)
$erreurs = "$Journal.err"
$commandeEffective = $Commande
if ($CommandeBase64) {
    try {
        $octets = [Convert]::FromBase64String($CommandeBase64)
        $commandeEffective = [Text.Encoding]::UTF8.GetString($octets)
    } catch {
        Add-Content -LiteralPath $erreurs -Value "Commande base64 invalide : $($_.Exception.Message)"
        exit 2
    }
}
if (-not $commandeEffective) { exit 2 }
# La redirection va DANS la ligne de commande, pas dans -RedirectStandardOutput :
# ce dernier pompe les flux de facon synchrone et PowerShell 5.1 attend alors
# la fin du serveur, ce qui rebloque exactement la tache qu'on veut liberer.
$interne = $commandeEffective + ' >> "' + $Journal + '" 2>> "' + $erreurs + '"'
$depart = @{ FilePath = "cmd.exe"; ArgumentList = "/c", $interne; WindowStyle = "Hidden" }
# Le repertoire est explicite : `npm run dev` ne veut pas dependre du shell
# appelant, qui n'est pas forcement a la racine du projet.
if ($Repertoire -and (Test-Path -LiteralPath $Repertoire)) {
    $depart["WorkingDirectory"] = $Repertoire
}
# Sans -NoNewWindow : le fils recoit ses propres handles et n'en laisse aucun
# ouvert vers la sortie du shell appelant. Sinon `start /b` laissait le pipe
# ouvert et l'appelant attendait la fin du serveur.
Start-Process @depart | Out-Null
Write-Output "[AI Studio] commande lancee en arriere-plan."
Write-Output "[AI Studio] journal : $Journal"
"""


def chemin_lanceur() -> Path:
    return dossier_agents() / "lancer_arriere_plan.cmd"


def chemin_detacheur() -> Path:
    return dossier_agents() / "demarrer_arriere_plan.ps1"



def plugin_js() -> str:
    return _PLUGIN % _js_commandes()


def dossier_plugins() -> Path:
    """Repertoire global des plugins OpenCode (`~/.config/opencode/plugins/`)."""
    return Path.home() / ".config" / "opencode" / "plugins"


def dossier_agents() -> Path:
    """Journaux des commandes lancees en arriere-plan (hors des projets)."""
    from ..config import DATA_DIR

    return DATA_DIR / "logs" / "agents"


def installer() -> Path | None:
    """Ecrit le plugin et le lanceur. Renvoie le chemin du plugin ecrit."""
    cible = dossier_plugins() / "aistudio-arriere-plan.js"
    contenu = plugin_js()
    fichiers = (
        (chemin_lanceur(), _LANCEUR),
        (chemin_detacheur(), _DETACHEUR),
    )
    try:
        for chemin, attendu in fichiers:
            chemin.parent.mkdir(parents=True, exist_ok=True)
            if not chemin.is_file() or chemin.read_text(encoding="utf-8") != attendu:
                chemin.write_text(attendu, encoding="utf-8")
        if cible.is_file() and cible.read_text(encoding="utf-8") == contenu:
            return cible
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(contenu, encoding="utf-8")
    except OSError:
        return None
    return cible
