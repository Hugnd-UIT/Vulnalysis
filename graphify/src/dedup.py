from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
import re
import sys
import unicodedata

from graphify.ids import normalize
from graphify.minhash import MinHash, MinHashLSH
from rapidfuzz.distance import DamerauLevenshtein, Jaro, JaroWinkler


VARIANTSUFFIX = re.compile(r'^(.*[a-z])([0-9]+[a-z]*|[a-z]{2,})$')
DIGITRUN = re.compile(r'\d+')
STOPWORDS = frozenset({'a', 'an', 'the', 'and', 'or', 'of', 'for', 'to', 'in', 'on', 'at', 'by', 'with', 'from', 'as', 'is', 'are', 'be', 'this', 'that', 'its'})
ANCHORED = frozenset({'document'})
ENTROPYTHRESH = 2.5
LSHTHRESH = 0.7
MERGETHRESH = 92.0
COMMUNITYBOOST = 5.0
NUMPERM = 128
CHUNKSUFFIX = re.compile(r'_c\d+$')
EXTREGEX = re.compile(r'\.[^./]+$')
STRUCTUREKINDS = frozenset({'page', 'heading'})
ACTIVEPATHS = frozenset({'in-progress', 'in_progress', 'active', 'current', 'wip'})
ARCHIVEPATHS = frozenset({'_done', 'done', 'archive', 'archived', 'backup', 'bak', 'old', 'attic', 'graveyard', 'completed'})
RICHNESSIGNORED = frozenset({'id', 'label', 'norm_label', 'file_type', 'source_file', 'source_location'})


def norm(lbl: str | None) -> str:

    if not isinstance(lbl, str):
        lbl = '' if lbl is None else str(lbl)

    lbl = unicodedata.normalize('NFKC', lbl)

    return re.sub(r'[\W_]+', ' ', lbl.casefold(), flags=re.UNICODE).strip()


def entropy(lbl: str) -> float:
    s = norm(lbl)

    if not s:
        return 0.0

    freq: dict[str, int] = defaultdict(int)

    for ch in s:
        freq[ch] += 1

    n = len(s)

    return -sum((c / n * math.log2(c / n) for c in freq.values()))


def shingles(text: str, k: int = 3) -> set[str]:

    if len(text) < k:
        return {text}

    return {text[i:i + k] for i in range(len(text) - k + 1)}


def minhash(text: str, perm: int = 128) -> MinHash:
    m = MinHash(num_perm=perm)

    for item in shingles(text.replace(' ', '')):
        m.update(item.encode('utf-8'))

    return m


def variant(a: str, b: str) -> bool:

    if a == b:
        return False

    if max(len(a), len(b)) >= 12:
        return False

    ma, mb = VARIANTSUFFIX.match(a), VARIANTSUFFIX.match(b)

    if not (ma and mb):
        return False

    return ma.group(1) == mb.group(1) and ma.group(2) != mb.group(2)


def short(a: str, b: str, scoreval: float) -> bool:

    if max(len(a), len(b)) >= 12:
        return False

    if scoreval >= 97.0 and len(a) == len(b) and DamerauLevenshtein.distance(a, b) <= 1:
        return False

    return True


def numeric(a: str, b: str) -> bool:

    if a == b:
        return False

    return sorted((t.lstrip('0') or '0' for t in DIGITRUN.findall(a))) != sorted((t.lstrip('0') or '0' for t in DIGITRUN.findall(b)))


def word(x: str, y: str) -> bool:

    if len(x) == len(y) and DamerauLevenshtein.distance(x, y) <= 1:
        return True

    if min(len(x), len(y)) < 6:
        return False

    return JaroWinkler.normalized_similarity(x, y) * 100 >= MERGETHRESH


def swap(a: str, b: str) -> bool:
    ta, tb = a.split(), b.split()

    if len(ta) != len(tb):
        return False

    for x, y in zip(ta, tb):

        if x == y:
            continue

        if x in STOPWORDS or y in STOPWORDS:
            continue

        if word(x, y):
            continue

        return True

    return False


def anchored(first: dict, second: dict) -> bool:

    if first.get('file_type') not in ANCHORED and second.get('file_type') not in ANCHORED:
        return False

    return (first.get('source_file') or '') != (second.get('source_file') or '')


class UF:

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)

        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]

        return x

    def union(self, x: str, y: str) -> None:
        self.parent.setdefault(x, x)
        self.parent.setdefault(y, y)
        rx, ry = self.find(x), self.find(y)

        if rx != ry:
            self.parent[ry] = rx

    def components(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)

        for x in self.parent:
            groups[self.find(x)].append(x)

        return dict(groups)


def iscode(node: dict) -> bool:
    return node.get('file_type') == 'code'


def prefixes(src: str) -> set[str]:
    stem = EXTREGEX.sub('', src.replace('\\', '/'))
    segs = [s for s in (normalize(p) for p in stem.split('/')) if s]

    return {'_'.join(segs[i:]) for i in range(len(segs))}


def defines(node: dict) -> bool:
    nid = node.get('id') or ''
    src = node.get('source_file') or ''

    if not nid or not src:
        return False

    return any((nid == pref or nid.startswith(f'{pref}_') for pref in prefixes(src)))


def isfile(node: dict) -> bool:
    nid = node.get('id') or ''
    src = node.get('source_file') or ''

    if not nid or not src:
        return False

    if node.get('node_kind') in STRUCTUREKINDS:
        return False

    return nid not in prefixes(src)


def penalty(loc: str) -> int:
    segs = [s for s in loc.casefold().split('/') if s]
    marks = [0 if s in ACTIVEPATHS else 2 for s in segs[:-1] if s in ACTIVEPATHS or s in ARCHIVEPATHS]

    return min(marks) if marks else 1


def rank(src: str, root: Path | None) -> str:
    normpath = src.replace('\\', '/')

    if root is not None and normpath:

        try:
            return Path(normpath).resolve().relative_to(root).as_posix()
        except Exception:
            pass

    return normpath


def collision(node: dict, root: Path | None = None) -> tuple:
    lbl = node.get('label') or ''
    rpath = rank(node.get('source_file') or '', root)

    return (not defines(node), penalty(rpath), len(lbl), lbl, tuple(reversed([s for s in rpath.split('/') if s and s != '.'])))


def same(first: dict, second: dict) -> bool:
    f1 = first.get('source_file') or ''
    f2 = second.get('source_file') or ''

    return bool(f1) and f1 == f2


def merge(first: dict, second: dict) -> dict:
    res = dict(first)

    for k, val in second.items():
        if val is None:
            continue

        if res.get(k) is None:
            res[k] = val

    return res


def report(nid: str, survivor: dict, losers: list[dict]) -> None:
    f1 = survivor.get('source_file') or ''
    l1 = survivor.get('label') or ''

    for loser in losers:
        f2 = loser.get('source_file') or ''
        l2 = loser.get('label') or ''

        if f2 == f1:

            if norm(l2) != norm(l1):
                print(f"[graphify] note: node '{nid}' was extracted twice from '{f1}' under different labels — keeping '{l1}', dropping '{l2}'.", file=sys.stderr)

        elif defines(survivor) and not defines(loser):
            continue

        else:
            print(f"[graphify] WARNING: node '{nid}' is minted by two different files — keeping '{l1}' from '{f1}', dropping '{l2}' from '{f2}'. An ID is derived from the source path plus the entity name, so this one does not identify a single entity and the dropped node is lost. To keep them distinct, run 'graphify extract' per subfolder and merge with 'graphify merge-graphs'.", file=sys.stderr)


def remap(edges: list[dict], mapping: dict[str, str]) -> None:

    for he in edges:

        if not isinstance(he, dict):
            continue

        members = he.get('nodes')

        if not isinstance(members, list):
            continue

        seen: set = set()
        rewired: list = []

        for m in members:

            if isinstance(m, str):
                newid = mapping.get(m, m)
                entry = newid
            elif isinstance(m, dict):
                raw = m.get('id')
                newid = mapping.get(raw, raw) if isinstance(raw, str) else raw
                entry = dict(m, id=newid) if newid != raw else m
            else:
                newid, entry = None, m

            if isinstance(newid, str):

                if newid in seen:
                    continue

                seen.add(newid)

            rewired.append(entry)

        he['nodes'] = rewired


def richness(node: dict) -> int:
    score = 0

    for k, val in node.items():

        if k in RICHNESSIGNORED:
            continue

        if val is None or val == '' or val == [] or val == {}:
            continue

        score += 1

        if k == 'attributes' and isinstance(val, dict):
            score += len(val)
        elif k == '_merged_from' and isinstance(val, list):
            score += len(val)

    return score


def winner(nodes: list[dict]) -> dict:

    if not nodes:
        raise ValueError('Cannot pick winner from empty list')

    def calc(n: dict) -> tuple[int, int, int, int, int]:
        hassuf = bool(CHUNKSUFFIX.search(n['id']))
        hassrc = bool(n.get('source_file'))
        nosrc = 0 if hassrc else 1
        noloc = 0 if hassrc and n.get('source_location') else 1

        return (1 if hassuf else 0, nosrc, noloc, -richness(n), len(n['id']))

    return min(nodes, key=calc)


def dedupe(nodes: list[dict], edges: list[dict], *, communities: dict[str, int], dedup_llm_backend: str | None = None, root: str | Path | None = None, hyperedges: list[dict] | None = None, protected_ids: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    repos = {n.get('repo') for n in nodes if n.get('repo')}

    if len(repos) > 1:
        raise ValueError(f'deduplicate: nodes span multiple repos {sorted(repos)!r}. Cross-project dedup is disabled — run dedup per-repo before merging.')

    if len(nodes) <= 1:
        return (nodes, edges)

    try:
        realroot: Path | None = Path(root).resolve() if root else None
    except Exception:
        realroot = None

    seen: dict[str, dict] = {}
    dropped: dict[str, list[dict]] = defaultdict(list)

    for n in nodes:
        nid = n.get('id', '')

        if not nid:
            continue

        cur = seen.get(nid)

        if cur is None:
            seen[nid] = n
        elif collision(n, realroot) < collision(cur, realroot):
            seen[nid] = n
            dropped[nid].append(cur)
        else:
            dropped[nid].append(n)

    for nid, losers in dropped.items():
        surv = seen[nid]
        samelist = sorted((l for l in losers if same(surv, l)), key=lambda l: collision(l, realroot))

        for l in samelist:
            surv = merge(surv, l)

        seen[nid] = surv

    for nid, losers in dropped.items():
        report(nid, seen[nid], losers)

    uniq = list(seen.values())

    if len(uniq) <= 1:
        return (uniq, edges)

    bykey: dict[str, list[dict]] = defaultdict(list)

    for n in uniq:

        if iscode(n):
            continue

        k = norm(n.get('label', n.get('id', '')))

        if k:
            bykey[k].append(n)

    uf = UF()
    exacts = 0
    protected: set[str] = set(protected_ids) if protected_ids is not None else set()
    protmap: dict[str, str] = {pid: pid for pid in protected}

    def getprot(nid: str) -> str | None:
        return protmap.get(uf.find(nid))

    def unionprot(x: str, y: str) -> None:
        px = getprot(x)
        py = getprot(y)
        uf.union(x, y)
        newroot = uf.find(x)
        p = px or py

        if p is not None:
            protmap[newroot] = p

    def unionfiles(left: dict, right: dict, *, withprot: bool) -> None:
        nonlocal exacts

        if anchored(left, right):
            return

        lid = left['id']
        rid = right['id']

        if withprot:
            px = getprot(lid)
            py = getprot(rid)

            if px is not None and py is not None and px != py:
                return

        if uf.find(lid) == uf.find(rid):
            return

        if withprot:
            unionprot(lid, rid)
        else:
            uf.union(lid, rid)

        exacts += 1

    def joinfiles(members: list[dict], *, withprot: bool) -> None:

        for i, left in enumerate(members):

            for right in members[i + 1:]:
                unionfiles(left, right, withprot=withprot)

    for k, group in bykey.items():

        if len(group) <= 1:
            continue

        byfile: dict[str, list[dict]] = defaultdict(list)

        for n in group:
            sf = n.get('source_file') or ''
            byfile[sf].append(n)

        for sf, fgroup in byfile.items():

            if not sf:
                continue

            if len(fgroup) > 1:

                if protected:
                    protf = [n for n in fgroup if n.get('id') in protected]
                    incf = [n for n in fgroup if n.get('id') not in protected]

                    if protf and not incf:
                        continue

                    if protf and incf:
                        win = winner(protf)

                        for n in incf:
                            px = getprot(win['id'])
                            py = getprot(n['id'])

                            if px is not None and py is not None and px != py:
                                continue

                            if uf.find(win['id']) != uf.find(n['id']):
                                unionprot(win['id'], n['id'])
                                exacts += 1

                    else:
                        win = winner(fgroup)

                        for n in fgroup:

                            if uf.find(win['id']) != uf.find(n['id']):
                                unionprot(win['id'], n['id'])
                                exacts += 1

                else:
                    win = winner(fgroup)

                    for n in fgroup:
                        uf.union(win['id'], n['id'])

                    exacts += len(fgroup) - 1

        mergeable = sorted((n for n in group if (n.get('file_type') == 'concept' or (n.get('file_type') in ANCHORED and isfile(n))) and (n.get('source_file') or '') and (entropy(n.get('label', '')) >= ENTROPYTHRESH)), key=lambda n: n['id'])

        if len(mergeable) > 1:

            if protected:
                protm = [n for n in mergeable if n.get('id') in protected]
                incm = [n for n in mergeable if n.get('id') not in protected]

                if not incm:
                    continue

                if protm:
                    canwin = winner(protm)

                    for inc in incm:
                        unionfiles(canwin, inc, withprot=True)

                    joinfiles(incm, withprot=True)

                else:
                    joinfiles(incm, withprot=True)

            else:
                joinfiles(mergeable, withprot=False)

    cands: list[dict] = []
    seennorms: set[str] = set()

    for n in uniq:

        if iscode(n):
            continue

        k = norm(n.get('label', n.get('id', '')))

        if k and k not in seennorms:
            seennorms.add(k)

            if entropy(n.get('label', '')) >= ENTROPYTHRESH:
                cands.append(n)

    fuzzies = 0

    if len(cands) >= 2:
        lsh = MinHashLSH(threshold=LSHTHRESH, num_perm=NUMPERM)
        hashes: dict[str, MinHash] = {}
        candsbyid: dict[str, dict] = {}
        normcache: dict[str, str] = {}

        for n in cands:
            nid = n['id']
            candsbyid[nid] = n
            nl = norm(n.get('label', n.get('id', '')))
            normcache[nid] = nl
            m = minhash(nl)
            hashes[nid] = m

            try:
                lsh.insert(nid, m)
            except ValueError:
                pass

        for n in cands:
            nid = n['id']
            nl = normcache[nid]
            neighs = lsh.query(hashes[nid])

            for neighid in neighs:

                if neighid == nid:
                    continue

                if uf.find(nid) == uf.find(neighid):
                    continue

                if protected:
                    px = getprot(nid)
                    py = getprot(neighid)

                    if px is not None and py is not None and px != py:
                        continue

                neigh = candsbyid.get(neighid)

                if neigh is None:
                    continue

                neighnorm = normcache.get(neighid) or norm(neigh.get('label', neigh.get('id', '')))
                crosssrc = (n.get('source_file') or '') != (neigh.get('source_file') or '')

                if crosssrc and max(len(nl), len(neighnorm)) >= 12:
                    sc = Jaro.normalized_similarity(nl, neighnorm) * 100
                else:
                    sc = JaroWinkler.normalized_similarity(nl, neighnorm) * 100

                if variant(nl, neighnorm):
                    continue

                if short(nl, neighnorm, sc):
                    continue

                lo, hi = sorted((nl, neighnorm), key=len)

                if hi.startswith(lo) and hi != lo:
                    continue

                if numeric(nl, neighnorm):
                    continue

                if swap(nl, neighnorm):
                    continue

                if anchored(n, neigh):
                    continue

                c1 = communities.get(nid)
                c2 = communities.get(neighid)

                if c1 is not None and c2 is not None and c1 == c2 and min(len(nl), len(neighnorm)) >= 12:
                    sc += COMMUNITYBOOST

                if sc >= MERGETHRESH:

                    if nl == neighnorm:
                        sa = n.get('source_file') or ''
                        sb = neigh.get('source_file') or ''

                        if sa != sb:
                            continue

                    if protected:
                        px = getprot(nid)
                        py = getprot(neighid)

                        if px is not None and py is not None and px != py:
                            continue

                        if nid in protected:
                            win = n
                        elif neighid in protected:
                            win = neigh
                        else:
                            win = winner([n, neigh])

                        unionprot(win['id'], nid)
                        unionprot(win['id'], neighid)

                    else:
                        win = winner([n, neigh])
                        uf.union(win['id'], nid)
                        uf.union(win['id'], neighid)

                    fuzzies += 1

    comps = uf.components()
    remapdict: dict[str, str] = {}
    nodesbyid: dict[str, tuple[int, dict]] = {n['id']: (i, n) for i, n in enumerate(uniq)}
    enriched: dict[str, dict] = {}

    for rootid, members in comps.items():

        if len(members) == 1:
            continue

        groupnodes = [n for _, n in sorted((nodesbyid[m] for m in members if m in nodesbyid), key=lambda pair: pair[0])]

        if protected:
            protgroup = [n for n in groupnodes if n.get('id') in protected]

            if protgroup:
                win = winner(protgroup)
            else:
                win = winner(groupnodes) if groupnodes else {'id': rootid}

        else:
            win = winner(groupnodes) if groupnodes else {'id': rootid}

        winid = win['id']
        mergednode = win

        for n in groupnodes:

            if n['id'] != winid:
                mergednode = merge(mergednode, n)

        if mergednode != win:
            enriched[winid] = mergednode

        for m in members:

            if m != winid:
                remapdict[m] = winid

    if not remapdict:
        return (uniq, edges)

    total = len(remapdict)
    msg = f'[graphify] Deduplicated {total} node(s)'
    parts: list[str] = []

    if exacts:
        parts.append(f'{exacts} exact')

    if fuzzies:
        parts.append(f'{fuzzies} fuzzy')

    if parts:
        msg += f" ({', '.join(parts)})"

    print(msg + '.', flush=True)

    if hyperedges:
        remap(hyperedges, remapdict)

    dedupnodes = [enriched.get(n['id'], n) for n in uniq if n['id'] not in remapdict]
    dedupedges = []

    for edge in edges:
        e = dict(edge)
        src = e['source'] if 'source' in e else e.get('from')
        tgt = e['target'] if 'target' in e else e.get('to')

        if src is None or tgt is None:
            continue

        e['source'] = remapdict.get(src, src)
        e['target'] = remapdict.get(tgt, tgt)
        e.pop('from', None)
        e.pop('to', None)

        if e['source'] != e['target'] or src == tgt:
            dedupedges.append(e)

    return (dedupnodes, dedupedges)