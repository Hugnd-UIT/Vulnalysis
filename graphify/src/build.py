from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata
from collections.abc import Iterable

import networkx as nx

from .dedup import dedupe
from .ids import make, normalize
from .paths import absolute, default
from .validate import validate


AST = re.compile('^L\\d')


def isasttier(item: dict) -> bool:
    loc = item.get('source_location')

    return isinstance(loc, str) and bool(AST.match(loc))


def backfill(item: dict) -> None:
    item.pop('_origin', None)


GENERIC: frozenset[str] = frozenset({'references', 'uses', 'mentions'})
STUBS: frozenset[str] = frozenset({'imports', 'imports_from', 're_exports'})


def mint(graph: nx.Graph, known: set, nid: str) -> None:
    if nid in known:
        return

    graph.add_node(nid, label=nid, file_type='concept', type='external', external=True, source_file='')
    known.add(nid)


def stub(item: dict) -> bool:
    return item.get('external') is True and item.get('type') == 'external' and (not item.get('source_file'))


def stubs(data: dict) -> None:
    nodes = data.get('nodes')
    links = data.get('links')

    if not isinstance(links, list):
        links = data.get('edges')

    if not isinstance(nodes, list) or not isinstance(links, list):
        return

    declared = {node.get('id') for node in nodes if isinstance(node, dict)}
    minted: set[str] = set()

    for item in links:
        if not isinstance(item, dict) or item.get('relation') not in STUBS:
            continue

        src, tgt = item.get('source'), item.get('target')

        if src in declared and isinstance(tgt, str) and tgt not in declared and tgt not in minted:
            nodes.append({'id': tgt, 'label': tgt, 'file_type': 'concept', 'type': 'external', 'external': True, 'source_file': ''})
            minted.add(tgt)


def finalize(data: dict) -> None:
    stubs(data)
    nodes = data.get('nodes')
    key = 'links' if isinstance(data.get('links'), list) else 'edges'
    links = data.get(key)

    if not isinstance(nodes, list) or not isinstance(links, list):
        return

    declared = {node.get('id') for node in nodes if isinstance(node, dict)}
    canon = {normalize(nid): nid for nid in declared if isinstance(nid, str)}
    kept: list = []
    referenced: set = set()

    for item in links:
        if not isinstance(item, dict):
            kept.append(item)

            continue

        attached = True

        for primary, alias in (('source', 'from'), ('target', 'to')):
            field = primary if primary in item else alias
            point = item.get(field)

            if isinstance(point, str) and point in declared:
                continue

            target = canon.get(normalize(point)) if isinstance(point, str) else None

            if target is None:
                attached = False

                break

            item[field] = target

        if attached:
            kept.append(item)
            referenced.add(item.get('source', item.get('from')))
            referenced.add(item.get('target', item.get('to')))

    links[:] = kept

    for hyperedge in data.get('hyperedges', []) or []:
        if isinstance(hyperedge, dict):
            members = hyperedge.get('nodes', hyperedge.get('members', hyperedge.get('node_ids', [])))

            if isinstance(members, list):
                referenced.update((member for member in members if isinstance(member, str)))

    nodes[:] = [node for node in nodes if not (isinstance(node, dict) and node.get('external') is True and node.get('type') == 'external' and (not node.get('source_file')) and node.get('id') not in referenced)]


FAMILIES: dict[str, str] = {'.py': 'py', '.pyi': 'py', '.js': 'js', '.mjs': 'js', '.cjs': 'js', '.jsx': 'js', '.ts': 'js', '.tsx': 'js', '.mts': 'js', '.cts': 'js', '.go': 'go', '.rs': 'rs', '.java': 'jvm', '.kt': 'jvm', '.scala': 'jvm', '.groovy': 'jvm', '.c': 'c', '.h': 'c', '.cc': 'c', '.cpp': 'c', '.hpp': 'c', '.cxx': 'c', '.hh': 'c', '.hxx': 'c', '.cu': 'c', '.cuh': 'c', '.metal': 'c', '.m': 'c', '.mm': 'c', '.rb': 'rb', '.rake': 'rb', '.php': 'php', '.cs': 'cs', '.swift': 'swift', '.lua': 'lua'}
SYNONYMS = {'markdown': 'document', 'text': 'document', 'tool': 'code', 'library': 'code', 'pattern': 'concept', 'principle': 'concept', 'constraint': 'concept', 'tech': 'concept', 'technology': 'concept', 'data-source': 'concept', 'data_source': 'concept', 'gotcha': 'concept', 'framework': 'concept'}
ALIASES = ('members', 'node_ids')


def coerce(hyperedge: dict, members: list) -> list:
    seen: set = set()
    coerced: list = []

    for ref in members:
        if isinstance(ref, dict):
            inner = coerceid(ref.get('id'))

            if inner in (None, '') or not hashable(inner):
                print(f"[graphify] WARNING: hyperedge '{hyperedge.get('id', '?')}' has a member object with no usable 'id' ({ref!r}); dropping that member.", file=sys.stderr)

                continue

            ref = inner
        elif not hashable(ref):
            print(f"[graphify] WARNING: hyperedge '{hyperedge.get('id', '?')}' has an unusable member reference {ref!r}; dropping that member.", file=sys.stderr)

            continue

        if ref in seen:
            continue

        seen.add(ref)
        coerced.append(ref)

    return coerced


def hyperedges(hyperedge: object) -> None:
    if not isinstance(hyperedge, dict):
        return

    if isinstance(hyperedge.get('nodes'), list):
        hyperedge['nodes'] = coerce(hyperedge, hyperedge['nodes'])
    else:
        for alias in ALIASES:
            val = hyperedge.get(alias)

            if isinstance(val, list):
                hyperedge['nodes'] = coerce(hyperedge, val)
                print(f"[graphify] WARNING: hyperedge '{hyperedge.get('id', '?')}' uses field '{alias}' instead of 'nodes'; normalizing.", file=sys.stderr)

                break

    for alias in ALIASES:
        hyperedge.pop(alias, None)


def foldnodes(node: dict) -> None:
    if not node.get('label') and isinstance(node.get('name'), str) and node['name']:
        node['label'] = node.pop('name')

    if not node.get('source_file') and isinstance(node.get('path'), str) and node['path']:
        node['source_file'] = node.pop('path')


def foldedges(edge: dict) -> None:
    if not edge.get('relation') and isinstance(edge.get('type'), str) and edge['type']:
        edge['relation'] = edge.pop('type')

    edge.pop('confidence', None)
    edge.pop('confidence_score', None)
    edge.pop('weight', None)
    edge.pop('_origin', None)


def coerceid(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value

    return str(value)


def hashable(value: object) -> bool:
    try:
        hash(value)
    except TypeError:
        return False

    return True


def coerceids(extraction: dict) -> None:
    for node in extraction.get('nodes') or ():
        if isinstance(node, dict) and 'id' in node:
            node['id'] = coerceid(node['id'])

    for edge in extraction.get('edges') or ():
        if not isinstance(edge, dict):
            continue

        for key in ('source', 'target', 'from', 'to'):
            if key in edge:
                edge[key] = coerceid(edge[key])

    for he in extraction.get('hyperedges') or ():
        if not isinstance(he, dict):
            continue

        members = he.get('nodes')

        if isinstance(members, list):
            he['nodes'] = [coerceid(ref) for ref in members]


def normfile(source: str | None, root: str | None = None) -> str | None:
    if not source:
        return source

    source = source.replace('\\', '/')

    if root and absolute(source):
        try:
            source = Path(source).relative_to(root).as_posix()
        except ValueError:
            try:
                source = Path(source).resolve().relative_to(Path(root).resolve()).as_posix()
            except (ValueError, OSError):
                pass

    return source


def identity(source: str | None, root: str | None = None) -> str | None:
    if not source:
        return None

    path = source.replace('\\', '/')
    loc = Path(path)

    if not absolute(path) and root:
        loc = Path(root) / path

    try:
        return loc.resolve().as_posix()
    except OSError:
        return loc.as_posix()


def isfilelabel(label: str | None, source: str | None) -> bool:
    if not label or not source:
        return False

    sf = str(source).replace('\\', '/')
    lbl = str(label)

    if lbl == sf.rsplit('/', 1)[-1]:
        return True

    return '/' in lbl and (sf == lbl or sf.endswith('/' + lbl))


def suffix(sf: str, others: set[str]) -> str:
    parts = [p for p in sf.replace('\\', '/').split('/') if p]
    candidates = [[p for p in o.replace('\\', '/').split('/') if p] for o in others if o != sf]

    for k in range(1, len(parts) + 1):
        tail = parts[-k:]

        if all(cand[-k:] != tail for cand in candidates):
            return '/'.join(tail)

    return '/'.join(parts)


def reassign(items: list[tuple]) -> dict:
    from collections import defaultdict

    groups: dict[str, list[tuple]] = defaultdict(list)

    for key, label, sf in items:
        if sf and label and isfilelabel(str(label), str(sf)):
            base = str(sf).replace('\\', '/').rsplit('/', 1)[-1]
            groups[base].append((key, str(sf)))

    out: dict = {}

    for members in groups.values():
        distinct = {sf for _, sf in members}

        if len(distinct) < 2:
            continue

        for key, sf in members:
            out[key] = suffix(sf, distinct)

    return out


def disambiguate(graph: nx.Graph) -> None:
    items = [(nid, attr.get('label'), attr.get('source_file')) for nid, attr in graph.nodes(data=True)]

    for nid, label in reassign(items).items():
        graph.nodes[nid]['label'] = label


def disambiguatenodes(nodes: list) -> None:
    items = [(i, node.get('label'), node.get('source_file')) for i, node in enumerate(nodes) if isinstance(node, dict)]

    for i, label in reassign(items).items():
        nodes[i]['label'] = label


def infer(path: Path) -> str | None:
    parent = path.parent

    try:
        marker = parent / '.graphify_root'

        if marker.exists():
            recorded = marker.read_text(encoding='utf-8-sig').strip()
            loc = Path(recorded)

            if recorded and loc.is_dir():
                return str(loc.resolve())
    except OSError:
        pass

    from .paths import GRAPHIFY_OUT

    try:
        if parent.name == Path(GRAPHIFY_OUT).name or (parent / '.graphify_root').exists() or (parent / 'manifest.json').exists():
            return str(parent.parent.resolve())
    except Exception:
        pass

    return None


def prunesets(sources: list[str] | None, root: str | None, current: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    subset: dict[str, str] = {}
    abset: dict[str, str] = {}

    for item in sources or []:
        if not item:
            continue

        subset.setdefault(item, item)
        norm = normfile(item, root)

        if norm:
            subset.setdefault(norm, item)

        loc = identity(item, root)

        if loc:
            abset.setdefault(loc, item)

    for item in current:
        subset.pop(item, None)
        loc = identity(item, root)

        if loc:
            abset.pop(loc, None)

    return (subset, abset)


def pruneroot(sources: list[str], stored: set[str]) -> str | None:
    rel = [sf.replace('\\', '/') for sf in stored if sf and isinstance(sf, str) and not absolute(sf.replace('\\', '/'))]

    if not rel:
        return None

    roots: set[str] = set()

    for item in sources:
        if not item or not isinstance(item, str):
            continue

        path = item.replace('\\', '/')

        if not absolute(path):
            continue

        hits = {path[:-len(s) - 1] for s in rel if path.endswith('/' + s)}

        if len(hits) > 1:
            return None

        roots |= hits

    if len(roots) == 1:
        return next(iter(roots))

    return None


def data(graph: nx.Graph, src: str, tgt: str) -> dict:
    raw = graph[src][tgt]

    if isinstance(graph, (nx.MultiGraph, nx.MultiDiGraph)):
        return next(iter(raw.values()), {})

    return raw


def datas(graph: nx.Graph, src: str, tgt: str) -> list[dict]:
    raw = graph[src][tgt]

    if isinstance(graph, (nx.MultiGraph, nx.MultiDiGraph)):
        return list(raw.values())

    return [raw]


def dedupenodes(nodes: list[dict]) -> list[dict]:
    items: dict = {}

    for node in nodes:
        nid = node.get('id')

        if nid is None:
            continue

        items[nid] = node

    return list(items.values())


def wins(first: dict, second: dict) -> None:
    if bool(first.get('type_only')) != bool(second.get('type_only')):
        first.pop('type_only', None)
        second.pop('type_only', None)


def dedupeedges(edges: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    out: list[dict] = []

    for edge in edges:
        key = (edge.get('source'), edge.get('target'), edge.get('relation'))

        if key in seen:
            wins(seen[key], edge)

            continue

        seen[key] = edge
        out.append(edge)

    return out


def stems(rel: Path) -> list[str]:
    forms: list[str] = []
    parent = rel.parent.name

    if parent and parent not in ('.', ''):
        forms.append(make(f'{parent}.{rel.stem}'))

    forms.append(make(rel.stem))
    seen: set[str] = set()

    return [f for f in forms if f and not (f in seen or seen.add(f))]


def remap(nodes: list, root: str | None) -> dict:
    from graphify.src.base import stem

    out: dict[str, str] = {}

    for node in nodes:
        if not isinstance(node, dict):
            continue

        if isasttier(node):
            continue

        nid = node.get('id')
        sf = node.get('source_file')

        if not nid or not isinstance(nid, str) or not sf:
            continue

        norm = normfile(str(sf), root) or str(sf)
        rel = Path(norm)

        if absolute(norm):
            continue

        if not rel.name:
            continue

        token = make(stem(rel))

        if not token:
            continue

        canon = normalize(nid)

        if canon == token or canon.startswith(token + '_'):
            continue

        newid: str | None = None
        forms = stems(rel)
        raw = str(sf).replace('\\', '/')

        if raw != norm and absolute(raw):
            loc = make(stem(Path(raw)))

            if loc and loc != token and loc not in forms:
                forms.insert(0, loc)

        for old in forms:
            if old == token:
                continue

            if canon == old:
                newid = token

                break

            prefix = old + '_'

            if canon.startswith(prefix):
                entity = canon[len(prefix):]
                newid = make(token, entity)

                break

        if newid and newid != nid:
            out[nid] = newid

    return out


MCP = frozenset({'mcp_command', 'mcp_package', 'env_var'})


def hasglobal(node: dict) -> bool:
    meta = node.get('metadata')

    if not isinstance(meta, dict):
        return False

    return meta.get('mcp_kind') in MCP


def legacy(nodes: list, root: str | Path | None = None, sample: int = 300) -> bool:
    from graphify.src.base import stem

    base = str(root) if root is not None else None
    count = 0

    for node in nodes:
        if not isinstance(node, dict):
            continue

        if str(node.get('source_location') or '') != 'L1':
            continue

        if hasglobal(node):
            continue

        nid = node.get('id')
        sf = node.get('source_file')

        if not nid or not isinstance(nid, str) or not sf:
            continue

        norm = normfile(str(sf), base) or str(sf)
        rel = Path(norm)

        if absolute(norm):
            continue

        if not rel.name:
            continue

        token = make(stem(rel))

        if not token:
            continue

        canon = normalize(nid)

        if canon == token or canon.startswith(token + '_'):
            count += 1
        else:
            for old in stems(rel):
                if old != token and (canon == old or canon.startswith(old + '_')):
                    return True

            count += 1

        if count >= sample:
            break

    return False


def twinremap(nodes: list) -> dict[str, str]:
    items: dict[str, dict] = {}

    for node in nodes:
        if isinstance(node, dict) and node.get('id'):
            items[str(node['id'])] = node

    out: dict[str, str] = {}

    for nid, node in items.items():
        if not nid.endswith('_doc'):
            continue

        bare = items.get(nid[:-4])

        if bare is None:
            continue

        sf = node.get('source_file')

        if not sf or bare.get('source_file') != sf:
            continue

        if node.get('file_type') != 'document' or bare.get('file_type') != 'document':
            continue

        out[nid[:-4]] = nid

    return out


def buildjson(extraction: dict, *, directed: bool = False, root: str | Path | None = None, dedup: bool = True) -> nx.Graph:
    base = str(Path(root).resolve()) if root else None

    if 'edges' not in extraction and 'links' in extraction:
        extraction = dict(extraction, edges=extraction['links'])

    if 'hyperedges' not in extraction and isinstance((extraction.get('graph') or {}).get('hyperedges'), list):
        extraction = dict(extraction, hyperedges=extraction['graph']['hyperedges'])

    coerceids(extraction)

    for node in extraction.get('nodes', []):
        if not isinstance(node, dict):
            continue

        if 'source' in node and 'source_file' not in node:
            nid = node.get('id', '?')
            hits = sum((1 for e in extraction.get('edges', []) if e.get('source') == nid or e.get('target') == nid))
            print(f"[graphify] WARNING: node '{nid}' uses field 'source' instead of 'source_file' — {hits} edge(s) may be misrouted. Rename the field to 'source_file' to silence this warning.", file=sys.stderr)
            node['source_file'] = node.pop('source')

        foldnodes(node)

        if node.get('file_type') in (None, ''):
            node['file_type'] = 'concept'

        ft = node.get('file_type', '')

        if ft and ft not in {'code', 'document', 'paper', 'image', 'concept'}:
            node['file_type'] = SYNONYMS.get(ft, 'concept')

    for he in extraction.get('hyperedges', []) or []:
        hyperedges(he)

    for edge in extraction.get('edges', []):
        if isinstance(edge, dict):
            foldedges(edge)

    errors = validate(extraction)
    actual = [e for e in errors if 'does not match any node id' not in e]

    if actual:
        causes: dict[str, list[str]] = {}

        for err in actual:
            m = re.search("missing required field '[^']*'", err)
            causes.setdefault(m.group(0) if m else 'other schema issue', []).append(err)

        breakdown = '; '.join((f'{len(errs)}x {cause} (e.g. {errs[0]})' for cause, errs in causes.items()))

        print(f'[graphify] Extraction warning ({len(actual)} issues): {breakdown}', file=sys.stderr)

    rekeyed = remap(extraction.get('nodes', []), base)

    if rekeyed:
        for node in extraction.get('nodes', []):
            if isinstance(node, dict) and node.get('id') in rekeyed:
                node['id'] = rekeyed[node['id']]

        for edge in extraction.get('edges', []):
            if not isinstance(edge, dict):
                continue

            if edge.get('source') in rekeyed:
                edge['source'] = rekeyed[edge['source']]

            if edge.get('target') in rekeyed:
                edge['target'] = rekeyed[edge['target']]

        for he in extraction.get('hyperedges', []) or []:
            if isinstance(he, dict) and isinstance(he.get('nodes'), list):
                he['nodes'] = [rekeyed.get(n, n) if hashable(n) else n for n in he['nodes']]

    twinned = twinremap(extraction.get('nodes', []))

    if twinned:
        extraction['nodes'] = [n for n in extraction.get('nodes', []) if not (isinstance(n, dict) and n.get('id') in twinned)]
        rewritten = []

        for edge in extraction.get('edges', []):
            if isinstance(edge, dict):
                s0, t0 = edge.get('source'), edge.get('target')

                if s0 in twinned:
                    edge['source'] = twinned[s0]

                if t0 in twinned:
                    edge['target'] = twinned[t0]

                if edge.get('source') == edge.get('target') and (s0 in twinned or t0 in twinned):
                    continue

            rewritten.append(edge)

        extraction['edges'] = rewritten

        for he in extraction.get('hyperedges', []) or []:
            if isinstance(he, dict) and isinstance(he.get('nodes'), list):
                he['nodes'] = [twinned.get(n, n) if hashable(n) else n for n in he['nodes']]

    graph: nx.Graph = nx.DiGraph() if directed else nx.Graph()

    for node in extraction.get('nodes', []):
        if isinstance(node, dict):
            if 'id' not in node:
                continue

            try:
                hash(node['id'])
            except TypeError:
                print(f"[graphify] WARNING: skipping node with non-hashable id {node['id']!r} (must be a string).", file=sys.stderr)

                continue

            if 'source_file' in node:
                node['source_file'] = normfile(node['source_file'], base)

            if 'definition_file' in node:
                node['definition_file'] = normfile(node['definition_file'], base)

        graph.add_node(node['id'], **{k: v for k, v in node.items() if k != 'id'})

    known = set(graph.nodes())
    locnodes: dict[tuple[str, str], str] = {}
    collisions: set[tuple[str, str]] = set()
    nolocnodes: dict[tuple[str, str], str] = {}
    filenodes: list[tuple[str, str]] = []
    methods: dict[tuple[str, str], list[str]] = {}

    for nid in sorted(known):
        attrs = graph.nodes[nid]
        label = str(attrs.get('label', '')).strip()
        sf = str(attrs.get('source_file', ''))

        if not label or not sf:
            continue

        is_ast = isasttier(attrs)

        if attrs.get('source_location') or is_ast:
            key = (sf, label)

            if is_ast:
                if key in locnodes and isasttier(graph.nodes[locnodes[key]]):
                    collisions.add(key)

                locnodes[key] = nid

                if isfilelabel(label, sf):
                    filenodes.append((nid, sf))

                if label.startswith('.') and label.endswith('()'):
                    token = make(label.removeprefix('.').removesuffix('()'))
                    methods.setdefault((sf, token), []).append(nid)
            else:
                locnodes.setdefault(key, nid)

    ghosts: dict[str, str] = {}

    for nid in sorted(known):
        attrs = graph.nodes[nid]

        if isasttier(attrs):
            continue

        label = str(attrs.get('label', '')).strip()
        sf = str(attrs.get('source_file', ''))

        if not label or not sf:
            continue

        key = (sf, label)

        if key in collisions:
            continue

        if key in locnodes and locnodes[key] != nid:
            if not dedup and not isasttier(graph.nodes[locnodes[key]]):
                continue

            nolocnodes[key] = nid
        elif key not in locnodes:
            token = make(label.removeprefix('.').removesuffix('()'))
            candidates = methods.get((sf, token), [])

            if len(candidates) == 1:
                ghosts[nid] = candidates[0]

    for key, semid in nolocnodes.items():
        astid = locnodes.get(key)

        if astid is not None:
            ghosts[semid] = astid

    if filenodes:
        for nid in sorted(known):
            if nid in ghosts:
                continue

            attrs = graph.nodes[nid]

            if isasttier(attrs):
                continue

            label = str(attrs.get('label', '')).strip()

            if not label:
                continue

            matches = {astid for astid, astsf in filenodes if isfilelabel(label, astsf)}

            if len(matches) == 1:
                ghosts[nid] = next(iter(matches))

    for ghost in ghosts:
        graph.remove_node(ghost)
        known.discard(ghost)

    canon = {normalize(nid): nid for nid in known}

    for ghost, target in ghosts.items():
        canon[normalize(ghost)] = target
        canon[ghost] = target

    from graphify.src.base import stem

    aliases: dict[str, set[str]] = {}

    for nid in known:
        attrs = graph.nodes[nid]
        sf = attrs.get('source_file')

        if not sf:
            continue

        rel = Path(str(sf))

        if absolute(str(sf)):
            continue

        token = make(stem(rel))

        if str(attrs.get('label', '')) == rel.name:
            tail = ''
        else:
            tail = ''

            if normalize(nid).startswith(token):
                tail = normalize(nid)[len(token):]

        for old in stems(rel):
            if old == token:
                continue

            alias = old + tail
            aliases.setdefault(normalize(alias), set()).add(nid)
            aliases.setdefault(alias, set()).add(nid)

        if isasttier(attrs) and str(attrs.get('label', '')).startswith('.'):
            method = make(str(attrs.get('label', '')).strip().removeprefix('.').removesuffix('()'))
            malias = f'{token}_{method}'

            if normalize(nid) != normalize(malias):
                aliases.setdefault(normalize(malias), set()).add(nid)
                aliases.setdefault(malias, set()).add(nid)

    for key, cands in aliases.items():
        if len(cands) == 1:
            canon.setdefault(key, next(iter(cands)))

    for edge in sorted(extraction.get('edges', []), key=lambda e: (str(e.get('source', e.get('from', ''))), str(e.get('target', e.get('to', ''))), str(e.get('relation', '')))):
        if 'source' not in edge and 'from' in edge:
            edge['source'] = edge['from']

        if 'target' not in edge and 'to' in edge:
            edge['target'] = edge['to']

        if 'source' not in edge or 'target' not in edge:
            continue

        src, tgt = edge['source'], edge['target']

        try:
            hash(src)
            hash(tgt)
        except TypeError:
            print(f'[graphify] WARNING: skipping edge with non-hashable endpoint (source={src!r}, target={tgt!r}).', file=sys.stderr)

            continue

        if src not in known:
            src = canon.get(normalize(src), src)

        if tgt not in known:
            tgt = canon.get(normalize(tgt), tgt)

        if src not in known or tgt not in known:
            if edge.get('relation') in STUBS and src in known and tgt not in known and isinstance(tgt, str):
                mint(graph, known, tgt)
            else:
                continue

        attrs = {k: v for k, v in edge.items() if k not in ('source', 'target', 'target_file', 'local_alias')}


        if not attrs.get('source_file'):
            attrs['source_file'] = graph.nodes[src].get('source_file') or graph.nodes[tgt].get('source_file') or ''

        if 'source_file' in attrs:
            attrs['source_file'] = normfile(attrs['source_file'], base)

        if attrs.get('definition_file'):
            attrs['definition_file'] = normfile(attrs['definition_file'], base)

        rel = attrs.get('relation')

        if rel in ('calls', 'imports', 'imports_from', 'references'):
            srcext = Path(graph.nodes[src].get('source_file') or '').suffix.lower()
            tgtext = Path(graph.nodes[tgt].get('source_file') or '').suffix.lower()
            srcfam = FAMILIES.get(srcext)
            tgtfam = FAMILIES.get(tgtext)

            if srcfam is not None and tgtfam is not None and (srcfam != tgtfam):
                continue

        if src == tgt and rel in ('imports', 'imports_from', 're_exports'):
            continue

        attrs['_src'] = src
        attrs['_tgt'] = tgt

        if rel in STUBS and graph.has_edge(src, tgt):
            prior = graph[src][tgt]
            rev = not graph.is_directed() and prior.get('relation') == attrs.get('relation') and (prior.get('_src') == tgt) and (prior.get('_tgt') == src)

            if prior.get('relation') in STUBS and not rev:
                wins(prior, attrs)

        if not graph.is_directed() and graph.has_edge(src, tgt):
            existing = data(graph, src, tgt)

            if existing.get('relation') == attrs.get('relation') and (existing.get('_src') == tgt and existing.get('_tgt') == src):
                continue

        if graph.has_edge(src, tgt):
            existing = data(graph, src, tgt)
            exrel = existing.get('relation')

            if attrs.get('relation') in GENERIC and exrel is not None and exrel not in GENERIC:
                continue

            if exrel == attrs.get('relation'):
                if existing.get('source_location') and not attrs.get('source_location'):
                    continue

        graph.add_edge(src, tgt, **attrs)

    hyper = extraction.get('hyperedges', [])

    if hyper:
        kept = []

        for he in hyper:
            if isinstance(he, dict) and he.get('source_file'):
                he['source_file'] = normfile(he['source_file'], base)

            if isinstance(he, dict) and isinstance(he.get('nodes'), list):
                valid = []

                for m in he['nodes']:
                    try:
                        hash(m)
                    except TypeError:
                        continue

                    if m not in known and isinstance(m, str):
                        m = canon.get(normalize(m), m)

                    if m in known:
                        valid.append(m)

                if not valid:
                    print(f"[graphify] WARNING: dropping hyperedge {he.get('id', '?')!r} — none of its members {he.get('nodes')!r} match built nodes.", file=sys.stderr)

                    continue

                if valid != he['nodes']:
                    he['nodes'] = valid

            kept.append(he)

        if kept:
            graph.graph['hyperedges'] = kept
        else:
            graph.graph['hyperedges'] = []
            print(f"[graphify] WARNING: all {len(hyper)} hyperedge(s) were dropped by member revalidation; graph.json's hyperedge set will be emptied on the next export.", file=sys.stderr)

    disambiguate(graph)

    return graph


def build(extractions: list[dict], *, directed: bool = False, dedup: bool = True, backend: str | None = None, root: str | Path | None = None, protected: set[str] | None = None) -> nx.Graph:
    combined: dict = {'nodes': [], 'edges': [], 'hyperedges': []}

    for ext in extractions:
        combined['nodes'].extend(ext.get('nodes', []))
        combined['edges'].extend(ext.get('edges', []))
        combined['hyperedges'].extend(ext.get('hyperedges', []))

    base = str(Path(root).resolve()) if root else None
    collapsed = 0

    if dedup and combined['nodes']:
        coerceids(combined)

        for n in combined['nodes']:
            if isinstance(n, dict):
                foldnodes(n)

                if 'source_file' in n:
                    n['source_file'] = normfile(n['source_file'], base)

                if 'definition_file' in n:
                    n['definition_file'] = normfile(n['definition_file'], base)

        before = len(combined['nodes'])
        combined['nodes'], combined['edges'] = dedupe(combined['nodes'], combined['edges'], communities={}, dedup_llm_backend=backend, root=base, hyperedges=combined.get('hyperedges'), protected_ids=protected)
        collapsed = before - len(combined['nodes'])

    graph = buildjson(combined, directed=directed, root=base, dedup=dedup)

    if collapsed:
        graph.graph['_dedup_collapsed'] = collapsed

    return graph


def shrink(graph) -> tuple[int, int]:
    attrs = getattr(graph, 'graph', None)

    if not isinstance(attrs, dict):
        return (0, 0)

    collapsed = int(attrs.pop('_dedup_collapsed', 0) or 0)
    pruned = int(attrs.pop('_pruned_node_count', 0) or 0)

    return (collapsed, pruned)


def normlabel(label: str | None) -> str:
    if not isinstance(label, str):
        label = '' if label is None else str(label)

    label = unicodedata.normalize('NFKC', label)

    return re.sub('[\\W_ ]+', ' ', label.casefold(), flags=re.UNICODE).strip()


def dedupelabel(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    chunk = re.compile('_c\\d+$')
    canonical: dict[str, dict] = {}
    remapping: dict[str, str] = {}

    for node in nodes:
        key = normlabel(node.get('label', node.get('id', '')))

        if not key:
            continue

        existing = canonical.get(key)

        if existing is None:
            canonical[key] = node
        else:
            hassuffix = bool(chunk.search(node['id']))
            exhassuffix = bool(chunk.search(existing['id']))

            if hassuffix and not exhassuffix:
                remapping[node['id']] = existing['id']
            elif exhassuffix and not hassuffix:
                remapping[existing['id']] = node['id']
                canonical[key] = node
            elif len(node['id']) < len(existing['id']):
                remapping[existing['id']] = node['id']
                canonical[key] = node
            else:
                remapping[node['id']] = existing['id']

    if not remapping:
        return (nodes, edges)

    print(f'[graphify] Deduplicated {len(remapping)} duplicate node(s) by label.', file=sys.stderr)
    deduped = list(canonical.values())
    filtered = []

    for edge in edges:
        e = dict(edge)
        e['source'] = remapping.get(e['source'], e['source'])
        e['target'] = remapping.get(e['target'], e['target'])

        if e['source'] != e['target']:
            filtered.append(e)

    return (deduped, filtered)


def load(path: Path) -> tuple[list, list, list, bool] | None:
    if not path.exists():
        return None

    from graphify.security import check

    check(path)

    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f'Cannot read {path} for incremental merge: {exc}. Delete the file and run a full rebuild.') from exc

    key = 'links' if 'links' in data else 'edges'
    nodes = list(data.get('nodes', []))
    edges = list(data.get(key, []))

    for item in nodes + edges:
        if isinstance(item, dict):
            backfill(item)

    return (nodes, edges, list(data.get('hyperedges', [])), bool(data.get('directed', False)))


def tiersources(chunks: Iterable[dict], root: str | Path | None = None, sources: Iterable[str | Path] | None = None) -> tuple[set[str], set[str]]:
    explicit: set[str] = set()

    if sources is not None:
        for s in sources:
            if s:
                explicit.add(str(s))

    for ch in chunks:
        if isinstance(ch, dict):
            for s in ch.get('extracted_sources') or []:
                if s:
                    explicit.add(str(s))

    astsources: set[str] = set()
    semsources: set[str] = set()

    if explicit:
        for sf in explicit:
            astsources.add(sf)
            norm = normfile(sf, root)

            if norm:
                astsources.add(norm)
    else:
        for ch in chunks:
            if not isinstance(ch, dict):
                continue

            for n in ch.get('nodes', []):
                if not isinstance(n, dict):
                    continue

                sf = n.get('source_file')

                if not sf or not isasttier(n):
                    continue

                astsources.add(sf)
                norm = normfile(sf, root)

                if norm:
                    astsources.add(norm)

    for ch in chunks:
        if not isinstance(ch, dict):
            continue

        for n in ch.get('nodes', []):
            if not isinstance(n, dict):
                continue

            sf = n.get('source_file')

            if not sf or isasttier(n):
                continue

            semsources.add(sf)
            norm = normfile(sf, root)

            if norm:
                semsources.add(norm)

    return (astsources, semsources)


def mergeraw(new: dict, path: str | Path, prunes: list[str] | None = None, root: str | Path | None = None, *, sources: Iterable[str | Path] | None = None) -> dict:
    graphpath = Path(path)
    loaded = load(graphpath)

    if loaded is None:
        return new

    exnodes, exedges, exhyper, _ = loaded
    base = str(Path(root).resolve()) if root is not None else infer(graphpath)
    astsources, semsources = tiersources([new], root=base, sources=sources)
    allsources: set[str] = astsources | semsources
    pruneset, pruneabs = prunesets(prunes, base, allsources)
    proot = base

    def hit(sf: str | None) -> bool:
        if not sf:
            return False

        if sf in pruneset:
            return True

        norm = normfile(sf, proot)

        if norm and norm in pruneset:
            return True

        loc = identity(sf, proot)

        return bool(loc) and loc in pruneabs

    if pruneset or pruneabs:
        stored = {item.get('source_file') for seq in (exnodes, exedges, exhyper) for item in seq if isinstance(item, dict)}
        stored.discard(None)

        if not any((hit(sf) for sf in stored)):
            derived = pruneroot(prunes or [], stored)

            if derived is not None and derived != proot:
                proot = derived
                pruneset, pruneabs = prunesets(prunes, proot, allsources)

    def dropped(item: dict) -> bool:
        if not isinstance(item, dict):
            return True

        sf = item.get('source_file')
        own = astsources if isasttier(item) else semsources

        if sf in own or normfile(sf, base) in own:
            return True

        if not sf:
            return False

        return hit(sf)

    shrinkage: dict[str, tuple[int, int]] = {}

    if semsources:
        priorcounts: dict[str, int] = {}

        for n in exnodes:
            if isinstance(n, dict) and not isasttier(n):
                sf = n.get('source_file')

                if sf:
                    canon = normfile(sf, base) or sf
                    priorcounts[canon] = priorcounts.get(canon, 0) + 1

        freshcounts: dict[str, int] = {}

        for n in new.get('nodes', []):
            if isinstance(n, dict) and not isasttier(n):
                sf = n.get('source_file')

                if sf:
                    canon = normfile(sf, base) or sf
                    freshcounts[canon] = freshcounts.get(canon, 0) + 1

        for canonsf, count in freshcounts.items():
            prior = priorcounts.get(canonsf, 0)

            if prior > 1 and count < prior:
                shrinkage[canonsf] = (prior, count)

    new['nodes'] = [n for n in exnodes if not dropped(n)] + list(new.get('nodes', []))
    new['edges'] = [e for e in exedges if not dropped(e)] + list(new.get('edges', []))
    carried = [he for he in exhyper if not dropped(he)]

    if carried or new.get('hyperedges'):
        new['hyperedges'] = carried + list(new.get('hyperedges', []))

    if shrinkage:
        new['_unverified_semantic_shrink'] = shrinkage

    return new


def buildmerge(chunks: list[dict], path: str | Path | None = None, prunes: list[str] | None = None, *, directed: bool | None = None, dedup: bool = True, backend: str | None = None, root: str | Path | None = None, sources: Iterable[str | Path] | None = None) -> nx.Graph:
    chunks = list(chunks)
    graphpath = Path(path if path is not None else default())
    loaded = load(graphpath)

    if loaded is not None:
        exnodes, exedges, exhyper, exdirected = loaded
        hadgraph = True
    else:
        exnodes = []
        exedges = []
        exhyper = []
        exdirected = False
        hadgraph = False

    if directed is None:
        directed = exdirected if hadgraph else False

    base = str(Path(root).resolve()) if root is not None else infer(graphpath)
    astsources, semsources = tiersources(chunks, root=base, sources=sources)
    allsources: set[str] = astsources | semsources
    disknodes = exnodes
    diskn = len(exnodes)
    shrinkage: dict[str, tuple[int, int]] = {}

    if hadgraph and semsources:
        priorcounts: dict[str, int] = {}

        for n in disknodes:
            if isinstance(n, dict) and not isasttier(n):
                sf = n.get('source_file')

                if sf:
                    canon = normfile(sf, base) or sf
                    priorcounts[canon] = priorcounts.get(canon, 0) + 1

        freshcounts: dict[str, int] = {}

        for ch in chunks:
            for n in ch.get('nodes', []):
                if isinstance(n, dict) and not isasttier(n):
                    sf = n.get('source_file')

                    if sf:
                        canon = normfile(sf, base) or sf
                        freshcounts[canon] = freshcounts.get(canon, 0) + 1

        for canonsf, count in freshcounts.items():
            prior = priorcounts.get(canonsf, 0)

            if prior > 1 and count < prior:
                shrinkage[canonsf] = (prior, count)

    if allsources:

        def kept(item: dict) -> bool:
            sf = item.get('source_file')
            own = astsources if isasttier(item) else semsources

            return sf not in own and normfile(sf, base) not in own

        exnodes = [n for n in exnodes if kept(n)]
        exedges = [e for e in exedges if kept(e)]
        replacedn = diskn - len(exnodes)

        if replacedn:
            print(f'[graphify] Replaced {replacedn} node(s) from re-extracted source file(s).', file=sys.stderr)

    proot = base
    pruneset, pruneabs = prunesets(prunes, proot, allsources)
    matched: set[str] = set()

    def match(sf: str | None) -> bool:
        if not sf:
            return False

        found = pruneset.get(sf)

        if found is None:
            norm = normfile(sf, proot)

            if norm:
                found = pruneset.get(norm)

        if found is None:
            loc = identity(sf, proot)

            if loc:
                found = pruneabs.get(loc)

        if found is None:
            return False

        matched.add(found)

        return True

    if pruneset or pruneabs:
        stored = {item.get('source_file') for seq in (disknodes, exedges, exhyper) for item in seq if isinstance(item, dict)}
        stored.discard(None)

        if not any((match(sf) for sf in stored)):
            derived = pruneroot(prunes or [], stored)

            if derived is not None and derived != proot:
                proot = derived
                pruneset, pruneabs = prunesets(prunes, proot, allsources)

    carried: list[dict] = []

    if exhyper:
        newkeys = {(he.get('id'), normfile(he.get('source_file'), base) or None) for chunk in chunks for he in chunk.get('hyperedges') or [] if isinstance(he, dict) and he.get('id')}

        for he in exhyper:
            if not isinstance(he, dict):
                continue

            sf = he.get('source_file')
            norm = normfile(sf, base)

            if sf in semsources or norm in semsources:
                continue

            if match(sf):
                continue

            if he.get('id') and (he.get('id'), norm or None) in newkeys:
                continue

            carried.append(he)

    if prunes:
        freshids = {n.get('id') for chunk in chunks for n in chunk.get('nodes', [])}
        exnodes = [n for n in exnodes if not (n.get('id') in freshids and match(n.get('source_file')))]

    baseline = [{'nodes': exnodes, 'edges': exedges, 'hyperedges': carried}] if hadgraph else []
    protected = {n['id'] for n in exnodes if isinstance(n, dict) and n.get('id')} if hadgraph else None
    allchunks = baseline + list(chunks)
    graph = build(allchunks, directed=directed, dedup=dedup, backend=backend, root=base, protected=protected)

    if prunes:
        isolated = {n for n, d in graph.nodes(data=True) if not d.get('source_file') and graph.degree(n) == 0}
        toremove = [n for n, d in graph.nodes(data=True) if match(d.get('source_file'))]
        graph.remove_nodes_from(toremove)
        nnodes = len(toremove)
        edgestoremove = [(u, v) for u, v, d in graph.edges(data=True) if match(d.get('source_file'))]

        if edgestoremove:
            graph.remove_edges_from(edgestoremove)

        orphaned = [n for n, d in graph.nodes(data=True) if not d.get('source_file') and graph.degree(n) == 0 and n not in isolated]

        if orphaned:
            graph.remove_nodes_from(orphaned)
            nnodes += len(orphaned)

        diskids = {n.get('id') for n in disknodes if isinstance(n, dict) and n.get('id') is not None}
        graph.graph['_pruned_node_count'] = sum((1 for nid in (*toremove, *orphaned) if nid in diskids))
        nfiles = len(matched)

        if nnodes:
            print(f'[graphify] Pruned {nnodes} node(s) from {nfiles} deleted or excluded source file(s).', file=sys.stderr)

        if edgestoremove:
            print(f'[graphify] Pruned {len(edgestoremove)} edge(s) from deleted or excluded source file(s).', file=sys.stderr)

        if not nnodes and not edgestoremove:
            if (pruneset or pruneabs) and not matched:
                p0 = next((p for p in prunes if p), None)
                sample = normfile(p0, proot) or p0
                samplesf = next(iter(sorted((str(d.get('source_file')) for _, d in graph.nodes(data=True) if d.get('source_file')))), None)
                print(f"[graphify] WARNING: {len(prunes)} prune source(s) matched no nodes or edges — nothing was removed. Prune entry {sample!r} does not correspond to any stored source_file (e.g. {samplesf!r}). If these files should have been pruned, pass root= to buildmerge so absolute paths relativize to the graph's source_file keys. (#2446)", file=sys.stderr)
            else:
                print(f'[graphify] {len(prunes)} source file(s) deleted or excluded since last run — no matching nodes or edges in graph, already clean.', file=sys.stderr)

    if hadgraph and not dedup:

        def innew(n: dict) -> bool:
            nid = n.get('id')

            if nid is None or not hashable(nid):
                return True

            if nid in graph:
                return True

            return isinstance(nid, str) and not nid.endswith('_doc') and n.get('file_type') == 'document' and f'{nid}_doc' in graph and graph.nodes[f'{nid}_doc'].get('source_file') == n.get('source_file')

        lost = [n for n in disknodes if isinstance(n, dict) and not innew(n)]

        def explained(n: dict) -> bool:
            sf = n.get('source_file')

            if not sf:
                return True

            own = astsources if isasttier(n) else semsources

            if sf in own or normfile(sf, base) in own:
                return True

            return match(sf)

        unexplained = [n for n in lost if not explained(n)]

        if unexplained:
            raise ValueError(f"graphify: buildmerge would drop {len(unexplained)} node(s) from sources that were neither re-extracted nor pruned this run (e.g. {unexplained[0].get('id')!r}); graph would go {diskn} → {graph.number_of_nodes()} nodes. Pass prune_sources explicitly if you intend to remove them. (#479)")

    if shrinkage:
        graph.graph['_unverified_semantic_shrink'] = shrinkage

    return graph


def prefix(graph: nx.Graph, tag: str, offset: int = 0) -> nx.Graph:
    relabel = {n: f'{tag}::{n}' for n, d in graph.nodes(data=True) if not d.get('external')}
    out = nx.relabel_nodes(graph, relabel, copy=True)

    for node, data_dict in out.nodes(data=True):
        if data_dict.get('external'):
            continue

        data_dict['repo'] = tag
        data_dict.setdefault('local_id', node.split('::', 1)[1])
        cid = data_dict.get('community')

        if offset and isinstance(cid, int):
            data_dict['local_community'] = cid
            data_dict['community'] = cid + offset

    for u, v, data_dict in out.edges(data=True):
        if '_src' in data_dict and data_dict['_src'] in relabel:
            data_dict['_src'] = relabel[data_dict['_src']]

        if '_tgt' in data_dict and data_dict['_tgt'] in relabel:
            data_dict['_tgt'] = relabel[data_dict['_tgt']]

    hyperedges_list = out.graph.get('hyperedges')

    if isinstance(hyperedges_list, list):
        rewritten = []

        for he in hyperedges_list:
            if isinstance(he, dict):
                he = dict(he)

                if isinstance(he.get('nodes'), list):
                    he['nodes'] = [relabel.get(m, m) if hashable(m) else m for m in he['nodes']]

                if he.get('id'):
                    he['id'] = f"{tag}::{he['id']}"

            rewritten.append(he)

        out.graph['hyperedges'] = rewritten

    return out


def repotags(paths: list[Path]) -> list[str]:
    dirs = [p.parent.parent for p in paths]
    tags = [d.name or 'repo' for d in dirs]

    if len(set(tags)) != len(tags):
        widened: list[str] = []

        for d in dirs:
            parent = d.parent.name
            widened.append(f'{parent}_{d.name}' if parent and d.name else d.name or 'repo')

        tags = widened

    seen: dict[str, int] = {}
    unique: list[str] = []

    for t in tags:
        seen[t] = seen.get(t, 0) + 1
        unique.append(t if seen[t] == 1 else f'{t}-{seen[t]}')

    return unique


def prunerepo(graph: nx.Graph, tag: str) -> int:
    toremove = [n for n, d in graph.nodes(data=True) if d.get('repo') == tag]
    graph.remove_nodes_from(toremove)

    return len(toremove)