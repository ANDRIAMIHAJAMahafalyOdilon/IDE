import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config

print("frozen              :", config.FROZEN if hasattr(config, "FROZEN") else "?")
print("OPENCODE_CONFIG     :", config.OPENCODE_CONFIG)
print("existe              :", Path(config.OPENCODE_CONFIG).exists())
print("OPENCODE_AGENT_URL  :", config.OPENCODE_AGENT_BASE_URL)

p = Path(config.OPENCODE_CONFIG)
if p.exists():
    txt = p.read_text(encoding="utf-8", errors="replace")
    for ligne in txt.splitlines():
        if '"bash"' in ligne or '"task"' in ligne:
            print("  config ->", ligne.strip())
