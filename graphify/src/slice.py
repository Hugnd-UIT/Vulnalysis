from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from graphify.src.base import text as read_text


SPLITTABLE = frozenset({'.md', '.mdx', '.markdown', '.txt', '.rst', '.qmd', '.skill', '.html', '.yaml', '.yml'})
CONVERTED = frozenset({'.pdf'})
CACHE: dict[tuple, str] = {}
LIMIT = 64
SEPARATORS = ('\n#', '\n\n', '\n')


def pdf(path: Path) -> str:
    from graphify.detect import extract_pdf_text

    return extract_pdf_text(path)


def text(loc: Path) -> str:
    if loc.suffix.lower() not in CONVERTED:
        return read_text(loc, warn=False)

    try:
        st = loc.stat()
        key = (str(loc), st.st_size, st.st_mtime_ns)
    except OSError:
        return pdf(loc)

    hit = CACHE.get(key)

    if hit is not None:
        return hit

    content = pdf(loc)

    if len(CACHE) >= LIMIT:
        CACHE.clear()

    CACHE[key] = content

    return content


@dataclass(frozen=True)


class Slice:
    path: Path
    start: int
    end: int
    index: int
    total: int


Unit = 'Path | Slice'


def path(unit: Unit) -> Path:
    return unit.path if isinstance(unit, Slice) else unit


def splittable(loc: Path) -> bool:
    ext = loc.suffix.lower()

    return ext in SPLITTABLE or ext in CONVERTED


def cut(body: str, start: int, end: int) -> int:
    span = body[start:end]

    for sep in SEPARATORS:
        pos = span.rfind(sep)

        if pos > 0:
            if sep == '\n#':
                return start + pos + 1

            return start + pos + len(sep)

    return end


def boundaries(body: str, cap: int) -> list[tuple[int, int]]:
    n = len(body)

    if n <= cap:
        return [(0, n)]

    bounds: list[tuple[int, int]] = []
    pos = 0

    while pos < n:
        hard = min(pos + cap, n)
        end = cut(body, pos, hard) if hard < n else n

        if end <= pos:
            end = hard

        bounds.append((pos, end))
        pos = end

    return bounds


def expand(files: list[Path], cap: int) -> list[Unit]:
    out: list[Unit] = []

    for f in files:
        if not splittable(f):
            out.append(f)
            continue

        try:
            body = text(f)
        except OSError:
            out.append(f)
            continue

        if len(body) <= cap:
            out.append(f)
            continue

        ranges = boundaries(body, cap)
        total = len(ranges)

        for i, (s, e) in enumerate(ranges):
            out.append(Slice(path=f, start=s, end=e, index=i, total=total))

    return out


def read(item: Slice) -> str:
    return text(item.path)[item.start:item.end]


def bisect(item: Slice) -> tuple[Slice, Slice] | None:
    if item.end - item.start <= 1:
        return None

    try:
        body = text(item.path)
    except Exception:
        return None

    mid = (item.start + item.end) // 2
    nl = body.find('\n', mid, item.end)
    split = nl + 1 if nl != -1 and item.start < nl + 1 < item.end else mid

    if not item.start < split < item.end:
        return None

    left = Slice(item.path, item.start, split, item.index, item.total)
    right = Slice(item.path, split, item.end, item.index, item.total)

    return (left, right)