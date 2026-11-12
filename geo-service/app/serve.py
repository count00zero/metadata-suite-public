"""serve.py -- container entry point: main API (8000) + Nominatim facade (GEO_NOMINATIM_PORT) + Photon facade
(GEO_PHOTON_PORT), all in one process and sharing one cache / one set of overrides.   python -m app.serve"""
from __future__ import annotations

import os
import threading

import uvicorn

from .compat import create_nominatim_app, create_photon_app
from .main import create_app


LOG_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_config.json")     # timestamps in every log line


def main() -> None:
    host = os.environ.get("GEO_BIND", "0.0.0.0")
    app = create_app()
    st = app.state.st
    for port, factory in ((st.s.compat_nominatim_port, create_nominatim_app), (st.s.compat_photon_port, create_photon_app)):
        if port:
            srv = uvicorn.Server(uvicorn.Config(factory(st), host=host, port=port, log_level="warning", log_config=LOG_CONFIG))
            threading.Thread(target=srv.run, name=f"compat-{port}", daemon=True).start()   # dies with the main server
    uvicorn.run(app, host=host, port=int(os.environ.get("GEO_PORT", "8000")), log_config=LOG_CONFIG)


if __name__ == "__main__":
    main()
