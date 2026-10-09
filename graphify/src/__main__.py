from __future__ import annotations

import json
from pathlib import Path
import sys

from graphify.extract import collect, extract


def run(target: str, out: str | None = None) -> dict:
    path = Path(target).resolve()

    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = collect(path)
    else:
        print(f"error: {path} not found", file=sys.stderr)
        sys.exit(1)

    result = extract(files)

    if out:
        dest = Path(out).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')

    return result


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m graphify <target> [output.json]")
        sys.exit(1)

    target = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None

    result = run(target, out)
    nodes = result.get('nodes', [])
    edges = result.get('edges', [])

    print(f"nodes: {len(nodes)}, edges: {len(edges)}")


if __name__ == '__main__':
    main()