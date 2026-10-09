from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..src.base import stem, make, bytes as read_bytes, read


TYPES = {
    'contract_declaration': 'contract',
    'interface_declaration': 'interface',
    'library_declaration': 'library',
}


def unquote(val: str) -> str:
    val = val.strip()

    if len(val) >= 2 and val[0] == val[-1] and (val[0] in '\'"'):
        return val[1:-1]

    return val


def resolve(items: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    lookup = {node.get('id'): node for node in nodes}
    files = {str(node['source_file']): node['id'] for node in nodes if node.get('source_file') and node.get('label') == Path(str(node['source_file'])).name}
    sources = {nid: src for src, nid in files.items()}
    imported: dict[str, set[str]] = {}

    for edge in edges:
        if edge.get('relation') != 'imports_from':
            continue

        src = sources.get(edge.get('source'))
        tgt = sources.get(edge.get('target'))

        if src and tgt:
            imported.setdefault(src, set()).add(tgt)

    declared: dict[tuple[str, str], list[str]] = {}

    for node in nodes:
        meta = node.get('metadata')

        if not isinstance(meta, dict) or meta.get('kind') not in TYPES.values():
            continue

        src = node.get('source_file')

        if src:
            declared.setdefault((str(src), str(node.get('label', ''))), []).append(node['id'])

    for edge in edges:
        ctx = edge.get('context')

        if not isinstance(ctx, str) or not ctx.startswith('solidity_type:'):
            continue

        src_node = lookup.get(edge.get('source'))

        if not src_node or not src_node.get('source_file'):
            continue

        src = str(src_node['source_file'])
        name = ctx.split(':', 1)[1]
        matches = list(declared.get((src, name), []))

        for imp in imported.get(src, set()):
            matches.extend(declared.get((imp, name), []))

        if len(matches) == 1:
            edge['target'] = matches[0]


def extract(path: Path) -> dict:
    try:
        import tree_sitter_solidity
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-solidity not installed'}

    try:
        source = read_bytes(path)
        root = Parser(Language(tree_sitter_solidity.language())).parse(source).root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': f'Solidity grammar failed to load: {exc}'}

    src = str(path)
    base = stem(path)
    file_id = make(src)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    pairs: set[tuple[str, str, str]] = set()

    def node(nid: str, label: str, raw: Node, *, kind: str, backed: bool = True, callable: bool = False) -> str:
        if nid not in seen:
            seen.add(nid)
            item: dict[str, Any] = {
                'id': nid,
                'label': label,
                'file_type': 'code',
                'source_location': f'L{raw.start_point[0] + 1}',
                'metadata': {'language': 'solidity', 'kind': kind},
            }

            if backed:
                item['source_file'] = src

            if callable:
                item['_callable'] = True

            nodes.append(item)

        return nid

    def edge(source_id: str, target_id: str, relation: str, raw: Node, *, context: str | None = None, target_file: str | None = None) -> None:
        key = (source_id, target_id, relation)

        if not source_id or not target_id or source_id == target_id or (key in pairs):
            return

        pairs.add(key)
        item: dict[str, Any] = {
            'source': source_id,
            'target': target_id,
            'relation': relation,
            'source_file': src,
            'source_location': f'L{raw.start_point[0] + 1}',
        }

        if context:
            item['context'] = context

        if target_file:
            item['target_file'] = target_file

        edges.append(item)

    node(file_id, path.name, root, kind='file')

    def stub(name: str, raw: Node) -> str:
        name = name.split('.')[-1]
        nid = make('solidity', 'type', name)
        node(nid, name, raw, kind='external_type', backed=False)

        return nid

    for decl in root.named_children:
        if decl.type != 'import_directive':
            continue

        source_node = decl.child_by_field_name('source')

        if source_node is None:
            continue

        rel = unquote(read(source_node, source))

        if not rel or rel.startswith(('http:', 'https:')):
            continue

        tgt = path.parent / rel
        edge(file_id, make(str(tgt)), 'imports_from', decl, target_file=str(tgt))

    for decl in root.named_children:
        kind = TYPES.get(decl.type)

        if kind is None:
            continue

        name_node = decl.child_by_field_name('name')
        body = decl.child_by_field_name('body')

        if name_node is None or body is None:
            continue

        name = read(name_node, source)
        type_id = node(make(base, kind, name), name, decl, kind=kind, callable=kind == 'contract')
        edge(file_id, type_id, 'contains', decl)

        for child in decl.named_children:
            if child.type != 'inheritance_specifier':
                continue

            ancestor = child.child_by_field_name('ancestor')

            if ancestor is None:
                continue

            base_name = read(ancestor, source).split('.')[-1]
            edge(type_id, stub(base_name, child), 'inherits', child, context=f'solidity_type:{base_name}')

        funcs: dict[tuple[str, int], list[str]] = {}
        mods: dict[str, str] = {}
        refs: dict[tuple[str, int], str] = {}
        bodies: list[tuple[Node, str]] = []
        pending: list[tuple[str, str, Node]] = []

        def member(raw: Node, name: str, label: str, kind: str, *, relation: str = 'contains', signature: str = '', callable: bool = False) -> str:
            mid = make(type_id, kind, name, signature or name)
            node(mid, label, raw, kind=kind, callable=callable)
            edge(type_id, mid, relation, raw)

            return mid

        for item in body.named_children:
            kind = item.type

            if kind == 'using_directive':
                alias = next((read(child, source) for child in item.named_children if child.type in {'type_alias', 'identifier', 'user_defined_type'}), '')

                if alias:
                    val = alias.split('.')[-1]
                    edge(type_id, stub(val, item), 'uses', item, context=f'solidity_type:{val}')

                continue

            if kind == 'struct_declaration':
                sname_node = item.child_by_field_name('name')
                sbody = item.child_by_field_name('body')

                if sname_node is None:
                    continue

                sname = read(sname_node, source)
                sid = member(item, sname, sname, 'struct')

                if sbody is not None:
                    for field in sbody.named_children:
                        if field.type != 'struct_member':
                            continue

                        fname_node = field.child_by_field_name('name')

                        if fname_node is None:
                            continue

                        fname = read(fname_node, source)
                        fid = make(sid, 'field', fname)
                        node(fid, fname, field, kind='field')
                        edge(sid, fid, 'contains', field)

                continue

            if kind == 'enum_declaration':
                ename_node = item.child_by_field_name('name')
                ebody = item.child_by_field_name('body')

                if ename_node is None:
                    continue

                ename = read(ename_node, source)
                eid = member(item, ename, ename, 'enum')

                if ebody is not None:
                    for val in ebody.named_children:
                        if val.type != 'enum_value':
                            continue

                        vname = read(val, source)
                        vid = make(eid, vname)
                        node(vid, vname, val, kind='enum_value')
                        edge(eid, vid, 'case_of', val)

                continue

            simple = {'event_definition': 'event', 'error_declaration': 'error', 'state_variable_declaration': 'state_variable'}

            if kind in simple:
                mname_node = item.child_by_field_name('name')

                if mname_node is None:
                    continue

                mname = read(mname_node, source)
                mid = member(item, mname, mname, simple[kind])
                arity = sum((child.type in {'event_parameter', 'parameter'} for child in item.named_children))
                refs[mname, arity] = mid

                continue

            if kind not in {'function_definition', 'constructor_definition', 'modifier_definition', 'fallback_receive_definition'}:
                continue

            name_node = item.child_by_field_name('name')

            if kind == 'constructor_definition':
                name = 'constructor'
                ckind = 'constructor'
            elif kind == 'fallback_receive_definition':
                raw_text = read(item, source).lstrip()
                name = 'receive' if raw_text.startswith('receive') else 'fallback'
                ckind = name
            else:
                if name_node is None:
                    continue

                name = read(name_node, source)
                ckind = 'modifier' if kind == 'modifier_definition' else 'function'

            params = [child for child in item.named_children if child.type in {'parameter', 'fallback_receive_parameter'}]
            sig = ','.join((read(child.child_by_field_name('type'), source) for child in params if child.child_by_field_name('type') is not None))
            arity = len(params)
            mid = member(item, name, f'{name}()', ckind, relation='method', signature=f'{arity}:{sig}', callable=True)

            if ckind == 'modifier':
                mods[name] = mid
            else:
                funcs.setdefault((name, arity), []).append(mid)

            mbody = item.child_by_field_name('body')

            if mbody is not None:
                bodies.append((mbody, mid))

            if ckind == 'function':
                for child in item.named_children:
                    if child.type != 'modifier_invocation':
                        continue

                    mod_name = next((read(sub, source) for sub in child.named_children if sub.type == 'identifier'), '')

                    if mod_name:
                        pending.append((mid, mod_name, child))

        for caller_id, mod_name, mod_node in pending:
            tgt = mods.get(mod_name)

            if tgt:
                edge(caller_id, tgt, 'uses', mod_node)

        for fbody, caller_id in bodies:
            stack = [fbody]

            while stack:
                curr = stack.pop()

                if curr.type == 'call_expression':
                    callee_node = curr.child_by_field_name('function')

                    if callee_node is not None:
                        written = read(callee_node, source)
                        callee = written.split('.')[-1]
                        arity = sum((child.type == 'call_argument' for child in curr.named_children))
                        cands: list[str] = []

                        if '.' not in written or written.startswith('this.'):
                            cands = funcs.get((callee, arity), [])

                        if len(cands) == 1:
                            edge(caller_id, cands[0], 'calls', curr)
                        else:
                            ref = refs.get((callee, arity))

                            if ref is not None and '.' not in written:
                                edge(caller_id, ref, 'references', curr)
                            elif callee and '.' in written:
                                calls.append({
                                    'caller_nid': caller_id,
                                    'callee': callee,
                                    'is_member_call': True,
                                    'language': 'solidity',
                                    'source_file': src,
                                    'source_location': f'L{curr.start_point[0] + 1}',
                                })

                stack.extend(reversed(curr.named_children))

    free_funcs: dict[tuple[str, int], list[str]] = {}
    free_bodies: list[tuple[Node, str]] = []

    for decl in root.named_children:
        if decl.type != 'function_definition':
            continue

        name_node = decl.child_by_field_name('name')

        if name_node is None:
            continue

        name = read(name_node, source)
        params = [child for child in decl.named_children if child.type == 'parameter']
        sig = ','.join((read(child.child_by_field_name('type'), source) for child in params if child.child_by_field_name('type') is not None))
        arity = len(params)
        fid = node(make(base, 'function', name, f'{arity}:{sig}'), f'{name}()', decl, kind='function', callable=True)
        edge(file_id, fid, 'contains', decl)
        free_funcs.setdefault((name, arity), []).append(fid)
        body = decl.child_by_field_name('body')

        if body is not None:
            free_bodies.append((body, fid))

    for fbody, caller_id in free_bodies:
        stack = [fbody]

        while stack:
            curr = stack.pop()

            if curr.type == 'call_expression':
                callee_node = curr.child_by_field_name('function')

                if callee_node is not None:
                    written = read(callee_node, source)
                    callee = written.split('.')[-1]
                    arity = sum((child.type == 'call_argument' for child in curr.named_children))

                    if '.' not in written:
                        cands = free_funcs.get((callee, arity), [])

                        if len(cands) == 1:
                            edge(caller_id, cands[0], 'calls', curr)
                    elif callee:
                        calls.append({
                            'caller_nid': caller_id,
                            'callee': callee,
                            'is_member_call': True,
                            'language': 'solidity',
                            'source_file': src,
                            'source_location': f'L{curr.start_point[0] + 1}',
                        })

            stack.extend(reversed(curr.named_children))

    clean = [edge for edge in edges if edge['source'] in seen and (edge['target'] in seen or edge['relation'] == 'imports_from')]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}