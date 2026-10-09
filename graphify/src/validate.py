from __future__ import annotations


TYPES = {'code', 'document', 'paper', 'image', 'concept'}

FIELDS_NODE = {'id', 'label', 'file_type', 'source_file'}

FIELDS_EDGE = {'source', 'target', 'relation', 'source_file'}


def validate(data: dict) -> list[str]:
    if not isinstance(data, dict):
        return ['Extraction must be a JSON object']

    errors: list[str] = []
    ids: set = set()

    if 'nodes' not in data:
        errors.append("Missing required key 'nodes'")

    elif not isinstance(data['nodes'], list):
        errors.append("'nodes' must be a list")

    else:
        for i, node in enumerate(data['nodes']):
            if not isinstance(node, dict):
                errors.append(f'Node {i} must be an object')
                continue

            for field in FIELDS_NODE:
                if field not in node:
                    errors.append(f"Node {i} (id={node.get('id', '?')!r}) missing required field '{field}'")

            if 'id' in node:
                try:
                    hash(node['id'])
                except TypeError:
                    errors.append(f"Node {i} has non-hashable id {node['id']!r} - id must be a string")
                else:
                    ids.add(node['id'])

            if 'file_type' in node and node['file_type'] not in TYPES:
                errors.append(f"Node {i} (id={node.get('id', '?')!r}) has invalid file_type '{node['file_type']}' - must be one of {sorted(TYPES)}")

    edges = data.get('edges') if 'edges' in data else data.get('links')

    if edges is None:
        errors.append("Missing required key 'edges'")

    elif not isinstance(edges, list):
        errors.append("'edges' must be a list")

    else:
        for i, edge in enumerate(edges):
            if not isinstance(edge, dict):
                errors.append(f'Edge {i} must be an object')
                continue

            for field in FIELDS_EDGE:
                if field not in edge:
                    errors.append(f"Edge {i} missing required field '{field}'")

            for point in ('source', 'target'):
                if point not in edge:
                    continue

                val = edge[point]

                try:
                    unmatched = bool(ids) and val not in ids
                except TypeError:
                    errors.append(f'Edge {i} {point} {val!r} is non-hashable - must be a string')
                    continue

                if unmatched:
                    errors.append(f"Edge {i} {point} '{val}' does not match any node id")

    return errors


def check(data: dict) -> None:
    errors = validate(data)

    if errors:
        msg = f'Extraction JSON has {len(errors)} error(s):\n' + '\n'.join((f'  • {e}' for e in errors))
        raise ValueError(msg)