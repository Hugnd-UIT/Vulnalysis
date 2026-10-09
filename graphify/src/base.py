from __future__ import annotations

from pathlib import Path
import sys

from graphify.ids import make as ids_make


GLOBALS: frozenset[str] = frozenset({'String', 'Number', 'Boolean', 'Object', 'Array', 'Symbol', 'BigInt', 'Date', 'RegExp', 'Error', 'TypeError', 'RangeError', 'SyntaxError', 'ReferenceError', 'EvalError', 'URIError', 'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet', 'JSON', 'Math', 'Reflect', 'Proxy', 'Intl', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent', 'decodeURIComponent', 'encodeURI', 'decodeURI', 'URL', 'URLSearchParams', 'FormData', 'Blob', 'File', 'Headers', 'Request', 'Response', 'AbortController', 'AbortSignal', 'TextEncoder', 'TextDecoder', 'console', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'bytes', 'len', 'range', 'enumerate', 'zip', 'map', 'filter', 'sum', 'min', 'max', 'print', 'open', 'isinstance', 'type', 'super', 'sorted', 'reversed', 'any', 'all', 'abs', 'round', 'next', 'iter', 'hash', 'id', 'repr', 'callable', 'getattr', 'setattr', 'hasattr', 'delattr', 'vars', 'dir', 'Int', 'Int8', 'Int16', 'Int32', 'Int64', 'UInt', 'UInt8', 'UInt16', 'UInt32', 'UInt64', 'Double', 'Float', 'Bool', 'Character', 'Sendable', 'Codable', 'Decodable', 'Encodable', 'Equatable', 'Hashable', 'Identifiable', 'Comparable', 'CaseIterable', 'RawRepresentable', 'CustomStringConvertible', 'CustomDebugStringConvertible', 'AnyObject', 'LocalizedError', 'Data', 'UUID', 'Decimal', 'Calendar', 'Locale', 'TimeZone', 'Bundle', 'IndexPath', 'IndexSet', 'NotificationCenter', 'UserDefaults', 'FileManager', 'URLSession', 'URLRequest', 'URLComponents', 'JSONDecoder', 'JSONEncoder', 'DateFormatter', 'NumberFormatter', 'ISO8601DateFormatter', 'NSObject', 'NSString', 'NSError', 'NSLock', 'NSAttributedString', 'DispatchQueue', 'DispatchGroup', 'OperationQueue', 'RunLoop', 'View', 'Color', 'Font'})

BOMS32 = (b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')
BOMS16 = (b'\xff\xfe', b'\xfe\xff')
FALLBACKS = ('cp1252', 'latin-1')
WARNED: set[str] = set()


def make(*parts: str) -> str:
    return ids_make(*parts)


def stem(loc: Path) -> str:
    if not loc.name:
        return ''

    return loc.with_suffix('').as_posix()


def read(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode('utf-8', errors='replace')


def bytes(loc: Path, *, warn: bool = True) -> bytes:
    raw = loc.read_bytes()

    if raw.isascii():
        return raw

    try:
        raw.decode('utf-8')

        return raw
    except UnicodeDecodeError:
        pass

    for boms, enc in ((BOMS32, 'utf-32'), (BOMS16, 'utf-16')):
        if raw.startswith(boms):
            try:
                return raw.decode(enc).encode('utf-8')
            except UnicodeDecodeError:
                continue

    for enc in FALLBACKS:
        try:
            body = raw.decode(enc)
        except UnicodeDecodeError:
            continue

        key = str(loc)

        if warn and key not in WARNED:
            WARNED.add(key)
            print(f'[graphify] warning: {loc} read as {enc}', file=sys.stderr)

        return body.encode('utf-8')

    return raw


def text(loc: Path, *, warn: bool = True) -> str:
    body = bytes(loc, warn=warn).decode('utf-8')

    return body.replace('\r\n', '\n').replace('\r', '\n')