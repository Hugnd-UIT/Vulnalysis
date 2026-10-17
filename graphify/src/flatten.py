from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

RELATIONS = frozenset({'calls', 'indirect_call'})


def load(source: str | Path | dict) -> dict:
    if isinstance(source, dict):
        return source

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"not found: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def build(data: dict) -> Tuple[Dict[str, dict], Dict[str, List[dict]], Set[str]]:
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("edges") or data.get("links") or []

    nodes: Dict[str, dict] = {}
    for n in raw_nodes:
        nid = str(n.get("id") or "")
        if not nid:
            continue
        nodes[nid] = {
            "id": nid,
            "label": str(n.get("label") or nid),
            "type": str(n.get("type") or "code"),
            "file": str(n.get("file") or n.get("source_file") or ""),
            "lines": str(n.get("lines") or n.get("source_location") or "")
        }

    adj: Dict[str, List[dict]] = defaultdict(list)
    deg: Dict[str, int] = defaultdict(int)
    callers: Set[str] = set()

    for e in raw_edges:
        rel = str(e.get("relation") or "")
        if rel not in RELATIONS:
            continue

        src = str(e.get("source") or e.get("_src") or "")
        tgt = str(e.get("target") or e.get("_tgt") or "")
        if not src or not tgt:
            continue

        if tgt not in nodes:
            callee = str(e.get("callee") or tgt)
            nodes[tgt] = {
                "id": tgt,
                "label": f"{callee}()",
                "type": "external_call",
                "file": str(e.get("file") or ""),
                "lines": str(e.get("lines") or "")
            }

        if src not in nodes:
            nodes[src] = {
                "id": src,
                "label": src,
                "type": "code",
                "file": str(e.get("file") or ""),
                "lines": str(e.get("lines") or "")
            }

        edge = {
            "target": tgt,
            "callee": str(e.get("callee") or ""),
            "file": str(e.get("file") or ""),
            "lines": str(e.get("lines") or "")
        }
        adj[src].append(edge)
        deg[tgt] += 1
        callers.add(src)

    roots: Set[str] = set()
    for src in callers:
        name = nodes[src]["label"].lower()
        if deg[src] == 0 or any(k in name for k in ("main", "handler", "entry")):
            roots.add(src)

    if not roots and callers:
        roots = set(callers)

    return nodes, adj, roots


def walk(
    nodes: Dict[str, dict],
    adj: Dict[str, List[dict]],
    roots: Set[str],
    limit: int | None = None
) -> List[dict]:
    paths: List[dict] = []
    seen: Set[str] = set()

    def record(stack: List[str], cycle: bool = False):
        if len(stack) < 2:
            return

        sig = " -> ".join(stack)
        if sig in seen:
            return
        seen.add(sig)
        def format_node(nid):
            n = nodes.get(nid, {})
            f = n.get("file", "")
            lbl = n.get("label", nid).replace("()", "").strip()
            return f"{f}:{lbl}" if f else lbl

        names = [format_node(nid) for nid in stack]
        root_id = stack[0]
        leaf_id = stack[-1]

        item = {
            "id": f"P{len(paths) + 1:04d}",
            "len": len(stack),
            "root": nodes.get(root_id),
            "leaf": nodes.get(leaf_id),
            "chain": names,
            "nodes": [nodes.get(nid) for nid in stack],
            "cycle": cycle
        }

        paths.append(item)
        bucket.append(item)

    def dfs(curr: str, stack: List[str], active: Set[str]):
        if limit is not None and len(bucket) >= limit:
            return

        children = adj.get(curr, [])
        if not children:
            record(stack, cycle=False)
            return

        valid = False
        for edge in children:
            tgt = edge["target"]

            if tgt in active:
                record(stack + [tgt], cycle=True)
                valid = True
                continue

            valid = True
            active.add(tgt)
            stack.append(tgt)

            dfs(tgt, stack, active)

            stack.pop()
            active.remove(tgt)

        if not valid:
            record(stack, cycle=False)

    for root in sorted(roots):
        bucket: List[dict] = []
        dfs(root, [root], {root})

    return paths


def flatten(
    source: str | Path | dict,
    dest: str | Path | None = None,
    limit: int | None = None,
    fmt: str = "jsonl"
) -> List[dict]:
    data = load(source)
    nodes, adj, roots = build(data)
    paths = walk(nodes, adj, roots, limit=limit)

    if dest:
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)

        with open(out, "w", encoding="utf-8") as f:
            if fmt == "jsonl":
                for p in paths:
                    f.write(json.dumps(p, ensure_ascii=False) + "\n")
            elif fmt == "json":
                json.dump(paths, f, indent=2, ensure_ascii=False)
            elif fmt == "txt":
                for p in paths:
                    tag = " [CYCLE]" if p.get("cycle") else ""
                    chain_str = " -> ".join(p.get("chain", []))
                    f.write(f"{p['id']}: {chain_str}{tag}\n")

    return paths


def main():
    parser = argparse.ArgumentParser(description="Flatten call graph into sequential paths")
    parser.add_argument("source", nargs="?", default="outputs/call-graph.json", help="Input call graph path")
    parser.add_argument("-o", "--output", default="outputs/call-flows.jsonl", help="Output path")
    parser.add_argument("--format", choices=["jsonl", "json", "txt"], default="jsonl", help="Output format")
    parser.add_argument("--limit", type=int, default=None, help="Max paths per root")

    args = parser.parse_args()

    try:
        paths = flatten(
            source=args.source,
            dest=args.output,
            limit=args.limit,
            fmt=args.format
        )
        print(f"[OK] Flattened {len(paths)} paths into {args.output}")
    except Exception as exc:
        print(f"[Error] Failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
