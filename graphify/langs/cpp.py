from __future__ import annotations

import re
from pathlib import Path

from ..src.base import read, make, bytes as readbytes, text, stem as filestem
from ..src.models import LanguageConfig
from ..src.engine import generic, cppfunc
from .c import include

macros = ('TEST_CASE', 'TEST_CASE_TEMPLATE', 'SCENARIO')

pattern = re.compile('^[ \\t]*(?:' + '|'.join(macros) + ')\\s*\\(\\s*"((?:[^"\\\\]|\\\\.)+)"', re.MULTILINE)


def augment(path: Path, result: dict) -> dict:
    try:
        source = text(path, warn=False)
    except OSError:
        return result

    matches = list(pattern.finditer(source))

    if not matches:
        return result

    target = str(path)
    stem = filestem(path)
    root = make(target)
    base = make(stem)

    nodes = result.setdefault('nodes', [])
    edges = result.setdefault('edges', [])
    seen = {n.get('id') for n in nodes}

    for m in matches:
        name = m.group(1)
        line = source.count('\n', 0, m.start()) + 1
        item = make(stem, name)

        if item == base:
            item = make(stem, 'test', f'L{line}')

        if item in seen:
            continue

        seen.add(item)
        nodes.append({'id': item, 'label': f'"{name}"', 'file_type': 'code', 'source_file': target, 'source_location': f'L{line}'})
        edges.append({'source': root, 'target': item, 'relation': 'contains', 'source_file': target, 'source_location': f'L{line}'})

    return result


mark = re.compile(b'\\b(?:ref|value)\\s+(?:class|struct)\\b|\\binterface\\s+class\\b|\\bgcnew\\b|\\[\\s*(?:assembly|module)\\s*:')

cls_re = re.compile(b'(?:\\b(?:public|private|protected)\\s+)?\\b(?:ref|value)\\s+(?=(?:class|struct)\\b)|(?:\\b(?:public|private|protected)\\s+)?\\binterface\\s+(?=class\\b)')

suffix_re = re.compile(b'(?<=[A-Za-z0-9_>])[\\^%](?=\\s*[,)\\]>;])')

decl_re = re.compile(b'(\\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+|\\b[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*|\\b[A-Z](?=[\\^%])|\\b(?:bool|char|wchar_t|short|int|long|float|double|unsigned|signed)|>)[\\^%](?=\\s+[A-Za-z_])')

attr_re = re.compile(b'\\[\\s*(?:assembly|module)\\s*:[^\\[\\]]*\\]', re.S)


def blank(m: re.Match[bytes]) -> bytes:
    return re.sub(b'[^\\r\\n]', b' ', m.group(0))


def cli(source: bytes) -> bytes | None:
    if not mark.search(source):
        return None

    out = cls_re.sub(blank, source)
    out = re.sub(b'\\bgcnew\\b', b'new  ', out)
    out = suffix_re.sub(b' ', out)
    out = decl_re.sub(b'\\1 ', out)

    return attr_re.sub(blank, out)


export_re = re.compile(b'(\\b(?:class|struct)[ \\t\\r\\n]+)((?:[A-Z][A-Z0-9_]*[ \\t\\r\\n]+)+)(?!final[ \\t\\r\\n]*[:{])([A-Za-z_][A-Za-z0-9_]*(?:[ \\t\\r\\n]+final)?[ \\t\\r\\n]*)([:{])')


def exports(source: bytes) -> bytes:
    def repl(m: re.Match[bytes]) -> bytes:
        start = m.start()
        idx = start - 1

        while idx >= 0 and source[idx] in b' \\t\\r\\n':
            idx -= 1

        if idx >= 0 and source[idx] == ord(b'('):
            return m.group(0)

        if m.group(4) == b'{':
            pos = m.end()

            while pos < len(source) and source[pos] in b' \\t\\r\\n':
                pos += 1

            if pos < len(source) and source[pos:pos + 1] in b"0123456789\"'-":
                return m.group(0)

        macro = bytearray(m.group(2))

        for i in range(len(macro)):
            if macro[i] not in b'\\r\\n':
                macro[i] = ord(b' ')

        return m.group(1) + bytes(macro) + m.group(3) + m.group(4)

    return export_re.sub(repl, source)


config = LanguageConfig(
    ts_module='tree_sitter_cpp',
    class_types=frozenset({'class_specifier', 'struct_specifier', 'enum_specifier', 'union_specifier'}),
    function_types=frozenset({'function_definition'}),
    import_types=frozenset({'preproc_include'}),
    call_types=frozenset({'call_expression'}),
    call_function_field='function',
    call_accessor_node_types=frozenset({'field_expression', 'qualified_identifier'}),
    call_accessor_field='field',
    function_boundary_types=frozenset({'function_definition'}),
    import_handler=include,
    resolve_function_name_fn=cppfunc,
)


def extract(path: Path) -> dict:
    try:
        source = readbytes(path)
    except OSError:
        return augment(path, generic(path, config))

    clean = cli(source) or source
    clean = exports(clean)

    res = generic(path, config, source_override=clean)

    return augment(path, res)