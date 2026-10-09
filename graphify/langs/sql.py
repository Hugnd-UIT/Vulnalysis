from __future__ import annotations

from pathlib import Path
import re

from ..src.base import bytes as read_bytes, make, stem


ROUTINE = re.compile(r'\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:FUNCTION|PROC(?:EDURE)?)\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:\"(?:[^\"]|\"\")+\"|\[(?:[^\]]|\]\])+\]|[\w$]+)(?:\s*\.\s*(?:\"(?:[^\"]|\"\")+\"|\[(?:[^\]]|\]\])+\]|[\w$]+))*)', re.IGNORECASE)
IDENT = r'(?:\"(?:[^\"]|\"\")+\"|\[(?:[^\]]|\]\])+\]|[\w$]+)(?:\s*\.\s*(?:\"(?:[^\"]|\"\")+\"|\[(?:[^\]]|\]\])+\]|[\w$]+))*'
TRIGGER = re.compile(r'\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(' + IDENT + r').*?\bON\s+(' + IDENT + r')', re.IGNORECASE | re.DOTALL)


def scan(text: str) -> tuple[str, list[tuple[int, int]]]:
    spans: list[tuple[int, int]] = []
    out: list[str] = []
    idx, length = (0, len(text))

    def blank(upto: int) -> int:
        for c in text[idx:upto]:
            out.append('\n' if c == '\n' else ' ')

        return upto

    def tail(start: int) -> int:
        pos = start

        while True:
            eol = text.find('\n', pos)
            eol = length if eol == -1 else eol
            depth = 0
            curr = pos

            while curr < eol:
                if text.startswith('/*', curr):
                    depth += 1
                    curr += 2
                elif text.startswith('*/', curr):
                    if depth:
                        depth -= 1
                    curr += 2
                else:
                    curr += 1

            for ch in text[pos:eol]:
                out.append('\n' if ch == '\n' else ' ')

            pos = eol

            if not depth:
                return pos

            step = pos

            while step < length and depth:
                if text.startswith('/*', step):
                    depth += 1
                    step += 2
                elif text.startswith('*/', step):
                    depth -= 1
                    step += 2
                else:
                    step += 1

            for ch in text[pos:step]:
                out.append('\n' if ch == '\n' else ' ')

            pos = step

            if pos >= length:
                return pos

    while idx < length:
        ch = text[idx]

        if ch == "'":
            step = idx + 1

            while step < length and text[step] != '\n':
                if text[step] == "'":
                    if step + 1 < length and text[step + 1] == "'":
                        step += 2
                        continue

                    step += 1
                    break

                step += 1

            idx = blank(step)

        elif ch == '"' or ch == '[':
            closer = '"' if ch == '"' else ']'
            step = idx + 1
            closed = False

            while step < length and text[step] != '\n':
                if text[step] == closer:
                    if step + 1 < length and text[step + 1] == closer:
                        step += 2
                        continue

                    step += 1
                    closed = True
                    break

                step += 1

            span = text[idx:step]

            if closed and '--' not in span and ('/*' not in span):
                spans.append((idx, step))
                out.append(span)
                idx = step
            else:
                idx = tail(idx)

        elif ch == '-' and idx + 1 < length and (text[idx + 1] == '-'):
            step = idx

            while step < length and text[step] != '\n':
                step += 1

            idx = blank(step)

        elif ch == '/' and idx + 1 < length and (text[idx + 1] == '*'):
            depth, step = (1, idx + 2)

            while step < length and depth:
                if text[step] == '/' and step + 1 < length and (text[step + 1] == '*'):
                    depth += 1
                    step += 2
                elif text[step] == '*' and step + 1 < length and (text[step + 1] == '/'):
                    depth -= 1
                    step += 2
                else:
                    step += 1

            idx = blank(step)

        else:
            out.append(ch)
            idx += 1

    return (''.join(out), spans)


def mask(text: str) -> str:
    return scan(text)[0]


def norm(name: str) -> str:
    parts = []

    for part in name.split('.'):
        item = part.strip()

        if len(item) >= 2 and (item[0] == item[-1] and item[0] in ('"', '`') or (item[0] == '[' and item[-1] == ']')):
            item = item[1:-1]

        parts.append(item.lower())

    return '.'.join(parts)


def extract(path: Path, content: str | bytes | None = None) -> dict:
    try:
        import tree_sitter_sql as ts
        from tree_sitter import Language, Parser
    except ImportError as exc:
        import importlib.util

        if importlib.util.find_spec('tree_sitter_sql') is None:
            return {'nodes': [], 'edges': [], 'error': 'tree_sitter_sql not installed'}

        return {'nodes': [], 'edges': [], 'error': str(exc)}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = content.encode('utf-8') if isinstance(content, str) else content if content is not None else read_bytes(path)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    base = stem(path)
    src = str(path)
    file_id = make(src)
    nodes: list[dict] = [{'id': file_id, 'label': path.name, 'file_type': 'code', 'source_file': src, 'source_location': None}]
    edges: list[dict] = []
    seen: set[str] = {file_id}
    tables: dict[str, str] = {}
    routines: dict[str, str] = {}

    def text(raw) -> str:
        return source[raw.start_byte:raw.end_byte].decode('utf-8', errors='replace')

    def name(raw) -> str | None:
        for child in raw.children:
            if child.type == 'object_reference':
                return text(child)

        return None

    def node(nid: str, label: str, line: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': src, 'source_location': f'L{line}'})
            edges.append({'source': file_id, 'target': nid, 'relation': 'contains', 'source_file': src, 'source_location': f'L{line}'})

    def edge(source_id: str, target_id: str, relation: str, line: int) -> None:
        edges.append({'source': source_id, 'target': target_id, 'relation': relation, 'source_file': src, 'source_location': f'L{line}'})

    def stub(raw_name: str) -> str:
        nid = make(raw_name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': raw_name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def walk(raw) -> None:
        kind = raw.type
        line = raw.start_point[0] + 1

        if kind == 'create_table':
            tname = name(raw)

            if tname:
                nid = make(base, tname)
                node(nid, tname, line)
                tables[norm(tname)] = nid

                for col in raw.children:
                    if col.type == 'column_definitions':
                        has_err = any((cd.type == 'ERROR' for cd in col.children))
                        refs: set[str] = set()

                        for cd in col.children:
                            if cd.type == 'column_definition':
                                ref_name: str | None = None
                                has_ref = False

                                for cc in cd.children:
                                    if cc.type == 'keyword_references':
                                        has_ref = True
                                    elif has_ref and cc.type == 'object_reference':
                                        ref_name = text(cc)
                                        break

                                if ref_name:
                                    ref_id = tables.get(norm(ref_name)) or stub(ref_name)
                                    edge(nid, ref_id, 'references', line)
                                    refs.add(norm(ref_name))

                            elif cd.type == 'constraints':
                                for constr in cd.children:
                                    if constr.type != 'constraint':
                                        continue

                                    ref_name = None
                                    has_ref = False

                                    for cc in constr.children:
                                        if cc.type == 'keyword_references':
                                            has_ref = True
                                        elif has_ref and cc.type == 'object_reference':
                                            ref_name = text(cc)
                                            break

                                    if ref_name:
                                        ref_id = tables.get(norm(ref_name)) or stub(ref_name)
                                        edge(nid, ref_id, 'references', line)
                                        refs.add(norm(ref_name))

                        if has_err:
                            col_text = text(col)

                            for rm in re.finditer(r'\bREFERENCES\s+([\w$]+)', col_text, re.IGNORECASE):
                                ref_name = rm.group(1)

                                if norm(ref_name) not in refs:
                                    ref_id = tables.get(norm(ref_name)) or stub(ref_name)
                                    edge(nid, ref_id, 'references', line)
                                    refs.add(norm(ref_name))

        elif kind == 'create_view':
            vname = name(raw)

            if vname:
                nid = make(base, vname)
                node(nid, vname, line)
                tables[norm(vname)] = nid
                fromrefs(raw, nid, line)

        elif kind == 'create_function':
            fname = name(raw)

            if fname:
                nid = make(base, fname)
                node(nid, f'{fname}()', line)
                routines[norm(fname)] = nid
                fromrefs(raw, nid, line)

        elif kind == 'create_procedure':
            pname = name(raw)

            if pname:
                nid = make(base, pname)
                node(nid, f'{pname}()', line)
                routines[norm(pname)] = nid
                fromrefs(raw, nid, line)

        elif kind == 'alter_table':
            aname = name(raw)

            if aname:
                src_id = tables.get(norm(aname))

                if not src_id:
                    src_id = stub(aname)
                    tables[norm(aname)] = src_id

                for child in raw.children:
                    if child.type == 'add_constraint':
                        for cc in child.children:
                            if cc.type != 'constraint':
                                continue

                            has_ref = False
                            ref_name: str | None = None

                            for ccc in cc.children:
                                if ccc.type == 'keyword_references':
                                    has_ref = True
                                elif has_ref and ccc.type == 'object_reference':
                                    ref_name = text(ccc)
                                    break

                            if ref_name:
                                ref_id = tables.get(norm(ref_name)) or stub(ref_name)
                                edge(src_id, ref_id, 'references', line)

        elif kind == 'create_trigger':
            trig_name: str | None = None
            tbl_name: str | None = None
            exec_name: str | None = None
            after_trig = False
            after_on = False
            after_exec = False

            for c in raw.children:
                if c.type == 'keyword_trigger':
                    after_trig = True
                elif after_trig and (not trig_name) and (c.type == 'object_reference'):
                    trig_name = text(c)
                elif c.type == 'keyword_on':
                    after_on = True
                elif after_on and (not tbl_name) and (c.type == 'object_reference'):
                    tbl_name = text(c)
                elif c.type == 'keyword_execute':
                    after_exec = True
                elif after_exec and (not exec_name) and (c.type == 'object_reference'):
                    exec_name = text(c)

            if trig_name:
                trig_id = make(base, trig_name)
                node(trig_id, trig_name, line)

                if tbl_name:
                    tbl_id = tables.get(norm(tbl_name)) or stub(tbl_name)
                    edge(trig_id, tbl_id, 'triggers', line)

                if exec_name:
                    routine_id = routines.get(norm(exec_name))

                    if routine_id:
                        edge(trig_id, routine_id, 'executes', line)

        elif kind == 'create_index':
            idx_name: str | None = None
            idx_table: str | None = None
            after_on = False

            for c in raw.children:
                if c.type == 'keyword_on':
                    after_on = True
                elif not after_on and idx_name is None and (c.type in ('identifier', 'literal')):
                    idx_name = text(c).strip('"`')
                elif after_on and idx_table is None and (c.type == 'object_reference'):
                    idx_table = text(c)

            if idx_name:
                idx_id = make(base, idx_name)
                node(idx_id, idx_name, line)

                if idx_table:
                    idx_tbl_id = tables.get(norm(idx_table)) or stub(idx_table)
                    edge(idx_id, idx_tbl_id, 'indexes', line)

        elif kind == 'fb_proc_or_trigger':
            raw_text = text(raw)
            hit = re.match(r'CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(PROCEDURE|TRIGGER|FUNCTION)\s+([\w$]+)', raw_text, re.IGNORECASE)

            if hit:
                obj_kind = hit.group(1).upper()
                obj_name = hit.group(2)
                obj_id = make(base, obj_name)
                lbl = obj_name if obj_kind == 'TRIGGER' else f'{obj_name}()'
                node(obj_id, lbl, line)

                if obj_kind == 'TRIGGER':
                    fm = re.search(r'\bFOR\s+([\w$]+)', raw_text, re.IGNORECASE)

                    if fm:
                        tbl = fm.group(1)
                        tbl_id = tables.get(norm(tbl)) or stub(tbl)
                        edge(obj_id, tbl_id, 'triggers', line)

                ignored = {'select', 'where', 'set', 'dual', 'null', 'true', 'false', 'first', 'skip', 'rows', 'next', 'only', 'lateral'}

                for cm in re.finditer(r'(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)([\w$]+)\s*(?:\([^()]*\))?\s+AS\s*\(', raw_text, re.IGNORECASE):
                    ignored.add(norm(cm.group(1)))

                seen_tbls: set[str] = set()

                for rm in re.finditer(r'\b(?:FROM|JOIN|INTO)\s+([\w$]+)', raw_text, re.IGNORECASE):
                    tbl = rm.group(1)

                    if norm(tbl) not in ignored and norm(tbl) not in seen_tbls:
                        seen_tbls.add(norm(tbl))
                        tbl_id = tables.get(norm(tbl)) or stub(tbl)
                        edge(obj_id, tbl_id, 'reads_from', line)

                for rm in re.finditer(r'\bUPDATE\s+([\w$]+)', raw_text, re.IGNORECASE):
                    tbl = rm.group(1)

                    if norm(tbl) not in ignored and norm(tbl) not in seen_tbls:
                        seen_tbls.add(norm(tbl))
                        tbl_id = tables.get(norm(tbl)) or stub(tbl)
                        edge(obj_id, tbl_id, 'reads_from', line)

        for child in raw.children:
            walk(child)

    def fromrefs(raw, caller_id: str, line: int, ctes: frozenset[str] = frozenset()) -> None:
        own: set[str] = set()

        for c in raw.children:
            if c.type != 'cte':
                continue

            for cc in c.children:
                if cc.type in ('identifier', 'object_reference'):
                    own.add(norm(text(cc)))
                    break

        if own:
            ctes = frozenset(ctes | own)

        if raw.type in ('from', 'join'):
            for c in raw.children:
                if c.type == 'relation':
                    for cc in c.children:
                        if cc.type == 'object_reference':
                            tbl = text(cc)

                            if norm(tbl) in ctes:
                                continue

                            tbl_id = tables.get(norm(tbl)) or stub(tbl)
                            edge(caller_id, tbl_id, 'reads_from', c.start_point[0] + 1)

        for child in raw.children:
            fromrefs(child, caller_id, line, ctes)

    def collect(raw) -> None:
        if raw.type in ('create_table', 'create_view'):
            item_name = name(raw)

            if item_name:
                tables[norm(item_name)] = make(base, item_name)

        for child in raw.children:
            collect(child)

    collect(root)
    bare_cands: dict[str, str | None] = {}

    for key, alias_id in tables.items():
        if '.' in key:
            bare = key.rsplit('.', 1)[1]
            bare_cands[bare] = alias_id if bare_cands.get(bare, alias_id) == alias_id else None

    for bare, alias_id in bare_cands.items():
        if alias_id is not None and bare not in tables:
            tables[bare] = alias_id

    for stmt in root.children:
        if stmt.type == 'statement':
            for child in stmt.children:
                walk(child)
        elif stmt.type == 'transaction':
            walk(stmt)
        elif stmt.type == 'block':
            walk(stmt)
        elif stmt.type in ('fb_proc_or_trigger', 'set_term', 'declare_external_function', 'ERROR'):
            walk(stmt)

    emitted = {(e['source'], e['target']) for e in edges if e['relation'] == 'references'}
    src_text = source.decode('utf-8', errors='replace')

    for hit in re.finditer(r'CREATE\s+TABLE\s+([\w$]+)\s*\(', src_text, re.IGNORECASE):
        tbl_name = hit.group(1)
        tbl_id = tables.get(norm(tbl_name))

        if tbl_id is None:
            continue

        tbl_line = src_text[:hit.start()].count('\n') + 1
        tail_text = src_text[hit.start():]
        end = re.search(r'(?:^|\n)(?:CREATE|SET\s+TERM|ALTER)\s', tail_text[1:], re.IGNORECASE)
        block = tail_text[:end.start() + 1] if end else tail_text

        for rm in re.finditer(r'\bREFERENCES\s+([\w$]+)', block, re.IGNORECASE):
            ref_name = rm.group(1)
            ref_id = tables.get(norm(ref_name)) or stub(ref_name)

            if (tbl_id, ref_id) not in emitted:
                edge(tbl_id, ref_id, 'references', tbl_line)
                emitted.add((tbl_id, ref_id))

    if root.has_error:
        masked_src, spans = scan(src_text)

        for hit in ROUTINE.finditer(masked_src):
            if any((s <= hit.start() < e for s, e in spans)):
                continue

            fn_name = hit.group(1)
            fn_line = src_text[:hit.start()].count('\n') + 1
            node(make(base, fn_name), f'{fn_name}()', fn_line)

        for hit in TRIGGER.finditer(masked_src):
            if any((s <= hit.start() < e for s, e in spans)):
                continue

            trig_name = hit.group(1)
            trig_id = make(base, trig_name)

            if trig_id in seen:
                continue

            trig_line = src_text[:hit.start()].count('\n') + 1
            node(trig_id, trig_name, trig_line)
            tbl_name = hit.group(2)
            tbl_id = tables.get(norm(tbl_name)) or stub(tbl_name)
            edge(trig_id, tbl_id, 'triggers', trig_line)

    return {'nodes': nodes, 'edges': edges}