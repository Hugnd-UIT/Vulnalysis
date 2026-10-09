from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from ..src.base import bytes as read_bytes, make, read, stem
from graphify.security import metadata


EXPANSION = re.compile(r'^(?:(?:\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*)/?)+')
DIRNAME = re.compile(r'\$\(dirname\s+"?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"?\)')
LEADING = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|^\$([A-Za-z_][A-Za-z0-9_]*)')
IDIOM = re.compile(r'dirname[^)]*\)((?:/\.\.)*)')
COMMANDS = frozenset({'source', '.'})
RUNNERS = frozenset({'bash', 'sh', 'zsh', 'ksh', 'dash'})
INVOKERS = frozenset({'python', 'python3', 'python2', 'node', 'nodejs', 'ruby', 'perl'})
EXTENSIONS = ('.py', '.js', '.mjs', '.cjs', '.rb', '.pl', '.sh')
PARENTS = frozenset({'command_substitution', 'process_substitution'})


def suffix(raw: str, allow: bool = False) -> str | None:
    val = EXPANSION.sub('', raw, count=1).lstrip('/')

    if not val or '$' in val:
        return None

    if not allow and '..' in val.split('/'):
        return None

    return val


def within(ceiling: Path, target: Path) -> bool:
    top = os.path.normpath(str(ceiling))
    sub = os.path.normpath(str(target))

    return sub == top or sub.startswith(top + os.sep)


def base(val: str, folder: Path) -> Path | None:
    raw = val.strip().strip('\'"')

    if not raw:
        return None

    if 'dirname' in raw and ('BASH_SOURCE' in raw or '$0' in raw):
        hit = IDIOM.search(raw)
        root = folder

        for _ in range(hit.group(1).count('..') if hit else 0):
            root = root.parent

        return root

    if '$' in raw or '`' in raw:
        return None

    item = Path(raw)

    return item if item.is_absolute() else folder / item


def extract(path: Path) -> dict:
    try:
        import tree_sitter_bash as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-bash not installed'}

    try:
        source = read_bytes(path)
        root = Parser(Language(ts.language())).parse(source).root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    src = str(path)
    base_name = stem(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    calls: list[dict] = []
    sources: list[dict] = []
    raw_calls: set[tuple[str, str]] = set()
    seen: set[str] = set()
    bodies: list[tuple[str, Any]] = []
    funcs: set[str] = set()

    def node(nid: str, label: str, line: int, kind: str = 'code') -> None:
        if nid and nid not in seen:
            seen.add(nid)
            nodes.append({
                'id': nid,
                'label': label,
                'file_type': 'code',
                'source_file': src,
                'source_location': f'L{line}',
                'metadata': metadata({'language': 'bash', 'kind': kind}),
            })

    def edge(source_id: str, target_id: str, relation: str, line: int, context: str | None = None, target_file: str | None = None) -> None:
        if not source_id or not target_id or source_id == target_id:
            return

        item = {
            'source': source_id,
            'target': target_id,
            'relation': relation,
            'source_file': src,
            'source_location': f'L{line}',
        }

        if context:
            item['context'] = context

        if target_file is not None:
            item['target_file'] = target_file

        edges.append(item)

    file_id = make(src)
    entry_id = file_id + '__entry'

    node(file_id, path.name, 1, kind='file')
    node(entry_id, f'{path.name} script', 1, kind='bash_entrypoint')
    edge(file_id, entry_id, 'contains', 1)

    def text(raw) -> str:
        return source[raw.start_byte:raw.end_byte].decode('utf-8', errors='replace')

    def expanded(raw) -> bool:
        parent = raw.parent

        while parent is not None:
            if parent.type in PARENTS:
                return True

            parent = parent.parent

        return False

    def allowed(cmd) -> bool:
        parent = cmd.parent
        sub = False

        while parent is not None:
            if parent.type == 'process_substitution':
                return False

            if parent.type == 'command_substitution':
                sub = True

            if parent.type == 'variable_assignment' and sub:
                return True

            parent = parent.parent

        return not sub

    def literal(raw) -> str | None:
        val = text(raw).strip()

        if not val:
            return None

        if val[0:1] in {"'", '"'} and val[-1:] == val[0]:
            val = val[1:-1]

        if any((tok in val for tok in ('$', '`', '$(', '<(', '>', '|', ';', '&'))):
            return None

        return val

    def funcname(raw) -> str | None:
        for child in raw.children:
            if child.type == 'word':
                return literal(child)

        return None

    def trace(body, caller_id: str, visited: set) -> None:
        if body is None:
            return

        for child in body.children:
            if child.type == 'function_definition':
                continue

            if child.type == 'command' and allowed(child):
                name_node = child.child_by_field_name('name')

                if name_node is None and child.children:
                    name_node = child.children[0]

                if name_node:
                    name = literal(name_node)

                    if name and name in funcs:
                        tgt = make(base_name, name)
                        key = (caller_id, tgt)

                        if tgt and key not in visited:
                            visited.add(key)
                            edge(caller_id, tgt, 'calls', child.start_point[0] + 1, context='call')
                    elif name and name not in COMMANDS and (name not in RUNNERS):
                        raw_key = (caller_id, name)

                        if raw_key not in raw_calls:
                            raw_calls.add(raw_key)
                            calls.append({
                                'language': 'bash',
                                'callee': name,
                                'caller_nid': caller_id,
                                'source_file': src,
                                'source_location': f'L{child.start_point[0] + 1}',
                            })

            trace(child, caller_id, visited)

    def walk(raw, parent_id: str) -> None:
        kind = raw.type

        if kind == 'function_definition':
            name = funcname(raw)

            if name:
                fn_id = make(base_name, name)
                line = raw.start_point[0] + 1
                node(fn_id, f'{name}()', line, kind='bash_function')
                edge(parent_id, fn_id, 'defines', line)
                funcs.add(name)
                body = None

                for child in raw.children:
                    if child.type == 'compound_statement':
                        body = child
                        break

                bodies.append((fn_id, body))

                if body is not None:
                    walk(body, fn_id)

            return

        if kind == 'command':
            if expanded(raw):
                return

            name_node = raw.child_by_field_name('name')

            if name_node is None and raw.children:
                name_node = raw.children[0]

            if name_node:
                cmd = literal(name_node)
                args = [c for c in raw.children if c.type in ('word', 'string', 'concatenation') and c != name_node]

                if cmd in COMMANDS and cmd not in funcs:
                    if args:
                        val = read(args[0], source).strip().strip('\'"')
                        line = raw.start_point[0] + 1

                        if val.startswith(('.', '/')):
                            target = (path.parent / val).resolve()

                            if target.exists():
                                tgt_id = make(str(target))
                                edge(file_id, tgt_id, 'imports_from', line, context='import', target_file=str(target))
                                sources.append({'target_path': val, 'source_file': src, 'source_location': f'L{line}'})

                        elif '$' in val:
                            hit = DIRNAME.match(val)

                            if hit:
                                var_name = hit.group(1)

                                if var_name in bases:
                                    top = bases[var_name].parent
                                else:
                                    top = path.parent.parent

                                sub = val[hit.end():].lstrip('/')

                                if sub and '$' not in sub and ('..' not in sub.split('/')):
                                    target = (top / sub).resolve()

                                    if target.is_file():
                                        edge(file_id, make(str(target)), 'imports_from', line, context='import', target_file=str(target))
                                        sources.append({'target_path': str(target), 'source_file': src, 'source_location': f'L{line}'})

                            else:
                                hit_var = LEADING.match(val)
                                allow_dots = False

                                if hit_var:
                                    var_tok = hit_var.group(1) or hit_var.group(2)

                                    if var_tok in bases:
                                        allow_dots = True

                                sub = suffix(val, allow=allow_dots)

                                if sub:
                                    top = path.parent

                                    if hit_var:
                                        var_tok = hit_var.group(1) or hit_var.group(2)

                                        if var_tok in bases:
                                            top = bases[var_tok]

                                    if hit_var and var_tok in bases:
                                        target = Path(os.path.normpath(top / sub))
                                        limit = top.parent
                                    else:
                                        target = (top / sub).resolve()
                                        limit = top

                                    if within(limit, target) and target.is_file():
                                        edge(file_id, make(str(target)), 'imports_from', line, context='import', target_file=str(target))
                                        sources.append({'target_path': str(target), 'source_file': src, 'source_location': f'L{line}'})

                        else:
                            sibling: Path | None = None

                            if val:
                                try:
                                    cand = path.parent / val

                                    if cand.is_file():
                                        sibling = cand.resolve()
                                except OSError:
                                    sibling = None

                            if sibling is not None:
                                edge(file_id, make(str(sibling)), 'imports_from', line, context='import', target_file=str(sibling))
                                sources.append({'target_path': val, 'source_file': src, 'source_location': f'L{line}'})
                            else:
                                tgt_id = make(val)

                                if tgt_id:
                                    edge(file_id, tgt_id, 'imports', line, context='import')

                elif cmd not in funcs:
                    val = cmd if cmd and cmd.endswith('.sh') else None

                    if cmd and cmd in RUNNERS and args:
                        val = literal(args[0])

                    if val is None:
                        cand = read(name_node, source).strip().strip('\'"')

                        if cand.endswith('.sh') and '$' in cand:
                            val = suffix(cand) or None

                    if val and val.endswith('.sh'):
                        target = (path.parent / val).resolve()

                        if target.is_file():
                            dest = target

                            if not path.is_absolute():
                                try:
                                    dest = target.relative_to(Path.cwd().resolve())
                                except ValueError:
                                    pass

                            caller_id = entry_id if parent_id == file_id else parent_id
                            edge(caller_id, make(str(dest)) + '__entry', 'calls', raw.start_point[0] + 1, context='script_invocation', target_file=str(dest))

                known = cmd is not None and cmd in INVOKERS and (cmd not in funcs)
                word = text(name_node).strip()

                if word[0:1] in {"'", '"'} and word[-1:] == word[0]:
                    word = word[1:-1]

                interp = cmd is None and word.startswith('$') and ('$(' not in word) and ('/' not in word)

                if (known or interp) and args:
                    invoked = literal(args[0])

                    if invoked and invoked.endswith(EXTENSIONS):
                        target = (path.parent / invoked).resolve()

                        if target.is_file():
                            dest = target

                            if not path.is_absolute():
                                try:
                                    dest = target.relative_to(Path.cwd().resolve())
                                except ValueError:
                                    pass

                            caller_id = entry_id if parent_id == file_id else parent_id
                            tgt_id = make(str(dest))

                            if target.suffix == '.sh':
                                tgt_id += '__entry'

                            edge(caller_id, tgt_id, 'invokes', raw.start_point[0] + 1, context='script_invocation', target_file=str(dest))

            return

        if kind == 'declaration_command':
            if raw.parent and raw.parent.type == 'program':
                for child in raw.children:
                    if child.type == 'variable_assignment':
                        var_node = child.child_by_field_name('name')

                        if var_node:
                            var = read(var_node, source).strip()

                            if var:
                                var_id = make(base_name, var)
                                line = child.start_point[0] + 1
                                node(var_id, var, line)
                                edge(file_id, var_id, 'defines', line)

            return

        for child in raw.children:
            walk(child, parent_id)

    def prescan(raw) -> None:
        if raw.type == 'function_definition':
            name = funcname(raw)

            if name:
                funcs.add(name)

            for child in raw.children:
                prescan(child)
        else:
            for child in raw.children:
                prescan(child)

    prescan(root)
    bases: dict[str, Path] = {}

    for item in root.children:
        if item.type != 'variable_assignment':
            continue

        name_node = item.child_by_field_name('name')
        val_node = item.child_by_field_name('value')

        if name_node is None or val_node is None:
            continue

        name = read(name_node, source).strip()

        if not name:
            continue

        root_path = base(read(val_node, source), path.parent)

        if root_path is not None:
            bases[name] = root_path

    walk(root, file_id)
    visited: set = set()
    trace(root, entry_id, visited)

    for fn_id, body in bodies:
        trace(body, fn_id, set())

    return {'nodes': nodes, 'edges': edges, 'raw_calls': calls, 'bash_sources': sources}