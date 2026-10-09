from __future__ import annotations

from pathlib import Path
from typing import Any

from ..src.base import bytes, make, read, stem


def tagged(n) -> bool:
    return n.type == 'union_declaration' and any((child.type == '(' for child in n.children))


def extract(path: Path) -> dict:
    try:
        import tree_sitter_zig as tszig
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_zig not installed'}

    try:
        lang = Language(tszig.language())
        parser = Parser(lang)
        src = bytes(path)
        tree = parser.parse(src)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    sname = stem(path)
    loc = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    bodies: list[tuple[str, Any]] = []

    def node(nid: str, label: str, line: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': loc, 'source_location': f'L{line}'})

    def edge(src_id: str, dst_id: str, rel: str, line: int, ctx: str | None = None) -> None:
        item = {'source': src_id, 'target': dst_id, 'relation': rel, 'source_file': loc, 'source_location': f'L{line}'}

        if ctx:
            item['context'] = ctx

        edges.append(item)

    fnid = make(loc)
    node(fnid, path.name, 1)

    def imports(n) -> None:
        for child in n.children:
            if child.type == 'builtin_function':
                bi = None
                args = None

                for c in child.children:
                    if c.type == 'builtin_identifier':
                        bi = read(c, src)
                    elif c.type == 'arguments':
                        args = c

                if bi in ('@import', '@cImport') and args:
                    for arg in args.children:
                        if arg.type in ('string_literal', 'string'):
                            raw = read(arg, src).strip('"')
                            mod = raw.split('/')[-1].split('.')[0]

                            if mod:
                                tnid = make(mod)
                                edge(fnid, tnid, 'imports_from', n.start_point[0] + 1)

                            return
            elif child.type == 'field_expression':
                imports(child)
                return

    def walk(n, parent_nid: str | None = None) -> None:
        t = n.type

        if t == 'container_field' and parent_nid and n.parent is not None and (n.parent.type == 'enum_declaration' or tagged(n.parent)):
            nnode = n.child_by_field_name('name')

            if nnode is None:
                nnode = next((c for c in n.children if c.type == 'identifier'), None)

            if nnode is not None:
                mname = read(nnode, src)

                if mname:
                    line = n.start_point[0] + 1
                    mnid = make(parent_nid, mname)
                    node(mnid, mname, line)
                    edge(parent_nid, mnid, 'case_of', line)

            return

        if t == 'function_declaration':
            nnode = n.child_by_field_name('name')

            if nnode:
                fname = read(nnode, src)
                line = n.start_point[0] + 1

                if parent_nid:
                    func_nid = make(parent_nid, fname)
                    node(func_nid, f'.{fname}()', line)
                    edge(parent_nid, func_nid, 'method', line)
                else:
                    func_nid = make(sname, fname)
                    node(func_nid, f'{fname}()', line)
                    edge(fnid, func_nid, 'contains', line)

                body = n.child_by_field_name('body')

                if body:
                    bodies.append((func_nid, body))

            return

        if t == 'variable_declaration':
            nnode = None
            vnode = None

            for child in n.children:
                if child.type == 'identifier':
                    nnode = child
                elif child.type in ('struct_declaration', 'enum_declaration', 'union_declaration', 'error_set_declaration', 'builtin_function', 'field_expression'):
                    vnode = child

            if vnode and vnode.type == 'struct_declaration':
                if nnode:
                    s_name = read(nnode, src)
                    line = n.start_point[0] + 1
                    snid = make(sname, s_name)
                    node(snid, s_name, line)
                    edge(fnid, snid, 'contains', line)

                    for child in vnode.children:
                        walk(child, parent_nid=snid)

                return

            if vnode and vnode.type in ('enum_declaration', 'union_declaration'):
                if nnode:
                    t_name = read(nnode, src)
                    line = n.start_point[0] + 1
                    tnid = make(sname, t_name)
                    node(tnid, t_name, line)
                    edge(fnid, tnid, 'contains', line)

                    for child in vnode.children:
                        walk(child, parent_nid=tnid)

                return

            if vnode and vnode.type == 'error_set_declaration':
                if nnode:
                    t_name = read(nnode, src)
                    line = n.start_point[0] + 1
                    tnid = make(sname, t_name)
                    node(tnid, t_name, line)
                    edge(fnid, tnid, 'contains', line)

                    for child in vnode.children:
                        if child.type != 'identifier':
                            continue

                        m_name = read(child, src)

                        if not m_name:
                            continue

                        mline = child.start_point[0] + 1
                        mnid = make(tnid, m_name)
                        node(mnid, m_name, mline)
                        edge(tnid, mnid, 'case_of', mline)

                return

            if vnode and vnode.type in ('builtin_function', 'field_expression'):
                imports(n)

            return

        for child in n.children:
            walk(child, parent_nid)

    walk(root)
    pairs: set[tuple[str, str]] = set()
    raw_calls: list[dict] = []

    def calls(n, caller_id: str) -> None:
        if n.type == 'function_declaration':
            return

        if n.type == 'call_expression':
            fn = n.child_by_field_name('function')

            if fn:
                txt = read(fn, src)
                callee = txt.split('.')[-1]
                member = '.' in txt
                target_id = next((item['id'] for item in nodes if item['label'] in (f'{callee}()', f'.{callee}()')), None)

                if target_id and target_id != caller_id:
                    pair = (caller_id, target_id)

                    if pair not in pairs:
                        pairs.add(pair)
                        edge(caller_id, target_id, 'calls', n.start_point[0] + 1)
                elif callee:
                    raw_calls.append({
                        'caller_nid': caller_id,
                        'callee': callee,
                        'is_member_call': member,
                        'source_file': loc,
                        'source_location': f'L{n.start_point[0] + 1}',
                    })

        for child in n.children:
            calls(child, caller_id)

    for cid, bnode in bodies:
        calls(bnode, cid)

    clean = [e for e in edges if e['source'] in seen and (e['target'] in seen or e['relation'] == 'imports_from')]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': raw_calls}