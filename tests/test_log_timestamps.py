"""Every uvicorn log line (metadata-suite and geo-service) carries a timestamp with UTC offset."""
import json, logging, logging.config, os, io, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("app/log_config.json", "geo-service/app/log_config.json"):
    cfg = json.load(open(os.path.join(ROOT, p), encoding="utf-8"))
    assert "%(asctime)s" in cfg["formatters"]["default"]["fmt"] and "%(asctime)s" in cfg["formatters"]["access"]["fmt"], p
    buf = io.StringIO()
    logging.config.dictConfig(cfg)
    for h in logging.getLogger("uvicorn.access").handlers + logging.getLogger("uvicorn").handlers:
        h.setStream(buf)
    logging.getLogger("uvicorn.error").info("Started")
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "192.168.1.1:1", "GET", "/x", "1.1", 200)
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 2 and all(re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d{4} INFO:", l) for l in lines), lines
assert open(os.path.join(ROOT, "app/log_config.json")).read() == open(os.path.join(ROOT, "geo-service/app/log_config.json")).read()
assert "--log-config" in open(os.path.join(ROOT, "Dockerfile")).read()
assert "log_config=LOG_CONFIG" in open(os.path.join(ROOT, "geo-service/app/serve.py")).read()
print("PASS")
