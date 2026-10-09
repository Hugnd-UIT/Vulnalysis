from __future__ import annotations
from typing import Any, Callable
from pathlib import Path
from .models import LanguageConfig, _JS_CACHE_BYPASS_SUFFIXES, _NamespaceExportFact, _StarExportFact, _SymbolAliasFact, _SymbolDeclarationFact, _SymbolExportFact, _SymbolImportFact, _SymbolResolutionFacts, _SymbolUseFact, _WORKSPACE_PACKAGE_CACHE
from . import base
from .base import GLOBALS, stem, make, read
import functools
import hashlib
import json
import os
import re
import sys
TSALIASCACHE: dict[str, dict[str, list[str]]] = {}
TSBASEURLCACHE: 'dict[str, Path | None]' = {}
WORKSPACEMANIFESTS = ('pnpm-workspace.yaml', 'package.json')
PACKAGEIMPORTSCACHE: 'dict[str, tuple[Path, dict] | None]' = {}
JSRESOLVEEXTS = ('.ts', '.tsx', '.mts', '.cts', '.svelte', '.js', '.jsx', '.mjs', '.cjs')
JSINDEXFILES = ('index.ts', 'index.tsx', 'index.svelte', 'index.js', 'index.jsx', 'index.mjs')


def jspath(candidate: Path) -> Path:
    candidate = Path(os.path.normpath(candidate))

    if candidate.is_file():
        return candidate

    if candidate.suffix == '.js':
        ts_candidate = candidate.with_suffix('.ts')

        if ts_candidate.is_file():
            return ts_candidate
    elif candidate.suffix == '.jsx':
        tsx_candidate = candidate.with_suffix('.tsx')

        if tsx_candidate.is_file():
            return tsx_candidate

    for ext in JSRESOLVEEXTS:
        with_ext = candidate.parent / f'{candidate.name}{ext}'

        if with_ext.is_file():
            return with_ext

    if candidate.is_dir():
        for index_name in JSINDEXFILES:
            index_candidate = candidate / index_name

            if index_candidate.is_file():
                return index_candidate

    return candidate


def stripjsonc(text: str) -> str:
    pattern = re.compile('"(?:\\\\.|[^"\\\\])*"|/\\*.*?\\*/|//[^\\n]*', re.DOTALL)

    def replace(match: re.Match) -> str:
        token = match.group(0)

        if token.startswith('"'):
            return token

        return ''

    stripped = pattern.sub(replace, text)
    stripped = re.sub(',(\\s*[}\\]])', '\\1', stripped)

    return stripped


def tsaliases(tsconfig: Path, base_dir: Path, seen: set) -> dict[str, list[str]]:
    if str(tsconfig) in seen:
        return {}

    seen.add(str(tsconfig))

    try:
        raw = tsconfig.read_text(encoding='utf-8')
    except Exception as e:
        print(f'  warning: could not read {tsconfig} ({type(e).__name__}: {e})', file=sys.stderr, flush=True)

        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(stripjsonc(raw))
        except json.JSONDecodeError as e:
            print(f'  warning: failed to parse {tsconfig} as JSON/JSONC ({e.msg} at line {e.lineno} col {e.colno})', file=sys.stderr, flush=True)

            return {}
    except Exception as e:
        print(f'  warning: failed to parse {tsconfig} ({type(e).__name__}: {e})', file=sys.stderr, flush=True)

        return {}

    aliases: dict[str, list[str]] = {}
    extends = data.get('extends')

    if isinstance(extends, str):
        extends_list = [extends]
    elif isinstance(extends, list):
        extends_list = [e for e in extends if isinstance(e, str)]
    else:
        extends_list = []

    for ext in extends_list:
        if not ext or ext.startswith('@'):
            continue

        extended_path = resolvecached(base_dir / ext)

        if not extended_path.suffix:
            extended_path = extended_path.with_suffix('.json')

        if extended_path.exists():
            aliases.update(tsaliases(extended_path, extended_path.parent, seen))
    references = data.get('references')

    if isinstance(references, list):
        for ref in references:
            if not isinstance(ref, dict):
                continue

            ref_path = ref.get('path')

            if not isinstance(ref_path, str) or not ref_path:
                continue

            referenced = resolvecached(base_dir / ref_path)

            if referenced.is_dir():
                referenced = referenced / 'tsconfig.json'
            elif not referenced.suffix:
                referenced = referenced.with_suffix('.json')

            if referenced.exists():
                aliases.update(tsaliases(referenced, referenced.parent, seen))
    compiler_options = data.get('compilerOptions', {})
    base_url = compiler_options.get('baseUrl') or '.'
    paths_base = base_dir / base_url
    paths = compiler_options.get('paths', {})

    for alias, targets in paths.items():
        if not targets:
            continue

        target_patterns = [str(paths_base / t) for t in targets if isinstance(t, str) and t]

        if target_patterns:
            aliases[alias] = target_patterns

    return aliases


def jsonconfig(path: Path) -> 'dict | None':
    try:
        raw = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None

    for candidate in (raw, stripjsonc(raw)):
        try:
            data = json.loads(candidate)
        except Exception:
            continue

        return data if isinstance(data, dict) else None

    return None


def jsconfig(start_dir: Path) -> 'tuple[Path, Path] | None':
    current = resolvecached(start_dir)

    for candidate in [current, *current.parents]:
        for name in ('tsconfig.json', 'jsconfig.json'):
            config = candidate / name

            if config.exists():
                return (config, candidate)

    return None


def loadaliases(start_dir: Path) -> dict[str, list[str]]:
    found = jsconfig(start_dir)

    if found is None:
        return {}

    config, candidate = found
    key = str(config)

    if key not in TSALIASCACHE:
        TSALIASCACHE[key] = tsaliases(config, candidate, seen=set())

    return TSALIASCACHE[key]


def baseurl(start_dir: Path) -> 'Path | None':
    found = jsconfig(start_dir)

    if found is None:
        return None

    config, candidate = found
    key = str(config)

    if key not in TSBASEURLCACHE:
        base_url = None
        data = jsonconfig(config)

        if data is not None:
            raw_base = data.get('compilerOptions', {}).get('baseUrl')

            if isinstance(raw_base, str) and raw_base:
                base_url = Path(os.path.normpath(candidate / raw_base))
        TSBASEURLCACHE[key] = base_url

    return TSBASEURLCACHE[key]


def matchalias(raw: str, pattern: str) -> 'tuple[tuple[int, int], str, bool] | None':
    if '*' in pattern:
        if pattern.count('*') != 1:
            return None

        prefix, suffix = pattern.split('*', 1)

        if not raw.startswith(prefix) or not raw.endswith(suffix):
            return None

        end = len(raw) - len(suffix) if suffix else len(raw)

        if end < len(prefix):
            return None

        return ((1, -len(prefix)), raw[len(prefix):end], True)

    if raw == pattern:
        return ((0, -len(pattern)), '', False)

    prefix = pattern.rstrip('/')

    if prefix and raw.startswith(prefix + '/'):
        return ((2, -len(prefix)), raw[len(prefix):].lstrip('/'), False)

    return None


def tsalias(raw: str, aliases: dict[str, list[str]], base_url: 'Path | None'=None) -> 'Path | None':
    best: 'tuple[tuple[int, int], str, bool, list[str]] | None' = None

    for pattern, targets in aliases.items():
        match = matchalias(raw, pattern)

        if match is None:
            continue

        specificity, captured, is_wildcard = match

        if best is None or specificity < best[0]:
            best = (specificity, captured, is_wildcard, targets)

    if best is None:
        if base_url is not None:
            candidate = Path(os.path.normpath(base_url / raw))
            resolved = jspath(candidate)

            if resolved.is_file():
                return resolved

        return None

    _, captured, is_wildcard, targets = best
    first = None

    for target in targets:
        if is_wildcard:
            substituted = target.replace('*', captured, 1) if captured else target
            cand = Path(os.path.normpath(substituted))
        else:
            cand = Path(target)

            if captured:
                cand = Path(os.path.normpath(cand / captured))
        resolved = jspath(cand)

        if resolved.is_file():
            return resolved

        if first is None:
            first = cand

    return first


def workspaceroot(start_dir: Path) -> Path | None:
    current = resolvecached(start_dir)

    for candidate in [current, *current.parents]:
        if (candidate / 'pnpm-workspace.yaml').exists():
            return candidate

        package_json = candidate / 'package.json'

        if package_json.is_file():
            try:
                data = json.loads(package_json.read_text(encoding='utf-8'))
            except Exception:
                continue

            if 'workspaces' in data:
                return candidate

    return None


def pnpmglobs(workspace_file: Path) -> list[str]:
    globs: list[str] = []
    in_packages = False

    for raw_line in workspace_file.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw_line.strip()

        if not line or line.startswith('#'):
            continue

        if line.startswith('packages:'):
            in_packages = True
            continue

        if in_packages and line.startswith('-'):
            value = line[1:].strip().strip('\'"')

            if value and (not value.startswith('!')):
                globs.append(value)
            continue

        if in_packages and (not raw_line.startswith((' ', '\t'))):
            break

    return globs


def workspaceglobs(root: Path) -> list[str]:
    pnpm_workspace = root / 'pnpm-workspace.yaml'

    if pnpm_workspace.exists():
        return pnpmglobs(pnpm_workspace)

    package_json = root / 'package.json'

    try:
        data = json.loads(package_json.read_text(encoding='utf-8'))
    except Exception:
        return []

    workspaces = data.get('workspaces')

    if isinstance(workspaces, list):
        return [item for item in workspaces if isinstance(item, str) and (not item.startswith('!'))]

    if isinstance(workspaces, dict):
        packages = workspaces.get('packages')

        if isinstance(packages, list):
            return [item for item in packages if isinstance(item, str) and (not item.startswith('!'))]

    return []


def loadpackages(start_dir: Path) -> dict[str, Path]:
    root = workspaceroot(start_dir)

    if root is None:
        return {}

    manifest_mtimes = tuple(((name, (root / name).stat().st_mtime_ns) for name in WORKSPACEMANIFESTS if (root / name).is_file()))
    key = str((root, manifest_mtimes))

    if key in _WORKSPACE_PACKAGE_CACHE:
        return _WORKSPACE_PACKAGE_CACHE[key]

    packages: dict[str, Path] = {}

    for pattern in workspaceglobs(root):
        package_dirs: list[Path] = [root] if pattern in ('.', './') else list(root.glob(pattern))

        for package_dir in package_dirs:
            manifest = package_dir / 'package.json'

            if not manifest.is_file():
                continue

            try:
                data = json.loads(manifest.read_text(encoding='utf-8'))
            except Exception:
                continue

            name = data.get('name')

            if isinstance(name, str) and name:
                packages[name] = package_dir
    _WORKSPACE_PACKAGE_CACHE[key] = packages

    return packages

EXPORTCONDITIONS = ('source', 'import', 'module', 'svelte', 'require', 'default', 'types')
_PLATFORM_EXPORT_CONDITIONS = {'native': ('react-native',)}
_PLATFORM_SUFFIXES = ('web', 'native', 'ios', 'android')
_PLATFORM_PATH_HINTS = (('native', ('native', 'mobile', 'ios', 'android', 'react-native')), ('web', ('web', 'browser', 'desktop', 'electron')))


def platform(start_dir: Path) -> str | None:
    for part in reversed(start_dir.parts):
        segment = part.lower()

        for platform, hints in _PLATFORM_PATH_HINTS:
            if segment in hints:
                return platform

    return None


def variants(candidate: Path, platform: str | None) -> list[Path]:
    order = [p for p in (platform,) if p in _PLATFORM_SUFFIXES]
    order.extend((p for p in _PLATFORM_SUFFIXES if p not in order))

    return [candidate.parent / f'{candidate.stem}.{p}{candidate.suffix}' for p in order]


def exporttargets(value: Any, platform: str | None=None) -> list[str]:
    if isinstance(value, str):
        return [value]

    if isinstance(value, dict):
        targets: list[str] = []

        for cond in (*_PLATFORM_EXPORT_CONDITIONS.get(platform or '', ()), *EXPORTCONDITIONS):
            v = value.get(cond)

            if isinstance(v, (str, dict)):
                targets.extend(exporttargets(v, platform))

        return targets

    return []


def exporttarget(value: Any) -> str | None:
    targets = exporttargets(value)

    return targets[0] if targets else None


def contained(resolved: Path, package_dir: Path) -> bool:
    try:
        return resolvecached(resolved).is_relative_to(resolvecached(package_dir))
    except ValueError:
        return False


def exportcandidates(package_dir: Path, targets: list[str], platform: str | None) -> list[Path]:
    candidates: list[Path] = []

    for target in targets:
        candidate = package_dir / target

        if not contained(candidate, package_dir):
            continue

        candidates.append(candidate)
        candidates.extend(variants(candidate, platform))

    return candidates


def entrycandidates(package_dir: Path, subpath: str, platform: str | None=None) -> list[Path]:
    manifest = package_dir / 'package.json'
    manifest_data: dict[str, Any] = {}

    try:
        manifest_data = json.loads(manifest.read_text(encoding='utf-8'))
    except Exception:
        pass

    if subpath:
        exports = manifest_data.get('exports')

        if isinstance(exports, dict):
            subpath_key = './' + subpath
            targets = exporttargets(exports.get(subpath_key), platform)

            if targets:
                candidates = exportcandidates(package_dir, targets, platform)

                if candidates:
                    return candidates
            else:
                for pattern, pattern_value in exports.items():
                    if '*' in pattern and pattern.count('*') == 1:
                        prefix, suffix = pattern.split('*', 1)

                        if subpath_key.startswith(prefix) and (not suffix or subpath_key.endswith(suffix)):
                            matched = subpath_key[len(prefix):len(subpath_key) - len(suffix) if suffix else None]
                            wildcard_targets = [resolved.replace('*', matched) for resolved in exporttargets(pattern_value, platform) if '*' in resolved]

                            if wildcard_targets:
                                candidates = exportcandidates(package_dir, wildcard_targets, platform)

                                if candidates:
                                    return candidates

        return [package_dir / subpath]

    exports = manifest_data.get('exports')

    if isinstance(exports, str):
        return [package_dir / exports]

    if isinstance(exports, dict):
        dot_targets = exporttargets(exports.get('.'), platform)

        if dot_targets:
            return exportcandidates(package_dir, dot_targets, platform)

    candidates: list[Path] = []

    for key in ('svelte', 'module', 'main', 'types'):
        value = manifest_data.get(key)

        if isinstance(value, str):
            candidates.append(package_dir / value)
    candidates.append(package_dir / 'src/index')
    candidates.append(package_dir / 'index')

    return candidates

_BUILD_OUTPUT_DIRS = frozenset({'dist', 'build', 'target', 'out', 'dist-protected'})


def isbuildoutput(path: Path, package_dir: Path) -> bool:
    try:
        rel = resolvecached(path).relative_to(resolvecached(package_dir))

        return bool(rel.parts and rel.parts[0] in _BUILD_OUTPUT_DIRS)
    except ValueError:
        return False


def workspaceimport(raw: str, start_dir: Path) -> Path | None:
    packages = loadpackages(start_dir)
    platform = platform(start_dir)

    for package_name, package_dir in packages.items():
        if raw == package_name:
            subpath = ''
        elif raw.startswith(package_name + '/'):
            subpath = raw[len(package_name) + 1:]
        else:
            continue

        build_fallback: Path | None = None

        for candidate in entrycandidates(package_dir, subpath, platform):
            resolved = jspath(candidate)

            if resolved.is_file():
                if isbuildoutput(resolved, package_dir):
                    if build_fallback is None:
                        build_fallback = resolved
                else:
                    return resolved

        if build_fallback is not None:
            return build_fallback

    return None


def projectanchor(start_dir: Path) -> Path:
    from graphify.detect import vcs
    current = resolvecached(start_dir)
    home = Path.home()

    for candidate in [current, *current.parents]:
        if candidate == home:
            break

        if (candidate / 'package.json').is_file() or (candidate / 'pnpm-workspace.yaml').is_file():
            return candidate

    vcs_root = vcs(start_dir)

    if vcs_root is not None:
        return vcs_root

    return current


def loadimports(start_dir: Path) -> 'tuple[Path, dict] | None':
    current = resolvecached(start_dir)
    key = str(current)

    if key in PACKAGEIMPORTSCACHE:
        return PACKAGEIMPORTSCACHE[key]

    result: 'tuple[Path, dict] | None' = None

    for candidate in [current, *current.parents]:
        manifest = candidate / 'package.json'

        if manifest.is_file():
            data = jsonconfig(manifest)
            imports = data.get('imports') if isinstance(data, dict) else None

            if isinstance(imports, dict) and imports:
                result = (candidate, imports)
            break

    PACKAGEIMPORTSCACHE[key] = result

    return result


def matchsubpath(raw: str, pattern: str) -> 'tuple[int, str, bool] | None':
    if '*' in pattern:
        if pattern.count('*') != 1:
            return None

        prefix, suffix = pattern.split('*', 1)

        if not raw.startswith(prefix) or not raw.endswith(suffix):
            return None

        end = len(raw) - len(suffix) if suffix else len(raw)

        if end < len(prefix):
            return None

        return (-len(prefix), raw[len(prefix):end], True)

    if raw == pattern:
        return (-(len(pattern) + 1000000), '', False)

    return None


def packageimport(raw: str, start_dir: Path) -> 'Path | None':
    if not raw.startswith('#'):
        return None

    loaded = loadimports(start_dir)

    if loaded is None:
        return None

    package_dir, imports = loaded
    best: 'tuple[int, str, bool, object] | None' = None

    for pattern, value in imports.items():
        if not isinstance(pattern, str) or not pattern.startswith('#'):
            continue

        match = matchsubpath(raw, pattern)

        if match is None:
            continue

        specificity, captured, is_wildcard = match

        if best is None or specificity < best[0]:
            best = (specificity, captured, is_wildcard, value)

    if best is None:
        return None

    _, captured, is_wildcard, value = best
    target = exporttarget(value)

    if not isinstance(target, str) or not target.startswith('./'):
        return None

    if is_wildcard:
        target = target.replace('*', captured, 1) if captured else target
    candidate = Path(os.path.normpath(package_dir / target))
    resolved = jspath(candidate)

    return resolved if resolved.is_file() else None


def jsmodulepath(raw: str | Path, start_dir: Path | None=None) -> Path | None:
    if isinstance(raw, Path):
        return jspath(raw)

    if start_dir is None:
        return jspath(Path(raw))

    if raw.startswith('.'):
        return jspath(start_dir / raw)

    aliases = loadaliases(start_dir)
    hit = tsalias(raw, aliases, base_url=baseurl(start_dir))

    if hit is not None:
        return jspath(hit)

    if raw.startswith('#'):
        hit = packageimport(raw, start_dir)

        if hit is not None:
            return hit

    workspace_hit = workspaceimport(raw, start_dir)

    if workspace_hit is not None:
        return workspace_hit

    if raw.startswith('@/') and jsconfig(start_dir) is None:
        subpath = raw[2:]

        if subpath:
            anchor = projectanchor(start_dir)

            if (anchor / 'src').is_dir():
                cand = jspath(anchor / 'src' / subpath)

                if cand.is_file():
                    return cand

            cand = jspath(anchor / subpath)

            if cand.is_file():
                return cand

    return None


def jstarget(raw: str, str_path: str) -> 'tuple[str, Path | None] | None':
    if not raw:
        return None

    resolved_path = jsmodulepath(raw, Path(str_path).parent)

    if resolved_path is not None:
        return (make(str(resolved_path)), resolved_path)

    module_name = raw.split('/')[-1]

    if not module_name:
        return None

    if raw.startswith('@'):
        package_root = '/'.join(raw.split('/')[:2])
    else:
        package_root = raw.split('/')[0]

    return (make('ref', package_root), None)


def cinclude(raw: str, str_path: str) -> 'Path | None':
    if not raw:
        return None

    candidate = resolvecached(Path(str_path).parent / raw)

    if candidate.is_file():
        return candidate

    return None


def luatarget(raw_module: str, str_path: str) -> str:
    if not raw_module:
        return ''

    rel = raw_module.replace('.', '/')

    try:
        start_dir = Path(str_path).parent
    except Exception:
        start_dir = None

    if start_dir is not None:
        probe = start_dir

        for _ in range(6):
            for suffix in ('.lua', '.luau'):
                cand = probe / f'{rel}{suffix}'

                if cand.is_file():
                    return make(str(cand))

            for suffix in ('.lua', '.luau'):
                cand = probe / rel / f'init{suffix}'

                if cand.is_file():
                    return make(str(cand))

            if probe.parent == probe:
                break

            probe = probe.parent

    return make(raw_module)

VUESCRIPT = re.compile('(<script\\b(?:"[^"]*"|\'[^\']*\'|[^>"\'])*>)([\\s\\S]*?)(</script\\s*>)', re.IGNORECASE)
VUESCRIPTLANG = re.compile('\\blang\\s*=\\s*[\'"]?([A-Za-z]+)[\'"]?', re.IGNORECASE)


def vuemask(src: str) -> tuple[str, str | None]:

    def blank(s: str) -> str:
        return re.sub('[^\\r\\n]', ' ', s)

    out: list[str] = []
    pos = 0
    lang: str | None = None

    for m in VUESCRIPT.finditer(src):
        out.append(blank(src[pos:m.start()]))
        out.append(blank(m.group(1)))
        out.append(m.group(2))
        out.append(blank(m.group(3)))
        pos = m.end()

        if lang is None:
            lang_m = VUESCRIPTLANG.search(m.group(1))

            if lang_m:
                lang = lang_m.group(1).lower()
    out.append(_blank(src[pos:]))

    return (''.join(out), lang)


@functools.lru_cache(maxsize=65536)


def cachedsourcekey(source_file: str, root_str: str, _cwd: str) -> str:
    source_path = Path(source_file)

    try:
        return str(source_path.resolve().relative_to(root_str))
    except Exception:
        return str(source_path)


def sourcekey(source_file: str, root: Path) -> str:
    if not source_file:
        return ''

    return cachedsourcekey(source_file, str(root), os.getcwd())


def nodedisambigkey(node: dict, root: Path) -> str:
    source_file = str(node.get('source_file', ''))

    if source_file:
        return sourcekey(source_file, root)

    return sourcekey(str(node.get('origin_file', '')), root)


def disambiguatenodes(nodes: list[dict], edges: list[dict], raw_calls: list[dict], root: Path) -> None:
    by_id: dict[str, list[dict]] = {}

    for node in nodes:
        if node.get('type') in ('module', 'namespace'):
            continue

        nid = node.get('id')

        if isinstance(nid, str) and nid:
            by_id.setdefault(nid, []).append(node)
    remap: dict[tuple[str, str], str] = {}
    ambiguous_ids: set[str] = set()

    for old_id, group in by_id.items():
        source_keys = {nodedisambigkey(node, root) for node in group}

        if len(group) < 2 or len(source_keys) < 2:
            continue

        ambiguous_ids.add(old_id)
        naive: dict[str, str] = {}

        for source_key in source_keys:
            if source_key:
                naive[source_key] = make(source_key, old_id)
        seen: dict[str, int] = {}

        for nid in naive.values():
            seen[nid] = seen.get(nid, 0) + 1
        needs_hash = {sk for sk, nid in naive.items() if seen.get(nid, 0) > 1}

        for node in group:
            source_key = nodedisambigkey(node, root)

            if not source_key:
                continue

            if source_key in needs_hash:
                salt = hashlib.sha1(source_key.encode('utf-8')).hexdigest()[:6]
                new_id = make(source_key, old_id, salt)
            else:
                new_id = naive.get(source_key) or make(source_key, old_id)
            remap[old_id, source_key] = new_id

            if new_id != old_id:
                node['id'] = new_id

    if not remap:
        for edge in edges:
            edge.pop('target_file', None)
        return
    unambiguous_remaps: dict[str, str] = {}

    for old_id, group in by_id.items():
        if old_id in ambiguous_ids:
            continue

        candidates = {node['id'] for node in group if isinstance(node.get('id'), str) and node['id'] != old_id}

        if len(candidates) == 1:
            unambiguous_remaps[old_id] = next(iter(candidates))
    _HEADER_SUFFIXES = ('.h', '.hpp', '.hh', '.hxx')
    header_remaps: dict[str, str] = {}

    for old_id in ambiguous_ids:
        for node in by_id.get(old_id, []):
            sk = nodedisambigkey(node, root)

            if sk and Path(sk).suffix.lower() in _HEADER_SUFFIXES:
                new_id = remap.get((old_id, sk))

                if new_id:
                    header_remaps[old_id] = new_id
                    break

    for edge in edges:
        edge_source_key = sourcekey(str(edge.get('source_file', '')), root)
        source_key = (edge.get('source', ''), edge_source_key)
        target_file = edge.pop('target_file', None)

        if target_file and edge.get('relation') in ('imports', 'imports_from', 're_exports'):
            target_edge_key = sourcekey(str(target_file), root)
        else:
            target_edge_key = edge_source_key
        target_key = (edge.get('target', ''), target_edge_key)

        if source_key in remap:
            edge['source'] = remap[source_key]
        elif edge.get('source') in unambiguous_remaps:
            edge['source'] = unambiguous_remaps[str(edge['source'])]

        if edge.get('relation') in ('imports', 'imports_from') and edge.get('target') in header_remaps:
            edge['target'] = header_remaps[str(edge['target'])]
        elif target_key in remap:
            edge['target'] = remap[target_key]
        elif edge.get('target') in unambiguous_remaps:
            edge['target'] = unambiguous_remaps[str(edge['target'])]

    for raw_call in raw_calls:
        call_source_key = sourcekey(str(raw_call.get('source_file', '')), root)
        caller_key = (raw_call.get('caller_nid', ''), call_source_key)

        if caller_key in remap:
            raw_call['caller_nid'] = remap[caller_key]
        elif raw_call.get('caller_nid') in unambiguous_remaps:
            raw_call['caller_nid'] = unambiguous_remaps[str(raw_call['caller_nid'])]


def istypelike(node: dict) -> bool:
    if node.get('type') == 'namespace':
        return False

    label = str(node.get('label', '')).strip()

    if not label:
        return False

    if label.endswith(')') or label.startswith('.'):
        return False

    if '.' in label:
        return False

    return node.get('file_type') == 'code'


@functools.lru_cache(maxsize=65536)


def cachedrealpath(path_str: str, _cwd: str) -> Path:
    return Path(path_str).resolve()


def resolvecached(path: 'Path | str') -> Path:
    return cachedrealpath(str(path), os.getcwd())


def jssourcepath(source_file: str, root: Path) -> Path | None:
    if not source_file:
        return None

    path = Path(source_file)

    if not path.is_absolute():
        path = root / path

    try:
        return resolvecached(path)
    except Exception:
        return path


def applyfacts(paths: list[Path], nodes: list[dict], edges: list[dict], root: Path, facts: _SymbolResolutionFacts, resolution_context_nodes: list[dict] | None=None) -> None:
    if not (facts.declarations or facts.imports or facts.aliases or facts.exports or facts.star_exports or facts.namespace_exports or facts.uses or facts.module_imports):
        return
    path_by_resolved = {resolvecached(path): path for path in paths}
    source_file_id = {resolvecached(path): make(str(path)) for path in paths}
    symbol_nodes: dict[tuple[Path, str], str] = {}
    member_symbol_keys: set[tuple[Path, str]] = set()

    if resolution_context_nodes:
        _fresh_node_ids = {n.get('id') for n in nodes if n.get('id')}
        _fresh_paths = {resolvecached(p) for p in paths}

        for node in resolution_context_nodes:
            nid = node.get('id')

            if not nid or nid in _fresh_node_ids:
                continue

            source_path = jssourcepath(str(node.get('source_file', '')), root)

            if source_path is None or source_path in _fresh_paths:
                continue

            raw_label = str(node.get('label', '')).strip()
            label = raw_label.strip('()').lstrip('.')

            if not label:
                continue

            key = (source_path, label)

            if raw_label.startswith('.'):
                if key in symbol_nodes:
                    continue

                member_symbol_keys.add(key)
            else:
                member_symbol_keys.discard(key)
            symbol_nodes[key] = str(nid)

    for node in nodes:
        source_path = jssourcepath(str(node.get('source_file', '')), root)

        if source_path is None:
            continue

        raw_label = str(node.get('label', '')).strip()
        label = raw_label.strip('()').lstrip('.')

        if not label or not node.get('id'):
            continue

        key = (source_path, label)

        if raw_label.startswith('.'):
            if key in symbol_nodes:
                continue

            member_symbol_keys.add(key)
        else:
            member_symbol_keys.discard(key)
        symbol_nodes[key] = str(node['id'])

    def ensuresymbol(path: Path, name: str, line: int) -> str:
        resolved_path = resolvecached(path)
        existing = symbol_nodes.get((resolved_path, name))

        if existing is not None:
            return existing

        node_id = make(stem(path), name)
        symbol_nodes[resolved_path, name] = node_id
        nodes.append({'id': node_id, 'label': name, 'file_type': 'code', 'source_file': str(path), 'source_location': f'L{line}'})

        return node_id

    existing_edges = {(str(edge.get('source')), str(edge.get('target')), str(edge.get('relation')), str(edge.get('context') or ''), jssourcepath(str(edge.get('source_file') or ''), root)) for edge in edges}

    def addedge(source: str, target: str, relation: str, context: str, line: int, source_path: Path, target_file: str | None=None, local_alias: str | None=None, type_only: bool=False) -> None:
        key = (source, target, relation, context or '', jssourcepath(str(source_path), root))

        if key in existing_edges:
            return
        existing_edges.add(key)
        edge = {'source': source, 'target': target, 'relation': relation, 'context': context, 'source_file': str(source_path), 'source_location': f'L{line}'}

        if target_file is not None:
            edge['target_file'] = target_file

        if local_alias is not None:
            edge['local_alias'] = local_alias

        if type_only:
            edge['type_only'] = True
        edges.append(edge)

    for declaration in facts.declarations:
        ensuresymbol(declaration.file_path, declaration.name, declaration.line)
    local_aliases_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}

    for import_fact in facts.imports:
        file_path = resolvecached(import_fact.file_path)
        local_aliases_by_file.setdefault(file_path, {})[import_fact.local_name] = (resolvecached(import_fact.target_path), import_fact.imported_name)
    pending_aliases_by_file: dict[Path, list[_SymbolAliasFact]] = {}

    for alias_fact in facts.aliases:
        pending_aliases_by_file.setdefault(resolvecached(alias_fact.file_path), []).append(alias_fact)

    for file_path, aliases in pending_aliases_by_file.items():
        local_aliases = local_aliases_by_file.setdefault(file_path, {})
        changed = True

        while changed:
            changed = False

            for alias_fact in aliases:
                if alias_fact.alias in local_aliases:
                    continue

                origin = local_aliases.get(alias_fact.target_name)

                if origin is not None:
                    local_aliases[alias_fact.alias] = origin
                    changed = True
    named_exports_by_file: dict[Path, dict[str, tuple[Path, str]]] = {}
    export_origins: dict[tuple[Path, str], set[tuple[Path, str]]] = {}
    star_exports_by_file: dict[Path, list[Path]] = {}

    for star_fact in facts.star_exports:
        source_path = resolvecached(star_fact.file_path)
        target_path = resolvecached(star_fact.target_path)
        star_exports_by_file.setdefault(source_path, []).append(target_path)
        source_id = source_file_id.get(source_path)

        if source_id is not None:
            addedge(source_id, make(str(path_by_resolved.get(target_path, target_path))), 're_exports', 'export', star_fact.line, star_fact.file_path, target_file=str(path_by_resolved.get(target_path, target_path)), type_only=star_fact.type_only)

    for namespace_fact in facts.namespace_exports:
        source_path = resolvecached(namespace_fact.file_path)
        target_path = resolvecached(namespace_fact.target_path)
        namespace_id = ensuresymbol(namespace_fact.file_path, namespace_fact.exported_name, namespace_fact.line)
        named_exports_by_file.setdefault(source_path, {})[namespace_fact.exported_name] = (source_path, namespace_fact.exported_name)
        source_id = source_file_id.get(source_path)

        if source_id is not None:
            addedge(source_id, namespace_id, 'contains', 'namespace_export', namespace_fact.line, namespace_fact.file_path)
            addedge(source_id, make(str(path_by_resolved.get(target_path, target_path))), 're_exports', 'export', namespace_fact.line, namespace_fact.file_path, target_file=str(path_by_resolved.get(target_path, target_path)), type_only=namespace_fact.type_only)

    for export_fact in facts.exports:
        file_path = resolvecached(export_fact.file_path)
        origin: tuple[Path, str] | None = None

        if export_fact.target_path is not None and export_fact.target_name is not None:
            origin = (resolvecached(export_fact.target_path), export_fact.target_name)
        elif export_fact.local_name is not None:
            origin = local_aliases_by_file.get(file_path, {}).get(export_fact.local_name)

            if origin is None and (file_path, export_fact.local_name) in symbol_nodes:
                origin = (file_path, export_fact.local_name)

        if origin is None:
            if export_fact.local_name is not None:
                export_origins.setdefault((file_path, export_fact.exported_name), set()).add((file_path, export_fact.local_name))
            continue

        export_origins.setdefault((file_path, export_fact.exported_name), set()).add(origin)
        named_exports_by_file.setdefault(file_path, {})[export_fact.exported_name] = origin

        if origin[0] != file_path:
            source_id = source_file_id.get(file_path)

            if source_id is not None:
                addedge(source_id, make(str(path_by_resolved.get(origin[0], origin[0]))), 're_exports', 'export', export_fact.line, export_fact.file_path, target_file=str(path_by_resolved.get(origin[0], origin[0])), type_only=export_fact.type_only)

    def resolveorigin(target_path: Path, imported_name: str, seen: set[tuple[Path, str]] | None=None) -> tuple[Path, str]:
        target_path = resolvecached(target_path)
        key = (target_path, imported_name)

        if seen is None:
            seen = set()

        if key in seen:
            return key

        seen.add(key)
        origin = named_exports_by_file.get(target_path, {}).get(imported_name)

        if origin is not None:
            return resolveorigin(origin[0], origin[1], seen)

        for star_target in star_exports_by_file.get(target_path, []):
            star_key = (star_target, imported_name)

            if star_key in symbol_nodes and star_key not in member_symbol_keys:
                return star_key

            resolved = resolveorigin(star_target, imported_name, seen)

            if resolved in symbol_nodes and resolved not in member_symbol_keys:
                return resolved

        return key

    def exportedcandidates(key: tuple[Path, str], seen: frozenset[tuple[Path, str]]) -> tuple[set[tuple[Path, str]], bool]:
        if key in seen:
            return (set(), True)

        seen = seen | {key}
        origins = export_origins.get(key)
        candidates: set[tuple[Path, str]] = set()
        cyclic = False

        if origins is None:
            for path in star_exports_by_file.get(key[0], []):
                branch, branch_cyclic = exportedcandidates((path, key[1]), seen)
                candidates.update(branch)
                cyclic |= branch_cyclic

            return (candidates, cyclic)

        for origin in origins:
            if origin == key:
                candidates.add(origin)
            else:
                branch, branch_cyclic = exportedcandidates(origin, seen)
                candidates.update(branch)
                cyclic |= branch_cyclic

                if not branch and (not branch_cyclic):
                    candidates.add(origin)

        return (candidates, cyclic)

    export_sites: dict[tuple[Path, str, Path], list[_SymbolExportFact]] = {}

    for export_fact in facts.exports:
        if export_fact.target_path is not None and export_fact.target_name is not None:
            site = (resolvecached(export_fact.file_path), f'L{export_fact.line}', resolvecached(export_fact.target_path))
            export_sites.setdefault(site, []).append(export_fact)
    owned_ids = {node.get('id') for node in nodes}

    for edge in edges:
        if edge.get('relation') != 're_exports' or edge.get('target') in owned_ids:
            continue

        target_file = edge.get('target_file')
        source_path = jssourcepath(str(edge.get('source_file', '')), root)

        if not target_file or source_path is None:
            continue

        target_path = Path(target_file)
        site = (source_path, str(edge.get('source_location', '')), resolvecached(target_path))

        for export_fact in export_sites.get(site, []):
            expected = make(stem(target_path), export_fact.target_name)

            if edge.get('target') != expected:
                continue

            candidates, _ = exportedcandidates((resolvecached(export_fact.target_path), export_fact.target_name), frozenset())

            if len(candidates) == 1:
                origin = next(iter(candidates))
                target_id = symbol_nodes.get(origin)

                if target_id in owned_ids:
                    edge['target'] = target_id
                    edge['target_file'] = str(path_by_resolved.get(origin[0], origin[0]))
            break

    for import_fact in facts.imports:
        source_id = source_file_id.get(resolvecached(import_fact.file_path))

        if source_id is None:
            continue

        origin_path, origin_symbol = resolveorigin(import_fact.target_path, import_fact.imported_name)
        target_id = symbol_nodes.get((origin_path, origin_symbol))

        if target_id is None:
            continue

        addedge(source_id, target_id, 'imports', 'import', import_fact.line, import_fact.file_path)
    provisional_by_loc: dict[tuple[Path, str], list[dict]] = {}

    for edge in edges:
        if edge.get('relation') == 'imports_from' and edge.get('context') == 'import':
            src = jssourcepath(str(edge.get('source_file', '')), root)
            loc = edge.get('source_location')

            if src is not None and loc:
                provisional_by_loc.setdefault((src, loc), []).append(edge)
    retracted_edge_ids: set[int] = set()

    for from_path, to_path, line, local_name in facts.module_imports:
        try:
            from_rel = from_path.relative_to(root)
            to_rel = to_path.relative_to(root)
        except ValueError:
            continue

        source_id = make(stem(from_rel))
        target_id = make(stem(to_rel))
        from_canon = resolvecached(from_path)
        pkg_dir = to_path.parent
        candidate_targets: set[str] = set()

        try:
            pkg_rel = pkg_dir.relative_to(root)
            candidate_targets.add(make('.'.join(pkg_rel.parts)))
            candidate_targets.add(make(stem(pkg_rel)))
            candidate_targets.add(make(str(pkg_rel)))
        except ValueError:
            pass
        candidate_targets.add(make(str(pkg_dir)))
        candidate_targets.add(make(str(pkg_dir.with_suffix('.py'))))

        try:
            from_rel_parent = from_path.parent.relative_to(root)
            candidate_targets.add(make(str(from_rel_parent.with_suffix('.py'))))
        except ValueError:
            pass
        loc_str = f'L{line}'
        loc_candidates = [e for e in provisional_by_loc.get((from_canon, loc_str), []) if id(e) not in retracted_edge_ids]
        matched_edge = None

        for edge in loc_candidates:
            if edge.get('target') in candidate_targets:
                matched_edge = edge
                break

        if matched_edge is not None:
            retracted_edge_ids.add(id(matched_edge))
            existing_edges.discard((str(matched_edge.get('source')), str(matched_edge.get('target')), str(matched_edge.get('relation')), str(matched_edge.get('context') or '')))
        addedge(source_id, target_id, 'imports_from', 'submodule_import', line, from_path, local_alias=local_name if local_name != to_path.stem else None)

    if retracted_edge_ids:
        edges[:] = [e for e in edges if id(e) not in retracted_edge_ids]
    owned = {str(n.get('id')) for n in nodes}

    for use_fact in facts.uses:
        file_path = resolvecached(use_fact.file_path)
        target_id = None
        unresolved_origin = local_aliases_by_file.get(file_path, {}).get(use_fact.local_name)

        if unresolved_origin is not None:
            origin_path, origin_symbol = resolveorigin(*unresolved_origin)
            target_id = symbol_nodes.get((origin_path, origin_symbol))

        if target_id is None and use_fact.relation in ('inherits', 'implements'):
            target_id = symbol_nodes.get((file_path, use_fact.local_name))

        if target_id is None:
            continue

        source_id = use_fact.source_id

        if use_fact.relation in ('inherits', 'implements', 'references') and source_id not in owned:
            continue

        if use_fact.relation == 'calls' and source_id not in owned:
            source_id = source_file_id.get(file_path)

            if source_id is None:
                continue

        addedge(source_id, target_id, use_fact.relation, use_fact.context, use_fact.line, use_fact.file_path)


def parsejstree(path: Path):
    try:
        from tree_sitter import Language, Parser
        vue_lang: str | None = None

        if path.suffix == '.vue':
            masked, vue_lang = vuemask(base.text(path, warn=False))
            source = masked.encode('utf-8')
        else:
            source = base.bytes(path, warn=False)
        use_ts = path.suffix in ('.ts', '.mts', '.cts') or (path.suffix == '.vue' and vue_lang not in ('js', 'jsx'))

        if path.suffix == '.tsx':
            import tree_sitter_typescript as tstypescript
            language = Language(tstypescript.language_tsx())
        elif use_ts:
            import tree_sitter_typescript as tstypescript
            language = Language(tstypescript.language_typescript())
        else:
            import tree_sitter_javascript as tsjavascript
            language = Language(tsjavascript.language())
        parser = Parser(language)

        return (source, parser.parse(source).root_node)
    except Exception:
        return None


def walkjstree(node):
    stack = [node]

    while stack:
        n = stack.pop()

        yield n

        stack.extend(reversed(n.children))


def jsspecifier(node, source: bytes) -> str | None:
    source_node = node.child_by_field_name('source')

    if source_node is None:
        for child in node.children:
            if child.type == 'string':
                source_node = child
                break

    if source_node is None:
        return None

    raw = read(source_node, source).strip()

    return raw.strip('\'"`') or None


def jsnamed(node, source: bytes, specifier_type: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []

    for child in walkjstree(node):
        if child.type != specifier_type:
            continue

        name_node = child.child_by_field_name('name')

        if name_node is None:
            continue

        alias_node = child.child_by_field_name('alias')
        name = read(name_node, source)
        exposed = read(alias_node, source) if alias_node is not None else name

        if name and exposed:
            pairs.append((name, exposed))

    return pairs


def jsexportclause(node):
    for child in node.children:
        if child.type == 'export_clause':
            return child

    return None


def jsisstar(node) -> bool:
    return any((child.type == '*' for child in node.children))


def jsnamespacename(node, source: bytes) -> str | None:
    for child in node.children:
        if child.type != 'namespace_export':
            continue

        for sub in child.children:
            if sub.type == 'identifier':
                return read(sub, source) or None

    return None


def jslexicalaliases(node, source: bytes) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []

    if node.type != 'lexical_declaration':
        return aliases

    for child in node.children:
        if child.type != 'variable_declarator':
            continue

        name_node = child.child_by_field_name('name')
        value_node = child.child_by_field_name('value')

        if name_node is not None and value_node is not None and (value_node.type in ('identifier', 'type_identifier')):
            aliases.append((read(name_node, source), read(value_node, source)))

    return aliases


def jsdeclarations(node, source: bytes) -> list[str]:
    names: list[str] = []
    declaration = node.child_by_field_name('declaration')

    if declaration is None:
        return names

    if declaration.type in ('lexical_declaration', 'variable_declaration'):
        names.extend((alias for alias, _target in jslexicalaliases(declaration, source)))

        for declarator in declaration.named_children:
            if declarator.type != 'variable_declarator':
                continue

            name_node = declarator.child_by_field_name('name')

            if name_node is not None and name_node.type == 'identifier':
                name = read(name_node, source)

                if name not in names:
                    names.append(name)

        return names

    if declaration.type in ('class_declaration', 'abstract_class_declaration', 'interface_declaration', 'type_alias_declaration', 'function_declaration'):
        name_node = declaration.child_by_field_name('name')

        if name_node is not None:
            names.append(read(name_node, source))

    return names


def jsdefaultimport(node, source: bytes) -> str | None:
    for child in node.children:
        if child.type == 'import_clause':
            for sub in child.children:
                if sub.type == 'identifier':
                    return read(sub, source)

    return None


def jsdefaultexport(node, source: bytes) -> str | None:
    if not any((child.type == 'default' for child in node.children)):
        return None

    declaration = node.child_by_field_name('declaration')

    if declaration is not None:
        name_node = declaration.child_by_field_name('name')

        return read(name_node, source) if name_node is not None else None

    value = node.child_by_field_name('value')

    if value is not None and value.type == 'identifier':
        return read(value, source)

    return None


def jsbodies(path: Path, root_node, source: bytes) -> list[tuple[str, object]]:
    bodies: list[tuple[str, object]] = []
    stem = base.stem(path)
    top_nodes: list = []

    for node in root_node.children:
        if node.type == 'export_statement' and (not any((c.type == 'string' for c in node.children))):
            top_nodes.extend((c for c in node.children if c.type in ('function_declaration', 'lexical_declaration')))
        else:
            top_nodes.append(node)

    for node in top_nodes:
        if node.type == 'function_declaration':
            name_node = node.child_by_field_name('name')
            body = node.child_by_field_name('body')

            if name_node is not None and body is not None:
                bodies.append((make(stem, read(name_node, source)), body))
            continue

        if node.type != 'lexical_declaration':
            continue

        for child in node.children:
            if child.type != 'variable_declarator':
                continue

            name_node = child.child_by_field_name('name')
            value_node = child.child_by_field_name('value')

            if name_node is not None and value_node is not None and (value_node.type == 'arrow_function'):
                bodies.append((make(stem, read(name_node, source)), value_node))

    return bodies


def jscallident(node, source: bytes) -> str | None:
    if node.type != 'call_expression':
        return None

    function_node = node.child_by_field_name('function')

    if function_node is None:
        for child in node.children:
            if child.is_named:
                function_node = child
                break

    if function_node is not None and function_node.type in ('identifier', 'type_identifier'):
        return read(function_node, source)

    return None

JSPRIMITIVES = frozenset({'string', 'number', 'boolean', 'any', 'unknown', 'void', 'never', 'object', 'null', 'undefined', 'bigint', 'symbol', 'this'})


def tsheritage(clause_node, source: bytes) -> list[str]:
    out: list[str] = []

    for child in clause_node.children:
        if not child.is_named:
            continue

        if child.type in ('identifier', 'type_identifier'):
            name = read(child, source)

            if name:
                out.append(name)
        elif child.type == 'generic_type':
            name_node = child.child_by_field_name('name')

            if name_node is None:
                for sub in child.children:
                    if sub.type in ('type_identifier', 'nested_type_identifier', 'identifier'):
                        name_node = sub
                        break

            if name_node is not None:
                text = read(name_node, source).rsplit('.', 1)[-1]

                if text:
                    out.append(text)
        elif child.type == 'nested_type_identifier':
            text = read(child, source).rsplit('.', 1)[-1]

            if text:
                out.append(text)

    return out


def tstyperefs(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t == 'type_annotation':
        for c in node.children:
            if c.is_named:
                tstyperefs(c, source, generic, out)
        return

    if t in ('type_identifier', 'identifier'):
        name = read(node, source)

        if name and name not in JSPRIMITIVES:
            out.append((name, 'generic_arg' if generic else 'type'))
        return

    if t == 'nested_type_identifier':
        tail = read(node, source).rsplit('.', 1)[-1]

        if tail and tail not in JSPRIMITIVES:
            out.append((tail, 'generic_arg' if generic else 'type'))
        return

    if t == 'generic_type':
        name_node = node.child_by_field_name('name')

        if name_node is not None:
            text = read(name_node, source).rsplit('.', 1)[-1]

            if text and text not in JSPRIMITIVES:
                out.append((text, 'generic_arg' if generic else 'type'))
        else:
            for c in node.children:
                if c.type in ('type_identifier', 'nested_type_identifier'):
                    text = read(c, source).rsplit('.', 1)[-1]

                    if text and text not in JSPRIMITIVES:
                        out.append((text, 'generic_arg' if generic else 'type'))
                    break

        for c in node.children:
            if c.type == 'type_arguments':
                for sub in c.children:
                    if sub.is_named:
                        tstyperefs(sub, source, True, out)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                tstyperefs(c, source, generic, out)


def tsmembers(class_node, source: bytes, path: Path, class_nid: str, facts: _SymbolResolutionFacts) -> None:
    line = class_node.start_point[0] + 1

    for child in class_node.children:
        if child.type == 'class_heritage':
            saw_clause = False

            for clause in child.children:
                if clause.type == 'extends_clause':
                    saw_clause = True

                    for name in tsheritage(clause, source):
                        facts.uses.append(_SymbolUseFact(path, class_nid, name, 'inherits', 'type', clause.start_point[0] + 1))
                elif clause.type == 'implements_clause':
                    saw_clause = True

                    for name in tsheritage(clause, source):
                        facts.uses.append(_SymbolUseFact(path, class_nid, name, 'implements', 'type', clause.start_point[0] + 1))

            if not saw_clause:
                for name in tsheritage(child, source):
                    facts.uses.append(_SymbolUseFact(path, class_nid, name, 'inherits', 'type', child.start_point[0] + 1))
        elif child.type == 'extends_type_clause':
            for name in tsheritage(child, source):
                facts.uses.append(_SymbolUseFact(path, class_nid, name, 'inherits', 'type', child.start_point[0] + 1))
    body = class_node.child_by_field_name('body')

    if body is None:
        return

    for member in body.children:
        m_line = member.start_point[0] + 1

        if member.type in ('method_definition', 'method_signature', 'abstract_method_signature'):
            name_node = member.child_by_field_name('name')

            if name_node is None:
                continue

            method_name = read(name_node, source)
            method_nid = make(class_nid, method_name)
            params = member.child_by_field_name('parameters')

            if params is not None:
                for p in params.children:
                    if p.type not in ('required_parameter', 'optional_parameter'):
                        continue

                    type_anno = p.child_by_field_name('type')

                    if type_anno is None:
                        continue

                    refs: list[tuple[str, str]] = []
                    tstyperefs(type_anno, source, False, refs)

                    for name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                        facts.uses.append(_SymbolUseFact(path, method_nid, name, 'references', ctx, m_line))
            return_type = member.child_by_field_name('return_type')

            if return_type is not None:
                refs = []
                tstyperefs(return_type, source, False, refs)

                for name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                    facts.uses.append(_SymbolUseFact(path, method_nid, name, 'references', ctx, m_line))
        elif member.type in ('public_field_definition', 'property_signature'):
            type_anno = None

            for c in member.children:
                if c.type == 'type_annotation':
                    type_anno = c
                    break

            if type_anno is None:
                continue

            refs = []
            tstyperefs(type_anno, source, False, refs)

            for name, role in refs:
                ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                facts.uses.append(_SymbolUseFact(path, class_nid, name, 'references', ctx, m_line))


def collectjsfacts(paths: list[Path], facts: _SymbolResolutionFacts) -> None:
    js_paths = [path for path in paths if path.suffix in _JS_CACHE_BYPASS_SUFFIXES]

    if not js_paths:
        return
    trees: dict[Path, tuple[bytes, object]] = {}

    for path in js_paths:
        resolved_path = resolvecached(path)
        parsed = parsejstree(path)

        if parsed is None:
            continue

        source, root_node = parsed
        trees[resolved_path] = parsed

        for node in walkjstree(root_node):
            if node.type == 'export_statement':
                for name in jsdeclarations(node, source):
                    facts.declarations.append(_SymbolDeclarationFact(path, name, node.start_point[0] + 1))

            if node.type != 'import_statement':
                continue

            raw_module = jsspecifier(node, source)

            if raw_module is None:
                continue

            target_path = jsmodulepath(raw_module, path.parent)

            if target_path is None:
                continue

            target_path = resolvecached(target_path)

            for imported_name, local_name in jsnamed(node, source, 'import_specifier'):
                facts.imports.append(_SymbolImportFact(path, local_name, target_path, imported_name, node.start_point[0] + 1))
            default_local = jsdefaultimport(node, source)

            if default_local is not None:
                facts.imports.append(_SymbolImportFact(path, default_local, target_path, 'default', node.start_point[0] + 1))

        for node in walkjstree(root_node):
            for alias, target in jslexicalaliases(node, source):
                facts.aliases.append(_SymbolAliasFact(path, alias, target, node.start_point[0] + 1))

    for path in js_paths:
        resolved_path = resolvecached(path)
        parsed = trees.get(resolved_path)

        if parsed is None:
            continue

        source, root_node = parsed

        for node in walkjstree(root_node):
            if node.type != 'export_statement':
                continue

            raw_module = jsspecifier(node, source)
            export_clause = jsexportclause(node)
            stmt_type_only = any((child.type == 'type' and (not child.is_named) for child in node.children))

            if raw_module is not None:
                target_path = jsmodulepath(raw_module, path.parent)

                if target_path is None:
                    continue

                target_path = resolvecached(target_path)
                namespace_name = jsnamespacename(node, source)

                if namespace_name is not None:
                    facts.namespace_exports.append(_NamespaceExportFact(path, namespace_name, target_path, node.start_point[0] + 1, type_only=stmt_type_only))
                elif jsisstar(node):
                    facts.star_exports.append(_StarExportFact(path, target_path, node.start_point[0] + 1, type_only=stmt_type_only))

                if export_clause is not None:
                    for original_name, exported_name in jsnamed(export_clause, source, 'export_specifier'):
                        facts.exports.append(_SymbolExportFact(path, exported_name, node.start_point[0] + 1, target_path=target_path, target_name=original_name, type_only=stmt_type_only))
                continue

            if export_clause is not None:
                for local_name, exported_name in jsnamed(export_clause, source, 'export_specifier'):
                    facts.exports.append(_SymbolExportFact(path, exported_name, node.start_point[0] + 1, local_name=local_name))
                continue

            for exported_name in jsdeclarations(node, source):
                facts.exports.append(_SymbolExportFact(path, exported_name, node.start_point[0] + 1, local_name=exported_name))
            default_name = jsdefaultexport(node, source)

            if default_name is not None:
                facts.exports.append(_SymbolExportFact(path, 'default', node.start_point[0] + 1, local_name=default_name))

    for path in js_paths:
        resolved_path = resolvecached(path)
        parsed = trees.get(resolved_path)

        if parsed is None:
            continue

        source, root_node = parsed

        for source_id, body in jsbodies(path, root_node, source):
            for node in walkjstree(body):
                imported_name = jscallident(node, source)

                if imported_name is None:
                    continue

                facts.uses.append(_SymbolUseFact(path, source_id, imported_name, 'calls', 'call', node.start_point[0] + 1))

    for path in js_paths:
        resolved_path = resolvecached(path)
        parsed = trees.get(resolved_path)

        if parsed is None:
            continue

        source, root_node = parsed
        stem = base.stem(path)

        for node in walkjstree(root_node):
            if node.type not in ('class_declaration', 'abstract_class_declaration', 'interface_declaration'):
                continue

            name_node = node.child_by_field_name('name')

            if name_node is None:
                continue

            class_name = read(name_node, source)

            if not class_name:
                continue

            class_nid = make(stem, class_name)
            tsmembers(node, source, path, class_nid, facts)


@functools.lru_cache(maxsize=2048)


def parsepycached(path_str: str, _mtime_ns: int, _size: int):
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser
    source = base.bytes(Path(path_str), warn=False)
    parser = Parser(Language(tspython.language()))

    return (source, parser.parse(source).root_node)


def parsepytree(path: Path):
    try:
        st = path.stat()

        return parsepycached(str(path), st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def walkpytree(node):
    stack = [node]

    while stack:
        current = stack.pop()

        yield current

        stack.extend(reversed(current.children))


def pyfrommodule(node, source: bytes) -> tuple[int, str] | None:
    level = 0
    module_name = ''

    for child in node.children:
        if child.type == 'import':
            break

        if child.type == 'relative_import':
            raw = read(child, source)
            level = len(raw) - len(raw.lstrip('.'))
            remainder = raw.lstrip('.')

            if remainder:
                module_name = remainder

            for sub in child.children:
                if sub.type == 'dotted_name':
                    module_name = read(sub, source)
        elif child.type == 'dotted_name':
            module_name = read(child, source)

    if level == 0 and (not module_name):
        return None

    return (level, module_name)


def pyimportnames(node, source: bytes) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    past_import = False

    for child in node.children:
        if child.type == 'import':
            past_import = True
            continue

        if not past_import:
            continue

        if child.type == 'dotted_name':
            name = read(child, source)
            names.append((name, name.split('.')[-1]))
        elif child.type == 'aliased_import':
            name_node = child.child_by_field_name('name')
            alias_node = child.child_by_field_name('alias')

            if name_node is None:
                continue

            name = read(name_node, source)
            local = read(alias_node, source) if alias_node is not None else name.split('.')[-1]
            names.append((name, local))

    return names


def probemodule(candidate: Path) -> Path | None:
    if candidate.is_dir():
        init_path = candidate / '__init__.py'

        if init_path.is_file():
            return init_path

    if candidate.is_file():
        return candidate

    if not candidate.name:
        return None

    py_candidate = candidate.with_suffix('.py')

    if py_candidate.is_file():
        return py_candidate

    return None

ROOTNAMESPACECACHE: dict[str, str] = {}


def inferroot(root: Path) -> str:
    key = str(resolvecached(root))
    cached_ns = ROOTNAMESPACECACHE.get(key)

    if cached_ns is not None:
        return cached_ns

    parts: list[str] = []
    current = resolvecached(root)
    parts.append(current.name)
    parent = current.parent

    while parent != current:
        if not (parent / '__init__.py').is_file():
            break

        parts.append(parent.name)
        current = parent
        parent = parent.parent

    if len(parts) <= 1:
        ROOTNAMESPACECACHE[key] = ''

        return ''

    parts.reverse()
    namespace = '.'.join(parts)
    ROOTNAMESPACECACHE[key] = namespace

    return namespace


def resolvepymodule(module_name: str, current_path: Path, root: Path, level: int) -> Path | None:
    if level > 0:
        base = current_path.parent

        for _ in range(level - 1):
            base = base.parent
        candidate = base / module_name.replace('.', '/') if module_name else base

        return probemodule(candidate)

    rel = module_name.replace('.', '/')
    hit = probemodule(root / rel)

    if hit is not None:
        return hit

    ns = inferroot(root)

    if ns and (module_name == ns or module_name.startswith(ns + '.')):
        stripped = module_name[len(ns) + 1:] if module_name != ns else ''

        if stripped:
            hit = probemodule(root / stripped.replace('.', '/'))

            if hit is not None:
                return hit
        else:
            hit = probemodule(root)

            if hit is not None:
                return hit

    for anc in current_path.parents:
        try:
            anc.relative_to(root)
        except ValueError:
            break

        if anc == root:
            continue

        if (anc / '__init__.py').is_file():
            continue

        cand = probemodule(anc / rel)

        if cand is not None:
            return cand

    return None


def resolvepyns(module_name: str, current_path: Path, root: Path, level: int) -> 'Path | None':

    def namespace(candidate: Path) -> 'Path | None':
        if not candidate.is_dir() or (candidate / '__init__.py').is_file():
            return None

        try:
            resolvecached(candidate).relative_to(resolvecached(root))
        except ValueError:
            return None

        return candidate

    if level > 0:
        base = current_path.parent

        for _ in range(level - 1):
            base = base.parent

        return namespace(base / module_name.replace('.', '/') if module_name else base)

    if not module_name:
        return None

    rel = module_name.replace('.', '/')
    hit = namespace(root / rel)

    if hit is not None:
        return hit

    ns = inferroot(root)

    if ns and (module_name == ns or module_name.startswith(ns + '.')):
        stripped = module_name[len(ns) + 1:] if module_name != ns else ''

        if stripped:
            hit = _namespace(root / stripped.replace('.', '/'))

            if hit is not None:
                return hit
        else:
            hit = _namespace(root)

            if hit is not None:
                return hit

    for anc in current_path.parents:
        try:
            anc.relative_to(root)
        except ValueError:
            break

        if anc == root or (anc / '__init__.py').is_file():
            continue

        hit = _namespace(anc / rel)

        if hit is not None:
            return hit

    return None


def pybodies(path: Path, root_node, source: bytes) -> list[tuple[str, object]]:
    bodies: list[tuple[str, object]] = []
    stem = base.stem(path)

    for node in root_node.children:
        if node.type != 'function_definition':
            continue

        name_node = node.child_by_field_name('name')
        body = node.child_by_field_name('body')

        if name_node is not None and body is not None:
            bodies.append((make(stem, read(name_node, source)), body))

    return bodies


def pycallident(node, source: bytes) -> str | None:
    if node.type != 'call':
        return None

    function_node = node.child_by_field_name('function')

    if function_node is not None and function_node.type == 'identifier':
        return read(function_node, source)

    return None


def collectpyfacts(paths: list[Path], root: Path, facts: _SymbolResolutionFacts, ambiguous_python_modules: set[str] | None=None) -> None:
    ambiguous_python_modules = ambiguous_python_modules or set()
    py_paths = [path for path in paths if path.suffix == '.py']

    if not py_paths:
        return
    trees: dict[Path, tuple[bytes, object]] = {}

    for path in py_paths:
        parsed = parsepytree(path)

        if parsed is None:
            continue

        source, root_node = parsed
        trees[resolvecached(path)] = parsed

        for node in walkpytree(root_node):
            if node.type != 'import_from_statement':
                continue

            module = pyfrommodule(node, source)

            if module is None:
                continue

            level, module_name = module

            if level == 0 and make(module_name) in ambiguous_python_modules:
                continue

            target_path = resolvepymodule(module_name, path, root, level)

            if target_path is not None:
                pkg_dir = target_path.parent if target_path.name == '__init__.py' else None
            else:
                pkg_dir = resolvepyns(module_name, path, root, level)

                if pkg_dir is None:
                    continue

            for imported_name, local_name in pyimportnames(node, source):
                line = node.start_point[0] + 1

                if pkg_dir is not None:
                    sub_py = pkg_dir / f'{imported_name}.py'
                    sub_pkg = pkg_dir / imported_name / '__init__.py'
                    submodule = sub_py if sub_py.is_file() else sub_pkg if sub_pkg.is_file() else None

                    if submodule is not None:
                        imported_module = f'{module_name}.{imported_name}' if module_name else imported_name

                        if level == 0 and make(imported_module) in ambiguous_python_modules:
                            continue

                        facts.module_imports.append((path, submodule, line, local_name))
                        continue

                if target_path is None:
                    continue

                facts.imports.append(_SymbolImportFact(path, local_name, target_path, imported_name, line))

                if path.name == '__init__.py':
                    facts.exports.append(_SymbolExportFact(path, local_name, line, target_path=target_path, target_name=imported_name))

    for path in py_paths:
        parsed = trees.get(resolvecached(path))

        if parsed is None:
            continue

        source, root_node = parsed

        for source_id, body in pybodies(path, root_node, source):
            for node in walkpytree(body):
                imported_name = pycallident(node, source)

                if imported_name is None:
                    continue

                facts.uses.append(_SymbolUseFact(path, source_id, imported_name, 'calls', 'call', node.start_point[0] + 1))


def augmentedges(paths: list[Path], nodes: list[dict], edges: list[dict], root: Path, ambiguous_python_modules: set[str] | None=None, resolution_context_nodes: list[dict] | None=None) -> None:
    facts = _SymbolResolutionFacts()
    collectjsfacts(paths, facts)
    collectpyfacts(paths, root, facts, ambiguous_python_modules=ambiguous_python_modules)

    if resolution_context_nodes:
        applyfacts(paths, nodes, edges, root, facts, resolution_context_nodes=resolution_context_nodes)
    else:
        applyfacts(paths, nodes, edges, root, facts)


def resolvecrossimports(per_file: list[dict], paths: list[Path], all_nodes: list[dict] | None=None, all_edges: list[dict] | None=None, ambiguous_python_modules: set[str] | None=None) -> list[dict]:
    try:
        import tree_sitter_python
    except ImportError:
        return []

    ambiguous_python_modules = ambiguous_python_modules or set()
    stem_to_entities: dict[str, dict[str, str]] = {}
    bare_to_qualified: dict[str, str] = {}

    for file_result in per_file:
        for node in file_result.get('nodes', []):
            src = node.get('source_file', '')

            if not src:
                continue

            src_path = Path(src)
            fq_stem = stem(src_path)
            label = node.get('label', '')
            nid = node.get('id', '')

            if label and (not label.endswith((')', '.py'))) and ('_' not in label[:1]):
                stem_to_entities.setdefault(fq_stem, {})[label] = nid

                if src_path.stem not in bare_to_qualified:
                    bare_to_qualified[src_path.stem] = fq_stem
    new_edges: list[dict] = []
    node_by_id = {node['id']: node for node in all_nodes if node.get('id')} if all_nodes else {}
    repointed_from: set[str] = set()
    TYPE_REPOINT_RELATIONS = frozenset({'references', 'inherits', 'implements', 'extends'})

    for file_result, path in zip(per_file, paths):
        str_path = str(path)
        file_srcs = {n.get('source_file') for n in file_result.get('nodes', []) if n.get('source_file')}
        file_srcs.add(str_path)
        file_srcs.add(path.as_posix())
        own_stems = {stem(Path(s)) for s in file_srcs}
        name_to_nid: dict[str, str] = {}

        for n in file_result.get('nodes', []):
            if n.get('source_file') != str_path:
                continue

            label = n.get('label', '')

            if not label or label.endswith('.py'):
                continue

            sym_name = label[:-2] if label.endswith('()') else label

            if sym_name and sym_name not in name_to_nid:
                name_to_nid[sym_name] = n['id']

        if not name_to_nid:
            continue

        parsed = parsepytree(path)

        if parsed is None:
            continue

        source, root_node = parsed
        import_targets: dict[str, str] = {}
        ref_sources: dict[str, dict[str, int]] = {}

        def text(n) -> str:
            return source[n.start_byte:n.end_byte].decode('utf-8', errors='replace')

        def resolveimport(node) -> None:
            target_fq: str | None = None
            absolute_module: str | None = None

            for child in node.children:
                if child.type == 'relative_import':
                    prefix_text = ''
                    dotted_text = ''

                    for sub in child.children:
                        if sub.type == 'import_prefix':
                            prefix_text = text(sub)
                        elif sub.type == 'dotted_name':
                            dotted_text = text(sub)
                    dots = prefix_text.count('.') if prefix_text else 1
                    cur_dir = path.parent

                    for _ in range(dots - 1):
                        cur_dir = cur_dir.parent

                    if dotted_text:
                        candidate = cur_dir.joinpath(*dotted_text.split('.')).with_suffix('.py')
                    else:
                        candidate = cur_dir / '__init__.py'
                    target_fq = stem(candidate)
                    break

                if child.type == 'dotted_name' and target_fq is None:
                    dotted_name = text(child)
                    absolute_module = dotted_name

                    if make(dotted_name) in ambiguous_python_modules:
                        return
                    dotted_as_path = '/'.join(dotted_name.split('.'))

                    if dotted_as_path in stem_to_entities:
                        target_fq = dotted_as_path
                    else:
                        suffix_matches = [fq for fq in stem_to_entities if fq.endswith(f'/{dotted_as_path}') or fq.endswith(f'\\{dotted_as_path}')]

                        if len(suffix_matches) == 1:
                            target_fq = suffix_matches[0]
                        else:
                            bare = dotted_name.split('.')[-1]
                            target_fq = bare_to_qualified.get(bare)

            if not target_fq or target_fq not in stem_to_entities:
                return

            if target_fq in own_stems:
                return
            past_import_kw = False

            for child in node.children:
                if child.type == 'import':
                    past_import_kw = True
                    continue

                if not past_import_kw:
                    continue

                imported_name: str | None = None
                local_name: str | None = None

                if child.type == 'dotted_name':
                    imported_name = local_name = text(child)
                elif child.type == 'aliased_import':
                    name_node = child.child_by_field_name('name')
                    alias_node = child.child_by_field_name('alias')

                    if name_node is not None:
                        imported_name = text(name_node)
                        local_name = text(alias_node) if alias_node is not None else imported_name

                if not imported_name or not local_name:
                    continue

                if absolute_module is not None and make(f'{absolute_module}.{imported_name}') in ambiguous_python_modules:
                    continue

                tgt_nid = stem_to_entities[target_fq].get(imported_name)

                if tgt_nid:
                    import_targets[local_name] = tgt_nid

        def visit(node, current_nid: str | None) -> None:
            if node.type == 'import_from_statement':
                resolveimport(node)
                return

            if current_nid is None and node.type in ('class_definition', 'function_definition'):
                name_node = node.child_by_field_name('name')

                if name_node is not None:
                    mapped = name_to_nid.get(text(name_node))

                    if mapped is not None:
                        current_nid = mapped

            if node.type == 'identifier' and current_nid is not None:
                slot = ref_sources.setdefault(text(node), {})
                slot.setdefault(current_nid, node.start_point[0] + 1)

            for child in node.children:
                visit(child, current_nid)
        visit(root_node, None)

        for name, tgt_nid in import_targets.items():
            for src_nid, line in ref_sources.get(name, {}).items():
                if src_nid == tgt_nid:
                    continue

                new_edges.append({'source': src_nid, 'target': tgt_nid, 'relation': 'uses', 'source_file': str_path, 'source_location': f'L{line}'})

        if all_edges and import_targets:
            for edge in all_edges:
                if edge.get('source_file') not in file_srcs:
                    continue

                if edge.get('relation') not in TYPE_REPOINT_RELATIONS:
                    continue

                tgt = edge.get('target')
                tgt_node = node_by_id.get(tgt)

                if not tgt_node or tgt_node.get('source_file'):
                    continue

                stub_label = tgt_node.get('label', '')
                resolved_id = import_targets.get(stub_label)

                if resolved_id and resolved_id != tgt:
                    edge['target'] = resolved_id
                    repointed_from.add(tgt)

    if all_nodes is not None and all_edges is not None and repointed_from:
        still_referenced = {e.get('source') for e in all_edges} | {e.get('target') for e in all_edges}
        all_nodes[:] = [node for node in all_nodes if node.get('id') not in repointed_from or node.get('id') in still_referenced]

    return new_edges

DECLDEFHEADERS = frozenset({'.h', '.hpp', '.hh', '.hxx'})
DECLDEFIMPLS = frozenset({'.m', '.mm', '.cpp', '.cc', '.cxx', '.c'})


def decldefstem(source_file: str) -> tuple[str, str] | None:
    if not source_file:
        return None

    p = Path(source_file)
    suffix = p.suffix.lower()

    if suffix not in DECLDEFHEADERS and suffix not in DECLDEFIMPLS:
        return None

    stem = p.stem.split('+', 1)[0]

    if not stem:
        return None

    return (str(p.parent), stem)


def sourcestem(node: dict) -> str:
    return Path(str(node.get('source_file', ''))).stem


def mergedecldef(all_nodes: list[dict], all_edges: list[dict]) -> None:
    by_id: dict[str, list[dict]] = {}

    for n in all_nodes:
        if n.get('file_type') != 'code':
            continue

        nid = n.get('id')
        sf = str(n.get('source_file', ''))

        if not isinstance(nid, str) or not nid or (not sf):
            continue

        by_id.setdefault(nid, []).append(n)
    drop_objs: set[int] = set()

    for nid, group in by_id.items():
        if len(group) < 2:
            continue

        sibling_keys: set[tuple[str, str]] = set()
        headers: list[dict] = []
        ok = True

        for node in group:
            sf = str(node.get('source_file', ''))
            ds = decldefstem(sf)

            if ds is None:
                ok = False
                break

            sibling_keys.add(ds)

            if Path(sf).suffix.lower() in DECLDEFHEADERS:
                headers.append(node)

        if not ok:
            continue

        if len(sibling_keys) != 1 or not headers:
            continue

        if len(headers) == 1:
            keeper = headers[0]
        else:
            base_headers = [h for h in headers if '+' not in sourcestem(h)]

            if len(base_headers) > 1:
                continue

            keeper = base_headers[0] if base_headers else min(headers, key=sourcestem)
        impls = sorted((n for n in group if n is not keeper and Path(str(n.get('source_file', ''))).suffix.lower() in DECLDEFIMPLS), key=lambda n: (str(n.get('source_file', '')), str(n.get('source_location', ''))))

        if impls:
            definition = impls[0]

            if definition.get('source_file'):
                keeper['definition_file'] = definition['source_file']

            if definition.get('source_location'):
                keeper['definition_location'] = definition['source_location']

        for node in group:
            if node is not keeper:
                drop_objs.add(id(node))

    if not drop_objs:
        return
    all_nodes[:] = [n for n in all_nodes if id(n) not in drop_objs]
    seen_keys: set[tuple] = set()
    rewritten: list[dict] = []

    for e in all_edges:
        src = e.get('source')
        tgt = e.get('target')

        if src == tgt:
            continue

        k = (src, tgt, e.get('relation'), e.get('context'))

        if k in seen_keys:
            continue

        seen_keys.add(k)
        rewritten.append(e)
    all_edges[:] = rewritten


def resolvejavaimports(per_file: list[dict], paths: list[Path]) -> list[dict]:
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return []

    language = Language(tsjava.language())
    parser = Parser(language)
    parsed: dict[str, tuple[bytes, object]] = {}
    pkg_by_src: dict[str, str] = {}

    for path, file_result in zip(paths, per_file):
        try:
            source = base.bytes(path, warn=False)
            tree = parser.parse(source)
        except Exception:
            continue

        parsed[str(path)] = (source, tree)
        pkg = ''

        for child in tree.root_node.children:
            if child.type == 'package_declaration':
                pkg = read(child, source).strip()[len('package'):].strip().rstrip(';').strip()
                break

        pkg_by_src[str(path)] = pkg

        for node in file_result.get('nodes', []):
            src = node.get('source_file')

            if src:
                pkg_by_src.setdefault(src, pkg)

    def pkgmatches(imp_pkg: str, tgt_pkg: str) -> bool:
        if imp_pkg == tgt_pkg:
            return True

        if tgt_pkg:
            if not imp_pkg.startswith(tgt_pkg + '.'):
                return False

            rest = imp_pkg[len(tgt_pkg) + 1:]
        else:
            rest = imp_pkg

        return bool(rest) and all((seg[:1].isupper() for seg in rest.split('.')))

    name_to_ids: dict[str, list[tuple[str, str]]] = {}

    for file_result in per_file:
        for node in file_result.get('nodes', []):
            label = node.get('label', '')
            nid = node.get('id', '')
            src = node.get('source_file', '')

            if not label or not nid or (not src):
                continue

            if label.endswith(')') or label.endswith('.java'):
                continue

            if not label[0].isalpha() or not label[0].isupper():
                continue

            name_to_ids.setdefault(label, []).append((nid, pkg_by_src.get(src, '')))
    new_edges: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    for path in paths:
        file_nid = make(str(path))
        entry = parsed.get(str(path))

        if entry is None:
            continue

        source, tree = entry

        def walk(n) -> None:
            if n.type == 'import_declaration':
                raw = read(n, source).strip()
                body = raw[len('import'):].strip().rstrip(';').strip()

                if body.startswith('static '):
                    body = body[len('static '):].strip()

                if body.endswith('.*'):
                    return
                parts = body.split('.')

                if not parts:
                    return
                last = parts[-1]
                imp_pkg = '.'.join(parts[:-1])

                if last and last[0].islower() and (len(parts) >= 2):
                    last = parts[-2]
                    imp_pkg = '.'.join(parts[:-2])
                at_line = n.start_point[0] + 1

                for tgt_nid, tgt_pkg in name_to_ids.get(last, []):
                    if tgt_nid == file_nid:
                        continue

                    if not pkgmatches(imp_pkg, tgt_pkg):
                        continue

                    key = (file_nid, tgt_nid)

                    if key in seen_pairs:
                        continue

                    seen_pairs.add(key)
                    new_edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'source_file': str(path), 'source_location': f'L{at_line}'})

            for child in n.children:
                walk(child)
        walk(tree.root_node)

    return new_edges


def goimportpath(source_file: str | Path, root: Path, module_cache: dict[Path, str | None] | None=None) -> str | None:
    cache = module_cache if module_cache is not None else {}
    path = Path(source_file)

    if not path.is_absolute():
        path = root / path

    try:
        directory = resolvecached(path).parent
    except OSError:
        directory = path.absolute().parent
    module_dir: Path | None = None
    module_path: str | None = None

    for candidate in (directory, *directory.parents):
        if candidate in cache:
            cached = cache[candidate]

            if cached:
                module_dir, module_path = (candidate, cached)
            break

        go_mod = candidate / 'go.mod'

        if not go_mod.is_file():
            continue

        try:
            match = re.search('(?m)^\\s*module\\s+([^\\s]+)', go_mod.read_text(encoding='utf-8'))
        except (OSError, UnicodeError):
            match = None
        module_dir = candidate
        module_path = match.group(1) if match else None
        cache[candidate] = module_path
        break

    if not module_dir or not module_path:
        return None

    try:
        relative_dir = directory.relative_to(module_dir)
    except ValueError:
        return None

    suffix = relative_dir.as_posix()

    return module_path if suffix == '.' else f'{module_path}/{suffix}'


def resolvegotypes(per_file: list[dict], paths: list[Path], all_nodes: list[dict], all_edges: list[dict], root: Path, resolution_context_nodes: list[dict] | None=None, resolution_context_edges: list[dict] | None=None) -> None:
    imports_by_file: dict[str, dict[str, str]] = {}
    actual_path_by_file: dict[str, Path] = {}

    for path, result in zip(paths, per_file):
        imports = result.get('go_imports') or {}

        for node in result.get('nodes', []):
            source_file = node.get('source_file')

            if source_file:
                imports_by_file[str(source_file)] = imports
                actual_path_by_file[str(source_file)] = path

    if not imports_by_file:
        return
    definition_nodes = all_nodes + (resolution_context_nodes or [])
    definition_edges = all_edges + (resolution_context_edges or [])
    contained = {edge.get('target') for edge in definition_edges if edge.get('relation') == 'contains'}
    module_cache: dict[Path, str | None] = {}
    fqn_to_ids: dict[str, list[str]] = {}

    for node in definition_nodes:
        source_file = str(node.get('source_file') or '')
        label = str(node.get('label') or '')
        nid = node.get('id')

        if not source_file or not label or (not nid) or (nid not in contained) or (not istypelike(node)):
            continue

        actual_path = actual_path_by_file.get(source_file, Path(source_file))
        package_path = goimportpath(actual_path, root, module_cache)

        if package_path:
            fqn_to_ids.setdefault(f'{package_path}.{label}', []).append(nid)
    qualified_stubs = {node['id']: str(node.get('label') or '') for node in all_nodes if node.get('id') and (not node.get('source_file')) and ('.' in str(node.get('label') or ''))}

    if not qualified_stubs:
        return
    node_ids = {node.get('id') for node in all_nodes if node.get('id')}
    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def externalstub(fqn: str) -> str:
        existing = external_stub_ids.get(fqn)

        if existing:
            return existing

        nid = make('go', 'type', fqn)

        if nid not in node_ids:
            new_nodes.append({'id': nid, 'label': fqn, 'file_type': 'code', 'source_file': '', 'source_location': ''})
            node_ids.add(nid)
        external_stub_ids[fqn] = nid

        return nid

    repointed_from: set[str] = set()

    for edge in all_edges:
        if edge.get('relation') not in {'references', 'embeds'}:
            continue

        target = edge.get('target')
        qualified = qualified_stubs.get(target)

        if not qualified:
            continue

        alias, _, type_name = qualified.rpartition('.')
        import_path = imports_by_file.get(str(edge.get('source_file') or ''), {}).get(alias)

        if not import_path or not type_name:
            continue

        fqn = f'{import_path}.{type_name}'
        candidates = fqn_to_ids.get(fqn, [])
        edge['target'] = candidates[0] if len(candidates) == 1 else externalstub(fqn)
        repointed_from.add(str(target))

    if new_nodes:
        all_nodes.extend(new_nodes)

    if not repointed_from:
        return
    referenced = {endpoint for edge in all_edges for endpoint in (edge.get('source'), edge.get('target'))}
    all_nodes[:] = [node for node in all_nodes if node.get('id') not in repointed_from or node.get('id') in referenced]


def resolvejavatypes(per_file: list[dict], paths: list[Path], all_nodes: list[dict], all_edges: list[dict]) -> None:
    try:
        import tree_sitter_java as tsjava
        from tree_sitter import Language, Parser
    except ImportError:
        return
    language = Language(tsjava.language())
    parser = Parser(language)
    pkg_by_file: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, str]] = {}

    for path, result in zip(paths, per_file):
        srcs = {n.get('source_file') for n in result.get('nodes', []) if n.get('source_file')}

        if not srcs:
            continue

        try:
            source = base.bytes(path, warn=False)
            tree = parser.parse(source)
        except Exception:
            continue

        pkg = ''
        imps: dict[str, str] = {}

        def walk(n) -> None:
            nonlocal pkg

            if n.type == 'package_declaration':
                pkg = read(n, source).strip()[len('package'):].strip().rstrip(';').strip()
            elif n.type == 'import_declaration':
                body = read(n, source).strip()[len('import'):].strip().rstrip(';').strip()

                if body.startswith('static '):
                    body = body[len('static '):].strip()

                if body.endswith('.*') or '.' not in body:
                    return
                simple = body.split('.')[-1]

                if simple and simple[0].isupper():
                    imps[simple] = body

            for child in n.children:
                walk(child)
        walk(tree.root_node)

        for s in srcs:
            pkg_by_file[s] = pkg
            imports_by_file[s] = imps
    fqn_to_id: dict[str, str] = {}
    node_by_id = {node.get('id'): node for node in all_nodes if node.get('id')}
    type_parent_by_id: dict[str, str] = {}

    for edge in all_edges:
        if edge.get('relation') != 'contains':
            continue

        child = node_by_id.get(edge.get('target'))
        parent = node_by_id.get(edge.get('source'))

        if not child or not parent:
            continue

        child_src = child.get('source_file', '')
        parent_label = parent.get('label', '')

        if child_src and parent.get('source_file') == child_src and parent_label[:1].isupper() and (not parent_label.endswith('.java')):
            type_parent_by_id[child['id']] = parent['id']

    for node in all_nodes:
        label = node.get('label', '')
        src = node.get('source_file', '')
        nid = node.get('id', '')

        if not (label and src and nid) or src not in pkg_by_file:
            continue

        if not label[:1].isupper() or label.endswith(')') or label.endswith('.java'):
            continue

        pkg = pkg_by_file[src]
        fqn_to_id.setdefault(f'{pkg}.{label}' if pkg else label, nid)
        type_path = [label]
        seen = {nid}
        parent_id = type_parent_by_id.get(nid)

        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = node_by_id[parent_id]
            type_path.append(parent['label'])
            parent_id = type_parent_by_id.get(parent_id)

        if len(type_path) > 1:
            nested_name = '.'.join(reversed(type_path))
            fqn_to_id.setdefault(f'{pkg}.{nested_name}' if pkg else nested_name, nid)
    stub_label: dict[str, str] = {node['id']: node.get('label', '') for node in all_nodes if node.get('id') and (not node.get('source_file')) and (node.get('label', '')[:1].isupper() or '.' in node.get('label', ''))}

    if not stub_label:
        return
    REPOINT_RELATIONS = {'implements', 'inherits', 'extends', 'imports', 'references'}
    node_ids = {n.get('id') for n in all_nodes if n.get('id')}
    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def extstub(fqn: str) -> str:
        nid = external_stub_ids.get(fqn)

        if nid:
            return nid

        nid = make(fqn)

        if nid not in node_ids:
            new_nodes.append({'id': nid, 'label': fqn, 'file_type': 'code', 'source_file': '', 'source_location': ''})
            node_ids.add(nid)
        external_stub_ids[fqn] = nid

        return nid

    repointed_from: set[str] = set()

    for edge in all_edges:
        if edge.get('relation') not in REPOINT_RELATIONS:
            continue

        tgt = edge.get('target')
        label = stub_label.get(tgt)

        if not label:
            continue

        ref_file = edge.get('source_file', '')

        if '.' in label:
            resolved = fqn_to_id.get(label)

            if resolved and resolved != tgt:
                edge['target'] = resolved
                repointed_from.add(tgt)
            continue

        fqn = imports_by_file.get(ref_file, {}).get(label)

        if fqn:
            resolved = fqn_to_id.get(fqn)

            if resolved is None:
                head = fqn.split('.')[:-1]

                while resolved is None and head and head[-1][:1].isupper():
                    head.pop()
                    resolved = fqn_to_id.get('.'.join(head + [label]))

            if resolved is None:
                edge['target'] = extstub(fqn)
                repointed_from.add(tgt)
                continue
        else:
            pkg = pkg_by_file.get(ref_file, '')
            resolved = fqn_to_id.get(f'{pkg}.{label}' if pkg else label)

        if resolved and resolved != tgt:
            edge['target'] = resolved
            repointed_from.add(tgt)

    if new_nodes:
        all_nodes.extend(new_nodes)
    seen_attribute_refs: set[tuple] = set()
    deduped_edges: list[dict] = []

    for edge in all_edges:
        if edge.get('relation') == 'references' and edge.get('context') == 'attribute' and (edge.get('source_file', '') in pkg_by_file):
            key = (edge.get('source'), edge.get('target'), edge.get('relation'), edge.get('context'), edge.get('source_file'), edge.get('source_location'))

            if key in seen_attribute_refs:
                continue

            seen_attribute_refs.add(key)
        deduped_edges.append(edge)
    all_edges[:] = deduped_edges

    if not repointed_from:
        return
    still_referenced: set[str] = set()

    for edge in all_edges:
        still_referenced.add(edge.get('source'))
        still_referenced.add(edge.get('target'))
    all_nodes[:] = [node for node in all_nodes if node.get('id') not in repointed_from or node.get('id') in still_referenced]
_PHP_SUPERTYPE_RELATIONS = ('inherits', 'implements', 'mixes_in')
_PHP_REPOINT_RELATIONS = frozenset({'inherits', 'implements', 'mixes_in', 'imports', 'references'})


def phpfqn(raw: str, ns: str, uses: dict[str, str]) -> str:
    raw = raw.strip()

    if raw.startswith('\\'):
        return raw[1:]

    if '\\' in raw:
        first, rest = raw.split('\\', 1)
        mapped = uses.get(first.lower())

        if mapped:
            return f'{mapped}\\{rest}'

        return f'{ns}\\{raw}' if ns else raw

    mapped = uses.get(raw.lower())

    if mapped:
        return mapped

    return f'{ns}\\{raw}' if ns else raw


def resolvephptypes(per_file: list[dict], paths: list[Path], all_nodes: list[dict], all_edges: list[dict]) -> None:
    try:
        import tree_sitter_php as tsphp
        from tree_sitter import Language, Parser
    except ImportError:
        return
    lang_fn = getattr(tsphp, 'language_php', None) or getattr(tsphp, 'language', None)

    if lang_fn is None:
        return
    language = Language(lang_fn())
    parser = Parser(language)
    ns_by_file: dict[str, str] = {}
    uses_by_file: dict[str, dict[str, str]] = {}
    raw_by_file: dict[str, dict[tuple[str, str], str | None]] = {}

    for path, result in zip(paths, per_file):
        srcs = {n.get('source_file') for n in result.get('nodes', []) if n.get('source_file')}

        if not srcs:
            continue

        try:
            source = base.bytes(path, warn=False)
            tree = parser.parse(source)
        except Exception:
            continue

        namespaces: list[str] = []
        uses: dict[str, str] = {}
        raws: dict[tuple[str, str], str | None] = {}

        def recordraw(relation: str, raw: str) -> None:
            bare = raw.rsplit('\\', 1)[-1].strip().lower()

            if not bare:
                return
            key = (relation, bare)

            if key in raws and raws[key] != raw:
                raws[key] = None
            else:
                raws.setdefault(key, raw)

        def recorduse(clause, prefix: str) -> None:
            target = None
            alias = None
            saw_as = False

            for c in clause.children:
                if c.type in ('function', 'const'):
                    return

                if c.type == 'as':
                    saw_as = True
                elif c.type in ('qualified_name', 'name'):
                    if saw_as:
                        alias = read(c, source)
                    elif target is None:
                        target = read(c, source)

            if not target:
                return
            fqn = (f'{prefix}\\{target}' if prefix else target).lstrip('\\')
            key = (alias or fqn.rsplit('\\', 1)[-1]).strip().lower()

            if key:
                uses.setdefault(key, fqn)

        def walk(n) -> None:
            t = n.type

            if t == 'namespace_definition':
                for c in n.children:
                    if c.type == 'namespace_name':
                        namespaces.append(read(c, source))
                        break
            elif t == 'namespace_use_declaration':
                prefix = ''
                group = None

                for c in n.children:
                    if c.type == 'namespace_name':
                        prefix = read(c, source)
                    elif c.type == 'namespace_use_group':
                        group = c
                    elif c.type == 'namespace_use_clause':
                        recorduse(c, '')

                if group is not None:
                    for c in group.children:
                        if c.type == 'namespace_use_clause':
                            recorduse(c, prefix)
                return
            elif t == 'class_declaration':
                for child in n.children:
                    if child.type == 'base_clause':
                        for sub in child.children:
                            if sub.type in ('name', 'qualified_name'):
                                recordraw('inherits', read(sub, source))
                    elif child.type == 'class_interface_clause':
                        for sub in child.children:
                            if sub.type in ('name', 'qualified_name'):
                                recordraw('implements', read(sub, source))
                    elif child.type == 'declaration_list':
                        for member in child.children:
                            if member.type != 'use_declaration':
                                continue

                            for sub in member.children:
                                if sub.type in ('name', 'qualified_name'):
                                    recordraw('mixes_in', read(sub, source))

            for child in n.children:
                walk(child)
        walk(tree.root_node)

        if len(set(namespaces)) > 1:
            continue

        ns = namespaces[0] if namespaces else ''

        for s in srcs:
            ns_by_file[s] = ns
            uses_by_file[s] = uses
            raw_by_file[s] = raws

    if not ns_by_file:
        return
    fqn_to_id: dict[str, str] = {}

    for node in all_nodes:
        label = node.get('label', '')
        src = node.get('source_file', '')
        nid = node.get('id', '')

        if not (label and src and nid) or src not in ns_by_file:
            continue

        if label.endswith(')') or '.' in label:
            continue

        ns = ns_by_file[src]
        fqn = f'{ns}\\{label}' if ns else label
        fqn_to_id.setdefault(fqn.lower(), nid)
    node_ids = {n.get('id') for n in all_nodes if n.get('id')}
    stub_label: dict[str, str] = {n['id']: n.get('label', '') for n in all_nodes if n.get('id') and (not n.get('source_file')) and n.get('label')}
    external_stub_ids: dict[str, str] = {}
    new_nodes: list[dict] = []

    def extstub(fqn: str) -> str:
        key = fqn.lower()
        nid = external_stub_ids.get(key)

        if nid:
            return nid

        nid = make(fqn)

        if nid not in node_ids:
            new_nodes.append({'id': nid, 'label': fqn, 'file_type': 'code', 'source_file': '', 'source_location': ''})
            node_ids.add(nid)
        external_stub_ids[key] = nid

        return nid

    repointed_from: set[str] = set()

    for edge in all_edges:
        relation = edge.get('relation')

        if relation not in _PHP_REPOINT_RELATIONS:
            continue

        ref_file = edge.get('source_file', '')

        if ref_file not in ns_by_file:
            continue

        tgt = edge.get('target')
        label = stub_label.get(tgt)
        uses = uses_by_file.get(ref_file, {})

        if not label and relation == 'imports':
            label = next((alias for alias in uses if make(alias) == tgt), '')

        if not label:
            continue

        bare = label.strip().lower()
        ns = ns_by_file[ref_file]
        raw = None

        if relation in _PHP_SUPERTYPE_RELATIONS:
            raw = raw_by_file.get(ref_file, {}).get((relation, bare))
        explicit = False

        if raw and '\\' in raw:
            fqn = phpfqn(raw, ns, uses)
            explicit = True
        elif bare in uses:
            fqn = uses[bare]
            explicit = True
        elif ns:
            fqn = f'{ns}\\{label}'
        else:
            continue

        resolved = fqn_to_id.get(fqn.lower())

        if resolved and resolved != tgt:
            edge['target'] = resolved
            repointed_from.add(tgt)
        elif explicit and resolved is None:
            edge['target'] = extstub(fqn)
            repointed_from.add(tgt)

    if new_nodes:
        all_nodes.extend(new_nodes)

    if not repointed_from:
        return
    still_referenced: set[str] = set()

    for edge in all_edges:
        still_referenced.add(edge.get('source'))
        still_referenced.add(edge.get('target'))
    all_nodes[:] = [n for n in all_nodes if n.get('id') not in repointed_from or n.get('id') in still_referenced]
_pascal_unit_cache: dict[str, dict[str, str]] = {}
_pascal_class_stem_cache: dict[str, dict[str, str]] = {}


def pascalroot(from_path: Path) -> Path:
    best = from_path.parent
    current = from_path.parent

    for _ in range(12):
        if len(current.parts) <= 1:
            break

        pas_count = sum((1 for _ in current.glob('*.pas')))
        dpr_count = sum((1 for _ in current.glob('*.dpr')))

        if pas_count >= 2 or dpr_count >= 1:
            best = current
        parent = current.parent

        if parent == current:
            break

        current = parent

    return best


def pascalunit(from_path: Path, unit_name: str) -> str:
    root = pascalroot(from_path)
    root_key = str(root)

    if root_key not in _pascal_unit_cache:
        unit_map: dict[str, str] = {}

        for ext in ('.pas', '.pp', '.dpr', '.dpk', '.inc'):
            for f in root.rglob('*' + ext):
                unit_map[f.stem.lower()] = make(str(f))
        _pascal_unit_cache[root_key] = unit_map

    return _pascal_unit_cache[root_key].get(unit_name.lower(), make(unit_name))


def pascalclass(from_path: Path, class_name: str) -> str | None:
    prefix = class_name[:1]
    unit_name = class_name[1:] if prefix in ('T', 'I') else class_name
    root = pascalroot(from_path)
    root_key = str(root)

    if root_key not in _pascal_class_stem_cache:
        stem_map: dict[str, str] = {}

        for ext in ('.pas', '.pp', '.dpr', '.dpk'):
            for f in root.rglob('*' + ext):
                stem_map[f.stem.lower()] = stem(f)
        _pascal_class_stem_cache[root_key] = stem_map
    file_stem = _pascal_class_stem_cache[root_key].get(unit_name.lower())

    if file_stem:
        return make(file_stem, class_name)

    return None


import ast
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from graphify.src.base import text as _read_source_text
from graphify.ids import make as make
from graphify.paths import disambiguate
from graphify.security import metadata


@dataclass(frozen=True)


class ImportedSymbol:
    local_name: str
    imported_name: str
    module_stem: str
    source_file: str
    source_location: str


def normlabel(label: str) -> str:
    return label.strip().strip('()').lstrip('.').lower()


def resolvablesymbol(node: dict[str, Any]) -> bool:
    if node.get('file_type') != 'code':
        return False

    label = str(node.get('label', '')).strip()

    if not label:
        return False

    if label.endswith(('.py', '.js', '.ts', '.tsx', '.java', '.go', '.rs')):
        return False

    return bool(normlabel(label))


def buildlabelindex(nodes: list[dict[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}

    for node in nodes:
        if not resolvablesymbol(node):
            continue

        node_id = node.get('id')

        if not node_id:
            continue

        key = normlabel(str(node.get('label', '')))

        if not key:
            continue

        index.setdefault(key, []).append(str(node_id))

    return index


def existingpairs(edges: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    triples: set[tuple[str, str, str]] = set()

    for edge in edges:
        source = edge.get('source')
        target = edge.get('target')
        relation = edge.get('relation', '')

        if source and target:
            triples.add((str(source), str(target), str(relation)))

    return triples


def iterrawcalls(per_file: Sequence[object]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    for result in per_file:
        if not isinstance(result, dict):
            continue

        raw_calls = result.get('raw_calls', [])

        if not isinstance(raw_calls, list):
            continue

        for raw_call in raw_calls:
            if isinstance(raw_call, dict):
                calls.append(raw_call)

    return calls


def modulestem(module_name: str | None) -> str:
    if not module_name:
        return ''

    return module_name.strip('.').split('.')[-1]


def parsepyaliases(path: Path) -> dict[str, ImportedSymbol]:
    try:
        source = base.text(path, warn=False)
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return {}

    aliases: dict[str, ImportedSymbol] = {}
    source_file = str(path)

    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue

        module_stem = modulestem(node.module)

        if not module_stem:
            continue

        for alias in node.names:
            if alias.name == '*':
                continue

            local_name = alias.asname or alias.name
            aliases[local_name] = ImportedSymbol(local_name=local_name, imported_name=alias.name, module_stem=module_stem, source_file=source_file, source_location=f"L{getattr(node, 'lineno', 1)}")

    return aliases


def nodesourcestem(node: dict[str, Any]) -> str:
    source_file = str(node.get('source_file', ''))

    if not source_file:
        return ''

    return Path(source_file).stem


def buildpysymbols(nodes: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    index: dict[tuple[str, str], list[str]] = {}

    for node in nodes:
        if not resolvablesymbol(node):
            continue

        source_stem = nodesourcestem(node)

        if not source_stem:
            continue

        label = normlabel(str(node.get('label', '')))

        if not label:
            continue

        node_id = node.get('id')

        if not node_id:
            continue

        index.setdefault((source_stem, label), []).append(str(node_id))

    return index


def findpysymbol(symbol_index: dict[tuple[str, str], list[str]], imported: ImportedSymbol) -> str | None:
    candidates = symbol_index.get((imported.module_stem, imported.imported_name.lower()), [])

    if len(candidates) == 1:
        return candidates[0]

    return None


def resolveguidedcalls(per_file: Sequence[object], paths: Sequence[Path], all_nodes: list[dict[str, Any]], all_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    symbol_index = buildpysymbols(all_nodes)
    known_pairs = existingpairs(all_edges)
    result_by_file: dict[str, dict[str, Any]] = {}

    for index, path in enumerate(paths):
        if path.suffix != '.py':
            continue

        slot: Any = per_file[index] if index < len(per_file) else None
        result_by_file[str(path)] = slot if isinstance(slot, dict) else {'nodes': [], 'edges': []}
    resolved_edges: list[dict[str, Any]] = []

    for path in paths:
        if path.suffix != '.py':
            continue

        source_file = str(path)
        aliases = parsepyaliases(path)

        if not aliases:
            continue

        file_result = result_by_file.get(source_file, {'raw_calls': []})
        raw_calls = file_result.get('raw_calls', [])

        if not isinstance(raw_calls, list):
            continue

        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue

            if raw_call.get('is_member_call'):
                continue

            callee = str(raw_call.get('callee', '')).strip()

            if not callee:
                continue

            imported = aliases.get(callee)

            if imported is None:
                continue

            target = findpysymbol(symbol_index, imported)

            if target is None:
                continue

            caller = str(raw_call.get('caller_nid', ''))

            if not caller or caller == target:
                continue

            pair = (caller, target, 'calls')

            if pair in known_pairs:
                continue

            known_pairs.add(pair)
            resolved_edges.append({'source': caller, 'target': target, 'relation': 'calls', 'context': 'import_guided_call', 'source_file': raw_call.get('source_file', source_file), 'source_location': raw_call.get('source_location') or imported.source_location, 'metadata': metadata({'resolver': 'python_import_guided', 'local_name': imported.local_name, 'imported_name': imported.imported_name, 'module_stem': imported.module_stem, 'import_source_location': imported.source_location})})

    return resolved_edges


def resolvecalls(per_file: Sequence[dict[str, Any] | None], all_nodes: list[dict[str, Any]], all_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    label_index = buildlabelindex(all_nodes)
    known_pairs = existingpairs(all_edges)
    nid_to_source_file = {str(n.get('id')): str(n.get('source_file', '')) for n in all_nodes if n.get('id')}
    resolved: list[dict[str, Any]] = []

    for raw_call in iterrawcalls(per_file):
        callee = str(raw_call.get('callee', '')).strip()

        if not callee:
            continue

        if raw_call.get('is_member_call'):
            continue

        candidates = label_index.get(callee.lower(), [])

        if not candidates:
            continue

        if len(candidates) == 1:
            target: str | None = candidates[0]
        else:
            target = disambiguate(candidates, {c: nid_to_source_file.get(c, '') for c in candidates}, str(raw_call.get('source_file', '')))

            if target is None:
                continue

        caller = str(raw_call.get('caller_nid', ''))

        if not caller:
            continue

        if target == caller:
            continue

        pair = (caller, target, 'calls')

        if pair in known_pairs:
            continue

        known_pairs.add(pair)
        resolved.append({'source': caller, 'target': target, 'relation': 'calls', 'context': 'call', 'source_file': raw_call.get('source_file', ''), 'source_location': raw_call.get('source_location')})

    return resolved


def bashmakeid(*parts: str) -> str:
    return make(*parts)

from graphify.src.base import stem as stem


def filenodeid(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return bashmakeid(str(path))

    return bashmakeid(stem(rel))


def resolvebashedges(per_file: Sequence[dict | None], paths: Sequence[Path], root: Path, existing_edges: list[dict] | None=None) -> list[dict]:
    path_by_index = [Path(p).resolve() for p in paths]
    file_nid_by_path = {p: filenodeid(p, root) for p in path_by_index}
    functions_by_file: dict[str, dict[str, str]] = {}

    for result, path in zip(per_file, path_by_index):
        if not isinstance(result, dict):
            continue

        file_nid = file_nid_by_path[path]
        nodes = result.get('nodes', [])

        if not isinstance(nodes, list):
            continue

        for node in nodes:
            if not isinstance(node, dict):
                continue

            metadata = node.get('metadata', {})

            if not isinstance(metadata, dict):
                continue

            if metadata.get('kind') != 'bash_function':
                continue

            name = str(node.get('label', '')).removesuffix('()').strip()
            node_id = node.get('id')

            if not name or not node_id:
                continue

            functions_by_file.setdefault(file_nid, {})[name] = str(node_id)
    sourced_files: dict[str, set[str]] = {}
    resolved_edges: list[dict] = []
    existing = existingpairs(existing_edges or [])

    for result, path in zip(per_file, path_by_index):
        if not isinstance(result, dict):
            continue

        src_file_nid = file_nid_by_path[path]
        bash_sources = result.get('bash_sources', [])

        if not isinstance(bash_sources, list):
            continue

        for source in bash_sources:
            if not isinstance(source, dict):
                continue

            raw_target = source.get('target_path')

            if not isinstance(raw_target, (str, Path)) or not str(raw_target).strip():
                continue

            candidate = Path(raw_target)

            if not candidate.is_absolute():
                candidate = path.parent / candidate

            try:
                target_path = candidate.resolve()
            except (OSError, RuntimeError):
                continue

            target_file_nid = file_nid_by_path.get(target_path)

            if target_file_nid is None:
                continue

            sourced_files.setdefault(src_file_nid, set()).add(target_file_nid)
            key = (src_file_nid, target_file_nid, 'imports_from')

            if key in existing:
                continue

            existing.add(key)
            resolved_edges.append({'source': src_file_nid, 'target': target_file_nid, 'relation': 'imports_from', 'context': 'import', 'source_file': source.get('source_file', str(path)), 'source_location': source.get('source_location', '')})

    for result, path in zip(per_file, path_by_index):
        if not isinstance(result, dict):
            continue

        caller_file_nid = file_nid_by_path[path]
        imported_file_ids = sourced_files.get(caller_file_nid, set())

        if not imported_file_ids:
            continue

        raw_calls = result.get('raw_calls', [])

        if not isinstance(raw_calls, list):
            continue

        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue

            if raw_call.get('language') != 'bash':
                continue

            callee = raw_call.get('callee')
            caller_nid = raw_call.get('caller_nid')

            if not isinstance(callee, str) or not callee or (not caller_nid):
                continue

            matches = [functions_by_file[file_nid][callee] for file_nid in imported_file_ids if callee in functions_by_file.get(file_nid, {})]

            if len(matches) != 1:
                continue

            target = matches[0]
            key = (str(caller_nid), target, 'calls')

            if key in existing:
                continue

            existing.add(key)
            resolved_edges.append({'source': str(caller_nid), 'target': target, 'relation': 'calls', 'context': 'call', 'source_file': raw_call.get('source_file', str(path)), 'source_location': raw_call.get('source_location', '')})

    return resolved_edges