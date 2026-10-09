from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import json as jsonlib
from pathlib import Path
import re
from typing import Any

import networkx as nx


SUPPRESSION_RE = re.compile(r'^\s*(?P<name>seen_[A-Za-z0-9_]+)\s*[:=]')
TUPLE_RE = re.compile(r'set\[tuple\[(?P<inside>[^\]]+)\]\]')


def text(val: Any) -> str:
    if val is None:
        return ''

    if isinstance(val, (str, int, float, bool)):
        return str(val)

    return jsonlib.dumps(val, sort_keys=True, default=str, ensure_ascii=False)


def edges(data: dict[str, Any]) -> list[Any]:
    res = data.get('edges')

    if res is None:
        res = data.get('links')

    return res if isinstance(res, list) else []


def nodes(data: dict[str, Any]) -> set[str]:
    res = data.get('nodes', [])

    if not isinstance(res, list):
        return set()

    return {str(n['id']) for n in res if isinstance(n, dict) and 'id' in n and (n.get('id') is not None)}


def canonical(edge: Any) -> dict[str, str]:
    if not isinstance(edge, dict):
        return {'source': '', 'target': '', 'relation': '', 'source_file': '', 'source_location': '', 'context': '', '_invalid': 'non_object_edge'}

    src = edge.get('source', edge.get('from'))
    dst = edge.get('target', edge.get('to'))

    return {

        'source': text(src),
        'target': text(dst),
        'relation': text(edge.get('relation')),
        'source_file': text(edge.get('source_file')),
        'source_location': text(edge.get('source_location')),
        'context': text(edge.get('context')),
        '_invalid': '',
    }


def signature(edge: Any) -> str:
    if not isinstance(edge, dict):
        return '<non-object>'

    item = dict(edge)

    if 'source' not in item and 'from' in item:
        item['source'] = item['from']

    if 'target' not in item and 'to' in item:
        item['target'] = item['to']

    item.pop('from', None)
    item.pop('to', None)

    return jsonlib.dumps(item, sort_keys=True, default=str, ensure_ascii=False, separators=(',', ':'))


def extra(counts: Counter[Any]) -> int:
    return sum((c - 1 for c in counts.values() if c > 1))


def variants(grouped: dict[tuple[str, str], list[dict[str, str]]], field: str, *, sensitive: bool = False) -> int:
    groups = 0

    for items in grouped.values():
        if sensitive:
            by_rel: dict[str, set[str]] = defaultdict(set)

            for e in items:
                by_rel[e['relation']].add(e[field])

            groups += sum((1 for vals in by_rel.values() if len(vals) > 1))
        elif len({e[field] for e in items}) > 1:
            groups += 1

    return groups


def arity(line: str) -> int:
    m = TUPLE_RE.search(line)

    if not m:
        return 0

    inside = m.group('inside').strip()

    if not inside:
        return 0

    return inside.count(',') + 1


def scan(loc: str | Path) -> dict[str, Any]:
    path = Path(loc)

    if not path.exists():
        return {'path': str(path), 'total_sites': 0, 'sites': [], 'error': 'not found'}

    sites: list[dict[str, Any]] = []
    lines = path.read_text(encoding='utf-8').splitlines()

    for idx, l in enumerate(lines, start=1):
        m = SUPPRESSION_RE.match(l)

        if not m:
            continue

        sites.append({'line': idx, 'name': m.group('name'), 'tuple_arity': arity(l), 'sample': l.strip()[:120]})

    return {'path': str(path), 'total_sites': len(sites), 'sites': sites, 'error': ''}


def diagnose(data: dict[str, Any], *, directed: bool = True, root: str | Path | None = None, max_examples: int = 5, extract_path: str | Path | None = None) -> dict[str, Any]:
    from graphify.build import buildjson

    nids = nodes(data)
    raw = edges(data)
    canon = [canonical(e) for e in raw]
    unverified = sum((1 for n in data.get('nodes', []) if isinstance(n, dict) and n.get('verification') == 'unverified'))
    exacts: Counter[str] = Counter((signature(e) for e in raw))
    dirs: Counter[tuple[str, str]] = Counter()
    undirs: Counter[tuple[str, str]] = Counter()
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    invalid = 0
    missing = 0
    dangling = 0
    refs = 0
    loops = 0
    valid = 0

    for e in canon:
        if e['_invalid']:
            invalid += 1
            continue

        src = e['source']
        dst = e['target']

        if not src or not dst:
            missing += 1
            continue

        if src not in nids or dst not in nids:
            if src in nids and dst.startswith('ref_'):
                refs += 1
            else:
                dangling += 1
            continue

        if src == dst:
            loops += 1

        valid += 1
        d_pair = (src, dst)
        u_pair = (src, dst) if src <= dst else (dst, src)
        dirs[d_pair] += 1
        undirs[u_pair] += 1
        grouped[d_pair].append(e)

    examples: list[dict[str, Any]] = []

    if max_examples > 0:
        for (src, dst), count in dirs.most_common():
            if count < 2:
                continue

            items = grouped[src, dst]
            examples.append({
                'source': src,
                'target': dst,
                'edge_count': count,
                'relations': sorted({item['relation'] for item in items}),
                'source_files': sorted({item['source_file'] for item in items}),
                'source_locations': sorted({item['source_location'] for item in items}),
                'contexts': sorted({item['context'] for item in items}),
            })

            if len(examples) >= max_examples:
                break

    err = ''
    gtype = ''
    post_edges: int | None = None
    post_nodes: int | None = None

    try:
        copy = deepcopy(data)
        g: nx.Graph = buildjson(copy, directed=directed, root=root)
        gtype = type(g).__name__
        post_edges = g.number_of_edges()
        post_nodes = g.number_of_nodes()
    except Exception as exc:
        err = f'{type(exc).__name__}: {exc}'

    suppression = Path(extract_path) if extract_path else Path(__file__).with_name('extract.py')

    return {

        'node_count': len(nids),
        'unverified_node_count': unverified,
        'raw_edge_count': len(raw),
        'non_object_edges': invalid,
        'missing_endpoint_edges': missing,
        'dangling_endpoint_edges': dangling,
        'external_reference_edges': refs,
        'self_loop_edges': loops,
        'valid_candidate_edges': valid,
        'exact_duplicate_edges': extra(exacts),
        'directed_unique_endpoint_pairs': len(dirs),
        'directed_same_endpoint_collapsed_edges': extra(dirs),
        'undirected_unique_endpoint_pairs': len(undirs),
        'undirected_same_endpoint_collapsed_edges': extra(undirs),
        'same_endpoint_group_count': sum((1 for c in dirs.values() if c > 1)),
        'relation_variant_groups': variants(grouped, 'relation'),
        'source_file_variant_groups': variants(grouped, 'source_file', sensitive=True),
        'source_location_variant_groups': variants(grouped, 'source_location', sensitive=True),
        'context_variant_groups': variants(grouped, 'context', sensitive=True),
        'post_build_graph_type': gtype,
        'post_build_node_count': post_nodes,
        'post_build_edge_count': post_edges,
        'post_build_error': err,
        'producer_suppression': scan(suppression),
        'examples': examples,
    }


def read(loc: str | Path) -> dict[str, Any]:
    from graphify.security import check

    p = Path(loc)
    check(p)

    try:
        data = jsonlib.loads(p.read_text(encoding='utf-8'))
    except (jsonlib.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f'Cannot parse {p}: {exc}') from exc

    if not isinstance(data, dict):
        raise ValueError('Must be JSON object')

    return data


def inspect(loc: str | Path, *, directed: bool | None = None, root: str | Path | None = None, max_examples: int = 5, extract_path: str | Path | None = None) -> dict[str, Any]:
    data = read(loc)

    if directed is None:
        raw = data.get('directed')
        eff = raw if isinstance(raw, bool) else True
    else:
        eff = directed

    summary = diagnose(data, directed=eff, root=root, max_examples=max_examples, extract_path=extract_path)
    summary['input_path'] = str(loc)
    summary['effective_directed'] = eff

    return summary


def json(summary: dict[str, Any]) -> dict[str, Any]:
    return {

        'schema_version': 1,
        'summary': {k: v for k, v in summary.items() if k not in {'examples', 'producer_suppression'}},
        'examples': summary.get('examples', []),
        'producer_suppression': summary.get('producer_suppression', {}),
    }


def report(summary: dict[str, Any]) -> str:
    supp = summary.get('producer_suppression', {})
    lines = [
        f"nodes: {summary['node_count']}",
        f"edges: {summary['raw_edge_count']}",
        f"valid: {summary['valid_candidate_edges']}",
        f"collapsed: {summary['directed_same_endpoint_collapsed_edges']}",
    ]

    return '\n'.join(lines)