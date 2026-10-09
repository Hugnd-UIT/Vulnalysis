from __future__ import annotations
import html
from pathlib import Path
from ..src.base import make


def extract(path: Path) -> dict:
    from graphify.extract import extcsharp

    return extcsharp(path)


def dispatch(batch: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    from graphify.interface import resolve
    resolve(batch, nodes, edges, suffixes=('.cs',))


def index(nodes: list[dict], edges: list[dict] | None=None) -> dict[tuple[str, str], str]:
    ignored: set[str] = set()

    if edges is not None:
        for edge in edges:
            rel = edge.get('relation')

            if rel in ('case_of', 'defines', 'method'):
                target = edge.get('_tgt') or edge.get('target')

                if isinstance(target, str):
                    ignored.add(target)
    items: dict[tuple[str, str], list[dict]] = {}

    for node in nodes:
        if node.get('type') == 'namespace':
            continue

        data = node.get('metadata') or {}

        if not isinstance(data, dict):
            data = {}

        if data.get('is_nested_type'):
            continue

        nid = node.get('id')
        label = node.get('label')

        if not (isinstance(nid, str) and nid and isinstance(label, str) and label):
            continue

        if edges is not None and nid in ignored:
            continue

        source = node.get('source_file')

        if not isinstance(source, str) or not source.endswith('.cs') or node.get('file_type') != 'code':
            continue

        if label.endswith(')') or label.startswith('.') or '.' in label:
            continue

        space = data.get('namespace', '')

        if not isinstance(space, str):
            space = ''
        items.setdefault((space, label), []).append(node)

    return {key: sorted(entry, key=lambda n: (str(n.get('source_file') or ''), str(n.get('source_location') or ''), str(n.get('id') or '')))[0]['id'] for key, entry in items.items()}


def strip(target: str) -> str:
    target = target.strip()

    if not target.endswith('>'):
        return target

    depth = 0

    for idx in range(len(target) - 1, -1, -1):
        ch = target[idx]

        if ch == '>':
            depth += 1
        elif ch == '<':
            depth -= 1

            if depth == 0:
                return target[:idx].strip()

    return target


def imports(batch: list[dict], paths: list[Path], nodes: list[dict], edges: list[dict]) -> None:
    _ = (batch, paths)
    spaces: dict[str, str] = {}

    for node in sorted(nodes, key=lambda n: (str(n.get('source_file') or ''), str(n.get('source_location') or ''), str(n.get('id') or ''))):
        if node.get('type') != 'namespace':
            continue

        label = node.get('label')
        nid = node.get('id')

        if isinstance(label, str) and label and isinstance(nid, str) and nid:
            spaces.setdefault(label, nid)
    indexed = index(nodes, edges)

    if not spaces and (not indexed):
        return
    repointed: set[str] = set()

    for edge in edges:
        if edge.get('relation') != 'imports':
            continue

        data = edge.get('metadata') or {}

        if not isinstance(data, dict):
            continue

        kind = data.get('using_kind')
        target = data.get('target_fqn')

        if not kind or not isinstance(target, str) or (not target):
            continue

        resolved = None

        if kind == 'namespace':
            resolved = spaces.get(target)
        elif kind == 'alias':
            clean = strip(html.unescape(target))
            prefix, sep, name = clean.rpartition('.')

            if sep and prefix in spaces:
                resolved = indexed.get((prefix, name))
        old = edge.get('target')

        if resolved and resolved != old:
            edge['target'] = resolved

            if isinstance(old, str) and old:
                repointed.add(old)

    if not repointed:
        return
    used: set[str] = set()

    for edge in edges:
        used.add(edge.get('source'))
        used.add(edge.get('target'))
    nodes[:] = [node for node in nodes if node.get('id') not in repointed or node.get('id') in used]
EXTS = ('.cs', '.razor', '.cshtml')


def cs(value: object) -> bool:
    return isinstance(value, str) and value.endswith('.cs')


def dotnet(value: object) -> bool:
    return isinstance(value, str) and value.endswith(EXTS)


def meta(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def space(node: dict | None) -> str:
    data = meta((node or {}).get('metadata'))
    val = data.get('namespace', '')

    return val if isinstance(val, str) else ''


def chain(node: dict) -> list[str]:
    val = meta(node.get('metadata')).get('scope_chain')

    return val if isinstance(val, list) else []


def scoped(kind: str, scope: str | None, src: dict) -> bool:
    if kind == 'file':
        return True

    return scope is not None and scope in chain(src)


def scopes(ctx: dict, src: dict, path: str) -> list[str]:

    def add(items: list[str], val: str) -> None:
        if val not in items:
            items.append(val)
    out: list[str] = []
    add(out, space(src))
    add(out, '')

    for space_val, kind, scope in ctx['usings'].get(path, []):
        if scoped(kind, scope, src):
            add(out, space_val)

    return out


def alias(ctx: dict, label: str, src: dict, path: str) -> str | None:
    hits = set()

    for target, kind, scope in ctx['aliases'].get(path, {}).get(label, []):
        if not scoped(kind, scope, src):
            continue

        clean = strip(html.unescape(target))
        prefix, sep, name = clean.rpartition('.')

        if not sep:
            name = prefix
            prefix = ''

        if not name:
            continue

        hit = ctx['indexed'].get((prefix, name))

        if hit:
            hits.add(hit)

    return next(iter(hits)) if len(hits) == 1 else None


def type(ctx: dict, label: str, src: dict, path: str) -> tuple[str | None, bool]:
    if label in ctx['aliases'].get(path, {}):
        return (alias(ctx, label, src, path), True)

    candidates: list[str] = []

    for space_val in scopes(ctx, src, path):
        hit = ctx['indexed'].get((space_val, label))

        if hit and hit not in candidates:
            candidates.append(hit)

    if len(candidates) == 1:
        return (candidates[0], True)

    if candidates:
        return (None, True)

    val = space(src)

    if '.' in val:
        parts = val.split('.')

        for idx in range(len(parts) - 1, 0, -1):
            hit = ctx['indexed'].get(('.'.join(parts[:idx]), label))

            if hit:
                return (hit, True)

    return (None, False)


def label(ctx: dict, val: str, src: dict, path: str) -> str | None:
    resolved, _ = type(ctx, val, src, path)

    return resolved


def qualified(ctx: dict, val: str, qualifier: object, src: dict, path: str) -> str | None:
    if not isinstance(qualifier, str) or not qualifier:
        return None

    entries = [entry for entry in ctx['aliases'].get(path, {}).get(qualifier, []) if scoped(entry[1], entry[2], src)]

    if entries:
        hits = set()

        for target, _, _ in entries:
            clean = strip(html.unescape(target))
            hit = ctx['indexed'].get((clean, val))

            if hit:
                hits.add(hit)

        return next(iter(hits)) if len(hits) == 1 else None

    if qualifier in ctx['known']:
        return ctx['indexed'].get((qualifier, val))

    return None


def resolver(nodes: list[dict], edges: list[dict]) -> dict:
    mapping: dict[str, dict] = {node['id']: node for node in nodes if isinstance(node.get('id'), str) and node.get('id')}
    indexed_map = index(nodes, edges)
    known_set = {node.get('label') for node in nodes if node.get('type') == 'namespace' and isinstance(node.get('label'), str)}
    usings: dict[str, list[tuple[str, str, str | None]]] = {}
    aliases: dict[str, dict[str, list[tuple[str, str, str | None]]]] = {}

    for edge in edges:
        if edge.get('relation') != 'imports':
            continue

        src = mapping.get(edge.get('source'))

        if not (src and isinstance(src.get('label'), str) and src.get('label', '').endswith(EXTS)):
            continue

        path = src.get('source_file')

        if not dotnet(path):
            continue

        data = meta(edge.get('metadata'))
        target = data.get('target_fqn')

        if not isinstance(target, str) or not target:
            continue

        kind = data.get('scope_kind') or 'file'
        scope = data.get('scope_id')
        ukind = data.get('using_kind')

        if ukind == 'namespace':
            entry = (target, kind, scope)
            bucket = usings.setdefault(path, [])

            if entry not in bucket:
                bucket.append(entry)
        elif ukind == 'alias':
            item = data.get('alias')

            if isinstance(item, str) and item:
                entry = (target, kind, scope)
                bucket = aliases.setdefault(path, {}).setdefault(item, [])

                if entry not in bucket:
                    bucket.append(entry)

    return {
        'mapping': mapping,
        'indexed': indexed_map,
        'known': known_set,
        'usings': usings,
        'aliases': aliases,
    }


def references(batch: list[dict], paths: list[Path], nodes: list[dict], edges: list[dict]) -> None:
    _ = (batch, paths)
    ctx = resolver(nodes, edges)
    mapping = ctx['mapping']
    aliases = ctx['aliases']

    def placeholder(node: dict | None) -> bool:
        return bool(node) and (not node.get('source_file'))

    def valid(node: dict) -> bool:
        if node.get('type') == 'namespace':
            return True

        path = node.get('source_file')

        return not path or cs(path)

    def token(target: dict, path: str) -> str | None:
        name = target.get('label')

        if not isinstance(name, str) or not name:
            return None

        if not name.endswith('.cs'):
            return name

        stem = name[:-3]

        for item in aliases.get(path, {}):
            if item.lower() == stem.lower() or make(item) == make(stem):
                return item

        return stem or None

    def mint(name: str, current: object) -> str:
        curr = mapping.get(current)

        if placeholder(curr) and curr.get('label') == name:
            return str(current)

        for node in nodes:
            nid = node.get('id')

            if isinstance(nid, str) and node.get('label') == name and placeholder(node):
                return nid

        stem = make(name)
        stub = stem

        if stub in mapping:
            stub = make('csharp_type_ref', name)
            num = 2

            while stub in mapping:
                stub = make('csharp_type_ref', name, str(num))
                num += 1
        node = {'id': stub, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': ''}
        nodes.append(node)
        mapping[stub] = node

        return stub

    relations = {'implements', 'inherits', 'references'}
    repointed: set[str] = set()

    for edge in edges:
        if edge.get('relation') not in relations:
            continue

        path = edge.get('source_file')

        if not dotnet(path):
            continue

        src = mapping.get(edge.get('source'))
        tgt = mapping.get(edge.get('target'))

        if not src or not tgt:
            continue

        if not valid(tgt):
            continue

        data = meta(edge.get('metadata'))
        name = data.get('ref_token') or token(tgt, path)

        if not name:
            continue

        if data.get('qualified'):
            resolved = qualified(ctx, name, data.get('ref_qualifier'), src, path)
        else:
            resolved = label(ctx, name, src, path)
        dest = edge.get('target')
        final = resolved or mint(name, dest)

        if final != dest:
            edge['target'] = final

            if isinstance(dest, str) and placeholder(tgt):
                repointed.add(dest)

    if not repointed:
        return
    used: set[str] = set()

    for edge in edges:
        used.add(edge.get('source'))
        used.add(edge.get('target'))
    nodes[:] = [node for node in nodes if node.get('id') not in repointed or node.get('id') in used]