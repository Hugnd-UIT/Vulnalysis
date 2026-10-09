from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from ..src.base import GLOBALS, bytes as read_bytes, make, read, stem


BUILTINS: frozenset[str] = frozenset({'String', 'str', 'Option', 'Result', 'Vec', 'VecDeque', 'Box', 'Rc', 'Arc', 'Weak', 'RefCell', 'Cell', 'Cow', 'Pin', 'HashMap', 'HashSet', 'BTreeMap', 'BTreeSet', 'BinaryHeap', 'Path', 'PathBuf', 'OsStr', 'OsString', 'CStr', 'CString', 'Duration', 'Instant', 'SystemTime', 'Ordering', 'Range', 'RangeInclusive', 'RangeFrom', 'RangeTo', 'RangeFull', 'PhantomData', 'ManuallyDrop', 'NonZeroU8', 'NonZeroU16', 'NonZeroU32', 'NonZeroU64', 'NonZeroUsize', 'Some', 'None', 'Ok', 'Err', 'Self', 'Default', 'Clone', 'Copy', 'Debug', 'Display', 'Error', 'From', 'Into', 'TryFrom', 'TryInto', 'AsRef', 'AsMut', 'Iterator', 'IntoIterator', 'Extend', 'PartialEq', 'Eq', 'PartialOrd', 'Ord', 'Hash', 'Send', 'Sync', 'Sized', 'Drop', 'Deref', 'DerefMut', 'Future', 'Fn', 'FnMut', 'FnOnce'})
BLOCKLIST: frozenset[str] = frozenset({'new', 'default', 'parse', 'from_str', 'now', 'clone', 'into', 'from', 'to_string', 'to_owned', 'len', 'is_empty', 'iter', 'next', 'build', 'start', 'run', 'init', 'app', 'get', 'set', 'push', 'pop', 'insert', 'remove', 'contains', 'collect', 'map', 'filter', 'unwrap', 'expect', 'ok', 'err', 'some', 'none', 'send', 'recv', 'lock', 'read', 'write'})
PRIMITIVES: frozenset[str] = frozenset({'bool', 'char', 'str', 'f32', 'f64', 'i8', 'i16', 'i32', 'i64', 'i128', 'isize', 'u8', 'u16', 'u32', 'u64', 'u128', 'usize'})
TYPENODES = ('type_identifier', 'generic_type', 'scoped_type_identifier', 'reference_type', 'primitive_type', 'tuple_type', 'array_type')
DISCOVERY_DEPTH = 2


def types(raw, source: bytes, generic: bool, out: list[tuple[str, str]], locals: frozenset[str] = frozenset()) -> None:
    def keep(text: str) -> bool:
        return bool(text) and (text not in BUILTINS or text in locals)

    if raw is None:
        return

    kind = raw.type

    if kind == 'primitive_type':
        return

    if kind == 'type_identifier':
        val = read(raw, source)

        if keep(val):
            out.append((val, 'generic_arg' if generic else 'type'))

        return

    if kind == 'scoped_type_identifier':
        val = read(raw, source).rsplit('::', 1)[-1]

        if keep(val):
            out.append((val, 'generic_arg' if generic else 'type'))

        return

    if kind == 'generic_type':
        name_node = raw.child_by_field_name('type')

        if name_node is None:
            for c in raw.children:
                if c.type in ('type_identifier', 'scoped_type_identifier'):
                    name_node = c
                    break

        if name_node is not None:
            val = read(name_node, source).rsplit('::', 1)[-1]

            if keep(val):
                out.append((val, 'generic_arg' if generic else 'type'))

        for c in raw.children:
            if c.type == 'type_arguments':
                for arg in c.children:
                    if arg.is_named:
                        types(arg, source, True, out, locals)

        return

    if kind in ('reference_type', 'pointer_type', 'array_type', 'tuple_type', 'slice_type'):
        for c in raw.children:
            if c.is_named:
                types(c, source, generic, out, locals)

        return

    if raw.is_named:
        for c in raw.children:
            if c.is_named:
                types(c, source, generic, out, locals)


def generickey(raw, source: bytes) -> str | None:
    if raw.child_by_field_name('trait') is not None:
        return None

    params = raw.child_by_field_name('type_parameters')
    owner_type = raw.child_by_field_name('type')

    if params is None or owner_type is None or owner_type.type != 'generic_type':
        return None

    if any((child.type == 'where_clause' for child in raw.named_children)):
        return None

    param_names: list[str] = []

    for param in params.named_children:
        if param.type != 'type_parameter':
            return None

        named_items = param.named_children

        if len(named_items) != 1 or named_items[0].type != 'type_identifier':
            return None

        name = read(named_items[0], source)

        if not name or name in param_names:
            return None

        param_names.append(name)

    if not param_names:
        return None

    owner = owner_type.child_by_field_name('type')

    if owner is None or owner.type != 'type_identifier':
        return None

    args = next((child for child in owner_type.named_children if child.type == 'type_arguments'), None)

    if args is None:
        return None

    arg_names = [read(arg, source) for arg in args.named_children if arg.type == 'type_identifier']

    if len(arg_names) != len(args.named_children):
        return None

    if arg_names != param_names:
        return None

    owner_name = read(owner, source)

    return f'{owner_name}/{len(param_names)}' if owner_name else None


def qualifier(path_node, source: bytes) -> str | None:
    if path_node is None:
        return None

    last = segment(read(path_node, source))

    return last if last[:1].isupper() or last in PRIMITIVES else None


def segment(text: str) -> str:
    text = re.sub(r"^(?:&\s*(?:'\w+\s+)?(?:mut\s+)?|dyn\s+)", '', text.strip())
    depth, kept = (0, [])

    for ch in text:
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(ch)

    parts = [s.strip() for s in ''.join(kept).split('::') if s.strip()]

    return parts[-1] if parts else ''


def extract(path: Path) -> dict:
    try:
        import tree_sitter_rust as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-rust not installed'}

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
    bodies: list[tuple[str, object, str | None, str | None]] = []
    keys: dict[str, str | None] = {}
    local_types: set[str] = set()

    def scantypes(raw) -> None:
        if raw.type in ('struct_item', 'enum_item', 'trait_item', 'union_item', 'type_item'):
            name_node = raw.child_by_field_name('name')

            if name_node is not None:
                val = read(name_node, source)

                if val:
                    local_types.add(val)

        for child in raw.children:
            scantypes(child)

    scantypes(root)
    local_names = frozenset(local_types)
    macros: dict[str, str] = {}
    methods: dict[tuple[str, str], set[str]] = {}

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
    items_map: dict[str, set[tuple[bool, str]]] = {}
    type_ids: set[str] = set()

    def items(raw) -> None:
        kind = raw.type

        if kind in ('struct_item', 'enum_item', 'trait_item', 'function_item', 'function_signature_item', 'static_item', 'const_item'):
            name_node = raw.child_by_field_name('name')

            if name_node:
                val = read(name_node, source)

                if kind in ('static_item', 'const_item') and val == '_':
                    return

                nid = make(base, val)
                is_type = kind in ('struct_item', 'enum_item', 'trait_item')
                items_map.setdefault(nid, set()).add((is_type, val))

                if is_type:
                    type_ids.add(nid)

            return

        if kind == 'impl_item':
            type_node = raw.child_by_field_name('type')

            if type_node is not None:
                type_name = read(type_node, source).strip()
                refs: list[tuple[str, str]] = []
                types(type_node, source, False, refs)
                bare = next((ref for ref, role in refs if role == 'type'), type_name)
                items_map.setdefault(make(base, type_name), set()).add((True, bare))

            return

        if kind == 'use_declaration':
            return

        for child in raw.children:
            items(child)

    items(root)
    local_names = {next(iter(vals))[1] for nid, vals in items_map.items() if nid in type_ids and len(vals) == 1}

    def named(name: str, line: int) -> str:
        nid = make(base, name)

        if nid in seen or name in local_names:
            return nid

        nid = make(name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def paramrefs(func_node, func_id: str, line: int) -> None:
        params = func_node.child_by_field_name('parameters')

        if params is not None:
            for p in params.children:
                if p.type != 'parameter':
                    continue

                type_node = p.child_by_field_name('type')
                refs: list[tuple[str, str]] = []
                types(type_node, source, False, refs, local_names)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                    tgt = named(ref_name, line)

                    if tgt != func_id:
                        edge(func_id, tgt, 'references', line, context=ctx)

        ret = func_node.child_by_field_name('return_type')

        if ret is not None:
            refs = []
            types(ret, source, False, refs, local_names)

            for ref_name, role in refs:
                ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                tgt = named(ref_name, line)

                if tgt != func_id:
                    edge(func_id, tgt, 'references', line, context=ctx)

    def walk(raw, parent_id: str | None = None, parent_type: str | None = None, parent_key: str | None = None) -> None:
        kind = raw.type

        if kind == 'function_item':
            name_node = raw.child_by_field_name('name')

            if name_node:
                func_name = read(name_node, source)
                line = raw.start_point[0] + 1

                if parent_id:
                    func_id = make(parent_id, func_name)
                    node(func_id, f'.{func_name}()', line)
                    edge(parent_id, func_id, 'method', line)

                    if parent_type:
                        owner = segment(parent_type)
                        methods.setdefault((owner, func_name), set()).add(func_id)
                else:
                    func_id = make(base, func_name)
                    node(func_id, f'{func_name}()', line)
                    edge(file_id, func_id, 'contains', line)

                paramrefs(raw, func_id, line)
                body = raw.child_by_field_name('body')

                if body:
                    bodies.append((func_id, body, parent_type, parent_key))

            return

        if kind == 'function_signature_item':
            name_node = raw.child_by_field_name('name')

            if name_node:
                func_name = read(name_node, source)
                line = raw.start_point[0] + 1

                if parent_id:
                    func_id = make(parent_id, func_name)
                    node(func_id, f'.{func_name}()', line)
                    edge(parent_id, func_id, 'method', line)
                else:
                    func_id = make(base, func_name)
                    node(func_id, f'{func_name}()', line)
                    edge(file_id, func_id, 'contains', line)

                paramrefs(raw, func_id, line)

            return

        if kind == 'macro_definition':
            name_node = raw.child_by_field_name('name')

            if name_node:
                macro_name = read(name_node, source)

                if macro_name:
                    line = raw.start_point[0] + 1
                    macro_id = make(base, 'macro', macro_name)
                    node(macro_id, f'{macro_name}!', line)
                    edge(file_id, macro_id, 'contains', line)
                    macros.setdefault(macro_name, macro_id)

            return

        if kind in ('struct_item', 'enum_item', 'trait_item'):
            name_node = raw.child_by_field_name('name')

            if name_node:
                item_name = read(name_node, source)
                line = raw.start_point[0] + 1
                item_id = make(base, item_name)
                node(item_id, item_name, line)
                decl_node = next((n for n in nodes if n['id'] == item_id))
                decl_node['_rust_declaration_count'] = decl_node.get('_rust_declaration_count', 0) + 1
                edge(file_id, item_id, 'contains', line)

                if kind == 'trait_item':
                    for c in raw.children:
                        if c.type != 'trait_bounds':
                            continue

                        for sub in c.children:
                            if not sub.is_named:
                                continue

                            refs: list[tuple[str, str]] = []
                            types(sub, source, False, refs, local_names)

                            for idx, (ref_name, _role) in enumerate(refs):
                                tgt = named(ref_name, line)

                                if tgt == item_id:
                                    continue

                                if idx == 0:
                                    edge(item_id, tgt, 'inherits', line)
                                else:
                                    edge(item_id, tgt, 'references', line, context='generic_arg')

                if kind == 'struct_item':
                    for c in raw.children:
                        if c.type != 'field_declaration_list':
                            continue

                        for field in c.children:
                            if field.type != 'field_declaration':
                                continue

                            type_node = field.child_by_field_name('type')

                            if type_node is None:
                                for fc in field.children:
                                    if fc.type in ('type_identifier', 'generic_type', 'scoped_type_identifier', 'reference_type', 'primitive_type'):
                                        type_node = fc
                                        break

                            refs = []
                            types(type_node, source, False, refs, local_names)

                            for ref_name, role in refs:
                                ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                tgt = named(ref_name, field.start_point[0] + 1)

                                if tgt != item_id:
                                    edge(item_id, tgt, 'references', field.start_point[0] + 1, context=ctx)

                    for c in raw.children:
                        if c.type != 'ordered_field_declaration_list':
                            continue

                        fline = c.start_point[0] + 1

                        for tc in c.children:
                            if tc.type not in ('type_identifier', 'generic_type', 'scoped_type_identifier', 'reference_type', 'primitive_type', 'tuple_type', 'array_type'):
                                continue

                            refs = []
                            types(tc, source, False, refs, local_names)

                            for ref_name, role in refs:
                                ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                tgt = named(ref_name, fline)

                                if tgt != item_id:
                                    edge(item_id, tgt, 'references', fline, context=ctx)

                if kind == 'enum_item':
                    def enums(tn, at_line):
                        if tn is None:
                            return

                        sub_refs: list[tuple[str, str]] = []
                        types(tn, source, False, sub_refs, local_names)

                        for ref_name, role in sub_refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                            tgt = named(ref_name, at_line)

                            if tgt != item_id:
                                edge(item_id, tgt, 'references', at_line, context=ctx)

                    for c in raw.children:
                        if c.type != 'enum_variant_list':
                            continue

                        for variant in c.children:
                            if variant.type != 'enum_variant':
                                continue

                            vline = variant.start_point[0] + 1
                            vname_node = next((vc for vc in variant.children if vc.type == 'identifier'), None)

                            if vname_node is not None:
                                vname = read(vname_node, source)

                                if vname:
                                    variant_id = make(item_id, vname)
                                    node(variant_id, vname, vline)
                                    edge(item_id, variant_id, 'case_of', vline)

                            for vc in variant.children:
                                if vc.type == 'ordered_field_declaration_list':
                                    for tc in vc.children:
                                        if tc.type in TYPENODES:
                                            enums(tc, vline)
                                elif vc.type == 'field_declaration_list':
                                    for field in vc.children:
                                        if field.type != 'field_declaration':
                                            continue

                                        tn = field.child_by_field_name('type')
                                        enums(tn, field.start_point[0] + 1)

                if kind == 'trait_item':
                    body = raw.child_by_field_name('body')

                    if body:
                        for child in body.children:
                            walk(child, parent_id=item_id)

            return

        if kind in ('static_item', 'const_item'):
            name_node = raw.child_by_field_name('name')

            if name_node:
                item_name = read(name_node, source)

                if item_name == '_':
                    return

                line = raw.start_point[0] + 1

                if parent_id:
                    item_id = make(parent_id, item_name)
                    node(item_id, f'.{item_name}', line)
                    edge(parent_id, item_id, 'contains', line)
                else:
                    item_id = make(base, item_name)
                    node(item_id, item_name, line)
                    edge(file_id, item_id, 'contains', line)

                type_node = raw.child_by_field_name('type')

                if type_node is not None:
                    refs = []
                    types(type_node, source, False, refs, local_names)

                    for ref_name, role in refs:
                        tgt = named(ref_name, line)

                        if tgt == item_id:
                            continue

                        ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                        edge(item_id, tgt, 'references', line, context=ctx)

            return

        if kind == 'impl_item':
            type_node = raw.child_by_field_name('type')
            trait_node = raw.child_by_field_name('trait')
            impl_id: str | None = None
            impl_type_bare: str | None = None
            impl_key: str | None = None

            if type_node:
                type_name = read(type_node, source).strip()
                impl_id = make(base, type_name)
                node(impl_id, type_name, raw.start_point[0] + 1)
                impl_key = generickey(raw, source)
                impl_type_bare = type_name.split('<')[0].strip()

            if trait_node is not None and impl_id is not None:
                refs = []
                types(trait_node, source, False, refs, local_names)

                for idx, (ref_name, _role) in enumerate(refs):
                    tgt = named(ref_name, raw.start_point[0] + 1)

                    if tgt == impl_id:
                        continue

                    if idx == 0:
                        edge(impl_id, tgt, 'implements', raw.start_point[0] + 1)
                    else:
                        edge(impl_id, tgt, 'references', raw.start_point[0] + 1, context='generic_arg')

            body = raw.child_by_field_name('body')

            if body:
                has_funcs = any((child.type in ('function_item', 'function_signature_item') for child in body.children))

                if impl_id is not None and has_funcs:
                    if impl_id not in keys:
                        keys[impl_id] = impl_key
                    elif keys[impl_id] != impl_key:
                        keys[impl_id] = None

                    impl_node = next((n for n in nodes if n['id'] == impl_id))

                    if keys[impl_id]:
                        impl_node['_rust_impl_key'] = keys[impl_id]
                    else:
                        impl_node.pop('_rust_impl_key', None)

                for child in body.children:
                    walk(child, parent_id=impl_id, parent_type=impl_type_bare, parent_key=impl_key)

            return

        if kind == 'use_declaration':
            arg = raw.child_by_field_name('argument')

            if arg:
                val = read(arg, source)
                cleaned = val.split('{')[0].rstrip(':').rstrip('*').rstrip(':')
                mod = cleaned.split('::')[-1].strip()

                if mod:
                    edge(file_id, make(mod), 'imports_from', raw.start_point[0] + 1, context='import')

            return

        for child in raw.children:
            walk(child, parent_id=None)

    walk(root)

    labels: dict[str, str] = {}
    methods_map: dict[str, str] = {}

    for n in nodes:
        lbl = n['label']
        clean_lbl = lbl.strip('()').lstrip('.')

        if lbl.startswith('.'):
            if lbl.endswith(')'):
                methods_map[clean_lbl] = n['id']
        else:
            labels[clean_lbl] = n['id']

    typids: set[str] = {n['id'] for n in nodes if not n['label'].endswith(')')}
    pairs: set[tuple[str, str]] = set()
    calls: list[dict] = []

    def trace(raw, caller_id: str, self_type: str | None = None, self_key: str | None = None) -> None:
        if raw.type == 'function_item':
            return

        if raw.type == 'macro_invocation':
            macro_node = raw.child_by_field_name('macro')

            if macro_node is not None and macro_node.type == 'identifier':
                macro_name = read(macro_node, source)
                tgt = macros.get(macro_name)

                if tgt and tgt != caller_id:
                    pair = (caller_id, tgt)

                    if pair not in pairs:
                        pairs.add(pair)
                        line = raw.start_point[0] + 1
                        edges.append({'source': caller_id, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': src, 'source_location': f'L{line}'})

        if raw.type == 'call_expression':
            func_node = raw.child_by_field_name('function')
            callee: str | None = None
            is_member = False
            is_scoped = False
            is_self = False
            qtype: str | None = None

            if func_node:
                if func_node.type == 'identifier':
                    callee = read(func_node, source)
                elif func_node.type == 'field_expression':
                    is_member = True
                    field = func_node.child_by_field_name('field')

                    if field:
                        callee = read(field, source)

                    recv = func_node.child_by_field_name('value')

                    if recv is not None and recv.type == 'self':
                        is_self = True
                elif func_node.type == 'scoped_identifier':
                    is_scoped = True
                    name = func_node.child_by_field_name('name')

                    if name:
                        callee = read(name, source)

                    qtype = qualifier(func_node.child_by_field_name('path'), source)

                    if qtype == 'Self':
                        qtype = self_type or ''

            if callee and callee not in GLOBALS and (callee not in BUILTINS or callee in local_types):
                if qtype is not None or (is_self and self_type):
                    owner = segment(qtype or self_type or '')
                    owned = methods.get((owner, callee), set())
                    tgt = next(iter(owned)) if len(owned) == 1 else None

                    if tgt is None and qtype is not None:
                        named_cand = labels.get(callee)
                        tgt = named_cand if named_cand in typids else None
                elif is_member:
                    tgt = methods_map.get(callee)
                else:
                    tgt = labels.get(callee)

                if tgt and tgt != caller_id:
                    pair = (caller_id, tgt)

                    if pair not in pairs:
                        pairs.add(pair)
                        line = raw.start_point[0] + 1
                        is_ctor = tgt in typids
                        edges.append({'source': caller_id, 'target': tgt, 'relation': 'references' if is_ctor else 'calls', 'context': 'constructor' if is_ctor else 'call', 'source_file': src, 'source_location': f'L{line}'})
                elif not is_scoped and callee.lower() not in BLOCKLIST:
                    entry = {'caller_nid': caller_id, 'callee': callee, 'is_member_call': is_member, 'source_file': src, 'source_location': f'L{raw.start_point[0] + 1}'}

                    if is_self and self_type:
                        entry['rust_self_type'] = self_type

                        if self_key:
                            entry['rust_self_impl_key'] = self_key

                    calls.append(entry)

        for child in raw.children:
            trace(child, caller_id, self_type, self_key)

    for caller_id, body_node, impl_type, impl_key in bodies:
        trace(body_node, caller_id, impl_type, impl_key)

    clean = []

    for item in edges:
        s, t = item['source'], item['target']

        if s in seen and (t in seen or item['relation'] in ('imports', 'imports_from')):
            clean.append(item)

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}


def toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            raise ImportError('--cargo on Python 3.10 needs tomli. Install with: pip install tomli') from None

    with path.open('rb') as manifest:
        return tomllib.load(manifest)


def rootmanifest(root: Path) -> Path | None:
    from graphify.detect import noise
    found: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        curr = Path(dirpath)
        depth = len(curr.relative_to(root).parts)

        if depth > 0 and 'Cargo.toml' in filenames:
            found.append(curr / 'Cargo.toml')

        if depth >= DISCOVERY_DEPTH:
            dirnames[:] = []
            continue

        dirnames[:] = [d for d in dirnames if not noise(d, curr)]

    if not found:
        return None

    return sorted(found)[0]


def members(root: Path, root_data: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []

    if isinstance(root_data.get('package'), dict):
        paths.append(root / 'Cargo.toml')

    workspace = root_data.get('workspace')
    items = workspace.get('members', []) if isinstance(workspace, dict) else []

    if not isinstance(items, list):
        return paths

    for pattern in items:
        if not isinstance(pattern, str):
            continue

        for member in sorted(root.glob(pattern)):
            manifest = member / 'Cargo.toml'

            if manifest.is_file() and manifest not in paths:
                paths.append(manifest)

    return paths


def cargo(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    manifest_path = root_path / 'Cargo.toml'

    if not manifest_path.is_file():
        discovered = rootmanifest(root_path)

        if discovered is not None:
            manifest_path = discovered

    root_data = toml(manifest_path)
    workspace = root_data.get('workspace')
    workspace_deps = workspace.get('dependencies', {}) if isinstance(workspace, dict) else {}

    if not isinstance(workspace_deps, dict):
        workspace_deps = {}

    manifests = members(manifest_path.parent, root_data)
    crates: dict[str, tuple[str, Path, dict[str, Any]]] = {}

    for item in manifests:
        data = root_data if item == manifest_path else toml(item)
        package = data.get('package')

        if not isinstance(package, dict):
            continue

        name = package.get('name')

        if isinstance(name, str):
            crates[name] = (f'crate:{name}', item, data)

    nodes = [{'id': crate_id, 'label': name, 'source_file': item.relative_to(root_path).as_posix(), 'source_location': 'L1'} for name, (crate_id, item, _data) in sorted(crates.items())]
    edges: list[dict[str, Any]] = []

    for source_name, (source_id, item, data) in sorted(crates.items()):
        deps = data.get('dependencies', {})

        if not isinstance(deps, dict):
            continue

        source_file = item.relative_to(root_path).as_posix()

        for dep_key, spec in sorted(deps.items()):
            inherited = isinstance(spec, dict) and 'workspace' in spec

            if inherited:
                if spec['workspace'] is not True:
                    continue

                spec = workspace_deps.get(dep_key)

                if not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
                    continue

            real_name = dep_key

            if isinstance(spec, dict):
                override = spec.get('package')

                if isinstance(override, str) and override:
                    real_name = override

            target = crates.get(real_name)

            if target is None:
                continue

            if inherited:
                inherited_manifest = (root_path / spec['path'] / 'Cargo.toml').resolve()

                if inherited_manifest != target[1].resolve():
                    continue

            edges.append({'source': source_id, 'target': target[0], 'relation': 'crate_depends_on', 'context': 'cargo_dependency', 'source_file': source_file, 'source_location': 'L1'})

    return {'nodes': nodes, 'edges': edges}