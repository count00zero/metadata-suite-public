"""Every variable appears only ONCE in .env.example (active or commented) -- no confusing duplicates --
and the container user comes from PUID/PGID."""
import os, re, sys
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for rel in (".env.example", os.path.join("geo-service", ".env.example")):
    names = re.findall(r"^#* ?([A-Z][A-Z0-9_]*)=", open(os.path.join(R, rel)).read(), re.M)
    dups = sorted({n for n in names if names.count(n) > 1})
    assert not dups, f"{rel}: duplicated entries {dups}"
env = open(os.path.join(R, ".env.example")).read()
assert re.search(r"^PUID=1026$", env, re.M) and re.search(r"^PGID=100$", env, re.M)
for rel in ("docker-compose.yml", os.path.join("geo-service", "docker-compose.yml")):
    assert 'user: "${PUID:-1026}:${PGID:-100}"' in open(os.path.join(R, rel)).read(), rel
import yaml
svcs = yaml.safe_load(open(os.path.join(R, "docker-compose.yml")))["services"]
# wikimedia-build runs as root on purpose (it starts its own PostgreSQL as 'postgres') and hands the result to PUID:PGID
assert all(v["user"] == "${PUID:-1026}:${PGID:-100}" for k, v in svcs.items() if k not in ("wikimedia-build", "media-tools"))
always = {k for k, v in svcs.items() if not v.get("profiles")}
assert always == {"metadata-suite", "geo-service"}, always                      # suite AND geo-service start with `up`
assert svcs["sights-import"]["profiles"] == ["import"] and svcs["sights-import"]["image"] == "sights-import"   # on demand only
assert any(v.endswith("/osm:ro") for v in svcs["sights-import"]["volumes"]), "the planet file is mounted read-only"
print("PASS")
