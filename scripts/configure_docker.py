"""Prepare local demo configuration without printing credentials."""

import argparse
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key

parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true")
args = parser.parse_args()
path = Path(".env")
if not path.exists():
    raise SystemExit("Run aegisops init first.")
values = dotenv_values(path)
if not values.get("AEGIS_DB_PASSWORD"):
    set_key(path, "AEGIS_DB_PASSWORD", secrets.token_hex(24))
set_key(path, "AEGIS_DOCKER_SOURCE", "live" if args.live else "replay")
set_key(path, "AEGIS_DOCKER_OTLP_ENDPOINT", "http://otel-collector:4318" if args.live else "")
print("Docker configuration prepared. Local native Python settings are preserved.")
