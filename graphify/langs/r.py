from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..src.base import bytes as read_bytes, make, read, stem


CONSTRUCTORS = frozenset({'R6Class', 'setRefClass', 'ggproto'})
SYSTEM = frozenset({'R6Class', 'c', 'get', 'ggproto', 'library', 'list', 'print', 'require', 'setClass', 'setGeneric', 'setMethod', 'setRefClass', 'source'})


def unquote(val: str) -> str:
    val = val.strip()

    if len(val) >= 2 and val[0] == val[-1] and (val[0] in '\'"'):
        return val[1:-1]

    return val


def resolve(items: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    sources = {node['id']: str(node['source_file']) for node in nodes if node.get('source_file') and node.get('label') == Path(str(node['source_file'])).name}
    files: dict[str, set[str]] = {}

    for edge in edges:
        if edge.get('relation') != 'imports_from':
            continue

        tgt = edge.get('target')

        if not isinstance(tgt, str):
            continue

        target_source = sources.get(tgt)

        if target_source:
            files.setdefault(edge['source'], set()).add(target_source)

    lookup = {src: nid for nid, src in sources.items()}
    callables: dict[tuple[str, str], list[str]] = {}

    for node in nodes:
        if not node.get('_callable') or not node.get('source_file'):
            continue

        name = str(node.get('label', '')).strip('()')
        callables.setdefault((str(node['source_file']), name), []).append(node['id'])

    pairs = {(edge.get('source'), edge.get('target')) for edge in edges if edge.get('relation') == 'calls'}

    for res in items:
        for call in res.get('raw_calls', []):
            if call.get('language') != 'r':
                continue

            caller = call.get('caller_nid')
            caller_file = lookup.get(str(call.get('source_file', '')))

            if caller_file is None:
                continue

            cands: list[str] = []

            for src in files.get(caller_file, set()):
                cands.extend(callables.get((src, str(call.get('callee', ''))), []))

            if len(cands) != 1 or cands[0] == caller:
                continue

            pair = (caller, cands[0])

            if pair in pairs:
                continue

            pairs.add(pair)
            edges.append({
                'source': caller,
                'target': cands[0],
                'relation': 'calls',
                'context': 'sourced_call',
                'source_file': call.get('source_file', ''),
                'source_location': call.get('source_location'),
            })


def extract(path: Path) -> dict:
    try:
        from tree_sitter import Parser
        from tree_sitter_language_pack import get_language
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-language-pack not installed'}

    try:
        source = read_bytes(path)
        root = Parser(get_language('r')).parse(source).root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': f'R grammar failed to load: {exc}'}

    src = str(path)
    base = stem(path)
    file_id = make(src)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    pairs: set[tuple[str, str, str]] = set()
    defs: dict[str, dict[str, str]] = {file_id: {}}
    parents: dict[str, str] = {}
    bodies: list[tuple[Node, str]] = []
    classes: dict[str, str] = {}

    def node(nid: str, label: str, line: int, *, kind: str, backed: bool = True, callable: bool = False) -> None:
        if not nid or nid in seen:
            return

        seen.add(nid)
        item: dict[str, Any] = {
            'id': nid,
            'label': label,
            'file_type': 'code',
            'source_location': f'L{line}',
            'metadata': {'language': 'r', 'kind': kind},
        }

        if backed:
            item['source_file'] = src

        if callable:
            item['_callable'] = True

        nodes.append(item)

    def edge(source_id: str, target_id: str, relation: str, line: int, *, target_file: str | None = None) -> None:
        key = (source_id, target_id, relation)

        if not source_id or not target_id or source_id == target_id or (key in pairs):
            return

        pairs.add(key)
        item: dict[str, Any] = {
            'source': source_id,
            'target': target_id,
            'relation': relation,
            'source_file': src,
            'source_location': f'L{line}',
        }

        if target_file is not None:
            item['target_file'] = target_file

        edges.append(item)

    node(file_id, path.name, 1, kind='file')

    def argval(argument: Node) -> Node | None:
        name_node = argument.child_by_field_name('name')
        values = [child for child in argument.named_children if child != name_node]

        return values[-1] if values else None

    def parts(raw: Node) -> tuple[str, Node | None]:
        func = raw.child_by_field_name('function')
        args = raw.child_by_field_name('arguments')

        if func is None:
            return ('', args)

        return (read(func, source), args)

    def ident(raw: Node | None) -> str:
        if raw is None:
            return ''

        return unquote(read(raw, source))

    def bindid(owner: str, name: str) -> str:
        suf = make(name)

        if not suf:
            return f"{owner}__operator__{name.encode('utf-8').hex()}"

        return f'{owner}_{suf}'

    def function(name: str, raw_func: Node, body: Node | None, owner: str, relation: str = 'contains') -> str:
        nid = bindid(owner, name)
        line = raw_func.start_point[0] + 1
        node(nid, f'{name}()', line, kind='function', callable=True)
        edge(owner, nid, relation, line)
        defs.setdefault(owner, {})[name] = nid
        defs.setdefault(nid, {})
        parents[nid] = owner

        if body is not None:
            bodies.append((body, nid))
            declarations(body, nid, top=False)

        return nid

    def cls(name: str, raw_decl: Node, owner: str = file_id) -> str:
        nid = make(base, 'class', name)
        line = raw_decl.start_point[0] + 1
        node(nid, name, line, kind='class', callable=True)
        edge(owner, nid, 'contains', line)
        defs.setdefault(nid, {})
        parents[nid] = owner
        classes[name] = nid

        return nid

    def members(class_id: str, args: Node) -> None:
        for arg in args.named_children:
            if arg.type != 'argument':
                continue

            key_node = arg.child_by_field_name('name')
            val = argval(arg)
            key = read(key_node, source) if key_node is not None else ''

            if key == 'inherit' and val is not None:
                base_name = ident(val).split('$')[-1]

                if base_name:
                    base_id = classes.get(base_name, make('r', 'class', base_name))
                    node(base_id, base_name, val.start_point[0] + 1, kind='external_class', backed=False)
                    edge(class_id, base_id, 'inherits', val.start_point[0] + 1)

            if key not in {'public', 'private', 'active', 'methods'} or val is None:
                continue

            if val.type != 'call':
                continue

            list_name, list_args = parts(val)

            if list_name != 'list' or list_args is None:
                continue

            for mem in list_args.named_children:
                if mem.type != 'argument':
                    continue

                mem_name_node = mem.child_by_field_name('name')
                mem_val = argval(mem)

                if mem_name_node is None or mem_val is None or mem_val.type != 'function_definition':
                    continue

                mem_name = read(mem_name_node, source)
                function(mem_name, mem_val, mem_val.child_by_field_name('body'), class_id, relation='method')

    def process(raw: Node, owner: str) -> bool:
        call_name, args = parts(raw)
        call_name = call_name.split('::')[-1].strip()

        if args is None:
            return False

        arg_list = [child for child in args.named_children if child.type == 'argument']

        if call_name in {'library', 'require'} and arg_list:
            pkg = ident(argval(arg_list[0]))

            if pkg:
                pkg_id = make('r', 'package', pkg)
                node(pkg_id, pkg, raw.start_point[0] + 1, kind='package', backed=False)
                edge(file_id, pkg_id, 'imports', raw.start_point[0] + 1)

            return True

        if call_name == 'source' and arg_list:
            rel = ident(argval(arg_list[0]))

            if rel and '$' not in rel and ('(' not in rel):
                target = path.parent / rel
                edge(file_id, make(str(target)), 'imports_from', raw.start_point[0] + 1, target_file=str(target))

            return True

        if call_name in {'setClass', 'setRefClass'} and arg_list:
            name = ident(argval(arg_list[0]))

            if name:
                class_id = classes.get(name) or cls(name, raw, owner)
                members(class_id, args)

            return True

        if call_name == 'setMethod' and len(arg_list) >= 3:
            method_name = ident(argval(arg_list[0]))
            class_name = ident(argval(arg_list[1]))
            impl = argval(arg_list[-1])

            if method_name and class_name and (impl is not None):
                class_id = classes.get(class_name) or cls(class_name, raw, owner)

                if impl.type == 'function_definition':
                    function(method_name, impl, impl.child_by_field_name('body'), class_id, relation='method')

            return True

        if call_name == 'setGeneric' and arg_list:
            name = ident(argval(arg_list[0]))

            if name:
                function(name, raw, None, owner)

            return True

        return False

    def binding(raw: Node | None) -> tuple[str, Node, Node | None] | None:
        if raw is None or raw.type != 'binary_operator':
            return None

        lhs = raw.child_by_field_name('lhs')
        rhs = raw.child_by_field_name('rhs')
        op_node = raw.child_by_field_name('operator')

        if lhs is None or rhs is None or op_node is None:
            return None

        op = read(op_node, source)

        if op in {'->', '->>'}:
            lhs, rhs = (rhs, lhs)
        elif op not in {'<-', '<<-', '='}:
            return None

        if lhs.type != 'identifier':
            return None

        return (read(lhs, source), rhs, lhs)

    def declarations(raw: Node, owner: str, *, top: bool) -> None:
        for child in raw.named_children:
            bound = binding(child)

            if bound is not None:
                name, val, name_node = bound

                if val.type == 'function_definition':
                    function(name, val, val.child_by_field_name('body'), owner)
                    continue

                if val.type == 'call':
                    ctor, ctor_args = parts(val)
                    ctor = ctor.split('::')[-1].strip()

                    if ctor in CONSTRUCTORS:
                        cname = name

                        if ctor_args is not None:
                            arg_items = [item for item in ctor_args.named_children if item.type == 'argument']

                            if arg_items:
                                cname = ident(argval(arg_items[0])) or name

                        class_id = cls(cname, child, owner)

                        if ctor_args is not None:
                            members(class_id, ctor_args)

                        continue

                if top and name_node is not None:
                    nid = bindid(owner, name)
                    line = name_node.start_point[0] + 1
                    node(nid, name, line, kind='variable')
                    edge(owner, nid, 'contains', line)

                continue

            if child.type == 'call' and process(child, owner):
                continue

            if child.type == 'function_definition':
                body = child.child_by_field_name('body')
                right = binding(body)
                op_node = body.child_by_field_name('operator') if body is not None else None

                if right is not None and op_node is not None and (read(op_node, source) in {'->', '->>'}):
                    name, val, _ = right
                    function(name, child, val, owner)

                continue

            declarations(child, owner, top=top)

    declarations(root, file_id, top=True)

    def lookup(caller_id: str, name: str) -> str | None:
        scope: str | None = caller_id
        visited: set[str] = set()

        while scope is not None and scope not in visited:
            visited.add(scope)
            target = defs.get(scope, {}).get(name)

            if target is not None:
                return target

            scope = parents.get(scope)

        return None

    def trace(raw: Node, caller_id: str) -> None:
        if raw.type == 'function_definition':
            return

        if raw.type == 'call':
            callee_node = raw.child_by_field_name('function')

            if callee_node is not None and callee_node.type == 'identifier':
                callee = read(callee_node, source)
                target = lookup(caller_id, callee)
                line = raw.start_point[0] + 1

                if target is not None:
                    edge(caller_id, target, 'calls', line)
                elif callee not in SYSTEM:
                    calls.append({
                        'caller_nid': caller_id,
                        'callee': callee,
                        'is_member_call': True,
                        'language': 'r',
                        'source_file': src,
                        'source_location': f'L{line}',
                    })

            elif callee_node is not None and callee_node.type == 'extract_operator':
                sub_parts = [c for c in callee_node.children if c.type == 'identifier']

                if len(sub_parts) == 2 and read(sub_parts[0], source) in ('self', 'private'):
                    method = read(sub_parts[1], source)
                    target = lookup(caller_id, method)

                    if target is not None:
                        edge(caller_id, target, 'calls', raw.start_point[0] + 1)

            elif callee_node is not None and callee_node.type == 'namespace_operator':
                pkg_node = callee_node.child_by_field_name('lhs')
                fn_node = callee_node.child_by_field_name('rhs')

                if pkg_node is not None and fn_node is not None:
                    pkg = read(pkg_node, source)
                    fn = read(fn_node, source)
                    line = raw.start_point[0] + 1
                    pkg_id = make('r', 'package', pkg)
                    node(pkg_id, pkg, line, kind='package', backed=False)
                    ext_id = make(pkg_id, 'function', fn)
                    node(ext_id, f'{pkg}::{fn}()', line, kind='external_function', backed=False, callable=True)
                    edge(caller_id, ext_id, 'calls', line)

        for child in raw.named_children:
            trace(child, caller_id)

    for body, caller_id in bodies:
        trace(body, caller_id)

    clean = [item for item in edges if item['source'] in seen and (item['target'] in seen or item['relation'] == 'imports_from')]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}