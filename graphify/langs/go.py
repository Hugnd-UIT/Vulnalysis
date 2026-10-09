from __future__ import annotations

import hashlib
from pathlib import Path

from ..src.base import GLOBALS, bytes as read_bytes, make, read, stem


TYPES = frozenset({'bool', 'byte', 'complex64', 'complex128', 'error', 'float32', 'float64', 'int', 'int8', 'int16', 'int32', 'int64', 'rune', 'string', 'uint', 'uint8', 'uint16', 'uint32', 'uint64', 'uintptr', 'any', 'comparable'})
FUNCS = frozenset({'append', 'cap', 'clear', 'close', 'complex', 'copy', 'delete', 'imag', 'len', 'make', 'max', 'min', 'new', 'panic', 'print', 'println', 'real', 'recover'})


def types(raw, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if raw is None:
        return

    kind = raw.type

    if kind == 'type_identifier':
        val = read(raw, source)

        if val and val not in TYPES:
            out.append((val, 'generic_arg' if generic else 'type'))

        return

    if kind == 'qualified_type':
        val = read(raw, source)

        if val:
            out.append((val, 'generic_arg' if generic else 'type'))

        return

    if kind == 'generic_type':
        type_field = raw.child_by_field_name('type')

        if type_field is not None:
            sub: list[tuple[str, str]] = []
            types(type_field, source, generic, sub)
            out.extend(sub)

        for child in raw.children:
            if child.type == 'type_arguments':
                for arg in child.children:
                    if arg.is_named:
                        types(arg, source, True, out)

        return

    if kind in ('pointer_type', 'slice_type', 'array_type', 'map_type', 'channel_type', 'parenthesized_type'):
        for child in raw.children:
            if child.is_named:
                types(child, source, generic, out)

        return

    if raw.is_named:
        for child in raw.children:
            if child.is_named:
                types(child, source, generic, out)


def extract(path: Path) -> dict:
    try:
        import tree_sitter_go as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-go not installed'}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = read_bytes(path)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    base = stem(path)
    scope = path.parent.name or base
    src = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    bodies: list[tuple[str, object]] = []
    imported: dict[str, str] = {}

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

    def named(name: str, line: int) -> str:
        nid = make(scope, name)

        if nid in seen:
            return nid

        nid = make(name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def methods(raw, caller_id: str, line: int) -> None:
        params = raw.child_by_field_name('parameters')

        if params is not None:
            for param in params.children:
                if param.type != 'parameter_declaration':
                    continue

                type_node = param.child_by_field_name('type')
                refs: list[tuple[str, str]] = []
                types(type_node, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                    tgt = named(ref_name, line)

                    if tgt != caller_id:
                        edge(caller_id, tgt, 'references', line, context=ctx)

        result = raw.child_by_field_name('result')

        if result is not None:
            if result.type == 'parameter_list':
                for param in result.children:
                    if param.type != 'parameter_declaration':
                        continue

                    type_node = param.child_by_field_name('type')

                    if type_node is None:
                        for child in param.children:
                            if child.is_named:
                                type_node = child
                                break

                    refs = []
                    types(type_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        tgt = named(ref_name, line)

                        if tgt != caller_id:
                            edge(caller_id, tgt, 'references', line, context=ctx)
            else:
                refs = []
                types(result, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                    tgt = named(ref_name, line)

                    if tgt != caller_id:
                        edge(caller_id, tgt, 'references', line, context=ctx)

    groups: dict[str, set[str]] = {}

    def receiver(raw) -> str | None:
        recv = raw.child_by_field_name('receiver')

        if not recv:
            return None

        for param in recv.children:
            if param.type == 'parameter_declaration':
                type_node = param.child_by_field_name('type')

                if type_node:
                    return read(type_node, source).lstrip('*').strip()

                break

        return None

    def symbol(raw) -> tuple[str, str] | None:
        name_node = raw.child_by_field_name('name')

        if not name_node:
            return None

        name = read(name_node, source)

        if raw.type == 'method_declaration':
            recv_type = receiver(raw)
            base_scope = make(scope, recv_type) if recv_type else base
        else:
            base_scope = base

        return (make(base_scope, name), name)

    def scan(raw) -> None:
        if raw.type == 'type_spec':
            name_node = raw.child_by_field_name('name')

            if name_node is not None:
                owner = make(scope, read(name_node, source))

                for item in raw.children:
                    if item.type != 'interface_type':
                        continue

                    for elem in item.children:
                        if elem.type != 'method_elem':
                            continue

                        method_name = elem.child_by_field_name('name')

                        if method_name is not None:
                            name = read(method_name, source)
                            nid = make(owner, name)
                            groups.setdefault(nid, set()).add(name)

        if raw.type in ('function_declaration', 'method_declaration'):
            found = symbol(raw)

            if found:
                groups.setdefault(found[0], set()).add(found[1])

            return

        for child in raw.children:
            scan(child)

    def disambiguate(plain_id: str, name: str) -> str:
        names = groups.get(plain_id) or set()

        if len(names) < 2:
            return plain_id

        exported = [n for n in names if n[:1].isupper()]

        if len(exported) == 1 and name == exported[0]:
            return plain_id

        salt = hashlib.sha1(name.encode('utf-8'), usedforsecurity=False).hexdigest()[:6]

        return make(plain_id, salt)

    def walk(raw) -> None:
        kind = raw.type

        if kind == 'function_declaration':
            name_node = raw.child_by_field_name('name')

            if name_node:
                func_name = read(name_node, source)
                line = raw.start_point[0] + 1
                func_id = disambiguate(make(base, func_name), func_name)
                node(func_id, f'{func_name}()', line)
                edge(file_id, func_id, 'contains', line)
                methods(raw, func_id, line)
                body = raw.child_by_field_name('body')

                if body:
                    bodies.append((func_id, body))

            return

        if kind == 'method_declaration':
            recv = raw.child_by_field_name('receiver')
            recv_type: str | None = None

            if recv:
                for param in recv.children:
                    if param.type == 'parameter_declaration':
                        type_node = param.child_by_field_name('type')

                        if type_node:
                            recv_type = read(type_node, source).lstrip('*').strip()

                        break

            name_node = raw.child_by_field_name('name')

            if not name_node:
                return

            method_name = read(name_node, source)
            line = raw.start_point[0] + 1

            if recv_type:
                parent_id = make(scope, recv_type)
                node(parent_id, recv_type, line)
                method_id = disambiguate(make(parent_id, method_name), method_name)
                node(method_id, f'.{method_name}()', line)
                edge(parent_id, method_id, 'method', line)
            else:
                method_id = disambiguate(make(base, method_name), method_name)
                node(method_id, f'{method_name}()', line)
                edge(file_id, method_id, 'contains', line)

            methods(raw, method_id, line)
            body = raw.child_by_field_name('body')

            if body:
                bodies.append((method_id, body))

            return

        if kind == 'type_declaration':
            for child in raw.children:
                if child.type != 'type_spec':
                    continue

                name_node = child.child_by_field_name('name')

                if not name_node:
                    continue

                type_name = read(name_node, source)
                line = child.start_point[0] + 1
                type_id = make(scope, type_name)
                node(type_id, type_name, line)
                edge(file_id, type_id, 'contains', line)
                type_body = None

                for tc in child.children:
                    if tc.type in ('struct_type', 'interface_type'):
                        type_body = tc
                        break

                if type_body is None:
                    continue

                if type_body.type == 'struct_type':
                    for fdl in type_body.children:
                        if fdl.type != 'field_declaration_list':
                            continue

                        for field in fdl.children:
                            if field.type != 'field_declaration':
                                continue

                            has_name = any((fc.type == 'field_identifier' for fc in field.children))
                            type_node = field.child_by_field_name('type')

                            if type_node is None:
                                for fc in field.children:
                                    if fc.is_named and fc.type != 'field_identifier':
                                        type_node = fc
                                        break

                            refs: list[tuple[str, str]] = []
                            types(type_node, source, False, refs)

                            for ref_name, role in refs:
                                tgt = named(ref_name, field.start_point[0] + 1)

                                if tgt == type_id:
                                    continue

                                if not has_name and role == 'type':
                                    edge(type_id, tgt, 'embeds', field.start_point[0] + 1)
                                else:
                                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                    edge(type_id, tgt, 'references', field.start_point[0] + 1, context=ctx)

                elif type_body.type == 'interface_type':
                    for elem in type_body.children:
                        if elem.type == 'method_elem':
                            m_name_node = elem.child_by_field_name('name')

                            if m_name_node is None:
                                for mc in elem.children:
                                    if mc.type == 'field_identifier':
                                        m_name_node = mc
                                        break

                            if m_name_node is None:
                                continue

                            m_name = read(m_name_node, source)
                            m_line = elem.start_point[0] + 1
                            m_id = disambiguate(make(type_id, m_name), m_name)
                            node(m_id, f'.{m_name}()', m_line)
                            edge(type_id, m_id, 'method', m_line)
                            methods(elem, m_id, m_line)

                            continue

                        if elem.type != 'type_elem':
                            continue

                        is_type_set = any((c.type == '|' or c.type == 'negated_type' for c in elem.children))
                        refs = []

                        for sub in elem.children:
                            if sub.is_named:
                                types(sub, source, False, refs)

                        for ref_name, role in refs:
                            tgt = named(ref_name, elem.start_point[0] + 1)

                            if tgt == type_id:
                                continue

                            if is_type_set:
                                edge(type_id, tgt, 'references', elem.start_point[0] + 1, context='type_constraint')
                            elif role == 'type':
                                edge(type_id, tgt, 'embeds', elem.start_point[0] + 1)
                            else:
                                edge(type_id, tgt, 'references', elem.start_point[0] + 1, context='generic_arg')

            return

        if kind == 'import_declaration':
            for child in raw.children:
                if child.type == 'import_spec_list':
                    for spec in child.children:
                        if spec.type == 'import_spec':
                            path_node = spec.child_by_field_name('path')

                            if path_node:
                                val = read(path_node, source).strip('"')
                                tgt_id = make('go', 'pkg', val)
                                edge(file_id, tgt_id, 'imports_from', spec.start_point[0] + 1, context='import')
                                alias = spec.child_by_field_name('name')
                                local_name = read(alias, source) if alias else val.split('/')[-1]

                                if local_name and local_name != '_' and (local_name != '.'):
                                    imported[local_name] = val

                elif child.type == 'import_spec':
                    path_node = child.child_by_field_name('path')

                    if path_node:
                        val = read(path_node, source).strip('"')
                        tgt_id = make('go', 'pkg', val)
                        edge(file_id, tgt_id, 'imports_from', child.start_point[0] + 1, context='import')
                        alias = child.child_by_field_name('name')
                        local_name = read(alias, source) if alias else val.split('/')[-1]

                        if local_name and local_name != '_' and (local_name != '.'):
                            imported[local_name] = val

            return

        for child in raw.children:
            walk(child)

    scan(root)
    walk(root)

    labels: dict[str, str] = {}

    for n in nodes:
        raw_label = n['label']
        clean_label = raw_label.strip('()').lstrip('.')
        labels[clean_label] = n['id']

    pairs: set[tuple[str, str]] = set()
    calls: list[dict] = []

    def trace(raw, caller_id: str) -> None:
        if raw.type in ('function_declaration', 'method_declaration'):
            return

        if raw.type == 'call_expression':
            func_node = raw.child_by_field_name('function')
            callee_name: str | None = None
            is_member: bool = False
            is_bare: bool = False
            pkg_receiver: str | None = None
            import_path: str | None = None

            if func_node:
                if func_node.type == 'identifier':
                    is_bare = True
                    callee_name = read(func_node, source)
                elif func_node.type == 'selector_expression':
                    field = func_node.child_by_field_name('field')
                    operand = func_node.child_by_field_name('operand')
                    receiver_name = read(operand, source) if operand else ''
                    is_member = receiver_name not in imported

                    if not is_member:
                        pkg_receiver = receiver_name
                        import_path = imported[receiver_name]

                    if field:
                        callee_name = read(field, source)

            if is_bare and callee_name in FUNCS:
                callee_name = None

            if callee_name and callee_name not in GLOBALS:
                tgt_id = None if import_path else labels.get(callee_name)

                if tgt_id and tgt_id != caller_id:
                    pair = (caller_id, tgt_id)

                    if pair not in pairs:
                        pairs.add(pair)
                        line = raw.start_point[0] + 1
                        edges.append({
                            'source': caller_id,
                            'target': tgt_id,
                            'relation': 'calls',
                            'context': 'call',
                            'source_file': src,
                            'source_location': f'L{line}',
                        })
                elif callee_name:
                    calls.append({
                        'caller_nid': caller_id,
                        'callee': callee_name,
                        'is_member_call': is_member,
                        'language': 'go',
                        'receiver': pkg_receiver,
                        'import_path': import_path,
                        'source_file': src,
                        'source_location': f'L{raw.start_point[0] + 1}',
                    })

        for child in raw.children:
            trace(child, caller_id)

    for caller_id, body_node in bodies:
        trace(body_node, caller_id)

    clean_edges = []

    for item in edges:
        s, t = item['source'], item['target']

        if s in seen and (t in seen or item['relation'] in ('imports', 'imports_from')):
            clean_edges.append(item)

    return {'nodes': nodes, 'edges': clean_edges, 'raw_calls': calls, 'go_imports': dict(imported)}