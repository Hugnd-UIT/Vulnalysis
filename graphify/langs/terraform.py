from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import re

from ..src.base import bytes as read_bytes, make
from graphify.security import CONTROL, MAX_ITEMS, MAX_VALUE


HEADS = frozenset({'count', 'each', 'self', 'path', 'terraform'})
SENSITIVE = re.compile(r'(password|passwd|secret|token|api[-_]?key|access[-_]?key|private[-_]?key|credential|client[-_]?secret|connection[-_]?string|sas[-_]?token|auth|passphrase)', re.IGNORECASE)
REDACTED = '[redacted]'
PAIRS = frozenset({'value', 'valuefrom', 'value_from'})


def redact(key: str, val: object) -> object:
    if SENSITIVE.search(key):
        return REDACTED

    if isinstance(val, dict):
        res = {k: redact(str(k), v) for k, v in val.items()}

        if any((str(k).lower() == 'name' and isinstance(v, str) and SENSITIVE.search(v) for k, v in val.items())):
            for k in res:
                if str(k).lower() in PAIRS:
                    res[k] = REDACTED

        return res

    if isinstance(val, (list, tuple)):
        return [redact(key, item) for item in val]

    return val


def scope(folder: str) -> str:
    digest = hashlib.sha256(folder.encode('utf-8')).hexdigest()[:16]

    return make('terraform', folder, digest)


def prepare(result: dict, path: Path, root: Path) -> None:
    folder = path.parent.resolve()

    try:
        rel = folder.relative_to(root).as_posix()
    except ValueError:
        return

    old = scope(folder.as_posix())
    new = scope(rel)

    for node in result.get('nodes', []):
        nid = node['id']

        if nid.startswith(old + '_'):
            node['id'] = new + nid[len(old):]

        node['_terraform_directory'] = rel

    for edge in result.get('edges', []):
        for key in ('source', 'target'):
            nid = edge[key]

            if nid.startswith(old + '_'):
                edge[key] = new + nid[len(old):]

    for node in result.get('nodes', []):
        src = node.get('module_source', '')

        if not src.startswith(('./', '../')):
            continue

        try:
            target = (folder / src).resolve().relative_to(root)
        except (ValueError, OSError, RuntimeError):
            continue

        node['_terraform_source_directory'] = target.as_posix()


def resolve(items: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    files: dict[str, list[dict]] = {}
    anchors = {}

    for node in nodes:
        folder = node.get('_terraform_directory')

        if folder is None:
            continue

        if node.get('type') == 'terraform_file' and Path(node['source_file']).suffix == '.tf':
            files.setdefault(folder, []).append(node)
        elif node.get('type') == 'module' and node.get('language') == 'terraform':
            anchors[folder] = node['id']

    fresh_files = {node['id'] for result in items for node in result.get('nodes', []) if node.get('type') == 'terraform_file'}
    known = {n['id'] for n in nodes}

    for folder, members in sorted(files.items()):
        anchor = anchors.setdefault(folder, make(scope(folder), 'directory'))

        if anchor not in known:
            owner = min(members, key=lambda n: n['source_file'])
            nodes.append({'id': anchor, 'label': f'Terraform module: {folder}', 'type': 'module', 'language': 'terraform', 'file_type': 'code', 'source_file': owner['source_file'], 'source_location': None, '_terraform_directory': folder})
            known.add(anchor)

        for member in members:
            if member['id'] in fresh_files:
                edges.append({'source': anchor, 'target': member['id'], 'relation': 'contains', 'source_file': member['source_file'], 'source_location': None})

    fresh_calls = {node['id'] for result in items for node in result.get('nodes', []) if 'module_source' in node}

    for node in nodes:
        if node['id'] not in fresh_calls:
            continue

        target = anchors.get(node.get('_terraform_source_directory'))

        if target is not None:
            edges.append({'source': node['id'], 'target': target, 'relation': 'module_source', 'source_file': node['source_file'], 'source_location': node['module_source_location']})


def refresh(changed: list[Path], corpus: list[Path], invalidated: Sequence[Path] = ()) -> list[Path]:
    if not any((p.suffix == '.tf' or p.name in ('.gitignore', '.graphifyignore') for p in [*changed, *invalidated])):
        return changed

    return list(dict.fromkeys([*changed, *(p for p in corpus if p.suffix == '.tf')]))


def extract(path: Path) -> dict:
    try:
        import tree_sitter_hcl as ts
        from tree_sitter import Language, Parser
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': 'tree_sitter_hcl not installed. Run: pip install tree-sitter-hcl'}

    try:
        language = Language(ts.language())
        parser = Parser(language)
        source = read_bytes(path)
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': str(exc)}

    src = str(path.parent.resolve() / path.name)
    file_id = make(src)
    dir_scope = scope(path.parent.resolve().as_posix())
    nodes: list[dict] = [{'id': file_id, 'label': path.name, 'file_type': 'code', 'source_file': src, 'source_location': None, 'type': 'terraform_file'}]
    edges: list[dict] = []
    seen: set[str] = {file_id}
    lookup = {file_id: nodes[0]}
    pairs: set[tuple[str, str, str]] = set()

    def text(raw) -> str:
        return source[raw.start_byte:raw.end_byte].decode('utf-8', errors='replace')

    def label(raw) -> str:
        return text(raw).strip().strip('"')

    def node(addr: str, lbl: str, line: int) -> str:
        nid = make(dir_scope, addr)

        if nid not in seen:
            seen.add(nid)
            nodes.append({'id': nid, 'label': lbl, 'file_type': 'code', 'source_file': src, 'source_location': f'L{line}'})
            lookup[nid] = nodes[-1]
            edges.append({'source': file_id, 'target': nid, 'relation': 'contains', 'source_file': src, 'source_location': f'L{line}'})

        return nid

    def edge(source_id: str, addr: str, relation: str, line: int) -> None:
        target_id = make(dir_scope, addr)

        if source_id == target_id:
            return

        key = (source_id, target_id, relation)

        if key in pairs:
            return

        pairs.add(key)
        edges.append({'source': source_id, 'target': target_id, 'relation': relation, 'source_file': src, 'source_location': f'L{line}'})

    def blocks(raw) -> tuple:
        btype = None
        labels: list[str] = []

        for child in raw.children:
            if child.type in ('block_start', 'body', 'block_end'):
                break

            if child.type == 'identifier' and btype is None:
                btype = text(child)
            elif child.type in ('string_lit', 'identifier'):
                labels.append(label(child))

        return (btype, labels)

    def address(expr):
        head = text(expr)
        parent = expr.parent
        attrs: list[str] = []

        if parent is not None:
            seen_self = False

            for child in parent.children:
                if child.id == expr.id:
                    seen_self = True
                    continue

                if seen_self and child.type == 'get_attr':
                    name = None

                    for gc in child.children:
                        if gc.type == 'identifier':
                            name = text(gc)
                            break

                    if name is None:
                        break

                    attrs.append(name)
                elif seen_self and child.type not in ('get_attr',):
                    break

        if head in HEADS or not head:
            return None

        if head == 'var':
            return f'var.{attrs[0]}' if attrs else None

        if head == 'local':
            return f'local.{attrs[0]}' if attrs else None

        if head == 'module':
            return f'module.{attrs[0]}' if attrs else None

        if head == 'data':
            return f'data.{attrs[0]}.{attrs[1]}' if len(attrs) >= 2 else None

        return f'{head}.{attrs[0]}' if attrs else None

    def refs(raw, owner_id: str, relation: str) -> None:
        rel = relation

        if raw.type == 'attribute':
            key_node = raw.child_by_field_name('key') or (raw.children[0] if raw.children else None)

            if key_node is not None and text(key_node) == 'depends_on':
                rel = 'depends_on'

        if raw.type == 'variable_expr':
            addr = address(raw)

            if addr:
                edge(owner_id, addr, rel, raw.start_point[0] + 1)

        for child in raw.children:
            if child.is_named:
                refs(child, owner_id, rel)

    def clean(val: str) -> str:
        res = CONTROL.sub('', str(val))

        if len(res) > MAX_VALUE:
            res = res[:MAX_VALUE]

        return res

    def parse(raw, depth: int = 0):
        if depth > 5:
            return clean(text(raw).strip())

        curr = raw

        while curr.type in ('expression', 'literal_value', 'collection_value') and len(curr.named_children) == 1:
            curr = curr.named_children[0]

        kind = curr.type
        val_str = text(curr).strip()

        if kind == 'bool_lit':
            return val_str == 'true'

        elif kind == 'numeric_lit':
            try:
                return int(val_str)
            except ValueError:
                try:
                    return float(val_str)
                except ValueError:
                    return clean(val_str)

        elif kind == 'null_lit':
            return None

        elif kind == 'string_lit':
            if val_str.startswith('"') and val_str.endswith('"'):
                try:
                    return clean(json.loads(val_str))
                except Exception:
                    return clean(val_str[1:-1])

            return clean(val_str)

        elif kind == 'tuple':
            items = []

            for child in curr.named_children:
                if child.type not in ('tuple_start', 'tuple_end'):
                    items.append(parse(child, depth + 1))

                    if len(items) >= MAX_ITEMS:
                        break

            return items

        elif kind == 'object':
            obj = {}

            for child in curr.children:
                if child.type == 'object_elem':
                    k_node = child.child_by_field_name('key') or (child.named_children[0] if child.named_children else None)
                    v_node = child.child_by_field_name('val') or (child.named_children[-1] if len(child.named_children) > 1 else None)

                    if k_node and v_node:
                        k_val = parse(k_node, depth + 1)
                        k_str = str(k_val) if k_val is not None else text(k_node).strip().strip('"')
                        k_str = clean(k_str)
                        obj[k_str] = parse(v_node, depth + 1)

                        if len(obj) >= MAX_ITEMS:
                            break

            return obj

        else:
            return clean(val_str)

    def attributes(blk) -> dict:
        attrs = {}

        for child in blk.children:
            if child.type != 'attribute':
                continue

            k_node = child.child_by_field_name('key') or (child.children[0] if child.children else None)

            if k_node is None:
                continue

            key = clean(text(k_node).strip())

            if not key:
                continue

            v_node = child.named_children[-1] if child.named_children else None

            if v_node is None:
                continue

            attrs[key] = redact(key, parse(v_node))

            if len(attrs) >= MAX_ITEMS:
                break

        return attrs

    def body(block):
        for child in block.children:
            if child.type == 'body':
                return child

        return None

    root_body = next((c for c in root.children if c.type == 'body'), root)

    for block in root_body.children:
        if block.type != 'block':
            continue

        btype, label_list = blocks(block)
        line = block.start_point[0] + 1
        blk_body = body(block)

        if btype == 'resource' and len(label_list) >= 2:
            owner = node(f'{label_list[0]}.{label_list[1]}', f'{label_list[0]}.{label_list[1]}', line)
        elif btype == 'data' and len(label_list) >= 2:
            owner = node(f'data.{label_list[0]}.{label_list[1]}', f'data.{label_list[0]}.{label_list[1]}', line)
        elif btype == 'module' and label_list:
            owner = node(f'module.{label_list[0]}', f'module.{label_list[0]}', line)

            if blk_body is not None and path.suffix == '.tf':
                for attr in blk_body.named_children:
                    if attr.type != 'attribute' or text(attr.named_children[0]) != 'source':
                        continue

                    value = attr.named_children[-1]

                    while value.type in ('expression', 'literal_value') and len(value.named_children) == 1:
                        value = value.named_children[0]

                    if value.type != 'string_lit' or value.has_error:
                        continue

                    try:
                        module_source = json.loads(text(value))
                    except (ValueError, TypeError):
                        continue

                    if not isinstance(module_source, str):
                        continue

                    lookup[owner]['module_source'] = module_source.replace('$${', '${').replace('%%{', '%{')
                    lookup[owner]['module_source_location'] = f'L{attr.start_point[0] + 1}'

        elif btype == 'variable' and label_list:
            owner = node(f'var.{label_list[0]}', f'var.{label_list[0]}', line)
        elif btype == 'output' and label_list:
            owner = node(f'output.{label_list[0]}', f'output.{label_list[0]}', line)
        elif btype == 'provider' and label_list:
            owner = node(f'provider.{label_list[0]}', f'provider.{label_list[0]}', line)
        elif btype == 'locals' and blk_body is not None:
            for attr in blk_body.children:
                if attr.type != 'attribute':
                    continue

                k_node = attr.children[0] if attr.children else None

                if k_node is None:
                    continue

                key = text(k_node)
                lnid = node(f'local.{key}', f'local.{key}', attr.start_point[0] + 1)
                refs(attr, lnid, 'references')

            continue
        else:
            continue

        if blk_body is not None:
            attrs = attributes(blk_body)

            if btype in ('variable', 'output') and (SENSITIVE.search(label_list[0]) or attrs.get('sensitive') in (True, 'true')):
                for secret_key in ('default', 'value'):
                    if secret_key in attrs:
                        attrs[secret_key] = REDACTED

            if attrs:
                lookup[owner]['attributes'] = attrs

            refs(blk_body, owner, 'references')

    return {'nodes': nodes, 'edges': edges}