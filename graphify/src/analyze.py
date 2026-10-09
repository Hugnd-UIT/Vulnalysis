from __future__ import annotations

from itertools import zip_longest
from pathlib import Path

import networkx as nx

from graphify.build import data
from graphify.cluster import cohesion
from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS, IMAGE_EXTENSIONS, PAPER_EXTENSIONS


NOISE = frozenset({'str', 'int', 'float', 'bool', 'bytes', 'bytearray', 'complex', 'object', 'True', 'False', 'MagicMock', 'Mock', 'AsyncMock', 'NonCallableMock', 'NonCallableMagicMock', 'PropertyMock', 'patch', 'sentinel', 'Path', 'Any', 'Optional', 'List', 'Dict', 'Set', 'Tuple', 'Union', 'Callable', 'Type', 'ClassVar', 'Final', 'Literal', 'Protocol', 'Counter', 'defaultdict', 'OrderedDict', 'datetime', 'Enum', 'os', 'sys', 're', 'json', 'io', 'abc', 'typing', 'Foundation', 'SwiftUI', 'UIKit', 'AppKit', 'Combine', 'String', 'Int', 'Double', 'Float', 'Bool', 'Data', 'URL', 'Date', 'UUID', 'Sendable', 'Codable', 'Decodable', 'Encodable', 'Equatable', 'Hashable', 'Identifiable', 'Comparable', 'AnyObject', 'Error', 'LocalizedError', 'NSObject', 'NSString', 'NSError', 'NSLock', 'View', 'Color', 'Font', 'DispatchQueue'})
FAMILIES: dict[str, str] = {**{e: 'python' for e in ('.py', '.pyw')}, **{e: 'js' for e in ('.js', '.jsx', '.mjs', '.cjs', '.ejs', '.ts', '.tsx', '.mts', '.cts', '.vue', '.svelte')}, **{e: 'go' for e in ('.go',)}, **{e: 'rust' for e in ('.rs',)}, **{e: 'jvm' for e in ('.java', '.kt', '.kts', '.scala')}, **{e: 'c' for e in ('.c', '.h', '.cpp', '.cc', '.cxx', '.hpp')}, **{e: 'ruby' for e in ('.rb', '.rake')}, **{e: 'swift' for e in ('.swift',)}, **{e: 'dotnet' for e in ('.cs', '.vb')}, **{e: 'php' for e in ('.php',)}, **{e: 'r' for e in ('.r',)}, **{e: 'cobol' for e in ('.cbl', '.cob', '.cobol', '.cpy')}, **{e: 'solidity' for e in ('.sol',)}, **{e: 'erlang' for e in ('.erl', '.hrl', '.escript')}}
JSONNOISE: frozenset[str] = frozenset({'start', 'end', 'name', 'id', 'type', 'properties', 'value', 'key', 'data', 'items', 'title', 'description', 'version', 'dependencies', 'devdependencies', 'peerdependencies', 'optionaldependencies', 'bundleddependencies', 'bundledependencies'})


def cross(srca: str, srcb: str) -> bool:
    exta = Path(srca).suffix.lower()
    extb = Path(srcb).suffix.lower()
    fama = FAMILIES.get(exta)
    famb = FAMILIES.get(extb)

    if fama is None or famb is None:
        return False

    return fama != famb


def commap(comms: dict[int, list[str]]) -> dict[str, int]:
    return {n: cid for cid, nodes in comms.items() for n in nodes}


def isfile(graph: nx.Graph, nid: str) -> bool:
    attrs = graph.nodes[nid]
    lbl = attrs.get('label', '')

    if not lbl:
        return False

    source = attrs.get('source_file', '')

    if source:
        from graphify.build import isfilelabel

        if isfilelabel(lbl, source):
            return True

    if lbl.startswith('.') and lbl.endswith('()'):
        return True

    if lbl.endswith('()') and graph.degree(nid) <= 1:
        return True

    return False


def isjson(graph: nx.Graph, nid: str) -> bool:
    attrs = graph.nodes[nid]
    src = (attrs.get('source_file') or '').lower()

    if not src.endswith('.json'):
        return False

    lbl = (attrs.get('label') or '').strip().lower()

    return lbl in JSONNOISE


def isconcept(graph: nx.Graph, nid: str) -> bool:
    data = graph.nodes[nid]
    src = data.get('source_file', '')

    if not src:
        return True

    if '.' not in src.split('/')[-1]:
        return True

    return False


def isast(graph: nx.Graph, nid: str) -> bool:
    from graphify.build import isasttier

    attrs = graph.nodes[nid]

    if not isasttier(attrs):
        return False

    if graph.degree(nid) != 1:
        return False

    edges = list(graph.edges(nid, data=True))

    return len(edges) == 1 and edges[0][2].get('relation') == 'contains'


def category(path: str) -> str:
    ext = '.' + path.rsplit('.', 1)[-1].lower() if '.' in path else ''

    if ext in CODE_EXTENSIONS:
        return 'code'

    if ext in PAPER_EXTENSIONS:
        return 'paper'

    if ext in IMAGE_EXTENSIONS:
        return 'image'

    return 'doc'


def topdir(path: str) -> str:
    path = path.replace('\\', '/')

    while path.startswith('./'):
        path = path[2:]

    return path.split('/')[0] if '/' in path else '.'


def score(graph: nx.Graph, u: str, v: str, data: dict, cmap: dict[str, int], usrc: str, vsrc: str, degs: dict[str, int] | None = None) -> tuple[int, list[str]]:
    val = 1
    reasons: list[str] = []
    rel = data.get('relation', '')
    catu = category(usrc)
    catv = category(vsrc)

    if catu != catv and not supp:
        val += 2
        reasons.append(f'crosses file types ({catu} ↔ {catv})')

    topu = (graph.nodes[u].get('repo'), topdir(usrc))
    topv = (graph.nodes[v].get('repo'), topdir(vsrc))

    if topu != topv and not supp:
        val += 2
        reasons.append('connects across different repos/directories')

    cidu = cmap.get(u)
    cidv = cmap.get(v)

    if cidu is not None and cidv is not None and cidu != cidv and not supp:
        val += 1
        reasons.append('bridges separate communities')

    if data.get('relation') == 'semantically_similar_to':
        val = int(val * 1.5)
        reasons.append('semantically similar concepts with no structural link')

    degu = degs[u] if degs is not None else graph.degree(u)
    degv = degs[v] if degs is not None else graph.degree(v)

    if min(degu, degv) <= 2 and max(degu, degv) >= 5:
        val += 1
        periph = graph.nodes[u].get('label', u) if degu <= 2 else graph.nodes[v].get('label', v)
        hub = graph.nodes[v].get('label', v) if degu <= 2 else graph.nodes[u].get('label', u)
        reasons.append(f'peripheral node `{periph}` unexpectedly reaches hub `{hub}`')

    return (val, reasons)


def commsurprises(graph: nx.Graph, comms: dict[int, list[str]], limit: int) -> list[dict]:

    if not comms:

        if graph.number_of_edges() == 0:
            return []

        if graph.number_of_nodes() > 5000:
            return []

        bet = nx.edge_betweenness_centrality(graph)
        top = sorted(bet.items(), key=lambda x: x[1], reverse=True)[:limit]
        res = []

        for (u, v), bscore in top:
            attrs = data(graph, u, v)
            res.append({'source': graph.nodes[u].get('label', u), 'target': graph.nodes[v].get('label', v), 'source_files': [graph.nodes[u].get('source_file', ''), graph.nodes[v].get('source_file', '')], 'relation': attrs.get('relation', ''), 'note': f'Bridges graph structure (betweenness={bscore:.3f})'})

        return res

    cmap = commap(comms)
    items = []

    for u, v, data in graph.edges(data=True):
        cidu = cmap.get(u)
        cidv = cmap.get(v)

        if cidu is None or cidv is None or cidu == cidv:
            continue

        if isfile(graph, u) or isfile(graph, v):
            continue

        rel = data.get('relation', '')

        if rel in ('imports', 'imports_from', 'contains', 'method'):
            continue

        su = data.get('_src', u)

        if su not in graph.nodes:
            su = u

        sv = data.get('_tgt', v)

        if sv not in graph.nodes:
            sv = v

        items.append({'source': graph.nodes[su].get('label', su), 'target': graph.nodes[sv].get('label', sv), 'source_files': [graph.nodes[su].get('source_file', ''), graph.nodes[sv].get('source_file', '')], 'relation': rel, 'note': f'Bridges community {cidu} → community {cidv}', '_pair': tuple(sorted([cidu, cidv]))})

    seen: set[tuple] = set()
    deduped = []

    for s in items:
        pair = s.pop('_pair')

        if pair not in seen:
            seen.add(pair)
            deduped.append(s)

    return deduped[:limit]


def filesurprises(graph: nx.Graph, comms: dict[int, list[str]], limit: int) -> list[dict]:
    cmap = commap(comms)
    degs = dict(graph.degree())
    cands = []

    for u, v, data in graph.edges(data=True):
        rel = data.get('relation', '')

        if rel in ('imports', 'imports_from', 'contains', 'method'):
            continue

        if isconcept(graph, u) or isconcept(graph, v):
            continue

        if isfile(graph, u) or isfile(graph, v):
            continue

        usrc = graph.nodes[u].get('source_file', '')
        vsrc = graph.nodes[v].get('source_file', '')

        if not usrc or not vsrc or usrc == vsrc:
            continue

        val, reasons = score(graph, u, v, data, cmap, usrc, vsrc, degs)
        su = data.get('_src', u)

        if su not in graph.nodes:
            su = u

        sv = data.get('_tgt', v)

        if sv not in graph.nodes:
            sv = v

        cands.append({'_score': val, 'source': graph.nodes[su].get('label', su), 'target': graph.nodes[sv].get('label', sv), 'source_files': [graph.nodes[su].get('source_file', ''), graph.nodes[sv].get('source_file', '')], 'relation': rel, 'why': '; '.join(reasons) if reasons else 'cross-file semantic connection'})

    cands.sort(key=lambda x: x['_score'], reverse=True)

    for c in cands:
        c.pop('_score')

    if cands:
        return cands[:limit]

    return commsurprises(graph, comms, limit)


def god(graph: nx.Graph, limit: int = 10, perc: float | None = None) -> list[dict]:
    deg = dict(graph.degree())
    thresh: float | None = None

    if perc is not None:
        degs = sorted(deg.values())

        if degs:
            idx = max(0, int(len(degs) * perc / 100) - 1)
            thresh = degs[idx]

    ordered = sorted(deg.items(), key=lambda x: x[1], reverse=True)
    res = []

    for nid, d in ordered:

        if thresh is not None and d > thresh:
            continue

        if isfile(graph, nid) or isconcept(graph, nid) or isjson(graph, nid):
            continue

        if graph.nodes[nid].get('label', '') in NOISE:
            continue

        res.append({'id': nid, 'label': graph.nodes[nid].get('label', nid), 'degree': d})

        if len(res) >= limit:
            break

    return res


def surprises(graph: nx.Graph, comms: dict[int, list[str]] | None = None, limit: int = 5) -> list[dict]:
    srcs = {data.get('source_file', '') for _, data in graph.nodes(data=True) if data.get('source_file', '')}

    if len(srcs) > 1:
        return filesurprises(graph, comms or {}, limit)

    return commsurprises(graph, comms or {}, limit)


def questions(graph: nx.Graph, comms: dict[int, list[str]], labels: dict[int, str], limit: int = 7) -> list[dict]:

    if labels:
        labels = {int(k) if isinstance(k, str) else k: v for k, v in labels.items()}

    res = []
    cmap = commap(comms)


    if graph.number_of_edges() > 0:
        k = min(100, graph.number_of_nodes()) if graph.number_of_nodes() > 1000 else None
        bet = nx.betweenness_centrality(graph, k=k, seed=42)
        bridges = sorted([(n, s) for n, s in bet.items() if not isfile(graph, n) and not isconcept(graph, n) and s > 0], key=lambda x: x[1], reverse=True)[:3]

        for nid, bscore in bridges:
            lbl = graph.nodes[nid].get('label', nid)
            cid = cmap.get(nid)
            clbl = labels.get(cid, f'Community {cid}') if cid is not None else 'unknown'
            neighs = list(graph.neighbors(nid))
            ncomms = {cmap.get(n) for n in neighs if cmap.get(n) != cid}

            if ncomms:
                olbls = [labels.get(c, f'Community {c}') for c in ncomms]
                res.append({'type': 'bridge_node', 'question': f"Why does `{lbl}` connect `{clbl}` to {', '.join((f'`{l}`' for l in olbls))}?", 'why': f'High betweenness centrality ({bscore:.3f}) - this node is a cross-community bridge.'})


    isolated = [n for n in graph.nodes() if graph.degree(n) <= 1 and not isfile(graph, n) and not isconcept(graph, n) and not isast(graph, n)]

    if isolated:
        isolabels = [graph.nodes[n].get('label', n) for n in isolated[:3]]
        res.append({'type': 'isolated_nodes', 'question': f"What connects {', '.join((f'`{l}`' for l in isolabels))} to the rest of the system?", 'why': f'{len(isolated)} weakly-connected nodes found - possible documentation gaps or missing edges.'})

    for cid, nodes in comms.items():
        cscore = cohesion(graph, nodes)

        if cscore < 0.15 and len(nodes) >= 5:
            lbl = labels.get(cid, f'Community {cid}')
            res.append({'type': 'low_cohesion', 'question': f'Should `{lbl}` be split into smaller, more focused modules?', 'why': f'Cohesion score {cscore} - nodes in this community are weakly interconnected.'})

    if not res:
        return [{'type': 'no_signal', 'question': None, 'why': 'Not enough signal to generate questions. All communities are tightly cohesive.'}]

    bytype: dict[str, list[dict]] = {}

    for q in res:
        bytype.setdefault(q['type'], []).append(q)

    diversified = [q for row in zip_longest(*bytype.values()) for q in row if q is not None]

    return diversified[:limit]


def diff(old: nx.Graph, new: nx.Graph) -> dict:
    oldnodes = set(old.nodes())
    newnodes = set(new.nodes())
    addednodes = newnodes - oldnodes
    removednodes = oldnodes - newnodes
    newlist = [{'id': n, 'label': new.nodes[n].get('label', n)} for n in addednodes]
    remlist = [{'id': n, 'label': old.nodes[n].get('label', n)} for n in removednodes]

    def edgekey(graph: nx.Graph, u: str, v: str, data: dict) -> tuple:

        if graph.is_directed():
            return (u, v, data.get('relation', ''))

        return (min(u, v), max(u, v), data.get('relation', ''))

    def edgedir(data: dict) -> tuple | None:

        if '_src' in data and '_tgt' in data:
            return (data['_src'], data['_tgt'])

        return None

    olddirs = {edgekey(old, u, v, d): edgedir(d) for u, v, d in old.edges(data=True)}
    newdirs = {edgekey(new, u, v, d): edgedir(d) for u, v, d in new.edges(data=True)}
    oldkeys = set(olddirs)
    newkeys = set(newdirs)
    revkeys = {k for k in oldkeys & newkeys if olddirs[k] and newdirs[k] and (olddirs[k] != newdirs[k])}
    addkeys = newkeys - oldkeys | revkeys
    remkeys = oldkeys - newkeys | revkeys
    addededges = []

    for u, v, d in new.edges(data=True):

        if edgekey(new, u, v, d) in addkeys:
            addededges.append({'source': d.get('_src', u), 'target': d.get('_tgt', v), 'relation': d.get('relation', '')})

    removededges = []

    for u, v, d in old.edges(data=True):

        if edgekey(old, u, v, d) in remkeys:
            removededges.append({'source': d.get('_src', u), 'target': d.get('_tgt', v), 'relation': d.get('relation', '')})

    parts = []

    if newlist:
        parts.append(f"{len(newlist)} new node{('s' if len(newlist) != 1 else '')}")

    if addededges:
        parts.append(f"{len(addededges)} new edge{('s' if len(addededges) != 1 else '')}")

    if remlist:
        parts.append(f"{len(remlist)} node{('s' if len(remlist) != 1 else '')} removed")

    if removededges:
        parts.append(f"{len(removededges)} edge{('s' if len(removededges) != 1 else '')} removed")

    summary = ', '.join(parts) if parts else 'no changes'

    return {'new_nodes': newlist, 'removed_nodes': remlist, 'new_edges': addededges, 'removed_edges': removededges, 'summary': summary}


def cycles(graph: nx.Graph, maxlen: int = 5, limit: int = 20) -> list[dict]:

    def fileloc(nid: str) -> str:
        attrs = graph.nodes.get(nid, {})
        sf = attrs.get('source_file', '')

        return sf if isinstance(sf, str) else ''

    fgraph = nx.DiGraph()

    for u, v, data in graph.edges(data=True):
        rel = data.get('relation', '')

        if rel not in ('imports_from', 're_exports'):
            continue

        if data.get('deferred') or data.get('type_only'):
            continue

        sf = data.get('source_file', '')

        if not isinstance(sf, str) or not sf:
            continue

        ufile = fileloc(u)
        vfile = fileloc(v)

        if ufile == sf:
            tgtfile = vfile
        elif vfile == sf:
            tgtfile = ufile
        else:
            tgtfile = vfile if vfile and vfile != sf else ufile

        if not tgtfile:
            continue

        fgraph.add_edge(sf, tgtfile)

    if not fgraph.edges():
        return []

    found: list[list[str]] = []

    for c in nx.simple_cycles(fgraph, length_bound=maxlen):

        if len(c) <= maxlen:
            found.append(c)

        if len(found) >= limit * 10:
            break

    found.sort(key=len)
    seen: set[tuple[str, ...]] = set()
    uniq: list[list[str]] = []

    for c in found:
        core = list(c)

        if not core:
            continue

        idx = core.index(min(core))
        norm = tuple(core[idx:] + core[:idx])

        if norm not in seen:
            seen.add(norm)
            uniq.append(list(norm))

            if len(uniq) >= limit:
                break

    return [{'cycle': c, 'length': len(c), 'why': 'circular dependency'} for c in uniq]