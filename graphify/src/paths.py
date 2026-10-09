from __future__ import annotations

import functools
import json as jsonlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tempfile


GRAPHIFY_OUT = os.environ.get('GRAPHIFY_OUT', 'graphify-out')


def replace(src: 'str | Path', dst: 'str | Path') -> None:
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        if not isinstance(exc, PermissionError) and getattr(exc, 'winerror', None) != 17:
            raise

    import shutil
    dst = os.fspath(dst)

    if os.path.normcase(os.path.abspath(os.fspath(src))) == os.path.normcase(os.path.abspath(dst)):
        return

    dst_dir = os.path.dirname(dst) or '.'
    fd, tmp_copy = tempfile.mkstemp(dir=dst_dir, prefix='.gfy-replace-', suffix='.tmp')
    os.close(fd)

    try:
        shutil.copy2(src, tmp_copy)
        backup = None

        if os.path.lexists(dst):
            bfd, backup = tempfile.mkstemp(dir=dst_dir, prefix='.gfy-replace-bak-', suffix='.tmp')
            os.close(bfd)
            os.unlink(backup)
            os.rename(dst, backup)

        try:
            os.rename(tmp_copy, dst)
        except BaseException:
            if backup is not None:
                try:
                    os.rename(backup, dst)
                except OSError:
                    pass
            raise

        if backup is not None:
            try:
                os.unlink(backup)
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp_copy)
        except OSError:
            pass
        raise

    os.unlink(src)


def atomic(loc: 'str | Path', writer) -> None:
    real = Path(os.path.realpath(str(loc)))
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix='.gfy-', suffix='.tmp')

    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            writer(f)

        try:
            mode = stat.S_IMODE(os.stat(real).st_mode)
        except OSError:
            mask = os.umask(0)
            os.umask(mask)
            mode = 438 & ~mask

        try:
            os.chmod(tmp, mode)
        except OSError:
            pass

        replace(tmp, str(real))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            try:
                os.chmod(tmp, stat.S_IWRITE)
                os.unlink(tmp)
            except OSError:
                pass
        raise


def text(loc: 'str | Path', body: str) -> None:
    atomic(loc, lambda f: f.write(body))


def touch(loc: 'str | Path', body: str) -> bool:
    try:
        if Path(os.path.realpath(str(loc))).read_text(encoding='utf-8') == body:
            return False
    except (OSError, UnicodeDecodeError):
        pass

    text(loc, body)

    return True


def json(loc: 'str | Path', obj, *, indent: 'int | None' = None, ensure_ascii: bool = True) -> None:
    atomic(loc, lambda f: jsonlib.dump(obj, f, indent=indent, ensure_ascii=ensure_ascii))


TEST_DIRS = frozenset({'tests', 'test', 'spec', 'specs', '__tests__'})
TEST_RES = (
    re.compile(r'^test_.*', re.IGNORECASE),
    re.compile(r'.*_test\..+$', re.IGNORECASE),
    re.compile(r'.*\.test\..+$', re.IGNORECASE),
    re.compile(r'.*\.spec\..+$', re.IGNORECASE),
    re.compile(r'.*_spec\..+$', re.IGNORECASE),
    re.compile(r'.*\.tests\.ps1$', re.IGNORECASE),
    re.compile(r'.*Test\.java$'),
    re.compile(r'.*Tests\.java$'),
    re.compile(r'.*Tests\.cs$'),
)


def istest(loc: str) -> bool:
    if not loc:
        return False

    norm = str(loc).replace('\\', '/')
    pure = PurePosixPath(norm)

    for seg in pure.parts:
        if seg.lower() in TEST_DIRS:
            return True

    name = pure.name

    if not name:
        return False

    for pat in TEST_RES:
        if pat.match(name):
            return True

    return False


@functools.lru_cache(maxsize=65536)


def parent(loc: str) -> tuple[str, ...]:
    return PurePosixPath(loc.replace('\\', '/')).parent.parts


def proximity(site: str, candidates: dict[str, str]) -> str | None:
    if not site:
        return None

    site_norm = str(site).replace('\\', '/')
    site_parts = parent(site_norm)
    same_file = [cid for cid, f in candidates.items() if str(f).replace('\\', '/') == site_norm]

    if len(same_file) == 1:
        return same_file[0]

    if len(same_file) > 1:
        return None

    same_dir = [cid for cid, f in candidates.items() if parent(str(f)) == site_parts]

    if len(same_dir) == 1:
        return same_dir[0]

    if len(same_dir) > 1:
        return None

    def prefix(f: str) -> int:
        parts = parent(str(f))
        n = 0

        for a, b in zip(site_parts, parts):
            if a != b:
                break

            n += 1

        return n

    scored = sorted(((cid, prefix(f)) for cid, f in candidates.items()), key=lambda kv: kv[1], reverse=True)

    if not scored:
        return None

    best = scored[0][1]
    winners = [cid for cid, score in scored if score == best]

    if len(winners) == 1 and best > 0:
        return winners[0]

    return None


def disambiguate(candidates: list[str], candidate_files: dict[str, str], call_site: str) -> str | None:
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    site_test = istest(call_site)
    test_cands = [c for c in candidates if istest(candidate_files.get(c, ''))]
    main_cands = [c for c in candidates if c not in set(test_cands)]

    if site_test:
        norm = str(call_site).replace('\\', '/')
        same = [c for c in test_cands if str(candidate_files.get(c, '')).replace('\\', '/') == norm]

        if len(same) == 1:
            return same[0]

        survivors = test_cands if test_cands else (main_cands or candidates)
    else:
        survivors = main_cands

    if len(survivors) == 1:
        return survivors[0]

    if not survivors:
        return None

    return proximity(call_site, {c: candidate_files.get(c, '') for c in survivors})


GRAPHIFY_OUT_NAME = os.path.basename(os.path.normpath(GRAPHIFY_OUT))


def out(*parts: str) -> Path:
    return Path(GRAPHIFY_OUT, *parts)


def default() -> str:
    return str(out('graph.json'))


def absolute(p: 'str | Path | None') -> bool:
    if not p:
        return False

    s = str(p)

    return PurePosixPath(s).is_absolute() or PureWindowsPath(s).is_absolute()


def budget(dest: 'str | Path', *, reserve: int = 0, limit: int = 200) -> int:
    if os.name != 'nt':
        return limit

    try:
        base = os.path.abspath(str(dest))
    except (OSError, ValueError):
        return limit

    if base.startswith('\\\\?\\'):
        return limit

    room = 260 - 1 - len(base) - len(os.sep) - reserve - len('.md')

    return max(16, min(limit, room))


def nfc(s: str) -> str:
    import unicodedata

    return unicodedata.normalize('NFC', s)


def restore(data: dict) -> dict:
    links = data.get('links')

    if not isinstance(links, list):
        return data

    return dict(data, links=[{**link, '_src': link.get('_src', link.get('source')), '_tgt': link.get('_tgt', link.get('target'))} if isinstance(link, dict) else link for link in links])


def load(source):
    from networkx.readwrite import json_graph
    data = source

    if not isinstance(data, dict):
        p = Path(data)
        from graphify.security import check
        check(p)
        data = jsonlib.loads(p.read_text(encoding='utf-8'))

    if isinstance(data, dict) and 'links' not in data and 'edges' in data:
        data = dict(data, links=data['edges'])

    if isinstance(data, dict):
        data = restore(data)

    try:
        return json_graph.node_link_graph(data, edges='links')
    except TypeError:
        return json_graph.node_link_graph(data)