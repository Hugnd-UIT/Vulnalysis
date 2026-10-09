from __future__ import annotations

import hashlib
import re
from typing import Any

from graphify.security import metadata as clean_metadata


def istrue(val: object) -> bool:
    return val is True


def coerce(val: object, default: str) -> str:
    if isinstance(val, str):
        return val

    if isinstance(default, str):
        return default

    return ''


def filetype(kind: str) -> str:
    _ = kind

    return 'code'


def line(occs: object) -> int:
    if not isinstance(occs, list) or not occs:
        return 0

    first = occs[0]

    if not isinstance(first, dict):
        return 0

    rng = first.get('range', [])

    if not isinstance(rng, list) or len(rng) < 1:
        return 0

    num = rng[0]

    if isinstance(num, bool) or not isinstance(num, int) or num < 0:
        return 0

    return num


def nid(symbol: str, loc: str) -> str:
    raw = f'{loc}:{symbol}'
    h = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]
    parts = symbol.split('#')
    tail = parts[-1] if parts else symbol
    tail = re.sub(r'[^a-zA-Z0-9_]', '_', tail).strip('_').lower()

    if tail:
        return f'scip_{tail}_{h}'

    return f'scip_{h}'


def meta(sid: str, kind: str, desc: str) -> dict[str, str]:
    data: dict[str, str] = {'scip_symbol': sid, 'scip_kind': kind}

    if desc:
        data['scip_description'] = desc

    return data


def relation(rel: dict[str, Any]) -> str:
    if istrue(rel.get('is_implementation')):
        return 'scip_impl'

    if istrue(rel.get('is_type_definition')):
        return 'scip_typed'

    if istrue(rel.get('is_definition')):
        return 'scip_def'

    return 'scip_ref'


def target(sym: str, loc: str, local: dict[tuple[str, str], str], global_idx: dict[str, list[str]]) -> str | None:
    hit = local.get((sym, loc))

    if hit is not None:
        return hit

    cands = global_idx.get(sym, [])

    if len(cands) == 1:
        return cands[0]

    return None


def node(rec: dict[str, Any], nodes: list[dict[str, Any]], seen: set[str]) -> None:
    sid = rec['node_id']

    if sid in seen:
        return

    raw = rec['raw']
    sym = rec['symbol_id']
    loc = rec['doc_path']
    kind = coerce(raw.get('kind'), 'unknown')
    display = coerce(raw.get('display_name'), '')
    docs = raw.get('documentation', [])
    desc = docs[0] if isinstance(docs, list) and docs and isinstance(docs[0], str) else ''
    lineno = line(raw.get('occurrences', []))
    tail = sym.split('#')[-1] if '#' in sym else sym
    label = display or tail or sym

    seen.add(sid)
    nodes.append({
        'id': sid,
        'label': label,
        'file_type': filetype(kind),
        'source_file': loc,
        'source_location': f'L{lineno}' if lineno else '',
        'metadata': clean_metadata(meta(sym, kind, desc)),
    })


def relationships(rec: dict[str, Any], local: dict[tuple[str, str], str], global_idx: dict[str, list[str]], nodes: list[dict[str, Any]], edges: list[dict[str, Any]], snodes: set[str], sedges: set[tuple[str, str, str, str | None]]) -> None:
    raw = rec['raw']
    src = rec['node_id']
    loc = rec['doc_path']
    lineno = line(raw.get('occurrences', []))
    rels = raw.get('relationships')

    if not isinstance(rels, list):
        return

    for item in rels:
        if not isinstance(item, dict):
            continue

        sym = coerce(item.get('symbol'), '')

        if not sym:
            continue

        dst = target(sym, loc, local, global_idx)

        if dst is None:
            dst = nid(sym, loc)

            if dst not in snodes:
                snodes.add(dst)
                tail = sym.split('#')[-1] if '#' in sym else sym
                nodes.append({
                    'id': dst,
                    'label': tail or sym,
                    'file_type': 'code',
                    'source_file': loc,
                    'source_location': '',
                    'metadata': clean_metadata(meta(sym, 'external', '')),
                })

        rel = relation(item)
        pos = f'L{lineno}' if lineno else ''
        key = (src, dst, rel, pos)

        if key in sedges:
            continue

        sedges.add(key)
        edges.append({
            'source': src,
            'target': dst,
            'relation': rel,
            'source_file': loc,
            'source_location': pos,
            'context': 'scip',
            'metadata': clean_metadata({'scip_relationship': item}),
        })


def ingest(doc: object, src: str = '', lang: str = 'python') -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    snodes: set[str] = set()
    sedges: set[tuple[str, str, str, str | None]] = set()

    if not isinstance(doc, dict):
        return {'nodes': nodes, 'edges': edges}

    docs = doc.get('documents', [])

    if not isinstance(docs, list):
        return {'nodes': nodes, 'edges': edges}

    local: dict[tuple[str, str], str] = {}
    global_idx: dict[str, list[str]] = {}
    recs: list[dict[str, Any]] = []

    for item in docs:
        if not isinstance(item, dict):
            continue

        path = coerce(item.get('relative_path'), src)
        langname = coerce(item.get('language'), lang)
        syms = item.get('symbols', [])

        if not isinstance(syms, list):
            continue

        for sym in syms:
            if not isinstance(sym, dict):
                continue

            sid = coerce(sym.get('symbol'), '')

            if not sid:
                continue

            node_id = nid(sid, path)
            local.setdefault((sid, path), node_id)
            cands = global_idx.setdefault(sid, [])

            if node_id not in cands:
                cands.append(node_id)

            recs.append({
                'node_id': node_id,
                'symbol_id': sid,
                'doc_path': path,
                'language': langname,
                'raw': sym,
            })

    for rec in recs:
        node(rec, nodes, snodes)
        relationships(rec, local, global_idx, nodes, edges, snodes, sedges)

    return {'nodes': nodes, 'edges': edges}