from __future__ import annotations

import re
from pathlib import Path

from ..src.base import stem as filestem, make, text


def extract(path: Path) -> dict:
    try:
        source = text(path)
    except OSError:
        return {'nodes': [], 'edges': []}

    target = str(path)
    stem = filestem(path)
    root = make(target)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def node(nid: str, label: str, line: int) -> None:
        if nid not in seen:
            seen.add(nid)
            nodes.append({
                'id': nid,
                'label': label,
                'file_type': 'code',
                'source_file': target,
                'source_location': f'L{line}',
            })

    def edge(src: str, dst: str, relation: str, line: int) -> None:
        edges.append({
            'source': src,
            'target': dst,
            'relation': relation,
            'source_file': target,
            'source_location': f'L{line}',
        })

    node(root, path.name, 1)

    lines = source.splitlines()

    access = '(?:public|private|protected|global|webService)?'
    sharing = '(?:\\s+(?:with|without|inherited)\\s+sharing)?'
    mod = '(?:\\s+(?:abstract|virtual|override|static|final|transient|testMethod))?'
    ann = '(?:\\s*@\\w+(?:\\s*\\([^)]*\\))?\\s*)*'

    cls = re.compile(
        f'^{ann}\\s*{access}{sharing}{mod}\\s*class\\s+(\\w+)(?:\\s+extends\\s+(\\w+))?(?:\\s+implements\\s+([\\w,\\s]+))?\\s*\\{{?',
        re.IGNORECASE,
    )

    iface = re.compile(
        f'^{ann}\\s*{access}{sharing}{mod}\\s*interface\\s+(\\w+)(?:\\s+extends\\s+([\\w,\\s]+))?\\s*\\{{?',
        re.IGNORECASE,
    )

    enum = re.compile(
        f'^{ann}\\s*{access}{sharing}{mod}\\s*enum\\s+(\\w+)\\s*\\{{?',
        re.IGNORECASE,
    )

    trigger = re.compile(
        '^\\s*trigger\\s+(\\w+)\\s+on\\s+(\\w+)\\s*\\(',
        re.IGNORECASE,
    )

    method = re.compile(
        f'^{ann}\\s*{access}{mod}\\s*(?:static\\s+)?[\\w<>\\[\\]]+\\s+(\\w+)\\s*\\([^)]*\\)\\s*(?:throws\\s+\\w+\\s*)?\\{{?',
        re.IGNORECASE,
    )

    at = re.compile('@(\\w+)', re.IGNORECASE)

    soql = re.compile('\\[\\s*SELECT\\b[^\\]]+FROM\\s+(\\w+)', re.IGNORECASE)

    dml = re.compile('\\b(insert|update|delete|upsert|merge|undelete)\\s+\\w', re.IGNORECASE)

    flow = frozenset({
        'if', 'else', 'for', 'while', 'do', 'switch', 'try', 'catch', 'finally',
        'return', 'throw', 'new', 'void', 'null', 'true', 'false', 'this',
        'super', 'class', 'interface', 'enum', 'trigger', 'on',
    })

    owner: str | None = None
    current: str | None = None
    tags: list[str] = []
    raw: list[dict] = []

    for num, line in enumerate(lines, start=1):
        clean = line.strip()

        if clean.startswith('@'):
            for m in at.finditer(clean):
                tags.append(m.group(1).lower())
            continue

        if clean == '}':
            current = None

        tm = trigger.match(clean)

        if tm:
            name, sobject = (tm.group(1), tm.group(2))
            trig = make(stem, name)

            node(trig, name, num)
            edge(root, trig, 'contains', num)

            sob = make(sobject)

            if sob not in seen:
                node(sob, sobject, num)

            edge(trig, sob, 'uses', num)

            owner = trig
            current = None
            tags = []
            continue

        cm = cls.match(clean)

        if cm:
            name = cm.group(1)

            if name.lower() in flow:
                tags = []
                continue

            item = make(stem, name)
            node(item, name, num)
            edge(root, item, 'contains', num)

            if cm.group(2):
                base = cm.group(2).strip()
                parent = make(stem, base)

                if parent not in seen:
                    parent = make(base)

                if parent not in seen:
                    node(parent, base, num)

                edge(item, parent, 'extends', num)

            if cm.group(3):
                for contract in cm.group(3).split(','):
                    contract = contract.strip()

                    if contract:
                        parent = make(stem, contract)

                        if parent not in seen:
                            parent = make(contract)

                        if parent not in seen:
                            node(parent, contract, num)

                        edge(item, parent, 'implements', num)

            owner = item
            current = None
            tags = []
            continue

        im = iface.match(clean)

        if im:
            name = im.group(1)

            if name.lower() in flow:
                tags = []
                continue

            item = make(stem, name)
            node(item, name, num)
            edge(root if owner is None else owner, item, 'contains', num)

            if im.group(2):
                for base in im.group(2).split(','):
                    base = base.strip()

                    if base:
                        parent = make(stem, base)

                        if parent not in seen:
                            parent = make(base)

                        if parent not in seen:
                            node(parent, base, num)

                        edge(item, parent, 'extends', num)

            tags = []
            continue

        em = enum.match(clean)

        if em:
            name = em.group(1)

            if name.lower() in flow:
                tags = []
                continue

            item = make(stem, name)
            node(item, name, num)
            edge(root if owner is None else owner, item, 'contains', num)

            tags = []
            continue

        if owner is not None:
            mm = method.match(clean)

            if mm:
                name = mm.group(1)

                if name.lower() not in flow:
                    item = make(owner, name)
                    label = f'.{name}()'

                    node(item, label, num)
                    edge(owner, item, 'method', num)

                    if 'auraenabled' in tags or 'invocablemethod' in tags:
                        edge(root, item, 'contains', num)

                    current = item
                    tags = []
                    continue

        if current is not None:
            for m in re.finditer(r'\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(', clean):
                call_name = m.group(1)

                if call_name.lower() not in flow:
                    tgt = make(owner, call_name) if '.' not in call_name else make(call_name.replace('.', '_'))
                    edge(current, tgt, 'calls', num)
                    raw.append({'caller_nid': current, 'callee': call_name.split('.')[-1], 'source_file': target, 'source_location': f'L{num}'})

        tags = []

        for sm in soql.finditer(line):
            sobject = sm.group(1)
            sob = make(sobject)

            if sob not in seen:
                node(sob, sobject, num)

            src = owner or root
            edge(src, sob, 'uses', num)

        for dm in dml.finditer(line):
            op = dm.group(1).lower()
            item = make(f'dml_{op}')

            if item not in seen:
                node(item, op, num)

            src = owner or root
            edge(src, item, 'uses', num)

    return {'nodes': nodes, 'edges': edges, 'raw_calls': raw}