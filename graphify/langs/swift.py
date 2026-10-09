from __future__ import annotations

from pathlib import Path

from ..src.base import read, make
from ..src.models import LanguageConfig
from ..src.engine import generic
from ..interface import resolve


def include(
    node,
    source: bytes,
    root: str,
    stem: str,
    edges: list,
    target: str,
    stack: list[str] | None = None,
) -> list[tuple[str, str]]:
    modules: list[tuple[str, str]] = []

    for child in node.children:
        if child.type == 'identifier':
            raw = read(child, source)
            dest = make(raw)

            edges.append({
                'source': root,
                'target': dest,
                'relation': 'imports',
                'context': 'import',
                'source_file': target,
                'source_location': f'L{node.start_point[0] + 1}',
            })
            modules.append((dest, raw))
            break

    return modules


config = LanguageConfig(
    ts_module='tree_sitter_swift',
    class_types=frozenset({'class_declaration', 'protocol_declaration'}),
    function_types=frozenset({
        'function_declaration',
        'protocol_function_declaration',
        'init_declaration',
        'deinit_declaration',
        'subscript_declaration',
    }),
    import_types=frozenset({'import_declaration'}),
    call_types=frozenset({'call_expression'}),
    call_function_field='',
    call_accessor_node_types=frozenset({'navigation_expression'}),
    call_accessor_field='',
    name_fallback_child_types=('simple_identifier', 'type_identifier', 'user_type'),
    body_fallback_child_types=('class_body', 'protocol_body', 'function_body', 'enum_class_body'),
    function_boundary_types=frozenset({
        'function_declaration',
        'protocol_function_declaration',
        'init_declaration',
        'deinit_declaration',
        'subscript_declaration',
    }),
    import_handler=include,
)


def extract(path: Path) -> dict:
    return generic(path, config)


def dispatch(batch: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    resolve(batch, nodes, edges, suffixes=('.swift',))