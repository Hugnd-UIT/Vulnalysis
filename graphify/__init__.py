from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys


root = str(Path(__file__).resolve().parent.parent)

if root not in sys.path:
    sys.path.insert(0, root)

src = str(Path(__file__).resolve().parent / 'src')

if src not in sys.path:
    sys.path.append(src)

if '__path__' in globals():
    __path__.append(src)
else:
    __path__ = [src]


def __getattr__(name: str):
    targets = {
        'extract': ('graphify.extract', 'extract'),
        'collect': ('graphify.extract', 'collect'),
        'build': ('graphify.build', 'build'),
        'cluster': ('graphify.cluster', 'cluster'),
        'scores': ('graphify.cluster', 'scores'),
        'god': ('graphify.analyze', 'god'),
        'json': ('graphify.export', 'json'),
    }

    if name in targets:
        mod, attr = targets[name]

        return getattr(importlib.import_module(mod), attr)

    raise AttributeError(f"module 'graphify' has no attribute {name!r}")


def traverse(loc: Path, root: Path | None = None) -> tuple[list[dict], list[dict]]:
    from graphify.extract import extract

    res = extract([loc], root=root)

    return (res.get('nodes', []), res.get('edges', []))


def callgraph(target: str | Path, output: str | Path | None = None) -> dict:
    from graphify.extract import collect, extract

    loc = Path(target).resolve()
    base = loc.parent if loc.is_file() else loc

    if loc.is_file():
        files = [loc]
    elif loc.is_dir():
        files = [p for p in collect(loc) if p.suffix.lower() not in ('.md', '.mdx', '.qmd', '.txt', '.rst', '.json')]
    else:
        raise FileNotFoundError(f'Target path not found: {loc}')

    res = extract(files, root=base)
    nodes = []
    edges = []
    seen = set()

    for n in res.get('nodes', []):
        nid = n.get('id')

        if nid and nid not in seen:
            seen.add(nid)
            nodes.append({
                'id': nid,
                'label': n.get('label', ''),
                'type': n.get('file_type', n.get('type', 'code')),
                'file': n.get('source_file', n.get('file', '')),
                'lines': n.get('source_location', n.get('lines', '')),
            })

    for e in res.get('edges', []):
        src = e.get('source')
        tgt = e.get('target')

        if src and tgt:
            edges.append({
                'source': src,
                'target': tgt,
                'callee': e.get('callee') or tgt.split('_')[-1],
                'relation': e.get('relation', 'calls'),
                'file': e.get('source_file', e.get('file', '')),
                'lines': e.get('source_location', e.get('lines', '')),
            })

    result = {'nodes': nodes, 'edges': edges}

    if output:
        dest = Path(output).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')

    return result


if __name__ == '__main__':
    args = sys.argv[1:]

    if not args:
        print('Usage: python graphify/__init__.py <target> [output.json]')
        sys.exit(1)

    target = args[0]
    out = args[1] if len(args) > 1 else 'call-graph.json'
    res = callgraph(target, out)
    nodes = res.get('nodes', [])
    edges = res.get('edges', [])
    calls = [e for e in edges if e.get('relation') == 'calls']
    print(f'[+] Done! Nodes: {len(nodes)}, Edges: {len(edges)}, Function calls: {len(calls)}')