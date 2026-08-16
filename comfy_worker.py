"""Run the chain runner in a separate process and release the controller lock."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def write_result(path: Path, exit_code: int) -> None:
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "exit_code": exit_code,
        "meaning": "needs_agent_review" if exit_code == 2 else "completed" if exit_code == 0 else "failed",
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--approve-latest", action="store_true")
    args = parser.parse_args()

    command = [sys.executable, str(args.runner), str(args.config)]
    if args.approve_latest:
        command.append("--approve-latest")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.log.open("a", encoding="utf-8") as log:
            log.write(f"\n--- controller started {datetime.now(timezone.utc).isoformat()} ---\n")
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
            log.write(f"--- controller finished with exit code {result.returncode} ---\n")
        write_result(args.log.parent / "controller-result.json", result.returncode)
        return result.returncode
    finally:
        args.lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
