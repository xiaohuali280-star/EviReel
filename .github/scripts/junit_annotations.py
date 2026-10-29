"""Turn pytest JUnit failures (+ tails of worker/dispatcher logs) into GitHub check annotations,
which are readable without downloading logs/artifacts. Usage: junit_annotations.py RESULT.xml [LOG_ROOT]"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def emit(title, text, limit=3500):
    text = text[-limit:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={title}::{text}")


def main():
    result, log_root = Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if not result.exists():
        emit("junit", f"{result} missing")
        return
    failed = []
    for case in ET.parse(result).iter("testcase"):
        for problem in list(case.iter("failure")) + list(case.iter("error")):
            name = case.get("name")
            failed.append(name)
            emit(f"{name}", f"{problem.get('message', '')}\n{problem.text or ''}")
    if log_root and log_root.exists() and failed:
        for log in sorted(log_root.rglob("logs/*.log")):
            test_dir = next((p.name for p in log.parents if any(f[:20] in p.name for f in failed)), None)
            if test_dir:
                emit(f"log {test_dir}/{log.name}", log.read_text(encoding="utf-8", errors="replace"), 2500)


if __name__ == "__main__":
    main()
