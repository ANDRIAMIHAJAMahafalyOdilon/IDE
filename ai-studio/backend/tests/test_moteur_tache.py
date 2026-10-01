"""Le chemin de production doit passer par le serveur, pas par `opencode run`.

    python tests/test_moteur_tache.py
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import agent_tache as at  # noqa: E402


def _racine() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "app"


def test_le_defaut_est_le_serveur():
    """Un `--auto` approuve tout en silence : c'est le serveur qui peut demander."""
    defaut = inspect.signature(at.executer_tache).parameters["moteur"].default
    assert defaut == "serveur"


def test_un_processus_injecte_force_toujours_la_cli():
    """Les tests de l'ancien chemin ne doivent pas parler à un vrai serveur."""
    from app.services import opencode as oc

    contacte: list[str] = []
    ancien = oc.assurer_serveur_agent
    oc.assurer_serveur_agent = lambda racine: contacte.append("agent")
    try:
        async def collect():
            return [
                evt
                async for evt in at.executer_tache(
                    _racine(), "rien", processus=object(), binaire="opencode.exe"
                )
            ]

        asyncio.run(collect())
    except Exception:  # noqa: BLE001 — le faux `object()` n'a pas de stdout
        pass
    finally:
        oc.assurer_serveur_agent = ancien
    assert contacte == [], "un processus injecte a quand meme contacte le serveur"


def test_le_serveur_dedie_est_bien_appele():
    """Appeler le moteur serveur doit démarrer le serveur AGENT (4097)."""
    from app.services import opencode as oc

    appels: list[str] = []
    ancien = oc.assurer_serveur_agent

    def faux(racine):
        appels.append("agent")

    oc.assurer_serveur_agent = faux
    try:
        async def collect():
            return [
                evt
                async for evt in at.executer_tache(_racine(), "lance mon projet")
            ]

        asyncio.run(collect())
    except Exception:  # noqa: BLE001 — le flux avise ensuite, sans serveur réel
        pass
    finally:
        oc.assurer_serveur_agent = ancien
    assert appels == ["agent"], f"assurer_serveur_agent non appele : {appels}"


if __name__ == "__main__":
    tests = [
        test_le_defaut_est_le_serveur,
        test_un_processus_injecte_force_toujours_la_cli,
        test_le_serveur_dedie_est_bien_appele,
    ]
    ok = 0
    for test in tests:
        try:
            test()
            print(f"  [OK] {test.__name__}")
            ok += 1
        except AssertionError as exc:
            print(f"  [ECHEC] {test.__name__} : {exc}")
    print(f"{ok}/{len(tests)} tests reussis")
    raise SystemExit(0 if ok == len(tests) else 1)
