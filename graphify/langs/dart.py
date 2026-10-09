from __future__ import annotations

from pathlib import Path
import re

from ..src.base import make, stem, text


PAT = re.compile(r'"""(?:\\.|[\s\S])*?"""|\'\'\'(?:\\.|[\s\S])*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*')
CLASS_PAT = r'^\s*(?:(?:abstract|sealed|base|interface|final|mixin)\s+)*(?:class|mixin|enum|extension\s+type)\s+(\w+)'
TYPEDEF_PAT = r'^\s*typedef\s+(\w+)\s*(?:<[^>]+>)?\s*=\s*([a-zA-Z0-9_<>,.?\s]+);'
EXT_PAT = r'^\s{0,4}extension\s+(\w+)?(?:<[^>]+>)?\s+on\s+(\w+)'
VAR_PAT = r'^\s{0,2}(?:late\s+)?(?:(?:final|const|var)\s+)?(?:\([^)]+\)\s+|([a-zA-Z0-9_<>,.?]+(?:\s+[a-zA-Z0-9_<>,.?]+){0,3})\s+)?(?:(\w+)|(?:\w+\s*)?\(([^)]+)\))\s*(?:=|$|;)'
METHOD_PAT = r'^\s{0,2}(?:factory\s+|static\s+|async\s+|external\s+|abstract\s+)?(?:\([^)]+\)|[a-zA-Z0-9_<>,.?]+)(?:\s+[a-zA-Z0-9_<>,.?]+){0,3}\s+(\w+(?:\.\w+)?)\s*\('
CALL_PAT = r'\b\w+<([a-zA-Z0-9_.]+(?:<[a-zA-Z0-9_.,\s<>]+>)?)\s*>\s*\('
IGNORED_TYPES = {'String', 'int', 'double', 'bool', 'num', 'dynamic', 'Object', 'List', 'Map', 'Set', 'Future', 'Stream', 'void'}


def extract(path: Path) -> dict:
    try:
        src = text(path)
    except OSError:
        return {'error': f'cannot read {path}'}

    def replace(m: re.Match) -> str:
        tok = m.group(0)

        if tok.startswith('/'):
            return '\n' * tok.count('\n')

        return tok

    clean = PAT.sub(replace, src)

    def lineno(pos: int) -> int:
        return clean[:pos].count('\n') + 1

    sname = stem(path)
    fnid = make(str(path))
    part_m = re.search(r'^\s*part\s+of\s+[\'"]([^\'"]+)[\'"]', clean, re.MULTILINE)
    is_part = False
    part_line = None

    if part_m:
        pref = part_m.group(1)

        if pref.endswith('.dart'):
            try:
                ppath = (path.parent / pref).resolve()

                if ppath.exists():
                    sname = stem(ppath)
                    fnid = make(str(ppath))
                    is_part = True
                    part_line = lineno(part_m.start(1))
            except Exception:
                pass

    own_id = make(str(path))
    nodes = [{'id': own_id, 'label': path.name, 'file_type': 'code', 'source_file': str(path), 'source_location': None}]
    edges = []
    seen: set[str] = set()

    def node(nid: str, label: str, ftype: str = 'code', loc: str | None = str(path), line: int | None = None) -> None:
        if nid not in seen:
            nodes.append({'id': nid, 'label': label, 'file_type': ftype, 'source_file': loc, 'source_location': f'L{line}' if line else None})
            seen.add(nid)

    def edge(src_id: str, dst_id: str, rel: str, ctx: str | None = None, line: int | None = None) -> None:
        item = {'source': src_id, 'target': dst_id, 'relation': rel, 'source_file': str(path), 'source_location': f'L{line}' if line else None}

        if ctx:
            item['context'] = ctx

        edges.append(item)

    def split(body: str) -> list[str]:
        parts = []
        cur = []
        depth = 0

        for ch in body:
            if ch == '<':
                depth += 1
                cur.append(ch)
            elif ch == '>':
                depth -= 1
                cur.append(ch)
            elif ch == ',' and depth == 0:
                parts.append(''.join(cur).strip())
                cur = []
            else:
                cur.append(ch)

        if cur:
            parts.append(''.join(cur).strip())

        return [p for p in parts if p]

    def brace(body: str, start: int) -> int:
        first = body.find('{', start)

        if first == -1:
            return len(body)

        count = 1
        i = first + 1
        n = len(body)
        esc = False
        in_double = False
        in_single = False

        while i < n:
            ch = body[i]

            if esc:
                esc = False
                i += 1
                continue

            if ch == '\\':
                esc = True
                i += 1
                continue

            if body[i:i + 3] == '"""' and not in_single:
                i += 3
                end = body.find('"""', i)
                i = end + 3 if end != -1 else n
                continue

            if body[i:i + 3] == "'''" and not in_double:
                i += 3
                end = body.find("'''", i)
                i = end + 3 if end != -1 else n
                continue

            if ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "'" and not in_double:
                in_single = not in_single
            elif not in_double and not in_single:
                if ch == '{':
                    count += 1
                elif ch == '}':
                    count -= 1

                    if count == 0:
                        return i + 1

            i += 1

        return len(body)

    for m in re.finditer(CLASS_PAT, clean, re.MULTILINE):
        cname = m.group(1)
        cline = lineno(m.start(1))
        cnid = make(sname, cname)
        node(cnid, cname, line=cline)
        edge(fnid, cnid, 'defines', line=cline)
        start_idx = m.end()
        rest = clean[start_idx:start_idx + 500]

        if rest.lstrip().startswith('<'):
            offset = rest.find('<')
            depth = 1
            i = offset + 1

            while i < len(rest) and depth > 0:
                if rest[i] == '<':
                    depth += 1
                elif rest[i] == '>':
                    depth -= 1

                i += 1

            rest = rest[i:]

        if rest.lstrip().startswith('('):
            offset = rest.find('(')
            depth = 1
            i = offset + 1

            while i < len(rest) and depth > 0:
                if rest[i] == '(':
                    depth += 1
                elif rest[i] == ')':
                    depth -= 1

                i += 1

            rest = rest[i:]

        hend = rest.find('{')

        if hend == -1:
            hend = rest.find(';')

        if hend == -1:
            hend = len(rest)

        hdr = rest[:hend]
        base = None
        gens = None
        mixins = []
        ifaces = []
        ext_m = re.search(r'^\s*(?:extends|on)\s+([a-zA-Z0-9_.]+)', hdr)

        if ext_m:
            base = ext_m.group(1)
            rhdr = hdr[ext_m.end():]

            if rhdr.strip().startswith('<'):
                sidx = rhdr.find('<')
                depth = 1
                i = sidx + 1

                while i < len(rhdr) and depth > 0:
                    if rhdr[i] == '<':
                        depth += 1
                    elif rhdr[i] == '>':
                        depth -= 1

                        if depth == 0:
                            gens = rhdr[sidx + 1:i]
                            break

                    i += 1

                hdr = rhdr[i + 1:] if gens is not None else rhdr
            else:
                hdr = rhdr

        with_m = re.search(r'^\s*with\s+', hdr)

        if with_m:
            rhdr = hdr[with_m.end():]
            idx = rhdr.find('implements')

            if idx != -1:
                mstr = rhdr[:idx]
                hdr = rhdr[idx:]
            else:
                mstr = rhdr
                hdr = ''

            mixins = split(mstr)

        impl_m = re.search(r'^\s*implements\s+', hdr)

        if impl_m:
            ifaces = split(hdr[impl_m.end():])

        if base:
            bnid = make(base)
            node(bnid, base, loc=None)
            edge(cnid, bnid, 'inherits', line=cline)

            if gens:
                for gen in split(gens):
                    gclean = gen.split('<')[0].strip()

                    if gclean not in IGNORED_TYPES:
                        gnid = make(gclean)
                        node(gnid, gclean, loc=None)
                        edge(cnid, gnid, 'references', line=cline)

        for mix in mixins:
            mclean = mix.split('<')[0].strip()
            mnid = make(mclean)
            node(mnid, mclean, loc=None)
            edge(cnid, mnid, 'mixes_in', line=cline)

        for iface in ifaces:
            iclean = iface.split('<')[0].strip()
            inid = make(iclean)
            node(inid, iclean, loc=None)
            edge(cnid, inid, 'implements', line=cline)

        pos_brace = clean.find('{', m.start())
        pos_semi = clean.find(';', m.start())
        has_body = pos_brace != -1

        if has_body and pos_semi != -1 and pos_semi < pos_brace:
            has_body = False

        if has_body:
            end_pos = brace(clean, m.start())
            cbody = clean[pos_brace:end_pos]

            for em in re.finditer(r'\bon<(\w+)>\s*\(', cbody):
                ename = em.group(1)
                enid = make(ename)
                node(enid, ename, loc=None)
                edge(cnid, enid, 'calls', ctx='bloc_event', line=lineno(pos_brace + em.start(1)))

            for sm in re.finditer(r'\b(?:emit|yield)\s*\(?\s*(?:const\s+)?([A-Z]\w*)\b', cbody):
                sname_state = sm.group(1)

                if sname_state not in IGNORED_TYPES:
                    snid_state = make(sname_state)
                    node(snid_state, sname_state, loc=None)
                    edge(cnid, snid_state, 'calls', ctx='emit_state', line=lineno(pos_brace + sm.start(1)))

            for rm in re.finditer(r'\bref\.(?:watch|read|listen)\s*\(\s*(\w+)\b', cbody):
                pname = rm.group(1)
                pnid = make(pname)
                node(pnid, pname, loc=None)
                edge(cnid, pnid, 'references', ctx='riverpod_reference', line=lineno(pos_brace + rm.start(1)))

    for m in re.finditer(TYPEDEF_PAT, clean, re.MULTILINE):
        tname = m.group(1)
        tline = lineno(m.start(1))
        target = m.group(2).split('<')[0].split('.')[-1].strip()

        if target not in IGNORED_TYPES:
            tnid = make(sname, tname)
            node(tnid, tname, line=tline)
            edge(fnid, tnid, 'defines', line=tline)
            dst_nid = make(target)
            node(dst_nid, target, loc=None)
            edge(tnid, dst_nid, 'references', ctx='typedef', line=tline)

    for m in re.finditer(EXT_PAT, clean, re.MULTILINE):
        ename = m.group(1) or f'{sname}_ext'
        target = m.group(2)
        eline = lineno(m.start(2))
        enid = make(sname, ename)
        node(enid, m.group(1) or f'Extension on {target}', line=eline)
        edge(fnid, enid, 'defines', line=eline)
        dst_nid = make(target)
        node(dst_nid, target, loc=None)
        edge(enid, dst_nid, 'extends', line=eline)

    for m in re.finditer(VAR_PAT, clean, re.MULTILINE):
        vtype = m.group(1)
        sname_var = m.group(2)
        dnames = m.group(3)

        if not re.match(r'^\s*(?:late|final|const|var)\b', m.group(0)) and not vtype:
            continue

        if sname_var and sname_var not in {'if', 'for', 'while', 'switch', 'catch', 'return'}:
            vline = lineno(m.start(2))
            vnid = make(sname, sname_var)
            node(vnid, sname_var, line=vline)
            edge(fnid, vnid, 'defines', line=vline)

            if vtype and vtype not in IGNORED_TYPES:
                cleant = vtype.split('<')[0].split('.')[-1].strip()
                tnid = make(cleant)
                node(tnid, cleant, loc=None)
                edge(fnid, tnid, 'references', ctx='variable_type', line=vline)

    raw_calls: list[dict] = []

    for m in re.finditer(METHOD_PAT, clean, re.MULTILINE):
        raw = m.group(1)
        mname = raw.split('.')[-1]

        if mname in {'if', 'for', 'while', 'switch', 'catch', 'return', 'void', 'dynamic', 'final', 'const', 'get', 'set'}:
            continue

        if re.match(r'^[A-Z]', mname):
            continue

        mline = lineno(m.start(1))
        mnid = make(sname, mname)
        node(mnid, mname, line=mline)
        edge(fnid, mnid, 'defines', line=mline)

        pos_b = clean.find('{', m.end())
        next_m = clean.find(';', m.end())

        if pos_b != -1 and (next_m == -1 or pos_b < next_m):
            end_b = brace(clean, pos_b)
            body = clean[pos_b:end_b]

            for cm in re.finditer(r'\b([a-zA-Z_]\w*)\s*\(', body):
                cname = cm.group(1)

                if cname not in {'if', 'for', 'while', 'switch', 'catch', 'return', 'print', mname} and cname not in IGNORED_TYPES:
                    call_line = lineno(pos_b + cm.start(1))
                    edge(mnid, make(sname, cname), 'calls', line=call_line)
                    raw_calls.append({'caller_nid': mnid, 'callee': cname, 'source_file': str(path), 'source_location': f'L{call_line}'})

    quoted = r'(?:\'[^\']*\'|"[^"]*")'
    condition = rf'if\s*\(\s*[\w.]+\s*(?:==\s*{quoted}\s*)?\)\s*'
    branch_re = re.compile(rf"""{condition}(['"])(?P<uri>[^'"]+)\1""")

    for k, rel in (('import', 'imports'), ('export', 'exports')):
        d_re = re.compile(rf"""^\s*{k}\s+(['"])(?P<uri>[^'"]+)\1(?P<branches>(?:\s*{condition}{quoted})*)""", re.MULTILINE)

        for m in d_re.finditer(clean):
            uris: list[tuple[str, int, str | None]] = [(m.group('uri'), m.start('uri'), None)]

            for bm in branch_re.finditer(m.group('branches')):
                uris.append((bm.group('uri'), m.start('branches') + bm.start('uri'), 'conditional_uri'))

            for pkg, offset, ctx in uris:
                tnid = make(pkg)
                node(tnid, pkg, loc=None)
                edge(fnid, tnid, rel, ctx=ctx, line=lineno(offset))

    for m in re.finditer(CALL_PAT, clean):
        tname = m.group(1).split('.')[-1].strip().split('<')[0].strip()

        if tname not in IGNORED_TYPES:
            tnid = make(tname)
            node(tnid, tname, loc=None)
            edge(fnid, tnid, 'references', ctx='type_lookup', line=lineno(m.start(1)))

    if is_part:
        part_edges = [{**e, 'source': own_id, 'relation': 'contains'} for e in edges if e['source'] == fnid and e['relation'] == 'defines']
        edge(fnid, own_id, 'includes', line=part_line)
        edges.extend(part_edges)

    return {'nodes': nodes, 'edges': edges, 'raw_calls': raw_calls}