from __future__ import annotations

from pathlib import Path

from ..src.base import bytes as read_bytes, make, read, stem


def extract(path: Path) -> dict:
    try:
        import tree_sitter_ocaml as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-ocaml not installed'}

    try:
        if path.suffix == '.mli':
            language = Language(ts.language_ocaml_interface())
        else:
            language = Language(ts.language_ocaml())

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
    defs: dict[str, str] = {}
    ambiguous: set[str] = set()
    mods: set[str] = set()
    sites: list[tuple[str, str, str | None, str, int]] = []

    def node(nid: str, label: str, lineno: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': src, 'source_location': f'L{lineno}'})

    def edge(source_id: str, target_id: str, relation: str, lineno: int) -> None:
        edges.append({'source': source_id, 'target': target_id, 'relation': relation, 'source_file': src, 'source_location': f'L{lineno}'})

    file_id = make(src)
    node(file_id, path.name, 1)

    def stub(name: str) -> str:
        nid = make(name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def line(raw) -> int:
        return raw.start_point[0] + 1

    def body(binding):
        stack = list(binding.children)

        while stack:
            n = stack.pop()

            if n.type in ('object_expression', 'class_body_type'):
                return n

            stack.extend(n.children)

        return None

    def text(raw, kind: str) -> str | None:
        for child in raw.children:
            if child.type == kind:
                return read(child, source)

        return None

    def last(path_node) -> str | None:
        found: str | None = None

        for n in path_node.children:
            if n.type in ('value_name', 'module_name', 'constructor_name'):
                found = read(n, source)

        return found

    def rootmod(path_node) -> str | None:
        mp = next((c for c in path_node.children if c.type == 'module_path'), None)

        if mp is None:
            return None

        curr = mp

        while True:
            inner = next((c for c in curr.children if c.type == 'module_path'), None)

            if inner is None:
                break

            curr = inner

        mn = next((c for c in curr.children if c.type == 'module_name'), None)

        return read(mn, source) if mn is not None else None

    def record(name: str, nid: str) -> None:
        if name in ambiguous:
            return

        if name in defs and defs[name] != nid:
            ambiguous.add(name)
            defs.pop(name, None)

            return

        defs[name] = nid

    def types(binding, container_id: str) -> None:
        name = text(binding, 'type_constructor')

        if not name:
            return

        lineno = line(binding)
        nid = make(base, name)
        node(nid, name, lineno)
        edge(container_id, nid, 'defines' if container_id == file_id else 'contains', lineno)
        record(name, nid)

        for vd in binding.children:
            if vd.type != 'variant_declaration':
                continue

            for cd in vd.children:
                if cd.type != 'constructor_declaration':
                    continue

                cname = text(cd, 'constructor_name')

                if not cname:
                    continue

                cid = make(base, name, cname)
                node(cid, cname, line(cd))
                edge(nid, cid, 'contains', line(cd))

    def walk(raw, container_id: str, scope: str) -> None:
        kind = raw.type

        if kind == 'open_module':
            mp = next((c for c in raw.children if c.type == 'module_path'), None)
            name = last(mp) if mp is not None else None

            if name:
                edge(container_id, stub(name), 'imports_from', line(raw))

            return

        if kind == 'module_definition':
            binding = next((c for c in raw.children if c.type == 'module_binding'), None)

            if binding is not None:
                mname = text(binding, 'module_name')

                if mname:
                    lineno = line(raw)
                    mnid = make(base, mname)
                    node(mnid, mname, lineno)
                    edge(container_id, mnid, 'defines' if container_id == file_id else 'contains', lineno)
                    record(mname, mnid)
                    mods.add(mname)

                    for child in binding.children:
                        walk(child, mnid, scope)

                    return

        if kind == 'module_type_definition':
            mname = text(raw, 'module_type_name')

            if mname:
                lineno = line(raw)
                mnid = make(base, mname)
                node(mnid, mname, lineno)
                edge(container_id, mnid, 'defines' if container_id == file_id else 'contains', lineno)
                record(mname, mnid)
                mods.add(mname)

                for child in raw.children:
                    walk(child, mnid, scope)

                return

        if kind == 'value_definition':
            toplevel = raw.parent is not None and raw.parent.type in ('compilation_unit', 'structure')

            for lb in raw.children:
                if lb.type != 'let_binding':
                    continue

                vname = text(lb, 'value_name')
                sub_scope = scope

                if vname and toplevel:
                    lineno = line(lb)
                    nid = make(base, vname)
                    node(nid, vname, lineno)
                    edge(container_id, nid, 'defines' if container_id == file_id else 'contains', lineno)
                    record(vname, nid)
                    sub_scope = nid

                for child in lb.children:
                    walk(child, container_id, sub_scope)

            return

        if kind == 'value_specification':
            vname = text(raw, 'value_name')

            if vname:
                lineno = line(raw)
                nid = make(base, vname)
                node(nid, vname, lineno)
                edge(container_id, nid, 'defines' if container_id == file_id else 'contains', lineno)
                record(vname, nid)

            return

        if kind == 'type_definition':
            for binding in raw.children:
                if binding.type == 'type_binding':
                    types(binding, container_id)

            return

        if kind == 'class_definition':
            binding = next((c for c in raw.children if c.type == 'class_binding'), None)

            if binding is not None:
                cname = text(binding, 'class_name')

                if cname:
                    lineno = line(raw)
                    cnid = make(base, cname)
                    node(cnid, cname, lineno)
                    edge(container_id, cnid, 'defines' if container_id == file_id else 'contains', lineno)
                    record(cname, cnid)
                    obj = body(binding)

                    if obj is not None:
                        for member in obj.children:
                            if member.type in ('method_definition', 'method_specification'):
                                mname = text(member, 'method_name')

                                if not mname:
                                    continue

                                mline = line(member)
                                mnid = make(base, cname, mname)
                                node(mnid, mname, mline)
                                edge(cnid, mnid, 'method', mline)

                                for child in member.children:
                                    walk(child, cnid, mnid)
                            elif member.type in ('instance_variable_definition', 'instance_variable_specification'):
                                vname = text(member, 'instance_variable_name')

                                if not vname:
                                    continue

                                vline = line(member)
                                vnid = make(base, cname, vname)
                                node(vnid, vname, vline)
                                edge(cnid, vnid, 'contains', vline)

                    return

        if kind == 'application_expression':
            fn = raw.named_children[0] if raw.named_children else None

            if fn is not None and fn.type == 'value_path':
                callee = last(fn)

                if callee:
                    caller = scope if scope else file_id
                    sites.append((caller, callee, rootmod(fn), read(fn, source), line(raw)))

        for child in raw.children:
            walk(child, container_id, scope)

    walk(root, file_id, '')

    for caller, callee, qualifier, full, lineno in sites:
        if qualifier is not None and qualifier not in mods:
            if callee in defs:
                edge(caller, stub(full), 'calls', lineno)
            else:
                edge(caller, stub(callee), 'calls', lineno)
        elif callee in defs:
            edge(caller, defs[callee], 'calls', lineno)
        else:
            edge(caller, stub(callee), 'calls', lineno)

    return {'nodes': nodes, 'edges': edges}