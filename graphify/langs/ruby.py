from __future__ import annotations
import re
from pathlib import Path
from typing import Any
from ..src.models import LanguageConfig
from ..src.engine import generic
from ..build import isfilelabel


def sanitize(name: str) -> str:
    if not name:
        return name

    if name.endswith('!'):
        return f'{name[:-1]}_bang'

    if name.endswith('?'):
        return f'{name[:-1]}_pred'

    if name.endswith('='):
        return f'{name[:-1]}_eq'

    return name

config = LanguageConfig(ts_module='tree_sitter_ruby', class_types=frozenset({'class', 'module'}), function_types=frozenset({'method', 'singleton_method'}), import_types=frozenset(), call_types=frozenset({'call'}), call_function_field='method', call_accessor_node_types=frozenset(), name_fallback_child_types=('constant', 'scope_resolution', 'identifier'), body_fallback_child_types=('body_statement',), function_boundary_types=frozenset({'method', 'singleton_method'}), sanitize_symbol_name_fn=sanitize)


def extract(path: Path) -> dict:
    return generic(path, config)


def key(label: str) -> str:
    return re.sub('[^a-zA-Z0-9]+', '', str(label)).lower()


def name(label: object) -> str:
    return str(label or '').strip('()').lstrip('.')


def kind(node: dict | None) -> str | None:
    data = node.get('metadata') if isinstance(node, dict) else None

    if not isinstance(data, dict):
        return None

    val = data.get('ruby_method_kind')

    return val if val in {'instance', 'singleton'} else None


def unsafe(node: dict | None) -> bool:
    data = node.get('metadata') if isinstance(node, dict) else None

    return isinstance(data, dict) and bool(data.get('ruby_lookup_unsafe') or data.get('ruby_reopened'))

CONST_RE = re.compile('^[A-Z][A-Za-z0-9_]*(?:::[A-Z][A-Za-z0-9_]*)*$')


def calls(batch: list[dict]) -> list[dict]:
    out: list[dict] = []

    for item in batch:
        if not isinstance(item, dict):
            continue

        for rc in item.get('raw_calls', []):
            if not isinstance(rc, dict):
                continue

            src = str(rc.get('source_file', ''))

            if src.endswith(('.rb', '.rake')):
                out.append(rc)

    return out


def resolve(batch: list[dict], nodes: list[dict], edges: list[dict]) -> None:
    mapping: dict[str, dict] = {str(node['id']): node for node in nodes if node.get('id')}
    files = [node for node in nodes if str(node.get('source_file', '')).endswith(('.rb', '.rake')) and isfilelabel(node.get('label'), node.get('source_file'))]
    complete = bool(files) and all((isinstance(n.get('metadata'), dict) and n['metadata'].get('ruby_resolution_schema') == 1 for n in files))
    bad = {str(node.get('source_file')) for node in files if unsafe(node)}
    external = {o for n in files for d in [n.get('metadata')] if isinstance(d, dict) for items in [d.get('ruby_external_method_owners', [])] if isinstance(items, list) for o in items if isinstance(o, str)}
    defs: dict[str, list[str]] = {}
    index: dict[tuple[str, str], str] = {}
    owners: dict[str, set[str]] = {}
    inherits: list[tuple[str, str, str | None, list[str] | None]] = []

    for e in edges:
        if e.get('relation') == 'inherits':
            src, tgt = (e.get('source'), e.get('target'))

            if src and tgt:
                d = e.get('metadata')
                cand = d.get('ruby_superclass_ref') if isinstance(d, dict) else None
                base = cand if isinstance(cand, str) else None
                cands = d.get('ruby_lexical_scopes') if isinstance(d, dict) else None
                scopes = cands if isinstance(cands, list) and all((isinstance(s, str) for s in cands)) else None
                inherits.append((str(src), str(tgt), base, scopes))
            continue

        if e.get('relation') != 'method':
            continue

        src, tgt = (e.get('source'), e.get('target'))

        if not src or not tgt:
            continue

        src, tgt = (str(src), str(tgt))
        cnode = mapping.get(src)

        if cnode is not None:
            csrc = str(cnode.get('source_file', ''))
            tsrc = str(mapping.get(tgt, {}).get('source_file', ''))

            if not csrc.endswith(('.rb', '.rake')) or not tsrc.endswith(('.rb', '.rake')):
                continue

            clabel = str(cnode.get('label', ''))
            defs.setdefault(key(clabel), []).append(src)

            if '::' in clabel:
                defs.setdefault(key(clabel.split('::')[-1]), []).append(src)
        tnode = mapping.get(tgt)

        if tnode is not None:
            mname = name(tnode.get('label'))
            index[src, mname] = tgt
            owners.setdefault(tgt, set()).add(src)

    for n in nodes:
        nid = n.get('id')
        sf = str(n.get('source_file', ''))
        label = str(n.get('label', ''))

        if nid and sf.endswith(('.rb', '.rake')) and CONST_RE.match(label):
            defs.setdefault(key(label), []).append(str(nid))

            if '::' in label:
                defs.setdefault(key(label.split('::')[-1]), []).append(str(nid))

    for k in list(defs):
        defs[k] = sorted(set(defs[k]))
    classes = {nid for nids in defs.values() for nid in nids}
    labels: dict[str, set[str]] = {}

    for nid in classes:
        label = str(mapping.get(nid, {}).get('label', ''))
        labels.setdefault(label, set()).add(nid)

    def lookup(raw: str | None, scopes: list[str] | None) -> str | None:
        if not raw or scopes is None:
            return None

        ref = raw.removeprefix('::')
        parts = [part for part in ref.split('::') if part]

        if not parts:
            return None

        search = [''] if raw.startswith('::') or not scopes else scopes

        for scope in search:
            target = '::'.join([*[p for p in scope.split('::') if p], *parts])
            cands = labels.get(target, set())

            if len(cands) == 1:
                return next(iter(cands))

            if len(cands) > 1:
                return None

            if scope and len(parts) > 1:
                prefix = '::'.join([*[p for p in scope.split('::') if p], parts[0]])

                if labels.get(prefix):
                    return None

        return None

    trees: dict[str, set[str]] = {}

    for src, tgt, base, scopes in inherits:
        if src not in classes:
            continue

        found = lookup(base, scopes)
        tnode = mapping.get(tgt)
        tlabel = str(tnode.get('label', '')) if tnode else ''
        tsrc = str(tnode.get('source_file', '')) if tnode else ''
        tail = str(base or '').removeprefix('::').split('::')[-1]
        ok = bool(tnode and tail and (key(tlabel.split('::')[-1]) == key(tail)))

        if found is not None and (found == tgt or (not tsrc and ok)):
            trees.setdefault(src, set()).add(found)
    typed: dict[tuple[str, str, str], set[str]] = {}
    ambiguous: set[tuple[str, str]] = set()

    for mid, group in owners.items():
        mname = name(mapping[mid].get('label'))
        mkind = kind(mapping.get(mid))

        if len(group) != 1 or mkind is None:
            ambiguous.update(((o, mname) for o in group))
            continue

        o = next(iter(group))
        typed.setdefault((o, mname, mkind), set()).add(mid)

    def outer(label: str) -> bool:
        parts = tuple((p for p in label.split('::') if p))

        for raw in external:
            prefix = raw.endswith('::*')
            ref = raw.removesuffix('::*') if prefix else raw
            oparts = tuple((p for p in ref.removeprefix('::').split('::') if p))

            if not oparts:
                continue

            if prefix:
                if ref.startswith('::'):
                    if parts[:len(oparts)] == oparts:
                        return True
                elif any((parts[i:i + len(oparts)] == oparts for i in range(len(parts) - len(oparts) + 1))):
                    return True

                continue

            if ref.startswith('::') and parts == oparts:
                return True

            if not ref.startswith('::') and parts[-len(oparts):] == oparts:
                return True

        return False

    def path(label: str) -> list[str]:
        return [s.strip().lower() for s in str(label).split('::') if s.strip()]

    fq: dict[tuple[str, ...], list[str]] = {}
    last: dict[str, list[str]] = {}

    for nid in sorted({n for items in defs.values() for n in items}):
        segs = path(str(mapping.get(nid, {}).get('label', '')))

        if segs:
            fq.setdefault(tuple(segs), []).append(nid)
            last.setdefault(segs[-1], []).append(nid)
    pairs = {(e.get('source'), e.get('target')) for e in edges}

    def single(item: str) -> str | None:
        items = defs.get(key(item), [])

        return items[0] if len(items) == 1 else None

    def direct(raw: str) -> str | None:
        segs = tuple(path(raw))

        if not segs:
            return None

        if raw.strip().startswith('::'):
            items = fq.get(segs, [])

            return items[0] if len(items) == 1 else None

        hits = {nid for p, nids in fq.items() if len(p) >= len(segs) and p[-len(segs):] == segs for nid in nids}

        return next(iter(hits)) if len(hits) == 1 else None

    def emit(src: str, tgt: str, rc: dict[str, Any], rel: str='calls', ctx: str='call') -> None:
        if not src or not tgt or src == tgt:
            return

        if (src, tgt) in pairs:
            return
        pairs.add((src, tgt))
        edges.append({'source': src, 'target': tgt, 'relation': rel, 'context': ctx, 'source_file': rc.get('source_file', ''), 'source_location': rc.get('source_location')})

    def inherited(owner: str, item: str, k: str) -> str | None:
        curr = owner
        visited: set[str] = set()
        initial = True

        while curr not in visited:
            visited.add(curr)
            node = mapping.get(curr)
            label = str(node.get('label', '')) if node else ''

            if curr not in classes or len(labels.get(label, set())) != 1 or unsafe(node) or outer(label):
                return None

            if (curr, item) in ambiguous:
                return None

            cands = typed.get((curr, item, k), set())

            if cands:
                if initial or len(cands) != 1:
                    return None

                return next(iter(cands))

            bases = trees.get(curr, set())

            if len(bases) != 1:
                return None

            curr = next(iter(bases))
            initial = False

        return None

    def promote(src: str, tgt: str, callee: str, rc: dict) -> None:
        pass

    for rc in calls(batch):
        if not rc.get('is_mixin'):
            continue

        caller = str(rc.get('caller_nid', ''))
        mod = rc.get('callee')

        if not caller or not mod:
            continue

        raw = str(mod)
        absolute = raw.startswith('::')
        segs = path(raw)
        cnode = mapping.get(caller)
        csegs = [] if absolute else path(str(cnode.get('label', '')) if cnode else '')
        target: str | None = None

        for i in range(len(csegs), -1, -1):
            items = fq.get(tuple(csegs[:i] + segs), [])

            if len(items) == 1:
                target = items[0]
                break

            if len(items) > 1:
                break

        if target is None and len(segs) == 1 and (not absolute):
            items = last.get(segs[0], [])

            if len(items) == 1:
                target = items[0]

        if target is not None:
            emit(caller, target, rc, rel='mixes_in', ctx='mixin')

    if complete:
        for rc in calls(batch):
            if rc.get('is_mixin') or rc.get('is_member_call') is not False:
                continue

            caller = str(rc.get('caller_nid', ''))
            callee = str(rc.get('callee', ''))
            mkind = kind(mapping.get(caller))
            group = owners.get(caller, set())
            cfile = str(mapping.get(caller, {}).get('source_file', ''))

            if not caller or not callee or mkind is None or (len(group) != 1) or (cfile in bad):
                continue

            target = inherited(next(iter(group)), callee, mkind)

            if target is not None and target != caller:
                promote(caller, target, callee, rc)

    for rc in calls(batch):
        if not rc.get('is_member_call'):
            continue

        caller = str(rc.get('caller_nid', ''))
        callee = rc.get('callee')

        if not caller or not callee:
            continue

        receiver = rc.get('receiver')

        if receiver and str(receiver).lstrip(':')[:1].isupper():
            raw = str(receiver)
            cid = direct(raw) if '::' in raw else single(raw)

            if cid is not None:
                if callee == 'new':
                    emit(caller, cid, rc)
                else:
                    mid = index.get((cid, str(callee)))
                    emit(caller, mid or cid, rc)
            continue

        rtype = rc.get('receiver_type')

        if not rtype:
            continue

        cid = single(str(rtype))

        if cid is None:
            continue

        mid = index.get((cid, str(callee)))

        if mid is None and complete and (not bad) and (not external):
            mid = inherited(cid, str(callee), 'instance')

        if mid is None:
            continue

        emit(caller, mid, rc)