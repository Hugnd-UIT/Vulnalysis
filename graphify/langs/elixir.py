from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..src.base import GLOBALS, bytes as read_bytes, make, stem


IMPORTS = frozenset({'alias', 'import', 'require', 'use'})
SKIPS = frozenset({'def', 'defp', 'defmodule', 'defmacro', 'defmacrop', 'defstruct', 'defprotocol', 'defimpl', 'defguard', 'alias', 'import', 'require', 'use', 'if', 'unless', 'case', 'cond', 'with', 'for'})
MODULE = re.compile(r'(__MODULE__|[A-Z]\w*)(\.[A-Z]\w*)*')


def extract(path: Path) -> dict:
    try:
        import tree_sitter_elixir as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_elixir not installed'}

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
    bodies: list[tuple[str, Any]] = []

    def node(nid: str, label: str, line: int, **attrs) -> None:
        if nid not in seen:
            seen.add(nid)
            item = {'id': nid, 'label': label, 'file_type': 'code', 'source_file': src, 'source_location': f'L{line}'}
            item.update(attrs)
            nodes.append(item)

    def edge(source_id: str, target_id: str, relation: str, line: int, context: str | None = None) -> None:
        item = {'source': source_id, 'target': target_id, 'relation': relation, 'source_file': src, 'source_location': f'L{line}'}

        if context:
            item['context'] = context

        edges.append(item)

    file_id = make(src)
    node(file_id, path.name, 1)
    calls_map: dict[str | None, list[str]] = {}
    enclosing: dict[str, str | None] = {}
    modnames: dict[str, str] = {}
    aliases: dict[str | None, dict[str, str]] = {}
    defs: dict[tuple[str, str], str] = {}

    def aliastxt(raw) -> str | None:
        for child in raw.children:
            if child.type == 'alias':
                return source[child.start_byte:child.end_byte].decode('utf-8', errors='replace')

        return None

    def aliasmods(raw) -> list[str]:
        def text(item) -> str:
            return source[item.start_byte:item.end_byte].decode('utf-8', errors='replace')

        for child in raw.children:
            if child.type == 'alias':
                return [text(child)]

            if child.type == 'dot':
                prefix = None
                tup = None

                for sub in child.children:
                    if sub.type == 'alias' and prefix is None:
                        prefix = text(sub)
                    elif sub.type == 'tuple':
                        tup = sub

                if prefix and tup is not None:
                    items = [text(m) for m in tup.children if m.type == 'alias']

                    if items:
                        return [f'{prefix}.{m}' for m in items]

                return [text(child)]

        return []

    def kwalias(raw, key: str) -> str | None:
        for child in raw.children:
            if child.type != 'keywords':
                continue

            for pair in child.children:
                if pair.type != 'pair':
                    continue

                kw = None
                val = None

                for sub in pair.children:
                    if sub.type == 'keyword':
                        kw = source[sub.start_byte:sub.end_byte].decode('utf-8', errors='replace')
                    elif sub.type == 'alias':
                        val = source[sub.start_byte:sub.end_byte].decode('utf-8', errors='replace')

                if kw and kw.rstrip(': ').strip() == key and val:
                    return val

        return None

    def expand(name: str, scope: str | None) -> str:
        first, _, rest = name.partition('.')
        target = None

        if first == '__MODULE__':
            if scope is not None and scope not in modnames:
                scope = enclosing.get(scope)

            target = (modnames.get(scope) if scope is not None else None) or None
        else:
            visited: set[str | None] = set()
            holder = scope

            while holder not in visited:
                visited.add(holder)
                target = aliases.get(holder, {}).get(first)

                if target is not None or holder is None:
                    break

                holder = enclosing.get(holder)

        if target is None:
            return name

        return f'{target}.{rest}' if rest else target

    def record(args_node, scope: str | None) -> None:
        modules = [expand(m, scope) for m in aliasmods(args_node)]
        as_val = kwalias(args_node, 'as')
        table = aliases.setdefault(scope, {})

        if as_val and len(modules) == 1:
            table[as_val] = modules[0]
        elif not as_val:
            for mod in modules:
                table[mod.rsplit('.', 1)[-1]] = mod

    def register(nid: str, name: str, parent: str | None) -> None:
        parent_name = modnames.get(parent) if parent is not None else None

        if parent_name:
            first = name.split('.', 1)[0]
            aliases.setdefault(parent, {})[first] = f'{parent_name}.{first}'
            name = f'{parent_name}.{name}'

        modnames[nid] = name

    def doval(raw):
        if raw is None:
            return None

        for child in raw.children:
            if child.type != 'keywords':
                continue

            for pair in child.children:
                if pair.type != 'pair':
                    continue

                kw_val = None

                for sub in pair.children:
                    if sub.type == 'keyword':
                        kw_val = source[sub.start_byte:sub.end_byte].decode('utf-8', errors='replace')
                    elif kw_val is not None and kw_val.rstrip(': ').strip() == 'do':
                        return sub

        return None

    def walk(raw, parent_id: str | None = None) -> None:
        if raw.type != 'call':
            for child in raw.children:
                walk(child, parent_id)

            return

        id_node = None
        args_node = None
        do_node = None

        for child in raw.children:
            if child.type == 'identifier':
                id_node = child
            elif child.type == 'arguments':
                args_node = child
            elif child.type == 'do_block':
                do_node = child

        if id_node is None:
            for child in raw.children:
                walk(child, parent_id)

            return

        word = source[id_node.start_byte:id_node.end_byte].decode('utf-8', errors='replace')
        line = raw.start_point[0] + 1

        if word == 'defmodule':
            name = aliastxt(args_node) if args_node else None

            if not name:
                return

            mid = make(base, name)
            node(mid, name, line, **{'_elixir_module': True} if parent_id is None else {})
            edge(file_id, mid, 'contains', line)
            enclosing[mid] = parent_id
            register(mid, name, parent_id)

            if do_node:
                for child in do_node.children:
                    walk(child, parent_id=mid)

            return

        if word == 'defprotocol':
            proto = aliastxt(args_node) if args_node else None

            if not proto:
                return

            proto_id = make(base, proto)
            node(proto_id, proto, line, **{'_elixir_module': True} if parent_id is None else {})
            edge(parent_id or file_id, proto_id, 'contains', line)
            enclosing[proto_id] = parent_id
            register(proto_id, proto, parent_id)

            if do_node:
                for child in do_node.children:
                    walk(child, parent_id=proto_id)

            return

        if word == 'defimpl':
            proto = aliastxt(args_node) if args_node else None

            if not proto:
                return

            target = kwalias(args_node, 'for')
            impl_id = make(base, 'defimpl', proto, target or '')
            label = f'{proto} (for {target})' if target else proto
            node(impl_id, label, line)
            edge(parent_id or file_id, impl_id, 'contains', line)
            enclosing[impl_id] = parent_id
            modnames[impl_id] = f'{proto}.{target}' if target else ''
            edge(impl_id, make(base, proto), 'implements', line)

            if do_node:
                for child in do_node.children:
                    walk(child, parent_id=impl_id)

            return

        if word in ('def', 'defp', 'defmacro', 'defmacrop', 'defguard', 'defguardp'):
            func_name = None

            if args_node:
                for child in args_node.children:
                    while child.type == 'binary_operator':
                        lead = None

                        for sub in child.children:
                            if sub.type in ('call', 'identifier', 'binary_operator'):
                                lead = sub
                                break

                        if lead is None:
                            break

                        child = lead

                    if child.type == 'call':
                        for sub in child.children:
                            if sub.type == 'identifier':
                                func_name = source[sub.start_byte:sub.end_byte].decode('utf-8', errors='replace')
                                break
                    elif child.type == 'identifier':
                        func_name = source[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
                        break

            if not func_name:
                return

            holder = parent_id or file_id
            func_id = make(holder, func_name)
            node(func_id, f'{func_name}()', line)
            enclosing[func_id] = parent_id

            if parent_id:
                edge(parent_id, func_id, 'method', line)
                defs.setdefault((parent_id, func_name), func_id)
            else:
                edge(file_id, func_id, 'contains', line)

            if do_node:
                bodies.append((func_id, do_node))
            else:
                kw_body = doval(args_node)

                if kw_body is not None:
                    bodies.append((func_id, kw_body))

            return

        if word in IMPORTS and args_node:
            for mod in aliasmods(args_node):
                edge(file_id, make(mod), 'imports', line, context='import')

                if word in ('import', 'use'):
                    calls_map.setdefault(parent_id, []).append(mod)

            if word == 'alias':
                record(args_node, parent_id)

            return

        for child in raw.children:
            walk(child, parent_id)

    walk(root)

    labels = {n['label'].strip('()').lstrip('.'): n['id'] for n in nodes}
    lookup = {name: nid for nid, name in modnames.items()}
    pairs: set[tuple[str, str]] = set()
    calls: list[dict] = []

    def scope(caller_id: str) -> list[str]:
        out: list[str] = []
        visited: set[str] = set()
        holder = enclosing.get(caller_id)

        while holder is not None and holder not in visited:
            visited.add(holder)
            out.extend(calls_map.get(holder, ()))
            holder = enclosing.get(holder)

        out.extend(calls_map.get(None, ()))

        return out

    def trace(raw, caller_id: str) -> None:
        if raw.type != 'call':
            for child in raw.children:
                trace(child, caller_id)

            return

        for child in raw.children:
            if child.type == 'identifier':
                kw_text = source[child.start_byte:child.end_byte].decode('utf-8', errors='replace')

                if kw_text in SKIPS:
                    if kw_text == 'alias':
                        for c in raw.children:
                            if c.type == 'arguments':
                                record(c, caller_id)

                    for c in raw.children:
                        trace(c, caller_id)

                    return

                break

        callee: str | None = None
        is_member = False
        target_mod: str | None = None

        for child in raw.children:
            if child.type == 'dot':
                is_member = True
                dot_val = source[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
                parts = dot_val.rstrip('.').split('.')

                if parts:
                    callee = parts[-1]

                if child.child_count == 3:
                    recv = child.children[0]
                    recv_text = source[recv.start_byte:recv.end_byte].decode('utf-8', errors='replace')

                    if MODULE.fullmatch(recv_text):
                        target_mod = expand(recv_text, caller_id)

                break

            if child.type == 'identifier':
                callee = source[child.start_byte:child.end_byte].decode('utf-8', errors='replace')
                break

        if callee and (target_mod or callee not in GLOBALS):
            if target_mod is not None:
                mod_id = lookup.get(target_mod)
                tgt_id = defs.get((mod_id, callee)) if mod_id else None
            else:
                tgt_id = None if is_member else labels.get(callee)

            if tgt_id and tgt_id != caller_id:
                pair = (caller_id, tgt_id)

                if pair not in pairs:
                    pairs.add(pair)
                    edge(caller_id, tgt_id, 'calls', raw.start_point[0] + 1, context='call')
            else:
                extra = {'elixir_module': target_mod} if target_mod else {}
                calls.append({
                    'caller_nid': caller_id,
                    'callee': callee,
                    'is_member_call': is_member,
                    'source_file': src,
                    'source_location': f'L{raw.start_point[0] + 1}',
                    'elixir_call_scope': scope(caller_id),
                    **extra,
                })

        for child in raw.children:
            trace(child, caller_id)

    for caller_id, body_node in bodies:
        trace(body_node, caller_id)

    clean = [e for e in edges if e['source'] in seen and (e['target'] in seen or e['relation'] == 'imports')]

    return {'nodes': nodes, 'edges': clean, 'raw_calls': calls}