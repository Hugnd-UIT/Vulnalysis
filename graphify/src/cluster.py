from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import sys

import networkx as nx


def suppress():
    return contextlib.redirect_stdout(io.StringIO())


def native(graph: nx.Graph, res: float) -> dict[str, int] | None:
    try:
        import graspologic_native as gn
    except ImportError:
        return None

    if graph.is_directed() or graph.is_multigraph():
        return None

    lut: dict[str, object] = {}

    for n in graph.nodes():
        k = str(n)
        prev = lut.get(k)

        if prev is not None and prev != n:
            return None

        lut[k] = n

    edges = [(str(u), str(v), float(attrs.get('weight', 1.0))) for u, v, attrs in graph.edges(data=True)]

    try:
        err = sys.stderr

        try:
            sys.stderr = io.StringIO()

            with suppress():
                _q, parts = gn.leiden(edges=edges, starting_communities=None, resolution=res, randomness=0.001, iterations=1, use_modularity=True, seed=42, trials=1)
        finally:
            sys.stderr = err
    except Exception:
        return None

    part = {lut[nid]: cid for nid, cid in parts.items()}
    nxt = max(part.values(), default=-1) + 1

    for n in graph.nodes():
        if n not in part:
            part[n] = nxt
            nxt += 1

    return part


def partition(graph: nx.Graph, res: float = 1.0) -> dict[str, int]:
    base = nx.Graph()
    base.add_nodes_from(sorted(graph.nodes(), key=str))
    rows = sorted(graph.edges(data=True), key=lambda r: (*sorted((str(r[0]), str(r[1]))), json.dumps(r[2], sort_keys=True, ensure_ascii=False, default=str)))

    for u, v, attrs in rows:
        base.add_edge(u, v, **attrs)

    fast = native(base, res)

    if fast is not None:
        return fast

    try:
        from graspologic.partition import leiden
        sig = inspect.signature(leiden).parameters
        opts: dict = {}

        if 'random_seed' in sig:
            opts['random_seed'] = 42

        if 'trials' in sig:
            opts['trials'] = 1

        if 'resolution' in sig:
            opts['resolution'] = res

        err = sys.stderr

        try:
            sys.stderr = io.StringIO()

            with suppress():
                return leiden(base, **opts)
        finally:
            sys.stderr = err
    except ImportError:
        pass

    opts = {'seed': 42, 'threshold': 0.0001, 'resolution': res}

    if 'max_level' in inspect.signature(nx.community.louvain_communities).parameters:
        opts['max_level'] = 10

    groups = nx.community.louvain_communities(base, **opts)

    return {n: cid for cid, group in enumerate(groups) for n in group}


FRACTION = 0.25
MIN_SPLIT = 10
THRESHOLD = 0.05
MIN_SIZE = 50


def labels(graph: nx.Graph, comms: dict[int, list[str]]) -> dict[int, str]:
    lut: dict[int, str] = {}

    for cid, members in comms.items():
        present = [n for n in members if n in graph]

        if not present:
            lut[cid] = f'Community {cid}'
            continue

        hub = min(present, key=lambda n: (-graph.degree(n), str(n)))
        name = str(graph.nodes[hub].get('label') or hub).strip()

        if name.endswith('()'):
            name = name[:-2]

        lut[cid] = name or f'Community {cid}'

    return lut


def signatures(comms: dict[int, list[str]]) -> dict[int, str]:
    sigs: dict[int, str] = {}

    for cid, members in comms.items():
        h = hashlib.sha256()

        for nid in sorted((str(n) for n in members)):
            h.update(nid.encode('utf-8', 'replace'))
            h.update(b'\x00')

        sigs[cid] = h.hexdigest()[:16]

    return sigs


def split(graph: nx.Graph, nodes: list[str]) -> list[list[str]]:
    sub = graph.subgraph(nodes)

    if sub.number_of_edges() == 0:
        return [[n] for n in sorted(nodes)]

    try:
        parts = partition(sub)
        groups: dict[int, list[str]] = {}

        for n, cid in parts.items():
            groups.setdefault(cid, []).append(n)

        if len(groups) <= 1:
            return [sorted(nodes)]

        return [sorted(v) for v in groups.values()]
    except Exception:
        return [sorted(nodes)]


def cohesion(graph: nx.Graph, nodes: list[str]) -> float:
    n = len(nodes)

    if n <= 1:
        return 1.0

    sub = graph.subgraph(nodes)
    real = sub.number_of_edges() - nx.number_of_selfloops(sub)
    total = n * (n - 1) / 2

    return real / total if total > 0 else 0.0


def scores(graph: nx.Graph, comms: dict[int, list[str]]) -> dict[int, float]:
    return {cid: cohesion(graph, nodes) for cid, nodes in comms.items()}


def remap(comms: dict[int, list[str]], prev: dict[str, int]) -> dict[int, list[str]]:
    if not comms:
        return {}

    nsets = {cid: set(nodes) for cid, nodes in comms.items()}
    osets: dict[int, set[str]] = {}

    for n, cid in prev.items():
        osets.setdefault(cid, set()).add(n)

    overlaps: list[tuple[int, int, int]] = []

    for ocid, onodes in osets.items():
        for ncid, nnodes in nsets.items():
            match = len(onodes & nnodes)

            if match > 0:
                overlaps.append((match, ocid, ncid))

    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))
    mapping: dict[int, int] = {}
    used: set[int] = set()
    matched: set[int] = set()

    for _m, ocid, ncid in overlaps:
        if ocid in used or ncid in matched:
            continue

        mapping[ncid] = ocid
        used.add(ocid)
        matched.add(ncid)

    unmatched = [cid for cid in comms if cid not in matched]
    unmatched.sort(key=lambda cid: (-len(comms[cid]), tuple(sorted(comms[cid]))))
    nxt = 0

    for ncid in unmatched:
        while nxt in used:
            nxt += 1

        mapping[ncid] = nxt
        used.add(nxt)
        nxt += 1

    final: dict[int, list[str]] = {}

    for ncid, nodes in comms.items():
        final[mapping[ncid]] = sorted(nodes)

    return dict(sorted(final.items(), key=lambda kv: kv[0]))


def cluster(graph: nx.Graph, res: float = 1.0, percentile: float | None = None, scale: float = 0.5) -> dict[int, list[str]]:
    if graph.number_of_nodes() == 0:
        return {}

    if graph.is_directed():
        graph = graph.to_undirected()

    if graph.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(graph.nodes))}

    hubs: set[str] = set()

    if percentile is not None:
        degrees = sorted((d for _, d in graph.degree()))

        if degrees:
            idx = max(0, int(len(degrees) * percentile / 100) - 1)
            cutoff = degrees[idx]
            hubs = {n for n, d in graph.degree() if d > cutoff}

    isolates = [n for n in graph.nodes() if graph.degree(n) == 0 and n not in hubs]
    active = [n for n in graph.nodes() if graph.degree(n) > 0 and n not in hubs]
    stranded: list[str] = []

    if hubs:
        stranded = [n for n in active if (nbs := [nb for nb in graph.neighbors(n) if nb != n]) and all((nb in hubs for nb in nbs))]

        if stranded:
            s_set = set(stranded)
            active = [n for n in active if n not in s_set]

    connected = graph.subgraph(active)
    raw: dict[int, list[str]] = {}

    if connected.number_of_nodes() > 0:
        part = partition(connected, res=res)

        for n, cid in part.items():
            raw.setdefault(cid, []).append(n)

    nxt_cid = max(raw.keys(), default=-1) + 1

    for n in isolates:
        raw[nxt_cid] = [n]
        nxt_cid += 1

    if hubs:
        lookup: dict[str, int] = {n: cid for cid, nodes in raw.items() for n in nodes}

        for hub in sorted(hubs):
            votes: dict[int, int] = {}

            for nb in graph.neighbors(hub):
                cid = lookup.get(nb)

                if cid is not None:
                    votes[cid] = votes.get(cid, 0) + 1

            if votes:
                best = min(votes, key=lambda c: (-votes[c], c))
                raw.setdefault(best, []).append(hub)
                lookup[hub] = best
            else:
                raw[nxt_cid] = [hub]
                lookup[hub] = nxt_cid
                nxt_cid += 1

        for n in sorted(stranded, key=str):
            votes = {}

            for nb in graph.neighbors(n):
                if nb == n:
                    continue

                cid = lookup.get(nb)
                votes[cid] = votes.get(cid, 0) + 1

            best = min(votes, key=lambda c: (-votes[c], c))
            raw[best].append(n)
            lookup[n] = best

    cap = max(MIN_SPLIT, int(graph.number_of_nodes() * FRACTION))
    expanded: list[list[str]] = []

    for nodes in raw.values():
        if len(nodes) > cap:
            expanded.extend(split(graph, nodes))
        else:
            expanded.append(nodes)

    cleaned: list[list[str]] = []

    for nodes in expanded:
        if len(nodes) >= MIN_SIZE and cohesion(graph, nodes) < THRESHOLD:
            subs = split(graph, nodes)
            cleaned.extend(subs if len(subs) > 1 else [nodes])
        else:
            cleaned.append(nodes)

    cleaned.sort(key=lambda nodes: (-len(nodes), tuple(sorted(map(str, nodes)))))

    return {i: sorted(nodes) for i, nodes in enumerate(cleaned)}