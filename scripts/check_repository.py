"""Fail CI on tracked runtime data or recognizable credentials."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(rb"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----")


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.split(b"\0")
    errors = []
    for raw in tracked:
        if not raw:
            continue
        name = raw.decode("utf-8")
        path = Path(name)
        forbidden = (
            path.name == "token.txt"
            or (path.name.startswith(".env") and path.name != ".env.example")
            or any(part.startswith(".venv") or part in {"data", "__pycache__"} for part in path.parts)
            or (path.parts[0] == "secrets" and name != "secrets/README.md")
            or path.suffix in {".sqlite", ".sqlite3", ".pem", ".key", ".zip"}
            or path.name == ".DS_Store"
        )
        if forbidden:
            errors.append(f"Forbidden tracked file: {name}")
        content = (ROOT / path).read_bytes()
        if TOKEN.search(content) or PRIVATE_KEY.search(content):
            errors.append(f"Possible credential in: {name}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Tracked files contain no runtime data or recognizable credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
