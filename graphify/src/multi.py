from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import sys
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph


@dataclass(frozen=True)


class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)


class Result:
    py: str
    nx: str
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all((c.ok for c in self.checks))

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple((c for c in self.checks if not c.ok))

    def error(self) -> str:
        if self.ok:
            return f'Graphify MultiDiGraph probe passed (Python {self.py}, NetworkX {self.nx}).'

        failed = '; '.join((f'{c.name}: {c.detail}' for c in self.failed))

        return f'error: --multigraph failed: {failed}.'


def inspect(name: str, func: Callable[[], bool | str]) -> Check:
    try:
        detail = func()
    except Exception as exc:
        return Check(name, False, f'{type(exc).__name__}: {exc}')

    if detail is True:
        return Check(name, True, 'ok')

    if isinstance(detail, str):
        return Check(name, False, detail)

    return Check(name, False, f'unexpected result {detail!r}')


def build() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.add_node('a', label='A')
    graph.add_node('b', label='B')
    graph.add_edge('a', 'b', key='calls:a.py:L1', relation='calls', source_file='a.py')
    graph.add_edge('a', 'b', key='imports:a.py:L2', relation='imports', source_file='a.py')

    return graph


def parallel() -> bool | str:
    graph = build()

    if not graph.is_multigraph() or not graph.is_directed():
        return f'probe graph type was {type(graph).__name__}'

    if graph.number_of_edges('a', 'b') != 2:
        return f"expected 2 keyed parallel edges, got {graph.number_of_edges('a', 'b')}"

    keys = set(graph['a']['b'].keys())
    expected = {'calls:a.py:L1', 'imports:a.py:L2'}

    if keys != expected:
        return f'expected keys {sorted(expected)}, got {sorted(keys)}'

    return True


def trip() -> bool | str:
    graph = build()
    data = json_graph.node_link_data(graph, edges='links')

    if data.get('multigraph') is not True:
        return f"serialized multigraph flag was {data.get('multigraph')!r}"

    if data.get('directed') is not True:
        return f"serialized directed flag was {data.get('directed')!r}"

    links = data.get('links')

    if not isinstance(links, list) or len(links) != 2:
        length = 0 if not isinstance(links, list) else len(links)

        return f'serialized links length was {length}'

    keys: set[str] = set()

    for edge in links:
        if isinstance(edge, dict):
            k = edge.get('key')

            if isinstance(k, str):
                keys.add(k)

    expected = {'calls:a.py:L1', 'imports:a.py:L2'}

    if keys != expected:
        return f'serialized keys {sorted(keys)} did not match {sorted(expected)}'

    loaded = json_graph.node_link_graph(data, edges='links')

    if not isinstance(loaded, nx.MultiDiGraph):
        return f'round-trip graph type was {type(loaded).__name__}'

    if loaded.number_of_edges('a', 'b') != 2:
        return f"round-trip edge count was {loaded.number_of_edges('a', 'b')}"

    found = set(loaded['a']['b'].keys())

    if found != expected:
        return f'round-trip keys {sorted(found)} did not match {sorted(expected)}'

    return True


def overwrite() -> bool | str:
    graph = nx.MultiDiGraph()
    graph.add_edge('x', 'y', key='same', marker='first')
    graph.add_edge('x', 'y', key='same', marker='second')
    edges = list(graph.edges(keys=True, data=True))

    if len(edges) != 1:
        return f'expected one edge after duplicate-key add, got {len(edges)}'

    if edges[0][3].get('marker') != 'second':
        return f"expected second attr overwrite, got {edges[0][3].get('marker')!r}"

    return True


def reserved() -> bool | str:
    graph = nx.MultiDiGraph()
    attrs: dict[str, Any] = {'key': 'attr-key', 'relation': 'calls'}

    try:
        graph.add_edge('a', 'b', key='schema-key', **attrs)
    except TypeError:
        return True

    return 'add_edge accepted duplicate key keyword and attr; loader must not rely on this'


def removal() -> bool | str:
    graph = nx.MultiDiGraph()
    graph.add_edge('a', 'b', key='one')
    graph.add_edge('a', 'b', key='two')
    graph.remove_edges_from([('a', 'b')])
    remaining = graph.number_of_edges('a', 'b')

    if remaining != 1:
        return f'expected one remaining edge after two-tuple removal, got {remaining}'

    return True


def undirected() -> bool | str:
    graph = build()
    target = graph.to_undirected()
    view = graph.to_undirected(as_view=True)

    if not isinstance(target, nx.MultiGraph):
        return f'to_undirected() returned {type(target).__name__}'

    if not isinstance(view, nx.MultiGraph):
        return f'to_undirected(as_view=True) returned {type(view).__name__}'

    return True


@lru_cache(maxsize=1)


def probe() -> Result:
    checks = (
        inspect('keyed_parallel_edges', parallel),
        inspect('node_link_edges_links_round_trip', trip),
        inspect('duplicate_key_overwrite_semantics', overwrite),
        inspect('reserved_key_attr_rejected', reserved),
        inspect('remove_edges_from_two_tuple_semantics', removal),
        inspect('to_undirected_preserves_multigraph_type', undirected),
    )

    return Result(

        py=f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
        nx=nx.__version__,
        checks=checks,
    )


def require() -> Result:
    res = probe()

    if not res.ok:
        raise RuntimeError(res.error())

    return res