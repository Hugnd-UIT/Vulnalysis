from __future__ import annotations

from pathlib import Path

from graphify.ids import normalize
from ..src.base import make, read, stem


NAMES = frozenset({'package.json', 'tsconfig.json', 'jsconfig.json', 'composer.json', 'deno.json', 'deno.jsonc', 'bower.json', 'manifest.json', 'app.json', 'now.json', 'vercel.json', 'angular.json', 'nest-cli.json', 'biome.json', 'biome.jsonc', 'renovate.json', '.babelrc', '.babelrc.json', '.eslintrc.json', '.prettierrc.json', '.prettierrc', 'babel.config.json'})
KEYS = frozenset({'dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'bundleDependencies', 'bundledDependencies', 'extends', '$schema', 'compilerOptions'})
SCHEMAS = frozenset({'$defs', 'definitions', '$id'})
LIMIT = 1048576
DEPKEYS = frozenset({'dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies', 'bundleDependencies', 'bundledDependencies'})


def isconfig(path: Path, obj, src: bytes) -> bool:
    name = path.name.casefold()

    if name in NAMES:
        return True

    if name.endswith(('.eslintrc.json', '.prettierrc.json', '.babelrc.json', 'tsconfig.json', 'jsconfig.json')):
        return True

    roots: set[str] = set()

    for child in obj.children:
        if child.type != 'pair':
            continue

        knode = child.child_by_field_name('key')

        if knode is None:
            continue

        kc = knode.child_by_field_name('string_content')
        roots.add(read(kc, src) if kc else read(knode, src).strip('"\''))

    if '$schema' in roots and roots & SCHEMAS:
        return False

    if roots & KEYS:
        return True

    return False


def key(pair, src: bytes) -> str | None:
    knode = pair.child_by_field_name('key')

    if knode is None:
        return None

    if knode.type == 'string':
        kc = knode.child_by_field_name('string_content')

        if kc:
            return read(kc, src)

        raw = read(knode, src)

        return raw.strip('"\'')

    return read(knode, src)


def val(pair):
    return pair.child_by_field_name('value')


def extract(path: Path) -> dict:
    try:
        import tree_sitter_json as tsjson
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree-sitter-json not installed'}

    try:
        with path.open('rb') as fh:
            src = fh.read(LIMIT + 1)

        if len(src) > LIMIT:
            return {'nodes': [], 'edges': [], 'error': 'json file too large'}

        language = Language(tsjson.language())
        parser = Parser(language)
        tree = parser.parse(src)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    sname = stem(path)
    loc = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def node(nid: str, label: str, line: int, ftype: str = 'code') -> None:
        if nid and nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': ftype, 'source_file': loc, 'source_location': f'L{line}'})

    def edge(src_nid: str, dst_nid: str, rel: str, line: int, ctx: str | None = None) -> None:
        if not src_nid or not dst_nid or src_nid == dst_nid:
            return

        item = {'source': src_nid, 'target': dst_nid, 'relation': rel, 'source_file': loc, 'source_location': f'L{line}'}

        if ctx:
            item['context'] = ctx

        edges.append(item)

    fnid = make(loc)
    node(fnid, path.name, 1)

    def walk(obj, parent_nid: str, parent_key: str | None, depth: int, count: list) -> None:
        if depth > 6:
            return

        for child in obj.children:
            if child.type != 'pair':
                continue

            if count[0] >= 500:
                return

            count[0] += 1
            ktext = key(child, src)

            if not ktext:
                continue

            if not normalize(ktext):
                continue

            knid = make(sname, *([parent_key] if parent_key else []) + [ktext])

            if not knid:
                continue

            line = child.start_point[0] + 1
            node(knid, ktext, line)
            edge(parent_nid, knid, 'contains', line)
            vnode = val(child)

            if vnode is None:
                continue

            if vnode.type == 'object':
                walk(vnode, knid, ktext, depth + 1, count)
            elif vnode.type == 'array' and ktext == 'extends':
                for item in vnode.children:
                    if item.type == 'string':
                        kc = item.child_by_field_name('string_content')
                        ref = read(kc, src) if kc else read(item, src).strip('"\'')

                        if ref:
                            rnid = make('ref', ref)

                            if rnid:
                                node(rnid, ref, line, ftype='concept')
                                edge(knid, rnid, 'extends', line, ctx='import')
            elif vnode.type == 'string':
                kc = vnode.child_by_field_name('string_content')
                vtext = read(kc, src) if kc else read(vnode, src).strip('"\'')

                if ktext == 'extends' and vtext:
                    rnid = make('ref', vtext)

                    if rnid:
                        node(rnid, vtext, line, ftype='concept')
                        edge(fnid, rnid, 'extends', line, ctx='import')
                elif ktext == '$ref' and vtext:
                    rnid = make('ref', vtext)

                    if rnid:
                        edge(parent_nid, rnid, 'references', line)
                elif parent_key in DEPKEYS and vtext:
                    rnid = make('ref', ktext)

                    if rnid:
                        node(rnid, ktext, line, ftype='concept')
                        edge(fnid, rnid, 'imports', line, ctx='import')

    doc = root

    if doc.type == 'document' and doc.child_count > 0:
        doc = doc.children[0]

    if doc.type == 'object':
        if not isconfig(path, doc, src):
            return {'nodes': [], 'edges': [], 'skipped': 'data json'}

        walk(doc, fnid, None, 0, [0])
    else:
        return {'nodes': [], 'edges': [], 'skipped': 'non-object root'}

    return {'nodes': nodes, 'edges': edges}