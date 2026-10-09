from __future__ import annotations

from pathlib import Path

from ..src.base import bytes as read_bytes, make, read, stem
from ..src.engine import semantic


def extract(path: Path) -> dict:
    try:
        import tree_sitter_julia as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-julia not installed'}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = read_bytes(path)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    base = stem(path)
    src = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    bodies: list[tuple[str, object]] = []
    raw_calls: list[dict] = []

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
        nid = make(base, name)

        if nid in seen:
            return nid

        nid = make(name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def heads(raw) -> tuple[str | None, str | None]:
        bin_expr = next((c for c in raw.children if c.type == 'binary_expression'), None)

        if bin_expr:
            idents = [c for c in bin_expr.children if c.type == 'identifier']

            if idents:
                name = read(idents[0], source)
                super_name = read(idents[-1], source) if len(idents) >= 2 else None

                return (name, super_name)

            return (None, None)

        name_node = next((c for c in raw.children if c.type == 'identifier'), None)

        return (read(name_node, source) if name_node else None, None)

    def sig(raw) -> str | None:
        for child in raw.children:
            if child.type == 'call_expression':
                callee = child.children[0] if child.children else None

                if callee and callee.type == 'identifier':
                    return read(callee, source)

        return None

    def calls(raw, caller_id: str) -> None:
        if raw is None:
            return

        kind = raw.type

        if kind in ('function_definition', 'short_function_definition'):
            return

        if kind == 'call_expression' and raw.children:
            callee = raw.children[0]

            if callee.type == 'identifier':
                callee_name = read(callee, source)
                target_id = make(base, callee_name)

                if target_id in seen:
                    edge(caller_id, target_id, 'calls', raw.start_point[0] + 1, context='call')
                else:
                    raw_calls.append({'caller_nid': caller_id, 'callee': callee_name, 'source_file': src, 'source_location': f'L{raw.start_point[0] + 1}'})
            elif callee.type == 'field_expression' and len(callee.children) >= 3:
                method_node = callee.children[-1]
                method_name = read(method_node, source)
                target_id = make(base, method_name)

                if target_id in seen:
                    edge(caller_id, target_id, 'calls', raw.start_point[0] + 1, context='call')
                else:
                    raw_calls.append({'caller_nid': caller_id, 'callee': method_name, 'source_file': src, 'source_location': f'L{raw.start_point[0] + 1}'})

        for child in raw.children:
            calls(child, caller_id)

    def walk(raw, scope_id: str) -> None:
        kind = raw.type

        if kind == 'module_definition':
            name_node = next((c for c in raw.children if c.type == 'identifier'), None)

            if name_node:
                mod_name = read(name_node, source)
                mod_id = make(base, mod_name)
                line = raw.start_point[0] + 1
                node(mod_id, mod_name, line)
                edge(file_id, mod_id, 'defines', line)

                for child in raw.children:
                    walk(child, mod_id)

            return

        if kind == 'struct_definition':
            head = next((c for c in raw.children if c.type == 'type_head'), None)

            if not head:
                return

            struct_name, super_name = heads(head)

            if not struct_name:
                return

            struct_id = make(base, struct_name)
            line = raw.start_point[0] + 1
            node(struct_id, struct_name, line)
            edge(scope_id, struct_id, 'defines', line)

            if super_name:
                edge(struct_id, named(super_name, line), 'inherits', line)

            for child in raw.children:
                if child.type == 'typed_expression':
                    idents = [c for c in child.children if c.type == 'identifier']

                    if len(idents) >= 2:
                        field_line = child.start_point[0] + 1
                        type_name = read(idents[-1], source)
                        type_id = named(type_name, field_line)
                        edges.append(semantic(struct_id, type_id, 'field', src, field_line))

            return

        if kind == 'abstract_definition':
            head = next((c for c in raw.children if c.type == 'type_head'), None)

            if head:
                abs_name, super_name = heads(head)

                if abs_name:
                    abs_id = make(base, abs_name)
                    line = raw.start_point[0] + 1
                    node(abs_id, abs_name, line)
                    edge(scope_id, abs_id, 'defines', line)

                    if super_name:
                        edge(abs_id, named(super_name, line), 'inherits', line)

            return

        if kind == 'function_definition':
            sig_node = next((c for c in raw.children if c.type == 'signature'), None)

            if sig_node:
                func_name = sig(sig_node)

                if func_name:
                    func_id = make(base, func_name)
                    line = raw.start_point[0] + 1
                    node(func_id, f'{func_name}()', line)
                    edge(scope_id, func_id, 'defines', line)
                    bodies.append((func_id, raw))

            return

        if kind == 'macro_definition':
            sig_node = next((c for c in raw.children if c.type == 'signature'), None)

            if sig_node:
                macro_name = sig(sig_node)

                if macro_name:
                    macro_id = make(base, '@' + macro_name)
                    line = raw.start_point[0] + 1
                    node(macro_id, f'@{macro_name}', line)
                    edge(scope_id, macro_id, 'defines', line)
                    bodies.append((macro_id, raw))

            return

        if kind == 'macrocall_expression':
            macro_id = next((c for c in raw.children if c.type == 'macro_identifier'), None)
            macro_name = None

            if macro_id is not None:
                id_node = next((c for c in macro_id.children if c.type == 'identifier'), None)

                if id_node is not None:
                    macro_name = read(id_node, source)

            if macro_name == 'enum':
                arglist = next((c for c in raw.children if c.type == 'macro_argument_list'), None)
                args = []

                if arglist is not None:
                    for c in arglist.children:
                        if c.type in ('identifier', 'typed_expression', 'assignment'):
                            args.append(c)
                        elif c.type == 'compound_statement':
                            args.extend((cc for cc in c.children if cc.type in ('identifier', 'assignment')))

                def ident(item):
                    if item.type == 'identifier':
                        return item

                    return next((cc for cc in item.children if cc.type == 'identifier'), None)

                type_node = ident(args[0]) if args else None

                if type_node is not None:
                    enum_name = read(type_node, source)
                    line = raw.start_point[0] + 1
                    enum_id = make(base, enum_name)
                    node(enum_id, enum_name, line)
                    edge(scope_id, enum_id, 'defines', line)

                    for member in args[1:]:
                        member_node = ident(member)

                        if member_node is None:
                            continue

                        member_name = read(member_node, source)
                        member_line = member_node.start_point[0] + 1
                        member_id = make(base, enum_name, member_name)
                        node(member_id, member_name, member_line)
                        edge(enum_id, member_id, 'case_of', member_line)

                return

            for child in raw.children:
                walk(child, scope_id)

            return

        if kind == 'assignment':
            lhs = raw.children[0] if raw.children else None

            if lhs and lhs.type == 'call_expression' and lhs.children:
                callee = lhs.children[0]

                if callee.type == 'identifier':
                    func_name = read(callee, source)
                    func_id = make(base, func_name)
                    line = raw.start_point[0] + 1
                    node(func_id, f'{func_name}()', line)
                    edge(scope_id, func_id, 'defines', line)
                    rhs = raw.children[-1] if len(raw.children) >= 3 else None

                    if rhs:
                        bodies.append((func_id, rhs))

            return

        if kind in ('using_statement', 'import_statement'):
            line = raw.start_point[0] + 1

            def modname(item):
                if item.type == 'import_path':
                    idents = [c for c in item.children if c.type == 'identifier']

                    return read(idents[-1], source) if idents else None

                if item.type in ('identifier', 'scoped_identifier'):
                    return read(item, source)

                return None

            def imported(name):
                if not name:
                    return

                imp_id = make(name)
                node(imp_id, name, line)
                edge(scope_id, imp_id, 'imports', line, context='import')

            for child in raw.children:
                if child.type in ('identifier', 'scoped_identifier', 'import_path'):
                    imported(modname(child))
                elif child.type == 'selected_import':
                    pkg = next((c for c in child.children if c.type in ('identifier', 'scoped_identifier', 'import_path')), None)

                    if pkg is not None:
                        imported(modname(pkg))

            return

        for child in raw.children:
            walk(child, scope_id)

    walk(root, file_id)

    for caller_id, body_node in bodies:
        if body_node.type in ('function_definition', 'macro_definition'):
            for child in body_node.children:
                if child.type != 'signature':
                    calls(child, caller_id)
        else:
            calls(body_node, caller_id)

    return {'nodes': nodes, 'edges': edges, 'raw_calls': raw_calls}