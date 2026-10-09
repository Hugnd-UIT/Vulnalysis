from __future__ import annotations

from pathlib import Path
import re

from ..src.base import make, text


PROJECT_RE = re.compile(r'Project\("[^"]*"\)\s*=\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]*)"')
DEP_RE = re.compile(r'\{([0-9a-fA-F-]+)\}\s*=\s*\{([0-9a-fA-F-]+)\}')
LINE_RE = re.compile(r'Project\("[^"]*"\)\s*=\s*"[^"]+"\s*,\s*"[^"]+"\s*,\s*"\{([^}]+)\}"')


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
    guids: dict[str, str] = {}

    for m in PROJECT_RE.finditer(src):
        pname = m.group(1)
        ppath = m.group(2).replace('\\', '/')
        pguid = m.group(3).strip('{}')

        if ppath == pname:
            target = pname
        else:
            try:
                target = str((path.parent / ppath).resolve())
            except Exception:
                target = ppath

        pnid = make(target)

        if pnid and pnid not in seen:
            seen.add(pnid)
            nodes.append({'id': pnid, 'label': pname, 'file_type': 'code', 'source_file': target, 'source_location': None})
            edges.append({'source': fnid, 'target': pnid, 'relation': 'contains', 'source_file': loc})

        if pguid:
            guids[pguid.lower()] = pnid

    active = False
    cur: str | None = None

    for line in src.splitlines():
        pm = LINE_RE.search(line)

        if pm:
            cur = pm.group(1).lower()
            continue

        if line.strip() == 'EndProject':
            cur = None
            continue

        if 'ProjectSection(ProjectDependencies)' in line:
            active = True
            continue

        if active and 'EndProjectSection' in line:
            active = False
            continue

        if active and cur:
            dm = DEP_RE.search(line)

            if dm:
                dst_guid = dm.group(1).lower()
                src_nid = guids.get(cur)
                dst_nid = guids.get(dst_guid)

                if src_nid and dst_nid and src_nid != dst_nid:
                    edges.append({'source': src_nid, 'target': dst_nid, 'relation': 'imports', 'source_file': loc})

    return {'nodes': nodes, 'edges': edges}