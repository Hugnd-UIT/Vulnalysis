from __future__ import annotations

from pathlib import Path
import re

from ..src.base import make, stem, text
from graphify.security import metadata


COMPONENT_RE = re.compile(r'<([A-Z][A-Za-z0-9]+)[\s/>]')
CODE_RE = re.compile(r'@(?:code|functions)\s*\{', re.MULTILINE)
METHOD_RE = re.compile(r'(?:public|private|protected|internal|static|async|override|virtual|abstract)\s+[\w<>\[\],\s]+\s+(\w+)\s*\(')
HTML_TAGS = frozenset({'DOCTYPE', 'Html', 'Head', 'Body', 'Div', 'Span', 'Table', 'Form', 'Input', 'Button', 'Select', 'Option', 'Label', 'Textarea', 'Script', 'Style', 'Link', 'Meta', 'Title', 'Header', 'Footer', 'Nav', 'Main', 'Section', 'Article', 'Aside'})


def extract(path: Path) -> dict:
    try:
        src = text(path)
    except OSError:
        return {'nodes': [], 'edges': [], 'error': f'cannot read {path}'}

    fnid = make(str(path))
    loc = str(path)
    nodes: list[dict] = [{'id': fnid, 'label': path.name, 'file_type': 'code', 'source_file': loc, 'source_location': None}]
    edges: list[dict] = []
    seen: set[str] = {fnid}

    def ref(target: str, relation: str, line: int) -> None:
        tnid = make(target)

        if not tnid:
            return

        if tnid not in seen:
            seen.add(tnid)
            nodes.append({'id': tnid, 'label': target, 'file_type': 'code', 'source_file': loc, 'source_location': f'L{line}'})

        edges.append({'source': fnid, 'target': tnid, 'relation': relation, 'source_file': loc, 'source_location': f'L{line}'})

    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r'@using\s+(.+)', line)

        if m:
            raw = m.group(1).strip().rstrip(';')
            kind = 'namespace'
            alias = None
            target = raw

            if raw.startswith('static '):
                kind = 'static'
                target = raw[len('static '):].strip()
            elif '=' in raw:
                lhs, rhs = raw.split('=', 1)
                kind = 'alias'
                alias = lhs.strip()
                target = rhs.strip()

            if target:
                tnid = make(target)

                if tnid and tnid not in seen:
                    seen.add(tnid)
                    nodes.append({'id': tnid, 'label': target, 'file_type': 'code', 'source_file': loc, 'source_location': f'L{i}'})

                md = {'using_kind': kind, 'target_fqn': target, 'scope_kind': 'file', 'scope_id': None}

                if alias:
                    md['alias'] = alias

                edges.append({'source': fnid, 'target': tnid, 'relation': 'imports', 'context': 'import', 'source_file': loc, 'source_location': f'L{i}', 'metadata': metadata({k: v for k, v in md.items() if v is not None})})
            continue

        m = re.match(r'@inject\s+([\w.<>\[\]]+)\s+(\w+)', line)

        if m:
            raw = m.group(1).strip()
            base = raw.split('<', 1)[0].strip()
            qual = '.' in base

            if qual:
                prefix, _, token = base.rpartition('.')
            else:
                prefix = ''
                token = base

            if token:
                tnid = make(token)

                if tnid and tnid not in seen:
                    seen.add(tnid)
                    nodes.append({'id': tnid, 'label': token, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': loc})

                md = {'ref_token': token}

                if qual:
                    md['qualified'] = True

                    if prefix:
                        md['ref_qualifier'] = prefix

                edges.append({'source': fnid, 'target': tnid, 'relation': 'references', 'context': 'field', 'source_file': loc, 'source_location': f'L{i}', 'metadata': metadata(md)})
            continue

        m = re.match(r'@inherits\s+([\w.<>\[\]]+)', line)

        if m:
            ref(m.group(1), 'inherits', i)
            continue

        m = re.match(r'@model\s+([\w.<>\[\]]+)', line)

        if m:
            ref(m.group(1), 'references', i)
            continue

        m = re.match(r'@page\s+"([^"]+)"', line)

        if m:
            route = m.group(1)
            rnid = make('route', route)

            if rnid and rnid not in seen:
                seen.add(rnid)
                nodes.append({'id': rnid, 'label': f'route:{route}', 'file_type': 'concept', 'source_file': loc, 'source_location': f'L{i}'})
                edges.append({'source': fnid, 'target': rnid, 'relation': 'references', 'source_file': loc})
            continue

    for m in COMPONENT_RE.finditer(src):
        comp = m.group(1)

        if comp in HTML_TAGS:
            continue

        num = src[:m.start()].count('\n') + 1
        ref(comp, 'calls', num)

    for m in CODE_RE.finditer(src):
        start = m.end()
        depth = 1
        pos = start

        while pos < len(src) and depth > 0:
            if src[pos] == '{':
                depth += 1
            elif src[pos] == '}':
                depth -= 1

            pos += 1

        body = src[start:pos - 1] if depth == 0 else ''

        for mm in METHOD_RE.finditer(body):
            name = mm.group(1)
            mpos = start + mm.start()
            mline = src[:mpos].count('\n') + 1
            mnid = make(stem(path), name)

            if mnid and mnid not in seen:
                seen.add(mnid)
                nodes.append({'id': mnid, 'label': name, 'file_type': 'code', 'source_file': loc, 'source_location': f'L{mline}'})
                edges.append({'source': fnid, 'target': mnid, 'relation': 'contains', 'source_file': loc})

    return {'nodes': nodes, 'edges': edges}