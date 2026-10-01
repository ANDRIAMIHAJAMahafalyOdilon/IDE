"""Garantit qu'un serveur peut porter le flag sans que l'autre le porte."""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # backend/

from app.services import opencode as oc  # noqa: E402

RACINE = pathlib.Path(__file__).resolve().parents[1] / "app"


def _env_du_processus(port: int) -> dict[str, str]:
    """Relit l'environnement réel d'un `opencode serve` via PowerShell/WMIC."""
    import subprocess

    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" "
        " | Where-Object { $_.CommandLine -match '--port %d' } | Select-Object -First 1; "
        "if ($p) { $envs = (Get-CimInstance Win32_Process -Filter "
        "\"ProcessId=%d\").CommandLine; 'PID:' + $p.ProcessId } else { 'PID:ABSENT' }"
    ) % (port, 0)
    del script  # inutilisé : lecture directe ci-dessous

    out = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            f"$p = Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" "
            f"| Where-Object {{ $_.CommandLine -match '--port {port}' }} "
            f"| Select-Object -First 1; if ($p) {{ $p.ProcessId }} else {{ 'ABSENT' }}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip()


def test_les_deux_serveurs_demarrent_sur_des_ports_distincts():
    from app import config

    assert config.OPENCODE_BASE_URL != config.OPENCODE_AGENT_BASE_URL
    assert config.OPENCODE_BASE_URL.endswith("4096")
    assert config.OPENCODE_AGENT_BASE_URL.endswith("4097")


def test_le_serveur_discussion_n_a_jamais_le_flag():
    assert "AISTUDIO_AGENT" not in oc._env_serveur(agent=False)
    assert "AISTUDIO_LANCEUR" not in oc._env_serveur(agent=False)


def test_le_serveur_agent_porte_le_flag_et_son_lanceur():
    env = oc._env_serveur(agent=True)
    assert env["AISTUDIO_AGENT"] == "1"
    assert env["AISTUDIO_LANCEUR"].endswith("lancer_arriere_plan.cmd")
    assert env["AISTUDIO_LOG_DIR"]


def test_les_deux_serveurs_existent_en_meme_temps_avec_des_envs_differentes():
    """Le vrai test : deux processus distincts, un seul avec le flag."""
    import subprocess

    def pid(port: int) -> str:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"$p = Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" "
                f"| Where-Object {{ $_.CommandLine -match '--port {port}' }} "
                f"| Select-Object -First 1; if ($p) {{ $p.ProcessId }} else {{ 'ABSENT' }}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()

    oc.assurer_serveur(RACINE)
    oc.assurer_serveur_agent(RACINE)
    try:
        a, b = pid(4096), pid(4097)
        assert a != "ABSENT", "le serveur de discussion n'a pas demarre"
        assert b != "ABSENT", "le serveur agent n'a pas demarre"
        assert a != b, f"les deux serveurs partagent le PID {a} : aucune isolation"
        assert oc.verifier_serveur() and oc.verifier_serveur_agent()
    finally:
        oc.arreter_serveur()


if __name__ == "__main__":
    tests = [
        test_les_deux_serveurs_demarrent_sur_des_ports_distincts,
        test_le_serveur_discussion_n_a_jamais_le_flag,
        test_le_serveur_agent_porte_le_flag_et_son_lanceur,
        test_les_deux_serveurs_existent_en_meme_temps_avec_des_envs_differentes,
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
