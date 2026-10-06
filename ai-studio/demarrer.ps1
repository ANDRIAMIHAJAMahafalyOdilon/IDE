param(
    [switch]$NePasOuvrir
)

$ErrorActionPreference = "Stop"
$racine = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $racine "backend"
$frontend = Join-Path $racine "frontend"
$python = Join-Path $backend "venv\Scripts\python.exe"
$vite = Join-Path $frontend "node_modules\.bin\vite.cmd"
$logs = Join-Path $racine "data\logs\lancement"
$backendLog = Join-Path $logs "backend.log"
$backendErr = Join-Path $logs "backend.err.log"
$frontendLog = Join-Path $logs "frontend.log"
$frontendErr = Join-Path $logs "frontend.err.log"

New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Url($url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

if (-not (Test-Path $python)) {
    throw "Environnement Python introuvable : $python"
}
if (-not (Test-Path $vite)) {
    throw "Dépendances frontend absentes. Exécutez npm install dans $frontend"
}

# Le mode développement démarre uvicorn directement (et non lancer.py, réservé
# à l'exécutable). On installe donc explicitement le plugin AI Studio avant le
# backend, sinon le serveur Edit peut réutiliser une ancienne version du helper.
$ancienPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $backend
try {
    & $python -c "from app.services.agent_fond import installer; installer()"
    if ($LASTEXITCODE -ne 0) {
        throw "Impossible d'installer le plugin AI Studio pour l'agent Edit."
    }
} finally {
    $env:PYTHONPATH = $ancienPythonPath
}

# Une instance déjà saine est réutilisée : le relancement est idempotent.
$backendPret = Test-Url "http://127.0.0.1:8010/health"
if (-not $backendPret) {
    $backendProcess = Start-Process `
        -FilePath $python `
        -WorkingDirectory $backend `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010") `
        -RedirectStandardOutput $backendLog `
        -RedirectStandardError $backendErr `
        -PassThru

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $backendPret = Test-Url "http://127.0.0.1:8010/health"
        if ($backendProcess.HasExited -and -not $backendPret) {
            throw "Le backend s'est arrêté pendant son démarrage. Consultez $backendLog"
        }
    } while (-not $backendPret -and (Get-Date) -lt $deadline)
    if (-not $backendPret) {
        throw "Le backend n'est pas prêt après 30 secondes. Consultez $backendLog"
    }
}

$frontendPret = Test-Url "http://127.0.0.1:5173/"
if (-not $frontendPret) {
    Start-Process `
        -FilePath $vite `
        -WorkingDirectory $frontend `
        -ArgumentList @("--host", "127.0.0.1", "--port", "5173") `
        -RedirectStandardOutput $frontendLog `
        -RedirectStandardError $frontendErr | Out-Null

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $frontendPret = Test-Url "http://127.0.0.1:5173/"
    } while (-not $frontendPret -and (Get-Date) -lt $deadline)
    if (-not $frontendPret) {
        throw "Le frontend n'est pas prêt après 30 secondes. Consultez $frontendLog"
    }
}

$url = "http://127.0.0.1:5173/"
Write-Output "AI Studio est prêt : $url"
Write-Output "Backend : http://127.0.0.1:8010/health"
if (-not $NePasOuvrir) {
    Start-Process $url | Out-Null
}
