from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

from graphify.ids import make


NAMES: dict[str, str] = {
    'apm.yml': 'apm',
    'apm.yaml': 'apm',
    'pyproject.toml': 'python',
    'cargo.toml': 'cargo',
    'go.mod': 'go',
    'pom.xml': 'maven',
}

LIMIT = 2000000


def toml():
    try:
        import tomllib as mod

        return mod
    except ImportError:
        try:
            import tomli as mod

            return mod
        except ImportError as exc:
            raise ImportError('tomli required') from exc


def match(path: Path) -> bool:
    return path.name.lower() in NAMES


def pkg(name: str) -> str:
    return make('pkg', name)


def coerce(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]

    if isinstance(value, list):
        out: list[str] = []

        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item:
                out.append(str(next(iter(item))))

        return out

    return []


def apm(text: str) -> dict | None:
    try:
        import yaml
    except ImportError:
        return fallback(text)

    data = yaml.safe_load(text)

    if not isinstance(data, dict):
        return None

    return {'name': data.get('name'), 'version': data.get('version'), 'deps': coerce(data.get('dependencies'))}


def fallback(text: str) -> dict | None:
    name = None
    version = None
    deps: list[str] = []
    active = False

    for line in text.splitlines():
        if not active:
            m = re.match('^name:\\s*["\\\']?([^"\\\'\\s#]+)', line)

            if m:
                name = m.group(1)
                continue

            m = re.match('^version:\\s*["\\\']?([^"\\\'\\s#]+)', line)

            if m:
                version = m.group(1)
                continue

        if re.match('^dependencies:\\s*$', line):
            active = True
            continue

        if active:
            dm = re.match('^\\s*-\\s*["\\\']?([^"\\\'\\s#:]+)', line) or re.match('^\\s{2,}([A-Za-z0-9._/@-]+)\\s*:', line)

            if dm:
                deps.append(dm.group(1))
            elif re.match('^\\S', line):
                active = False

    if name:
        return {'name': name, 'version': version, 'deps': deps}

    return None


def pep(spec: str) -> str:
    return re.split('[\\s<>=!~;\\[\\(]', spec.strip(), maxsplit=1)[0]


def pyproject(text: str) -> dict | None:
    mod = toml()
    data = mod.loads(text)
    proj = data.get('project', {}) if isinstance(data.get('project'), dict) else {}
    poetry = (data.get('tool', {}) or {}).get('poetry', {}) if isinstance(data.get('tool'), dict) else {}
    name = proj.get('name') or (poetry.get('name') if isinstance(poetry, dict) else None)

    if not name:
        return None

    deps: list[str] = [pep(s) for s in proj.get('dependencies') or [] if isinstance(s, str)]

    if isinstance(poetry, dict):
        for dep in poetry.get('dependencies') or {}:
            if str(dep).lower() != 'python':
                deps.append(str(dep))

    return {'name': name, 'version': proj.get('version') or (poetry.get('version') if isinstance(poetry, dict) else None), 'deps': deps}


def cargo(text: str) -> dict | None:
    mod = toml()
    data = mod.loads(text)
    pack = data.get('package', {}) if isinstance(data.get('package'), dict) else {}
    name = pack.get('name')

    if not isinstance(name, str) or not name:
        if isinstance(data.get('workspace'), dict):
            return {'name': None, 'skipped': 'virtual workspace root Cargo.toml'}

        return None

    version = pack.get('version')

    if not isinstance(version, str):
        version = None

    deps = coerce(data.get('dependencies'))
    targets = data.get('target')

    if isinstance(targets, dict):
        for cfg in targets.values():
            if isinstance(cfg, dict):
                deps += coerce(cfg.get('dependencies'))

    return {'name': name, 'version': version, 'deps': deps}


def gomod(text: str) -> dict | None:
    name = None
    deps: list[str] = []
    active = False

    for line in text.splitlines():
        item = line.strip()

        if name is None:
            m = re.match('^module\\s+(\\S+)', item)

            if m:
                name = m.group(1)
                continue

        if re.match('^require\\s*\\(', item):
            active = True
            continue

        if active:
            if item.startswith(')'):
                active = False
                continue

            dm = re.match('^(\\S+)\\s+v\\S+', item)

            if dm:
                deps.append(dm.group(1))
        else:
            dm = re.match('^require\\s+(\\S+)\\s+v\\S+', item)

            if dm:
                deps.append(dm.group(1))

    if name:
        return {'name': name, 'version': None, 'deps': deps}

    return None


def pom(text: str) -> dict | None:
    body = re.sub('\\sxmlns="[^"]*"', '', text, count=1)
    tree = ET.fromstring(body)
    aid = tree.findtext('artifactId')
    gid = tree.findtext('groupId') or tree.findtext('parent/groupId')
    version = tree.findtext('version') or tree.findtext('parent/version')

    if not aid:
        return None

    props: dict[str, str] = {}

    for prop in tree.findall('properties/*'):
        if isinstance(prop.tag, str) and prop.text:
            props[prop.tag] = prop.text.strip()

    for key, value in (('project.groupId', gid), ('project.artifactId', aid), ('project.version', version), ('project.parent.groupId', tree.findtext('parent/groupId')), ('project.parent.version', tree.findtext('parent/version'))):
        if value:
            props.setdefault(key, value.strip())

    def resolve(val: str | None) -> str | None:
        if not val:
            return val

        return re.sub('\\$\\{([^}]+)\\}', lambda m: props.get(m.group(1), m.group(0)), val.strip())

    gid = resolve(gid)
    version = resolve(version)
    name = f'{gid}:{aid}' if gid else aid
    deps: list[str] = []

    for dep in tree.findall('.//dependencies/dependency'):
        da = resolve(dep.findtext('artifactId'))
        dg = resolve(dep.findtext('groupId'))

        if da:
            deps.append(f'{dg}:{da}' if dg else da)

    return {'name': name, 'version': version, 'deps': deps}


PARSERS = {'apm': apm, 'python': pyproject, 'cargo': cargo, 'go': gomod, 'maven': pom}


def extract(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > LIMIT:
            return {'nodes': [], 'edges': [], 'error': 'manifest too large to index'}

        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        return {'nodes': [], 'edges': [], 'error': f'manifest read error: {exc}'}

    eco = NAMES[path.name.lower()]

    try:
        info = PARSERS[eco](text)
    except Exception as exc:
        return {'nodes': [], 'edges': [], 'error': f'manifest parse error: {exc}'}

    if isinstance(info, dict) and info.get('skipped'):
        return {'nodes': [], 'edges': [], 'skipped': info['skipped']}

    if not info or not info.get('name'):
        return {'nodes': [], 'edges': []}

    name = info['name']
    loc = str(path)
    nid = pkg(name)
    node: dict[str, Any] = {'id': nid, 'label': name, 'file_type': 'code', 'type': 'package', 'ecosystem': eco, 'source_file': loc, 'source_location': 'L1'}

    if info.get('version'):
        node['version'] = info['version']

    nodes: list[dict] = [node]
    edges: list[dict] = []
    seen: set[str] = set()

    for dep in info.get('deps', []):
        if not dep:
            continue

        did = pkg(dep)

        if did == nid or did in seen:
            continue

        seen.add(did)
        edges.append({'source': nid, 'target': did, 'relation': 'depends_on', 'context': 'dependency', 'source_file': loc, 'source_location': 'L1'})

    return {'nodes': nodes, 'edges': edges}