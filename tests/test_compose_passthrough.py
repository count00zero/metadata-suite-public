"""Every env var the suite code reads (and every one the geo-service reads, except container-internal ones)
must be passed through in docker-compose.yml -- otherwise a value in .env never reaches the container."""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAT = re.compile(r'''(?:environ\.get|getenv|environ\[|_env\w*|_s|_f|_i|_b)\(\s*["']([A-Z][A-Z0-9_]{2,})["']''')
INTERNAL = {"GEO_BIND", "GEO_PORT", "GEO_CACHE_DB", "GEO_SRTM_DIR", "COUNTRY_INFO_FILE", "SEARCH_BACKEND_ORDER"}


def names(pattern):
    out = set()
    for f in glob.glob(os.path.join(ROOT, pattern), recursive=True):
        out |= set(PAT.findall(open(f, errors="ignore").read()))
    return out


def inc(text, n):
    return re.search(r"-\s*" + n + r"=|\$\{" + n + r"\b", text) is not None


def main():
    bad = []
    for comp, pats in (("docker-compose.yml", ["app/**/*.py", "geo-service/app/**/*.py"]),
                       ("geo-service/docker-compose.yml", ["geo-service/app/**/*.py"])):
        text = open(os.path.join(ROOT, comp)).read()
        for p in pats:
            bad += [f"{comp}: {n}" for n in sorted(names(p)) if n not in INTERNAL and not inc(text, n)]
    ex = open(os.path.join(ROOT, ".env.example")).read()
    bad += [f".env.example: {n}" for n in sorted(names("app/**/*.py")) if not re.search(r"^#?\s*" + n + "=", ex, re.M)]
    assert not bad, bad
    print("PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAIL", e); sys.exit(1)
