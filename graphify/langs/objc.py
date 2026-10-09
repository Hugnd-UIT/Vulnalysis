from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..src.base import make, read, stem, bytes as readbytes
from ..src.engine import cppdeclarator, semantic
from ..src.resolve import cinclude


STEM = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
BLANKS = (b'NS_ASSUME_NONNULL_BEGIN', b'NS_ASSUME_NONNULL_END')


def basestem(val: str) -> str:
    head, sep, tail = val.rpartition('/')
    prefix, plus, suf = tail.partition('+')

    if not plus or not STEM.fullmatch(prefix) or (not STEM.fullmatch(suf)):
        return val

    return f'{head}{sep}{prefix}'


def category(raw) -> bool:
    return any((c.type == '(' for c in raw.children))


def vartypes(raw, source: bytes, table: dict[str, str]) -> None:
    stack = [raw]

    while stack:
        item = stack.pop()

        if item.type == 'method_definition' and item is not raw:
            continue

        if item.type == 'declaration':
            type_node = item.child_by_field_name('type')

            if type_node is None:
                for c in item.children:
                    if c.type == 'type_identifier':
                        type_node = c
                        break

            if type_node is not None and type_node.type == 'type_identifier':
                type_name = read(type_node, source).strip()
                decls = [c for c in item.children if c.type in ('identifier', 'pointer_declarator', 'init_declarator')]

                if type_name and type_name[:1].isupper() and (len(decls) == 1):
                    var = cppdeclarator(decls[0], source)

                    if var and var not in table:
                        table[var] = type_name

        for c in item.children:
            stack.append(c)


def extract(path: Path) -> dict:
    try:
        import tree_sitter_objc as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_objc not installed'}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = readbytes(path)

        for blank in BLANKS:
            source = source.replace(blank, b' ' * len(blank))

        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    base = stem(path)
    src = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    bodies: list[tuple[str, Any, str]] = []
    calls: list[dict] = []
    types: dict[str, str] = {}
    fields: dict[str, dict[str, str | None]] = {}

    def text(raw) -> str:
        return source[raw.start_byte:raw.end_byte].decode('utf-8', errors='replace')

    def name(raw, field: str) -> str | None:
        sub = raw.child_by_field_name(field)

        return text(sub) if sub else None

    def typeidents(raw):
        if raw.type == 'type_identifier':
            yield raw

            return

        for c in raw.children:
            yield from typeidents(c)

    def node(nid: str, label: str, line: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': src, 'source_location': f'L{line}'})

    def edge(source_id: str, target_id: str, relation: str, line: int, context: str | None = None) -> None:
        item = {'source': source_id, 'target': target_id, 'relation': relation, 'source_file': src, 'source_location': f'L{line}'}

        if context:
            item['context'] = context

        edges.append(item)

    file_id = make(src)
    node(file_id, path.name, 1)

    def named(name_str: str, line: int) -> str:
        nid = make(base, name_str)

        if nid in seen:
            return nid

        nid = make(name_str)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name_str, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def fieldentry(sd) -> tuple[str, str] | None:
        type_ids = [s for s in sd.children if s.type == 'type_identifier']
        decls = [s for s in sd.children if s.type == 'struct_declarator']

        if len(type_ids) != 1 or len(decls) != 1:
            return None

        type_name = text(type_ids[0]).strip()

        if not type_name or not type_name[:1].isupper():
            return None

        inner = decls[0].children

        if len(inner) != 1:
            return None

        var = cppdeclarator(inner[0], source)

        if not var:
            return None

        return (var, type_name)

    def recordfield(cls_id: str, field_name: str, type_name: str) -> None:
        table = fields.setdefault(cls_id, {})

        if field_name in table:
            if table[field_name] != type_name:
                table[field_name] = None
        else:
            table[field_name] = type_name

    def ivars(raw, cls_id: str) -> None:
        for iv in raw.children:
            if iv.type != 'instance_variable':
                continue

            for sd in iv.children:
                if sd.type == 'struct_declaration':
                    entry = fieldentry(sd)

                    if entry is not None:
                        recordfield(cls_id, *entry)

    def enums(raw, name_str: str, line: int) -> None:
        enum_id = make(base, name_str)
        node(enum_id, name_str, line)
        edge(file_id, enum_id, 'contains', line)
        body = next((c for c in raw.children if c.type == 'enumerator_list'), None)

        if body is None:
            return

        for member in body.children:
            if member.type != 'enumerator':
                continue

            mname = next((text(c) for c in member.children if c.type == 'identifier'), None)

            if not mname:
                continue

            mline = member.start_point[0] + 1
            member_id = make(enum_id, mname)
            node(member_id, mname, mline)
            edge(enum_id, member_id, 'case_of', mline)

    def walk(raw, parent_id: str | None = None) -> None:
        kind = raw.type
        line = raw.start_point[0] + 1

        if kind == 'preproc_include':
            for child in raw.children:
                if child.type == 'system_lib_string':
                    val = text(child).strip('<>')
                    mod = val.split('/')[-1].replace('.h', '')

                    if mod:
                        edge(file_id, make(mod), 'imports', line, context='import')

                elif child.type == 'string_literal':
                    for sub in child.children:
                        if sub.type == 'string_content':
                            val = text(sub)
                            resolved = cinclude(val, src)

                            if resolved is not None:
                                edge(file_id, make(str(resolved)), 'imports', line, context='import')
                            else:
                                mod = val.split('/')[-1].replace('.h', '')

                                if mod:
                                    edge(file_id, make(mod), 'imports', line, context='import')

            return

        if kind == 'module_import':
            path_node = raw.child_by_field_name('path')

            if path_node is not None:
                mod = text(path_node).split('.')[0].strip()

                if mod:
                    edge(file_id, make(mod), 'imports', line, context='import')

            return

        if kind == 'class_interface':
            idents = [c for c in raw.children if c.type == 'identifier']

            if not idents:
                for child in raw.children:
                    walk(child, parent_id)

                return

            cname = text(idents[0])
            cls_stem = basestem(base) if category(raw) else base
            cls_id = make(cls_stem, cname)
            node(cls_id, cname, line)
            edge(file_id, cls_id, 'contains', line)
            colon = False

            for child in raw.children:
                if child.type == ':':
                    colon = True
                elif colon and child.type == 'identifier':
                    super_id = named(text(child), line)
                    edge(cls_id, super_id, 'inherits', line)
                    colon = False
                elif child.type == 'parameterized_arguments':
                    for sub in child.children:
                        if sub.type == 'type_name':
                            for s in sub.children:
                                if s.type == 'type_identifier':
                                    proto_id = named(text(s), line)
                                    edge(cls_id, proto_id, 'implements', line)
                elif child.type == 'property_declaration':
                    pline = child.start_point[0] + 1

                    for sub in child.children:
                        if sub.type == 'struct_declaration':
                            seen_types: set[str] = set()

                            for s in sub.children:
                                if s.type in ('struct_declarator', ';'):
                                    continue

                                for ti in typeidents(s):
                                    tname = text(ti)

                                    if tname in seen_types:
                                        continue

                                    seen_types.add(tname)
                                    tid = named(tname, pline)
                                    edges.append(semantic(cls_id, tid, 'field', src, pline))

                            entry = fieldentry(sub)

                            if entry is not None:
                                recordfield(cls_id, *entry)
                elif child.type == 'instance_variables':
                    ivars(child, cls_id)
                elif child.type == 'method_declaration':
                    walk(child, cls_id)

            return

        if kind == 'class_implementation':
            cname = None

            for child in raw.children:
                if child.type == 'identifier':
                    cname = text(child)
                    break

            if not cname:
                for child in raw.children:
                    walk(child, parent_id)

                return

            impl_stem = basestem(base) if category(raw) else base
            impl_id = make(impl_stem, cname)

            if impl_id not in seen:
                node(impl_id, cname, line)
                edge(file_id, impl_id, 'contains', line)

            for child in raw.children:
                if child.type == 'instance_variables':
                    ivars(child, impl_id)
                elif child.type == 'implementation_definition':
                    for sub in child.children:
                        walk(sub, impl_id)

            return

        if kind == 'protocol_declaration':
            pname = None

            for child in raw.children:
                if child.type == 'identifier':
                    pname = text(child)
                    break

            if pname:
                proto_id = make(base, pname)
                node(proto_id, f'<{pname}>', line)
                edge(file_id, proto_id, 'contains', line)

                for child in raw.children:
                    if child.type == 'protocol_reference_list':
                        for sub in child.children:
                            if sub.type == 'identifier':
                                base_id = named(text(sub), line)

                                if base_id != proto_id:
                                    edge(proto_id, base_id, 'implements', line)

                for child in raw.children:
                    walk(child, proto_id)

            return

        if kind in ('method_declaration', 'method_definition'):
            holder = parent_id or file_id
            prefix = '-'

            for child in raw.children:
                if child.type in ('+', '-'):
                    prefix = child.type
                    break

            parts = [text(c) for c in raw.children if c.type == 'identifier']
            method_name = ''.join(parts) if parts else None

            if method_name:
                method_id = make(holder, method_name)
                node(method_id, f'{prefix}{method_name}', line)
                edge(holder, method_id, 'method', line)

                if kind == 'method_definition':
                    bodies.append((method_id, raw, holder))

            return

        if kind == 'type_definition':
            enum_spec = next((c for c in raw.children if c.type == 'enum_specifier'), None)

            if enum_spec is not None and any((c.type == 'enumerator_list' for c in enum_spec.children)):
                alias = next((text(c) for c in reversed(raw.children) if c.type == 'type_identifier'), None)

                if alias is None:
                    alias = next((text(c) for c in enum_spec.children if c.type == 'type_identifier'), None)

                if alias:
                    enums(enum_spec, alias, line)

                    return

        if kind == 'enum_specifier':
            ename = next((text(c) for c in raw.children if c.type == 'type_identifier'), None)

            if ename and any((c.type == 'enumerator_list' for c in raw.children)):
                enums(raw, ename, line)

            return

        for child in raw.children:
            walk(child, parent_id)

    walk(root)

    all_methods = {n['id'] for n in nodes if n['id'] != file_id}
    class_methods: dict[str, set[str]] = {}

    for mid, _, holder_id in bodies:
        class_methods.setdefault(holder_id, set()).add(mid)

    pairs: set[tuple[str, str]] = set()

    for _, body_node, _ in bodies:
        vartypes(body_node, source, types)

    for caller_id, body_node, holder_id in bodies:
        siblings = class_methods.get(holder_id, set())

        def trace(n) -> None:
            if n.type == 'message_expression':
                meth = n.child_by_field_name('method')
                recv = n.child_by_field_name('receiver')

                if meth is not None and meth.type == 'identifier' and (text(meth) == 'alloc') and (recv is not None) and (recv.type == 'identifier'):
                    tname = text(recv)
                    ref_line = n.start_point[0] + 1
                    tid = named(tname, ref_line)

                    if tid != caller_id:
                        edges.append(semantic(caller_id, tid, 'type', src, ref_line))

                parts = [text(child) for i, child in enumerate(n.children) if n.field_name_for_child(i) == 'method' and child.type == 'identifier']
                method_name = ''.join(parts)

                if method_name:
                    needle = make('', method_name).lstrip('_')

                    for cand in all_methods:
                        if cand.endswith(needle):
                            pair = (caller_id, cand)

                            if pair not in pairs and caller_id != cand:
                                pairs.add(pair)
                                edge(caller_id, cand, 'calls', n.start_point[0] + 1, context='call')

                    if recv is not None and recv.type == 'identifier':
                        calls.append({'caller_nid': caller_id, 'callee': method_name, 'is_member_call': True, 'source_file': src, 'source_location': f'L{n.start_point[0] + 1}', 'receiver': text(recv), 'lang': 'objc'})
                    elif recv is not None and recv.type == 'field_expression':
                        kids = recv.children

                        if len(kids) == 3 and kids[0].type == 'identifier' and (text(kids[0]) == 'self') and (kids[1].type == '.') and (kids[2].type == 'field_identifier'):
                            calls.append({'caller_nid': caller_id, 'callee': method_name, 'is_member_call': True, 'source_file': src, 'source_location': f'L{n.start_point[0] + 1}', 'receiver': text(kids[2]), 'receiver_kind': 'self_field', 'lang': 'objc'})

            elif n.type == 'field_expression':
                for child in n.children:
                    if child.type == 'field_identifier':
                        fname = text(child)
                        target = make(holder_id, fname)

                        if target in siblings and target != caller_id:
                            pair = (caller_id, target)

                            if pair not in pairs:
                                pairs.add(pair)
                                edge(caller_id, target, 'accesses', n.start_point[0] + 1)

            elif n.type == 'selector_expression':
                parts = [text(c) for c in n.children if c.type == 'identifier']
                sel_name = ''.join(parts)

                if sel_name:
                    matches = sorted({m for m, _, cont in bodies if m == make(cont, sel_name) and m != caller_id})

                    if len(matches) == 1:
                        pair = (caller_id, matches[0])

                        if pair not in pairs:
                            pairs.add(pair)
                            edge(caller_id, matches[0], 'calls', n.start_point[0] + 1, context='call')

            for child in n.children:
                trace(child)

        trace(body_node)

    out = {'nodes': nodes, 'edges': edges, 'raw_calls': calls}

    if types:
        out['objc_type_table'] = {'path': src, 'table': types}

    field_tables = {cls: {f: t for f, t in tbl.items() if t} for cls, tbl in fields.items()}
    field_tables = {cls: tbl for cls, tbl in field_tables.items() if tbl}

    if field_tables:
        out['objc_field_types'] = {'path': src, 'tables': field_tables}

    return out