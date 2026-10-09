from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import warnings
from collections.abc import Callable, Iterable

from graphify.paths import GRAPHIFY_OUT as _GRAPHIFY_OUT
from graphify.paths import replace

try:
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version('graphifyy')
except Exception:
    VERSION = 'unknown'

SCHEMA = 7
CLEANED: set[str] = set()


def clean(base: Path, current: Path) -> None:
    key = str(current)

    if key in CLEANED:
        return

    CLEANED.add(key)

    if not base.is_dir():
        return

    import shutil

    for child in base.iterdir():
        if child == current:
            continue

        try:
            if child.is_dir() and child.name.startswith('v'):
                shutil.rmtree(child, ignore_errors=True)
            elif child.suffix == '.json':
                child.unlink()
        except OSError:
            pass


FPLEN = 12
LEGACY = 0
CORRUPT = 0
CACHE: dict[tuple, str] = {}


def fingerprint(tag: str | Path) -> str:
    if isinstance(tag, Path):
        text = tag.read_text(encoding='utf-8', errors='replace')
    else:
        text = tag

    norm = '\n'.join((line.rstrip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'))).strip()

    return hashlib.sha256(norm.encode()).hexdigest()[:FPLEN]


def resolve(tag: str | Path | None = None, loc: str | Path | None = None) -> str | None:
    key = None

    if loc is not None:
        tag = Path(loc)

        try:
            st = tag.stat()
            key = (str(tag), st.st_size, st.st_mtime_ns)

            if key in CACHE:
                return CACHE[key]
        except OSError:
            pass

    if tag is None:
        return None

    try:
        fp = fingerprint(tag)

        if key is not None:
            CACHE[key] = fp

        return fp
    except (OSError, UnicodeError) as exc:
        warnings.warn(f'could not read cache tag {str(tag)!r} ({exc}); semantic cache entries cannot be attributed to a tag version and fall back to the unversioned layout, so this run may replay entries from an older extraction tag (#1939).', RuntimeWarning, stacklevel=3)

        return None


DELIM = re.compile('^---[ \\t]*\\r?$', re.MULTILINE)


def content(raw: bytes) -> bytes:
    body = raw.decode(errors='replace')
    opener = DELIM.match(body)

    if opener is None:
        return raw

    closer = DELIM.search(body, opener.end())

    if closer is None:
        return raw

    return body[closer.start() + 3:].encode()


INDEX: dict[str, dict] = {}
INDEXROOT: Path | None = None
INDEXANCHOR: Path | None = None
DIRTY: bool = False
REGISTERED: bool = False
INDEXPATH: Path | None = None
GRANULARITY = 2000000000


def granularity() -> int:
    raw = os.environ.get('GRAPHIFY_MTIME_GRANULARITY_MS', '').strip()

    if raw:
        try:
            ms = float(raw)
        except ValueError:
            return GRANULARITY

        if ms >= 0:
            return int(ms * 1000000)

    return GRANULARITY


def fresh(entry: object, st: os.stat_result) -> bool:
    if not isinstance(entry, dict):
        return False

    if entry.get('size') != st.st_size or entry.get('mtime_ns') != st.st_mtime_ns:
        return False

    indexed = entry.get('indexed_at_ns')

    if not isinstance(indexed, int):
        return False

    return st.st_mtime_ns + granularity() <= indexed


def entry(abskey: str, st: os.stat_result, observed: int) -> dict:
    item = INDEX.get(abskey)

    if not isinstance(item, dict) or item.get('size') != st.st_size or item.get('mtime_ns') != st.st_mtime_ns:
        item = {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
        INDEX[abskey] = item

    item['indexed_at_ns'] = observed

    return item


def relative(key: str, anchor: Path) -> str:
    p = Path(key)

    if not p.is_absolute():
        return key

    try:
        rel = os.path.relpath(p, anchor)
    except (ValueError, OSError):
        return key

    if rel == '..' or rel.startswith('..' + os.sep) or rel.startswith('../'):
        return key

    return rel.replace(os.sep, '/')


def absolute(key: str, anchor: Path) -> str:
    p = Path(key)

    if p.is_absolute():
        return str(p)

    return str(anchor / p)


def indexfile(root: Path) -> Path:
    out = Path(_GRAPHIFY_OUT)
    base = out if out.is_absolute() else Path(root).resolve() / out

    return base / 'cache' / 'stat-index.json'


def ensure(root: Path, cache: Path | None = None) -> None:
    global INDEX, INDEXROOT, INDEXANCHOR, DIRTY
    global REGISTERED, INDEXPATH

    newroot = Path(cache if cache is not None else root).resolve()

    if INDEXROOT is not None:
        if INDEXROOT == newroot:
            return

        warnings.warn(f"stat index switched from cache root {str(INDEXROOT)!r} to {str(newroot)!r} within one process; each root's stat-index.json now only reflects its own files, but library callers crossing project roots in one process should still use a fresh process per root to avoid losing the fastpath (#3989).", RuntimeWarning, stacklevel=3)
        flush()

    INDEXROOT = newroot
    INDEXANCHOR = Path(root).resolve()
    p = indexfile(INDEXROOT)
    INDEXPATH = p
    INDEX = {}

    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))

            if isinstance(raw, dict):
                for k, v in raw.items():
                    if not isinstance(k, str):
                        continue

                    if Path(k).is_absolute():
                        INDEX.setdefault(k, v)
                    else:
                        INDEX[absolute(k, INDEXANCHOR)] = v
        except (json.JSONDecodeError, OSError):
            INDEX = {}

    DIRTY = False

    if not REGISTERED:
        atexit.register(flush)
        REGISTERED = True


def flush() -> None:
    global DIRTY, INDEXROOT

    if not DIRTY or INDEXROOT is None:
        return

    p = INDEXPATH if INDEXPATH is not None else indexfile(INDEXROOT)
    disk: dict[str, dict] = {}

    for k, v in INDEX.items():
        try:
            if not os.path.exists(k):
                continue
        except OSError:
            continue

        dk = relative(k, INDEXANCHOR) if INDEXANCHOR is not None else k
        disk[dk] = v

    try:
        if not INDEXROOT.is_dir():
            DIRTY = False

            return
    except OSError:
        DIRTY = False

        return

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='stat-index.', suffix='.tmp')

        try:
            os.write(fd, json.dumps(disk, separators=(',', ':')).encode())
            os.close(fd)
            replace(tmp, p)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass

            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass

    DIRTY = False


def normpath(path: Path) -> Path:
    import sys

    if sys.platform != 'win32':
        return path

    s = str(path)

    if s.startswith('\\\\?\\'):
        s = s[4:]

    return Path(os.path.normcase(s))


def hash(path: Path, root: Path = Path('.'), cache: Path | None = None) -> str:
    global DIRTY

    p = normpath(Path(path))
    root = normpath(Path(root))

    if not p.is_file():
        raise IsADirectoryError(f'file_hash requires a file, got: {p}')

    ensure(root, cache=cache)
    resolved = p.resolve()
    abskey = str(resolved)
    rootresolved = root.resolve()

    try:
        relresolved = resolved.relative_to(rootresolved)
    except ValueError:
        salt = resolved.as_posix().lower()
    else:
        walked = Path(os.path.abspath(p))
        rootwalked = Path(os.path.abspath(root))

        try:
            walkedrel = walked.relative_to(rootwalked)
        except ValueError:
            walkedrel = None

            for parent in walked.parents:
                try:
                    if parent.resolve() == rootresolved:
                        walkedrel = walked.relative_to(parent)

                        break
                except OSError:
                    continue

            if walkedrel is None:
                walkedrel = relresolved

        salt = walkedrel.as_posix().lower()

    st: os.stat_result | None = None

    try:
        st = p.stat()

        if fresh(INDEX.get(abskey), st):
            hashes = INDEX[abskey].get('hashes')

            if isinstance(hashes, dict):
                cached = hashes.get(salt)

                if isinstance(cached, str):
                    return cached
    except OSError:
        pass

    observed = time.time_ns()
    raw = p.read_bytes()
    data = content(raw) if p.suffix.lower() == '.md' else raw
    h = hashlib.sha256()
    h.update(data)
    h.update(b'\x00')
    h.update(salt.encode())
    digest = h.hexdigest()

    if st is not None:
        item = entry(abskey, st, observed)
        hashes = item.get('hashes')

        if not isinstance(hashes, dict):
            hashes = {}
            item['hashes'] = hashes

        hashes[salt] = digest
        item.pop('hash', None)
        DIRTY = True

    return digest


def count(path: Path, root: Path, compute: Callable[[Path], int], cache: Path | None = None) -> int:
    global DIRTY

    p = normpath(Path(path))
    root = normpath(Path(root))
    ensure(root, cache=cache)
    abskey = str(p.resolve())
    st: os.stat_result | None = None

    try:
        st = p.stat()
        item = INDEX.get(abskey)

        if fresh(item, st) and 'word_count' in item:
            return item['word_count']
    except OSError:
        pass

    observed = time.time_ns()
    wc = compute(Path(path))

    if st is not None:
        entry(abskey, st, observed)['word_count'] = wc
        DIRTY = True

    return wc


def relativizesources(payload: dict, root: Path) -> None:
    try:
        rootresolved = Path(root).resolve()
    except OSError:
        return

    for bucket in ('nodes', 'edges', 'hyperedges', 'raw_calls'):
        for item in payload.get(bucket, []):
            if not isinstance(item, dict):
                continue

            for key in ('source_file', 'definition_file'):
                source = item.get(key)

                if not source:
                    continue

                sp = Path(source)

                if not sp.is_absolute():
                    cwdform = Path(os.path.abspath(sp))

                    try:
                        if cwdform == rootresolved / sp or not cwdform.exists():
                            continue
                    except OSError:
                        continue

                    sp = cwdform

                try:
                    rel = os.path.relpath(sp, rootresolved)
                except (ValueError, OSError):
                    continue

                if rel == '..' or rel.startswith('..' + os.sep) or rel.startswith('../'):
                    continue

                item[key] = rel.replace(os.sep, '/')


def normsource(src: str | Path, rootresolved: Path) -> str:
    s = str(src).replace('\\', '/')
    p = Path(s)

    if not p.is_absolute():
        return s

    try:
        rel = os.path.relpath(p, rootresolved)
    except (ValueError, OSError):
        return s

    if rel == '..' or rel.startswith('..' + os.sep) or rel.startswith('../'):
        return s

    return rel.replace(os.sep, '/')


def matches(result: dict, path: Path, root: Path) -> bool:
    expected = normpath(Path(os.path.abspath(path)))

    for bucket in ('nodes', 'edges', 'hyperedges'):
        for item in result.get(bucket, []):
            if not isinstance(item, dict):
                continue

            source = item.get('source_file')

            if not source:
                continue

            sp = Path(source)

            if not sp.is_absolute():
                sp = Path(root) / sp

            if normpath(Path(os.path.abspath(sp))) != expected:
                return False

    return True


MARKER = '$graphify-root$'


def anchor(pathstr: str, relstr: str) -> str:
    from graphify.ids import normalize

    full = normalize(pathstr)
    tail = normalize(relstr)

    if not tail or full == tail:
        return ''

    suffix = '_' + tail

    return full[:-len(suffix)] if full.endswith(suffix) else ''


def anchors(path: str | Path, root: str | Path) -> tuple[list[str], str, list[str], str]:
    from graphify.ids import normalize

    try:
        rootresolved = Path(root).resolve()
    except OSError:
        return ([], '', [], '')

    try:
        pathresolved = Path(path).resolve()
    except (OSError, RuntimeError):
        pathresolved = Path(path)

    try:
        rel = os.path.relpath(pathresolved, rootresolved)
    except (ValueError, OSError):
        rel = ''

    given = anchor(str(path), rel)
    fromres = anchor(str(pathresolved), rel)
    restore = next((a for a in (given, fromres, normalize(str(rootresolved))) if a), '')
    rootforms = (normalize(str(rootresolved)),)

    if Path(root).is_absolute():
        rootforms += (normalize(str(root)),)

    idanchors = sorted({a for a in (given, fromres, *rootforms) if a}, key=len, reverse=True)
    pathanchors = sorted({s for s in (str(rootresolved), str(root)) if Path(s).is_absolute()}, key=len, reverse=True)

    return (idanchors, restore, pathanchors, str(rootresolved))


def rewritetables(payload: object, fn: Callable[[str], str]) -> None:
    ft = payload.get('objc_field_types') if isinstance(payload, dict) else None
    tables = ft.get('tables') if isinstance(ft, dict) else None

    if isinstance(tables, dict):
        ft['tables'] = {fn(k) if isinstance(k, str) else k: v for k, v in tables.items()}


def rewritestrings(obj: object, fn: Callable[[str], str]) -> None:
    if isinstance(obj, dict):
        items: Iterable = obj.items()
    elif isinstance(obj, list):
        items = enumerate(obj)
    else:
        return

    for key, val in list(items):
        if isinstance(val, str):
            new = fn(val)

            if new != val:
                obj[key] = new
        else:
            rewritestrings(val, fn)


def relativizeids(payload: dict, path: str | Path, root: Path) -> None:
    idanchors, _, pathanchors, _ = anchors(path, root)

    if not idanchors and not pathanchors:
        return

    def hook(val: str) -> str:
        for a in pathanchors:
            if val == a:
                return MARKER

            for sep in ('/', '\\'):
                if val.startswith(a + sep):
                    return MARKER + '/' + val[len(a) + 1:].replace('\\', '/')

        for a in idanchors:
            if val.startswith(a + '_'):
                return MARKER + '_' + val[len(a) + 1:]

        return val

    rewritestrings(payload, hook)
    rewritetables(payload, hook)


def absolutizeids(payload: dict, path: str | Path, root: Path) -> None:
    _, restore, _, pathrestore = anchors(path, root)

    def hook(val: str) -> str:
        if not val.startswith(MARKER):
            return val

        rest = val[len(MARKER):]

        if not rest:
            return pathrestore

        if rest[0] == '/':
            tail = rest[1:]

            return str(Path(pathrestore) / tail) if tail else pathrestore

        if rest[0] == '_':
            return restore + rest if restore else rest[1:]

        return val

    rewritestrings(payload, hook)
    rewritetables(payload, hook)


def absolutizesources(payload: dict, root: Path) -> None:
    try:
        rootresolved = Path(root).resolve()
    except OSError:
        return

    for bucket in ('nodes', 'edges', 'hyperedges', 'raw_calls'):
        for item in payload.get(bucket, []):
            if not isinstance(item, dict):
                continue

            for key in ('source_file', 'definition_file'):
                source = item.get(key)

                if not source:
                    continue

                sp = Path(source)

                if sp.is_absolute():
                    continue

                try:
                    item[key] = str(rootresolved / sp)
                except (TypeError, OSError):
                    continue


def cachedir(root: Path = Path('.'), kind: str = 'ast', tagfp: str | None = None) -> Path:
    out = Path(_GRAPHIFY_OUT)
    base = out if out.is_absolute() else Path(root).resolve() / out
    d = base / 'cache' / kind

    if kind == 'ast':
        d = d / f'v{VERSION}-s{SCHEMA}'
        clean(d.parent, d)
    elif tagfp:
        d = d / f'p{tagfp}'

    d.mkdir(parents=True, exist_ok=True)

    return d


def load(path: Path, root: Path = Path('.'), kind: str = 'ast', cache: Path | None = None, tag: str | Path | None = None, tagfile: str | Path | None = None, allowlegacy: bool = True, allowpartial: bool = False) -> dict | None:
    global LEGACY, CORRUPT

    loc = cache if cache is not None else root

    try:
        h = hash(path, root, cache=cache)
    except OSError:
        return None

    tagfp = resolve(tag, tagfile)
    p = cachedir(loc, kind, tagfp) / f'{h}.json'
    legacyhit = False

    if tagfp and not p.exists() and allowlegacy:
        legacypath = cachedir(loc, kind) / f'{h}.json'

        if legacypath.exists():
            p, legacyhit = legacypath, True

    if p.exists():
        try:
            result = json.loads(p.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            CORRUPT += 1

            return None
        except OSError:
            return None

        if not allowpartial and isinstance(result, dict) and result.get('partial'):
            return None

        if not allowpartial and kind.startswith('semantic') and isinstance(result, dict) and not result.get('nodes') and not result.get('hyperedges'):
            return None

        if kind.startswith('semantic') and isinstance(result, dict) and not matches(result, Path(path), Path(root)):
            return None

        if legacyhit:
            LEGACY += 1

        if isinstance(result, dict):
            absolutizesources(result, root)
            absolutizeids(result, path, root)
            targets = result.pop('_cached_target_files', None)

            if isinstance(targets, list) and not all(present(t, root) for t in targets if isinstance(t, str)):
                return None

        return result

    return None


def present(target: str, root: Path) -> bool:
    cand = Path(target)

    if not cand.is_absolute():
        cand = Path(root) / cand

    try:
        return cand.is_file()
    except (OSError, ValueError):
        return False


def targets(result: dict, root: Path) -> list[str]:
    found: set[str] = set()

    for edge in result.get('edges', []) or []:
        if not isinstance(edge, dict):
            continue

        tgt = edge.get('target_file')

        if isinstance(tgt, str) and tgt and tgt not in found:
            if present(tgt, root):
                found.add(tgt)

    return sorted(found)


def save(path: Path, result: dict, root: Path = Path('.'), kind: str = 'ast', cache: Path | None = None, tag: str | Path | None = None, tagfile: str | Path | None = None) -> None:
    p = Path(path)

    if not p.is_file():
        return

    disk = result

    if isinstance(result, dict):
        import copy

        disk = copy.deepcopy(result)

        if kind == 'ast':
            cands = targets(disk, root)

            if cands:
                disk['_cached_target_files'] = cands

        relativizesources(disk, root)
        relativizeids(disk, p, root)

    h = hash(p, root, cache=cache)
    loc = cache if cache is not None else root
    targetdir = cachedir(loc, kind, resolve(tag, tagfile))
    entrypath = targetdir / f'{h}.json'
    fd, tmppath = tempfile.mkstemp(dir=targetdir, prefix=f'{h}.', suffix='.tmp')

    try:
        os.write(fd, json.dumps(disk).encode())
        os.close(fd)
        replace(tmppath, entrypath)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass

        try:
            os.unlink(tmppath)
        except OSError:
            pass

        raise


def files(root: Path = Path('.')) -> set[str]:
    base = Path(root).resolve() / _GRAPHIFY_OUT / 'cache'
    hashes: set[str] = set()

    if base.is_dir():
        hashes.update((p.stem for p in base.glob('*.json')))

    for kind in ('ast', 'semantic', 'semantic-deep'):
        d = base / kind

        if d.is_dir():
            hashes.update((p.stem for p in d.glob('**/*.json')))

    return hashes


def clear(root: Path = Path('.')) -> None:
    base = Path(root).resolve() / _GRAPHIFY_OUT / 'cache'

    if base.is_dir():
        for f in base.glob('*.json'):
            f.unlink()

    for kind in ('ast', 'semantic', 'semantic-deep'):
        d = base / kind

        if d.is_dir():
            for f in d.glob('**/*.json'):
                f.unlink()


def prune(root: Path, hashes: set[str]) -> int:
    out = Path(_GRAPHIFY_OUT)
    base = out if out.is_absolute() else Path(root).resolve() / out
    pruned = 0

    for kind in ('semantic', 'semantic-deep'):
        d = base / 'cache' / kind

        if not d.is_dir():
            continue

        for p in d.glob('**/*.json'):
            if p.stem in hashes:
                continue

            try:
                p.unlink()
                pruned += 1
            except OSError:
                pass

    return pruned


def check(items: list[str], root: Path = Path('.'), mode: str | None = None, tag: str | Path | None = None, tagfile: str | Path | None = None, cache: Path | None = None) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    global LEGACY

    kind = 'semantic' if mode is None else f'semantic-{mode}'
    nodes: list[dict] = []
    edges: list[dict] = []
    hyper: list[dict] = []
    uncached: list[str] = []
    beforelegacy = LEGACY
    beforecorrupt = CORRUPT

    for fpath in items:
        p = Path(fpath)

        if not p.is_absolute():
            p = Path(root) / p

        result = load(p, root, kind=kind, cache=cache, tag=tag, tagfile=tagfile)

        if result is not None:
            nodes.extend(result.get('nodes', []))
            edges.extend(result.get('edges', []))
            hyper.extend(result.get('hyperedges', []))
        else:
            uncached.append(fpath)

    legacycount = LEGACY - beforelegacy

    if legacycount:
        warnings.warn(f"{legacycount} semantic cache entr{('y' if legacycount == 1 else 'ies')} predate extraction-tag fingerprinting and were written by an unknown tag version; they were replayed as-is, so this graph may mix extraction vintages. Re-run with --force (or GRAPHIFY_FORCE=1) to re-extract them with the current tag (#1939).", RuntimeWarning, stacklevel=2)

    corruptcount = CORRUPT - beforecorrupt

    if corruptcount:
        warnings.warn(f"{corruptcount} semantic cache entr{('y' if corruptcount == 1 else 'ies')} could not be parsed as JSON and were treated as misses, so those files were re-extracted. A corrupt entry stays on disk and fails again every run; run with --force (or GRAPHIFY_FORCE=1) to rewrite them, or clear the cache to stop paying for the re-extraction (#2405).", RuntimeWarning, stacklevel=2)

    return (nodes, edges, hyper, uncached)


def partial(group: dict) -> bool:
    for bucket in ('nodes', 'edges', 'hyperedges'):
        for item in group.get(bucket, []):
            if isinstance(item, dict) and item.get('_partial'):
                return True

    return False


def matcher(root: Path) -> tuple[Callable[[str | Path], Path], Callable[[str | Path], str]]:
    walked = normpath(Path(os.path.abspath(root)))
    resolved = normpath(Path(root).resolve())

    def norm(src: str | Path) -> str:
        res = normsource(src, walked)

        if Path(res).is_absolute() and walked != resolved:
            res = normsource(src, resolved)

        return res

    def ident(val: str | Path) -> Path:
        p = Path(val)

        if not p.is_absolute():
            p = walked / p
        elif walked != resolved:
            normalized = normpath(Path(os.path.abspath(p)))

            try:
                rel = normalized.relative_to(resolved)
            except ValueError:
                pass
            else:
                p = walked / rel

        return normpath(Path(os.path.abspath(p)))

    return (ident, norm)


def savesemantic(nodes: list[dict], edges: list[dict], hyper: list[dict] | None = None, root: Path = Path('.'), merge: bool = False, allowed: Iterable[str | Path] | None = None, mode: str | None = None, tag: str | Path | None = None, tagfile: str | Path | None = None, parts: Iterable[str | Path] | None = None, cache: Path | None = None) -> int:
    from collections import defaultdict

    kind = 'semantic' if mode is None else f'semantic-{mode}'
    sourcepath, normval = matcher(root)

    def normalized(item: dict) -> dict:
        src = item.get('source_file')

        if not src:
            return item

        val = normval(src)

        if val != src:
            item = {**item, 'source_file': val}

        return item

    byfile: dict[str, dict] = defaultdict(lambda: {'nodes': [], 'edges': [], 'hyperedges': []})

    for n in nodes:
        n = normalized(n)
        src = n.get('source_file', '')

        if src:
            byfile[src]['nodes'].append(n)

    for e in edges:
        e = normalized(e)
        src = e.get('source_file', '')

        if src:
            byfile[src]['edges'].append(e)

    for h in hyper or []:
        h = normalized(h)
        src = h.get('source_file', '')

        if src:
            byfile[src]['hyperedges'].append(h)

    def respath(val: str | Path) -> Path:
        p = sourcepath(val)

        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return Path(os.path.abspath(p))

    allowedpaths = None

    if allowed is not None:
        allowedpaths = {sourcepath(p) for p in allowed}

    def recover(fpath: str) -> tuple[Path, Path]:
        cpath = sourcepath(fpath)
        rpath = respath(fpath)

        if not rpath.is_file() and allowedpaths is not None:
            cands = [ap for ap in allowedpaths if ap.name == cpath.name]

            if len(cands) == 1:
                cpath = cands[0]
                rpath = cands[0]

        return (cpath, rpath)

    partpaths = None

    if parts is not None:
        partpaths = {sourcepath(p) for p in parts}
        presentkeys = {sourcepath(k) for k in byfile}

        for pp in partpaths:
            if pp not in presentkeys:
                byfile[str(pp)]

    def skipped(fpath: str) -> bool:
        cpath, p = recover(fpath)

        return not p.is_file() or (allowedpaths is not None and cpath not in allowedpaths)

    if allowedpaths is not None:
        skippedids: set = set()
        writtenids: set = set()

        for fpath, result in byfile.items():
            target = skippedids if skipped(fpath) else writtenids

            for n in result['nodes']:
                nid = n.get('id')

                if nid is None:
                    continue

                try:
                    hash(nid)
                except TypeError:
                    continue

                target.add(nid)

        skippedids -= writtenids

        if skippedids:

            def edgedangles(e: dict) -> bool:
                try:
                    return e.get('source') in skippedids or e.get('target') in skippedids
                except TypeError:
                    return False

            def hyperdangles(h: dict) -> bool:
                try:
                    return bool(skippedids & set(h.get('nodes') or []))
                except TypeError:
                    return False

            for fpath, result in byfile.items():
                if skipped(fpath):
                    continue

                result['edges'] = [e for e in result['edges'] if not edgedangles(e)]
                result['hyperedges'] = [h for h in result['hyperedges'] if not hyperdangles(h)]

    saved = 0
    skippednotfile = 0

    for fpath, result in byfile.items():
        cpath, p = recover(fpath)

        if p.is_file():
            if cpath != sourcepath(fpath):
                corrected = normval(str(cpath))
                result = {**result, 'nodes': [{**n, 'source_file': corrected} for n in result['nodes']], 'edges': [{**e, 'source_file': corrected} for e in result['edges']], 'hyperedges': [{**h, 'source_file': corrected} for h in result['hyperedges']]}

            if allowedpaths is not None and cpath not in allowedpaths:
                warnings.warn(f'semantic cache skipped out-of-scope source_file {fpath!r}; the file was not dispatched for extraction', RuntimeWarning, stacklevel=2)

                continue

            if merge:
                prev = load(cpath, root, kind=kind, cache=cache, tag=tag, tagfile=tagfile, allowlegacy=False, allowpartial=True)
                prevpartial = bool(prev.get('partial')) if prev else False

                if prev:
                    result = {'nodes': (prev.get('nodes', []) or []) + result['nodes'], 'edges': (prev.get('edges', []) or []) + result['edges'], 'hyperedges': (prev.get('hyperedges', []) or []) + result['hyperedges']}
            else:
                prevpartial = False

            ispart = partpaths is not None and cpath in partpaths or partial(result) or prevpartial

            if ispart:
                result = {**result, 'partial': True}

            if not ispart and not (result.get('nodes') or result.get('hyperedges')):
                continue

            save(cpath, result, root, kind=kind, cache=cache, tag=tag, tagfile=tagfile)
            saved += 1
        else:
            skippednotfile += 1

    if skippednotfile and skippednotfile == len(byfile):
        warnings.warn(f'save_semantic_cache: all {skippednotfile} source_file group(s) were skipped because their paths do not resolve to real files. This usually means ``root`` is anchored to the wrong directory (e.g. the --out directory instead of the corpus root). Pass the corpus directory as ``root`` and the output directory as ``cache_root`` (#1991).', RuntimeWarning, stacklevel=2)

    return saved


def scope(result: dict, root: Path = Path('.'), allowed: Iterable[str | Path] | None = None) -> tuple[set[str], int]:
    if allowed is None:
        return (set(), 0)

    sourcepath, normval = matcher(root)

    def ident(item: dict) -> tuple[str | None, Path | None]:
        src = item.get('source_file')

        if not src:
            return (None, None)

        norm = normval(src)

        return (norm, sourcepath(norm))

    allowedpaths = {sourcepath(str(p)) for p in allowed}

    def canhash(val) -> bool:
        try:
            hash(val)
        except TypeError:
            return False

        return True

    droppedfiles: set[str] = set()
    droppeditems = 0
    droppedids: set = set()
    keptids: set = set()

    for bucket in ('nodes', 'edges', 'hyperedges'):
        kept: list[dict] = []

        for item in result.get(bucket) or []:
            display, loc = ident(item)

            if loc is not None and loc not in allowedpaths:
                droppedfiles.add(display)
                droppeditems += 1

                if bucket == 'nodes' and item.get('id') is not None:
                    nid = item['id']

                    if canhash(nid):
                        droppedids.add(nid)

                continue

            if bucket == 'nodes' and item.get('id') is not None and canhash(item['id']):
                keptids.add(item['id'])

            kept.append(item)

        result[bucket] = kept

    droppedids -= keptids

    if droppedids:

        def edgedangles(e: dict) -> bool:
            try:
                return e.get('source') in droppedids or e.get('target') in droppedids
            except TypeError:
                return False

        def hyperdangles(h: dict) -> bool:
            try:
                return bool(droppedids & set(h.get('nodes') or []))
            except TypeError:
                return False

        result['edges'] = [e for e in result.get('edges') or [] if not edgedangles(e)]
        result['hyperedges'] = [h for h in result.get('hyperedges') or [] if not hyperdangles(h)]

    return (droppedfiles, droppeditems)