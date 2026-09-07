#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

CONTRACT = ROOT / "contracts/openapi.v1.json"


def rendered() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    document = rendered()
    if args.write:
        CONTRACT.parent.mkdir(exist_ok=True)
        CONTRACT.write_text(document, encoding="utf-8")
        return
    if not CONTRACT.is_file() or CONTRACT.read_text(encoding="utf-8") != document:
        raise SystemExit("runtime OpenAPI differs from contracts/openapi.v1.json")
    parsed = json.loads(document)
    security_schemes = parsed.get("components", {}).get("securitySchemes", {})
    if not security_schemes:
        raise SystemExit("runtime OpenAPI lacks bearer security scheme")
    required_mutation_headers = {"X-Tenant-ID", "X-Correlation-ID", "Idempotency-Key"}
    for path, methods in parsed["paths"].items():
        for method, operation in methods.items():
            if method not in {"post", "put", "patch", "delete"} or path.startswith("/health"):
                continue
            headers = {
                parameter.get("name")
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header" and parameter.get("required")
            }
            missing = required_mutation_headers - headers
            if missing:
                raise SystemExit(f"{method.upper()} {path} lacks required headers: {sorted(missing)}")
    print("MARKETING_OPENAPI_PARITY=PASS")


if __name__ == "__main__":
    main()
