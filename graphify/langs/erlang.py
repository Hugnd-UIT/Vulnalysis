from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from tree_sitter import Node
from ..src.base import make, read, stem, bytes


def atom(val: str) -> str:
    val = val.strip()

    if len(val) >= 2 and val[0] == val[-1] == "'":
        return val[1:-1]

    return val


def descendants(n: Node) -> Iterable[Node]:
    for child in n.named_children:
        yield child

        yield from descendants(child)


def resolve(per: list[dict], nodes: list[dict], edges: list[dict]) -> None:

    funcs: dict[tuple[str, str, int], list[str]] = {}

    for n in nodes:
        meta = n.get('metadata')

        if not isinstance(meta, dict) or meta.get('language') != 'erlang':
            continue

        if meta.get('kind') != 'function':
            continue

        mod = meta.get('module')
        name = meta.get('name')
        arity = meta.get('arity')

        if isinstance(mod, str) and isinstance(name, str) and isinstance(arity, int):
            funcs.setdefault((mod, name, arity), []).append(n['id'])

    seen = {(e.get('source'), e.get('target')) for e in edges if e.get('relation') == 'calls'}

    for res in per:
        for c in res.get('raw_calls', []):
            if c.get('language') != 'erlang' or not c.get('remote_module'):
                continue

            k = (str(c['remote_module']), str(c.get('callee', '')), int(c.get('arity', -1)))
            cands = funcs.get(k, [])
            caller = c.get('caller_nid')

            if len(cands) != 1 or cands[0] == caller:
                continue

            pair = (caller, cands[0])

            if pair in seen:
                continue

            seen.add(pair)
            edges.append({
                'source': caller,
                'target': cands[0],
                'relation': 'calls',
                'context': 'remote_call',
                'source_file': c.get('source_file', ''),
                'source_location': c.get('source_location'),
            })


def extract(path: Path) -> dict:
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-language-pack not installed'}

    try:
        src = bytes(path)
        root = Parser(get_language('erlang')).parse(src).root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': f'Erlang failed: {exc}'}

    loc = str(path)
    sname = stem(path)
    fid = make(loc)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    snodes: set[str] = set()
    sedges: set[tuple[str, str, str]] = set()

    def node(nid: str, label: str, n: Node, *, kind: str, backed: bool = True, callable_node: bool = False, meta: dict[str, Any] | None = None) -> str:
        if nid not in snodes:
            snodes.add(nid)
            details: dict[str, Any] = {'language': 'erlang', 'kind': kind}

            if meta:
                details.update(meta)

            item: dict[str, Any] = {
                'id': nid,
                'label': label,
                'file_type': 'code',
                'source_location': f'L{n.start_point[0] + 1}',
                'metadata': details,
            }

            if backed:
                item['source_file'] = loc

            if callable_node:
                item['_callable'] = True

            nodes.append(item)

        return nid

    def edge(src_id: str, dst_id: str, rel: str, n: Node) -> None:
        key = (src_id, dst_id, rel)

        if not src_id or not dst_id or src_id == dst_id or key in sedges:
            return

        sedges.add(key)
        edges.append({
            'source': src_id,
            'target': dst_id,
            'relation': rel,
            'source_file': loc,
            'source_location': f'L{n.start_point[0] + 1}',
        })

    node(fid, path.name, root, kind='file')
    mname = path.stem
    mdecl = next((item for item in root.named_children if item.type == 'module_attribute'), None)
    mid = fid

    if mdecl is not None:
        nnode = mdecl.child_by_field_name('name')

        if nnode is not None:
            mname = atom(read(nnode, src))
            mid = node(make(sname, 'module', mname), mname, mdecl, kind='module', meta={'module': mname})
            edge(fid, mid, 'contains', mdecl)

    funcs: dict[tuple[str, int], str] = {}
    bodies: list[tuple[Node, str]] = []
    exports: list[tuple[str, int, Node]] = []

    for child in root.named_children:
        if child.type == 'export_attribute':
            for fa in (item for item in descendants(child) if item.type == 'fa'):
                anode = next((item for item in descendants(fa) if item.type == 'atom'), None)
                inode = next((item for item in descendants(fa) if item.type == 'integer'), None)

                if anode is not None and inode is not None:
                    exports.append((atom(read(anode, src)), int(read(inode, src)), fa))

            continue

        if child.type == 'behaviour_attribute':
            nnode = child.child_by_field_name('name')

            if nnode is not None:
                bname = atom(read(nnode, src))
                target = node(make('erlang', 'behaviour', bname), bname, nnode, kind='behaviour', backed=False)
                edge(mid, target, 'implements', child)

            continue

        if child.type in {'pp_include', 'pp_include_lib'}:
            snode = next((item for item in child.named_children if item.type == 'string'), None)

            if snode is None:
                continue

            inc = read(snode, src).strip('"')

            if child.type == 'pp_include':
                target_path = path.parent / inc
                target_id = make(str(target_path))
                key = (mid, target_id, 'imports_from')

                if key not in sedges:
                    sedges.add(key)
                    edges.append({
                        'source': mid,
                        'target': target_id,
                        'relation': 'imports_from',
                        'source_file': loc,
                        'source_location': f'L{child.start_point[0] + 1}',
                        'target_file': str(target_path),
                    })
            else:
                target = node(make('erlang', 'include_lib', inc), inc, snode, kind='include_lib', backed=False)
                edge(mid, target, 'imports', child)

            continue

        kinds = {
            'record_decl': ('record', 'name'),
            'type_alias': ('type', 'name'),
            'opaque': ('type', 'name'),
            'pp_define': ('macro', 'lhs'),
        }

        if child.type in kinds:
            kind_str, field_str = kinds[child.type]
            box = child.child_by_field_name(field_str)

            if box is None:
                continue

            if kind_str == 'type':
                nnode = box.child_by_field_name('name')
                tname = atom(read(nnode or box, src))
                args = box.child_by_field_name('args')
                t_arity = len(args.named_children) if args is not None else 0
                lbl = f'{tname}/{t_arity}'
            elif kind_str == 'macro':
                nnode = box.child_by_field_name('name')
                lbl = read(nnode or box, src)
                tname = lbl
            else:
                tname = atom(read(box, src))
                lbl = tname

            decl_id = node(make(mid, kind_str, tname), lbl, child, kind=kind_str, meta={'module': mname, 'name': tname})
            edge(mid, decl_id, 'contains', child)
            continue

        if child.type != 'fun_decl':
            continue

        clauses = [c for c in child.named_children if c.type == 'function_clause']

        for clause in clauses:
            nnode = clause.child_by_field_name('name')
            args = clause.child_by_field_name('args')

            if nnode is None or args is None:
                continue

            fname = atom(read(nnode, src))
            farity = len(args.named_children)
            k = (fname, farity)
            fid_func = funcs.get(k)

            if fid_func is None:
                fid_func = node(make(mid, 'function', fname, str(farity)), f'{fname}/{farity}', clause, kind='function', callable_node=True, meta={'module': mname, 'name': fname, 'arity': farity})
                funcs[k] = fid_func
                edge(mid, fid_func, 'contains', clause)

            fbody = clause.child_by_field_name('body')

            if fbody is not None:
                bodies.append((fbody, fid_func))

    for fname, farity, spec in exports:
        dst = funcs.get((fname, farity))

        if dst is not None:
            edge(mid, dst, 'exports', spec)

    for body_node, caller_id in bodies:
        stack = [body_node]

        while stack:
            cur = stack.pop()

            if cur.type == 'remote':
                rmod = next((c for c in cur.children if c.type == 'remote_module'), None)
                rcall = next((c for c in cur.children if c.type == 'call'), None)

                if rmod and rcall:
                    mod_part = read(rmod, src).strip(': \t\n')
                    atom_node = next((c for c in rcall.children if c.type == 'atom'), None)
                    args_node = next((c for c in rcall.children if c.type == 'expr_args'), None)
                    name_part = read(atom_node, src) if atom_node else ''
                    farity = len(args_node.named_children) if args_node else 0

                    if mod_part and name_part:
                        calls.append({
                            'caller_nid': caller_id,
                            'callee': atom(name_part),
                            'arity': farity,
                            'remote_module': atom(mod_part),
                            'is_member_call': True,
                            'language': 'erlang',
                            'source_file': loc,
                            'source_location': f'L{cur.start_point[0] + 1}',
                        })
            elif cur.type == 'call':
                callee_node = cur.child_by_field_name('expr')
                args = cur.child_by_field_name('args')

                if callee_node is not None and args is not None:
                    farity = len(args.named_children)

                    if callee_node.type == 'atom':
                        fname = atom(read(callee_node, src))
                        dst = funcs.get((fname, farity))

                        if dst is not None:
                            edge(caller_id, dst, 'calls', cur)
                    elif callee_node.type == 'remote':
                        txt = read(callee_node, src)
                        mod_part, sep, name_part = txt.partition(':')

                        if sep:
                            calls.append({
                                'caller_nid': caller_id,
                                'callee': atom(name_part),
                                'arity': farity,
                                'remote_module': atom(mod_part),
                                'is_member_call': True,
                                'language': 'erlang',
                                'source_file': loc,
                                'source_location': f'L{cur.start_point[0] + 1}',
                            })
            elif cur.type == 'internal_fun':
                nnode = cur.child_by_field_name('fun')
                anode = cur.child_by_field_name('arity')

                if nnode is not None and anode is not None:
                    digits = ''.join((ch for ch in read(anode, src) if ch.isdigit()))

                    if digits:
                        fname = atom(read(nnode, src))
                        dst = funcs.get((fname, int(digits)))

                        if dst is not None:
                            edge(caller_id, dst, 'indirect_call', cur)

            stack.extend(reversed(cur.named_children))

    clean = [e for e in edges if e['source'] in snodes and (e['target'] in snodes or e['relation'] == 'imports_from')]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}