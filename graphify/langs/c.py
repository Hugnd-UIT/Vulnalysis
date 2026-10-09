from __future__ import annotations

from pathlib import Path

from ..src.base import read, make
from ..src.models import LanguageConfig
from ..src.engine import generic
from ..src.resolve import cinclude


def symbol(node, source: bytes) -> str | None:
    if node.type == 'identifier':
        return read(node, source)

    decl = node.child_by_field_name('declarator')

    if decl:
        return symbol(decl, source)

    for child in node.children:
        if child.type == 'identifier':
            return read(child, source)

    return None


def include(node, source: bytes, root: str, stem: str, edges: list, target: str, stack: list[str] | None = None) -> None:
    for child in node.children:
        if child.type in ('string_literal', 'system_lib_string', 'string'):
            raw = read(child, source).strip('"<> ')

            if child.type != 'system_lib_string':
                found = cinclude(raw, target)

                if found is not None:
                    dest = make(str(found))
                    edges.append({
                        'source': root,
                        'target': dest,
                        'relation': 'imports',
                        'context': 'import',
                        'source_file': target,
                        'source_location': f'L{node.start_point[0] + 1}',
                        'target_file': str(found),
                    })
                    break

            module = raw.split('/')[-1].split('.')[0]

            if module:
                dest = make(module)
                edges.append({
                    'source': root,
                    'target': dest,
                    'relation': 'imports',
                    'context': 'import',
                    'source_file': target,
                    'source_location': f'L{node.start_point[0] + 1}',
                })
            break


config = LanguageConfig(
    ts_module='tree_sitter_c',
    class_types=frozenset(),
    function_types=frozenset({'function_definition'}),
    import_types=frozenset({'preproc_include'}),
    call_types=frozenset({'call_expression'}),
    call_function_field='function',
    call_accessor_node_types=frozenset({'field_expression'}),
    call_accessor_field='field',
    function_boundary_types=frozenset({'function_definition'}),
    import_handler=include,
    resolve_function_name_fn=symbol,
)


def extract(path: Path) -> dict:
    return generic(path, config)