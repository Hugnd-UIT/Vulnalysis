from __future__ import annotations

import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote

from graphify.base import GLOBALS, stem, make, text
from graphify.detect import CODE_EXTENSIONS, DOC_EXTENSIONS, classify
from graphify.security import metadata


INLINE = re.compile(r'(?<!\!)\[[^\]]*\]\(\s*(?:<([^<>\n]+)>\s*(?:"[^"\n]*"|\'[^\'\n]*\'|\([^()\n]*\))?\s*|<?([^)\s>]+)>?(?:\s+[^)]*)?)\)')
REFDEF = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*(?:<([^<>\n]+)>(?=\s*(?:["\'(]|$))|<?([^\s>]+)>?)')
WIKILINK = re.compile(r'(?<!\!)\[\[([^\]|#]+?)(?:\\?\|[^\]]*|#[^\]]*)?\]\]')
LINKABLE = {'.md', '.mdx', '.qmd', '.markdown', '.rst', '.txt'}
CODESPAN = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
PATHMENTION = re.compile(r'^([A-Za-z0-9_./\-]+\.[A-Za-z0-9]+)::([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)(?:\(\))?$')
BAREMENTION = re.compile(r'^(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*)(?:\(\))?$')
CLOSE = ('---', '...')
LIMIT = 200
SCALAR = re.compile(r'^([A-Za-z0-9_][A-Za-z0-9_\-. ]*):\s*(.*)$')
SUFFIXES = frozenset({'.md', '.mdx', '.qmd', '.skill'})
CONTAINMENT = frozenset({'contains', 'method'})
PREFIX = re.compile(r'^(\.\.?/)+')
CACHE: dict[str, dict[str, list[tuple[int, str, Path]]]] = {}


def split(lines: list[str]) -> tuple[list[str], int]:

    if not lines or lines[0].strip() != '---':
        return ([], 0)

    bound = min(len(lines), LIMIT + 1)

    for i in range(1, bound):

        if lines[i].strip() in CLOSE:
            return (lines[1:i], i + 1)

    return ([], 0)


def fallback(lines: list[str]) -> dict:
    out: dict = {}

    for line in lines:

        if not line[:1].strip():
            continue

        match = SCALAR.match(line.strip())

        if not match:
            continue

        k, val = match.group(1).strip(), match.group(2).strip()

        if not val:
            continue

        out[k] = val.strip('"\'')

    return out


def parse(lines: list[str]) -> dict:

    if not lines:
        return {}

    body = '\n'.join(lines)

    try:
        import yaml
    except ImportError:
        return fallback(lines)

    try:
        data = yaml.safe_load(body)
    except Exception:
        return fallback(lines)

    return data if isinstance(data, dict) else {}


def scanroot() -> Path | None:
    import graphify.extract as ext

    return getattr(ext, '_XAML_ACTIVE_EXTRACT_ROOT', None)


def nfc(val: str) -> str:
    return unicodedata.normalize('NFC', val)


def buildindex(loc: Path) -> dict[str, list[tuple[int, str, Path]]]:
    from graphify.detect import _SKIP_DIRS, ignored_predicate

    loc = Path(loc)
    pred = ignored_predicate(loc)
    idx: dict[str, list[tuple[int, str, Path]]] = {}

    for cur, dirs, files in os.walk(loc):
        dp = Path(cur)
        dirs[:] = sorted((d for d in dirs if not d.startswith('.') and d not in _SKIP_DIRS and (not pred(dp / d))))

        for fname in files:

            if Path(fname).suffix.lower() not in LINKABLE:
                continue

            full = dp / fname

            if pred(full):
                continue

            rel = os.path.relpath(str(full), str(loc)).replace('\\', '/')
            idx.setdefault(nfc(fname), []).append((rel.count('/'), nfc(rel), full))

    return idx


def lookup(target: str, loc: Path) -> Path | None:
    key = str(loc)
    idx = CACHE.get(key)

    if idx is None:

        try:
            idx = buildindex(loc)
        except OSError:
            idx = {}

        CACHE[key] = idx

    parts = nfc(target.replace('\\', '/')).split('/')
    hits = idx.get(parts[-1])

    if not hits:
        return None

    tail = '/'.join(parts)
    matches = [h for h in hits if h[1] == tail or h[1].endswith('/' + tail)]

    if not matches:
        return None

    return min(matches)[2]


def decoded(target: str, srcdir: Path) -> str:

    if '%' not in target:
        return target

    dec = unquote(target)

    if dec == target:
        return target

    lit = target if Path(target).suffix else target + '.md'

    try:

        if (srcdir / lit).is_file():
            return target

    except OSError:
        pass

    return dec


def external(target: str) -> bool:
    low = target.lower()

    return '://' in target or low.startswith(('mailto:', 'tel:', '//', 'data:'))


def resolvelink(raw: str, srcdir: Path, wiki: bool = False) -> Path | None:
    target = raw.strip()

    if not target:
        return None

    target = target.split('#', 1)[0].split('?', 1)[0].strip()

    if not target:
        return None

    if external(target):
        return None

    if not wiki:
        target = decoded(target, srcdir)

        if external(target):
            return None

    suf = Path(target).suffix.lower()

    if suf == '':
        target = target + '.md'
        suf = '.md'

    if suf not in LINKABLE:

        if not wiki:
            return None

        lit = Path(os.path.normpath(str(srcdir / target)))

        try:

            if lit.is_file() and classify(lit) is not None:
                return None

            hit = resolvelink(target + '.md', srcdir, wiki=True)

            return hit if hit is not None and hit.is_file() else None

        except OSError:
            return None

    cand = Path(target)

    if not cand.is_absolute():
        cand = srcdir / cand

    res = Path(os.path.normpath(str(cand)))

    if wiki and (not Path(target).is_absolute()):

        try:
            miss = not res.is_file()
        except OSError:
            miss = False

        if miss:
            active = scanroot()

            if active is not None:
                hit = lookup(target, active)

                if hit is not None:
                    return Path(os.path.normpath(str(hit)))

    return res


def mention(span: str) -> tuple[str | None, list[str]] | None:
    val = span.strip()

    if not val or ' ' in val:
        return None

    match = PATHMENTION.match(val)

    if match:
        return (match.group(1), match.group(2).split('::'))

    match = BAREMENTION.match(val)

    if not match:
        return None

    if '.' in val:

        if '.' + match.group(1) in CODE_EXTENSIONS or '.' + match.group(1) in DOC_EXTENSIONS:
            return None

        return (None, val.rstrip('()').split('.'))

    return (None, [match.group(1)])


def posix(path: str) -> str:
    return path.replace('\\', '/')


def label(node: dict) -> str:
    return str(node.get('label', '')).strip('()').lstrip('.')


def isfile(node: dict) -> bool:
    name = str(node.get('label', ''))
    src = str(node.get('source_file', ''))

    return bool(src) and name == os.path.basename(src)


def cited(item: str, doc: str, srcs: set[str]) -> str | None:
    cpos = posix(item)
    parent = os.path.dirname(doc)
    rel = posix(os.path.normpath(os.path.join(parent, cpos)))

    if rel in srcs:
        return rel

    if cpos.startswith(('./', '../')):
        return None

    if cpos in srcs:
        return cpos

    stripped = PREFIX.sub('', cpos)
    hits = [s for s in srcs if s == stripped or s.endswith('/' + stripped)]

    return hits[0] if len(hits) == 1 else None


def evidence(nid: str, byid: dict[str, dict], parents: dict[str, set[str]]) -> set[str]:
    ev: set[str] = set()
    frontier = {nid}
    seen: set[str] = set()

    while frontier:
        cur = frontier.pop()

        if cur in seen:
            continue

        seen.add(cur)
        owners = parents.get(cur, set())

        for o in owners:
            n = byid.get(o)

            if n is not None:
                ev.add(label(n))

        frontier |= owners

    n = byid.get(nid, {})

    for seg in posix(str(n.get('source_file', ''))).split('/'):

        if seg:
            ev.add(seg)
            ev.add(os.path.splitext(seg)[0])

    return ev


def rawcalls(results: list[dict]) -> list[dict]:
    calls: list[dict] = []

    for res in results:

        if not isinstance(res, dict):
            continue

        for rc in res.get('raw_calls', []) or []:

            if isinstance(rc, dict) and rc.get('language') == 'markdown':
                calls.append(rc)

    return calls


def chain(labels: dict[str, list[str]], names: list[str], parents: dict[str, set[str]]) -> str | None:
    cands = list(labels.get(names[-1], []))

    for depth, owner in enumerate(reversed(names[:-1]), start=1):
        owners = set(labels.get(owner, []))
        narrow = []

        for c in cands:
            ch = {c}

            for _ in range(depth):
                ch = {p for nid in ch for p in parents.get(nid, ())}

            if ch & owners:
                narrow.append(c)

        cands = narrow

    return cands[0] if len(cands) == 1 else None


def resolve(results: list[dict], allnodes: list[dict], alledges: list[dict]) -> None:
    calls = rawcalls(results)

    if not calls:
        return

    code = [n for n in allnodes if n.get('id') and n.get('file_type') == 'code' and not isfile(n)]
    bylabel: dict[str, list[str]] = {}
    byfile: dict[str, dict[str, list[str]]] = {}

    for n in code:
        lbl = label(n)

        if not lbl:
            continue

        if n.get('_callable'):
            bylabel.setdefault(lbl, []).append(n['id'])

        src = posix(str(n.get('source_file', '')))

        if src:
            byfile.setdefault(src, {}).setdefault(lbl, []).append(n['id'])

    srcs = set(byfile)
    parents: dict[str, set[str]] = {}

    for e in alledges:

        if e.get('relation') in CONTAINMENT:
            parents.setdefault(str(e.get('target')), set()).add(str(e.get('source')))

    byid = {n['id']: n for n in allnodes if n.get('id')}
    ids = set(byid)
    seen = {(e.get('source'), e.get('target')) for e in alledges if e.get('relation') == 'references'}

    for rc in calls:
        caller = str(rc.get('caller_nid', ''))
        callee = str(rc.get('callee', '')).strip()

        if not caller or not callee or caller not in ids:
            continue

        names = list(rc.get('qualifiers') or []) + [callee]
        pathref = rc.get('path')

        if pathref:
            docsrc = matchfile = cited(str(pathref), posix(str(rc.get('source_file', ''))), srcs)

            if docsrc is None:
                continue

            target = chain(byfile[docsrc], names, parents)

        else:

            if callee in GLOBALS:
                continue

            quals = set(names[:-1])
            cands = [c for c in bylabel.get(callee, []) if quals <= evidence(c, byid, parents)]
            target = cands[0] if len(cands) == 1 else None

        if target is None or target == caller or (caller, target) in seen:
            continue

        alledges.append({'source': caller, 'target': target, 'relation': 'references', 'context': rc.get('context', ''), 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def extract(path: Path) -> dict:

    try:
        source = text(path)
    except Exception as err:
        return {'nodes': [], 'edges': [], 'error': str(err)}

    s = stem(path)
    srcpath = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    raws: list[dict] = []
    ids: set[str] = set()
    mentions: set[tuple[str, str | None, tuple[str, ...]]] = set()

    def addmention(caller: str, raw: str, num: int) -> None:
        item = mention(raw)

        if item is None:
            return

        citedpath, names = item
        k = (caller, citedpath, tuple(names))

        if k in mentions:
            return

        mentions.add(k)
        record = {'caller_nid': caller, 'callee': names[-1], 'is_member_call': False, 'language': 'markdown', 'context': raw.strip(), 'source_file': srcpath, 'source_location': f'L{num}'}

        if citedpath is not None:
            record['path'] = citedpath

        if len(names) > 1:
            record['qualifiers'] = names[:-1]

        raws.append(record)

    def addnode(nid: str, lbl: str, num: int, kind: str = 'heading', ftype: str = 'document', extra: dict | None = None) -> None:

        if nid not in ids:
            ids.add(nid)
            entry = {'id': nid, 'label': lbl, 'file_type': ftype, 'node_kind': kind, 'source_file': srcpath, 'source_location': f'L{num}'}

            if extra:
                entry.update(extra)

            nodes.append(entry)

    def addedge(src: str, tgt: str, rel: str, num: int, tgtfile: str | None = None) -> None:
        entry = {'source': src, 'target': tgt, 'relation': rel, 'source_file': srcpath, 'source_location': f'L{num}'}

        if tgtfile is not None:
            entry['target_file'] = tgtfile

        edges.append(entry)

    lines = source.splitlines()
    fmlines, start = split(lines)
    meta = metadata(parse(fmlines))
    fileid = make(str(path))
    addnode(fileid, path.name, 1, kind='page', extra={'frontmatter': meta} if meta else None)
    srcdir = path.parent
    linked: set[str] = set()

    def addlink(raw: str, num: int, wiki: bool = False) -> None:
        res = resolvelink(raw, srcdir, wiki=wiki)

        if res is None:
            return

        tgtid = make(str(res))

        if tgtid == fileid or tgtid in linked:
            return

        linked.add(tgtid)
        tgtfile = None

        try:

            if res.is_file():
                tgtfile = str(res)

        except OSError:
            pass

        addedge(fileid, tgtid, 'references', num, tgtfile=tgtfile)

    stack: list[tuple[int, str]] = []
    block = False

    for idx, rawline in enumerate(lines):
        num = idx + 1
        stripped = rawline.strip()

        if stripped.startswith('```'):
            block = not block
            continue

        if block:
            continue

        for m in INLINE.finditer(rawline):
            addlink(m.group(1) or m.group(2), num)

        for m in WIKILINK.finditer(rawline):
            addlink(m.group(1), num, wiki=True)

        refmatch = REFDEF.match(rawline)

        if refmatch:
            addlink(refmatch.group(1) or refmatch.group(2), num)

        if idx < start:
            continue

        headmatch = re.match(r'^(#{1,6})\s+(.+)', rawline)

        if headmatch:
            depth = len(headmatch.group(1))
            title = headmatch.group(2).strip()
            hnid = make(s, title)

            if hnid in ids:
                hnid = make(s, title, str(num))

            addnode(hnid, title, num)

            while stack and stack[-1][0] >= depth:
                stack.pop()

            parent = stack[-1][1] if stack else fileid
            addedge(parent, hnid, 'contains', num)
            stack.append((depth, hnid))

            for m in CODESPAN.finditer(title):
                addmention(hnid, m.group(2), num)

            continue

        owner = stack[-1][1] if stack else fileid

        for m in CODESPAN.finditer(rawline):
            addmention(owner, m.group(2), num)

    return {'nodes': nodes, 'edges': edges, 'raw_calls': raws}