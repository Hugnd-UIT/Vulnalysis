from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json as jsonlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata

import networkx as nx
from networkx.readwrite import json_graph

from graphify.analyze import commap
from graphify.build import data
from graphify.paths import budget, replace, touch
from graphify.paths import json as write_json
from graphify.security import check
from graphify.security import label as clean_label


BACKUPS = ['graph.json', 'GRAPH_REPORT.md', '.graphify_labels.json', '.graphify_analysis.json', 'manifest.json', '.graphify_semantic_marker', 'cost.json']
COLORS = ['#4e79a7', '#f28e2b', '#e1575c', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac']
SCHEMA = 1
MALFORMED = object()
CYPHERRE = re.compile(r'[^A-Za-z0-9_]')
XMLILLEGAL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
CONTROLSPACE = re.compile(r'[\x00-\x1f\x7f]')
TAGRE = re.compile(r'^\s*-\s+graphify/\S')
PROBEBYTES = 4096
MARKER = 'FROM #community/'
RESERVE = 5
PREFIX = '_COMMUNITY_'
FTYPETAGS = {'code': 'graphify/code', 'document': 'graphify/document', 'paper': 'graphify/paper', 'image': 'graphify/image'}
CANVASCOLORS = ['1', '2', '3', '4', '5', '6']


def backup(dest: Path) -> Path | None:

    if os.environ.get('GRAPHIFY_NO_BACKUP'):
        return None

    out = Path(dest)

    if not (out / 'graph.json').exists():
        return None

    semantic = (out / '.graphify_semantic_marker').exists()
    curated = False
    lblfile = out / '.graphify_labels.json'

    if lblfile.exists():

        try:
            labels = jsonlib.loads(lblfile.read_text(encoding='utf-8'))
            curated = any((v != f'Community {k}' for k, v in labels.items()))
        except Exception:
            pass

    if not semantic and not curated:
        return None

    reason = '+'.join(filter(None, ['semantic' if semantic else '', 'curated' if curated else '']))
    today = date.today().isoformat()
    target = out / today
    source = out / 'graph.json'

    if target.exists() and (target / 'graph.json').exists():
        srchash = hashlib.sha256(source.read_bytes()).hexdigest()
        bakhash = hashlib.sha256((target / 'graph.json').read_bytes()).hexdigest()

        if srchash == bakhash:
            return target

    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = 0

        for name in BACKUPS:
            item = out / name

            if item.exists():

                try:
                    shutil.copy2(item, target / name)
                    copied += 1
                except Exception:
                    pass

        if copied:
            print(f'[graphify] backed up {reason} graph ({copied} files) -> {target.name}/')

        return target

    except Exception as exc:
        print(f'[graphify] warning: backup failed ({exc}) - continuing with overwrite', file=sys.stderr)

        return None


def tag(name: str) -> str:
    val = re.sub(r'[^\w\-/]', '', name.replace(' ', '_'))

    if not val.strip('_-/'):
        return 'unnamed'

    if val.isdigit():
        return f'c{val}'

    return val


def diacritics(text: str | None) -> str:

    if not isinstance(text, str):
        text = '' if text is None else str(text)

    nfkd = unicodedata.normalize('NFKD', text)

    return ''.join((c for c in nfkd if not unicodedata.combining(c)))


def yamlstr(val: str) -> str:

    if val is None:
        return ''

    out: list[str] = []

    for ch in str(val):
        cp = ord(ch)

        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif ch == '\x00':
            out.append('\\0')
        elif cp == 8232:
            out.append('\\L')
        elif cp == 8233:
            out.append('\\P')
        elif cp < 32 or cp == 127:
            out.append(f'\\x{cp:02x}')
        else:
            out.append(ch)

    return ''.join(out)


def version() -> str | None:

    try:
        from importlib.metadata import version as v

        return v('graphifyy')
    except Exception:
        return None


def hyperedges(graph: nx.Graph, items: list) -> None:
    existing = graph.graph.get('hyperedges', [])
    seen = {h['id'] for h in existing if h.get('id')}

    for h in items:

        if h.get('id') and h['id'] not in seen:
            existing.append(h)
            seen.add(h['id'])

    graph.graph['hyperedges'] = existing


def githead(loc: str | Path | None = None) -> str | None:

    try:
        r = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=3, cwd=str(loc) if loc is not None else None)

        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def nodecount(loc: str | Path):
    p = Path(loc)

    if not p.exists():
        return None

    try:
        check(p)
    except Exception:
        return None

    try:
        raw = p.read_text(encoding='utf-8')
    except Exception:

        try:
            return MALFORMED if p.stat().st_size > 0 else None
        except Exception:
            return None

    if not raw.strip():
        return None

    try:
        data = jsonlib.loads(raw)
    except Exception:
        return MALFORMED

    nodes = data.get('nodes') if isinstance(data, dict) else None

    return len(nodes) if isinstance(nodes, list) else MALFORMED


def json(graph: nx.Graph, comms: dict[int, list[str]], outpath: str, *, force: bool = False, built_at_commit: str | None = None, community_labels: dict[int, str] | None = None, original_links: list[dict] | None = None) -> bool:
    from graphify.build import shrink

    shrink(graph)
    target = Path(outpath)

    if not force and target.exists():

        try:
            check(target)
            over = False
        except Exception:
            over = True

        if not over:

            try:
                raw = target.read_text(encoding='utf-8')
            except Exception:
                raw = ''

            if not raw.strip():
                prior = 0
            else:

                try:
                    prior = len(jsonlib.loads(raw).get('nodes', []))
                except Exception as err:
                    print(f'[graphify] WARNING: existing {target} could not be read ({err}). Refusing to overwrite; pass force=True.', file=sys.stderr)

                    return False

            cur = graph.number_of_nodes()

            if cur < prior:
                print(f'[graphify] WARNING: new graph has {cur} nodes but existing graph.json has {prior}. Refusing overwrite.', file=sys.stderr)

                return False

    cmap = commap(comms)
    lbls: dict[int, str] = {int(k): v for k, v in (community_labels or {}).items()}

    try:
        data = json_graph.node_link_data(graph, edges='links')
    except TypeError:
        data = json_graph.node_link_data(graph)

    def sortkey(item: dict) -> str:
        return jsonlib.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    for n in data['nodes']:
        cid = cmap.get(n['id'])
        n['community'] = cid

        if cid is not None and lbls:
            n['community_name'] = lbls.get(cid, f'Community {cid}')

        n['norm_label'] = diacritics(n.get('label', '')).lower()

    if original_links is not None:
        ids = {n['id'] for n in data['nodes']}
        data['links'] = [dict(lnk) for lnk in original_links if isinstance(lnk, dict) and lnk.get('source') in ids and lnk.get('target') in ids]

    for lnk in data['links']:
        lnk.pop('confidence', None)
        lnk.pop('confidence_score', None)
        lnk.pop('weight', None)
        lnk.pop('_origin', None)
        tsrc = lnk.pop('_src', None)
        ttgt = lnk.pop('_tgt', None)

        if tsrc is not None and ttgt is not None:
            lnk['source'] = tsrc
            lnk['target'] = ttgt

    def canon(item: dict, lead: tuple[str, ...]) -> dict:
        heads = [k for k in lead if k in item]
        tails = sorted((k for k in item if k not in heads))

        return {k: item[k] for k in (*heads, *tails)}

    data['nodes'] = [canon(n, ('id', 'label')) for n in data['nodes']]
    data['links'] = [canon(lnk, ('source', 'target', 'relation')) for lnk in data['links']]
    data['nodes'].sort(key=sortkey)
    data['links'].sort(key=sortkey)

    hypers = sorted(getattr(graph, 'graph', {}).get('hyperedges', []), key=sortkey)

    if isinstance(data.get('graph'), dict) and 'hyperedges' in data['graph']:
        data['graph']['hyperedges'] = hypers

    data['hyperedges'] = hypers
    meta = data.setdefault('graph', {})
    meta['schema_version'] = SCHEMA
    v = version()

    if v is not None:
        meta['graphify_version'] = v

    commit = built_at_commit if built_at_commit is not None else githead(Path(outpath).resolve().parent)

    if commit:
        data['built_at_commit'] = commit

    write_json(outpath, data, indent=2)

    return True


def prune(data: dict) -> tuple[dict, int]:
    ids = {n['id'] for n in data['nodes']}
    key = 'links' if 'links' in data else 'edges'
    before = len(data[key])
    data[key] = [e for e in data[key] if e['source'] in ids and e['target'] in ids]

    return (data, before - len(data[key]))


def cypherescape(val: str) -> str:
    s = ''.join((ch for ch in val if ch >= ' ' or ch == '\t'))

    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')


def cypherlabel(raw: str, fallback: str) -> str:
    clean = CYPHERRE.sub('', raw or '')

    if not clean or not clean[0].isalpha():
        return fallback

    return clean


def cypher(graph: nx.Graph, outpath: str) -> None:
    lines = ['// Neo4j Cypher import - generated by /graphify', '']

    for nid, data in graph.nodes(data=True):
        lbl = cypherescape(data.get('label', nid))
        nesc = cypherescape(nid)
        ftype = cypherlabel((data.get('file_type', 'unknown') or 'unknown').capitalize(), 'Entity')
        lines.append(f"MERGE (n:{ftype} {{id: '{nesc}', label: '{lbl}'}});")

    lines.append('')

    for u, v, data in graph.edges(data=True):
        u, v = data.get('_src', u), data.get('_tgt', v)
        rel = cypherlabel((data.get('relation', 'RELATES_TO') or 'RELATES_TO').upper(), 'RELATES_TO')
        uesc = cypherescape(u)
        vesc = cypherescape(v)
        lines.append(f"MATCH (a {{id: '{uesc}'}}), (b {{id: '{vesc}'}}) MERGE (a)-[:{rel}]->(b);")

    with open(outpath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def cleanxml(val: str) -> str:
    return XMLILLEGAL.sub('', val)


def capfile(val: str, limit: int = 200) -> str:
    b = val.encode('utf-8')

    if len(b) <= limit:
        return val

    digest = hashlib.sha1(b).hexdigest()[:8]
    trunc = b[:limit - 9].decode('utf-8', 'ignore')

    return f'{trunc}_{digest}'


def isnote(path: Path) -> bool:

    try:
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            head = fh.read(PROBEBYTES)
    except OSError:
        return False

    if path.name.startswith(PREFIX) and MARKER in head:
        return True

    if not head.startswith('---'):
        return False

    for line in head.splitlines()[1:]:

        if line.strip() == '---':
            return False

        if TAGRE.match(line):
            return True

    return False


def adopt(out: Path) -> set[str]:

    try:
        cands = sorted(out.glob('*.md'))
    except OSError:
        return set()

    return {p.name for p in cands if isnote(p)}


def safestem(lbl: str, limit: int = 200) -> str:
    clean = re.sub(r'[\\/*?:"<>|#^[\]]', '', CONTROLSPACE.sub(' ', lbl)).strip()
    clean = re.sub(r'\.(md|mdx|qmd|markdown)$', '', clean, flags=re.IGNORECASE)

    if clean.startswith('.') and re.search(r'\w', clean.lstrip('.'), flags=re.UNICODE):
        clean = 'dot-' + clean.lstrip('.')

    if not re.search(r'\w', clean, flags=re.UNICODE):
        return 'unnamed'

    return capfile(clean, limit)


def dedupfiles(graph: nx.Graph, stemfn) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()

    for nid, data in graph.nodes(data=True):
        base = stemfn(data.get('label', nid))
        cand = base
        idx = 1

        while cand.lower() in used:
            cand = f'{base}_{idx}'
            idx += 1

        used.add(cand.lower())
        names[nid] = cand

    return names


def reach(graph: nx.Graph, nid: str, cmap: dict[str, int]) -> int:
    cur = cmap.get(nid)
    nbrs = {cmap[nb] for nb in graph.neighbors(nid) if nb in cmap and cmap[nb] != cur}

    return len(nbrs)


def commname(cid: int, labels: dict[int, str] | None = None) -> str:
    return labels.get(cid, f'Community {cid}') if labels and cid is not None else f'Community {cid}'


def obsidian(graph: nx.Graph, comms: dict[int, list[str]], outdir: str, community_labels: dict[int, str] | None = None, cohesion: dict[int, float] | None = None) -> int:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / '.graphify_obsidian_manifest.json'

    try:
        owned: set[str] = set(jsonlib.loads(manifest.read_text(encoding='utf-8')).get('files', []))
        existed = True
    except (OSError, ValueError):
        owned = set()
        existed = False

    if not existed:
        owned |= adopt(out)

    written: list[str] = []
    skipped: list[str] = []

    def writefile(rel: str, body: str) -> bool:
        dest = out / rel

        if dest.exists() and rel not in owned:
            skipped.append(rel)

            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        touch(dest, body)
        written.append(rel)

        return True

    cmap = commap(comms)
    stemlimit = budget(out, reserve=RESERVE)
    filenames = dedupfiles(graph, lambda l: safestem(l, stemlimit))
    nodeswritten = 0

    for nid, data in graph.nodes(data=True):
        lbl = data.get('label', nid)
        cid = cmap.get(nid)
        cname = commname(cid, community_labels)
        ftype = data.get('file_type', '')
        ftypetag = FTYPETAGS.get(ftype, f'graphify/{ftype}' if ftype else 'graphify/document')
        conftag = f'graphify/{dominant(graph, nid)}'
        commtag = f'community/{tag(cname)}'
        tags = [ftypetag, conftag, commtag]
        lines = ['---', f'source_file: "{yamlstr(data.get("source_file", ""))}"', f'type: "{yamlstr(ftype)}"', f'community: "{yamlstr(cname)}"']

        if data.get('source_location'):
            lines.append(f'location: "{yamlstr(str(data["source_location"]))}"')

        lines.append('tags:')

        for t in tags:
            lines.append(f'  - {t}')

        lines += ['---', '', f'# {lbl}', '']
        nbrs = list(graph.neighbors(nid))

        if nbrs:
            lines.append('## Connections')

            for nb in sorted(nbrs, key=lambda n: graph.nodes[n].get('label', n)):
                ed = data(graph, nid, nb)
                nblbl = filenames[nb]
                rel = ed.get('relation', '')
                lines.append(f'- [[{nblbl}]] - `{rel}`')

            lines.append('')

        lines.append(' '.join((f'#{t}' for t in tags)))
        fname = filenames[nid] + '.md'

        if writefile(fname, '\n'.join(lines)):
            nodeswritten += 1

    inter: dict[int, dict[int, int]] = {cid: {} for cid in comms}

    for u, v in graph.edges():
        cu, cv = cmap.get(u), cmap.get(v)

        if cu is not None and cv is not None and cu != cv:
            inter.setdefault(cu, {})
            inter.setdefault(cv, {})
            inter[cu][cv] = inter[cu].get(cv, 0) + 1
            inter[cv][cu] = inter[cv].get(cu, 0) + 1

    commfiles: dict = {}
    usedcomms: set[str] = set()
    commlimit = budget(out, reserve=RESERVE + len(PREFIX))

    for cid in comms:
        base = f'{PREFIX}{safestem(commname(cid, community_labels), commlimit)}'
        cand = base
        idx = 1

        while cand.lower() in usedcomms:
            cand = f'{base}_{idx}'
            idx += 1

        usedcomms.add(cand.lower())
        commfiles[cid] = cand

    commswritten = 0

    for cid, allmems in comms.items():
        cname = commname(cid, community_labels)
        mems = [m for m in allmems if m in graph and m in filenames]
        cohval = cohesion.get(cid) if cohesion else None
        lines = ['---', 'type: community']

        if cohval is not None:
            lines.append(f'cohesion: {cohval:.2f}')

        lines += [f'members: {len(mems)}', '---', '', f'# {cname}', '']

        if cohval is not None:
            desc = 'tightly connected' if cohval >= 0.7 else 'moderately connected' if cohval >= 0.4 else 'loosely connected'
            lines.append(f'**Cohesion:** {cohval:.2f} - {desc}')

        lines += [f'**Members:** {len(mems)} nodes', '', '## Members']

        for nid in sorted(mems, key=lambda n: graph.nodes[n].get('label', n)):
            ndata = graph.nodes[nid]
            entry = f'- [[{filenames[nid]}]]'

            if ndata.get('file_type'):
                entry += f' - {ndata["file_type"]}'

            if ndata.get('source_file'):
                entry += f' - {ndata["source_file"]}'

            lines.append(entry)

        lines += ['', '## Live Query (requires Dataview plugin)', '', '```dataview', f'TABLE source_file, type FROM #community/{tag(cname)}', 'SORT file.name ASC', '```', '']
        cross = inter.get(cid, {})

        if cross:
            lines.append('## Connections to other communities')

            for othercid, count in sorted(cross.items(), key=lambda x: -x[1]):
                otherfname = commfiles.get(othercid) or f'{PREFIX}{safestem(commname(othercid, community_labels), commlimit)}'
                lines.append(f"- {count} edge{('s' if count != 1 else '')} to [[{otherfname}]]")

            lines.append('')

        bridges = [(nid, graph.degree(nid), reach(graph, nid, cmap)) for nid in mems if reach(graph, nid, cmap) > 0]
        bridges.sort(key=lambda x: (-x[2], -x[1]))

        if bridges[:5]:
            lines.append('## Top bridge nodes')

            for nid, deg, rch in bridges[:5]:
                lines.append(f"- [[{filenames[nid]}]] - degree {deg}, connects to {rch} {('community' if rch == 1 else 'communities')}")

        if writefile(commfiles[cid] + '.md', '\n'.join(lines)):
            commswritten += 1

    cfg = {'colorGroups': [{'query': f'tag:#community/{tag(l)}', 'color': {'a': 1, 'rgb': int(COLORS[cid % len(COLORS)].lstrip('#'), 16)}} for cid, l in sorted((community_labels or {}).items())]}
    writefile('.obsidian/graph.json', jsonlib.dumps(cfg, indent=2))
    stale = owned - set(written) - set(skipped)
    pruned = 0

    for rel in sorted(stale):
        tgt = (out / rel).resolve()

        if out.resolve() not in tgt.parents:
            continue

        try:
            tgt.unlink(missing_ok=True)
            pruned += 1
        except OSError:
            pass

    if pruned:
        print(f'[graphify] pruned {pruned} note(s) for nodes no longer in graph', file=sys.stderr)

    try:
        manifest.write_text(jsonlib.dumps({'files': sorted(set(written))}, indent=2), encoding='utf-8')
    except OSError:
        pass

    return nodeswritten + commswritten


def canvas(graph: nx.Graph, comms: dict[int, list[str]], outpath: str, community_labels: dict[int, str] | None = None, node_filenames: dict[str, str] | None = None) -> None:
    stemlimit = budget(Path(outpath).parent, reserve=RESERVE)

    if node_filenames is None:
        node_filenames = dedupfiles(graph, lambda l: safestem(l, stemlimit))

    if not comms and graph.number_of_nodes() > 0:
        comms = {0: [str(n) for n in graph.nodes()]}

    total = len(comms)
    cols = math.ceil(math.sqrt(total)) if total > 0 else 1
    rows = math.ceil(total / cols) if total > 0 else 1
    cnodes: list[dict] = []
    cedges: list[dict] = []
    gap = 80
    sortedcids = sorted(comms.keys())
    gsizes: dict[int, tuple[int, int]] = {}
    gcols: dict[int, int] = {}

    for cid in sortedcids:
        mems = [m for m in comms[cid] if m in graph and m in node_filenames]
        icols = max(1, math.ceil(math.sqrt(len(mems))))
        w = max(600, 220 * icols)
        h = max(400, 100 * math.ceil(len(mems) / icols) + 120)
        gsizes[cid] = (w, h)
        gcols[cid] = icols

    cwidths: list[int] = []
    rheights: list[int] = []

    for cidx in range(cols):
        maxw = 0

        for ridx in range(rows):
            linear = ridx * cols + cidx

            if linear < len(sortedcids):
                maxw = max(maxw, gsizes[sortedcids[linear]][0])

        cwidths.append(maxw)

    for ridx in range(rows):
        maxh = 0

        for cidx in range(cols):
            linear = ridx * cols + cidx

            if linear < len(sortedcids):
                maxh = max(maxh, gsizes[sortedcids[linear]][1])

        rheights.append(maxh)

    glayout: dict[int, tuple[int, int, int, int]] = {}

    for idx, cid in enumerate(sortedcids):
        cidx, ridx = idx % cols, idx // cols
        gx = sum(cwidths[:cidx]) + cidx * gap
        gy = sum(rheights[:ridx]) + ridx * gap
        gw, gh = gsizes[cid]
        glayout[cid] = (gx, gy, gw, gh)

    allnodes: set[str] = set()

    for m in comms.values():
        allnodes.update(m)

    for idx, cid in enumerate(sortedcids):
        mems = comms[cid]
        cname = commname(cid, community_labels)
        gx, gy, gw, gh = glayout[cid]
        cnode = {'id': f'g{cid}', 'type': 'group', 'label': cname, 'x': gx, 'y': gy, 'width': gw, 'height': gh, 'color': CANVASCOLORS[idx % len(CANVASCOLORS)]}
        cnodes.append(cnode)
        icols = gcols[cid]
        validmems = sorted([m for m in mems if m in graph and m in node_filenames], key=lambda n: graph.nodes[n].get('label', n))

        for midx, nid in enumerate(validmems):
            c, r = midx % icols, midx // icols
            nxpos = gx + 20 + c * 200
            nypos = gy + 80 + r * 80
            fname = node_filenames.get(nid, safestem(graph.nodes[nid].get('label', nid), stemlimit))
            cnodes.append({'id': f'n_{nid}', 'type': 'file', 'file': f'{fname}.md', 'x': nxpos, 'y': nypos, 'width': 180, 'height': 60})

    wedges: list[tuple[float, str, str, str]] = []

    for u, v, ed in graph.edges(data=True):
        su, sv = ed.get('_src', u), ed.get('_tgt', v)

        if su in allnodes and sv in allnodes:
            rel = ed.get('relation', '')
            wedges.append((su, sv, rel))

    for u, v, lbl in wedges[:200]:
        cedges.append({'id': f'e_{u}_{v}', 'fromNode': f'n_{u}', 'toNode': f'n_{v}', 'label': lbl})

    write_json(outpath, {'nodes': cnodes, 'edges': cedges}, indent=2)


def graphml(graph: nx.Graph, comms: dict[int, list[str]], outpath: str) -> None:
    gcopy = graph.copy()
    cmap = commap(comms)

    for nid in gcopy.nodes():
        gcopy.nodes[nid]['community'] = cmap.get(nid, -1)

    for _, attrs in gcopy.nodes(data=True):

        for k in [k for k in attrs if k.startswith('_')]:
            del attrs[k]

    for _, _, attrs in gcopy.edges(data=True):

        for k in [k for k in attrs if k.startswith('_')]:
            del attrs[k]

    def safeval(val):

        if val is None:
            return ''

        if isinstance(val, (bool, int, float)):
            return val

        if isinstance(val, str):
            return cleanxml(val)

        try:
            return cleanxml(jsonlib.dumps(val, default=str, sort_keys=True))
        except (TypeError, ValueError):
            return cleanxml(str(val))

    remapdict = {n: cleanxml(n) for n in gcopy.nodes if isinstance(n, str)}
    remapdict = {k: v for k, v in remapdict.items() if k != v}

    if remapdict:
        gcopy = nx.relabel_nodes(gcopy, remapdict, copy=True)

    for k, val in list(gcopy.graph.items()):
        gcopy.graph[k] = safeval(val)

    for nid in gcopy.nodes():

        for k, val in list(gcopy.nodes[nid].items()):
            gcopy.nodes[nid][k] = safeval(val)

    for u, v in gcopy.edges():

        for k, val in list(gcopy.edges[u, v].items()):
            gcopy.edges[u, v][k] = safeval(val)

    out = Path(outpath)
    tmp = out.with_name(out.name + '.tmp')

    try:
        nx.write_graphml(gcopy, str(tmp))
        replace(str(tmp), str(out))
    finally:

        if tmp.exists():

            try:
                tmp.unlink()
            except OSError:
                pass


def svg(graph: nx.Graph, comms: dict[int, list[str]], outpath: str, community_labels: dict[int, str] | None = None, figsize: tuple[int, int] = (20, 14)) -> None:

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
    except ImportError as err:
        raise ImportError('matplotlib required') from err

    cmap = commap(comms)
    fig, ax = plt.subplots(figsize=figsize, facecolor='#1a1a2e')
    ax.set_facecolor('#1a1a2e')
    ax.axis('off')
    pos = nx.spring_layout(graph, seed=42, k=2.0 / (graph.number_of_nodes() ** 0.5 + 1))
    deg = dict(graph.degree())
    maxdeg = max(deg.values(), default=1) or 1
    nodecolors = [COLORS[cmap.get(n, 0) % len(COLORS)] for n in graph.nodes()]
    nodesizes = [300 + 1200 * (deg.get(n, 1) / maxdeg) for n in graph.nodes()]

    for u, v, ed in graph.edges(data=True):
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        ax.plot([x0, x1], [y0, y1], color='#aaaaaa', linewidth=0.8, linestyle='solid', alpha=0.6, zorder=1)

    nx.draw_networkx_nodes(graph, pos, ax=ax, node_color=nodecolors, node_size=nodesizes, alpha=0.9)
    nx.draw_networkx_labels(graph, pos, ax=ax, labels={n: cleanxml(str(graph.nodes[n].get('label', n))) for n in graph.nodes()}, font_size=7, font_color='white')

    if community_labels:
        patches = [mpatches.Patch(color=COLORS[cid % len(COLORS)], label=cleanxml(f'{lbl} ({len(comms.get(cid, []))})')) for cid, lbl in sorted(community_labels.items())]
        ax.legend(handles=patches, loc='upper left', framealpha=0.7, facecolor='#2a2a4e', labelcolor='white', fontsize=8)

    plt.tight_layout()
    plt.savefig(outpath, format='svg', bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)


def html(*args, **kwargs) -> bool:
    return False