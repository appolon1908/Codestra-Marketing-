#!/usr/bin/env python3
"""Export the private command schema without touching a database or provider."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.marketing_policy import ProviderCommand

if __name__ == "__main__":
    (ROOT / "contracts/marketing.commands.v1.json").write_text(
        json.dumps(ProviderCommand.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
