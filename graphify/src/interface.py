from __future__ import annotations


RELATION = 'dispatches_to'


def declared(node: dict | None, suffixes: tuple[str, ...]) -> bool:
    if not node:
        return False

    loc = node.get('source_file')

    return bool(loc) and str(loc).endswith(suffixes)


def label(node: dict) -> str:
    text = str(node.get('label', '')).strip().removeprefix('.')

    return text.split('(', 1)[0]


def resolve(per: list[dict], nodes: list[dict], edges: list[dict], *, suffixes: tuple[str, ...]) -> None:
    if not any((str(r.get('source_file', '')).endswith(suffixes) for r in per if isinstance(r, dict))) and not any((str(n.get('source_file', '')).endswith(suffixes) for n in nodes)):
        return

    lut = {n.get('id'): n for n in nodes}
    impls: dict[str, set[str]] = {}
    defs: dict[str, dict[str, set[str]]] = {}

    for e in edges:
        rel = e.get('relation')

        if rel == 'implements':
            impls.setdefault(e.get('target'), set()).add(e.get('source'))
        elif rel == 'method':
            src, dst = (e.get('source'), e.get('target'))
            mnode = lut.get(dst)

            if mnode is None or not declared(mnode, suffixes):
                continue

            name = label(mnode)

            if name:
                defs.setdefault(src, {}).setdefault(name, set()).add(dst)

    if not impls or not defs:
        return

    pairs = {(e.get('source'), e.get('target')) for e in edges if e.get('relation') == RELATION}
    extra: list[dict] = []

    for target, group in impls.items():
        if len(group) != 1:
            continue

        item = next(iter(group))
        inode = lut.get(target)
        cnode = lut.get(item)

        if inode is None or cnode is None:
            continue

        if not declared(inode, suffixes) or not declared(cnode, suffixes):
            continue

        methods = defs.get(item, {})

        for name, decls in defs.get(target, {}).items():
            if len(decls) != 1:
                continue

            cands = methods.get(name, set())

            if len(cands) != 1:
                continue

            src = next(iter(decls))
            dst = next(iter(cands))

            if src == dst or (src, dst) in pairs:
                continue

            pairs.add((src, dst))
            extra.append({
                'source': src,
                'target': dst,
                'relation': RELATION,
                'context': 'call',
                'source_file': str(cnode.get('source_file', '')),
                'source_location': cnode.get('source_location'),
            })

    edges.extend(extra)