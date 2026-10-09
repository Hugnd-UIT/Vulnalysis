from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..src.base import bytes as read_bytes, make, read, stem


SKIP = frozenset({'using', 'return', 'if', 'else', 'elseif', 'foreach', 'for', 'while', 'do', 'switch', 'try', 'catch', 'finally', 'throw', 'break', 'continue', 'exit', 'param', 'begin', 'process', 'end', 'import-module'})
KEYS = frozenset({'RootModule', 'NestedModules', 'RequiredModules'})


def extract(path: Path) -> dict:
    try:
        import tree_sitter_powershell as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_powershell not installed'}

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
    bodies: list[tuple[str, Any, str | None]] = []
    classes: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    methods: dict[str, dict[str, str]] = {}

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

    def body(raw):
        for child in raw.children:
            if child.type == 'script_block':
                for sub in child.children:
                    if sub.type == 'script_block_body':
                        return sub

                return child

        return None

    def named(name: str, line: int) -> str:
        nid = make(base, name)

        if nid in seen:
            return nid

        nid = make(name)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': src})

        return nid

    def typename(raw) -> str | None:
        if raw is None:
            return None

        for spec in raw.children:
            if spec.type != 'type_spec':
                continue

            for tname in spec.children:
                if tname.type != 'type_name':
                    continue

                for tid in tname.children:
                    if tid.type == 'type_identifier':
                        return read(tid, source)

        return None

    def walk(raw, parent_class: str | None = None) -> None:
        kind = raw.type

        if kind == 'function_statement':
            name_node = next((c for c in raw.children if c.type == 'function_name'), None)

            if name_node:
                func_name = read(name_node, source)
                line = raw.start_point[0] + 1
                func_id = make(base, func_name)
                node(func_id, f'{func_name}()', line)
                edge(file_id, func_id, 'contains', line)
                blk = body(raw)

                if blk:
                    bodies.append((func_id, blk, None))
                    walk(blk, parent_class)

            return

        if kind == 'enum_statement':
            name_node = next((c for c in raw.children if c.type == 'simple_name'), None)

            if name_node:
                enum_name = read(name_node, source)
                line = raw.start_point[0] + 1
                enum_id = make(base, enum_name)
                node(enum_id, enum_name, line)
                edge(file_id, enum_id, 'contains', line)

                for member in raw.children:
                    if member.type != 'enum_member':
                        continue

                    mname_node = next((c for c in member.children if c.type == 'simple_name'), None)

                    if mname_node is None:
                        continue

                    member_name = read(mname_node, source)
                    mline = member.start_point[0] + 1
                    member_id = make(enum_id, member_name)
                    node(member_id, member_name, mline)
                    edge(enum_id, member_id, 'case_of', mline)

            return

        if kind == 'class_statement':
            name_node = next((c for c in raw.children if c.type == 'simple_name'), None)

            if name_node:
                class_name = read(name_node, source)
                line = raw.start_point[0] + 1
                class_id = make(base, class_name)
                classes.setdefault(class_name.lower(), []).append(class_id)
                names[class_id] = class_name.lower()
                node(class_id, class_name, line)
                edge(file_id, class_id, 'contains', line)
                colon = False
                idx = 0

                for child in raw.children:
                    if child.type == ':':
                        colon = True
                    elif colon and child.type == 'simple_name':
                        base_id = named(read(child, source), line)

                        if base_id != class_id:
                            rel = 'inherits' if idx == 0 else 'implements'
                            edge(class_id, base_id, rel, line)

                        idx += 1

                for child in raw.children:
                    walk(child, parent_class=class_id)

            return

        if kind == 'class_property_definition' and parent_class:
            lit = next((c for c in raw.children if c.type == 'type_literal'), None)
            tname = typename(lit)

            if tname:
                line = raw.start_point[0] + 1
                target_id = named(tname, line)

                if target_id != parent_class:
                    edge(parent_class, target_id, 'references', line, context='field')

            return

        if kind == 'class_method_definition':
            name_node = next((c for c in raw.children if c.type == 'simple_name'), None)

            if name_node:
                method_name = read(name_node, source)
                line = raw.start_point[0] + 1

                if parent_class:
                    method_id = make(parent_class, method_name)
                    node(method_id, f'.{method_name}()', line)
                    edge(parent_class, method_id, 'method', line)
                    methods.setdefault(parent_class, {})[method_name.lower()] = method_id
                else:
                    method_id = make(base, method_name)
                    node(method_id, f'{method_name}()', line)
                    edge(file_id, method_id, 'contains', line)

                return_lit = next((c for c in raw.children if c.type == 'type_literal'), None)
                return_name = typename(return_lit)

                if return_name:
                    target_id = named(return_name, line)

                    if target_id != method_id:
                        edge(method_id, target_id, 'references', line, context='return_type')

                plist = next((c for c in raw.children if c.type == 'class_method_parameter_list'), None)

                if plist is not None:
                    for p in plist.children:
                        if p.type != 'class_method_parameter':
                            continue

                        plit = next((c for c in p.children if c.type == 'type_literal'), None)
                        pname = typename(plit)

                        if not pname:
                            continue

                        pline = p.start_point[0] + 1
                        target_id = named(pname, pline)

                        if target_id != method_id:
                            edge(method_id, target_id, 'references', pline, context='parameter_type')

                blk = body(raw)

                if blk:
                    bodies.append((method_id, blk, parent_class))

            return

        if kind == 'command':
            invoke = next((c for c in raw.children if c.type == 'command_invokation_operator'), None)

            if invoke is not None and read(invoke, source).strip() == '.':
                expr = next((c for c in raw.children if c.type == 'command_name_expr'), None)

                if expr is not None:
                    name_node = next((c for c in expr.children if c.type == 'command_name'), None)

                    if name_node:
                        raw_path = read(name_node, source)
                        stem_val = re.sub('^[./\\\\]+', '', raw_path)
                        stem_val = re.sub(r'\.[^.]+$', '', stem_val).replace('\\', '/')
                        mod_name = stem_val.split('/')[-1]

                        if mod_name:
                            edge(file_id, make(mod_name), 'imports_from', raw.start_point[0] + 1)

                return

            cmd_name_node = next((c for c in raw.children if c.type == 'command_name'), None)

            if cmd_name_node:
                cmd_text = read(cmd_name_node, source).lower()

                if cmd_text == 'using':
                    tokens = []

                    for child in raw.children:
                        if child.type == 'command_elements':
                            for el in child.children:
                                if el.type == 'generic_token':
                                    tokens.append(read(el, source))

                    mod_tokens = [t for t in tokens if t.lower() not in ('namespace', 'module', 'assembly')]

                    if mod_tokens:
                        mod_name = mod_tokens[-1].split('.')[-1]
                        edge(file_id, make(mod_name), 'imports_from', raw.start_point[0] + 1)

                elif cmd_text == 'import-module':
                    mod_name: str | None = None
                    expect = False

                    for child in raw.children:
                        if child.type != 'command_elements':
                            continue

                        for el in child.children:
                            if el.type == 'command_parameter':
                                param_text = read(el, source).lstrip('-').lower()
                                expect = param_text in ('name', 'n')
                            elif el.type == 'generic_token':
                                token = read(el, source)

                                if mod_name is None or expect:
                                    mod_name = token
                                    expect = False

                    if mod_name:
                        bare = re.sub(r'\.[^.]+$', '', mod_name).split('/')[-1].split('\\')[-1]

                        if bare:
                            edge(file_id, make(bare), 'imports_from', raw.start_point[0] + 1)

            return

        for child in raw.children:
            walk(child, parent_class)

    walk(root)

    labels = {n['label'].strip('()').lstrip('.').lower(): n['id'] for n in nodes}
    pairs: set[tuple[str, str]] = set()
    calls: list[dict] = []

    def trace(raw, caller_id: str, parent_class: str | None) -> None:
        if raw.type in ('function_statement', 'class_statement'):
            return

        if raw.type == 'command':
            cmd_name_node = next((c for c in raw.children if c.type == 'command_name'), None)

            if cmd_name_node:
                cmd_text = read(cmd_name_node, source)

                if cmd_text.lower() not in SKIP:
                    tgt_id = labels.get(cmd_text.lower())

                    if tgt_id and tgt_id != caller_id:
                        pair = (caller_id, tgt_id)

                        if pair not in pairs:
                            pairs.add(pair)
                            edge(caller_id, tgt_id, 'calls', raw.start_point[0] + 1)
                    elif cmd_text:
                        calls.append({'caller_nid': caller_id, 'callee': cmd_text, 'is_member_call': False, 'source_file': src, 'source_location': f'L{raw.start_point[0] + 1}'})

        elif raw.type == 'invokation_expression':
            member_node = next((c for c in raw.children if c.type == 'member_name'), None)

            if member_node is not None:
                method_name = read(member_node, source)
                receiver_class = None
                recv = raw.children[0] if raw.children else None
                op = raw.children[1].type if len(raw.children) > 1 else None

                if recv is not None and recv.type == 'variable' and (op == '.'):
                    if read(recv, source).lower() == '$this' and parent_class:
                        cname = names[parent_class]

                        if len(classes[cname]) == 1:
                            receiver_class = parent_class

                elif recv is not None and recv.type == 'type_literal' and (op == '::'):
                    tname = read(recv, source)[1:-1].strip() if not recv.has_error else ''
                    cids = classes.get(tname.lower(), []) if tname else []

                    if len(cids) == 1:
                        receiver_class = cids[0]

                is_simple = len(member_node.named_children) == 1 and member_node.named_children[0].type == 'simple_name'
                tgt_id = methods.get(receiver_class, {}).get(method_name.lower()) if receiver_class and is_simple else None

                if tgt_id and tgt_id != caller_id:
                    pair = (caller_id, tgt_id)

                    if pair not in pairs:
                        pairs.add(pair)
                        edge(caller_id, tgt_id, 'calls', raw.start_point[0] + 1)
                elif method_name and (not tgt_id):
                    calls.append({'caller_nid': caller_id, 'callee': method_name, 'is_member_call': True, 'source_file': src, 'source_location': f'L{raw.start_point[0] + 1}'})

        for child in raw.children:
            trace(child, caller_id, parent_class)

    for caller_id, body_node, parent_class in bodies:
        trace(body_node, caller_id, parent_class)

    clean = [e for e in edges if e['source'] in seen and (e['target'] in seen or e['relation'] in ('imports_from', 'imports'))]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}


def literals(raw, source: bytes) -> list[str]:
    out: list[str] = []

    def trace(item) -> None:
        if item.type == 'string_literal':
            val = source[item.start_byte:item.end_byte].decode(errors='replace')
            out.append(val.strip('\'"'))

            return

        for child in item.children:
            trace(child)

    trace(raw)

    return out


def modname(raw: str) -> str:
    name = raw.replace('\\', '/').split('/')[-1]
    name = re.sub(r'\.[^.]+$', '', name)

    return name.strip()


def manifest(path: Path) -> dict:
    try:
        import tree_sitter_powershell as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_powershell not installed'}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = read_bytes(path)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    src = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def node(nid: str, label: str, line: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': src, 'source_location': f'L{line}'})

    def imported(source_id: str, raw_mod: str, line: int) -> None:
        name = modname(raw_mod)

        if not name:
            return

        tgt_id = make(name)
        edges.append({'source': source_id, 'target': tgt_id, 'relation': 'imports_from', 'source_file': src, 'source_location': f'L{line}', 'context': 'import'})

    file_id = make(src)
    node(file_id, path.name, 1)

    def walk(raw) -> None:
        if raw.type != 'hash_entry':
            for child in raw.children:
                walk(child)

            return

        key_node = next((c for c in raw.children if c.type == 'key_expression'), None)

        if key_node is None:
            return

        key_text = source[key_node.start_byte:key_node.end_byte].decode(errors='replace').strip()

        if key_text not in KEYS:
            return

        line = raw.start_point[0] + 1
        val_node = next((c for c in raw.children if c.type == 'pipeline'), None)

        if val_node is None:
            return

        if key_text in ('RootModule', 'NestedModules'):
            strings = literals(val_node, source)

            for s in strings:
                imported(file_id, s, line)

        elif key_text == 'RequiredModules':
            names: list[str] = []
            nested: set[int] = set()

            def entries(item) -> None:
                if item.type == 'hash_entry':
                    sub_key = next((c for c in item.children if c.type == 'key_expression'), None)

                    if sub_key is not None:
                        sub_text = source[sub_key.start_byte:sub_key.end_byte].decode(errors='replace').strip()

                        for c in item.children:
                            if c.type == 'pipeline':
                                for s_node in collect(c):
                                    nested.add(s_node.start_byte)

                        if sub_text == 'ModuleName':
                            for c in item.children:
                                if c.type == 'pipeline':
                                    for s in literals(c, source):
                                        names.append(s)

                    return

                for child in item.children:
                    entries(child)

            def collect(item):
                if item.type == 'string_literal':
                    yield item

                    return

                for child in item.children:
                    yield from collect(child)

            entries(val_node)
            direct: list[str] = []

            for s_node in collect(val_node):
                if s_node.start_byte not in nested:
                    val = source[s_node.start_byte:s_node.end_byte].decode(errors='replace')
                    direct.append(val.strip('\'"'))

            for s in direct + names:
                imported(file_id, s, line)

    walk(root)

    return {'nodes': nodes, 'edges': edges, 'raw_calls': []}