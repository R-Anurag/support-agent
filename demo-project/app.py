"""acme-notes: a tiny notes server. Reads config from .env, logs to logs/app.log."""
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
(HERE / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=HERE / "logs" / "app.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def config():
    return {"port": int(os.environ["PORT"]), "secret": os.environ["APP_SECRET"],
            "log_level": os.environ.get("LOG_LEVEL", "info")}


class Notes(BaseHTTPRequestHandler):
    def do_GET(self):
        notes = HERE / "data" / "notes.txt"
        body = notes.read_text() if notes.exists() else "no notes yet\n"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == "__main__":
    load_env()
    try:
        cfg = config()
    except KeyError as e:
        logging.exception("startup failed: missing config %s", e)
        sys.exit(f"startup failed: missing config {e}")
    if "--check" in sys.argv:
        print("config OK")
        sys.exit(0)
    logging.info("listening on %s", cfg["port"])
    HTTPServer(("127.0.0.1", cfg["port"]), Notes).serve_forever()
