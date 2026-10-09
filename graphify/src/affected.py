from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable
import unicodedata

import networkx as nx
from networkx.readwrite import json_graph


RELATIONS = ('calls', 'indirect_call', 'references', 'imports', 'imports_from', 'dynamic_import', 're_exports', 'inherits', 'extends', 'implements', 'uses', 'mixes_in', 'embeds', 'requires')


@dataclass(frozen=True)


class Hit:
    node: str
    depth: int
    relation: str
    file: str | None = None
    location: str | None = None


def label(graph: nx.Graph, nid: str) -> str:
    data = graph.nodes[nid]

    return str(data.get('label') or nid)


def location(data: dict) -> str:
    file = data.get('source_file') or '-'
    loc = data.get('source_location')

    if loc:
        return f'{file}:{loc}'

    return str(file)


def norm(text: str) -> str:
    return unicodedata.normalize('NFC', text).casefold()


def bare(text: str) -> str:
    val = norm(text)

    return val[:-2] if val.endswith('()') else val


def relative(query: str, root: Path | None = None) -> str:
    path = Path(query)

    if path.is_absolute():
        anchor = root if root is not None else Path.cwd()

        try:
            return path.relative_to(anchor).as_posix()
        except ValueError:
            return query

    return path.as_posix()


def prefer(graph: nx.Graph, nids: list[str], query: str) -> str | None:
    base = norm(Path(query).name)
    exact = [nid for nid in nids if str(graph.nodes[nid].get('source_location', '')) == 'L1' and norm(str(graph.nodes[nid].get('label', ''))) == base]

    if len(exact) == 1:
        return exact[0]

    l1 = [nid for nid in nids if str(graph.nodes[nid].get('source_location', '')) == 'L1']

    if len(l1) == 1:
        return l1[0]

    matches = [nid for nid in nids if norm(str(graph.nodes[nid].get('label', ''))) == base]

    if len(matches) == 1:
        return matches[0]

    return None


def seed(graph: nx.Graph, query: str, root: Path | None = None) -> str | None:
    query = query.rstrip('/\\') or query

    if query in graph:
        return query

    low = norm(query)
    exact = [str(nid) for nid, data in graph.nodes(data=True) if norm(str(data.get('label', ''))) == low]

    if len(exact) == 1:
        return exact[0]

    bname = bare(low)
    bare_matches = [str(nid) for nid, data in graph.nodes(data=True) if bare(str(data.get('label', ''))) == bname]

    if len(bare_matches) == 1:
        return bare_matches[0]

    path = norm(relative(query, root))
    src_matches = [str(nid) for nid, data in graph.nodes(data=True) if norm(str(data.get('source_file', ''))) in (low, path)]

    if len(src_matches) == 1:
        return src_matches[0]

    if src_matches:
        preferred = prefer(graph, src_matches, relative(query, root))

        if preferred is not None:
            return preferred

    contains = [str(nid) for nid, data in graph.nodes(data=True) if low in norm(str(data.get('label', '')))]

    if len(contains) == 1:
        return contains[0]

    return None


def nodes(graph: nx.Graph, src: str, *, relations: Iterable[str] = RELATIONS, depth: int = 2) -> list[Hit]:
    valid = set(relations)
    seen = {src}
    queue: deque[tuple[str, int]] = deque([(src, 0)])
    hits: list[Hit] = []

    if hasattr(graph, 'out_edges'):
        member_edges = graph.out_edges(src, data=True)
    else:
        member_edges = ((s, t, d) for s, t, d in graph.edges(data=True) if s == src)

    for _s, member, data in member_edges:
        if str(data.get('relation', '')) not in ('method', 'contains'):
            continue

        item = str(member)

        if item not in seen:
            seen.add(item)
            queue.append((item, 0))

    while queue:
        current, cur_depth = queue.popleft()

        if cur_depth >= depth:
            continue

        if hasattr(graph, 'in_edges'):
            incoming = graph.in_edges(current, data=True)
        else:
            incoming = ((s, t, d) for s, t, d in graph.edges(data=True) if t == current)

        for source, _target, data in incoming:
            rel = str(data.get('relation', ''))

            if rel not in valid:
                continue

            src_nid = str(source)

            if src_nid in seen:
                continue

            seen.add(src_nid)
            hit = Hit(src_nid, cur_depth + 1, rel, file=str(data.get('source_file') or '') or None, location=str(data.get('source_location') or '') or None)
            hits.append(hit)
            queue.append((src_nid, cur_depth + 1))

    return hits


def format(graph: nx.Graph, query: str, *, relations: Iterable[str] = RELATIONS, depth: int = 2, root: Path | None = None) -> str:
    rellist = tuple(relations)
    src = seed(graph, query, root)

    if src is None:
        return f'No match for {query}'

    results = nodes(graph, src, relations=rellist, depth=depth)
    lines = [f'Affected: {label(graph, src)}', f"Relations: {', '.join(rellist)}", f'Depth: {depth}']

    if not results:
        lines.append('No affected nodes.')

        return '\n'.join(lines)

    for h in results:
        data = graph.nodes[h.node]

        if h.location:
            loc = f"{h.file or data.get('source_file') or '-'}:{h.location}"
        else:
            loc = location(data)

        lines.append(f'- {label(graph, h.node)} [{h.relation}] {loc}')

    return '\n'.join(lines)


def load(loc: Path) -> nx.Graph:
    try:
        raw = json.loads(loc.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f'Cannot read graph {loc}: {exc}') from exc

    raw = {**raw, 'directed': True}

    if 'links' not in raw and 'edges' in raw:
        raw = dict(raw, links=raw['edges'])

    try:
        return json_graph.node_link_graph(raw, edges='links')
    except TypeError:
        return json_graph.node_link_graph(raw)