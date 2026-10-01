"""Le port 4097 est-il vraiment occupe, ou est-ce une entree fantome ?

    python tests/_port_4097_etat.py
"""

from __future__ import annotations

import socket
import subprocess

for port in (4096, 4097):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            print(f"  port {port} : LIBRE (on peut le prendre)")
        except OSError as exc:
            print(f"  port {port} : OCCUPE -> {exc}")

print("\nnetstat :")
sortie = subprocess.run(
    ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True
).stdout
for ligne in sortie.splitlines():
    if ":4096" in ligne or ":4097" in ligne:
        print("  " + ligne.strip())
