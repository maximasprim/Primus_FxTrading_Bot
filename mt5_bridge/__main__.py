"""Run with:  python -m mt5_bridge   (from the Server folder, on the Windows machine)."""
import logging
import os

import uvicorn

from mt5_bridge.server import app_from_env


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = app_from_env()  # also loads .env
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8765"))
    if host not in ("127.0.0.1", "localhost"):
        logging.getLogger("mt5_bridge").warning(
            "Listening on %s without TLS: put it behind HTTPS (Cloudflare Tunnel / Tailscale / reverse proxy).", host)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
