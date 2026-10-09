from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from graphify.ids import make
from graphify.security import label as clean_label
from graphify.src.base import stem


NAMES: frozenset[str] = frozenset({'.mcp.json', 'claude_desktop_config.json', 'mcp.json', 'mcp_servers.json'})
LIMIT = 1048576
MAX_SERVERS = 200

NPM_RE = re.compile(r'^@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:@[\w.\-+]+)?$')
PY_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*-mcp(?:-[a-z0-9._-]+)?$|^mcp-[a-z0-9][a-z0-9._-]*$')
FLAG_RE = re.compile(r'^-{1,2}\w')


def match(loc: Path) -> bool:
    return loc.name in NAMES


def version(pkg: str) -> str:
    if pkg.startswith('@'):
        pos = pkg.find('@', 1)

        return pkg if pos == -1 else pkg[:pos]

    pos = pkg.find('@')

    return pkg if pos == -1 else pkg[:pos]


def package(args: list[Any]) -> str | None:
    for raw in args:
        if not isinstance(raw, str):
            continue

        arg = raw.strip()

        if not arg or FLAG_RE.match(arg):
            continue

        if NPM_RE.match(arg):
            return version(arg)

        if PY_RE.match(arg):
            return arg

    return None


def node(nodes: list[dict[str, Any]], seen: set[str], *, nid: str, label: str, kind: str, loc: str, line: int) -> None:
    if not nid or nid in seen:
        return

    seen.add(nid)
    nodes.append({
        'id': nid,
        'label': clean_label(label),
        'file_type': 'code',
        'source_file': loc,
        'source_location': f'L{line}',
        'metadata': {'mcp_kind': kind},
    })


def edge(edges: list[dict[str, Any]], seen: set[tuple[str, str, str]], *, src: str, dst: str, rel: str, loc: str, line: int, ctx: str | None = None) -> None:
    if not src or not dst or src == dst:
        return

    key = (src, dst, rel)

    if key in seen:
        return

    seen.add(key)
    item: dict[str, Any] = {
        'source': src,
        'target': dst,
        'relation': rel,
        'source_file': loc,
        'source_location': f'L{line}',
    }

    if ctx:
        item['context'] = ctx

    edges.append(item)


def server(*, name: str, spec: dict[str, Any], fnid: str, fstem: str, loc: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], snodes: set[str], sedges: set[tuple[str, str, str]]) -> None:
    snid = make(fstem, 'mcp_server', name)
    node(nodes, snodes, nid=snid, label=name, kind='mcp_server', loc=loc, line=1)
    edge(edges, sedges, src=fnid, dst=snid, rel='contains', loc=loc, line=1)

    cmd = spec.get('command')

    if isinstance(cmd, str) and cmd.strip():
        clabel = cmd.strip()
        cnid = make('mcp_command', clabel)
        node(nodes, snodes, nid=cnid, label=clabel, kind='mcp_command', loc=loc, line=1)
        edge(edges, sedges, src=snid, dst=cnid, rel='references', loc=loc, line=1, ctx='command')

    args = spec.get('args')

    if isinstance(args, list):
        pkg = package(args)

        if pkg:
            pnid = make('mcp_package', pkg)
            node(nodes, snodes, nid=pnid, label=pkg, kind='mcp_package', loc=loc, line=1)
            edge(edges, sedges, src=snid, dst=pnid, rel='references', loc=loc, line=1, ctx='package')

    env = spec.get('env')

    if isinstance(env, dict):
        for ename in env.keys():
            if not isinstance(ename, str) or not ename:
                continue

            enid = make('env_var', ename)
            node(nodes, snodes, nid=enid, label=ename, kind='env_var', loc=loc, line=1)
            edge(edges, sedges, src=snid, dst=enid, rel='requires_env', loc=loc, line=1)


def extract(path: Path) -> dict[str, Any]:
    try:
        with path.open('rb') as fh:
            raw = fh.read(LIMIT + 1)
    except OSError as exc:
        return {'nodes': [], 'edges': [], 'error': f'mcp read error: {exc}'}

    if len(raw) > LIMIT:
        return {'nodes': [], 'edges': [], 'error': 'mcp config too large'}

    try:
        body = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        return {'nodes': [], 'edges': [], 'error': f'mcp decode error: {exc}'}

    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        return {'nodes': [], 'edges': [], 'error': f'mcp json error: {exc}'}

    if not isinstance(doc, dict):
        return {'nodes': [], 'edges': [], 'error': 'root is not object'}

    servers = doc.get('mcpServers')

    if not isinstance(servers, dict):
        nested = doc.get('mcp')

        if isinstance(nested, dict):
            servers = nested.get('servers')

        if not isinstance(servers, dict):
            return {'nodes': [], 'edges': [], 'error': 'no mcpServers'}

    loc = str(path)
    fnid = make(loc)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    snodes: set[str] = set()
    sedges: set[tuple[str, str, str]] = set()

    node(nodes, snodes, nid=fnid, label=path.name, kind='mcp_config_file', loc=loc, line=1)
    fstem = stem(path)
    count = 0

    for sname, spec in servers.items():
        if not isinstance(sname, str) or not sname:
            continue

        if not isinstance(spec, dict):
            continue

        if count >= MAX_SERVERS:
            break

        count += 1
        server(name=sname, spec=spec, fnid=fnid, fstem=fstem, loc=loc, nodes=nodes, edges=edges, snodes=snodes, sedges=sedges)

    return {'nodes': nodes, 'edges': edges}