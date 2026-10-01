import httpx

r = httpx.get("http://127.0.0.1:8011/api/projects", timeout=20)
for p in r.json()["projets"]:
    print(f"  {p['id']:<34} {p.get('origine',''):<8} {p.get('chemin','')}")
