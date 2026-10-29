"""Turn pytest JUnit failures (+ tails of worker/dispatcher logs) into GitHub check annotations,
which are readable without downloading logs/artifacts. Usage: junit_annotations.py RESULT.xml [LOG_ROOT]"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def emit(title, text, limit=3500):
    text = text[-limit:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title={title}::{text}")


def summary(paths, require_no_skips):
    """Publish executed/failed/skipped counts as notices; optionally fail when anything was skipped."""
    bad = False
    for path in map(Path, paths):
        root = ET.parse(path).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        counts = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
        print(f"::notice title=junit {path.name}::" + " ".join(f"{k}={v}" for k, v in counts.items()))
        bad |= require_no_skips and counts["skipped"] > 0
    if bad:
        print("::error title=skipped tests::fault tests must run, not skip, in this job")
        sys.exit(1)


def main():
    if sys.argv[1] == "--summary":
        args = [a for a in sys.argv[2:] if a != "--no-skips"]
        return summary(args, "--no-skips" in sys.argv)
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
