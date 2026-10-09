from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph as jg

DIR = Path.home() / '.graphify'
GRAPH = DIR / 'global-graph.json'
MANIFEST = DIR / 'global-manifest.json'


def manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding='utf-8'))
        except Exception as exc:
            backup = MANIFEST.with_suffix(MANIFEST.suffix + f'.corrupt.{int(datetime.now(timezone.utc).timestamp())}')

            try:
                MANIFEST.rename(backup)
                print(f'[graphify global] manifest failed ({exc}); moved to {backup}', file=sys.stderr)
            except Exception as rename_exc:
                print(f'[graphify global] manifest backup failed ({rename_exc})', file=sys.stderr)

    return {'version': 1, 'repos': {}}


def record(data: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    from graphify.paths import json as write_json
    write_json(MANIFEST, data, indent=2)


def graph() -> nx.Graph:
    if GRAPH.exists():
        from graphify.security import check
        check(GRAPH)

        data = json.loads(GRAPH.read_text(encoding='utf-8'))

        if 'links' not in data and 'edges' in data:
            data = dict(data, links=data['edges'])

        try:
            return jg.node_link_graph(data, edges='links')
        except TypeError:
            return jg.node_link_graph(data)

    return nx.Graph()


def save(item: nx.Graph) -> None:
    DIR.mkdir(parents=True, exist_ok=True)

    try:
        data = jg.node_link_data(item, edges='links')
    except TypeError:
        data = jg.node_link_data(item)

    from graphify.paths import json as write_json
    write_json(GRAPH, data, indent=2)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())

    return h.hexdigest()[:16]


def add(source: Path, tag: str) -> dict:
    from graphify.build import prefix, prunerepo

    if not source.exists():
        raise FileNotFoundError(f'graph not found: {source}')

    data = manifest()
    current = digest(source)
    existing = data['repos'].get(tag, {})
    prior = existing.get('source_path', '')

    if prior and prior != str(source.resolve()):
        print(f"[graphify global] warning: repo tag '{tag}' updated from {prior!r} to {str(source.resolve())!r}.", file=sys.stderr)

    if existing.get('source_hash') == current:
        return {'repo_tag': tag, 'nodes_added': 0, 'nodes_removed': 0, 'skipped': True, 'cross_repo_calls': 0}

    from graphify.security import check
    check(source)

    raw = json.loads(source.read_text(encoding='utf-8'))

    if 'links' not in raw and 'edges' in raw:
        raw = dict(raw, links=raw['edges'])

    try:
        src = jg.node_link_graph(raw, edges='links')
    except TypeError:
        src = jg.node_link_graph(raw)

    prefixed = prefix(src, tag)
    target = graph()
    removed = prunerepo(target, tag)

    external = {d.get('label', ''): n for n, d in target.nodes(data=True) if not d.get('source_file') and d.get('label')}
    remap = {}

    for node, item in prefixed.nodes(data=True):
        if not item.get('source_file') and item.get('label') in external:
            remap[node] = external[item['label']]

    for node, item in prefixed.nodes(data=True):
        if node not in remap:
            target.add_node(node, **item)

    for u, v, item in prefixed.edges(data=True):
        u = remap.get(u, u)
        v = remap.get(v, v)

        if u != v:
            target.add_edge(u, v, **item)

    added = prefixed.number_of_nodes() - len(remap)

    from graphify.calls import link
    calls = link(target)

    save(target)

    data['repos'][tag] = {
        'added_at': datetime.now(timezone.utc).isoformat(),
        'source_path': str(source.resolve()),
        'node_count': added,
        'edge_count': prefixed.number_of_edges(),
        'source_hash': current,
    }
    record(data)

    return {'repo_tag': tag, 'nodes_added': added, 'nodes_removed': removed, 'skipped': False, 'cross_repo_calls': calls}


def remove(tag: str) -> int:
    from graphify.build import prunerepo

    data = manifest()

    if tag not in data['repos']:
        raise KeyError(f"repo '{tag}' not in global graph")

    target = graph()
    removed = prunerepo(target, tag)
    save(target)

    del data['repos'][tag]
    record(data)

    return removed


def repos() -> dict:
    return manifest().get('repos', {})


def path() -> Path:
    return GRAPH