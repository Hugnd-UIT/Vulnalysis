from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _parse_line(lines_val: Any) -> Optional[int]:
    if lines_val is None:
        return None
    if isinstance(lines_val, int):
        return lines_val
    s = str(lines_val).strip()
    if not s:
        return None
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else None


def _format_src(leaf: dict) -> dict:
    lbl = str(leaf.get("label") or leaf.get("id") or "").replace("()", "").strip()
    is_external = (
        leaf.get("type") in ("external", "external_call")
        or not leaf.get("file")
        or leaf.get("role") == "external"
    )
    role = "external" if is_external else "internal"
    file_val = None if is_external else str(leaf.get("file") or leaf.get("source_file") or "")
    line_val = None if is_external else _parse_line(leaf.get("lines") or leaf.get("source_location"))

    return {
        "id": str(leaf.get("id") or ""),
        "func": lbl,
        "file": file_val,
        "line": line_val,
        "role": role,
    }


def _format_dst(call_site: dict, leaf: dict, parent_node: Optional[dict] = None) -> dict:
    file_val = str(call_site.get("file") or "").strip()
    line_val = _parse_line(call_site.get("lines"))

    if not file_val and parent_node:
        file_val = str(parent_node.get("file") or "").strip()
        line_val = _parse_line(parent_node.get("lines"))

    if not file_val:
        file_val = str(leaf.get("file") or "").strip()
        line_val = _parse_line(leaf.get("lines"))

    return {
        "file": file_val or None,
        "line": line_val,
    }


def load_paths(source: Union[str, Path, List[dict], dict]) -> List[dict]:
    if isinstance(source, list):
        return source

    if isinstance(source, dict):
        if "flows" in source and isinstance(source["flows"], list):
            return source["flows"]
        if "nodes" in source and ("edges" in source or "links" in source):
            from graphify.src.flatten import flatten
            return flatten(source)
        return []

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []

    if content.startswith("["):
        return json.loads(content)

    paths = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            paths.append(json.loads(line))
        except Exception:
            pass
    return paths


def combine(
    source: Union[str, Path, List[dict], dict],
    dest: Optional[Union[str, Path]] = None,
    fmt: str = "jsonl"
) -> List[dict]:
    raw_paths = load_paths(source)
    grouped: Dict[tuple, dict] = {}

    for p in raw_paths:
        leaf = p.get("leaf") or {}
        call_site = p.get("call_site") or {}
        nodes = p.get("nodes") or []
        parent_node = nodes[-2] if len(nodes) >= 2 else None

        src_info = _format_src(leaf)
        dst_info = _format_dst(call_site, leaf, parent_node)

        group_key = (src_info["id"], dst_info["file"], dst_info["line"])

        if group_key not in grouped:
            grouped[group_key] = {
                "src": src_info,
                "dst": dst_info,
                "paths": []
            }

        chain = p.get("chain", [])
        if isinstance(chain, list):
            chain_list = [str(x) for x in chain]
            if chain_list and chain_list not in grouped[group_key]["paths"]:
                grouped[group_key]["paths"].append(chain_list)

    combined_list = list(grouped.values())

    if dest:
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            if fmt == "jsonl":
                for item in combined_list:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            elif fmt == "json":
                json.dump(combined_list, f, indent=2, ensure_ascii=False)

    return combined_list


def main():
    parser = argparse.ArgumentParser(description="Combine flattened call flows by definition (src) and call-site (dst)")
    parser.add_argument("source", nargs="?", default="outputs/call-flows.jsonl", help="Input paths (jsonl/json)")
    parser.add_argument("-o", "--output", default="outputs/call-flows.jsonl", help="Output destination")
    parser.add_argument("--format", choices=["jsonl", "json"], default="jsonl", help="Output format")

    args = parser.parse_args()

    try:
        combined = combine(
            source=args.source,
            dest=args.output,
            fmt=args.format
        )
        print(f"[OK] Combined {len(combined)} flows into {args.output}")
    except Exception as exc:
        print(f"[Error] Failed to combine flows: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
