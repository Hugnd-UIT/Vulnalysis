from __future__ import annotations

import ast
from pathlib import Path


def attr(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id

    if isinstance(node, ast.Attribute):
        val = attr(node.value)

        return f'{val}.{node.attr}' if val else node.attr

    if isinstance(node, ast.Call):
        val = attr(node.func)

        return f'{val}()' if val else None

    return None


def define(node, scope: list, stem: str, file: str, table: dict, types: set, nodes: list, edges: list) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            for item in child.names:
                mod = item.name
                asname = item.asname or mod
                dest = mod.replace('.', '_')
                table[asname] = dest
                nodes.append({
                    'id': dest,
                    'label': mod,
                    'file_type': 'package',
                    'source_file': file,
                    'source_location': f'L{child.lineno}',
                })
                edges.append({
                    'source': stem,
                    'target': dest,
                    'relation': 'imports',
                    'source_file': file,
                    'source_location': f'L{child.lineno}',
                })
        elif isinstance(child, ast.ImportFrom):
            mod = child.module or ''

            for item in child.names:
                name = item.name
                asname = item.asname or name
                dest = f'{mod}_{name}'.strip('_').replace('.', '_')
                table[asname] = dest
                label = f'{mod}.{name}' if mod else name
                nodes.append({
                    'id': dest,
                    'label': label,
                    'file_type': 'symbol',
                    'source_file': file,
                    'source_location': f'L{child.lineno}',
                })
                edges.append({
                    'source': stem,
                    'target': dest,
                    'relation': 'imports',
                    'source_file': file,
                    'source_location': f'L{child.lineno}',
                })
        elif isinstance(child, ast.ClassDef):
            dest = f'{scope[-1]}_{child.name}'
            types.add(dest)
            table[child.name] = dest
            nodes.append({
                'id': dest,
                'label': child.name,
                'file_type': 'class',
                'source_file': file,
                'source_location': f'L{child.lineno}',
            })
            edges.append({
                'source': scope[-1],
                'target': dest,
                'relation': 'contains',
                'source_file': file,
                'source_location': f'L{child.lineno}',
            })
            scope.append(dest)
            define(child, scope, stem, file, table, types, nodes, edges)
            scope.pop()
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dest = f'{scope[-1]}_{child.name}'
            table[child.name] = dest
            nodes.append({
                'id': dest,
                'label': f'{child.name}()',
                'file_type': 'function',
                'source_file': file,
                'source_location': f'L{child.lineno}',
            })
            edges.append({
                'source': scope[-1],
                'target': dest,
                'relation': 'contains',
                'source_file': file,
                'source_location': f'L{child.lineno}',
            })
            scope.append(dest)
            define(child, scope, stem, file, table, types, nodes, edges)
            scope.pop()


def target(callee: str, scope: list, stem: str, table: dict, types: set) -> str:
    if callee.startswith('self.'):
        method = callee[5:]
        cls = next((s for s in reversed(scope) if s in types), None)

        if cls:
            return f'{cls}_{method}'

        return f'{scope[-1]}_{method}'

    if callee in table:
        return table[callee]

    if '.' in callee:
        prefix, _, suffix = callee.partition('.')

        if prefix in table:
            return f'{table[prefix]}_{suffix}'.replace('.', '_')

        return callee.replace('.', '_')

    return f'{stem}_{callee}'


def resolve(node, scope: list, stem: str, file: str, table: dict, types: set, nodes: list, edges: list) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            dest = f'{scope[-1]}_{child.name}'
            scope.append(dest)
            resolve(child, scope, stem, file, table, types, nodes, edges)
            scope.pop()
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dest = f'{scope[-1]}_{child.name}'
            scope.append(dest)
            resolve(child, scope, stem, file, table, types, nodes, edges)
            scope.pop()
        elif isinstance(child, ast.Call):
            callee = attr(child.func)

            if callee:
                dest = target(callee, scope, stem, table, types)

                if not any((n['id'] == dest for n in nodes)):
                    nodes.append({
                        'id': dest,
                        'label': f'{callee}()',
                        'file_type': 'external',
                    })

                edges.append({
                    'source': scope[-1],
                    'target': dest,
                    'callee': callee,
                    'relation': 'calls',
                    'source_file': file,
                    'source_location': f'L{child.lineno}',
                })

            resolve(child, scope, stem, file, table, types, nodes, edges)
        else:
            resolve(child, scope, stem, file, table, types, nodes, edges)


def extract(path: Path, *, root: Path | None = None) -> dict:
    stem = path.stem
    file = str(path.relative_to(root) if root else path.name).replace('\\', '/')
    nodes = [{
        'id': stem,
        'label': path.name,
        'file_type': 'module',
        'source_file': file,
        'source_location': 'L1',
    }]
    edges = []
    table = {}
    types = set()
    scope = [stem]

    try:
        raw = path.read_text(encoding='utf-8')
        tree = ast.parse(raw, filename=str(path))
        define(tree, scope, stem, file, table, types, nodes, edges)
        resolve(tree, scope, stem, file, table, types, nodes, edges)
    except Exception:
        pass

    return {'nodes': nodes, 'edges': edges}