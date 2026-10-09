from __future__ import annotations
from . import base
import hashlib
import importlib
import json
import os
import re
import sys
import textwrap
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Callable
from .cache import load, save
from . import mcp, manifest, registry
from .registry import LanguageResolver
from graphify.src.base import GLOBALS, stem, make, bytes as readbytes, text, read
from graphify.langs import apex, bash, c, cpp, csharp, dart, elixir, erlang, go, json as jsonlang, julia, markdown, objc, ocaml, powershell, r, razor, ruby, rust, sln, solidity, sql, swift, terraform, zig
resolve_pascal_inherited_calls = None
from graphify.security import metadata
from graphify.paths import disambiguate
from graphify.src.models import LanguageConfig, _JS_CACHE_BYPASS_SUFFIXES, _NamespaceExportFact, _StarExportFact, _SymbolAliasFact, _SymbolDeclarationFact, _SymbolExportFact, _SymbolImportFact, _SymbolResolutionFacts, _SymbolUseFact, _WORKSPACE_PACKAGE_CACHE
from graphify.src.resolve import DECLDEFHEADERS, DECLDEFIMPLS, EXPORTCONDITIONS, JSINDEXFILES, JSPRIMITIVES, JSRESOLVEEXTS, PACKAGEIMPORTSCACHE, ROOTNAMESPACECACHE, TSALIASCACHE, TSBASEURLCACHE, VUESCRIPTLANG, VUESCRIPT, WORKSPACEMANIFESTS, applyfacts, augmentedges, cachedrealpath, cachedsourcekey, collectjsfacts, collectpyfacts, contained, decldefstem, disambiguatenodes, workspaceroot, goimportpath, istypelike, jscallident, jsdefaultexport, jsdefaultimport, jsexportclause, jsisstar, jsdeclarations, jslexicalaliases, jsspecifier, jsnamed, jsnamespacename, jssourcepath, jsbodies, loadaliases, baseurl, loadpackages, matchalias, mergedecldef, nodedisambigkey, entrycandidates, parsejstree, parsepytree, _pascal_class_stem_cache, pascalroot, pascalclass, pascalunit, _pascal_unit_cache, pnpmglobs, pycallident, pyfrommodule, pyimportnames, pybodies, tsaliases, cinclude, resolvecrossimports, resolvejavaimports, exporttarget, resolvegotypes, resolvejavatypes, resolvephptypes, jspath, jstarget, jsmodulepath, luatarget, probemodule, resolvepymodule, resolvepyns, tsalias, workspaceimport, sourcekey, stripjsonc, tstyperefs, tsheritage, tsmembers, VUESCRIPTLANG, vuemask, walkjstree, walkpytree, workspaceglobs
from graphify.resolve import resolvebashedges
from graphify.src.engine import CONTEXTS, CSPARAMSCOPE, CPRIMITIVES, JAVABUILTINS, JAVAPARAMSCOPE, JSFUNCTIONS, JSBOUNDARY, PYNOISE, PYCONTAINERS, RUBYFACTORIES, ctypes, cpptypes, cppdeclarator, cpplocals, csattributes, csclassify, cstypes, cswalk, csnsid, csnamespace, csinterfaces, csparams, jsdynamic, generic, findbody, findrequire, cppfunc, javaannotations, javatypes, javawalk, javaparams, jspatterns, jsdispatch, jswalk, jslocals, jsmember, jsmodules, kttypes, ktreturn, ktproperty, ktuser, phptypes, phpreturn, phpname, pytargets, pyparams, pytypes, pylocals, pymodules, pynames, cstypename, jsrequire, rubyconstlast, rubywalk, rubylocalclasses, rubynewclass, scalatypes, semantic, location, swiftclassify, swifttypes, swiftconstructor, swiftkeyword, swiftwalk, swiftlocals, swiftscan, swiftpropname, swiftproperty, swiftreceiver, swiftuser, tsdecorator, tsdescendants, tsedges, tswalk, tsmethod, tsreceivers
_RECURSION_LIMIT = 10000


def raiseoverflow() -> None:
    if sys.getrecursionlimit() < _RECURSION_LIMIT:
        sys.setrecursionlimit(_RECURSION_LIMIT)


def safeextract(extractor: Callable, path: Path, *, scan_root: Path | None=None) -> dict:
    try:
        if extractor is extpython:
            return extractor(path, root=scan_root)

        return extractor(path)
    except RecursionError:
        print(f'  warning: skipped {path} (recursion limit exceeded)', file=sys.stderr, flush=True)

        return {'nodes': [], 'edges': [], 'error': 'recursion_limit_exceeded'}
    except Exception as e:
        if os.environ.get('GRAPHIFY_DEBUG'):
            import traceback
            traceback.print_exc(file=sys.stderr)
        print(f'  warning: skipped {path} ({type(e).__name__}: {e})', file=sys.stderr, flush=True)

        return {'nodes': [], 'edges': [], 'error': f'{type(e).__name__}: {e}'}


def filenodeid(rel_path: Path) -> str:
    return make(stem(rel_path))


def pyaliasfiles(paths, root) -> tuple[dict[str, set[str]], set[str]]:
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)
    package_dirs: dict[Path, bool] = {}

    def ispackage(path: Path) -> bool:
        if path not in package_dirs:
            package_dirs[path] = (path / '__init__.py').is_file()

        return package_dirs[path]

    aliases: dict[str, set[str]] = {}
    scan_root_aliases: set[str] = set()

    for raw_path in paths:
        p = Path(raw_path)

        if p.suffix.lower() != '.py':
            continue

        try:
            p = p.resolve()
            rel = p.relative_to(root)
        except (ValueError, OSError, RuntimeError):
            continue

        module_path = p.parent if p.name == '__init__.py' else p.with_suffix('')
        candidate_roots = [root]

        for ancestor in p.parents:
            if ancestor == root:
                break

            try:
                ancestor.relative_to(root)
            except ValueError:
                break

            if not ispackage(ancestor):
                candidate_roots.append(ancestor)

        for candidate_root in candidate_roots:
            try:
                module_rel = module_path.relative_to(candidate_root)
            except ValueError:
                continue

            if not module_rel.parts or not all((part.isidentifier() for part in module_rel.parts)):
                continue

            module_name = '.'.join(module_rel.parts)
            candidate = candidate_root.joinpath(*module_rel.parts)

            try:
                hit = probemodule(candidate)

                if hit is None or hit.resolve() != p:
                    continue
            except (OSError, RuntimeError):
                continue

            module_id = make(module_name)
            aliases.setdefault(module_id, set()).add(str(p))

            if candidate_root == root:
                scan_root_aliases.add(module_id)

    return (aliases, scan_root_aliases)


def suppressambiguous(edges: list[dict], nodes: list[dict], raw_calls: list[dict], ambiguous_modules: set[str], module_alias_files: dict[str, set[str]], root: Path) -> None:
    node_ids = {n.get('id') for n in nodes if isinstance(n, dict)}
    ambiguous_bindings_by_file: dict[str, set[str]] = {}
    ambiguous_all_call_files: set[str] = set()

    def isloosesibling(edge: dict, module_id: str) -> bool:
        target_file = edge.get('target_file')
        source_file = edge.get('source_file')

        if not target_file or not source_file:
            return False

        candidates = module_alias_files.get(module_id, set())

        if not candidates:
            return False

        try:
            source_path = Path(source_file)

            if not source_path.is_absolute():
                source_path = root / source_path
            source_path = source_path.resolve()
            target_path = Path(target_file).resolve()

            if target_path.parent != source_path.parent:
                return False

            if (source_path.parent / '__init__.py').is_file() or (source_path.parent / '__init__.pyi').is_file():
                return False

            return any((Path(candidate).resolve() == target_path for candidate in candidates))
        except (OSError, RuntimeError):
            return False

    kept_edges: list[dict] = []

    for edge in edges:
        if not isinstance(edge, dict):
            kept_edges.append(edge)
            continue

        module_name = edge.pop('_python_import_module', None)
        bindings = edge.pop('pybindings', [])
        is_module_binding = edge.pop('_python_import_module_binding', False)

        if is_module_binding is True:
            edge['_python_plain_import'] = True
        marker_only = edge.pop('_python_import_marker_only', False)

        if not module_name:
            if not marker_only:
                kept_edges.append(edge)
            continue

        if not marker_only and edge.get('relation') not in ('imports', 'imports_from'):
            kept_edges.append(edge)
            continue

        module_id = make(module_name)
        module_is_ambiguous = module_id in ambiguous_modules

        if module_is_ambiguous and is_module_binding and ('.' not in module_name) and isloosesibling(edge, module_id):
            module_is_ambiguous = False

        if is_module_binding:
            if module_is_ambiguous:
                ambiguous_bindings_by_file.setdefault(str(edge.get('source_file', '')), set()).update((local for _, local in bindings))
        else:
            for imported, local in bindings:
                if imported == '*':
                    module_prefix = make(module_name) + '_'

                    if module_is_ambiguous or any((name.startswith(module_prefix) for name in ambiguous_modules)):
                        ambiguous_all_call_files.add(str(edge.get('source_file', '')))
                    continue

                if module_is_ambiguous or make(f'{module_name}.{imported}') in ambiguous_modules:
                    ambiguous_bindings_by_file.setdefault(str(edge.get('source_file', '')), set()).add(local)

        if marker_only:
            continue

        if not module_is_ambiguous:
            kept_edges.append(edge)
            continue

        identity = '\x00'.join((str(module_name), str(edge.get('source_file', '')), str(edge.get('source_location', '')), str(edge.get('source', ''))))
        target = make(f'ambiguous_python_import_{hashlib.sha1(identity.encode()).hexdigest()[:12]}')

        while target in node_ids:
            target += '_'
        edge['target'] = target
        edge.pop('target_file', None)
        kept_edges.append(edge)
    edges[:] = kept_edges

    for raw_call in raw_calls:
        source_file = str(raw_call.get('source_file', ''))

        if source_file in ambiguous_all_call_files:
            raw_call['_ambiguous_python_import'] = True
            continue

        bindings = ambiguous_bindings_by_file.get(source_file)

        if not bindings:
            continue

        callee = str(raw_call.get('callee', ''))
        receiver = str(raw_call.get('receiver', ''))
        receiver_root = receiver.split('.', 1)[0]

        if callee in bindings or receiver_root in bindings:
            raw_call['_ambiguous_python_import'] = True


def repointpackage(paths, all_nodes, all_edges, root) -> None:
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)
    node_ids = {n.get('id') for n in all_nodes if isinstance(n, dict)}
    alias_to_files: dict[str, set[str]] = {}

    for p in paths:
        if p.suffix.lower() not in ('.py', '.pyi'):
            continue

        try:
            rel = Path(p).resolve().relative_to(root)
        except (ValueError, OSError):
            continue

        parts = rel.parts

        if len(parts) < 2:
            continue

        d = Path(p).resolve().parent
        levels = 0

        while levels < len(parts) - 1 and (d / '__init__.py').is_file():
            levels += 1
            d = d.parent

        if levels == 0:
            continue

        mod_parts = parts[-(levels + 1):]

        if len(mod_parts) == len(parts):
            continue

        file_node = filenodeid(rel)
        alias = make(str(Path(*mod_parts).with_suffix('')))
        alias_to_files.setdefault(alias, set()).add(file_node)

        if p.name in ('__init__.py', '__init__.pyi') and len(mod_parts) > 1:
            pkg_alias = make(str(Path(*mod_parts[:-1])))
            alias_to_files.setdefault(pkg_alias, set()).add(file_node)
    alias_map = {a: next(iter(fs)) for a, fs in alias_to_files.items() if len(fs) == 1 and a not in node_ids}

    if not alias_map:
        return

    for e in all_edges:
        if isinstance(e, dict) and e.get('relation') in ('imports', 'imports_from') and str(e.get('source_file', '')).lower().endswith(('.py', '.pyi')):
            tgt = e.get('target')

            if tgt in alias_map:
                e['target'] = alias_map[tgt]


def repointsiblings(paths, all_nodes, all_edges, root) -> None:
    try:
        root = Path(root).resolve()
    except OSError:
        root = Path(root)
    node_ids = {n.get('id') for n in all_nodes if isinstance(n, dict)}
    dir_siblings: dict[Path, dict[str, set[str]]] = {}

    for p in paths:
        if p.suffix.lower() not in ('.py', '.pyi'):
            continue

        try:
            p_res = Path(p).resolve()
            rel = p_res.relative_to(root)
        except (ValueError, OSError):
            continue

        file_node = filenodeid(rel)

        if file_node not in node_ids:
            continue

        if p_res.name in ('__init__.py', '__init__.pyi'):
            pkg_dir = p_res.parent
            parent_dir = pkg_dir.parent

            if not (parent_dir / '__init__.py').is_file() and (not (parent_dir / '__init__.pyi').is_file()):
                mod_key = make(pkg_dir.name)
                dir_siblings.setdefault(parent_dir, {}).setdefault(mod_key, set()).add(file_node)
            continue

        d = p_res.parent

        if (d / '__init__.py').is_file() or (d / '__init__.pyi').is_file():
            continue

        mod_key = make(p_res.stem)
        dir_siblings.setdefault(d, {}).setdefault(mod_key, set()).add(file_node)
    dir_alias_map: dict[Path, dict[str, str]] = {d: {mod: next(iter(fns)) for mod, fns in mod_map.items() if len(fns) == 1} for d, mod_map in dir_siblings.items()}

    if not dir_alias_map:
        return

    for e in all_edges:
        if not (isinstance(e, dict) and e.get('relation') in ('imports', 'imports_from') and str(e.get('source_file', '')).lower().endswith(('.py', '.pyi'))):
            continue

        sf = e.get('source_file')

        if not sf:
            continue

        try:
            sf_path = Path(sf)

            if not sf_path.is_absolute():
                sf_path = (root / sf_path).resolve()
            else:
                sf_path = sf_path.resolve()
        except OSError:
            continue

        imp_dir = sf_path.parent
        aliases = dir_alias_map.get(imp_dir)

        if not aliases:
            continue

        tgt = e.get('target')

        if tgt in aliases:
            repointed = aliases[tgt]

            if repointed != e.get('source'):
                e['target'] = repointed
SEMANTIC_RELATIONS = frozenset({'inherits', 'implements', 'mixes_in', 'embeds', 'references', 'calls', 'imports', 'imports_from', 're_exports', 'contains', 'method'})


def resolvename(node, source: bytes, config: LanguageConfig) -> str | None:
    if config.resolve_function_name_fn is not None:
        return None

    n = node.child_by_field_name(config.name_field)

    if n:
        return read(n, source)

    for child in node.children:
        if child.type in config.name_fallback_child_types:
            return read(child, source)

    return None


def pybindings(node, source: bytes) -> list[tuple[str, str]]:
    bindings: list[tuple[str, str]] = []
    past_import = False

    for child in node.children:
        if child.type == 'import':
            past_import = True
            continue

        if not past_import:
            continue

        if child.type == 'dotted_name':
            imported = read(child, source)
            bindings.append((imported, imported.split('.')[-1]))
        elif child.type == 'aliased_import':
            name_node = child.child_by_field_name('name')
            alias_node = child.child_by_field_name('alias')

            if name_node is None:
                continue

            imported = read(name_node, source)
            local = read(alias_node, source) if alias_node is not None else imported.split('.')[-1]
            bindings.append((imported, local))
        elif child.type == 'wildcard_import':
            bindings.append(('*', '*'))

    return bindings


def missingmodule(attempted: Path, root: Path, raw: str) -> str:
    try:
        rel = attempted.relative_to(root)
    except ValueError:
        return f'ref:{raw}'

    parts = list(rel.with_suffix('').parts)

    if parts and parts[-1] == '__init__':
        parts.pop()

    return '.'.join(parts) if parts else f'ref:{raw}'


def pytypechecking(node, source: bytes) -> bool:
    child, parent = (node, node.parent)

    while parent is not None:
        if parent.type in ('if_statement', 'elif_clause') and parent.child_by_field_name('consequence') == child:
            cond = parent.child_by_field_name('condition')

            if cond is not None and cond.type == 'attribute':
                cond = cond.child_by_field_name('attribute')

            if cond is not None and cond.type == 'identifier' and (read(cond, source) == 'TYPE_CHECKING'):
                return True

        child, parent = (parent, parent.parent)

    return False


def importpython(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None, scan_root: Path | None=None) -> None:
    t = node.type
    current_path = Path(str_path)

    try:
        current_path = current_path.resolve()
    except OSError:
        pass
    root = Path(scan_root) if scan_root is not None else current_path.parent

    try:
        root = root.resolve()
    except OSError:
        pass
    type_only = pytypechecking(node, source)

    if t == 'import_statement':
        for child in node.children:
            if child.type in ('dotted_name', 'aliased_import'):
                raw = read(child, source)
                raw_module, _, raw_alias = raw.partition(' as ')
                module_name = raw_module.strip().lstrip('.')
                local_binding = raw_alias.strip() if raw_alias else module_name.split('.')[0]
                target_path = resolvepymodule(module_name, current_path, root, level=0)

                if target_path is not None:
                    try:
                        if target_path.resolve() == current_path:
                            target_path = None
                    except OSError:
                        pass
                tgt_nid = make(str(target_path)) if target_path is not None else make(module_name)
                edge = {'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}', '_python_import_module': module_name, 'pybindings': [(module_name, local_binding)], '_python_import_module_binding': True}

                if target_path is not None:
                    edge['target_file'] = str(target_path)

                if raw_alias:
                    edge['local_alias'] = raw_alias.strip()

                if type_only:
                    edge['type_only'] = True
                edges.append(edge)
    elif t == 'import_from_statement':
        module_node = node.child_by_field_name('module_name')

        if module_node:
            raw = read(module_node, source)
            target_path: 'Path | None' = None

            if raw.startswith('.'):
                dots = len(raw) - len(raw.lstrip('.'))
                module_name = raw.lstrip('.')
                target_path = resolvepymodule(module_name, current_path, root, level=dots)
                missing = False

                if target_path is None:
                    base = current_path.parent

                    for _ in range(dots - 1):
                        base = base.parent
                    rel = module_name.replace('.', '/') + '.py' if module_name else '__init__.py'
                    target_path = base / rel
                    missing = not target_path.is_file()

                if missing:
                    tgt_nid = make(missingmodule(target_path, root, raw))
                else:
                    tgt_nid = make(str(target_path))
            else:
                target_path = resolvepymodule(raw, current_path, root, level=0)

                if target_path is None and resolvepyns(raw, current_path, root, level=0) is not None:
                    edges.append({'source': file_nid, 'target': '', 'relation': '_python_import_marker', 'source_file': str_path, '_python_import_module': raw, 'pybindings': pybindings(node, source), '_python_import_marker_only': True})
                    return

                if target_path is not None:
                    try:
                        if target_path.resolve() == current_path:
                            target_path = None
                    except OSError:
                        pass
                tgt_nid = make(str(target_path)) if target_path is not None else make(raw)
            edge = {'source': file_nid, 'target': tgt_nid, 'relation': 'imports_from', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'}

            if not raw.startswith('.'):
                edge['_python_import_module'] = raw
                edge['pybindings'] = pybindings(node, source)

            if target_path is not None:
                try:
                    if target_path.is_file():
                        edge['target_file'] = str(target_path)
                except OSError:
                    pass

            if type_only:
                edge['type_only'] = True
            edges.append(edge)


def importjs(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    is_reexport = node.type == 'export_statement'

    if is_reexport:
        has_from = any((child.type == 'from' or read(child, source) == 'from' for child in node.children if child.type in ('from', 'identifier')))

        if not has_from:
            has_from = any((child.type == 'string' for child in node.children))

            if not has_from:
                return
    is_type_only = any((child.type == 'type' and (not child.is_named) for child in node.children))
    resolved_path: 'Path | None' = None
    module_string = None

    for child in node.children:
        if child.type == 'string':
            module_string = child
            break

        if child.type == 'import_require_clause':
            module_string = next((sub for sub in child.children if sub.type == 'string'), None)
            break

    if module_string is not None:
        raw = read(module_string, source).strip('\'"` ')
        resolved = jstarget(raw, str_path)

        if resolved is not None:
            tgt_nid, resolved_path = resolved

            if resolved_path is not None and (not resolved_path.is_file()):
                tgt_nid = make('ref', raw)
                resolved_path = None
            edge = {'source': file_nid, 'target': tgt_nid, 'relation': 'imports_from', 'context': 're-export' if is_reexport else 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'}

            if resolved_path is not None:
                edge['target_file'] = str(resolved_path)

            if is_type_only:
                edge['type_only'] = True
            edges.append(edge)

    if resolved_path is not None:
        target_stem = base.stem(resolved_path)
        line = node.start_point[0] + 1

        if is_reexport:
            for child in node.children:
                if child.type == 'export_clause':
                    for spec in child.children:
                        if spec.type == 'export_specifier':
                            name_node = spec.child_by_field_name('name')

                            if name_node:
                                sym = read(name_node, source)

                                if sym == 'default':
                                    continue

                                edges.append({**({'type_only': True} if is_type_only else {}), 'source': file_nid, 'target': make(target_stem, sym), 'relation': 're_exports', 'context': 're-export', 'source_file': str_path, 'source_location': f'L{line}', 'target_file': str(resolved_path)})
        else:
            for child in node.children:
                if child.type == 'import_clause':
                    for sub in child.children:
                        if sub.type == 'named_imports':
                            for spec in sub.children:
                                if spec.type == 'import_specifier':
                                    name_node = spec.child_by_field_name('name')

                                    if name_node:
                                        sym = read(name_node, source)
                                        edges.append({'source': file_nid, 'target': make(target_stem, sym), 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{line}', 'target_file': str(resolved_path)})


def importjava(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:

    def walkscoped(n) -> str:
        parts: list[str] = []
        cur = n

        while cur:
            if cur.type == 'scoped_identifier':
                name_node = cur.child_by_field_name('name')

                if name_node:
                    parts.append(read(name_node, source))
                cur = cur.child_by_field_name('scope')
            elif cur.type == 'identifier':
                parts.append(read(cur, source))
                break
            else:
                break

        parts.reverse()

        return '.'.join(parts)

    for child in node.children:
        if child.type in ('scoped_identifier', 'identifier'):
            path_str = walkscoped(child)
            module_name = path_str.split('.')[-1].strip('*').strip('.') or (path_str.split('.')[-2] if len(path_str.split('.')) > 1 else path_str)

            if module_name:
                tgt_nid = make(module_name)
                edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'})
            break


def importc(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    for child in node.children:
        if child.type in ('string_literal', 'system_lib_string', 'string'):
            raw = read(child, source).strip('"<> ')

            if child.type != 'system_lib_string':
                resolved = cinclude(raw, str_path)

                if resolved is not None:
                    tgt_nid = make(str(resolved))
                    edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}', 'target_file': str(resolved)})
                    break

            module_name = raw.split('/')[-1].split('.')[0]

            if module_name:
                tgt_nid = make(module_name)
                edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'})
            break


def importcsharp(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    text = read(node, source).strip().rstrip(';')

    if text.startswith('global '):
        text = text[len('global '):].strip()

    if not text.startswith('using'):
        return
    body = text[len('using'):].strip()
    using_kind, alias, target_fqn = ('namespace', None, body)

    if body.startswith('static '):
        using_kind, target_fqn = ('static', body[len('static '):].strip())
    elif '=' in body:
        lhs, rhs = body.split('=', 1)
        using_kind, alias, target_fqn = ('alias', lhs.strip(), rhs.strip())

    if not target_fqn:
        return
    edges.append({'source': file_nid, 'target': make(target_fqn), 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}', 'metadata': metadata({k: v for k, v in {'using_kind': using_kind, 'alias': alias, 'target_fqn': target_fqn, 'scope_kind': 'namespace' if scope_stack else 'file', 'scope_id': scope_stack[-1] if scope_stack else None}.items() if v is not None})})


def importkotlin(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    path_node = node.child_by_field_name('path')

    if path_node is None:
        path_node = next((c for c in node.children if c.type == 'qualified_identifier'), None)

    if path_node is not None:
        raw = read(path_node, source).strip()
    else:
        raw = next((read(c, source).strip() for c in node.children if c.type == 'identifier'), '')

    if not raw:
        return

    if raw.endswith('.*') or raw == '*' or any((c.type == '*' for c in node.children)):
        return
    alias = None
    saw_as = False

    for child in node.children:
        if not saw_as:
            saw_as = child.type == 'as'
        elif child.type in ('identifier', 'simple_identifier'):
            alias = read(child, source).strip() or None
            break

    module_name = raw.split('.')[-1].strip()

    if not module_name:
        return
    edges.append({'source': file_nid, 'target': make(module_name), 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}', 'metadata': metadata({k: v for k, v in {'target_fqn': raw, 'alias': alias}.items() if v is not None})})


def importscala(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    for child in node.children:
        if child.type in ('stable_id', 'identifier'):
            raw = read(child, source)
            module_name = raw.split('.')[-1].strip('{} ')

            if module_name and module_name != '_':
                tgt_nid = make(module_name)
                edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'})
            break


def importphp(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    saw_as = False
    module_name = None

    for child in node.children:
        if child.type == 'as':
            saw_as = True
            continue

        if child.type in ('qualified_name', 'name', 'identifier'):
            bare = read(child, source).split('\\')[-1].strip()

            if saw_as:
                module_name = bare
                break

            if module_name is None:
                module_name = bare

    if module_name:
        tgt_nid = make(module_name)
        edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'})


def cfunc(node, source: bytes) -> str | None:
    if node.type == 'identifier':
        return read(node, source)

    decl = node.child_by_field_name('declarator')

    if decl:
        return cfunc(decl, source)

    for child in node.children:
        if child.type == 'identifier':
            return read(child, source)

    return None

_PYTHON_CONFIG = LanguageConfig(ts_module='tree_sitter_python', class_types=frozenset({'class_definition'}), function_types=frozenset({'function_definition'}), import_types=frozenset({'import_statement', 'import_from_statement'}), call_types=frozenset({'call'}), call_function_field='function', call_accessor_node_types=frozenset({'attribute'}), call_accessor_field='attribute', call_accessor_object_field='object', function_boundary_types=frozenset({'function_definition'}), import_handler=importpython)
_JS_CONFIG = LanguageConfig(ts_module='tree_sitter_javascript', class_types=frozenset({'class_declaration'}), function_types=frozenset({'function_declaration', 'generator_function_declaration', 'method_definition'}), import_types=frozenset({'import_statement', 'export_statement'}), call_types=frozenset({'call_expression', 'new_expression', 'jsx_opening_element', 'jsx_self_closing_element'}), call_function_field='function', call_accessor_node_types=frozenset({'member_expression'}), call_accessor_field='property', call_accessor_object_field='object', function_boundary_types=frozenset({'function_declaration', 'generator_function_declaration', 'arrow_function', 'method_definition', 'function_expression', 'generator_function'}), import_handler=importjs)
_TS_CONFIG = LanguageConfig(ts_module='tree_sitter_typescript', ts_language_fn='language_typescript', class_types=frozenset({'class_declaration', 'abstract_class_declaration', 'interface_declaration', 'enum_declaration', 'type_alias_declaration'}), function_types=frozenset({'function_declaration', 'generator_function_declaration', 'method_definition', 'method_signature', 'abstract_method_signature'}), import_types=frozenset({'import_statement', 'export_statement'}), call_types=frozenset({'call_expression', 'new_expression'}), call_function_field='function', call_accessor_node_types=frozenset({'member_expression'}), call_accessor_field='property', call_accessor_object_field='object', function_boundary_types=frozenset({'function_declaration', 'generator_function_declaration', 'arrow_function', 'method_definition', 'function_expression', 'generator_function'}), import_handler=importjs)
_TSX_CONFIG = LanguageConfig(ts_module='tree_sitter_typescript', ts_language_fn='language_tsx', class_types=_TS_CONFIG.class_types, function_types=_TS_CONFIG.function_types, import_types=_TS_CONFIG.import_types, call_types=_TS_CONFIG.call_types | {'jsx_opening_element', 'jsx_self_closing_element'}, call_function_field=_TS_CONFIG.call_function_field, call_accessor_node_types=_TS_CONFIG.call_accessor_node_types, call_accessor_field=_TS_CONFIG.call_accessor_field, call_accessor_object_field=_TS_CONFIG.call_accessor_object_field, function_boundary_types=_TS_CONFIG.function_boundary_types, import_handler=_TS_CONFIG.import_handler)
_JAVA_CONFIG = LanguageConfig(ts_module='tree_sitter_java', class_types=frozenset({'class_declaration', 'interface_declaration', 'record_declaration', 'enum_declaration', 'annotation_type_declaration'}), function_types=frozenset({'method_declaration', 'constructor_declaration'}), import_types=frozenset({'import_declaration'}), call_types=frozenset({'method_invocation', 'object_creation_expression'}), call_function_field='name', call_accessor_node_types=frozenset(), function_boundary_types=frozenset({'method_declaration', 'constructor_declaration'}), import_handler=importjava)
_GROOVY_CONFIG = LanguageConfig(ts_module='tree_sitter_groovy', class_types=frozenset({'class_declaration', 'interface_declaration', 'enum_declaration'}), function_types=frozenset({'method_declaration', 'constructor_declaration'}), import_types=frozenset({'import_declaration'}), call_types=frozenset({'method_invocation'}), call_function_field='name', call_accessor_node_types=frozenset(), function_boundary_types=frozenset({'method_declaration', 'constructor_declaration'}), import_handler=importjava)
_C_CONFIG = LanguageConfig(ts_module='tree_sitter_c', class_types=frozenset(), function_types=frozenset({'function_definition'}), import_types=frozenset({'preproc_include'}), call_types=frozenset({'call_expression'}), call_function_field='function', call_accessor_node_types=frozenset({'field_expression'}), call_accessor_field='field', function_boundary_types=frozenset({'function_definition'}), import_handler=importc, resolve_function_name_fn=cfunc)
_CPP_CONFIG = LanguageConfig(ts_module='tree_sitter_cpp', class_types=frozenset({'class_specifier', 'struct_specifier', 'enum_specifier', 'union_specifier'}), function_types=frozenset({'function_definition'}), import_types=frozenset({'preproc_include'}), call_types=frozenset({'call_expression'}), call_function_field='function', call_accessor_node_types=frozenset({'field_expression', 'qualified_identifier'}), call_accessor_field='field', function_boundary_types=frozenset({'function_definition'}), import_handler=importc, resolve_function_name_fn=cppfunc)


def rubysanitize(name: str) -> str:
    if not name:
        return name

    if name.endswith('!'):
        return f'{name[:-1]}_bang'

    if name.endswith('?'):
        return f'{name[:-1]}_pred'

    if name.endswith('='):
        return f'{name[:-1]}_eq'

    return name

_RUBY_CONFIG = LanguageConfig(ts_module='tree_sitter_ruby', class_types=frozenset({'class', 'module'}), function_types=frozenset({'method', 'singleton_method'}), import_types=frozenset(), call_types=frozenset({'call'}), call_function_field='method', call_accessor_node_types=frozenset(), name_fallback_child_types=('constant', 'scope_resolution', 'identifier'), body_fallback_child_types=('body_statement',), function_boundary_types=frozenset({'method', 'singleton_method'}), sanitize_symbol_name_fn=rubysanitize)
_CSHARP_CONFIG = LanguageConfig(ts_module='tree_sitter_c_sharp', class_types=frozenset({'class_declaration', 'interface_declaration', 'enum_declaration', 'struct_declaration', 'record_declaration'}), function_types=frozenset({'method_declaration'}), import_types=frozenset({'using_directive'}), call_types=frozenset({'invocation_expression', 'object_creation_expression'}), call_function_field='function', call_accessor_node_types=frozenset({'member_access_expression'}), call_accessor_field='name', body_fallback_child_types=('declaration_list',), function_boundary_types=frozenset({'method_declaration'}), import_handler=importcsharp)
_KOTLIN_CONFIG = LanguageConfig(ts_module='tree_sitter_kotlin', class_types=frozenset({'class_declaration', 'object_declaration'}), function_types=frozenset({'function_declaration'}), import_types=frozenset({'import_header', 'import'}), call_types=frozenset({'call_expression'}), call_function_field='', call_accessor_node_types=frozenset({'navigation_expression'}), call_accessor_field='', name_fallback_child_types=('simple_identifier', 'identifier'), body_fallback_child_types=('function_body', 'class_body', 'enum_class_body'), function_boundary_types=frozenset({'function_declaration'}), import_handler=importkotlin)
_SCALA_CONFIG = LanguageConfig(ts_module='tree_sitter_scala', class_types=frozenset({'class_definition', 'object_definition', 'trait_definition', 'enum_definition'}), function_types=frozenset({'function_definition', 'function_declaration'}), import_types=frozenset({'import_declaration'}), call_types=frozenset({'call_expression'}), call_function_field='', call_accessor_node_types=frozenset({'field_expression'}), call_accessor_field='field', name_fallback_child_types=('identifier',), body_fallback_child_types=('template_body', 'enum_body'), function_boundary_types=frozenset({'function_definition'}), import_handler=importscala)
_PHP_CONFIG = LanguageConfig(ts_module='tree_sitter_php', ts_language_fn='language_php', class_types=frozenset({'class_declaration', 'interface_declaration', 'enum_declaration', 'trait_declaration'}), function_types=frozenset({'function_definition', 'method_declaration', 'anonymous_function', 'arrow_function'}), import_types=frozenset({'namespace_use_clause'}), call_types=frozenset({'function_call_expression', 'member_call_expression', 'scoped_call_expression', 'class_constant_access_expression', 'object_creation_expression'}), static_prop_types=frozenset({'scoped_property_access_expression'}), helper_fn_names=frozenset({'config'}), container_bind_methods=frozenset({'bind', 'singleton', 'scoped', 'instance'}), event_listener_properties=frozenset({'listen', 'subscribe'}), call_function_field='function', call_accessor_node_types=frozenset({'member_call_expression'}), call_accessor_field='name', name_fallback_child_types=('name',), body_fallback_child_types=('declaration_list', 'compound_statement', 'enum_declaration_list'), function_boundary_types=frozenset({'function_definition', 'method_declaration', 'anonymous_function', 'arrow_function'}), import_handler=importphp)


def importlua(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> None:
    text = read(node, source)
    import re
    m = re.search('require\\s*[\\(\'"]\\s*[\'"]?([^\'")\\s]+)', text)

    if m:
        raw_module = m.group(1)

        if raw_module:
            tgt_nid = luatarget(raw_module, str_path)

            if tgt_nid:
                edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': str(node.start_point[0] + 1)})
_LUA_CONFIG = LanguageConfig(ts_module='tree_sitter_lua', ts_language_fn='language', class_types=frozenset(), function_types=frozenset({'function_declaration'}), import_types=frozenset({'variable_declaration'}), call_types=frozenset({'function_call'}), call_function_field='name', call_accessor_node_types=frozenset({'method_index_expression'}), call_accessor_field='method', call_accessor_object_field='table', name_fallback_child_types=('identifier', 'method_index_expression'), body_fallback_child_types=('block',), function_boundary_types=frozenset({'function_declaration'}), import_handler=importlua)


def importswift(node, source: bytes, file_nid: str, stem: str, edges: list, str_path: str, scope_stack: list[str] | None=None) -> list[tuple[str, str]]:
    modules: list[tuple[str, str]] = []

    for child in node.children:
        if child.type == 'identifier':
            raw = read(child, source)
            tgt_nid = make(raw)
            edges.append({'source': file_nid, 'target': tgt_nid, 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'})
            modules.append((tgt_nid, raw))
            break

    return modules

_SWIFT_CONFIG = LanguageConfig(ts_module='tree_sitter_swift', class_types=frozenset({'class_declaration', 'protocol_declaration'}), function_types=frozenset({'function_declaration', 'protocol_function_declaration', 'init_declaration', 'deinit_declaration', 'subscript_declaration'}), import_types=frozenset({'import_declaration'}), call_types=frozenset({'call_expression'}), call_function_field='', call_accessor_node_types=frozenset({'navigation_expression'}), call_accessor_field='', name_fallback_child_types=('simple_identifier', 'type_identifier', 'user_type'), body_fallback_child_types=('class_body', 'protocol_body', 'function_body', 'enum_class_body'), function_boundary_types=frozenset({'function_declaration', 'protocol_function_declaration', 'init_declaration', 'deinit_declaration', 'subscript_declaration'}), import_handler=importswift)
def tsiscode(root: Any, start: int) -> bool:
    node = root.descendant_for_byte_range(start, start + 1)

    if node is None:
        return True

    in_template_substitution = False

    while node is not None:
        if node.type == 'comment' or node.type in ('string', 'regex', 'regex_pattern'):
            return False

        if node.type == 'template_substitution':
            in_template_substitution = True
        elif node.type == 'template_string' and (not in_template_substitution):
            return False

        node = node.parent

    return True


def tsranges(root: Any, *, call_only: bool) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    stack = [root]

    while stack:
        node = stack.pop()

        if node.type == 'type_arguments':
            parent = node.parent

            if not call_only or (parent is not None and parent.type in ('call_expression', 'new_expression')):
                ranges.append((node.start_byte, node.end_byte))
        stack.extend(node.children)

    return ranges

_TsRangeIndex = tuple[list[int], list[tuple[int, int]], list[int]]


def tscontaining(ranges: _TsRangeIndex, offset: int) -> list[tuple[int, int]]:
    starts, spans, prefix_max_end = ranges
    hits: list[tuple[int, int]] = []
    index = bisect_right(starts, offset) - 1

    while index >= 0 and prefix_max_end[index] > offset:
        start, end = spans[index]

        if end > offset:
            hits.append((start, end))
        index -= 1
    hits.reverse()

    return hits


def tsrangeindex(root: Any, *, call_only: bool) -> _TsRangeIndex:
    spans = sorted(tsranges(root, call_only=call_only))
    starts = [start for start, _ in spans]
    prefix_max_end: list[int] = []
    running = -1

    for _, end in spans:
        running = max(running, end)
        prefix_max_end.append(running)

    return (starts, spans, prefix_max_end)


def tserrors(root: Any) -> list[Any]:
    errors: list[Any] = []
    stack = [root]

    while stack:
        node = stack.pop()

        if node.type == 'ERROR' or node.is_error:
            errors.append(node)
        stack.extend(node.children)

    return errors


def tsmalformed(original_root: Any, masked_range: tuple[int, int], errors: list[Any]) -> bool:
    start, end = masked_range
    opener = original_root.descendant_for_byte_range(start, start + 1)

    if opener is None or opener.type != '<':
        return True

    if opener.parent is None or opener.parent.type != 'binary_expression':
        return True

    for error in errors:
        if error.start_byte <= end + 2 and error.end_byte >= end - 1:
            return True

    return False


_TS_IMPORT_CALL_RE = re.compile(rb'\bimport\s*(?:<[^>]+>)?\s*\(')


def normtstypes(source: bytes, *, tsx: bool=False) -> bytes | None:
    raw_matches = list(_TS_IMPORT_CALL_RE.finditer(source))

    if not raw_matches:
        return None

    try:
        import tree_sitter_typescript as ts_typescript
        from tree_sitter import Language, Parser
        language_factory = ts_typescript.language_tsx if tsx else ts_typescript.language_typescript
        parser = Parser(Language(language_factory()))
        original_root = parser.parse(source).root_node
    except Exception:
        return None

    matches = [match for match in raw_matches if tsiscode(original_root, match.start())]

    if not matches:
        return None

    original_type_ranges = tsrangeindex(original_root, call_only=False)
    matches = [match for match in matches if not tscontaining(original_type_ranges, match.start())]

    if not matches:
        return None

    def placeholder(match: 're.Match[bytes]') -> bytes:
        matched = match.group(0)

        return b'T' + re.sub(b'[^\\r\\n]', b' ', matched[1:])

    masked = bytearray(source)

    for match in matches:
        masked[match.start():match.end()] = placeholder(match)
    root = parser.parse(bytes(masked)).root_node
    type_argument_ranges = tsrangeindex(root, call_only=True)
    errors = tserrors(original_root)

    if not type_argument_ranges[1]:
        return None

    norm = bytearray(source)
    changed = False

    for match in matches:
        containing_ranges = tscontaining(type_argument_ranges, match.start())

        if containing_ranges and any((tsmalformed(original_root, candidate, errors) for candidate in containing_ranges)):
            norm[match.start():match.end()] = placeholder(match)
            changed = True

    return bytes(norm) if changed else None


def extpython(path: Path, *, root: Path | None=None) -> dict:
    from graphify.langs.python import extract as pnextract

    return pnextract(path, root=root)


def extjs(path: Path) -> dict:
    suffix = path.suffix.lower()
    is_ts = suffix in ('.ts', '.tsx', '.mts', '.cts')

    if suffix == '.tsx':
        config = _TSX_CONFIG
    elif suffix in ('.ts', '.mts', '.cts'):
        config = _TS_CONFIG
    else:
        config = _JS_CONFIG
    source_override = None

    if is_ts:
        try:
            source = base.bytes(path)
            source_override = normtstypes(source, tsx=suffix == '.tsx')
        except OSError:
            pass
    result = generic(path, config, source_override=source_override)

    if 'error' not in result:
        rescuedynamic(path, result)

    return result


def rescuedynamic(path: Path, result: dict) -> None:
    try:
        import re
        src = text(path, warn=False)

        if not re.search('(?<!\\w)import\\s*\\(', src):
            return
        existing_ids = {n['id'] for n in result.get('nodes', [])}
        file_node_id = make(str(path))
        aliases = loadaliases(path.parent)
        base_url = baseurl(path.parent)
        deferred_ids: set[str] = set()
        deferred_files: set[str] = set()
        rescued_targets: set[str] = set()

        for e in result.get('edges', []):
            if e.get('deferred') and e.get('relation') == 'imports_from' and (e.get('source') == file_node_id):
                deferred_ids.add(e.get('target'))
                tf = e.get('target_file')

                if tf:
                    try:
                        deferred_files.add(str(Path(tf).resolve()))
                    except OSError:
                        deferred_files.add(str(tf))

        for m in re.finditer('(?<!\\w)import\\s*\\(\\s*(?:\'([^\'\\n]+)\'|"([^"\\n]+)"|`([^`$\\n]+)`)\\s*\\)', src):
            raw = m.group(1) or m.group(2) or m.group(3)

            if not raw:
                continue

            line_start = src.rfind('\n', 0, m.start()) + 1

            if '//' in src[line_start:m.start()]:
                continue

            resolution = rescuedspecifier(path, raw, aliases, base_url)

            if resolution is None:
                continue

            node_id, _stub_sf, resolved_file = resolution

            if node_id in deferred_ids or make('ref', raw) in deferred_ids:
                continue

            if resolved_file is not None:
                try:
                    if str(resolved_file.resolve()) in deferred_files:
                        continue
                except OSError:
                    pass
            emit_key = str(resolved_file.resolve()) if resolved_file is not None else raw

            if emit_key in rescued_targets:
                continue

            rescued_targets.add(emit_key)
            emitrescued(result, existing_ids, file_node_id, path, raw, 'dynamic_import', aliases, base_url)
    except Exception:
        pass
def rescuedspecifier(path: Path, raw: str, aliases, base_url) -> 'tuple[str, str, Path | None] | None':
    if raw.startswith('.'):
        resolved = jsmodulepath(Path(os.path.normpath(path.parent / raw)))
        resolved_file = resolved if resolved is not None and resolved.is_file() else None

        return (make(str(resolved)), str(resolved), resolved_file)

    resolved_alias = tsalias(raw, aliases, base_url=base_url)

    if resolved_alias is not None:
        resolved_alias = jsmodulepath(resolved_alias)
        resolved_file = resolved_alias if resolved_alias is not None and resolved_alias.is_file() else None

        return (make(str(resolved_alias)), str(resolved_alias), resolved_file)

    if raw.startswith('@/'):
        resolved_unmapped = jsmodulepath(raw, path.parent)

        if resolved_unmapped is not None and resolved_unmapped.is_file():
            return (make(str(resolved_unmapped)), str(resolved_unmapped), resolved_unmapped)

    module_name = raw.split('/')[-1]

    if not module_name:
        return None

    return (make(module_name), raw, None)


def emitrescued(result: dict, existing_ids: set, file_node_id: str, path: Path, raw: str, relation: str, aliases, base_url) -> None:
    resolution = rescuedspecifier(path, raw, aliases, base_url)

    if resolution is None:
        return
    node_id, stub_source_file, resolved_file = resolution
    edge = {'source': file_node_id, 'target': node_id, 'relation': relation, 'source_file': str(path)}

    if resolved_file is not None:
        edge['target_file'] = str(resolved_file)
        result.setdefault('edges', []).append(edge)
        return

    if node_id in existing_ids:
        result.setdefault('edges', []).append(edge)
        return
    result.setdefault('nodes', []).append({'id': node_id, 'label': raw, 'file_type': 'code', 'source_file': stub_source_file})
    result.setdefault('edges', []).append(edge)
    existing_ids.add(node_id)
_HTML_SCRIPT_TAG_RE = re.compile('<script\\b((?:\\"[^\\"]*\\"|\'[^\']*\'|[^>\\"\'])*)>([\\s\\S]*?)</script\\s*>', re.IGNORECASE)
_NON_JS_SCRIPT_TYPE_RE = re.compile('\\btype\\s*=\\s*["\']?(?!module\\b|text/javascript\\b|application/javascript\\b)', re.IGNORECASE)


def sveltemask(src: str) -> tuple[str, str | None]:
    chars = [c if c in '\r\n' else ' ' for c in src]
    lang: str | None = None

    for m in _HTML_SCRIPT_TAG_RE.finditer(src):
        if _NON_JS_SCRIPT_TYPE_RE.search(m.group(1)):
            continue

        start, end = (m.start(2), m.end(2))
        chars[start:end] = src[start:end]
        chars[end:end + 4] = ['\u2028', ';', '', '']

        if lang is None:
            lang_m = VUESCRIPTLANG.search(m.group(1))

            if lang_m:
                lang = lang_m.group(1).lower()

    return (''.join(chars), lang)


def extsvelte(path: Path) -> dict:
    try:
        src = text(path)
    except OSError as e:
        return {'nodes': [], 'edges': [], 'error': str(e)}

    masked, lang = sveltemask(src)
    config = _JS_CONFIG if lang in ('js', 'jsx') else _TS_CONFIG
    masked_bytes = masked.encode('utf-8')

    if config is _TS_CONFIG:
        masked_bytes = normtstypes(masked_bytes) or masked_bytes
    result = generic(path, config, source_override=masked_bytes)

    try:
        import re
        existing_ids = {n['id'] for n in result.get('nodes', [])}
        file_node_id = make(str(path))
        aliases = loadaliases(path.parent)
        base_url = baseurl(path.parent)

        for m in re.finditer('import\\(\\s*[\'"]([^\'"]+)[\'"]\\s*\\)', src):
            raw = m.group(1)

            if not raw:
                continue

            emitrescued(result, existing_ids, file_node_id, path, raw, 'dynamic_import', aliases, base_url)
        script_re = re.compile('<script\\b[^>]*>([\\s\\S]*?)</script\\s*>', re.IGNORECASE)
        static_import_re = re.compile('import\\s+(?:[^\'"`;]+?\\s+from\\s+)?[\'"]([^\'"]+)[\'"]')

        for script_match in script_re.finditer(src):
            script_body = script_match.group(1)

            for m in static_import_re.finditer(script_body):
                raw = m.group(1)

                if not raw:
                    continue

                emitrescued(result, existing_ids, file_node_id, path, raw, 'imports_from', aliases, base_url)
    except Exception:
        pass

    return result

_ASTRO_FRONTMATTER_RE = re.compile('\\A\\s*---[^\\S\\r\\n]*\\r?\\n([\\s\\S]*?)\\r?\\n---')
_ASTRO_SCRIPT_RE = _HTML_SCRIPT_TAG_RE
_ASTRO_NON_JS_TYPE_RE = _NON_JS_SCRIPT_TYPE_RE


def astromask(src: str) -> str:
    keep: list[tuple[int, int]] = []
    fm = _ASTRO_FRONTMATTER_RE.match(src)

    if fm:
        keep.append((fm.start(1), fm.end(1)))

    for m in _ASTRO_SCRIPT_RE.finditer(src, fm.end() if fm else 0):
        if not _ASTRO_NON_JS_TYPE_RE.search(m.group(1)):
            keep.append((m.start(2), m.end(2)))
    chars = [c if c in '\r\n' else ' ' for c in src]

    for start, end in keep:
        chars[start:end] = src[start:end]

        if end < len(chars) and chars[end] == ' ':
            chars[end:end + 4] = ['\u2028', ';', '', '']

    return ''.join(chars)


def extastro(path: Path) -> dict:
    try:
        src = text(path)
    except OSError:
        return {'nodes': [], 'edges': []}

    masked = astromask(src).encode('utf-8')
    masked = normtstypes(masked) or masked
    result = generic(path, _TS_CONFIG, source_override=masked)

    try:
        import re
        src = text(path, warn=False)
        existing_ids = {n['id'] for n in result.get('nodes', [])}
        file_node_id = make(str(path))
        aliases = loadaliases(path.parent)
        base_url = baseurl(path.parent)

        for m in re.finditer('import\\(\\s*[\'"]([^\'"]+)[\'"]\\s*\\)', src):
            raw = m.group(1)

            if not raw:
                continue

            emitrescued(result, existing_ids, file_node_id, path, raw, 'dynamic_import', aliases, base_url)
        frontmatter_re = re.compile('\\A\\s*---\\s*\\r?\\n([\\s\\S]*?)\\r?\\n---\\s*(?:\\r?\\n|\\Z)')
        script_re = re.compile('<script\\b[^>]*>([\\s\\S]*?)</script\\s*>', re.IGNORECASE)
        static_import_re = re.compile('import\\s+(?:[^\'"`;]+?\\s+from\\s+)?[\'"]([^\'"]+)[\'"]')
        regions: list[str] = []
        fm = frontmatter_re.search(src)

        if fm:
            regions.append(fm.group(1))

        for script_match in script_re.finditer(src):
            regions.append(script_match.group(1))

        for region in regions:
            for m in static_import_re.finditer(region):
                raw = m.group(1)

                if not raw:
                    continue

                emitrescued(result, existing_ids, file_node_id, path, raw, 'imports_from', aliases, base_url)
    except Exception:
        pass

    return result


def extvue(path: Path) -> dict:
    try:
        src = text(path)
    except OSError:
        return {'nodes': [], 'edges': []}

    masked, lang = vuemask(src)

    if lang == 'tsx':
        config = _TSX_CONFIG
    elif lang in ('js', 'jsx'):
        config = _JS_CONFIG
    else:
        config = _TS_CONFIG
    masked_bytes = masked.encode('utf-8')

    if config in (_TS_CONFIG, _TSX_CONFIG):
        masked_bytes = normtstypes(masked_bytes, tsx=config is _TSX_CONFIG) or masked_bytes
    result = generic(path, config, source_override=masked_bytes)

    try:
        existing_ids = {n['id'] for n in result.get('nodes', [])}
        file_node_id = make(str(path))
        aliases = loadaliases(path.parent)
        base_url = baseurl(path.parent)

        for m in re.finditer('import\\(\\s*[\'"]([^\'"]+)[\'"]\\s*\\)', src):
            raw = m.group(1)

            if not raw:
                continue

            emitrescued(result, existing_ids, file_node_id, path, raw, 'dynamic_import', aliases, base_url)
    except Exception:
        pass

    return result


def extjava(path: Path) -> dict:
    return generic(path, _JAVA_CONFIG)


def isspock(path: Path, ts_result: dict) -> bool:
    import re
    _SPOCK_FEATURE_RE = re.compile('^\\s*def\\s+[\\"\']', re.MULTILINE)

    try:
        return bool(_SPOCK_FEATURE_RE.search(text(path, warn=False)))
    except OSError:
        return False


def spockfallback(path: Path, ts_result: dict) -> dict:
    import re
    source = text(path, warn=False)
    str_path = str(path)
    stem = base.stem(path)
    file_node = next((n for n in ts_result.get('nodes', []) if n.get('label') == path.name), None)
    nodes: list[dict] = [file_node] if file_node else []
    edges: list[dict] = [e for e in ts_result.get('edges', []) if e.get('context') == 'import']
    seen_ids: set[str] = {n['id'] for n in nodes}

    def addnode(nid: str, label: str, line: int) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': str_path, 'source_location': f'L{line}'})

    def addedge(src: str, tgt: str, relation: str, line: int) -> None:
        edges.append({'source': src, 'target': tgt, 'relation': relation, 'source_file': str_path, 'source_location': f'L{line}'})
    lines_text = source.splitlines()
    class_re = re.compile('^\\s*(?:[\\w@]+\\s+)*class\\s+(\\w+)')
    feature_re = re.compile('^\\s*def\\s+(?:\\"([^\\"]+)\\"|\'([^\']+)\')\\s*\\(')
    plain_method_re = re.compile('^\\s*def\\s+(\\w+)\\s*\\(')
    current_class_nid: str | None = None
    file_nid = make(str_path)

    if file_nid not in seen_ids:
        addnode(file_nid, path.name, 1)

    for lineno, line_text in enumerate(lines_text, start=1):
        cm = class_re.match(line_text)

        if cm:
            class_name = cm.group(1)
            class_nid = make(stem, class_name)
            addnode(class_nid, class_name, lineno)
            addedge(file_nid, class_nid, 'contains', lineno)
            current_class_nid = class_nid
            continue

        if current_class_nid is None:
            continue

        fm = feature_re.match(line_text)

        if fm:
            method_name = fm.group(1) or fm.group(2)
            method_label = f'"{method_name}"'
            method_nid = make(current_class_nid, method_name)
            addnode(method_nid, method_label, lineno)
            addedge(current_class_nid, method_nid, 'method', lineno)
            continue

        pm = plain_method_re.match(line_text)

        if pm:
            method_name = pm.group(1)

            if method_name not in ('if', 'while', 'for', 'switch', 'catch'):
                method_label = f'.{method_name}()'
                method_nid = make(current_class_nid, method_name)
                addnode(method_nid, method_label, lineno)
                addedge(current_class_nid, method_nid, 'method', lineno)

    return {'nodes': nodes, 'edges': edges}


def extgroovy(path: Path) -> dict:
    result = generic(path, _GROOVY_CONFIG)

    if isspock(path, result):
        result = spockfallback(path, result)

    return result


def extc(path: Path) -> dict:
    return generic(path, _C_CONFIG)

_CPP_STRING_TEST_MACROS = ('TEST_CASE', 'TEST_CASE_TEMPLATE', 'SCENARIO')
_CPP_STRING_TEST_RE = re.compile('^[ \\t]*(?:' + '|'.join(_CPP_STRING_TEST_MACROS) + ')\\s*\\(\\s*"((?:[^"\\\\]|\\\\.)+)"', re.MULTILINE)


def cppstrings(path: Path, result: dict) -> dict:
    try:
        source = text(path, warn=False)
    except OSError:
        return result

    matches = list(_CPP_STRING_TEST_RE.finditer(source))

    if not matches:
        return result

    str_path = str(path)
    stem = base.stem(path)
    file_nid = make(str_path)
    stem_collapse_id = make(stem)
    nodes = result.setdefault('nodes', [])
    edges = result.setdefault('edges', [])
    seen_ids = {n.get('id') for n in nodes}

    for m in matches:
        test_name = m.group(1)
        line = source.count('\n', 0, m.start()) + 1
        test_nid = make(stem, test_name)

        if test_nid == stem_collapse_id:
            test_nid = make(stem, 'test', f'L{line}')

        if test_nid in seen_ids:
            continue

        seen_ids.add(test_nid)
        nodes.append({'id': test_nid, 'label': f'"{test_name}"', 'file_type': 'code', 'source_file': str_path, 'source_location': f'L{line}'})
        edges.append({'source': file_nid, 'target': test_nid, 'relation': 'contains', 'source_file': str_path, 'source_location': f'L{line}'})

    return result

_CPP_CLI_MARKER_RE = re.compile(b'\\b(?:ref|value)\\s+(?:class|struct)\\b|\\binterface\\s+class\\b|\\bgcnew\\b|\\[\\s*(?:assembly|module)\\s*:')
_CPP_CLI_CLASS_RE = re.compile(b'(?:\\b(?:public|private|protected)\\s+)?\\b(?:ref|value)\\s+(?=(?:class|struct)\\b)|(?:\\b(?:public|private|protected)\\s+)?\\binterface\\s+(?=class\\b)')
_CPP_CLI_SUFFIX_UNAMBIGUOUS_RE = re.compile(b'(?<=[A-Za-z0-9_>])[\\^%](?=\\s*[,)\\]>;])')
_CPP_CLI_SUFFIX_DECL_RE = re.compile(b'(\\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+|\\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*|\\b[A-Z](?=[\\^%])|\\b(?:bool|char|wchar_t|short|int|long|float|double|unsigned|signed)|>)[\\^%](?=\\s+[A-Za-z_])')
_CPP_CLI_ATTR_RE = re.compile(b'\\[\\s*(?:assembly|module)\\s*:[^\\[\\]]*\\]', re.S)


def blanknewlines(m: 're.Match[bytes]') -> bytes:
    return re.sub(b'[^\\r\\n]', b' ', m.group(0))


def normcppcli(source: bytes) -> bytes | None:
    if not _CPP_CLI_MARKER_RE.search(source):
        return None

    out = _CPP_CLI_CLASS_RE.sub(blanknewlines, source)
    out = re.sub(b'\\bgcnew\\b', b'new  ', out)
    out = _CPP_CLI_SUFFIX_UNAMBIGUOUS_RE.sub(b' ', out)
    out = _CPP_CLI_SUFFIX_DECL_RE.sub(b'\\1 ', out)

    return _CPP_CLI_ATTR_RE.sub(blanknewlines, out)

_CPP_EXPORT_MACRO_RE = re.compile(b'(\\b(?:class|struct)[ \\t\\r\\n]+)((?:[A-Z][A-Z0-9_]*[ \\t\\r\\n]+)+)(?!final[ \\t\\r\\n]*[:{])([A-Za-z_][A-Za-z0-9_]*(?:[ \\t\\r\\n]+final)?[ \\t\\r\\n]*)([:{])')


def normcppmacros(source: bytes) -> bytes:

    def repl(m: re.Match[bytes]) -> bytes:
        start = m.start()
        idx = start - 1

        while idx >= 0 and source[idx] in b' \t\r\n':
            idx -= 1

        if idx >= 0 and source[idx] == ord(b'('):
            return m.group(0)

        if m.group(4) == b'{':
            end = m.end()
            idx_after = end

            while idx_after < len(source) and source[idx_after] in b' \t\r\n':
                idx_after += 1

            if idx_after < len(source):
                next_char = source[idx_after:idx_after + 1]

                if next_char in b'0123456789"\'-':
                    return m.group(0)

        macro_text = m.group(2)
        blanked_macro = bytearray(macro_text)

        for i in range(len(blanked_macro)):
            if blanked_macro[i] not in b'\r\n':
                blanked_macro[i] = ord(b' ')

        return m.group(1) + bytes(blanked_macro) + m.group(3) + m.group(4)

    return _CPP_EXPORT_MACRO_RE.sub(repl, source)


def extcpp(path: Path) -> dict:
    try:
        source = base.bytes(path)
    except OSError:
        return cppstrings(path, generic(path, _CPP_CONFIG))

    source_override = normcppcli(source) or source
    source_override = normcppmacros(source_override)
    result = generic(path, _CPP_CONFIG, source_override=source_override)

    return cppstrings(path, result)


def extruby(path: Path) -> dict:
    return generic(path, _RUBY_CONFIG)


def extcsharp(path: Path) -> dict:
    return generic(path, _CSHARP_CONFIG)


def extkotlin(path: Path) -> dict:
    return generic(path, _KOTLIN_CONFIG)


def extscala(path: Path) -> dict:
    return generic(path, _SCALA_CONFIG)

_PHP_SCRIPT_RE = re.compile('<script\\b(?:\\"[^\\"]*\\"|\'[^\']*\'|[^>\\"\'])*>([\\s\\S]*?)</script\\s*>', re.IGNORECASE)


def phpmask(src: str) -> tuple[str, bool]:

    def blank(s: str) -> str:
        return re.sub('[^\\r\\n]', ' ', s)

    out: list[str] = []
    pos = 0
    had_script = False

    for m in _PHP_SCRIPT_RE.finditer(src):
        had_script = True
        out.append(blank(src[pos:m.start(1)]))
        out.append(m.group(1))
        out.append(blank(src[m.end(1):m.end()]))
        pos = m.end()

    if not had_script:
        return (src, False)

    out.append(blank(src[pos:]))

    return (''.join(out), True)

_PHP_UNPARSED_CAST_RE = re.compile(b'\\(\\s*void\\s*\\)|\\(\\s*(?:string|int|integer|float|double|real|bool|boolean|array|object|binary)\\s*\\)(?=\\s*match\\s*\\()', re.IGNORECASE)
_PHP_NON_CODE = frozenset({'comment', 'string', 'encapsed_string', 'heredoc', 'nowdoc', 'text'})


def phpblankcasts(source: bytes) -> bytes:
    matches = list(_PHP_UNPARSED_CAST_RE.finditer(source))

    if not matches:
        return source

    import tree_sitter_php
    from tree_sitter import Language, Parser
    root = Parser(Language(tree_sitter_php.language_php())).parse(source).root_node
    out = bytearray(source)

    for m in matches:
        node = root.descendant_for_byte_range(m.start(), m.start() + 1)

        while node is not None and node.type not in _PHP_NON_CODE:
            node = node.parent

        if node is None:
            out[m.start():m.end()] = re.sub(b'[^\\r\\n]', b' ', m.group())

    return bytes(out)


def extphp(path: Path) -> dict:
    try:
        source = phpblankcasts(base.bytes(path))
    except Exception:
        source = None
    result = generic(path, _PHP_CONFIG, source_override=source)

    try:
        src = text(path, warn=False)
        masked, had_script = phpmask(src)

        if not had_script:
            return result

        js_result = generic(path, _JS_CONFIG, source_override=masked.encode('utf-8'))
        file_node_id = result['nodes'][0]['id'] if result.get('nodes') else None
        existing_ids = {n['id'] for n in result.get('nodes', [])}
        colliding_ids: set = set()

        for n in js_result.get('nodes', []):
            if n['id'] in existing_ids:
                if n['id'] != file_node_id:
                    colliding_ids.add(n['id'])
                continue

            result.setdefault('nodes', []).append(n)
            existing_ids.add(n['id'])

        for e in js_result.get('edges', []):
            if e.get('source') in colliding_ids or e.get('target') in colliding_ids:
                continue

            result.setdefault('edges', []).append(e)
    except Exception:
        pass

    return result


def extlua(path: Path) -> dict:
    return generic(path, _LUA_CONFIG)


def extswift(path: Path) -> dict:
    return generic(path, _SWIFT_CONFIG)


def canonicalizecsns(all_nodes: list[dict], all_edges: list[dict]) -> None:
    by_label: dict[str, list[dict]] = {}

    for node in all_nodes:
        if node.get('type') != 'namespace':
            continue

        label = node.get('label')

        if isinstance(label, str):
            by_label.setdefault(label, []).append(node)
    remap: dict[str, str] = {}
    drop_node_ids: set[int] = set()

    for group in by_label.values():
        if len(group) < 2:
            continue

        canonical = sorted(group, key=lambda node: (str(node.get('source_file') or ''), str(node.get('source_location') or ''), str(node.get('id') or '')))[0]
        canonical_id = canonical.get('id')

        for node in group:
            if node is canonical:
                continue

            drop_node_ids.add(id(node))
            dup_id = node.get('id')

            if isinstance(dup_id, str) and isinstance(canonical_id, str):
                remap[dup_id] = canonical_id

    if remap:
        for edge in all_edges:
            if edge.get('source') in remap:
                edge['source'] = remap[str(edge['source'])]

            if edge.get('target') in remap:
                edge['target'] = remap[str(edge['target'])]

    if drop_node_ids:
        all_nodes[:] = [node for node in all_nodes if id(node) not in drop_node_ids]
_CASE_INSENSITIVE_EXTS = frozenset({'.php', '.phtml', '.php3', '.php4', '.php5', '.php7', '.phps', '.sql', '.nim', '.nims', '.nimble', '.cbl', '.cob', '.cobol', '.cpy', '.vb'})


def caseinsensitive(source_file: object) -> bool:
    if not source_file:
        return False

    return Path(str(source_file)).suffix.lower() in _CASE_INSENSITIVE_EXTS

_LANG_FAMILY_BY_EXT: dict[str, str] = {'.js': 'jsts', '.jsx': 'jsts', '.mjs': 'jsts', '.cjs': 'jsts', '.ts': 'jsts', '.tsx': 'jsts', '.mts': 'jsts', '.cts': 'jsts', '.vue': 'jsts', '.svelte': 'jsts', '.astro': 'jsts', '.java': 'jvm', '.kt': 'jvm', '.kts': 'jvm', '.scala': 'jvm', '.groovy': 'jvm', '.gradle': 'jvm', '.c': 'native', '.h': 'native', '.cpp': 'native', '.cc': 'native', '.cxx': 'native', '.hpp': 'native', '.cu': 'native', '.cuh': 'native', '.metal': 'native', '.m': 'native', '.mm': 'native', '.swift': 'native', '.py': 'python', '.go': 'go', '.rs': 'rust', '.cbl': 'cobol', '.cob': 'cobol', '.cobol': 'cobol', '.cpy': 'cobol', '.r': 'r', '.sol': 'solidity', '.erl': 'erlang', '.hrl': 'erlang', '.escript': 'erlang', '.rb': 'ruby', '.rake': 'ruby', '.php': 'php', '.phtml': 'php', '.php3': 'php', '.php4': 'php', '.php5': 'php', '.php7': 'php', '.phps': 'php', '.cs': 'dotnet', '.vb': 'dotnet', '.razor': 'dotnet', '.cshtml': 'dotnet', '.xaml': 'dotnet', '.lua': 'lua', '.luau': 'lua', '.zig': 'zig', '.ex': 'elixir', '.exs': 'elixir', '.jl': 'julia', '.dart': 'dart', '.sh': 'shell', '.bash': 'shell', '.ps1': 'powershell', '.psm1': 'powershell', '.psd1': 'powershell'}


def langfamily(source_file: object) -> str | None:
    if not source_file:
        return None

    return _LANG_FAMILY_BY_EXT.get(Path(str(source_file)).suffix.lower())

_LANGUAGE_BUILTIN_BASE_CLASSES: dict[str, frozenset[str]] = {'php': frozenset({'Throwable', 'Exception', 'ErrorException', 'Error', 'TypeError', 'ValueError', 'ArgumentCountError', 'ArithmeticError', 'DivisionByZeroError', 'RuntimeException', 'LogicException', 'InvalidArgumentException', 'DomainException', 'LengthException', 'OutOfRangeException', 'OutOfBoundsException', 'RangeException', 'OverflowException', 'UnderflowException', 'UnexpectedValueException', 'BadFunctionCallException', 'BadMethodCallException', 'JsonException'}), 'jvm': frozenset({'Throwable', 'Exception', 'RuntimeException', 'Error', 'IllegalArgumentException', 'IllegalStateException', 'UnsupportedOperationException', 'IndexOutOfBoundsException', 'NullPointerException', 'IOException'}), 'python': frozenset({'BaseException', 'Exception', 'ValueError', 'TypeError', 'KeyError', 'IndexError', 'RuntimeError', 'NotImplementedError', 'AttributeError', 'OSError', 'IOError', 'StopIteration', 'Warning', 'UserWarning', 'DeprecationWarning'}), 'jsts': frozenset({'Error', 'TypeError', 'RangeError', 'SyntaxError', 'ReferenceError', 'EvalError', 'URIError', 'AggregateError'}), 'dotnet': frozenset({'Exception', 'ApplicationException', 'SystemException', 'ArgumentException', 'ArgumentNullException', 'ArgumentOutOfRangeException', 'InvalidOperationException', 'NotImplementedException', 'NotSupportedException'}), 'ruby': frozenset({'Exception', 'StandardError', 'RuntimeError', 'ArgumentError', 'TypeError', 'NameError', 'NoMethodError', 'IOError'})}
_LANGUAGE_BUILTIN_BASE_CLASSES_CI: dict[str, frozenset[str]] = {family: frozenset((name.lower() for name in names)) for family, names in _LANGUAGE_BUILTIN_BASE_CLASSES.items()}


def nodelabelkey(node: dict, fold: bool=False) -> str:
    label = str(node.get('label', '')).strip()
    key = re.sub('[^a-zA-Z0-9]+', '', label)

    return key.lower() if fold else key


def istoplevel(node: dict) -> bool:
    label = str(node.get('label', '')).strip()

    return node.get('file_type') == 'code' and label.endswith(')') and (not label.startswith('.')) and ('.' not in label)


def rewirestubs(nodes: list[dict], edges: list[dict]) -> None:
    real_by_label: dict[str, list[dict]] = {}
    real_by_label_ci: dict[str, list[dict]] = {}
    func_by_label: dict[str, list[dict]] = {}
    stubs: list[dict] = []

    for node in nodes:
        key = nodelabelkey(node)

        if not key:
            continue

        if node.get('source_file'):
            if istypelike(node):
                real_by_label.setdefault(key, []).append(node)

                if caseinsensitive(node.get('source_file')):
                    real_by_label_ci.setdefault(nodelabelkey(node, fold=True), []).append(node)
            elif istoplevel(node):
                func_by_label.setdefault(key, []).append(node)
            continue

        stubs.append(node)
    stub_ids = {str(s.get('id')) for s in stubs if s.get('id')}
    stub_families: dict[str, set] = {}
    supertype_stub_ids: set[str] = set()
    _SUPERTYPE_RELATIONS = {'inherits', 'implements', 'extends'}

    for edge in edges:
        rel = edge.get('relation')

        for endpoint in ('source', 'target'):
            nid = edge.get(endpoint)

            if nid in stub_ids:
                fam = langfamily(edge.get('source_file'))

                if fam is not None:
                    stub_families.setdefault(str(nid), set()).add(fam)

                if endpoint == 'target' and rel in _SUPERTYPE_RELATIONS:
                    supertype_stub_ids.add(str(nid))
    remap: dict[str, str] = {}

    for stub in stubs:
        stub_id = str(stub.get('id', ''))

        if not stub_id:
            continue

        candidates = real_by_label.get(nodelabelkey(stub), [])

        if len(candidates) != 1:
            candidates = real_by_label_ci.get(nodelabelkey(stub, fold=True), [])

        if len(candidates) != 1:
            fcands = func_by_label.get(nodelabelkey(stub), [])

            if len(fcands) == 1 and stub_id not in supertype_stub_ids:
                fams = stub_families.get(stub_id, set())
                cand_fam = langfamily(fcands[0].get('source_file'))

                if not fams or cand_fam is None or cand_fam in fams:
                    candidates = fcands

        if len(candidates) != 1:
            continue

        target_id = candidates[0].get('id')

        if isinstance(target_id, str) and target_id and (target_id != stub_id):
            remap[stub_id] = target_id

    if not remap:
        return
    by_id = {node.get('id'): node for node in nodes if node.get('id')}
    csharp_scoped_relations = {'inherits', 'implements', 'references', 'imports'}

    def isbuiltinbase(edge: dict, stub_id: str, remapped_id: str) -> bool:
        if edge.get('relation') not in _SUPERTYPE_RELATIONS:
            return False

        edge_fam = langfamily(edge.get('source_file'))

        if edge_fam is None:
            return False

        label = str(by_id.get(stub_id, {}).get('label', '')).strip()

        if caseinsensitive(edge.get('source_file')):
            builtins = _LANGUAGE_BUILTIN_BASE_CLASSES_CI.get(edge_fam, frozenset())
            label = label.lower()
        else:
            builtins = _LANGUAGE_BUILTIN_BASE_CLASSES.get(edge_fam, frozenset())

        if label not in builtins:
            return False

        target_fam = langfamily(by_id.get(remapped_id, {}).get('source_file'))

        return target_fam is not None and target_fam != edge_fam

    def crosslangcandidate(edge: dict, remapped_id: str) -> bool:
        edge_fam = langfamily(edge.get('source_file'))
        cand_fam = langfamily(by_id.get(remapped_id, {}).get('source_file'))

        return edge_fam is not None and cand_fam is not None and (edge_fam != cand_fam)

    for edge in edges:
        is_csharp_scoped_edge = str(edge.get('source_file', '')).endswith(('.cs', '.razor', '.cshtml')) and edge.get('relation') in csharp_scoped_relations
        source = edge.get('source')

        if source in remap:
            remapped_source = remap[str(source)]

            if not (is_csharp_scoped_edge and str(by_id.get(remapped_source, {}).get('source_file', '')).endswith('.cs')) and (not crosslangcandidate(edge, remapped_source)):
                edge['source'] = remapped_source
        target = edge.get('target')

        if target in remap:
            remapped_target = remap[str(target)]

            if not (is_csharp_scoped_edge and str(by_id.get(remapped_target, {}).get('source_file', '')).endswith('.cs')) and (not isbuiltinbase(edge, str(target), remapped_target)) and (not crosslangcandidate(edge, remapped_target)):
                edge['target'] = remapped_target
    referenced = {x for e in edges for x in (e.get('source'), e.get('target'))}
    drop_ids = {stub_id for stub_id in remap if stub_id not in referenced}
    nodes[:] = [node for node in nodes if node.get('id') not in drop_ids]


def augmentreexports(paths: list[Path], nodes: list[dict], edges: list[dict], root: Path) -> None:
    facts = _SymbolResolutionFacts()
    collectjsfacts(paths, facts)
    applyfacts(paths, nodes, edges, root, facts)


def remapobjcfields(per_file: list, mapping: dict) -> None:
    for result in per_file:
        ft = result.get('objc_field_types') if isinstance(result, dict) else None
        tables = ft.get('tables') if isinstance(ft, dict) else None

        if not isinstance(tables, dict):
            continue

        if any((k in mapping for k in tables)):
            ft['tables'] = {mapping.get(k, k): v for k, v in tables.items()}


def mergeswiftexts(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    extension_nids: set[str] = set()
    extension_labels: dict[str, str] = {}

    for result in per_file:
        for ext in result.get('swift_extensions', []) or []:
            extension_nids.add(ext['nid'])
            extension_labels[ext['nid']] = ext['label']

    if not extension_nids:
        return
    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}
    label_to_canonical: dict[str, list[str]] = {}

    for n in all_nodes:
        if n.get('id') in extension_nids:
            continue

        label = n.get('label')

        if not label:
            continue

        if langfamily(n.get('source_file')) != 'native':
            continue

        if label in GLOBALS:
            continue

        if not (n.get('source_file') and n.get('id') in contained and istypelike(n)):
            continue

        label_to_canonical.setdefault(label, []).append(n['id'])
    remap: dict[str, str] = {}

    for ext_nid in extension_nids:
        candidates = label_to_canonical.get(extension_labels[ext_nid], [])

        if len(candidates) != 1:
            continue

        canonical_nid = candidates[0]

        if canonical_nid != ext_nid:
            remap[ext_nid] = canonical_nid

    if not remap:
        return
    all_nodes[:] = [n for n in all_nodes if n.get('id') not in remap]

    def keyof(e: dict, src: str, tgt: str) -> tuple:
        return (src, tgt, e.get('relation'), e.get('source_file'), e.get('source_location'))

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()

    for e in all_edges:
        src0, tgt0 = (e.get('source'), e.get('target'))
        src = remap.get(src0, src0)
        tgt = remap.get(tgt0, tgt0)

        if src == src0 and tgt == tgt0:
            seen_keys.add(keyof(e, src0, tgt0))
            rewritten.append(e)
            continue

        if src == tgt:
            continue

        e['source'] = src
        e['target'] = tgt
        key = keyof(e, src, tgt)

        if key in seen_keys:
            continue

        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten


def mergecspartials(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict], paths: list[Path], root: Path) -> None:
    groups: dict[tuple[str, str], list[dict]] = {}

    for n in all_nodes:
        if not str(n.get('source_file', '')).endswith('.cs'):
            continue

        if n.get('file_type') != 'code':
            continue

        md = n.get('metadata') or {}

        if not md.get('is_partial') or md.get('is_nested_type'):
            continue

        label = n.get('label')

        if not label:
            continue

        groups.setdefault((str(md.get('namespace', '')), str(label)), []).append(n)

    if not any((len(members) >= 2 for members in groups.values())):
        return
    nid_to_path: dict[str, Path] = {}

    for result, path in zip(per_file, paths):
        for pn in result.get('nodes') or []:
            nid_to_path.setdefault(pn['id'], path)
    proj_exts = ('.csproj', '.fsproj', '.vbproj')
    project_dirs: set[Path] = set()

    for p in paths:
        if p.suffix.lower() in proj_exts:
            try:
                project_dirs.add(p.resolve().parent)
            except OSError:
                pass

    try:
        stop = root.resolve()
    except OSError:
        stop = root
    dir_assembly: dict[Path, str] = {}

    def assemblyofdir(d: Path) -> str:
        chain: list[Path] = []
        key = ''

        while True:
            cached = dir_assembly.get(d)

            if cached is not None:
                key = cached
                break

            chain.append(d)

            if d in project_dirs:
                key = str(d)
                break

            try:
                has_project = any((c.suffix.lower() in proj_exts for c in d.iterdir()))
            except OSError:
                has_project = False

            if has_project:
                key = str(d)
                break

            if d == stop or d.parent == d:
                break

            d = d.parent

        for c in chain:
            dir_assembly[c] = key

        return key

    def assemblyofnode(nid: str) -> str:
        path = nid_to_path.get(nid)

        if path is None:
            return ''

        try:
            d = path.resolve().parent
        except OSError:
            return ''

        return assemblyofdir(d)

    remap: dict[str, str] = {}

    for members in groups.values():
        if len(members) < 2:
            continue

        by_assembly: dict[str, list[dict]] = {}

        for n in members:
            by_assembly.setdefault(assemblyofnode(n['id']), []).append(n)

        for halves in by_assembly.values():
            if len(halves) < 2:
                continue

            halves.sort(key=lambda n: (str(n.get('source_file', '')), str(n.get('source_location', '')), str(n.get('id', ''))))
            canonical_nid = halves[0]['id']

            for other in halves[1:]:
                if other['id'] != canonical_nid:
                    remap[other['id']] = canonical_nid

    if not remap:
        return
    all_nodes[:] = [n for n in all_nodes if n.get('id') not in remap]

    def keyof(e: dict, src: str | None, tgt: str | None) -> tuple:
        return (src, tgt, e.get('relation'), e.get('source_file'), e.get('source_location'))

    rewritten: list[dict] = []
    seen_keys: set[tuple] = set()

    for e in all_edges:
        src0, tgt0 = (e.get('source'), e.get('target'))
        src = remap.get(src0, src0)
        tgt = remap.get(tgt0, tgt0)

        if src == src0 and tgt == tgt0:
            seen_keys.add(keyof(e, src0, tgt0))
            rewritten.append(e)
            continue

        if src == tgt and src0 != tgt0:
            continue

        e['source'] = src
        e['target'] = tgt
        key = keyof(e, src, tgt)

        if key in seen_keys:
            continue

        seen_keys.add(key)
        rewritten.append(e)
    all_edges[:] = rewritten

    for result in per_file:
        for rc in result.get('raw_calls', []) or []:
            cn = rc.get('caller_nid')

            if cn in remap:
                rc['caller_nid'] = remap[cn]
UNRESOLVED_CALLS_KEY = 'unresolved_calls'
_MAX_PARKED_CALLS_PER_NODE = 64


def parkcall(caller_node: dict | None, callee: str, receiver_type: str, lang: str, raw_call: dict) -> None:
    if not caller_node or not callee or (not receiver_type):
        return
    metadata = caller_node.setdefault('metadata', {})

    if not isinstance(metadata, dict):
        return
    parked = metadata.setdefault(UNRESOLVED_CALLS_KEY, [])

    if not isinstance(parked, list) or len(parked) >= _MAX_PARKED_CALLS_PER_NODE:
        return
    callee, receiver_type = (str(callee), str(receiver_type))

    for previous in parked:
        if isinstance(previous, dict) and previous.get('callee') == callee and (previous.get('receiver_type') == receiver_type):
            return
    entry = {'callee': callee, 'receiver_type': receiver_type, 'lang': lang}
    location = raw_call.get('source_location')

    if location:
        entry['line'] = str(location)
    parked.append(entry)


def resolveswiftmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    type_table_by_file: dict[str, dict[str, str]] = {}

    for result in per_file:
        tt = result.get('swift_type_table')

        if tt and tt.get('path'):
            type_table_by_file[tt['path']] = tt.get('table', {})

    if not type_table_by_file:
        return

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}

    for n in all_nodes:
        node_by_id[n.get('id')] = n

        if n.get('source_file') and n.get('id') in contained and istypelike(n):
            type_def_nids.setdefault(key(n.get('label', '')), []).append(n['id'])
    method_index: dict[tuple[str, str], str] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))
        tnode = node_by_id.get(tgt)

        if tnode is not None:
            method_index[src, key(tnode.get('label', ''))] = tgt
    factory_by_file: dict[str, dict] = {}

    for result in per_file:
        tt = result.get('swift_type_table')

        if tt and tt.get('path') and tt.get('factory'):
            factory_by_file[tt['path']] = tt['factory']

    if factory_by_file:
        return_targets_by_method: dict[str, set[str]] = {}

        for e in all_edges:
            if e.get('relation') == 'references' and e.get('context') == 'return_type' and (e.get('metadata') or {}).get('swift_plain_return'):
                return_targets_by_method.setdefault(e.get('source'), set()).add(e.get('target'))

        for path, pending in factory_by_file.items():
            table = dict(type_table_by_file.get(path, {}))
            type_table_by_file[path] = table

            for receiver, bind in pending.items():
                try:
                    factory_type, factory_method = bind
                except (TypeError, ValueError):
                    continue

                if factory_type in GLOBALS:
                    continue

                factory_defs = type_def_nids.get(key(factory_type), [])

                if len(factory_defs) != 1:
                    continue

                method_nid = method_index.get((factory_defs[0], key(factory_method)))

                if method_nid is None:
                    continue

                targets = return_targets_by_method.get(method_nid, set())

                if len(targets) != 1:
                    continue

                tnode = node_by_id.get(next(iter(targets)))
                ret_label = str(tnode.get('label', '')) if tnode else ''

                if not ret_label or ret_label in GLOBALS:
                    continue

                if len(type_def_nids.get(key(ret_label), [])) != 1:
                    continue

                table.setdefault(receiver, ret_label)
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in all_raw_calls:
        if not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if not receiver or not callee or (not caller):
            continue

        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True
        else:
            type_name = type_table_by_file.get(rc.get('source_file', ''), {}).get(receiver)
            type_qualified = False

        if not type_name:
            continue

        if type_name in GLOBALS:
            continue

        type_defs = type_def_nids.get(key(type_name), [])

        if not type_defs:
            if str(rc.get('source_file', '')).lower().endswith('.swift'):
                parkcall(node_by_id.get(caller), callee, type_name, 'swift', rc)
            continue

        if len(type_defs) != 1:
            continue

        type_nid = type_defs[0]
        method_nid = method_index.get((type_nid, key(callee)))
        target = method_nid or type_nid
        relation = 'calls' if method_nid else 'references'

        if target == caller or (caller, target) in existing_pairs:
            continue

        existing_pairs.add((caller, target))
        all_edges.append({'source': caller, 'target': target, 'relation': relation, 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolvepymembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    node_by_id: dict[str, dict] = {n.get('id'): n for n in all_nodes}
    class_def_nids: dict[str, list[str]] = {}
    method_index: dict[tuple[str, str], str] = {}
    owner_of: dict[str, str] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))

        if src and tgt:
            owner_of[tgt] = src
        cnode = node_by_id.get(src)

        if cnode is not None:
            class_def_nids.setdefault(key(cnode.get('label', '')), []).append(src)
        tnode = node_by_id.get(tgt)

        if tnode is not None:
            method_index[src, key(tnode.get('label', ''))] = tgt

    for k in list(class_def_nids):
        class_def_nids[k] = sorted(set(class_def_nids[k]))
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    contains_children: dict[str, dict[str, list[str]]] = {}
    file_of_node: dict[str, str] = {}

    for e in all_edges:
        if e.get('relation') == 'contains':
            src, tgt = (e.get('source'), e.get('target'))
            tnode = node_by_id.get(tgt)

            if tnode is not None:
                contains_children.setdefault(src, {}).setdefault(key(tnode.get('label', '')), []).append(tgt)
                file_of_node[tgt] = src
    imported_by_filenode: dict[str, set[str]] = {}
    import_alias_by_filenode: dict[str, dict[str, str]] = {}
    external_plain_imports: dict[str, dict[str, list[str]]] = {}

    for e in all_edges:
        if e.get('relation') in ('imports', 'imports_from'):
            imported_by_filenode.setdefault(e.get('source'), set()).add(e.get('target'))
            alias = e.get('local_alias')

            if alias:
                import_alias_by_filenode.setdefault(e.get('source'), {})[e.get('target')] = key(alias)

        if e.get('relation') == 'imports' and e.get('_python_plain_import') is True:
            src, target = (e.get('source'), e.get('target'))

            if not isinstance(src, str) or not isinstance(target, str):
                continue

            if target in node_by_id or target in contains_children or target in file_of_node:
                continue

            binding = e.get('local_alias') or target

            if isinstance(binding, str):
                external_plain_imports.setdefault(src, {}).setdefault(binding, []).append(target)

    def modulestemkey(nid: str) -> str:
        n = node_by_id.get(nid)

        if not n:
            return ''

        sf = n.get('source_file') or ''
        stem = Path(sf).stem if sf else ''

        return key(stem or n.get('label', ''))

    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}
    method_class: dict[str, str] = {}

    for e in all_edges:
        src, tgt = (e.get('source'), e.get('target'))

        if e.get('relation') == 'method' and isinstance(src, str) and isinstance(tgt, str):
            method_class[tgt] = src

    def filenode(nid: str) -> 'str | None':
        seen: set[str] = set()
        parent: 'str | None' = None

        while nid and nid not in seen:
            seen.add(nid)
            up = file_of_node.get(nid) or method_class.get(nid)

            if not up:
                return parent

            parent = nid = up

        return None

    def emitcall(caller: str, target_nid: 'str | None', rc: dict) -> None:
        if not target_nid or target_nid == caller or (caller, target_nid) in existing_pairs:
            return
        existing_pairs.add((caller, target_nid))
        all_edges.append({'source': caller, 'target': target_nid, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})
    inherits_edges = [dict(e) for e in all_edges if e.get('relation') == 'inherits']

    if any((not node_by_id.get(str(e.get('target')), {}).get('source_file') for e in inherits_edges)):
        rewirestubs(list(all_nodes), inherits_edges)
    bases_of: dict[str, list[str]] = {}

    for e in inherits_edges:
        src, tgt = (e.get('source'), e.get('target'))

        if isinstance(src, str) and isinstance(tgt, str):
            bases_of.setdefault(src, [])

            if tgt not in bases_of[src]:
                bases_of[src].append(tgt)

    def inheritedmethod(caller: str, callee: str, skip_own: bool) -> 'str | None':
        cls = method_class.get(caller)
        seen: set[str] = set()
        first_hop = True

        while cls and cls not in seen:
            seen.add(cls)

            if not skip_own:
                hit = method_index.get((cls, key(callee)))

                if hit:
                    return hit

            skip_own = False

            if (node_by_id.get(cls, {}).get('metadata') or {}).get('python_opaque_bases'):
                return None

            bases = bases_of.get(cls, [])

            if len(bases) != 1 or bases[0] == cls:
                return None

            base = bases[0]
            cls_file, base_file = (filenode(cls), filenode(base))

            if cls_file is None or base_file is None or (not node_by_id.get(base, {}).get('source_file')):
                return None

            if first_hop:
                imported = imported_by_filenode.get(cls_file, set())

                if not (base_file == cls_file or base in imported or base_file in imported):
                    return None

            first_hop = False
            cls = base

        return None

    for rc in all_raw_calls:
        if rc.get('_ambiguous_python_import'):
            continue

        if not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if receiver in ('self', 'cls', 'super') and callee and caller and (not rc.get('_python_super_args')):
            target = inheritedmethod(caller, callee, skip_own=receiver == 'super')

            if target:
                emitcall(caller, target, rc)
                continue

        attr_type = rc.get('_python_self_attr_type')

        if attr_type and callee and caller:
            class_nids = class_def_nids.get(key(attr_type), [])
            caller_file = file_of_node.get(owner_of.get(caller, ''))

            if len(class_nids) == 1 and caller_file is not None and (file_of_node.get(class_nids[0]) == caller_file or class_nids[0] in imported_by_filenode.get(caller_file, ())):
                method_nid = method_index.get((class_nids[0], key(callee)))
                emitcall(caller, method_nid, rc)
            continue

        if not receiver or not callee or (not caller):
            continue

        if rc.get('_python_receiver_shadowed') is False:
            caller_file = file_of_node.get(caller)
            targets = external_plain_imports.get(caller_file, {}).get(receiver, []) if caller_file is not None else []

            if len(targets) == 1:
                emitcall(caller, targets[0], rc)
                continue

            if targets:
                continue

        receiver_type = rc.get('receiver_type')

        if receiver_type and (not receiver[:1].isupper()):
            class_nids = class_def_nids.get(key(receiver_type), [])
            caller_file = filenode(caller)

            if len(class_nids) == 1 and caller_file is not None:
                cls = class_nids[0]
                imported = imported_by_filenode.get(caller_file, set())
                cls_file = filenode(cls)

                if cls_file == caller_file or cls in imported or (cls_file is not None and cls_file in imported):
                    emitcall(caller, method_index.get((cls, key(callee))), rc)
            continue

        if receiver[:1].isupper():
            class_nids = class_def_nids.get(key(receiver), [])

            if len(class_nids) != 1:
                continue

            emitcall(caller, method_index.get((class_nids[0], key(callee))), rc)
        else:
            rkey = key(receiver)
            caller_file = file_of_node.get(caller)
            file_aliases = import_alias_by_filenode.get(caller_file, {})
            mods = [t for t in imported_by_filenode.get(caller_file, ()) if t in contains_children and t not in file_of_node and (modulestemkey(t) == rkey or file_aliases.get(t) == rkey)]

            if len(mods) != 1:
                continue

            children = contains_children[mods[0]].get(key(callee), [])

            if len(children) != 1:
                continue

            emitcall(caller, children[0], rc)


def resolvetsmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    type_table_by_file: dict[str, dict[str, str]] = {}

    for result in per_file:
        tt = result.get('ts_type_table')

        if tt and tt.get('path'):
            type_table_by_file[tt['path']] = tt.get('table', {})

    if not type_table_by_file:
        return

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}

    for n in all_nodes:
        node_by_id[n.get('id')] = n

        if n.get('source_file') and n.get('id') in contained and istypelike(n):
            type_def_nids.setdefault(key(n.get('label', '')), []).append(n['id'])
    method_index: dict[tuple[str, str], str] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))
        tnode = node_by_id.get(tgt)

        if tnode is not None:
            method_index[src, key(tnode.get('label', ''))] = tgt
    file_of_node: dict[str, str] = {}

    for e in all_edges:
        if e.get('relation') == 'contains':
            file_of_node[e.get('target')] = e.get('source')

    for e in all_edges:
        if e.get('relation') == 'method':
            owner_file = file_of_node.get(e.get('source'))

            if owner_file is not None:
                file_of_node.setdefault(e.get('target'), owner_file)
    imported_by_filenode: dict[str, set[str]] = {}

    for e in all_edges:
        if e.get('relation') in ('imports', 'imports_from'):
            imported_by_filenode.setdefault(e.get('source'), set()).add(e.get('target'))
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in all_raw_calls:
        if not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if not receiver or not callee or (not caller):
            continue

        if receiver[:1].isupper():
            type_name = receiver
            type_qualified = True
        else:
            type_qualified = False
            type_name = type_table_by_file.get(rc.get('source_file', ''), {}).get(receiver)

        if not type_name:
            continue

        if type_name in GLOBALS:
            continue

        type_defs = type_def_nids.get(key(type_name), [])
        caller_file = file_of_node.get(caller)
        imported = imported_by_filenode.get(caller_file, set())

        def visible(nid: str) -> bool:
            type_file = file_of_node.get(nid)

            return caller_file is not None and caller_file == type_file or nid in imported or (type_file is not None and type_file in imported)

        visible = [nid for nid in type_defs if visible(nid)]

        if len(visible) != 1:
            continue

        type_nid = visible[0]
        method_nid = method_index.get((type_nid, key(callee)))

        if not method_nid:
            continue

        method_name = str(node_by_id.get(method_nid, {}).get('label', '')).lstrip('.')

        if method_name.startswith('#') != str(callee).startswith('#'):
            continue

        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue

        existing_pairs.add((caller, method_nid))
        all_edges.append({'source': caller, 'target': method_nid, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolvecppmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    type_table_by_file: dict[str, dict[str, str]] = {}

    for result in per_file:
        tt = result.get('cpp_type_table')

        if tt and tt.get('path'):
            type_table_by_file[tt['path']] = tt.get('table', {})

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}

    for n in all_nodes:
        node_by_id[n.get('id')] = n

        if n.get('source_file') and n.get('id') in contained and istypelike(n):
            type_def_nids.setdefault(key(n.get('label', '')), []).append(n['id'])
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}

    for rel in ('defines', 'method'):
        for e in all_edges:
            if e.get('relation') != rel:
                continue

            src, tgt = (e.get('source'), e.get('target'))
            tnode = node_by_id.get(tgt)

            if tnode is None:
                continue

            enclosing_type.setdefault(tgt, src)
            method_index[src, key(tnode.get('label', ''))] = tgt
    qualified_method_nids: dict[str, list[str]] = {}

    for n in all_nodes:
        label = str(n.get('label', ''))

        if n.get('source_file') and label.endswith('()') and ('::' in label):
            qualified_method_nids.setdefault(label, []).append(n['id'])
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in all_raw_calls:
        if not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if not receiver or not callee or (not caller):
            continue

        src_file = rc.get('source_file', '')

        if rc.get('lang') != 'cpp':
            continue

        qualified_fallback_label: str | None = None

        if receiver == 'this':
            type_nid = enclosing_type.get(caller)

            if not type_nid:
                continue

            type_qualified = True
        elif receiver[:1].isupper():
            qualified_fallback_label = f'{receiver}::{callee}()'
            type_defs = type_def_nids.get(key(receiver), [])

            if not type_defs:
                parkcall(node_by_id.get(caller), callee, receiver, 'cpp', rc)
                continue

            if len(type_defs) != 1:
                continue

            type_nid = type_defs[0]
            type_qualified = True
        else:
            type_name = type_table_by_file.get(src_file, {}).get(receiver)

            if not type_name:
                continue

            type_defs = type_def_nids.get(key(type_name), [])

            if not type_defs:
                parkcall(node_by_id.get(caller), callee, type_name, 'cpp', rc)
                continue

            if len(type_defs) != 1:
                continue

            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, key(callee)))

        if method_nid is None and qualified_fallback_label is not None:
            candidates = qualified_method_nids.get(qualified_fallback_label, [])

            if len(candidates) == 1:
                method_nid = candidates[0]
        target = method_nid or type_nid
        relation = 'calls' if method_nid else 'references'

        if target == caller or (caller, target) in existing_pairs:
            continue

        existing_pairs.add((caller, target))
        all_edges.append({'source': caller, 'target': target, 'relation': relation, 'context': 'call', 'source_file': src_file, 'source_location': rc.get('source_location')})


def resolvecsmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}
    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}

    for n in all_nodes:
        node_by_id[n.get('id')] = n

        if n.get('source_file') and n.get('id') in contained and istypelike(n):
            type_def_nids.setdefault(key(n.get('label', '')), []).append(n['id'])
    resolver = csharp.resolver(all_nodes, all_edges)
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))
        tnode = node_by_id.get(tgt)

        if tnode is None:
            continue

        enclosing_type.setdefault(tgt, src)
        method_index[src, key(tnode.get('label', ''))] = tgt
    bases_of: dict[str, list[str]] = {}
    unresolved_base: set[str] = set()

    for e in all_edges:
        if e.get('relation') != 'inherits':
            continue

        src_file = e.get('source_file')

        if not (isinstance(src_file, str) and src_file.endswith('.cs')):
            continue

        src, tgt = (e.get('source'), e.get('target'))

        if not (isinstance(src, str) and isinstance(tgt, str)):
            continue

        tnode = node_by_id.get(tgt)

        if tnode is None or not tnode.get('source_file'):
            unresolved_base.add(src)
        else:
            bucket = bases_of.setdefault(src, [])

            if tgt not in bucket:
                bucket.append(tgt)

    def methodontype(type_nid: str, callee_key: str) -> str | None:
        hits: set[str] = set()
        seen: set[str] = set()
        frontier = [type_nid]

        while frontier:
            nid = frontier.pop()

            if nid in seen:
                continue

            seen.add(nid)
            method_nid = method_index.get((nid, callee_key))

            if method_nid:
                hits.add(method_nid)
                continue

            if nid in unresolved_base:
                return None

            frontier.extend(bases_of.get(nid, []))

        return next(iter(hits)) if len(hits) == 1 else None

    def resolvetypenid(type_name: str | None, caller_node: dict | None, src_file: str) -> str | None:
        if not type_name:
            return None

        if caller_node is not None:
            resolved, decisive = csharp.type(resolver, type_name, caller_node, src_file)

            if resolved:
                return resolved

            if decisive:
                return None

        type_defs = type_def_nids.get(key(type_name), [])

        return type_defs[0] if len(type_defs) == 1 else None

    def parkabsent(type_name: str | None, caller_node: dict | None, rc: dict) -> None:
        if type_name and (not type_def_nids.get(key(type_name))):
            parkcall(caller_node, rc.get('callee'), type_name, 'csharp', rc)
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in all_raw_calls:
        if rc.get('lang') != 'csharp' or not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if not receiver or not callee or (not caller):
            continue

        src_file = rc.get('source_file', '')
        caller_node = node_by_id.get(caller)

        if receiver == 'this':
            type_nid = enclosing_type.get(caller)

            if not type_nid:
                continue

            type_qualified = True
        elif receiver == 'base':
            enclosing = enclosing_type.get(caller)

            if not enclosing or enclosing in unresolved_base:
                continue

            bases = bases_of.get(enclosing, [])

            if len(bases) != 1:
                continue

            type_nid = bases[0]
            type_qualified = True
        elif receiver[:1].isupper():
            type_name = rc.get('receiver_type')
            type_nid = resolvetypenid(type_name, caller_node, src_file)

            if not type_nid:
                type_nid = resolvetypenid(receiver, caller_node, src_file)

                if not type_nid:
                    parkabsent(type_name or receiver, caller_node, rc)
                    continue

            type_qualified = True
        else:
            type_name = rc.get('receiver_type')

            if not type_name:
                continue

            type_nid = resolvetypenid(type_name, caller_node, src_file)

            if not type_nid:
                parkabsent(type_name, caller_node, rc)
                continue

            type_qualified = False
        method_nid = methodontype(type_nid, key(callee))

        if not method_nid:
            continue

        if method_nid == caller or (caller, method_nid) in existing_pairs:
            continue

        existing_pairs.add((caller, method_nid))
        all_edges.append({'source': caller, 'target': method_nid, 'relation': 'calls', 'context': 'call', 'source_file': src_file, 'source_location': rc.get('source_location')})


def bindfieldtables(per_file: list[dict], all_nodes: list[dict], *, lang: str) -> dict[str, dict[str, str]]:
    by_label: dict[str, list[dict]] = {}

    for n in all_nodes:
        if n.get('label') and n.get('source_file'):
            by_label.setdefault(str(n['label']), []).append(n)
    bound: dict[str, dict[str, str]] = {}

    for result in per_file:
        for entry in result.get('member_field_tables') or []:
            if not isinstance(entry, dict) or entry.get('lang') != lang:
                continue

            label, fields = (entry.get('class_label'), entry.get('fields'))

            if not label or not isinstance(fields, dict):
                continue

            candidates = by_label.get(str(label), [])

            if len(candidates) > 1:
                sf = str(entry.get('source_file') or '')
                exact = [n for n in candidates if str(n.get('source_file')) == sf]

                if len(exact) == 1:
                    candidates = exact
                else:
                    base = os.path.basename(sf)
                    candidates = [n for n in candidates if os.path.basename(str(n.get('source_file'))) == base]

            if len(candidates) != 1:
                continue

            merged = bound.setdefault(candidates[0]['id'], {})

            for fname, tname in fields.items():
                if merged.get(fname) not in (None, tname):
                    merged.pop(fname, None)
                else:
                    merged[fname] = tname

    return bound


def resolvejavamembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:

    def key(label: str) -> str:
        return str(label).strip().removeprefix('.').removesuffix('()')

    contained = {edge.get('target') for edge in all_edges if edge.get('relation') == 'contains'}
    node_by_id = {node.get('id'): node for node in all_nodes}
    type_def_nids: dict[str, list[str]] = {}

    for node in all_nodes:
        if node.get('source_file') and node.get('id') in contained and istypelike(node):
            type_def_nids.setdefault(key(node.get('label', '')), []).append(node['id'])
    method_index: dict[tuple[str, str], set[str]] = {}
    enclosing_type: dict[str, str] = {}

    for edge in all_edges:
        if edge.get('relation') != 'method':
            continue

        owner, method = (edge.get('source'), edge.get('target'))
        method_node = node_by_id.get(method)

        if method_node is None:
            continue

        enclosing_type.setdefault(method, owner)
        method_index.setdefault((owner, key(method_node.get('label', ''))), set()).add(method)
    existing_pairs = {(edge.get('source'), edge.get('target')) for edge in all_edges}
    inherits_bases: dict[str, list[str]] = {}

    for edge in all_edges:
        if edge.get('relation') == 'inherits':
            inherits_bases.setdefault(edge['source'], []).append(edge['target'])
    class_fields = bindfieldtables(per_file, all_nodes, lang='java')

    def inheritedfieldtype(class_nid, field: str):
        seen: set = set()
        queue = [class_nid]

        while queue:
            cls = queue.pop(0)

            if not cls or cls in seen:
                continue

            seen.add(cls)
            hit = class_fields.get(cls, {}).get(field)

            if hit:
                return hit

            queue.extend(inherits_bases.get(cls, []))

        return None

    def jvmbases(type_nid: str) -> list[str] | None:
        bases = inherits_bases.get(type_nid, [])

        for base in bases:
            family = langfamily(node_by_id.get(base, {}).get('source_file'))

            if base == type_nid or family != 'jvm':
                return None

        return bases

    def methodontype(type_nid: str, callee_key: str) -> str | None:
        hits: set[str] = set()
        seen: set[str] = set()
        frontier = [type_nid]

        while frontier:
            nid = frontier.pop()

            if nid in seen:
                continue

            seen.add(nid)
            declared = method_index.get((nid, callee_key))

            if declared:
                hits |= declared
                continue

            bases = jvmbases(nid)

            if bases is None:
                return None

            frontier.extend(bases)

        return next(iter(hits)) if len(hits) == 1 else None

    for result in per_file:
        for raw_call in result.get('raw_calls', []):
            if raw_call.get('lang') != 'java' or not raw_call.get('is_member_call'):
                continue

            receiver = raw_call.get('receiver')
            callee = raw_call.get('callee')
            caller = raw_call.get('caller_nid')

            if not receiver or not callee or (not caller):
                continue

            exact = False

            if receiver == 'this':
                type_nid = enclosing_type.get(caller)
                exact = True

                if not type_nid:
                    continue
            elif receiver == 'super':
                superclasses = jvmbases(enclosing_type.get(caller, ''))

                if not superclasses or len(superclasses) != 1:
                    continue

                type_nid = superclasses[0]
                exact = True
            else:
                type_name = raw_call.get('receiver_type')

                if not type_name and receiver[:1].isupper():
                    type_name = receiver
                    exact = True

                if not type_name and receiver.startswith('this.'):
                    type_name = inheritedfieldtype(enclosing_type.get(caller), receiver[len('this.'):])

                if not type_name:
                    continue

                type_defs = type_def_nids.get(key(type_name), [])

                if not type_defs:
                    parkcall(node_by_id.get(caller), callee, type_name, 'java', raw_call)
                    continue

                if len(type_defs) != 1:
                    continue

                type_nid = type_defs[0]
            method_nid = methodontype(type_nid, key(callee))

            if not method_nid or method_nid == caller or (caller, method_nid) in existing_pairs:
                continue

            existing_pairs.add((caller, method_nid))
            all_edges.append({'source': caller, 'target': method_nid, 'relation': 'calls', 'context': 'call', 'source_file': raw_call.get('source_file', ''), 'source_location': raw_call.get('source_location')})


def resolveobjcmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    type_table_by_file: dict[str, dict[str, str]] = {}

    for result in per_file:
        tt = result.get('objc_type_table')

        if tt and tt.get('path'):
            type_table_by_file[tt['path']] = tt.get('table', {})
    field_types_by_class: dict[str, dict[str, str]] = {}
    field_conflicts: set[tuple[str, str]] = set()

    for result in per_file:
        ft = result.get('objc_field_types')

        if not ft:
            continue

        for cls_nid, tbl in (ft.get('tables') or {}).items():
            merged = field_types_by_class.setdefault(cls_nid, {})

            for field, tname in tbl.items():
                if (cls_nid, field) in field_conflicts:
                    continue

                prev = merged.get(field)

                if prev is None:
                    merged[field] = tname
                elif prev != tname:
                    del merged[field]
                    field_conflicts.add((cls_nid, field))

    def key(label: str) -> str:
        return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()

    contained = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}

    def isprotocol(n: dict) -> bool:
        label = str(n.get('label', '')).strip()

        return label.startswith('<') and label.endswith('>')

    type_def_nids: dict[str, list[str]] = {}
    node_by_id: dict[str, dict] = {}

    for n in all_nodes:
        node_by_id[n.get('id')] = n

        if n.get('source_file') and n.get('id') in contained and istypelike(n) and (not isprotocol(n)):
            type_def_nids.setdefault(key(n.get('label', '')), []).append(n['id'])
    method_index: dict[tuple[str, str], str] = {}
    enclosing_type: dict[str, str] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))
        enclosing_type.setdefault(tgt, src)
        tnode = node_by_id.get(tgt)

        if tnode is not None:
            method_index[src, key(tnode.get('label', ''))] = tgt
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_raw_calls.extend(result.get('raw_calls', []))
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}
    _objc_bases: dict[str, list[str]] = {}

    for e in all_edges:
        if e.get('relation') == 'inherits':
            _objc_bases.setdefault(e['source'], []).append(e['target'])

    def fieldtypechain(cls, receiver):
        seen: set = set()
        queue = [cls]

        while queue:
            c = queue.pop(0)

            if not c or c in seen:
                continue

            seen.add(c)
            hit = field_types_by_class.get(c, {}).get(receiver)

            if hit:
                return hit

            queue.extend(_objc_bases.get(c, []))

        return None

    for rc in all_raw_calls:
        if not rc.get('is_member_call'):
            continue

        receiver = rc.get('receiver')
        callee = rc.get('callee')
        caller = rc.get('caller_nid')

        if not receiver or not callee or (not caller):
            continue

        src_file = rc.get('source_file', '')

        if rc.get('lang') != 'objc':
            continue

        if rc.get('receiver_kind') == 'self_field':
            cls = enclosing_type.get(caller)
            type_name = fieldtypechain(cls, receiver) if cls else None

            if not type_name:
                continue

            type_defs = type_def_nids.get(key(type_name), [])

            if len(type_defs) != 1:
                continue

            type_nid = type_defs[0]
            type_qualified = False
        elif receiver in ('self', 'super'):
            type_nid = enclosing_type.get(caller)

            if not type_nid:
                continue

            type_qualified = True
        elif receiver[:1].isupper():
            type_defs = type_def_nids.get(key(receiver), [])

            if len(type_defs) != 1:
                continue

            type_nid = type_defs[0]
            type_qualified = True
        else:
            type_name = type_table_by_file.get(src_file, {}).get(receiver)

            if not type_name:
                cls = enclosing_type.get(caller)
                type_name = fieldtypechain(cls, receiver) if cls else None

            if not type_name:
                continue

            type_defs = type_def_nids.get(key(type_name), [])

            if len(type_defs) != 1:
                continue

            type_nid = type_defs[0]
            type_qualified = False
        method_nid = method_index.get((type_nid, key(callee)))
        target = method_nid or type_nid
        relation = 'calls' if method_nid else 'references'

        if target == caller or (caller, target) in existing_pairs:
            continue

        existing_pairs.add((caller, target))
        all_edges.append({'source': caller, 'target': target, 'relation': relation, 'context': 'call', 'source_file': src_file, 'source_location': rc.get('source_location')})


def ktpackageindex(per_file: list[dict]) -> dict[str, list[dict]]:
    pkg_results: dict[str, list[dict]] = {}

    for result in per_file:
        pkg = result.get('kotlin_package')

        if pkg:
            pkg_results.setdefault(pkg, []).append(result)

    return pkg_results


def resolvektimporttargets(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    pkg_results = ktpackageindex(per_file)

    if not pkg_results:
        return
    pkg_symbols: dict[str, dict[str, list[str]]] = {}

    for pkg, results in pkg_results.items():
        by_label = pkg_symbols.setdefault(pkg, {})

        for result in results:
            for n in result.get('nodes', []):
                if not n.get('source_file') or n.get('type') == 'namespace':
                    continue

                label = str(n.get('label', ''))

                if not label or label.startswith('.'):
                    continue

                by_label.setdefault(label.strip('()'), []).append(n['id'])

    for e in all_edges:
        if e.get('relation') != 'imports':
            continue

        if not str(e.get('source_file', '')).endswith(('.kt', '.kts')):
            continue

        fqn = (e.get('metadata') or {}).get('target_fqn', '')
        pkg, _, name = str(fqn).rpartition('.')

        if not pkg or not name:
            continue

        candidates = pkg_symbols.get(pkg, {}).get(name, [])

        if len(candidates) == 1:
            e['target'] = candidates[0]


def resolvecsquals(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    raw = [rc for result in per_file for rc in result.get('raw_calls', []) if rc.get('lang') == 'csharp' and rc.get('qualified_prefix') and rc.get('callee') and rc.get('caller_nid')]

    if not raw:
        return
    by_namespace: dict[tuple[str, str], list[str]] = {}

    for n in all_nodes:
        if not n.get('_callable_class') or not n.get('source_file'):
            continue

        namespace = str((n.get('metadata') or {}).get('namespace') or '')
        label = str(n.get('label', '')).strip('()')

        if namespace and label:
            by_namespace.setdefault((namespace, label), []).append(n['id'])

    if not by_namespace:
        return
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges if e.get('relation') == 'calls'}

    for rc in raw:
        candidates = by_namespace.get((rc['qualified_prefix'], rc['callee']), [])

        if len(candidates) != 1:
            continue

        caller = rc['caller_nid']
        tgt = candidates[0]

        if tgt == caller or (caller, tgt) in existing_pairs:
            continue

        existing_pairs.add((caller, tgt))
        all_edges.append({'source': caller, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolvektquals(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    pkg_results = ktpackageindex(per_file)

    if not pkg_results:
        return
    raw = [rc for result in per_file for rc in result.get('raw_calls', []) if rc.get('lang') == 'kotlin' and rc.get('qualified_prefix') and rc.get('callee') and rc.get('caller_nid')]

    if not raw:
        return
    node_by_id: dict[str, dict] = {n.get('id'): n for n in all_nodes}
    contains_by_source: dict[str, list[str]] = {}
    methods_by_type: dict[str, list[str]] = {}

    for e in all_edges:
        rel = e.get('relation')

        if rel == 'contains':
            contains_by_source.setdefault(e.get('source'), []).append(e.get('target'))
        elif rel == 'method':
            methods_by_type.setdefault(e.get('source'), []).append(e.get('target'))
    pkg_callables: dict[str, dict[str, list[str]]] = {}
    pkg_types: dict[str, dict[str, list[str]]] = {}

    for pkg, results in pkg_results.items():
        callables = pkg_callables.setdefault(pkg, {})
        types = pkg_types.setdefault(pkg, {})

        for result in results:
            file_nid = next((n['id'] for n in result.get('nodes', []) if n.get('source_file') and n.get('label') == Path(str(n['source_file'])).name), None)

            if file_nid is None:
                continue

            for tgt in contains_by_source.get(file_nid, []):
                n = node_by_id.get(tgt)

                if n is None or not n.get('source_file'):
                    continue

                name = str(n.get('label', '')).strip('()')

                if not name or name.startswith('.'):
                    continue

                if n.get('_callable'):
                    callables.setdefault(name, []).append(tgt)

                if n.get('_callable_class'):
                    types.setdefault(name, []).append(tgt)
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in raw:
        prefix = rc['qualified_prefix']
        callee = rc['callee']
        caller = rc['caller_nid']
        candidates: list[str] = []

        if prefix in pkg_callables:
            candidates = pkg_callables[prefix].get(callee, [])
        else:
            pkg, _, type_name = prefix.rpartition('.')
            type_nids = pkg_types.get(pkg, {}).get(type_name, []) if pkg else []

            if len(type_nids) == 1:
                wanted = f'.{callee}'
                candidates = [m for m in methods_by_type.get(type_nids[0], []) if str(node_by_id.get(m, {}).get('label', '')).strip('()') == wanted]

        if len(candidates) != 1:
            continue

        tgt = candidates[0]

        if tgt == caller or (caller, tgt) in existing_pairs:
            continue

        existing_pairs.add((caller, tgt))
        all_edges.append({'source': caller, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolverustmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    raw = [rc for result in per_file for rc in result.get('raw_calls', []) if rc.get('rust_self_type') and rc.get('callee') and rc.get('caller_nid')]

    if not raw:
        return
    node_by_id: dict[str, dict] = {n.get('id'): n for n in all_nodes}
    nids_by_label: dict[str, list[str]] = {}
    nids_by_rust_impl_key: dict[str, list[str]] = {}

    for n in all_nodes:
        if str(n.get('source_file') or '').endswith('.rs'):
            nids_by_label.setdefault(n.get('label', ''), []).append(n.get('id'))
            impl_key = n.get('_rust_impl_key')

            if isinstance(impl_key, str) and impl_key:
                nids_by_rust_impl_key.setdefault(impl_key, []).append(n.get('id'))
    declared_type_count: dict[str, int] = {}
    generic_declared_type_count: dict[str, int] = {}
    contains_targets = {e.get('target') for e in all_edges if e.get('relation') == 'contains'}

    for label, nids in nids_by_label.items():
        declared_type_count[label] = sum((1 for nid in nids if nid in contains_targets))
        generic_declared_type_count[label] = sum((count for nid in nids if isinstance((count := node_by_id.get(nid, {}).get('_rust_declaration_count')), int) and (not isinstance(count, bool)) and (count > 0)))
    method_index: dict[tuple[str, str], set[str]] = {}

    for e in all_edges:
        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))
        tnode = node_by_id.get(tgt)

        if tnode is not None:
            name = str(tnode.get('label', '')).strip('()').lstrip('.')
            method_index.setdefault((src, name), set()).add(tgt)
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges if e.get('relation') == 'calls'}

    for rc in raw:
        caller = rc['caller_nid']
        callee = rc['callee']
        self_type = rc['rust_self_type']
        impl_key = rc.get('rust_self_impl_key')

        if isinstance(impl_key, str) and impl_key:
            if generic_declared_type_count.get(self_type, 0) != 1:
                continue

            owner_nids = nids_by_rust_impl_key.get(impl_key, [])
        else:
            if declared_type_count.get(self_type, 0) >= 2:
                continue

            owner_nids = nids_by_label.get(self_type, [])
        candidates: set[str] = set()

        for nid in owner_nids:
            candidates |= method_index.get((nid, callee), set())

        if len(candidates) != 1:
            continue

        tgt = next(iter(candidates))

        if tgt == caller or (caller, tgt) in existing_pairs:
            continue

        existing_pairs.add((caller, tgt))
        all_edges.append({'source': caller, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolveelixirimporttargets(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    node_by_id: dict[str, dict] = {}
    module_nids_by_bare_id: dict[str, list[str]] = {}

    for n in all_nodes:
        node_by_id[n['id']] = n

        if not n.get('_elixir_module'):
            continue

        label = str(n.get('label') or '')

        if not label:
            continue

        module_nids_by_bare_id.setdefault(make(label), []).append(n['id'])

    if not module_nids_by_bare_id:
        return
    node_ids = set(node_by_id)

    for e in all_edges:
        if e.get('relation') != 'imports' or e.get('context') != 'import' or (not str(e.get('source_file') or '').endswith(('.ex', '.exs'))):
            continue

        tgt = e.get('target')

        if tgt in node_ids:
            continue

        candidates = module_nids_by_bare_id.get(tgt, [])

        if len(candidates) != 1:
            continue

        target_nid = candidates[0]
        target_node = node_by_id.get(target_nid)

        if target_node is not None and str(target_node.get('source_file') or '') == str(e.get('source_file') or ''):
            continue

        e['target'] = target_nid


def resolveelixirquals(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    raw = [rc for result in per_file for rc in result.get('raw_calls', []) if rc.get('elixir_module') and rc.get('callee') and rc.get('caller_nid')]

    if not raw:
        return
    module_nids: dict[str, list[str]] = {}
    labels: dict[str, str] = {}

    for n in all_nodes:
        labels[n['id']] = str(n.get('label') or '')

        if n.get('_elixir_module') and n.get('source_file'):
            module_nids.setdefault(labels[n['id']], []).append(n['id'])
    defs: dict[tuple[str, str], str] = {}

    for e in all_edges:
        if e.get('relation') == 'method':
            name = labels.get(e.get('target', ''), '')

            if name.endswith('()'):
                defs.setdefault((e['source'], name[:-2]), e['target'])
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges if e.get('relation') == 'calls'}

    for rc in raw:
        candidates = module_nids.get(rc['elixir_module'], [])

        if len(candidates) != 1:
            continue

        caller = rc['caller_nid']
        tgt = defs.get((candidates[0], rc['callee']))

        if tgt is None or tgt == caller or (caller, tgt) in existing_pairs:
            continue

        existing_pairs.add((caller, tgt))
        all_edges.append({'source': caller, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})


def resolvektmembers(per_file: list[dict], all_nodes: list[dict], all_edges: list[dict]) -> None:
    raw = [rc for result in per_file for rc in result.get('raw_calls', []) if rc.get('lang') == 'kotlin' and rc.get('kotlin_object_receiver') and rc.get('callee') and rc.get('caller_nid')]

    if not raw:
        return
    node_by_id: dict[str, dict] = {n.get('id'): n for n in all_nodes}
    methods_by_type: dict[str, list[str]] = {}

    for e in all_edges:
        if e.get('relation') == 'method':
            methods_by_type.setdefault(e.get('source'), []).append(e.get('target'))
    types_by_name: dict[str, list[str]] = {}

    for n in all_nodes:
        if not n.get('_callable_class'):
            continue

        sf = str(n.get('source_file') or '')

        if not sf.endswith(('.kt', '.kts')):
            continue

        name = str(n.get('label', ''))

        if name:
            types_by_name.setdefault(name, []).append(n['id'])
    existing_pairs = {(e.get('source'), e.get('target')) for e in all_edges}

    for rc in raw:
        receiver = rc['kotlin_object_receiver']
        callee = rc['callee']
        caller = rc['caller_nid']
        type_nids = types_by_name.get(receiver, [])

        if len(type_nids) != 1:
            continue

        wanted = f'.{callee}'
        candidates = [m for m in methods_by_type.get(type_nids[0], []) if str(node_by_id.get(m, {}).get('label', '')).strip('()') == wanted]

        if len(candidates) != 1:
            continue

        tgt = candidates[0]

        if tgt == caller or (caller, tgt) in existing_pairs:
            continue

        existing_pairs.add((caller, tgt))
        all_edges.append({'source': caller, 'target': tgt, 'relation': 'calls', 'context': 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})
_KOTLIN_IMPORT_TARGET_RESOLVER = LanguageResolver('kotlin_import_targets', frozenset({'.kt', '.kts'}), resolvektimporttargets)
registry.register(LanguageResolver('terraform_modules', frozenset({'.tf'}), terraform.resolve))
registry.register(LanguageResolver('swift_member_calls', frozenset({'.swift'}), resolveswiftmembers))
registry.register(LanguageResolver('python_member_calls', frozenset({'.py'}), resolvepymembers))
registry.register(LanguageResolver('ruby_member_calls', frozenset({'.rb', '.rake'}), ruby.resolve))
registry.register(LanguageResolver('typescript_member_calls', frozenset({'.ts', '.tsx', '.mts', '.cts', '.js', '.jsx'}), resolvetsmembers))
registry.register(LanguageResolver('cpp_member_calls', frozenset({'.cpp', '.cc', '.cxx', '.hpp', '.cu', '.cuh', '.metal', '.h'}), resolvecppmembers))
registry.register(LanguageResolver('objc_member_calls', frozenset({'.m', '.mm', '.h'}), resolveobjcmembers))
registry.register(LanguageResolver('csharp_member_calls', frozenset({'.cs'}), resolvecsmembers))
registry.register(LanguageResolver('java_member_calls', frozenset({'.java'}), resolvejavamembers))
registry.register(LanguageResolver('rust_self_member_calls', frozenset({'.rs'}), resolverustmembers))
registry.register(LanguageResolver('r_sourced_calls', frozenset({'.r', '.R'}), r.resolve))
registry.register(LanguageResolver('solidity_type_references', frozenset({'.sol'}), solidity.resolve))
registry.register(LanguageResolver('erlang_remote_calls', frozenset({'.erl', '.hrl', '.escript'}), erlang.resolve))
registry.register(LanguageResolver('elixir_import_targets', frozenset({'.ex', '.exs'}), resolveelixirimporttargets))
registry.register(LanguageResolver('elixir_qualified_calls', frozenset({'.ex', '.exs'}), resolveelixirquals))
registry.register(LanguageResolver('kotlin_qualified_calls', frozenset({'.kt', '.kts'}), resolvektquals))
registry.register(LanguageResolver('kotlin_member_calls', frozenset({'.kt', '.kts'}), resolvektmembers))
registry.register(LanguageResolver('csharp_qualified_calls', frozenset({'.cs'}), resolvecsquals))
registry.register(LanguageResolver('csharp_interface_dispatch', frozenset({'.cs'}), csharp.dispatch))
registry.register(LanguageResolver('swift_protocol_dispatch', frozenset({'.swift'}), swift.dispatch))
registry.register(LanguageResolver('markdown_mentions', markdown.SUFFIXES, markdown.resolve))
_PROJECT_XML_MAX_BYTES = 2 * 1024 * 1024


def xmlsafe(src: bytes) -> bool:
    lowered = src.lower()

    return b'<!doctype' not in lowered and b'<!entity' not in lowered

    try:
        import xml.etree.ElementTree as etree
        src = path.read_bytes()
    except OSError as e:
        return {'nodes': [], 'edges': [], 'error': str(e)}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {'nodes': [], 'edges': [], 'error': 'package file too large'}

    if not xmlsafe(src):
        return {'nodes': [], 'edges': [], 'error': 'refusing XML with DOCTYPE/ENTITY declaration'}

    try:
        xml_root = etree.fromstring(src)
    except Exception as e:
        return {'nodes': [], 'edges': [], 'error': str(e)}

    str_path = str(path)
    stem = base.stem(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def addnode(nid: str, label: str) -> None:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({'id': nid, 'label': label, 'file_type': 'code', 'source_file': str_path, 'source_location': 'L1'})

    def addedge(src: str, tgt: str, relation: str, context: str | None=None) -> None:
        edge: dict[str, Any] = {'source': src, 'target': tgt, 'relation': relation, 'source_file': str_path, 'source_location': 'L1'}

        if context:
            edge['context'] = context
        edges.append(edge)
    file_nid = make(str(path))
    addnode(file_nid, path.name)
    name_elem = xml_root.find('.//Package/Name')
    pkg_name = name_elem.get('Value') if name_elem is not None else path.stem
    pkg_nid = make(stem, pkg_name)
    addnode(pkg_nid, pkg_name)
    addedge(file_nid, pkg_nid, 'contains')

    for item in xml_root.findall('.//RequiredPkgs/'):
        dep_elem = item.find('PackageName')

        if dep_elem is not None:
            dep_name = dep_elem.get('Value', '')

            if dep_name:
                dep_nid = make(dep_name)
                addnode(dep_nid, dep_name)
                addedge(pkg_nid, dep_nid, 'imports', context='import')

    for item in xml_root.findall('.//Files/'):
        unit_elem = item.find('UnitName')

        if unit_elem is not None:
            unit_name = unit_elem.get('Value', '')

            if unit_name:
                unit_nid = pascalunit(path, unit_name)
                addnode(unit_nid, unit_name)
                addedge(pkg_nid, unit_nid, 'contains')

    return {'nodes': nodes, 'edges': edges, 'input_tokens': 0, 'output_tokens': 0}


def checktsversion() -> None:
    try:
        from tree_sitter import LANGUAGE_VERSION
    except ImportError:
        raise ImportError("tree-sitter is not installed. Run: pip install 'tree-sitter>=0.23.0'")

    if LANGUAGE_VERSION < 14:
        import tree_sitter
        raise RuntimeError(f"tree-sitter {getattr(_ts, '__version__', 'unknown')} is too old. graphify requires tree-sitter >= 0.23.0 (Language API v2). Run: pip install --upgrade tree-sitter")


def extslnx(path: Path) -> dict:
    import xml.etree.ElementTree as etree

    try:
        src = path.read_bytes()
    except OSError:
        return {'nodes': [], 'edges': [], 'error': f'cannot read {path}'}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {'nodes': [], 'edges': [], 'error': 'project file too large'}

    if not xmlsafe(src):
        return {'nodes': [], 'edges': [], 'error': 'refusing XML with DOCTYPE/ENTITY declaration'}

    try:
        tree = etree.fromstring(src)
    except etree.ParseError as e:
        return {'nodes': [], 'edges': [], 'error': f'XML parse error: {e}'}

    file_nid = make(str(path))
    str_path = str(path)
    nodes: list[dict] = [{'id': file_nid, 'label': path.name, 'file_type': 'code', 'source_file': str_path, 'source_location': None}]
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_ids.add(file_nid)
    ns = ''

    if tree.tag.startswith('{'):
        ns = tree.tag.split('}')[0] + '}'

    def resolve(proj_path: str) -> str:
        proj_path = proj_path.replace('\\', '/')

        try:
            return str((path.parent / proj_path).resolve())
        except Exception:
            return proj_path

    project_nids: set[str] = set()

    for proj in tree.iter(f'{ns}Project'):
        proj_path = proj.get('Path')

        if not proj_path:
            continue

        abs_proj = resolve(proj_path)
        proj_nid = make(abs_proj)

        if proj_nid and proj_nid not in seen_ids:
            seen_ids.add(proj_nid)
            label = Path(proj_path).stem
            nodes.append({'id': proj_nid, 'label': label, 'file_type': 'code', 'source_file': abs_proj, 'source_location': None})
            edges.append({'source': file_nid, 'target': proj_nid, 'relation': 'contains', 'source_file': str_path})

        if proj_nid:
            project_nids.add(proj_nid)

    for proj in tree.iter(f'{ns}Project'):
        proj_path = proj.get('Path')

        if not proj_path:
            continue

        from_nid = make(resolve(proj_path))

        for dep in proj.iter(f'{ns}BuildDependency'):
            dep_path = dep.get('Project')

            if not dep_path:
                continue

            to_nid = make(resolve(dep_path))

            if from_nid and to_nid and (from_nid != to_nid) and (to_nid in project_nids):
                edges.append({'source': from_nid, 'target': to_nid, 'relation': 'imports', 'source_file': str_path})

    return {'nodes': nodes, 'edges': edges}


def extcsproj(path: Path) -> dict:
    import xml.etree.ElementTree as etree

    try:
        src = path.read_bytes()
    except OSError:
        return {'nodes': [], 'edges': [], 'error': f'cannot read {path}'}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {'nodes': [], 'edges': [], 'error': 'project file too large'}

    if not xmlsafe(src):
        return {'nodes': [], 'edges': [], 'error': 'refusing XML with DOCTYPE/ENTITY declaration'}

    try:
        tree = etree.fromstring(src)
    except etree.ParseError as e:
        return {'nodes': [], 'edges': [], 'error': f'XML parse error: {e}'}

    file_nid = make(str(path))
    str_path = str(path)
    nodes: list[dict] = [{'id': file_nid, 'label': path.name, 'file_type': 'code', 'source_file': str_path, 'source_location': None}]
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_ids.add(file_nid)
    ns = ''
    root_tag = tree.tag

    if root_tag.startswith('{'):
        ns = root_tag.split('}')[0] + '}'

    def findall(tag: str):
        return tree.iter(f'{ns}{tag}')

    for tf in findall('TargetFramework'):
        if tf.text:
            fw_nid = make('framework', tf.text.strip())

            if fw_nid and fw_nid not in seen_ids:
                seen_ids.add(fw_nid)
                nodes.append({'id': fw_nid, 'label': tf.text.strip(), 'file_type': 'concept', 'source_file': str_path, 'source_location': None})
                edges.append({'source': file_nid, 'target': fw_nid, 'relation': 'references', 'source_file': str_path})

    for tf in findall('TargetFrameworks'):
        if tf.text:
            for fw in tf.text.strip().split(';'):
                fw = fw.strip()

                if fw:
                    fw_nid = make('framework', fw)

                    if fw_nid and fw_nid not in seen_ids:
                        seen_ids.add(fw_nid)
                        nodes.append({'id': fw_nid, 'label': fw, 'file_type': 'concept', 'source_file': str_path, 'source_location': None})
                        edges.append({'source': file_nid, 'target': fw_nid, 'relation': 'references', 'source_file': str_path})

    for pkg in findall('PackageReference'):
        name = pkg.get('Include') or pkg.get('include') or ''
        version = pkg.get('Version') or pkg.get('version') or ''

        if not name:
            continue

        pkg_nid = make('nuget', name)
        label = f'{name} ({version})' if version else name

        if pkg_nid and pkg_nid not in seen_ids:
            seen_ids.add(pkg_nid)
            nodes.append({'id': pkg_nid, 'label': label, 'file_type': 'code', 'source_file': str_path, 'source_location': None})
        edges.append({'source': file_nid, 'target': pkg_nid, 'relation': 'imports', 'source_file': str_path})

    for proj in findall('ProjectReference'):
        ref_path = proj.get('Include') or proj.get('include') or ''

        if not ref_path:
            continue

        ref_path_norm = ref_path.replace('\\', '/')

        try:
            abs_ref = str((path.parent / ref_path_norm).resolve())
        except Exception:
            abs_ref = ref_path_norm
        proj_nid = make(abs_ref)

        if proj_nid and proj_nid not in seen_ids:
            seen_ids.add(proj_nid)
            proj_label = Path(ref_path_norm).name
            nodes.append({'id': proj_nid, 'label': proj_label, 'file_type': 'code', 'source_file': abs_ref, 'source_location': None})
        edges.append({'source': file_nid, 'target': proj_nid, 'relation': 'imports', 'source_file': str_path})
    sdk = tree.get('Sdk') or ''

    if sdk:
        sdk_nid = make('sdk', sdk)

        if sdk_nid and sdk_nid not in seen_ids:
            seen_ids.add(sdk_nid)
            nodes.append({'id': sdk_nid, 'label': sdk, 'file_type': 'concept', 'source_file': str_path, 'source_location': None})
            edges.append({'source': file_nid, 'target': sdk_nid, 'relation': 'references', 'source_file': str_path})

    return {'nodes': nodes, 'edges': edges}


def xmllocalname(name: str) -> str:
    return name.rsplit('}', 1)[-1] if name.startswith('{') else name

_EVENT_HANDLER_SIGNATURE_RE = re.compile('\\(\\s*object\\??\\s+\\w+\\s*,\\s*[\\w.]*EventArgs(?:<[^>]*>)?\\s+\\w+\\s*\\)')
_XAML_NON_EVENT_ATTRS = frozenset({'Name', 'Content', 'Text', 'Title', 'Tag', 'ToolTip', 'Header', 'Class', 'Key', 'Uid', 'DataContext', 'Style', 'Source'})
_XAML_IDENT_RE = re.compile('[A-Za-z_]\\w*')
_XAML_DESIGN_INSTANCE_TYPE_RE = re.compile('\\bType\\s*=\\s*(?:\\{x:Type\\s+)?(?P<type>[\\w.:+]+)')


def xamlmarkupext(value: str) -> tuple[str, str] | None:
    value = value.strip()

    if not (value.startswith('{') and value.endswith('}')):
        return None

    inner = value[1:-1].strip()

    if not inner or inner.startswith('}'):
        return None

    name, _, args = inner.partition(' ')

    return (name, args.strip())


def xamlsplitargs(args: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0

    for idx, ch in enumerate(args):
        if ch == '{':
            depth += 1
        elif ch == '}' and depth:
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(args[start:idx].strip())
            start = idx + 1
    tail = args[start:].strip()

    if tail:
        parts.append(tail)

    return parts


def xamlresourcekey(value: str) -> str | None:
    markup = xamlmarkupext(value)

    if not markup:
        return None

    name, args = markup

    if name != 'StaticResource':
        return None

    for part in xamlsplitargs(args):
        if '=' not in part:
            return part.strip() or None

        key, resource = part.split('=', 1)

        if key.strip() == 'ResourceKey':
            return resource.strip() or None

    return None


def xamlbindingrefs(value: str) -> tuple[str | None, str | None]:
    markup = xamlmarkupext(value)

    if not markup:
        return (None, None)

    name, args = markup

    if name != 'Binding':
        return (None, None)

    path_ref = None
    converter_ref = None

    for part in xamlsplitargs(args):
        if not part:
            continue

        if '=' not in part:
            if path_ref is None:
                path_ref = part.strip()
            continue

        key, raw_value = part.split('=', 1)
        key = key.strip()
        raw_value = raw_value.strip()

        if key == 'Path':
            path_ref = raw_value
        elif key == 'Converter':
            converter_ref = xamlresourcekey(raw_value)

    if path_ref and ('{' in path_ref or '}' in path_ref):
        path_ref = None

    return (path_ref or None, converter_ref or None)


def xamlcodebehindpath(path: Path) -> Path | None:
    expected = path.with_suffix(path.suffix + '.cs')

    if expected.exists():
        return expected

    try:
        for sibling in path.parent.iterdir():
            if sibling.name.casefold() == expected.name.casefold():
                return sibling
    except OSError:
        return None

    return None


def xamlcodebehindsymbols(path: Path, class_name: str | None) -> tuple[dict | None, dict[str, dict], list[dict]]:
    codebehind = xamlcodebehindpath(path)

    if not codebehind:
        return (None, {}, [])

    result = csharp(codebehind)

    if result.get('error'):
        return (None, {}, [])

    class_simple = class_name.rsplit('.', 1)[-1] if class_name else None
    class_node = None

    if class_simple:
        for node in result.get('nodes', []):
            if node.get('label') == class_simple:
                class_node = node
                break

    class_method_edges: list[dict] = []

    if class_node:
        class_id = class_node.get('id')

        for edge in result.get('edges', []):
            if edge.get('source') == class_id and edge.get('relation') == 'method':
                class_method_edges.append(edge)
    method_ids = {edge.get('target') for edge in class_method_edges} if class_node else None

    try:
        cb_lines = text(codebehind, warn=False).splitlines()
    except OSError:
        cb_lines = []

    def haseventhandler(node: dict) -> bool:
        loc = str(node.get('source_location') or '')
        m = re.match('L(\\d+)', loc)

        if not m or not cb_lines:
            return False

        start = int(m.group(1)) - 1
        snippet = ' '.join(cb_lines[start:start + 3])

        return _EVENT_HANDLER_SIGNATURE_RE.search(snippet) is not None

    methods: dict[str, dict] = {}

    for node in result.get('nodes', []):
        if method_ids is not None and node.get('id') not in method_ids:
            continue

        label = str(node.get('label', ''))

        if label.startswith('.') and label.endswith('()') and haseventhandler(node):
            methods[label.strip('()').lstrip('.')] = node

    return (class_node, methods, class_method_edges)


def xamltypesimplename(type_ref: str) -> str | None:
    type_ref = type_ref.strip().strip('{}')

    if not type_ref:
        return None

    type_ref = type_ref.split(',', 1)[0].strip()

    if type_ref.startswith('x:Type '):
        type_ref = type_ref[len('x:Type '):].strip()

    if ':' in type_ref:
        type_ref = type_ref.rsplit(':', 1)[-1]

    if '.' in type_ref:
        type_ref = type_ref.rsplit('.', 1)[-1]

    if '+' in type_ref:
        type_ref = type_ref.rsplit('+', 1)[-1]

    return type_ref if _XAML_IDENT_RE.fullmatch(type_ref) else None


def xamlexplicitviewmodels(tree) -> tuple[bool, list[str]]:
    has_data_context = False
    names: list[str] = []

    for elem in tree.iter():
        elem_type = xmllocalname(elem.tag)

        if elem_type.endswith('.DataContext') or elem_type == 'DataContext':
            has_data_context = True

            for child in list(elem):
                vm_name = xamltypesimplename(xmllocalname(child.tag))

                if vm_name and vm_name not in names:
                    names.append(vm_name)

        for key, value in elem.attrib.items():
            if xmllocalname(key) != 'DataContext' or not value:
                continue

            has_data_context = True
            match = _XAML_DESIGN_INSTANCE_TYPE_RE.search(value)

            if match:
                vm_name = xamltypesimplename(match.group('type'))

                if vm_name and vm_name not in names:
                    names.append(vm_name)

    return (has_data_context, names)


def xamlprismautowire(tree) -> bool:
    for elem in tree.iter():
        for key, value in elem.attrib.items():
            if xmllocalname(key).endswith('ViewModelLocator.AutoWireViewModel') and value.strip().lower() == 'true':
                return True

    return False


def xamlviewmodels(view_name: str | None) -> list[str]:
    if not view_name:
        return []

    names: list[str] = []

    def add(name: str) -> None:
        if name.endswith('ViewModel') and name not in names:
            names.append(name)

    if view_name == 'MainWindow':
        add('MainWindowViewModel')
        add('MainViewModel')

    for suffix in ('UserControl', 'View', 'Page', 'Control'):
        if view_name.endswith(suffix) and len(view_name) > len(suffix):
            add(view_name[:-len(suffix)] + 'ViewModel')
            break

    return names


def xamlprojectroot(path: Path) -> Path:
    project_markers = ('.csproj', '.fsproj', '.vbproj', '.sln', '.slnx')
    root = path.parent

    for directory in (path.parent, *path.parent.parents):
        try:
            if any((child.suffix in project_markers for child in directory.iterdir())):
                root = directory
                break
        except OSError:
            continue

    if _XAML_ACTIVE_EXTRACT_ROOT is None:
        return root

    boundary = _XAML_ACTIVE_EXTRACT_ROOT.resolve()

    try:
        root.resolve().relative_to(boundary)

        return root
    except ValueError:
        return boundary


def xamlcsclassnodes(path: Path) -> dict[str, list[dict]]:
    from graphify.detect import ignored, noise, ignore
    root = xamlprojectroot(path)
    cache_key = str(root.resolve()) if _XAML_ACTIVE_EXTRACT_ROOT is not None else None

    if cache_key and cache_key in _XAML_CSHARP_CLASS_CACHE:
        return _XAML_CSHARP_CLASS_CACHE[cache_key]

    classes: dict[str, list[dict]] = {}
    patterns = ignore(root)
    ignore_cache: dict[Path, bool] = {}
    import os
    _DIR_CAP = 20000
    cs_files: list[Path] = []
    visited = 0

    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith('.') and (not noise(d))]

            for fn in filenames:
                if fn.endswith('.cs'):
                    cs_files.append(Path(dirpath) / fn)
            visited += 1

            if visited >= _DIR_CAP:
                break
    except OSError:
        return classes

    cs_files.sort()

    for cs_path in cs_files:
        if patterns and ignored(cs_path, root, patterns, _cache=ignore_cache):
            continue

        result = csharp(cs_path)

        if result.get('error'):
            continue

        for node in result.get('nodes', []):
            label = str(node.get('label', ''))

            if not label.endswith('ViewModel') or not _XAML_IDENT_RE.fullmatch(label):
                continue

            if node.get('source_file'):
                classes.setdefault(label, []).append(node)

    if cache_key:
        _XAML_CSHARP_CLASS_CACHE[cache_key] = classes

    return classes


def xamlpascalname(name: str) -> str | None:
    name = name.strip().lstrip('_')

    if name.startswith('m_'):
        name = name[2:]

    return name[:1].upper() + name[1:] if _XAML_IDENT_RE.fullmatch(name) else None

_XAML_TOOLKIT_FIELD_RE = re.compile('\\b(?P<name>_?m?_?[A-Za-z_]\\w*)\\s*(?:=.*)?;')
_XAML_TOOLKIT_METHOD_RE = re.compile('\\b(?P<name>[A-Za-z_]\\w*)\\s*\\(')
_XAML_ACTIVE_EXTRACT_ROOT: Path | None = None
_XAML_CSHARP_CLASS_CACHE: dict[str, dict[str, list[dict]]] = {}


def xamltoolkitmembers(vm_node: dict) -> tuple[dict[str, dict], list[dict]]:
    source_file = vm_node.get('source_file')
    vm_id = vm_node.get('id')

    if not source_file or not vm_id:
        return ({}, [])

    try:
        lines = text(Path(source_file), warn=False).splitlines()
    except OSError:
        return ({}, [])

    members: dict[str, dict] = {}
    edges: list[dict] = []

    def addmember(label: str, line_no: int, context: str) -> None:
        nid = make(vm_id, label)
        members[label] = {'id': nid, 'label': label, 'file_type': 'code', 'source_file': source_file, 'source_location': f'L{line_no}'}
        edges.append({'source': vm_id, 'target': nid, 'relation': 'defines', 'source_file': source_file, 'source_location': f'L{line_no}', 'context': context})
    pending: tuple[str, int] | None = None

    for line_no, line in enumerate(lines, 1):
        remainder = line.split(']', 1)[1].strip() if ']' in line else ''

        if '[' in line and 'ObservableProperty' in line:
            pending = ('property', line_no)

            if not remainder:
                continue

            line = remainder

        if '[' in line and 'RelayCommand' in line:
            pending = ('command', line_no)

            if not remainder:
                continue

            line = remainder

        if not pending or not line.strip() or line.lstrip().startswith('['):
            continue

        kind, attr_line = pending
        pending = None

        if kind == 'property':
            match = _XAML_TOOLKIT_FIELD_RE.search(line)
            label = xamlpascalname(match.group('name')) if match else None

            if label:
                addmember(label, attr_line, 'communitytoolkit_observable_property')
        else:
            match = _XAML_TOOLKIT_METHOD_RE.search(line)

            if match:
                method = match.group('name').removesuffix('Async')
                addmember(f'{method}Command', attr_line, 'communitytoolkit_relay_command')

    return (members, edges)


def extxaml(path: Path) -> dict:
    import xml.etree.ElementTree as etree

    try:
        src = path.read_bytes()
    except OSError:
        return {'nodes': [], 'edges': [], 'error': f'cannot read {path}'}

    if len(src) > _PROJECT_XML_MAX_BYTES:
        return {'nodes': [], 'edges': [], 'error': 'xaml file too large'}

    if not xmlsafe(src):
        return {'nodes': [], 'edges': [], 'error': 'refusing XML with DOCTYPE/ENTITY declaration'}

    try:
        tree = etree.fromstring(src)
    except etree.ParseError as e:
        return {'nodes': [], 'edges': [], 'error': f'XML parse error: {e}'}

    text = src.decode('utf-8', errors='replace')
    lines = text.splitlines()
    str_path = str(path)
    stem = base.stem(path)
    file_nid = make(str(path))
    root_type = xmllocalname(tree.tag)
    root_nid = make(stem, root_type)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str, str | None]] = set()

    def linefor(value: str | None) -> int:
        if value:
            for idx, line in enumerate(lines, 1):
                if value in line:
                    return idx

        return 1

    def addnode(nid: str, label: str, line: int | None, *, file_type: str='code', source_file: str=str_path) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append({'id': nid, 'label': label, 'file_type': file_type, 'source_file': source_file, 'source_location': f'L{line}' if line else None})

    def addexistnode(node: dict | None) -> None:
        if not node:
            return
        nid = node.get('id')

        if not nid or nid in seen_ids:
            return
        seen_ids.add(nid)
        nodes.append(dict(node))

    def addedge(src_nid: str, tgt_nid: str, relation: str, line: int, *, context: str | None=None, source_file: str=str_path) -> None:
        key = (src_nid, tgt_nid, relation, context)

        if key in seen_edges:
            return
        seen_edges.add(key)
        edge = {'source': src_nid, 'target': tgt_nid, 'relation': relation, 'source_file': source_file, 'source_location': f'L{line}'}

        if context:
            edge['context'] = context
        edges.append(edge)

    def addexistedge(edge: dict) -> None:
        key = (edge.get('source'), edge.get('target'), edge.get('relation'), edge.get('context'))

        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(dict(edge))
    addnode(file_nid, path.name, 1)
    addnode(root_nid, root_type, 1)
    addedge(file_nid, root_nid, 'contains', 1)
    class_name = None

    for key, value in tree.attrib.items():
        if xmllocalname(key) == 'Class' and value:
            class_name = value.strip()
            break

    class_node, codebehind_methods, class_method_edges = xamlcodebehindsymbols(path, class_name)

    if class_name:
        if class_node:
            class_nid = class_node['id']
            addexistnode(class_node)
        else:
            class_label = class_name.rsplit('.', 1)[-1]
            class_nid = make(stem, class_label)
            addnode(class_nid, class_label, linefor(class_name))
        addedge(root_nid, class_nid, 'references', linefor(class_name), context='x_class')
    has_data_context, vm_names = xamlexplicitviewmodels(tree)
    prism_autowire = xamlprismautowire(tree)

    if not has_data_context:
        view_name = class_name.rsplit('.', 1)[-1] if class_name else None
        view_name = view_name or (path.stem if prism_autowire else None)
        vm_names = xamlviewmodels(view_name)
        generated_members: dict[str, dict] = {}
    generated_member_edges: list[dict] = []

    if vm_names:
        csharp_classes = xamlcsclassnodes(path)
        vm_candidates = []

        for vm_name in vm_names:
            vm_candidates.extend(csharp_classes.get(vm_name, []))
        by_id = {node.get('id'): node for node in vm_candidates if node.get('id')}

        if len(by_id) == 1:
            vm_node = next(iter(by_id.values()))
            addexistnode(vm_node)
            addedge(root_nid, vm_node['id'], 'references', linefor(vm_node['label']), context='view_model')
            generated_members, generated_member_edges = xamltoolkitmembers(vm_node)

            for member in generated_members.values():
                addexistnode(member)

            for member_edge in generated_member_edges:
                addexistedge(member_edge)

    for elem in tree.iter():
        elem_type = xmllocalname(elem.tag)
        elem_name = None

        for key, value in elem.attrib.items():
            if xmllocalname(key) == 'Name' and value:
                elem_name = value.strip()
                break

        owner_nid = root_nid

        if elem_name:
            owner_nid = make(stem, elem_name)
            addnode(owner_nid, elem_name, linefor(elem_name))
            addedge(root_nid, owner_nid, 'contains', linefor(elem_name))
            type_nid = make('xaml', elem_type)
            addnode(type_nid, elem_type, linefor(elem_name), file_type='concept')
            addedge(owner_nid, type_nid, 'references', linefor(elem_name), context='type')

        for key, value in elem.attrib.items():
            value = value or ''
            attr_local = xmllocalname(key)

            if attr_local not in _XAML_NON_EVENT_ATTRS and _XAML_IDENT_RE.fullmatch(value):
                method = codebehind_methods.get(value)

                if method:
                    addexistnode(method)
                    addedge(owner_nid, method['id'], 'references', linefor(value), context='event')

                    for method_edge in class_method_edges:
                        if method_edge.get('target') == method['id']:
                            addexistnode(class_node)
                            addexistedge(method_edge)
                            break

            binding_path, binding_converter = xamlbindingrefs(value)

            if binding_path:
                bind_nid = make('binding', binding_path)
                addnode(bind_nid, binding_path, linefor(value), file_type='concept')
                binding_context = 'binding_command' if attr_local == 'Command' or attr_local.endswith('.Command') else 'binding_path'
                addedge(owner_nid, bind_nid, 'references', linefor(value), context=binding_context)
                generated_member = generated_members.get(binding_path)

                if generated_member:
                    addexistnode(generated_member)
                    addedge(owner_nid, generated_member['id'], 'references', linefor(value), context=binding_context)

            if binding_converter:
                converter_nid = make('binding_converter', binding_converter)
                addnode(converter_nid, binding_converter, linefor(value), file_type='concept')
                addedge(owner_nid, converter_nid, 'references', linefor(value), context='binding_converter')

            if elem_type == 'Binding' and attr_local == 'Path':
                direct_path = value.strip()

                if direct_path and '{' not in direct_path and ('}' not in direct_path):
                    bind_nid = make('binding', direct_path)
                    addnode(bind_nid, direct_path, linefor(value), file_type='concept')
                    addedge(owner_nid, bind_nid, 'references', linefor(value), context='binding_path')

            if elem_type == 'Binding' and attr_local == 'Converter':
                direct_converter = xamlresourcekey(value)

                if direct_converter:
                    converter_nid = make('binding_converter', direct_converter)
                    addnode(converter_nid, direct_converter, linefor(value), file_type='concept')
                    addedge(owner_nid, converter_nid, 'references', linefor(value), context='binding_converter')

    return {'nodes': nodes, 'edges': edges}

_DISPATCH: dict[str, Any] = {
    '.py': extpython,
    '.js': extjs,
    '.jsx': extjs,
    '.mjs': extjs,
    '.cjs': extjs,
    '.ts': extjs,
    '.tsx': extjs,
    '.mts': extjs,
    '.cts': extjs,
    '.go': go.extract,
    '.rs': rust.extract,
    '.r': r.extract,
    '.sol': solidity.extract,
    '.java': extjava,
    '.groovy': extgroovy,
    '.gradle': extgroovy,
    '.c': c.extract,
    '.h': c.extract,
    '.cpp': cpp.extract,
    '.cc': cpp.extract,
    '.cxx': cpp.extract,
    '.hpp': cpp.extract,
    '.cu': cpp.extract,
    '.cuh': cpp.extract,
    '.metal': cpp.extract,
    '.rb': ruby.extract,
    '.rake': ruby.extract,
    '.cs': csharp.extract,
    '.kt': extkotlin,
    '.kts': extkotlin,
    '.scala': extscala,
    '.php': extphp,
    '.swift': swift.extract,
    '.lua': extlua,
    '.luau': extlua,
    '.toc': extlua,
    '.zig': zig.extract,
    '.ps1': powershell.extract,
    '.psm1': powershell.extract,
    '.psd1': powershell.manifest,
    '.ex': elixir.extract,
    '.exs': elixir.extract,
    '.erl': erlang.extract,
    '.hrl': erlang.extract,
    '.escript': erlang.extract,
    '.m': objc.extract,
    '.mm': objc.extract,
    '.jl': julia.extract,
    '.vue': extvue,
    '.svelte': extsvelte,
    '.astro': extastro,
    '.dart': dart.extract,
    '.ml': ocaml.extract,
    '.mli': ocaml.extract,
    '.sql': sql.extract,
    '.md': markdown.extract,
    '.mdx': markdown.extract,
    '.qmd': markdown.extract,
    '.skill': markdown.extract,
    '.sh': bash.extract,
    '.bash': bash.extract,
    '.json': jsonlang.extract,
    '.tf': terraform.extract,
    '.tfvars': terraform.extract,
    '.hcl': terraform.extract,
    '.sln': sln.extract,
    '.slnx': extslnx,
    '.csproj': extcsproj,
    '.fsproj': extcsproj,
    '.vbproj': extcsproj,
    '.xaml': extxaml,
    '.razor': razor.extract,
    '.cshtml': razor.extract,
    '.cls': apex.extract,
    '.trigger': apex.extract,
}
_EXTRA_FOR_EXTENSION = {'.vb': 'vbnet', '.r': 'r', '.sol': 'solidity', '.erl': 'erlang', '.hrl': 'erlang', '.escript': 'erlang', '.sql': 'sql', '.tf': 'terraform', '.tfvars': 'terraform', '.hcl': 'terraform', '.dm': 'dm', '.dme': 'dm', '.ml': 'ocaml', '.mli': 'ocaml', '.lisp': 'commonlisp', '.cl': 'commonlisp', '.lsp': 'commonlisp', '.asd': 'commonlisp', '.robot': 'robot', '.resource': 'robot'}
_DEP_MISSING_MARKER = 'not installed'
_DEP_LOAD_FAILED_MARKER = 'failed to load'
_SHEBANG_DISPATCH: dict[str, Any] = {
    'python': extpython,
    'python2': extpython,
    'python3': extpython,
    'bash': bash.extract,
    'sh': bash.extract,
    'dash': bash.extract,
    'zsh': bash.extract,
    'ksh': bash.extract,
    'node': extjs,
    'nodejs': extjs,
    'ruby': ruby.extract,
    'lua': extlua,
    'php': extphp,
    'julia': julia.extract,
    'Rscript': r.extract,
}
_OBJC_HEADER_MARKERS = (b'@interface', b'@protocol', b'@implementation', b'@import', b'#import')


def isobjcheader(path: Path) -> bool:
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False

    return any((marker in head for marker in _OBJC_HEADER_MARKERS))

_CPP_HEADER_MARKERS = (b'class ', b'namespace ', b'template', b'::', b'public:', b'private:', b'protected:')


def isobjcsource(path: Path) -> bool:
    return isobjcheader(path)


def iscppheader(path: Path) -> bool:
    try:
        head = path.read_bytes()[:256 * 1024]
    except OSError:
        return False

    return any((marker in head for marker in _CPP_HEADER_MARKERS))


def extractor(path: Path) -> Any | None:
    if path.name.lower().endswith('.blade.php'):
        return extract_blade

    if mcp.match(path):
        return mcp.extract

    if manifest.match(path):
        return manifest.extract

    suffix = path.suffix

    if suffix not in _DISPATCH and suffix.lower() in _DISPATCH:
        suffix = suffix.lower()

    if suffix == '.h':
        if isobjcheader(path):
            return extract_objc

        if iscppheader(path):
            return cpp

    if suffix == '.m' and (not isobjcsource(path)):
        return None

    if not suffix:
        from graphify.detect import shebang
        interp = shebang(path)

        if interp is not None:
            return _SHEBANG_DISPATCH.get(interp)

    return _DISPATCH.get(suffix)


def extractwithxaml(extractor, path: Path, root: Path) -> dict:
    global _XAML_ACTIVE_EXTRACT_ROOT
    previous_root = _XAML_ACTIVE_EXTRACT_ROOT
    _XAML_ACTIVE_EXTRACT_ROOT = root.resolve()

    try:
        return safeextract(extractor, path, scan_root=root)
    finally:
        _XAML_ACTIVE_EXTRACT_ROOT = previous_root


def extractsingle(args: tuple) -> tuple[int, dict]:
    if len(args) == 4:
        idx, path_str, root_str, cache_location_str = args
    else:
        idx, path_str, root_str = args
        cache_location_str = root_str
    path = Path(path_str)
    root = Path(root_str)
    cache_location = Path(cache_location_str)
    raiseoverflow()
    bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES

    if not bypass_cache:
        cached = load(path, root, cache=cache_location)

        if cached is not None:
            return (idx, cached)

    handler = extractor(path)

    if handler is None:
        return (idx, {'nodes': [], 'edges': []})

    result = extractwithxaml(handler, path, root)

    if not bypass_cache and 'error' not in result and (result.get('nodes') or result.get('skipped')):
        save(path, result, root, cache=cache_location)

    return (idx, result)


def spawnnoreimport() -> bool:
    import multiprocessing

    if multiprocessing.get_start_method(allow_none=True) != 'spawn' and sys.platform != 'win32':
        return False

    import __main__

    if getattr(getattr(__main__, '__spec__', None), 'name', None) is not None:
        return False

    main_file = getattr(__main__, '__file__', None)

    return main_file is None or not os.path.isfile(main_file)


def extractparallel(uncached_work: list[tuple[int, Path]], per_file: list[dict | None], root: Path, max_workers: int | None, total_files: int, cache_location: Path | None=None) -> bool:
    import concurrent.futures

    if max_workers is None:
        env_raw = os.environ.get('GRAPHIFY_MAX_WORKERS', '').strip()
        env_cap = None

        if env_raw:
            try:
                v = int(env_raw)

                if v > 0:
                    env_cap = v
            except ValueError:
                pass
        cpu_cap = env_cap if env_cap is not None else os.cpu_count() or 4
        max_workers = min(cpu_cap, len(uncached_work))

    if sys.platform == 'win32':
        max_workers = min(max_workers, 61)
    max_workers = max(max_workers, 1)

    if max_workers == 1:
        return False

    root_str = str(root)
    cache_loc_str = str(cache_location if cache_location is not None else root)
    work_items = [(idx, str(path), root_str, cache_loc_str) for idx, path in uncached_work]
    done_count = 0
    failed: list[int] = []
    _PROGRESS_INTERVAL = 100

    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(extractsingle, item): pos for pos, item in enumerate(work_items)}

            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result = future.result()
                    per_file[idx] = result
                except concurrent.futures.process.BrokenProcessPool:
                    raise
                except Exception as exc:
                    pos = futures[future]
                    print(f'  warning: worker failed for {work_items[pos][1]}: {exc}', file=sys.stderr, flush=True)
                    failed.append(pos)
                done_count += 1

                if total_files >= _PROGRESS_INTERVAL and done_count % _PROGRESS_INTERVAL == 0:
                    print(f'  AST extraction: {done_count}/{len(uncached_work)} uncached files ({done_count * 100 // len(uncached_work)}%) [{max_workers} workers]', flush=True)
    except concurrent.futures.process.BrokenProcessPool:
        print('  warning: parallel extraction failed (BrokenProcessPool); falling back to sequential. On Windows this usually means the caller is missing an `if __name__ == "__main__":` guard. Pass parallel=False to extract() to skip the pool entirely.', flush=True)

        return False
    except OSError as exc:
        print(f'  warning: parallel extraction unavailable ({exc}); falling back to sequential.', flush=True)

        return False

    if failed:
        extractsequential([uncached_work[pos] for pos in failed], per_file, root, total_files, cache_location)

    if total_files >= _PROGRESS_INTERVAL:
        _done = len(uncached_work)
        print(f'  AST extraction: {_done}/{_done} uncached files (100%) [{max_workers} workers]', flush=True)

    return True


def extractsequential(uncached_work: list[tuple[int, Path]], per_file: list[dict | None], root: Path, total_files: int, cache_location: Path | None=None) -> None:
    _PROGRESS_INTERVAL = 100

    for work_idx, (idx, path) in enumerate(uncached_work):
        if total_files >= _PROGRESS_INTERVAL and work_idx % _PROGRESS_INTERVAL == 0 and (work_idx > 0):
            print(f'  AST extraction: {work_idx}/{len(uncached_work)} uncached files ({work_idx * 100 // len(uncached_work)}%)', flush=True)
        handler = extractor(path)

        if handler is None:
            per_file[idx] = {'nodes': [], 'edges': []}
            continue

        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES
        result = extractwithxaml(handler, path, root)

        if not bypass_cache and 'error' not in result and (result.get('nodes') or result.get('skipped')):
            save(path, result, root, cache=cache_location)
        per_file[idx] = result

    if total_files >= _PROGRESS_INTERVAL:
        _done = len(uncached_work)
        print(f'  AST extraction: {_done}/{_done} uncached files (100%)', flush=True)
_PARALLEL_THRESHOLD = 20


def extract(paths: list[Path], cache_root: Path | None=None, *, root: Path | None=None, parallel: bool=True, max_workers: int | None=None, resolution_context_nodes: list[dict] | None=None, resolution_context_edges: list[dict] | None=None) -> dict:
    paths = [Path(p) for p in paths]
    anchor_root = Path(root) if root is not None else None
    checktsversion()
    raiseoverflow()
    _WORKSPACE_PACKAGE_CACHE.clear()
    TSALIASCACHE.clear()
    TSBASEURLCACHE.clear()
    PACKAGEIMPORTSCACHE.clear()
    _XAML_CSHARP_CLASS_CACHE.clear()
    markdown.CACHE.clear()
    ROOTNAMESPACECACHE.clear()
    cachedrealpath.cache_clear()
    cachedsourcekey.cache_clear()

    try:
        if not paths:
            root = Path('.')
        elif len(paths) == 1:
            root = paths[0].parent
        else:
            min_parts = min((len(p.parts) for p in paths))
            common_len = 0

            for i in range(min_parts):
                if len({p.parts[i] for p in paths}) == 1:
                    common_len += 1
                else:
                    break

            root = Path(*paths[0].parts[:common_len]) if common_len else Path('.')
    except Exception:
        root = Path('.')

    if anchor_root is not None:
        root = anchor_root
    elif cache_root is not None:
        root = cache_root
    root = root.resolve()
    python_scan_paths = list(paths)

    for context_node in resolution_context_nodes or []:
        source_file = context_node.get('source_file')

        if not source_file or Path(source_file).suffix.lower() != '.py':
            continue

        context_path = Path(source_file)

        if not context_path.is_absolute():
            context_path = root / context_path

        if context_path.is_file():
            python_scan_paths.append(context_path)
    python_module_alias_files, scan_root_aliases = pyaliasfiles(python_scan_paths, root)
    ambiguous_python_modules = {module_id for module_id, module_paths in python_module_alias_files.items() if len(module_paths) > 1 and module_id not in scan_root_aliases}
    cache_location = (cache_root if cache_root is not None else Path('.')).resolve()
    total = len(paths)
    per_file: list[dict | None] = [None] * total
    uncached_work: list[tuple[int, Path]] = []

    for i, path in enumerate(paths):
        if extractor(path) is None:
            per_file[i] = {'nodes': [], 'edges': []}
            continue

        bypass_cache = path.suffix in _JS_CACHE_BYPASS_SUFFIXES

        if not bypass_cache:
            cached = load(path, root, cache=cache_location)

            if cached is not None:
                per_file[i] = cached
                continue

        uncached_work.append((i, path))

    if uncached_work:
        if parallel and len(uncached_work) >= _PARALLEL_THRESHOLD and spawnnoreimport():
            print('  note: running AST extraction sequentially — a parallel pool needs an importable __main__ to relaunch workers, which a stdin (`… | python -`), `python -c`, or REPL invocation does not have. Write the step to a .py file (or pass parallel=False) to silence this.', file=sys.stderr, flush=True)
            parallel = False
        ran_parallel = False

        if parallel and len(uncached_work) >= _PARALLEL_THRESHOLD:
            ran_parallel = extractparallel(uncached_work, per_file, root, max_workers, total, cache_location)

        if not ran_parallel:
            extractsequential([(i, p) for i, p in uncached_work if per_file[i] is None], per_file, root, total, cache_location)

    for i in range(total):
        if per_file[i] is None:
            per_file[i] = {'nodes': [], 'edges': [], 'error': 'internal: no extraction result produced'}
    _empty_sources: list[str] = []

    for i, _p in enumerate(paths):
        _res = per_file[i] or {}

        if _res.get('nodes') or _res.get('error') or _res.get('skipped'):
            continue

        if extractor(_p) is not None:
            _empty_sources.append(str(_p))

    if _empty_sources:
        _shown = ', '.join((Path(x).name for x in _empty_sources[:5]))
        _more = f' (+{len(_empty_sources) - 5} more)' if len(_empty_sources) > 5 else ''
        print(f'  warning: {len(_empty_sources)} source file(s) produced zero nodes and are absent from the graph: {_shown}{_more}. A re-run will retry them (empties are no longer cached); if it persists, please report the file(s) (#1666).', file=sys.stderr, flush=True)
    _failed_sources: list[str] = []
    _failed_seen: set[str] = set()

    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _key = str(_p)

        if _res.get('error'):
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)
            continue

        if _res.get('skipped'):
            continue

        if not _res.get('nodes') and extractor(_p) is not None:
            if _key not in _failed_seen:
                _failed_sources.append(_key)
                _failed_seen.add(_key)
    from graphify.detect import CODE_EXTENSIONS as CODEEXTS
    _no_extractor: dict[str, int] = {}

    for _p in paths:
        _ext = _p.suffix.lower()

        if _ext in CODEEXTS and extractor(_p) is None:
            _no_extractor[_ext] = _no_extractor.get(_ext, 0) + 1

    if _no_extractor:
        _by_count = ', '.join((f'{ext} ({n})' for ext, n in sorted(_no_extractor.items(), key=lambda kv: (-kv[1], kv[0]))))
        _tot = sum(_no_extractor.values())
        print(f'  warning: {_tot} file(s) are classified as code but graphify has no AST extractor for their language, so they contributed nothing to the graph: {_by_count}. Please open an issue to request support for these (#1689).', file=sys.stderr, flush=True)
    _missing_dep_count: dict[str, int] = {}
    _missing_dep_error: dict[str, str] = {}

    for i, _p in enumerate(paths):
        _err = (per_file[i] or {}).get('error') or ''

        if _DEP_MISSING_MARKER in _err or _DEP_LOAD_FAILED_MARKER in _err:
            _ext = _p.suffix.lower()
            _missing_dep_count[_ext] = _missing_dep_count.get(_ext, 0) + 1
            _missing_dep_error.setdefault(_ext, _err)

    for _ext, _n in sorted(_missing_dep_count.items(), key=lambda kv: (-kv[1], kv[0])):
        _extra = _EXTRA_FOR_EXTENSION.get(_ext)
        _err_text = _missing_dep_error[_ext]

        if _extra and _DEP_MISSING_MARKER in _err_text:
            _reason = _err_text.split('. ')[0]
            _hint = f' Install it with: pip install "graphifyy[{_extra}]"'
            _cause = 'a dependency is missing'
        else:
            _reason = _err_text
            _hint = ''
            _cause = 'a dependency is missing' if _DEP_MISSING_MARKER in _err_text else 'a dependency failed to load'
        print(f'  warning: {_n} {_ext} file(s) contributed nothing to the graph because {_cause}: {_reason}.{_hint} (#1745)', file=sys.stderr, flush=True)
    _syntax_error_files: list[tuple[str, int | None]] = []

    for i, _p in enumerate(paths):
        _res = per_file[i] or {}
        _pe = _res.get('parse_errors')

        if not _pe:
            continue

        if len(_res.get('nodes', [])) <= 1 or _pe.get('multiline_error'):
            try:
                _rel = os.path.relpath(str(_p), str(root)).replace('\\', '/')
            except ValueError:
                _rel = _p.as_posix()
            _kept = max(len(_res.get('nodes', [])) - 1, 0)
            _syntax_error_files.append((_rel, _pe.get('first_error_line'), _kept))

    if _syntax_error_files:

        def syntaxerror(rel: str, line: 'int | None', kept: int) -> str:
            _where = f'first error at line {line}' if line else 'syntax error'
            _got = 'no symbols extracted' if kept == 0 else f'{kept} symbol(s) extracted'

            return f'{rel} ({_where}, {_got})'

        _shown = ', '.join((syntaxerror(*f) for f in _syntax_error_files[:5]))
        _more = f' (+{len(_syntax_error_files) - 5} more)' if len(_syntax_error_files) > 5 else ''
        print(f'  warning: {len(_syntax_error_files)} file(s) had syntax errors and may be partially extracted: {_shown}{_more}', file=sys.stderr, flush=True)
    _symbolless_files: list[tuple[str, int]] = []

    for i, _p in enumerate(paths):
        _res = per_file[i] or {}

        if _res.get('parse_errors') or _res.get('error') or _res.get('skipped'):
            continue

        if extractor(_p) is None:
            continue

        if len(_res.get('nodes', [])) != 1:
            continue

        try:
            _size = _p.stat().st_size

            if not _p.read_text(encoding='utf-8', errors='ignore').strip():
                continue
        except OSError:
            _size = 0

        try:
            _display_path = os.path.relpath(str(_p), str(root)).replace('\\', '/')
        except ValueError:
            _display_path = _p.as_posix()
        _symbolless_files.append((_display_path, _size))

    if _symbolless_files:
        _total_bytes = sum((size for _, size in _symbolless_files))
        _total_mb = _total_bytes / (1024 * 1024)
        _shown_sl = ', '.join((rel for rel, _ in _symbolless_files[:5]))
        _more_sl = f' (+{len(_symbolless_files) - 5} more)' if len(_symbolless_files) > 5 else ''
        print(f'  warning: {len(_symbolless_files)} code file(s) yielded no symbols ({_total_mb:.2f} MB) — a data file, or source this extractor models no symbols for: {_shown_sl}{_more_sl}', file=sys.stderr, flush=True)

    for path, result in zip(paths, per_file):
        if path.suffix in ('.tf', '.tfvars', '.hcl'):
            terraform.prepare(result, path, root)
    all_nodes: list[dict] = []
    all_edges: list[dict] = []
    all_raw_calls: list[dict] = []

    for result in per_file:
        all_nodes.extend(result.get('nodes', []))
        all_edges.extend(result.get('edges', []))
        all_raw_calls.extend(result.get('raw_calls', []))
    callable_nids: set[str] = set()
    suppressambiguous(all_edges, all_nodes, all_raw_calls, ambiguous_python_modules, python_module_alias_files, root)
    augmentedges(paths, all_nodes, all_edges, root, ambiguous_python_modules=ambiguous_python_modules, resolution_context_nodes=resolution_context_nodes)
    mergedecldef(all_nodes, all_edges)
    id_remap: dict[str, str] = {}

    def portableroot(p: Path) -> str:
        try:
            rel = os.path.relpath(str(p), str(root)).replace('\\', '/')
        except ValueError:
            return p.name

        updepth = 0

        for seg in rel.split('/'):
            if seg == '..':
                updepth += 1
            else:
                break

        return p.name if updepth > 3 else rel

    prefix_remap: dict[Path, list[tuple[str, str]]] = {}
    stem_forms: dict[Path, tuple[str, list[str]]] = {}
    remap_paths: list[Path] = list(paths)
    _resolve_memo: dict[str, Path] = {}

    def resolved(p: 'str | Path') -> Path:
        key = str(p)
        hit = _resolve_memo.get(key)

        if hit is None:
            hit = Path(p).resolve()
            _resolve_memo[key] = hit

        return hit

    _remap_seen: set[Path] = set()

    for _p in paths:
        try:
            _remap_seen.add(resolved(_p))
        except (OSError, RuntimeError):
            pass

    for _e in all_edges:
        _tf = _e.get('target_file')

        if not _tf:
            continue

        _raw_tp = Path(_tf)

        try:
            _tp = resolved(_raw_tp)
        except (OSError, RuntimeError):
            continue

        if _tp in _remap_seen:
            continue

        _remap_seen.add(_tp)

        try:
            _tp.relative_to(root)
        except ValueError:
            try:
                if _tp.is_file():
                    ext_new_id = make('ext', portableroot(_tp))
                    id_remap[make(str(_tp))] = ext_new_id

                    if _raw_tp != _tp:
                        id_remap[make(str(_raw_tp))] = ext_new_id
                    id_remap.setdefault(make(str(_tp)) + '__entry', ext_new_id + '__entry')

                    if _raw_tp != _tp:
                        id_remap.setdefault(make(str(_raw_tp)) + '__entry', ext_new_id + '__entry')
            except OSError:
                pass
            continue

        try:
            if not _tp.is_file():
                continue
        except OSError:
            continue

        remap_paths.append(_tp)

        if _raw_tp != _tp:
            _remap_seen.add(_raw_tp)
            remap_paths.append(_raw_tp)

    for path in remap_paths:
        old_id = make(str(path))

        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = resolved(path).relative_to(root)
            except ValueError:
                continue

        new_id = filenodeid(rel)

        if old_id != new_id:
            id_remap[old_id] = new_id
        path_abs = resolved(path)
        old_id_abs = make(str(path_abs))

        if old_id_abs != new_id:
            id_remap[old_id_abs] = new_id
        old_prefs: list[tuple[str, str]] = []
        old_pref = filenodeid(path)

        if old_pref != new_id:
            old_prefs.append((old_pref, new_id))
        old_pref_abs = filenodeid(path_abs)

        if old_pref_abs != new_id and old_pref_abs != old_pref:
            old_prefs.append((old_pref_abs, new_id))

        for _old, _pref in ((old_id, old_pref), (old_id_abs, old_pref_abs)):
            if not _old.startswith(_pref):
                continue

            _entry_new = new_id + _old[len(_pref):] + '__entry'
            _entry_old = _old + '__entry'

            if _entry_old != _entry_new:
                id_remap.setdefault(_entry_old, _entry_new)

        if old_prefs:
            prefix_remap[path_abs] = old_prefs
        stem_forms[path_abs] = (new_id, [old_pref_abs, old_pref, new_id])

    if id_remap:
        for n in all_nodes:
            if n.get('id') in id_remap:
                n['id'] = id_remap[n['id']]

        for e in all_edges:
            if e.get('source') in id_remap:
                e['source'] = id_remap[e['source']]

            if e.get('target') in id_remap:
                e['target'] = id_remap[e['target']]

        for rc in all_raw_calls:
            cn = rc.get('caller_nid')

            if cn in id_remap:
                rc['caller_nid'] = id_remap[cn]

        for result in per_file:
            for ext in result.get('swift_extensions', []) or []:
                en = ext.get('nid')

                if en in id_remap:
                    ext['nid'] = id_remap[en]
        remapobjcfields(per_file, id_remap)

    if prefix_remap:
        sym_remap: dict[str, str] = {}
        edge_alias_candidates: dict[str, set[str]] = {}

        for n in all_nodes:
            sf = n.get('source_file')

            if not sf:
                continue

            if n.get('type') == 'package':
                continue

            try:
                entry = prefix_remap.get(resolved(sf))
            except Exception:
                continue

            if entry is None:
                continue

            nid = n.get('id', '')
            canonical_nid: str | None = None

            for old_pref, new_pref in entry:
                if nid.startswith(old_pref + '_'):
                    canonical_nid = new_pref + nid[len(old_pref):]

                    if canonical_nid != nid:
                        sym_remap[nid] = canonical_nid
                    break

                if nid.startswith(new_pref + '_'):
                    canonical_nid = nid
                    break

            if canonical_nid is None:
                continue

            for old_pref, new_pref in entry:
                if not canonical_nid.startswith(new_pref + '_'):
                    continue

                old_nid = old_pref + canonical_nid[len(new_pref):]

                if old_nid != canonical_nid:
                    edge_alias_candidates.setdefault(old_nid, set()).add(canonical_nid)

        if sym_remap:
            for n in all_nodes:
                if n.get('id') in sym_remap:
                    n['id'] = sym_remap[n['id']]

            for e in all_edges:
                if e.get('source') in sym_remap:
                    e['source'] = sym_remap[e['source']]

                if e.get('target') in sym_remap:
                    e['target'] = sym_remap[e['target']]

            for rc in all_raw_calls:
                cn = rc.get('caller_nid')

                if cn in sym_remap:
                    rc['caller_nid'] = sym_remap[cn]

            for result in per_file:
                for ext in result.get('swift_extensions', []) or []:
                    en = ext.get('nid')

                    if en in sym_remap:
                        ext['nid'] = sym_remap[en]
            remapobjcfields(per_file, sym_remap)

        if edge_alias_candidates:

            def edgekey(edge: dict) -> str:
                return json.dumps({k: v for k, v in edge.items() if k != 'target_file'}, sort_keys=True, separators=(',', ':'), default=str)

            edge_key_counts = Counter((edgekey(edge) for edge in all_edges))
            owned_node_ids = {node.get('id') for node in all_nodes}
            deduped_edges: list[dict] = []

            for edge in all_edges:
                if edge.get('relation') == 're_exports':
                    candidates = edge_alias_candidates.get(edge.get('target', ''), set())

                    if len(candidates) == 1 and edge.get('target') not in owned_node_ids:
                        edge['target'] = next(iter(candidates))
                    deduped_edges.append(edge)
                    continue

                candidates = edge_alias_candidates.get(edge.get('target', ''), set()) if edge.get('relation') == 'imports' else set()

                if len(candidates) == 1:
                    candidate = next(iter(candidates))
                    twin_key = edgekey({**edge, 'target': candidate})

                    if edge_key_counts[twin_key]:
                        if edge.get('target') in owned_node_ids:
                            edge_key_counts[twin_key] -= 1
                        continue

                deduped_edges.append(edge)
            all_edges[:] = deduped_edges

    if stem_forms:
        owned_ids = {n.get('id') for n in all_nodes}

        def decompose(target: str, tf: str) -> 'tuple[str, str] | None':
            try:
                forms = stem_forms.get(resolved(tf))
            except (OSError, RuntimeError):
                return None

            if not forms:
                return None

            canonical, prefixes = forms

            for pref in prefixes:
                if pref and target.startswith(pref + '_'):
                    return (canonical, target[len(pref) + 1:])

            return None

        chain: dict[tuple[str, str], set] = {}

        def first(key) -> 'str | None':
            targets = chain.get(key)

            return next(iter(targets)) if targets and len(targets) == 1 else None

        def learn(e: dict) -> None:
            tf = e.get('target_file')

            if not tf or e.get('target') not in owned_ids:
                return
            dec = decompose(e.get('target', ''), tf)

            if dec is not None:
                chain.setdefault((e.get('source'), dec[1]), set()).add(e['target'])

        for e in all_edges:
            if e.get('relation') == 're_exports':
                learn(e)
        pending = [e for e in all_edges if e.get('relation') in ('re_exports', 'imports') and e.get('target_file') and (e.get('target') not in owned_ids)]

        for _ in range(8):
            progressed = False
            still: list[dict] = []

            for e in pending:
                dec = decompose(e.get('target', ''), e['target_file'])
                resolved_target = first((dec[0], dec[1])) if dec else None

                if resolved_target is None:
                    still.append(e)
                    continue

                e['target'] = resolved_target

                if e.get('relation') == 're_exports':
                    chain.setdefault((e.get('source'), dec[1]), set()).add(resolved_target)
                progressed = True
            pending = still

            if not progressed:
                break

        for e in pending:
            dec = decompose(e.get('target', ''), e['target_file'])

            if dec is not None:
                e['target'] = f'{dec[0]}_{dec[1]}'
    repointpackage(paths, all_nodes, all_edges, root)
    repointsiblings(paths, all_nodes, all_edges, root)
    mergeswiftexts(per_file, all_nodes, all_edges)
    mergecspartials(per_file, all_nodes, all_edges, paths, root)
    disambiguatenodes(all_nodes, all_edges, all_raw_calls, root)
    canonicalizecsns(all_nodes, all_edges)
    _php_exts = {'.php', '.phtml', '.php3', '.php4', '.php5', '.php7', '.phps'}
    _php_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix.lower() in _php_exts and (not p.name.lower().endswith('.blade.php'))]

    if _php_sel:
        try:
            resolvephptypes([r for r, _ in _php_sel], [p for _, p in _php_sel], all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('PHP type-reference resolution failed, skipping: %s', exc)
    _java_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == '.java']

    if _java_sel:
        try:
            resolvejavatypes([r for r, _ in _java_sel], [p for _, p in _java_sel], all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Java type-reference resolution failed, skipping: %s', exc)
    _go_sel = [(r, p) for r, p in zip(per_file, paths) if p.suffix == '.go']

    if _go_sel:
        try:
            resolvegotypes([r for r, _ in _go_sel], [p for _, p in _go_sel], all_nodes, all_edges, root, resolution_context_nodes, resolution_context_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Go type-reference resolution failed, skipping: %s', exc)
    py_paths = [p for p in paths if p.suffix == '.py']

    if py_paths:
        py_results = [r for r, p in zip(per_file, paths) if p.suffix == '.py']

        try:
            cross_file_edges = resolvecrossimports(py_results, py_paths, all_nodes, all_edges, ambiguous_python_modules=ambiguous_python_modules)
            all_edges.extend(cross_file_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Cross-file import resolution failed, skipping: %s', exc)
    rewirestubs(all_nodes, all_edges)
    java_paths = [p for p in paths if p.suffix == '.java']

    if java_paths:
        java_results = [r for r, p in zip(per_file, paths) if p.suffix == '.java']

        try:
            all_edges.extend(resolvejavaimports(java_results, java_paths))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Java cross-file import resolution failed, skipping: %s', exc)
    _DOTNET_TYPE_EXTS = {'.cs', '.razor', '.cshtml'}
    cs_paths = [p for p in paths if p.suffix.lower() in _DOTNET_TYPE_EXTS]

    if cs_paths:
        cs_results = [r for r, p in zip(per_file, paths) if p.suffix.lower() in _DOTNET_TYPE_EXTS]

        try:
            csharp.references(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('C# type-reference resolution failed, skipping: %s', exc)

        try:
            csharp.imports(cs_results, cs_paths, all_nodes, all_edges)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('C# cross-file import resolution failed, skipping: %s', exc)

    def isbash(result: object) -> bool:
        if not isinstance(result, dict):
            return False

        nodes = result.get('nodes')

        if not isinstance(nodes, list):
            return False

        for n in nodes:
            if not isinstance(n, dict):
                continue

            md = n.get('metadata')

            if isinstance(md, dict) and md.get('language') == 'bash':
                return True

        return False

    sh_pairs = [(r, p) for r, p in zip(per_file, paths) if p.suffix in ('.sh', '.bash') or isbash(r)]

    if sh_pairs:
        sh_results = [r for r, _ in sh_pairs]
        sh_paths = [p for _, p in sh_pairs]

        try:
            all_edges.extend(resolvebashedges(sh_results, sh_paths, root, existing_edges=all_edges))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Bash cross-file call resolution failed, skipping: %s', exc)
    global_label_to_nids: dict[str, list[str]] = {}
    global_label_to_nids_ci: dict[str, list[str]] = {}
    resolution_nodes = all_nodes

    if resolution_context_nodes:
        _fresh_ids = {n['id'] for n in all_nodes}
        resolution_nodes = all_nodes + [n for n in resolution_context_nodes if n.get('id') and n['id'] not in _fresh_ids]

    for n in resolution_nodes:
        if n.get('type') == 'namespace':
            continue

        raw = n.get('label', '')
        normalised = raw.strip('()').lstrip('.')

        if normalised:
            global_label_to_nids.setdefault(normalised, []).append(n['id'])

            if caseinsensitive(n.get('source_file')):
                global_label_to_nids_ci.setdefault(normalised.lower(), []).append(n['id'])
    callable_nids = {n['id'] for n in resolution_nodes if n.get('_callable')}
    class_nids = {n['id'] for n in resolution_nodes if n.get('_callable_class')}
    registry.run(paths, per_file, all_nodes, all_edges, resolvers=[_KOTLIN_IMPORT_TARGET_RESOLVER])
    file_to_symbol_imports: dict[str, set[str]] = {}
    file_to_module_imports: dict[str, set[str]] = {}

    for e in all_edges:
        if e.get('relation') == 'imports':
            file_to_symbol_imports.setdefault(e['source'], set()).add(e['target'])
        elif e.get('relation') == 'imports_from':
            file_to_module_imports.setdefault(e['source'], set()).add(e['target'])
    sf_to_file_nid: dict[str, str] = {}

    for n in resolution_nodes:
        sf = n.get('source_file')

        if sf and n.get('label') == Path(str(sf)).name:
            sf_to_file_nid.setdefault(str(sf), n['id'])
    nid_to_file_nid: dict[str, str] = {}
    nid_to_source_file: dict[str, str] = {}

    for n in resolution_nodes:
        sf = n.get('source_file')

        if not sf:
            continue

        nid_to_source_file[n['id']] = str(sf)
        fnid = sf_to_file_nid.get(str(sf))

        if fnid is not None:
            nid_to_file_nid[n['id']] = fnid
            continue

        sf_path = Path(sf)

        try:
            sf_rel = sf_path.relative_to(root) if sf_path.is_absolute() else sf_path
        except ValueError:
            sf_rel = sf_path
        nid_to_file_nid[n['id']] = filenodeid(sf_rel)
    existing_pairs = {(e['source'], e['target']) for e in all_edges}
    nid_to_label: dict[str, str] = {n['id']: str(n.get('label', '')) for n in resolution_nodes if n.get('id')}
    call_like_pairs = {(e['source'], e['target']) for e in all_edges if e.get('relation') in ('calls', 'indirect_call')}
    _JS_TS_CALL_SUFFIXES = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs')
    _go_module_cache: dict[Path, str | None] = {}
    _member_nids = {e.get('target') for e in all_edges if e.get('relation') in ('case_of', 'defines')}
    _elixir_modules_by_file: dict[str, set[str]] = {}

    for n in resolution_nodes:
        if n.get('_elixir_module') and n.get('source_file'):
            _elixir_modules_by_file.setdefault(str(n['source_file']), set()).add(str(n.get('label', '')))
    _csharp_stub_resolver: CsharpNameResolver | None = None

    for rc in all_raw_calls:
        if rc.get('_ambiguous_python_import'):
            continue

        callee = rc.get('callee', '')

        if not callee:
            continue

        if callee in GLOBALS:
            continue

        if rc.get('is_member_call'):
            continue

        if rc.get('is_mixin'):
            continue

        if rc.get('language') == 'bash':
            continue

        if rc.get('language') == 'markdown':
            continue

        if rc.get('language') == 'go' and callee in go.FUNCS:
            continue

        candidates = global_label_to_nids.get(callee, [])

        if not candidates and caseinsensitive(rc.get('source_file')):
            candidates = global_label_to_nids_ci.get(callee.lower(), [])

        if not candidates:
            continue

        if rc.get('csharp_new'):
            candidates = [c for c in candidates if c not in _member_nids]

            if not candidates:
                continue

        stub_scope_hit = False

        if rc.get('via_stub') and rc.get('lang') == 'csharp':
            if _csharp_stub_resolver is None:
                _csharp_stub_resolver = csharp.resolver(all_nodes, all_edges)
            caller_node = _csharp_stub_resolver['mapping'].get(rc.get('caller_nid', ''))
            scoped = csharp.label(_csharp_stub_resolver, callee, caller_node, str(rc.get('source_file', ''))) if caller_node is not None else None

            if scoped in candidates:
                candidates = [scoped]
                stub_scope_hit = True
        caller_family = langfamily(rc.get('source_file'))

        if caller_family is not None:
            candidates = [c for c in candidates if (candidate_family := langfamily(nid_to_source_file.get(c))) is None or candidate_family == caller_family]

            if not candidates:
                continue

        go_exact_import = False

        if rc.get('language') == 'go' and rc.get('import_path'):
            import_path = str(rc['import_path'])
            candidates = [candidate for candidate in candidates if goimportpath(nid_to_source_file.get(candidate, ''), root, _go_module_cache) == import_path]

            if not candidates:
                continue

            go_exact_import = True
        elixir_call_scope = rc.get('elixir_call_scope')

        if elixir_call_scope is not None:
            scope = set(elixir_call_scope)
            candidates = [candidate for candidate in candidates if _elixir_modules_by_file.get(nid_to_source_file.get(candidate, ''), set()) & scope]

            if not candidates:
                continue

        caller = rc['caller_nid']
        caller_file_nid = sf_to_file_nid.get(str(rc.get('source_file', ''))) or nid_to_file_nid.get(caller)
        imported_symbols = file_to_symbol_imports.get(caller_file_nid, set())
        imported_modules = file_to_module_imports.get(caller_file_nid, set())

        def hasimportevidence(candidate_id: str) -> bool:
            candidate_file_nid = nid_to_file_nid.get(candidate_id)

            return candidate_id in imported_symbols or (candidate_file_nid is not None and candidate_file_nid in imported_modules)

        if len(candidates) == 1:
            tgt = candidates[0]
            has_import_evidence = go_exact_import or hasimportevidence(tgt)
        else:
            symbol_matches = [c for c in candidates if c in imported_symbols]

            if len(symbol_matches) == 1:
                tgt = symbol_matches[0]
                has_import_evidence = True
            else:
                module_matches = [c for c in candidates if (cf := nid_to_file_nid.get(c)) is not None and cf in imported_modules]

                if len(module_matches) == 1:
                    tgt = module_matches[0]
                    has_import_evidence = True
                else:
                    tgt = disambiguate(candidates, {c: nid_to_source_file.get(c, '') for c in candidates}, rc.get('source_file', ''))

                    if tgt is None:
                        continue

                    has_import_evidence = False

        if not nid_to_source_file.get(tgt):
            continue

        if rc.get('indirect'):
            if tgt != caller and (caller, tgt) not in call_like_pairs and (tgt in callable_nids) and (tgt not in class_nids):
                call_like_pairs.add((caller, tgt))
                all_edges.append({'source': caller, 'target': tgt, 'relation': 'indirect_call', 'context': rc.get('context', 'argument'), 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})
            continue

        if not has_import_evidence and str(rc.get('source_file', '')).endswith(_JS_TS_CALL_SUFFIXES):
            continue

        if rc.get('via_stub') and (not (has_import_evidence or stub_scope_hit)):
            continue

        if tgt != caller and (caller, tgt) not in existing_pairs:
            existing_pairs.add((caller, tgt))

            _tgt_label = nid_to_label.get(tgt, '')
            is_rust_constructor = bool(str(rc.get('source_file', '')).endswith('.rs') and _tgt_label and (not _tgt_label.endswith(')')))
            all_edges.append({'source': caller, 'target': tgt, 'relation': 'references' if is_rust_constructor else 'calls', 'context': 'constructor' if is_rust_constructor else 'call', 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})
    go_pkg_files: dict[str, list[str]] = {}

    for sf, fnid in sf_to_file_nid.items():
        if not sf.endswith('.go'):
            continue

        import_path = goimportpath(sf, root, _go_module_cache)

        if import_path:
            go_pkg_files.setdefault(import_path, []).append(fnid)

    if go_pkg_files:
        go_pkg_sink_ids = {make('go', 'pkg', ip): ip for ip in go_pkg_files}
        rewritten_edges: list[dict] = []

        for e in all_edges:
            import_path = go_pkg_sink_ids.get(e.get('target', '')) if e.get('relation') == 'imports_from' else None

            if import_path is None:
                rewritten_edges.append(e)
                continue

            for fnid in go_pkg_files[import_path]:
                if fnid == e['source'] or (e['source'], fnid) in existing_pairs:
                    continue

                repointed = dict(e)
                repointed['target'] = fnid
                rewritten_edges.append(repointed)
                existing_pairs.add((e['source'], fnid))
        all_edges[:] = rewritten_edges

    if resolution_context_nodes or resolution_context_edges:
        _rl_nodes = list(resolution_nodes)
        _rl_edges = all_edges + list(resolution_context_edges or [])
        _n0, _e0 = (len(_rl_nodes), len(_rl_edges))
        registry.run(paths, per_file, _rl_nodes, _rl_edges)
        all_nodes.extend(_rl_nodes[_n0:])
        all_edges.extend(_rl_edges[_e0:])
    else:
        registry.run(paths, per_file, all_nodes, all_edges)
    ext_id_remap: dict[str, str] = {}
    owned_ids = {n.get('id') for n in all_nodes}
    _sf_forms: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    def sfentry(sf: str, sf_path: Path) -> tuple[str, str, tuple[str, ...]]:
        cached = _sf_forms.get(sf)

        if cached is not None:
            return cached

        try:
            rel = sf_path.relative_to(root)
        except ValueError:
            portable = portableroot(sf_path)
            canonical_id = make('ext', portable)
            new_sf = portable
        else:
            canonical_id = filenodeid(rel)
            new_sf = rel.as_posix()

        try:
            sf_resolved = sf_path.resolve()
        except (OSError, RuntimeError):
            sf_resolved = sf_path
        keys = tuple({make(str(sf_path)), make(str(sf_resolved)), make(stem(sf_path)), make(stem(sf_resolved))})
        entry = (new_sf, canonical_id, keys)
        _sf_forms[sf] = entry

        return entry

    for item in all_nodes + all_edges:
        sf = item.get('source_file')

        if sf:
            sf_path = Path(sf)

            if sf_path.is_absolute():
                new_sf, canonical_id, keys = sfentry(str(sf), sf_path)

                if 'id' in item:
                    for key in keys:
                        if key == canonical_id or key in ext_id_remap:
                            continue

                        if key in owned_ids and item.get('id') != key:
                            continue

                        ext_id_remap[key] = canonical_id
                item['source_file'] = new_sf
        df = item.get('definition_file')

        if df:
            df_path = Path(df)

            if df_path.is_absolute():
                new_df, _, _ = sfentry(str(df), df_path)
                item['definition_file'] = new_df

    if ext_id_remap:
        _ENTRY = '__entry'

        def canon(nid: str) -> str:
            if nid in ext_id_remap:
                return ext_id_remap[nid]

            if nid.endswith(_ENTRY) and nid[:-len(_ENTRY)] in ext_id_remap:
                return ext_id_remap[nid[:-len(_ENTRY)]] + _ENTRY

            if nid not in owned_ids:
                idx = nid.rfind('_')

                while idx > 0:
                    canonical = ext_id_remap.get(nid[:idx])

                    if canonical is not None:
                        return canonical + nid[idx:]

                    idx = nid.rfind('_', 0, idx)

            return nid

        for n in all_nodes:
            if n.get('id'):
                n['id'] = canon(n['id'])

        for e in all_edges:
            if e.get('source'):
                e['source'] = canon(e['source'])

            if e.get('target'):
                e['target'] = canon(e['target'])

    for n in all_nodes:
        n.pop('origin_file', None)

    for e in all_edges:
        e.pop('local_alias', None)
        e.pop('_python_plain_import', None)


    for _item in (*all_nodes, *all_edges):
        _sf = _item.get('source_file')

        if _sf and '\\' in str(_sf):
            _item['source_file'] = PurePath(_sf).as_posix()
        _df = _item.get('definition_file')

        if _df and '\\' in str(_df):
            _item['definition_file'] = PurePath(_df).as_posix()

    return {'nodes': all_nodes, 'edges': all_edges, 'failed_sources': _failed_sources, 'extracted_sources': [str(p) for p in paths]}

RUNKEYS = frozenset({'extracted_sources', 'failed_sources'})


def collect(target: Path, *, follow_symlinks: bool=False, root: Path | None=None) -> list[Path]:
    containment_root = root if root is not None else target
    from graphify.detect import resolves

    if target.is_file():
        return [target] if resolves(target, containment_root) else []

    _EXTENSIONS = set(_DISPATCH.keys())
    from graphify.detect import ignored as isignored, installed, noise, ignore
    ignore_root = root if root is not None else target
    patterns = ignore(ignore_root)
    ignore_cache: dict[Path, bool] = {}

    def skipped(p: Path) -> bool:
        return bool(patterns and isignored(p, ignore_root, patterns, _cache=ignore_cache))

    if not follow_symlinks:
        if any((noise(part) for part in target.parts)):
            return []

        has_negation = any((pat.startswith('!') for _, pat in patterns))
        results: list[Path] = []

        for dirpath, dirnames, filenames in os.walk(target):
            dp = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not noise(d, dp) and (has_negation or not skipped(dp / d))]

            for fname in filenames:
                p = dp / fname
                suffix = p.suffix

                if (suffix in _EXTENSIONS or suffix.lower() in _EXTENSIONS) and (not skipped(p)) and (not installed(p)) and resolves(p, containment_root):
                    results.append(p)

        return sorted(results)

    results = []

    for dirpath, dirnames, filenames in os.walk(target, followlinks=True):
        if os.path.islink(dirpath):
            real = os.path.realpath(dirpath)
            parent_real = os.path.realpath(os.path.dirname(dirpath))

            if parent_real == real or parent_real.startswith(real + os.sep):
                dirnames.clear()
                continue

        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not noise(d, dp) and (not (dp / d).is_symlink() or resolves(dp / d, containment_root))]

        for fname in filenames:
            p = dp / fname
            suffix = p.suffix

            if (suffix in _EXTENSIONS or suffix.lower() in _EXTENSIONS) and (not skipped(p)) and (not installed(p)) and resolves(p, containment_root):
                results.append(p)

        return sorted(results)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python -m graphify.extract <file_or_dir> ...', file=sys.stderr)
        sys.exit(1)
    paths: list[Path] = []

    for arg in sys.argv[1:]:
        paths.extend(collect_files(Path(arg)))
    result = extract(paths)
    print(json.dumps(result, indent=2))