"""Create local Compose credentials without overwriting a user's configuration."""

import argparse
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    credentials = (
        "CASECHAMP_DB_OWNER_PASSWORD",
        "CASECHAMP_DB_APP_PASSWORD",
        "CASECHAMP_DB_MAINTENANCE_PASSWORD",
        "CASECHAMP_SESSION_SECRET",
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(args.output, flags, 0o600), "w", encoding="utf-8") as stream:
        for key in credentials:
            stream.write(f"{key}={secrets.token_hex(24)}\n")
        stream.write(f"CASECHAMP_PORT={args.port}\nCASECHAMP_WEB_ORIGIN=http://localhost:{args.port}\n")
        stream.write(f"CASECHAMP_ML_MODE={'REPLAY' if args.replay else 'DISABLED'}\n")
    print(f"Created {args.output}")


if __name__ == "__main__":
    main()
