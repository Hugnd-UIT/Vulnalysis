from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx

RELATION = 'same_type_as'


def link(merged: nx.Graph) -> int:
    types: dict[tuple[str, str], list[str]] = defaultdict(list)

    for node, data in merged.nodes(data=True):
        if not data.get('_callable_class') or not data.get('source_file') or not data.get('repo'):
            continue

        space = str((data.get('metadata') or {}).get('namespace') or '')
        label = str(data.get('label') or '')

        if not space or not label:
            continue

        types[space, label].append(node)

    added = 0

    for (space, label), nodes in types.items():
        if len(nodes) < 2:
            continue

        if len({merged.nodes[n].get('repo') for n in nodes}) < 2:
            continue

        for i, left in enumerate(nodes):
            for right in nodes[i + 1:]:
                if merged.nodes[left].get('repo') == merged.nodes[right].get('repo'):
                    continue

                if merged.has_edge(left, right):
                    continue

                merged.add_edge(
                    left,
                    right,
                    relation=RELATION,
                    context='cross_repo',
                    source_file=str(merged.nodes[left].get('source_file') or ''),
                    _src=left,
                    _tgt=right,
                )
                added += 1

    return added