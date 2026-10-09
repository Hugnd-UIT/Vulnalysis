from __future__ import annotations
import hashlib
import importlib
import json
from . import base
from .base import GLOBALS, stem, make, read
from graphify.ids import normalize
from .models import LanguageConfig
from .resolve import jstarget
from graphify.security import metadata
from pathlib import Path


def csnsid(dotted_name: str) -> str:
    digest = hashlib.sha1(dotted_name.encode('utf-8')).hexdigest()[:16]

    return f'csharp_namespace:{digest}'

JSXELEMENTS = frozenset({'jsx_opening_element', 'jsx_self_closing_element'})
CONTEXTS = frozenset({'field', 'parameter_type', 'return_type', 'generic_arg', 'attribute', 'value', 'type'})


def location(line: int | str | None) -> str | None:
    if line is None:
        return None

    if isinstance(line, str):
        return line if line.startswith('L') else f'L{line}'

    return f'L{line}'


def semantic(source: str, target: str, context: str, source_file: str, line: int | str | None) -> dict:
    if context not in CONTEXTS:
        raise ValueError(f'unknown reference context: {context}')

    return {'source': source, 'target': target, 'relation': 'references', 'context': context, 'source_file': source_file, 'source_location': location(line)}

PYCONTAINERS = frozenset({'list', 'dict', 'set', 'tuple', 'frozenset', 'type', 'List', 'Dict', 'Set', 'Tuple', 'FrozenSet', 'Type', 'Optional', 'Union', 'Sequence', 'Iterable', 'Mapping', 'MutableMapping', 'Iterator', 'Callable', 'Awaitable', 'AsyncIterable', 'AsyncIterator', 'Coroutine', 'Generator', 'AsyncGenerator', 'ContextManager', 'AsyncContextManager', 'Annotated', 'ClassVar', 'Final', 'Literal', 'Concatenate', 'ParamSpec', 'TypeVar', 'None', 'Ellipsis'})
PYNOISE = frozenset({'str', 'int', 'float', 'bool', 'bytes', 'bytearray', 'complex', 'object', 'True', 'False', 'MagicMock', 'Mock', 'AsyncMock', 'NonCallableMock', 'NonCallableMagicMock', 'PropertyMock', 'patch', 'sentinel'})
PYDECORATORS = frozenset({'property', 'staticmethod', 'classmethod', 'abstractmethod', 'abstractproperty', 'cached_property', 'wraps', 'lru_cache', 'cache', 'singledispatch', 'singledispatchmethod', 'total_ordering', 'contextmanager', 'asynccontextmanager', 'overload', 'override', 'final', 'no_type_check', 'runtime_checkable', 'dataclass'})


def pytypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t == 'type':
        for c in node.children:
            if c.is_named:
                pytypes(c, source, generic, out)
        return

    if t == 'identifier':
        name = read(node, source)

        if name and name not in PYCONTAINERS and (name not in PYNOISE):
            out.append((name, 'generic_arg' if generic else 'type'))
        return

    if t == 'attribute':
        tail = read(node, source).rsplit('.', 1)[-1]

        if tail and tail not in PYCONTAINERS and (tail not in PYNOISE):
            out.append((tail, 'generic_arg' if generic else 'type'))
        return

    if t == 'generic_type':
        for c in node.children:
            if c.type == 'identifier':
                container = read(c, source)

                if container and container not in PYCONTAINERS and (container not in PYNOISE):
                    out.append((container, 'generic_arg' if generic else 'type'))
            elif c.type == 'type_parameter':
                for sub in c.children:
                    if sub.is_named:
                        pytypes(sub, source, True, out)
        return

    if t == 'subscript':
        value = node.child_by_field_name('value')

        if value is not None:
            pytypes(value, source, generic, out)

        for c in node.children:
            if c is value or not c.is_named:
                continue

            pytypes(c, source, True, out)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                pytypes(c, source, generic, out)


def csinterfaces(root_node, source: bytes) -> set[str]:
    out: set[str] = set()
    stack = [root_node]

    while stack:
        n = stack.pop()

        if n.type == 'interface_declaration':
            name_node = n.child_by_field_name('name')

            if name_node is not None:
                text = read(name_node, source)

                if text:
                    out.add(text)
        stack.extend(n.children)

    return out


def csclassify(name: str, interface_names: set[str]) -> str:
    if name in interface_names:
        return 'implements'

    if len(name) >= 2 and name[0] == 'I' and name[1].isupper():
        return 'implements'

    return 'inherits'

CSPARAMSCOPE = frozenset({'class_declaration', 'interface_declaration', 'record_declaration', 'struct_declaration', 'method_declaration'})


def csparams(node, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    scope = node

    while scope is not None:
        if scope.type in CSPARAMSCOPE:
            for child in scope.children:
                if child.type != 'type_parameter_list':
                    continue

                for param in child.children:
                    if param.type == 'type_parameter':
                        name_node = next((sub for sub in param.children if sub.type == 'identifier'), None)

                        if name_node is not None:
                            name = read(name_node, source)

                            if name:
                                names.add(name)
                    elif param.type == 'identifier':
                        name = read(param, source)

                        if name:
                            names.add(name)
        scope = scope.parent

    return frozenset(names)


def cstypes(node, source: bytes, generic: bool, out: list[tuple[str, str, bool, str]], skip: frozenset[str] | None=None) -> None:
    if node is None:
        return

    if skip is None:
        skip = csparams(node, source)
    t = node.type

    if t == 'predefined_type':
        return

    if t == 'identifier':
        name = read(node, source)

        if name and name not in skip:
            out.append((name, 'generic_arg' if generic else 'type', False, ''))
        return

    if t == 'qualified_name':
        prefix, _, text = read(node, source).rpartition('.')
        text = text.split('<', 1)[0]

        if text and text not in skip:
            out.append((text, 'generic_arg' if generic else 'type', True, prefix))
        return

    if t == 'generic_name':
        name_child = node.child_by_field_name('name')

        if name_child is None:
            for sub in node.children:
                if sub.type == 'identifier':
                    name_child = sub
                    break

        if name_child is not None:
            qualified = name_child.type == 'qualified_name'
            prefix, _, name = read(name_child, source).rpartition('.')

            if name and name not in skip:
                out.append((name, 'generic_arg' if generic else 'type', qualified, prefix if qualified else ''))

        for sub in node.children:
            if sub.type == 'type_argument_list':
                for arg in sub.children:
                    if arg.is_named:
                        cstypes(arg, source, True, out, skip)
        return

    if t in ('nullable_type', 'array_type', 'pointer_type', 'ref_type'):
        for c in node.children:
            if c.is_named:
                cstypes(c, source, generic, out, skip)
        return

    if t == 'tuple_type':
        for el in node.children:
            if el.type == 'tuple_element':
                type_child = el.child_by_field_name('type')

                if type_child is not None:
                    cstypes(type_child, source, generic, out, skip)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                cstypes(c, source, generic, out, skip)


def csattributes(method_node, source: bytes) -> list[tuple[str, bool, str]]:
    names: list[tuple[str, bool, str]] = []
    skip = csparams(method_node, source)

    for child in method_node.children:
        if child.type != 'attribute_list':
            continue

        for attr in child.children:
            if attr.type != 'attribute':
                continue

            name_node = attr.child_by_field_name('name')

            if name_node is None:
                for sub in attr.children:
                    if sub.type in ('identifier', 'qualified_name'):
                        name_node = sub
                        break

            if name_node is not None:
                qualified = name_node.type == 'qualified_name'
                prefix, _, text = read(name_node, source).rpartition('.')

                if text and text not in skip:
                    names.append((text, qualified, prefix if qualified else ''))

    return names

JAVAPARAMSCOPE = frozenset({'class_declaration', 'interface_declaration', 'record_declaration', 'method_declaration', 'constructor_declaration'})


def javaparams(node, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    scope = node

    while scope is not None:
        if scope.type in JAVAPARAMSCOPE:
            params = scope.child_by_field_name('type_parameters')

            if params is not None:
                for param in params.children:
                    if param.type != 'type_parameter':
                        continue

                    name_node = next((child for child in param.children if child.type == 'type_identifier'), None)

                    if name_node is not None:
                        names.add(read(name_node, source))
        scope = scope.parent

    return frozenset(names)

JAVABUILTINS = frozenset({'Object', 'String', 'CharSequence', 'StringBuilder', 'StringBuffer', 'Number', 'Byte', 'Short', 'Integer', 'Long', 'Float', 'Double', 'Boolean', 'Character', 'Void', 'Class', 'Enum', 'Record', 'Math', 'System', 'Thread', 'Runnable', 'Comparable', 'Iterable', 'Cloneable', 'AutoCloseable', 'Appendable', 'Readable', 'Process', 'ProcessBuilder', 'Runtime', 'Package', 'ThreadLocal', 'InheritableThreadLocal', 'Throwable', 'Exception', 'RuntimeException', 'Error', 'IllegalArgumentException', 'IllegalStateException', 'NullPointerException', 'IndexOutOfBoundsException', 'ArrayIndexOutOfBoundsException', 'ClassCastException', 'NumberFormatException', 'ArithmeticException', 'UnsupportedOperationException', 'InterruptedException', 'CloneNotSupportedException', 'SecurityException', 'StackOverflowError', 'OutOfMemoryError', 'AssertionError', 'Collection', 'List', 'ArrayList', 'LinkedList', 'Vector', 'Stack', 'Set', 'HashSet', 'LinkedHashSet', 'TreeSet', 'SortedSet', 'NavigableSet', 'EnumSet', 'Map', 'HashMap', 'LinkedHashMap', 'TreeMap', 'SortedMap', 'NavigableMap', 'Hashtable', 'EnumMap', 'Properties', 'Queue', 'Deque', 'ArrayDeque', 'PriorityQueue', 'Iterator', 'ListIterator', 'Comparator', 'Optional', 'OptionalInt', 'OptionalLong', 'OptionalDouble', 'Collections', 'Arrays', 'Objects', 'Date', 'Calendar', 'Random', 'UUID', 'Scanner', 'StringJoiner', 'StringTokenizer', 'BitSet', 'Spliterator', 'Locale', 'NoSuchElementException', 'ConcurrentModificationException', 'Stream', 'IntStream', 'LongStream', 'DoubleStream', 'Collector', 'Collectors', 'Function', 'BiFunction', 'Consumer', 'BiConsumer', 'Supplier', 'Predicate', 'BiPredicate', 'UnaryOperator', 'BinaryOperator', 'IntFunction', 'ToIntFunction', 'ToLongFunction', 'ToDoubleFunction', 'Callable', 'Future', 'CompletableFuture', 'CompletionStage', 'Executor', 'ExecutorService', 'Executors', 'ScheduledExecutorService', 'TimeUnit', 'ConcurrentHashMap', 'ConcurrentMap', 'CopyOnWriteArrayList', 'BlockingQueue', 'CountDownLatch', 'Semaphore', 'CyclicBarrier', 'AtomicInteger', 'AtomicLong', 'AtomicBoolean', 'AtomicReference', 'Instant', 'Duration', 'Period', 'LocalDate', 'LocalTime', 'LocalDateTime', 'ZonedDateTime', 'OffsetDateTime', 'ZoneId', 'ZoneOffset', 'DayOfWeek', 'Month', 'Year', 'Clock', 'DateTimeFormatter', 'IOException', 'UncheckedIOException', 'FileNotFoundException', 'File', 'InputStream', 'OutputStream', 'Reader', 'Writer', 'BufferedReader', 'BufferedWriter', 'InputStreamReader', 'OutputStreamWriter', 'FileReader', 'FileWriter', 'PrintStream', 'PrintWriter', 'ByteArrayInputStream', 'ByteArrayOutputStream', 'Serializable', 'Closeable', 'Path', 'Paths', 'Files', 'BigDecimal', 'BigInteger'})


def javatypes(node, source: bytes, generic: bool, out: list[tuple[str, str]], skip: frozenset[str] | None=None, preserve_qualified: bool=False) -> None:
    if node is None:
        return

    if skip is None:
        skip = javaparams(node, source)
    t = node.type

    if t in ('integral_type', 'floating_point_type', 'boolean_type', 'void_type'):
        return

    if t == 'type_identifier':
        name = read(node, source)

        if name and name not in skip and (name not in JAVABUILTINS):
            out.append((name, 'generic_arg' if generic else 'type'))
        return

    if t == 'scoped_type_identifier':
        raw = read(node, source)
        simple = raw.rsplit('.', 1)[-1]
        text = raw if preserve_qualified else raw.rsplit('.', 1)[-1]

        if text and simple not in JAVABUILTINS:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t == 'generic_type':
        for c in node.children:
            if c.type in ('type_identifier', 'scoped_type_identifier'):
                raw = read(c, source)
                simple = raw.rsplit('.', 1)[-1]
                text = raw if preserve_qualified and c.type == 'scoped_type_identifier' else simple

                if text and simple not in JAVABUILTINS and (c.type == 'scoped_type_identifier' or simple not in skip):
                    out.append((text, 'generic_arg' if generic else 'type'))
                break

        for c in node.children:
            if c.type == 'type_arguments':
                for arg in c.children:
                    if arg.is_named:
                        javatypes(arg, source, True, out, skip, preserve_qualified)
        return

    if t == 'array_type':
        for c in node.children:
            if c.is_named:
                javatypes(c, source, generic, out, skip, preserve_qualified)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                javatypes(c, source, generic, out, skip, preserve_qualified)


def javareceiver(type_node, source: bytes) -> str | None:
    if type_node is None:
        return None

    t = type_node.type

    if t == 'type_identifier':
        name = read(type_node, source)
    elif t == 'scoped_type_identifier':
        name = read(type_node, source).rsplit('.', 1)[-1]
    elif t == 'generic_type':
        base = next((child for child in type_node.children if child.type in ('type_identifier', 'scoped_type_identifier')), None)

        return javareceiver(base, source)
    else:
        return None

    if not name or name in JAVABUILTINS or name in javaparams(type_node, source):
        return None

    return name


def javadeclarators(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []

    for child in declaration_node.children:
        if child.type != 'variable_declarator':
            continue

        name_node = child.child_by_field_name('name')

        if name_node is not None:
            name = read(name_node, source)

            if name:
                names.append(name)

    return names


def javalambdas(lambda_node, source: bytes) -> list[tuple[str, str | None]]:
    parameters = lambda_node.child_by_field_name('parameters')

    if parameters is None:
        return []

    if parameters.type == 'identifier':
        return [(read(parameters, source), None)]

    if parameters.type == 'inferred_parameters':
        return [(read(child, source), None) for child in parameters.children if child.type == 'identifier']

    bindings: list[tuple[str, str | None]] = []

    for parameter in parameters.children:
        if parameter.type not in ('formal_parameter', 'spread_parameter'):
            continue

        name_node = parameter.child_by_field_name('name')

        if name_node is not None:
            bindings.append((read(name_node, source), javareceiver(parameter.child_by_field_name('type'), source)))

    return bindings


def javamethods(method_node, source: bytes, field_types: dict[str, str]) -> dict[str, str]:
    method_types: dict[str, str] = {}
    ambiguous: set[str] = set()

    def bind(name: str, type_name: str | None) -> None:
        if not name or not type_name or name in ambiguous:
            return
        previous = method_types.get(name)

        if previous is not None and previous != type_name:
            method_types.pop(name, None)
            ambiguous.add(name)
        else:
            method_types[name] = type_name
    params = method_node.child_by_field_name('parameters')

    if params is not None:
        for param in params.children:
            if param.type not in ('formal_parameter', 'spread_parameter'):
                continue

            type_name = javareceiver(param.child_by_field_name('type'), source)
            name_node = param.child_by_field_name('name')

            if name_node is not None:
                bind(read(name_node, source), type_name)
    body = method_node.child_by_field_name('body')
    stack = list(body.children) if body is not None else []

    while stack:
        node = stack.pop()

        if node.type in ('class_declaration', 'class_body', 'interface_declaration', 'record_declaration', 'enum_declaration', 'annotation_type_declaration'):
            continue

        if node.type == 'lambda_expression':
            for name, type_name in javalambdas(node, source):
                if type_name is None or field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)

        if node.type == 'local_variable_declaration':
            type_name = javareceiver(node.child_by_field_name('type'), source)

            for name in javadeclarators(node, source):
                if field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        stack.extend(node.children)
    table = dict(field_types)
    table.update(method_types)

    for name in ambiguous:
        table.pop(name, None)
    table.update({f'this.{name}': type_name for name, type_name in field_types.items()})

    return table


def javanodes(declaration_node) -> list:
    modifiers = None

    for child in declaration_node.children:
        if child.type == 'modifiers':
            modifiers = child
            break

    if modifiers is None:
        return []

    return [child for child in modifiers.children if child.type in ('marker_annotation', 'annotation')]


def javaannotations(declaration_node, source: bytes) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []

    for anno in javanodes(declaration_node):
        name_node = anno.child_by_field_name('name')

        if name_node is None:
            for sub in anno.children:
                if sub.type in ('identifier', 'scoped_identifier', 'type_identifier'):
                    name_node = sub
                    break

        if name_node is not None:
            raw = read(name_node, source)
            text = raw.rsplit('.', 1)[-1]

            if text:
                names.append((text, raw))

    return names


def javaliterals(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []

    for anno in javanodes(declaration_node):
        arguments = anno.child_by_field_name('arguments')

        if arguments is None:
            continue

        stack = [arguments]

        while stack:
            current = stack.pop()

            if current.type == 'class_literal':
                type_node = next((child for child in current.children if child.is_named), None)
                refs: list[tuple[str, str]] = []
                javatypes(type_node, source, False, refs, preserve_qualified=True)
                names.extend((name for name, _role in refs))
                continue

            stack.extend((child for child in current.children if child.is_named))

    return names


def phpname(node, source: bytes) -> str | None:
    if node is None:
        return None

    return read(node, source).rsplit('\\', 1)[-1] or None


def phptypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t == 'primitive_type':
        return

    if t == 'named_type':
        for c in node.children:
            if c.type in ('name', 'qualified_name'):
                text = phpname(c, source)

                if text:
                    out.append((text, 'generic_arg' if generic else 'type'))
                return
        return

    if t in ('name', 'qualified_name'):
        text = phpname(node, source)

        if text:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t in ('nullable_type', 'union_type', 'intersection_type', 'optional_type'):
        for c in node.children:
            if c.is_named:
                phptypes(c, source, generic, out)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                phptypes(c, source, generic, out)


def phpreturn(method_node):
    saw_params = False

    for c in method_node.children:
        if c.type == 'formal_parameters':
            saw_params = True
            continue

        if saw_params and c.is_named and (c.type not in ('compound_statement',)):
            if c.type in ('named_type', 'primitive_type', 'nullable_type', 'union_type', 'intersection_type', 'optional_type'):
                return c

    return None

KTBUILTINS = frozenset({'Any', 'Unit', 'Nothing', 'Boolean', 'Byte', 'Short', 'Int', 'Long', 'Float', 'Double', 'Char', 'String', 'CharSequence', 'Number', 'Comparable', 'Enum', 'Annotation', 'Pair', 'Triple', 'Lazy', 'Function', 'Throwable', 'Exception', 'RuntimeException', 'Error', 'IllegalArgumentException', 'IllegalStateException', 'NullPointerException', 'IndexOutOfBoundsException', 'ClassCastException', 'NumberFormatException', 'ArithmeticException', 'UnsupportedOperationException', 'NoSuchElementException', 'ConcurrentModificationException', 'StackOverflowError', 'OutOfMemoryError', 'AssertionError', 'InterruptedException', 'Array', 'List', 'MutableList', 'ArrayList', 'Set', 'MutableSet', 'HashSet', 'LinkedHashSet', 'Map', 'MutableMap', 'HashMap', 'LinkedHashMap', 'Collection', 'MutableCollection', 'Iterable', 'MutableIterable', 'Iterator', 'MutableIterator', 'ListIterator', 'MutableListIterator', 'Sequence', 'Comparator', 'Regex', 'MatchResult', 'StringBuilder'})


def ktuser(user_type_node, source: bytes) -> str | None:
    if user_type_node is None:
        return None

    name: str | None = None

    for c in user_type_node.children:
        if c.type in ('type_identifier', 'identifier'):
            text = read(c, source)

            if text:
                name = text
        elif c.type == 'simple_user_type':
            for sub in c.children:
                if sub.type in ('identifier', 'type_identifier'):
                    text = read(sub, source)

                    if text:
                        name = text
                    break

    return name


def ktannotations(declaration_node, source: bytes) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    modifiers = None

    if declaration_node is None:
        return names

    for child in declaration_node.children:
        if child.type == 'modifiers':
            modifiers = child
            break

    if modifiers is None:
        return names

    for anno in modifiers.children:
        if anno.type != 'annotation':
            continue

        for sub in anno.children:
            user_type_node = None

            if sub.type == 'user_type':
                user_type_node = sub
            elif sub.type == 'constructor_invocation':
                for inner in sub.children:
                    if inner.type == 'user_type':
                        user_type_node = inner
                        break

            if user_type_node is not None:
                name = ktuser(user_type_node, source)

                if name:
                    raw = read(user_type_node, source)
                    names.append((name, raw))

    return names


def ktliterals(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []

    if declaration_node is None:
        return names

    modifiers = None

    for child in declaration_node.children:
        if child.type == 'modifiers':
            modifiers = child
            break

    if modifiers is None:
        return names

    seen: set[str] = set()

    for anno in modifiers.children:
        if anno.type != 'annotation':
            continue

        for sub in anno.children:
            ci = sub if sub.type == 'constructor_invocation' else None

            if ci is None:
                continue

            args_node = None

            for c in ci.children:
                if c.type == 'value_arguments':
                    args_node = c
                    break

            if args_node is None:
                continue

            stack = [args_node]

            while stack:
                curr = stack.pop()

                if curr.type == 'navigation_expression':
                    has_double_colon = False
                    rhs_is_class = False
                    lhs_node = None

                    for ch in curr.children:
                        ch_text = read(ch, source)

                        if ch.type == '::' or ch_text == '::':
                            has_double_colon = True
                        elif has_double_colon and ch.type in ('identifier', 'simple_identifier') and (ch_text == 'class'):
                            rhs_is_class = True
                        elif not has_double_colon and ch.is_named:
                            lhs_node = ch

                    if has_double_colon and rhs_is_class and (lhs_node is not None):
                        raw = read(lhs_node, source)
                        text = raw.rsplit('.', 1)[-1]

                        if text and text not in seen and (text not in KTBUILTINS) and (text not in JAVABUILTINS):
                            names.append(text)
                            seen.add(text)
                        continue

                stack.extend((child for child in curr.children if child.is_named))

    return names


def kttypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t in ('integral_literal', 'boolean_literal'):
        return

    if t == 'user_type':
        for c in node.children:
            if c.type in ('identifier', 'type_identifier'):
                text = read(c, source)

                if text and text not in KTBUILTINS and (text not in JAVABUILTINS):
                    out.append((text, 'generic_arg' if generic else 'type'))
                break

            if c.type == 'simple_user_type':
                for sub in c.children:
                    if sub.type in ('identifier', 'type_identifier'):
                        text = read(sub, source)

                        if text and text not in KTBUILTINS and (text not in JAVABUILTINS):
                            out.append((text, 'generic_arg' if generic else 'type'))
                        break

                break

        for c in node.children:
            if c.type == 'type_arguments':
                for arg in c.children:
                    if arg.type == 'type_projection':
                        for sub in arg.children:
                            if sub.is_named:
                                kttypes(sub, source, True, out)
                    elif arg.is_named:
                        kttypes(arg, source, True, out)
        return

    if t in ('identifier', 'type_identifier'):
        text = read(node, source)

        if text and text not in KTBUILTINS and (text not in JAVABUILTINS):
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t in ('nullable_type', 'parenthesized_type', 'type_reference'):
        for c in node.children:
            if c.is_named:
                kttypes(c, source, generic, out)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                kttypes(c, source, generic, out)


def ktproperty(property_node):
    for c in property_node.children:
        if c.type == 'variable_declaration':
            for sub in c.children:
                if sub.type in ('user_type', 'nullable_type', 'type_reference'):
                    return sub

        if c.type in ('user_type', 'nullable_type', 'type_reference'):
            return c

    return None


def ktreturn(func_node):
    saw_params = False
    saw_colon = False

    for c in func_node.children:
        if c.type == 'function_value_parameters':
            saw_params = True
            continue

        if saw_params and c.type == ':':
            saw_colon = True
            continue

        if saw_colon:
            if c.is_named:
                return c

    return None


def swiftkeyword(node) -> str | None:
    for c in node.children:
        if not c.is_named and c.type in ('class', 'struct', 'enum', 'extension', 'actor'):
            return c.type

    return None


def pycollisions(root_node, source: bytes, stem: str) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}

    def record(plain_nid: str, name: str) -> None:
        groups.setdefault(plain_nid, set()).add(name)

    for child in root_node.children:
        if child.type == 'function_definition':
            name_node = child.child_by_field_name('name')

            if name_node is not None:
                name = read(name_node, source)

                if name:
                    record(make(stem, name), name)
        elif child.type == 'class_definition':
            class_name_node = child.child_by_field_name('name')
            body = child.child_by_field_name('body')

            if class_name_node is None or body is None:
                continue

            class_name = read(class_name_node, source)

            if not class_name:
                continue

            class_nid = make(stem, class_name)

            for member in body.children:
                if member.type != 'function_definition':
                    continue

                name_node = member.child_by_field_name('name')

                if name_node is None:
                    continue

                name = read(name_node, source)

                if name:
                    record(make(class_nid, name), name)

    return {nid: names for nid, names in groups.items() if len(names) >= 2}


def pysalted(plain_nid: str, name: str, groups: dict[str, set[str]]) -> str:
    names = groups.get(plain_nid)

    if not names or len(names) < 2:
        return plain_nid

    public = [n for n in names if not n.startswith('_')]

    if len(public) == 1 and name == public[0]:
        return plain_nid

    salt = hashlib.sha1(name.encode('utf-8'), usedforsecurity=False).hexdigest()[:6]

    return make(plain_nid, salt)


def swiftscan(root_node, source: bytes) -> tuple[set[str], set[str]]:
    protocols: set[str] = set()
    classes: set[str] = set()
    stack = [root_node]

    while stack:
        n = stack.pop()

        if n.type == 'protocol_declaration':
            name_node = n.child_by_field_name('name')

            if name_node is None:
                for c in n.children:
                    if c.type == 'type_identifier':
                        name_node = c
                        break

            if name_node is not None:
                text = read(name_node, source)

                if text:
                    protocols.add(text)
        elif n.type == 'class_declaration':
            kw = swiftkeyword(n)

            if kw in ('class', 'struct', 'enum', 'actor'):
                name_node = n.child_by_field_name('name')

                if name_node is not None:
                    text = read(name_node, source)

                    if text:
                        classes.add(text)
        stack.extend(n.children)

    return (protocols, classes)


def swiftclassify(name: str, kind: str | None, is_first: bool, protocols: set[str], classes: set[str]) -> str:
    if name in protocols:
        return 'implements'

    if name in classes:
        return 'inherits'

    if kind in ('struct', 'enum', 'extension', 'actor'):
        return 'implements'

    return 'inherits' if is_first else 'implements'


def swiftuser(user_type_node, source: bytes) -> str | None:
    if user_type_node is None:
        return None

    for c in user_type_node.children:
        if c.type == 'type_identifier':
            text = read(c, source)

            return text or None

    return None


def swifttypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t == 'type_annotation':
        for c in node.children:
            if c.is_named:
                swifttypes(c, source, generic, out)
        return

    if t == 'user_type':
        for c in node.children:
            if c.type == 'type_identifier':
                text = read(c, source)

                if text:
                    out.append((text, 'generic_arg' if generic else 'type'))
                break

        for c in node.children:
            if c.type == 'type_arguments':
                for arg in c.children:
                    if arg.is_named:
                        swifttypes(arg, source, True, out)
        return

    if t == 'type_identifier':
        text = read(node, source)

        if text:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t in ('optional_type', 'implicitly_unwrapped_optional_type', 'array_type', 'dictionary_type', 'tuple_type'):
        for c in node.children:
            if c.is_named:
                swifttypes(c, source, generic, out)
        return

    if node.is_named:
        for c in node.children:
            if c.is_named:
                swifttypes(c, source, generic, out)


def swiftproperty(property_node):
    for c in property_node.children:
        if c.type == 'type_annotation':
            return c

    return None


def swiftattribute(property_node, source: bytes) -> str | None:
    for c in property_node.children:
        if c.type != 'modifiers':
            continue

        for attr in c.children:
            if attr.type != 'attribute':
                continue

            head = next((a for a in attr.children if a.type == 'user_type'), None)

            if head is None or read(head, source) != 'Environment':
                continue

            arg = next((a for a in attr.children if a.type == 'navigation_expression'), None)

            if arg is None:
                continue

            named = [a for a in arg.children if a.is_named]

            if len(named) != 2:
                continue

            ident, suffix = named

            if ident.type != 'simple_identifier' or suffix.type != 'navigation_suffix':
                continue

            if read(suffix, source) != '.self':
                continue

            name = read(ident, source)

            if name and name[:1].isupper():
                return name

    return None


def swiftfactory(call_node, source: bytes) -> tuple[str, str] | None:
    first = call_node.children[0] if call_node.children else None

    if first is None or first.type != 'navigation_expression':
        return None

    named = [c for c in first.children if c.is_named]

    if len(named) != 2:
        return None

    head, suffix = named

    if head.type != 'simple_identifier' or suffix.type != 'navigation_suffix':
        return None

    htext = read(head, source)

    if not htext or not htext[:1].isupper():
        return None

    mname = next((read(sc, source) for sc in suffix.children if sc.type == 'simple_identifier'), None)

    if not mname:
        return None

    return (htext, mname)


def swiftpropname(property_node, source: bytes) -> str | None:
    for c in property_node.children:
        if c.type == 'pattern':
            for sc in c.children:
                if sc.type == 'simple_identifier':
                    return read(sc, source)

        if c.type == 'simple_identifier':
            return read(c, source)

    return None


def swiftconstructor(call_node, source: bytes) -> str | None:
    first = call_node.children[0] if call_node.children else None

    if first is not None and first.type == 'simple_identifier':
        text = read(first, source)

        if text and text[:1].isupper():
            return text

    return None


def swiftreceiver(recv_node, source: bytes) -> str | None:
    if recv_node is None:
        return None

    if recv_node.type == 'simple_identifier':
        return read(recv_node, source)

    if recv_node.type == 'navigation_expression':
        head = recv_node.children[0] if recv_node.children else None

        if head is not None and head.type == 'simple_identifier':
            return read(head, source)

        if head is not None and head.type == 'self_expression':
            for child in recv_node.children:
                if child.type == 'navigation_suffix':
                    for sc in child.children:
                        if sc.type == 'simple_identifier':
                            return read(sc, source)

    return None

CPRIMITIVES = frozenset({'primitive_type', 'sized_type_specifier', 'auto', 'placeholder_type_specifier'})


def ctypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None or node.type in CPRIMITIVES:
        return
    t = node.type

    if t == 'type_identifier':
        text = read(node, source)

        if text:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t in ('pointer_declarator', 'reference_declarator', 'array_declarator', 'type_qualifier', 'type_descriptor', 'abstract_pointer_declarator', 'abstract_reference_declarator', 'abstract_array_declarator'):
        for c in node.children:
            if c.is_named:
                ctypes(c, source, generic, out)


def cpptypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None or node.type in CPRIMITIVES:
        return
    t = node.type

    if t == 'type_identifier':
        text = read(node, source)

        if text:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t == 'qualified_identifier':
        name_node = node.child_by_field_name('name')

        if name_node is not None:
            cpptypes(name_node, source, generic, out)
        return

    if t == 'template_type':
        name_node = node.child_by_field_name('name')

        if name_node is not None:
            text = read(name_node, source)

            if text:
                out.append((text, 'generic_arg' if generic else 'type'))
        args_node = node.child_by_field_name('arguments')

        if args_node is not None:
            for c in args_node.children:
                if c.is_named:
                    cpptypes(c, source, True, out)
        return

    if t in ('type_descriptor', 'pointer_declarator', 'reference_declarator', 'array_declarator', 'type_qualifier', 'abstract_pointer_declarator', 'abstract_reference_declarator', 'abstract_array_declarator'):
        for c in node.children:
            if c.is_named:
                cpptypes(c, source, generic, out)


def scalatypes(node, source: bytes, generic: bool, out: list[tuple[str, str]]) -> None:
    if node is None:
        return
    t = node.type

    if t == 'type_identifier':
        text = read(node, source)

        if text:
            out.append((text, 'generic_arg' if generic else 'type'))
        return

    if t == 'generic_type':
        base = node.child_by_field_name('type')

        if base is None:
            for c in node.children:
                if c.type == 'type_identifier':
                    base = c
                    break

        if base is not None and base.type == 'type_identifier':
            text = read(base, source)

            if text:
                out.append((text, 'generic_arg' if generic else 'type'))

        for c in node.children:
            if c.type == 'type_arguments':
                for arg in c.children:
                    if arg.is_named:
                        scalatypes(arg, source, True, out)
        return

    if t in ('compound_type', 'infix_type', 'function_type', 'tuple_type', 'annotated_type', 'projected_type'):
        for c in node.children:
            if c.is_named:
                scalatypes(c, source, generic, out)

    if t in ('match_type', 'type_case_clause'):
        for c in node.children:
            if c.is_named:
                scalatypes(c, source, generic, out)


def scalabounds(tp_node, source: bytes, out: list[tuple[str, str]]) -> None:
    if tp_node is None:
        return

    for c in tp_node.children:
        if c.type == 'type_parameters':
            scalabounds(c, source, out)
        elif c.type == 'context_bound':
            bound_type = c.child_by_field_name('type')

            if bound_type is not None:
                scalatypes(bound_type, source, False, out)


def pyparams(params_node, source: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    if params_node is None:
        return out

    for child in params_node.children:
        if child.type in ('typed_parameter', 'typed_default_parameter'):
            type_node = child.child_by_field_name('type')
            pytypes(type_node, source, False, out)

    return out


def pynames(params_node, source: bytes) -> set[str]:
    out: set[str] = set()

    if params_node is None:
        return out

    for child in params_node.children:
        if child.type == 'identifier':
            out.add(read(child, source))
        elif child.type in ('typed_parameter', 'default_parameter', 'typed_default_parameter', 'list_splat_pattern', 'dictionary_splat_pattern'):
            name_n = child.child_by_field_name('name')

            if name_n is None:
                name_n = next((c for c in child.children if c.type == 'identifier'), None)

            if name_n is not None:
                out.add(read(name_n, source))

    return out


def pytargets(node, source: bytes, out: set[str]) -> None:
    if node is None:
        return

    if node.type == 'identifier':
        out.add(read(node, source))
        return

    if node.type in ('pattern_list', 'tuple_pattern', 'list_pattern'):
        for c in node.children:
            pytargets(c, source, out)
SELFLANGS = frozenset({'tree_sitter_python', 'tree_sitter_javascript', 'tree_sitter_typescript', 'tree_sitter_swift', 'tree_sitter_ruby', 'tree_sitter_php'})


def selftarget(caller_nid: str, callee: str, receiver: str, label_to_nid: dict[str, str], scope_parents: dict[str, str], method_owner: dict[str, str], methods_by_owner: dict[tuple[str, str], str], class_bases: dict[str, list[str]], walk_bases: bool=True, require_method_owner: bool=False) -> str | None:
    scope: str | None = caller_nid

    while scope and scope not in method_owner:
        scope = scope_parents.get(scope)
    fallback = label_to_nid.get(callee)

    if not scope:
        return None if require_method_owner else fallback

    level = [method_owner[scope]]
    seen: set[str] = set()
    skip_own = receiver == 'super'

    while level:
        seen.update(level)

        if not skip_own:
            hits = {methods_by_owner[c, callee] for c in level if (c, callee) in methods_by_owner}

            if hits:
                return hits.pop() if len(hits) == 1 else None

        if not walk_bases:
            return None if skip_own else fallback

        skip_own = False
        level = list(dict.fromkeys((base for c in level for base in class_bases.get(c, ()) if base not in seen)))

    return None if fallback in method_owner else fallback


def pylocals(func_def_node, source: bytes) -> set[str]:
    bound: set[str] = set()
    bound |= pynames(func_def_node.child_by_field_name('parameters'), source)

    def walk(n) -> None:
        for child in n.children:
            t = child.type

            if t in ('function_definition', 'class_definition', 'lambda'):
                continue

            if t == 'assignment':
                pytargets(child.child_by_field_name('left'), source, bound)
            elif t in ('for_statement', 'for_in_clause'):
                pytargets(child.child_by_field_name('left'), source, bound)
            elif t == 'with_statement':
                for item in child.children:
                    if item.type == 'with_clause':
                        for wi in item.children:
                            if wi.type == 'with_item':
                                alias = wi.child_by_field_name('alias')
                                pytargets(alias, source, bound)
            elif t == 'named_expression':
                pytargets(child.child_by_field_name('name'), source, bound)
            walk(child)
    body = func_def_node.child_by_field_name('body')

    if body is not None:
        walk(body)

    return bound


def pymodules(root, source: bytes) -> set[str]:
    bound: set[str] = set()

    def walk(n) -> None:
        for child in n.children:
            t = child.type

            if t in ('function_definition', 'class_definition', 'lambda'):
                continue

            if t == 'assignment':
                pytargets(child.child_by_field_name('left'), source, bound)
            elif t in ('for_statement', 'for_in_clause'):
                pytargets(child.child_by_field_name('left'), source, bound)
            elif t == 'named_expression':
                pytargets(child.child_by_field_name('name'), source, bound)
            walk(child)
    walk(root)

    return bound


def pyshadow(root, source: bytes):
    scope_types = {'module', 'function_definition', 'class_definition', 'lambda'}
    scopes: dict[int, tuple[set[str], dict[str, int], bool]] = {}
    dynamic_binding = False
    dynamic_names = {'exec', 'eval', 'globals', 'locals', 'vars', 'setattr', 'delattr'}
    pending = [root]

    while pending:
        candidate = pending.pop()

        if candidate.type in {'global_statement', 'nonlocal_statement'} or (candidate.type == 'identifier' and read(candidate, source) in dynamic_names):
            dynamic_binding = True
            break

        pending.extend(candidate.named_children)

    def targetnames(node, names: set[str]) -> bool:
        if node is None:
            return False

        if node.type == 'identifier':
            names.add(read(node, source))

            return True

        if node.type in {'tuple_pattern', 'list_pattern', 'pattern_list', 'parenthesized_expression', 'as_pattern_target'}:
            return all((targetnames(c, names) for c in node.named_children))

        if node.type in {'attribute', 'subscript'}:
            return targetnames(node.child_by_field_name('object') or node.named_children[0], names)

        return False

    def importnames(node) -> list[str] | None:
        names: list[str] = []
        past_import = False

        for item in node.children:
            if item.type == 'import':
                past_import = True
                continue

            if not past_import:
                continue

            if item.type == 'aliased_import':
                alias = item.child_by_field_name('alias')

                if alias is None:
                    return None

                names.append(read(alias, source))
            elif item.type == 'dotted_name':
                names.append(read(item, source).split('.')[0])
            elif item.type == 'wildcard_import':
                return None

        return names

    def facts(scope):
        key = scope.id

        if key in scopes:
            return scopes[key]

        bound: set[str] = set()
        plain_imports: dict[str, int] = {}
        unsafe = bool(scope.has_error or scope.child_by_field_name('type_parameters') or (scope.type == 'module' and dynamic_binding))

        if scope.type in {'function_definition', 'lambda'}:
            bound.update(pynames(scope.child_by_field_name('parameters'), source))

        def walk(node) -> None:
            nonlocal unsafe

            for child in node.named_children:
                kind = child.type

                if kind in {'function_definition', 'class_definition'}:
                    name = child.child_by_field_name('name')

                    if name is None:
                        unsafe = True
                    else:
                        bound.add(read(name, source))
                    continue

                if kind == 'lambda':
                    continue

                if kind in {'global_statement', 'nonlocal_statement', 'match_statement', 'ERROR'}:
                    unsafe = True
                    continue

                if kind in {'import_statement', 'import_from_statement'}:
                    names = importnames(child)

                    if names is None:
                        unsafe = True
                    elif kind == 'import_statement' and scope.type == 'module' and (node == scope):
                        for name in names:
                            plain_imports[name] = plain_imports.get(name, 0) + 1
                    else:
                        bound.update(names)
                    continue

                if kind in {'assignment', 'augmented_assignment', 'for_statement', 'for_in_clause'}:
                    if not targetnames(child.child_by_field_name('left'), bound):
                        unsafe = True
                elif kind == 'type_alias_statement':
                    left = child.child_by_field_name('left')
                    name = read(left, source).split('[', 1)[0].strip() if left is not None else ''

                    if name.isidentifier():
                        bound.add(name)
                    else:
                        unsafe = True
                elif kind == 'named_expression':
                    if not targetnames(child.child_by_field_name('name'), bound):
                        unsafe = True
                elif kind == 'with_item':
                    alias = child.child_by_field_name('alias')
                    value = child.child_by_field_name('value')

                    if alias is None and value is not None and (value.type == 'as_pattern'):
                        alias = value.child_by_field_name('alias')

                    if alias is not None and (not targetnames(alias, bound)):
                        unsafe = True
                elif kind == 'as_pattern' and node.type == 'except_clause':
                    if not targetnames(child.child_by_field_name('alias'), bound):
                        unsafe = True
                elif kind == 'delete_statement':
                    for item in child.named_children:
                        if not targetnames(item, bound):
                            unsafe = True
                walk(child)
        body = scope if scope.type == 'module' else scope.child_by_field_name('body')

        if body is None:
            unsafe = True
        else:
            walk(body)
        scopes[key] = (bound, plain_imports, unsafe)

        return scopes[key]

    def isshadowed(call, receiver: str) -> bool:
        node = call
        found_module = False

        while node is not None:
            if node.type in scope_types:
                bound, plain_imports, unsafe = facts(node)

                if unsafe or receiver in bound:
                    return True

                if node.type == 'module':
                    found_module = True

                    if plain_imports.get(receiver) != 1:
                        return True

            node = node.parent

        return not found_module

    return isshadowed


def pyself(node, source: bytes) -> str | None:
    if node is None or node.type != 'attribute':
        return None

    obj, attr = (node.child_by_field_name('object'), node.child_by_field_name('attribute'))

    if obj is None or attr is None or read(obj, source) != 'self':
        return None

    return read(attr, source)


def pybindings(body, source: bytes):
    stack = list(body.named_children)

    while stack:
        node = stack.pop()

        if node.type in ('function_definition', 'class_definition', 'lambda'):
            continue

        stack.extend(node.named_children)
        left = node.child_by_field_name('left') if node.type == 'assignment' else None

        if left is None:
            continue

        value, right = (node.child_by_field_name('type'), node.child_by_field_name('right'))

        if value is None and right is not None:
            if right.type == 'none':
                continue

            value = right.child_by_field_name('function') if right.type == 'call' else None
        name = read(value, source) if value is not None else ''
        typed = name.isidentifier() and name[:1].isupper()

        for target in left.named_children if left.type == 'pattern_list' else [left]:
            attr = pyself(target, source)

            if attr:
                yield (attr, name if typed and target is left else None)

JSBOUNDARY = frozenset({'function_declaration', 'function_expression', 'function', 'arrow_function', 'method_definition', 'class_declaration', 'class', 'generator_function', 'generator_function_declaration'})


def jspatterns(node, source: bytes, bound: set) -> None:
    t = node.type

    if t in ('identifier', 'shorthand_property_identifier_pattern'):
        bound.add(read(node, source))
        return

    if t == 'type_annotation':
        return

    if t == 'assignment_pattern':
        left = node.child_by_field_name('left')

        if left is not None:
            jspatterns(left, source, bound)
        return

    if t == 'pair_pattern':
        val = node.child_by_field_name('value')

        if val is not None:
            jspatterns(val, source, bound)
        return

    for c in node.children:
        if c.is_named:
            jspatterns(c, source, bound)


def jslocals(func_node, source: bytes) -> set[str]:
    bound: set[str] = set()
    params = func_node.child_by_field_name('parameters')

    if params is not None:
        jspatterns(params, source, bound)
    solo = func_node.child_by_field_name('parameter')

    if solo is not None:
        jspatterns(solo, source, bound)

    def walk(n) -> None:
        for c in n.children:
            if c.type in JSBOUNDARY:
                continue

            if c.type == 'variable_declarator' and n.type == 'variable_declaration':
                name = c.child_by_field_name('name')

                if name is not None:
                    jspatterns(name, source, bound)
            elif c.type == 'for_in_statement' and any((gc.type == 'var' for gc in c.children)):
                left = c.child_by_field_name('left')

                if left is not None:
                    jspatterns(left, source, bound)
            walk(c)
    body = func_node.child_by_field_name('body')

    if body is not None:
        walk(body)

    return bound


def jslexical(node, source: bytes) -> frozenset[str]:
    names: set[str] = set()

    for child in node.children:
        if child.type != 'lexical_declaration':
            continue

        for declarator in child.children:
            if declarator.type == 'variable_declarator':
                name = declarator.child_by_field_name('name')

                if name is not None:
                    jspatterns(name, source, names)

    return frozenset(names)


def jsmodules(root, source: bytes) -> set[str]:
    bound: set[str] = set()

    def walk(n) -> None:
        for c in n.children:
            if c.type in JSBOUNDARY:
                continue

            if c.type == 'variable_declarator':
                value = c.child_by_field_name('value')

                if value is None or value.type not in JSFUNCTIONS:
                    name = c.child_by_field_name('name')

                    if name is not None:
                        jspatterns(name, source, bound)
            walk(c)
    walk(root)

    return bound


def jsexternal(raw: str, str_path: str) -> bool:
    resolved = jstarget(raw, str_path)

    if resolved is None:
        return False

    _target_nid, resolved_path = resolved

    if resolved_path is None:
        return True

    return 'node_modules' in resolved_path.parts


def jsimports(root, source: bytes, str_path: str) -> set[str]:
    bound: set[str] = set()

    def clausenames(clause) -> None:
        for c in clause.children:
            if c.type == 'identifier':
                bound.add(read(c, source))
            elif c.type == 'namespace_import':
                for ident in c.children:
                    if ident.type == 'identifier':
                        bound.add(read(ident, source))
            elif c.type == 'named_imports':
                for spec in c.children:
                    if spec.type != 'import_specifier':
                        continue

                    idents = [g for g in spec.children if g.type == 'identifier']

                    if idents:
                        bound.add(read(idents[-1], source))

    def walk(n) -> None:
        for c in n.children:
            if c.type == 'import_statement':
                src_node = c.child_by_field_name('source')

                if src_node is not None:
                    raw = read(src_node, source).strip('"\'`')

                    if jsexternal(raw, str_path):
                        for child in c.children:
                            if child.type == 'import_clause':
                                clausenames(child)
                continue

            walk(c)
    walk(root)

    return bound


def jsdispatch(coll_node):
    if coll_node.type == 'object':
        for c in coll_node.children:
            if c.type == 'pair':
                val = c.child_by_field_name('value')

                if val is not None and val.type == 'identifier':
                    yield val
            elif c.type == 'shorthand_property_identifier':
                yield c
    else:
        for el in coll_node.children:
            if el.type == 'identifier':
                yield el


def findbody(node, config: LanguageConfig):
    b = node.child_by_field_name(config.body_field)

    if b:
        return b

    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child

    return None


def jsdynamic(node, source: bytes, caller_nid: str, str_path: str, edges: list, seen_dyn_pairs: set) -> bool:
    func_node = node.child_by_field_name('function')

    if func_node is None:
        if node.children and read(node.children[0], source) == 'import':
            func_node = node.children[0]
        else:
            return False

    if read(func_node, source) != 'import':
        return False

    args = node.child_by_field_name('arguments')

    if args is None:
        return True

    for arg in args.children:
        if arg.type == 'template_string':
            if any((c.type == 'template_substitution' for c in arg.children)):
                break

            raw = read(arg, source).strip('`')
        elif arg.type == 'string':
            raw = read(arg, source).strip('\'" ')
        else:
            continue

        if not raw:
            break

        resolved = jstarget(raw, str_path)

        if resolved is None:
            break

        tgt_nid, resolved_path = resolved
        pair = (caller_nid, tgt_nid)

        if pair not in seen_dyn_pairs:
            seen_dyn_pairs.add(pair)
            edge = {'source': caller_nid, 'target': tgt_nid, 'relation': 'imports_from', 'context': 'import', 'deferred': True, 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}'}

            if resolved_path is not None:
                edge['target_file'] = str(resolved_path)
            edges.append(edge)
        break

    return True


def cppfunc(node, source: bytes) -> str | None:
    if node.type == 'identifier':
        return read(node, source)

    if node.type in ('field_identifier', 'destructor_name', 'operator_name'):
        return read(node, source)

    if node.type == 'qualified_identifier':
        return read(node, source)

    decl = node.child_by_field_name('declarator')

    if decl:
        return cppfunc(decl, source)

    for child in node.children:
        if child.type == 'identifier':
            return read(child, source)

    return None


def cppdeclarator(node, source: bytes) -> str | None:
    t = node.type

    if t == 'identifier':
        return read(node, source)

    if t in ('pointer_declarator', 'reference_declarator', 'init_declarator'):
        inner = node.child_by_field_name('declarator')

        if inner is None:
            for c in node.children:
                if c.type in ('identifier', 'pointer_declarator', 'reference_declarator'):
                    inner = c
                    break

        if inner is not None:
            return cppdeclarator(inner, source)

    return None


def cpplocals(body_node, source: bytes, table: dict[str, str]) -> None:
    stack = [body_node]

    while stack:
        n = stack.pop()

        if n.type in ('function_definition', 'lambda_expression'):
            if n is not body_node:
                continue

        if n.type == 'declaration':
            type_node = n.child_by_field_name('type')

            if type_node is not None and type_node.type in ('type_identifier', 'qualified_identifier'):
                type_name = read(type_node, source).split('::')[-1].strip()
                declarators = [c for c in n.children if c.type in ('identifier', 'pointer_declarator', 'reference_declarator', 'init_declarator')]

                if type_name and type_name[:1].isupper() and (len(declarators) == 1):
                    var = cppdeclarator(declarators[0], source)

                    if var and var not in table:
                        table[var] = type_name

        for c in n.children:
            stack.append(c)


def swiftlocals(body_node, source: bytes, table: dict[str, str], factory: dict[str, tuple[str, str]] | None=None) -> None:
    stack = [body_node]

    while stack:
        n = stack.pop()

        if n.type == 'function_declaration' and n is not body_node:
            continue

        if n.type == 'property_declaration':
            prop_type: str | None = None
            factory_bind: tuple[str, str] | None = None

            for child in n.children:
                if child.type == 'call_expression':
                    prop_type = swiftconstructor(child, source)

                    if prop_type is None:
                        factory_bind = swiftfactory(child, source)
                    break

                if child.type == 'navigation_expression':
                    head = child.children[0] if child.children else None

                    if head is not None and head.type == 'simple_identifier':
                        htext = read(head, source)

                        if htext and htext[:1].isupper():
                            prop_type = htext
                    break

            name = swiftpropname(n, source)

            if name and prop_type and (name not in table):
                table[name] = prop_type
            elif name and factory_bind is not None and (factory is not None) and (name not in table) and (name not in factory):
                factory[name] = factory_bind

        for c in n.children:
            stack.append(c)


def csreceiver(type_node, source: bytes) -> str | None:
    info = cstypename(type_node, source)

    if not info:
        return None

    name = info[0]

    return name if name and name[:1].isupper() else None


def csmethods(method_node, source: bytes, field_types: dict[str, str]) -> tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]]:
    bindings: dict[str, list[tuple[int, int, str | None]]] = {}
    field_poisoned: set[str] = set()

    def bind(name: str | None, type_name: str | None, scope_node) -> None:
        if not name or scope_node is None:
            return

        if field_types.get(name) not in (None, type_name):
            field_poisoned.add(name)
        bindings.setdefault(name, []).append((scope_node.start_byte, scope_node.end_byte, type_name))

    def bindparam(param, scope_node) -> None:
        name_node = param.child_by_field_name('name')

        if name_node is not None:
            bind(read(name_node, source), csreceiver(param.child_by_field_name('type'), source), scope_node)
    body = method_node.child_by_field_name('body')
    param_scope = body if body is not None else method_node
    params = method_node.child_by_field_name('parameters')

    if params is not None:
        for param in params.children:
            if param.type == 'parameter':
                bindparam(param, param_scope)
    stack = [(child, param_scope) for child in body.children] if body is not None else []

    while stack:
        node, scope = stack.pop()

        if node.type in ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration', 'enum_declaration'):
            continue

        if node.type == 'lambda_expression':
            lam_params = node.child_by_field_name('parameters')

            if lam_params is not None:
                if lam_params.type == 'implicit_parameter':
                    bind(read(lam_params, source), None, node)
                else:
                    for param in lam_params.children:
                        if param.type == 'parameter':
                            bindparam(param, node)
                        elif param.type == 'implicit_parameter':
                            bind(read(param, source), None, node)
        elif node.type == 'local_function_statement':
            lf_params = node.child_by_field_name('parameters')

            if lf_params is not None:
                for param in lf_params.children:
                    if param.type == 'parameter':
                        bindparam(param, node)
        elif node.type == 'local_declaration_statement':
            vd = next((c for c in node.children if c.type == 'variable_declaration'), None)

            if vd is not None:
                declared = csreceiver(vd.child_by_field_name('type'), source)

                for declarator in vd.children:
                    if declarator.type != 'variable_declarator':
                        continue

                    name_node = declarator.child_by_field_name('name') or next((g for g in declarator.children if g.type == 'identifier'), None)

                    if name_node is None:
                        continue

                    type_name = declared

                    if type_name is None:
                        for g in declarator.children:
                            if g.type == 'object_creation_expression':
                                type_name = csreceiver(g.child_by_field_name('type'), source)
                                break

                    bind(read(name_node, source), type_name, scope)
        elif node.type in ('declaration_expression', 'declaration_pattern'):
            name_node = node.child_by_field_name('name')

            if name_node is not None and name_node.type == 'identifier':
                bind(read(name_node, source), csreceiver(node.child_by_field_name('type'), source), scope)
        child_scope = node if node.type in ('block', 'lambda_expression', 'local_function_statement') else scope
        stack.extend(((child, child_scope) for child in node.children))
    base = {name: type_name for name, type_name in field_types.items() if name not in field_poisoned}

    for name in field_poisoned:
        bindings.pop(name, None)

    return (bindings, base)


def csscoped(table: tuple[dict[str, list[tuple[int, int, str | None]]], dict[str, str]] | None, name: str | None, call_byte: int) -> str | None:
    if not table or not name:
        return None

    bindings, base = table
    candidates = [b for b in bindings.get(name, ()) if b[0] <= call_byte < b[1]]

    if not candidates:
        return base.get(name)

    innermost = min((end - start for start, end, _ in candidates))
    inner = [b for b in candidates if b[1] - b[0] == innermost]

    if len(inner) == 1:
        return inner[0][2]

    return None


def tsreceivers(root, source: bytes, table: dict[str, str]) -> None:

    def baretype(type_annotation):
        idents = [c for c in type_annotation.children if c.type == 'type_identifier']
        others = [c for c in type_annotation.children if c.is_named and c.type not in ('type_identifier',)]

        if len(idents) == 1 and (not others):
            return read(idents[0], source)

        return None

    def optionaltype(type_annotation):
        unions = [c for c in type_annotation.children if c.is_named]

        if len(unions) != 1 or unions[0].type != 'union_type':
            return None

        members = [c for c in unions[0].children if c.is_named]
        idents = [c for c in members if c.type == 'type_identifier']
        empties = [c for c in members if read(c, source) in ('undefined', 'null')]

        if len(idents) == 1 and len(idents) + len(empties) == len(members):
            return read(idents[0], source)

        return None

    stack = [root]

    while stack:
        n = stack.pop()
        t = n.type

        if t == 'variable_declarator':
            name_n = n.child_by_field_name('name')
            value = n.child_by_field_name('value')

            if name_n is not None and name_n.type == 'identifier' and (value is not None) and (value.type == 'new_expression'):
                ctor = value.child_by_field_name('constructor')

                if ctor is not None and ctor.type in ('identifier', 'type_identifier'):
                    name = read(name_n, source)
                    tname = read(ctor, source)

                    if name and tname and (name not in table):
                        table[name] = tname
        elif t == 'required_parameter' or t == 'optional_parameter':
            pat = n.child_by_field_name('pattern')
            ann = n.child_by_field_name('type')

            if pat is not None and pat.type == 'identifier' and (ann is not None):
                tname = baretype(ann)
                name = read(pat, source)

                if name and tname and (name not in table):
                    table[name] = tname
        elif t in ('public_field_definition', 'field_definition'):
            name_n = n.child_by_field_name('name') or n.child_by_field_name('property')

            if name_n is not None and name_n.type in ('property_identifier', 'private_property_identifier') and (not any((c.type == 'static' for c in n.children))):
                ann = n.child_by_field_name('type')
                value = n.child_by_field_name('value')
                tname = None

                if ann is not None:
                    tname = baretype(ann) or optionaltype(ann)
                elif value is not None and value.type == 'new_expression':
                    ctor = value.child_by_field_name('constructor')

                    if ctor is not None and ctor.type in ('identifier', 'type_identifier'):
                        tname = read(ctor, source)
                name = read(name_n, source)

                if name and tname and (name not in table):
                    table[name] = tname

        for c in n.children:
            stack.append(c)


def findrequire(value_node):
    if value_node is None:
        return None

    if value_node.type == 'call_expression':
        fn = value_node.child_by_field_name('function')

        if fn is not None and fn.type == 'identifier':
            return value_node

    if value_node.type == 'member_expression':
        obj = value_node.child_by_field_name('object')

        return findrequire(obj)

    return None


def jsrequire(node, source: bytes, importer_nid: str, stem: str, edges: list, str_path: str) -> bool:
    if node.type not in ('lexical_declaration', 'variable_declaration'):
        return False

    found = False

    for child in node.children:
        if child.type != 'variable_declarator':
            continue

        value = child.child_by_field_name('value')
        call = findrequire(value)

        if call is None:
            continue

        fn = call.child_by_field_name('function')

        if fn is None or read(fn, source) != 'require':
            continue

        args = call.child_by_field_name('arguments')

        if args is None:
            continue

        raw = None

        for arg in args.children:
            if arg.type == 'string':
                raw = read(arg, source).strip('\'"` ')
                break

        if not raw:
            continue

        resolved = jstarget(raw, str_path)

        if resolved is None:
            continue

        tgt_nid, resolved_path = resolved

        if resolved_path is not None and (not resolved_path.is_file()):
            tgt_nid = make('ref', raw)
            resolved_path = None
        line = node.start_point[0] + 1
        edge = {'source': importer_nid, 'target': tgt_nid, 'relation': 'imports_from', 'context': 'import', 'source_file': str_path, 'source_location': f'L{line}'}

        if resolved_path is not None:
            edge['target_file'] = str(resolved_path)
        edges.append(edge)
        found = True
        target_stem = stem(resolved_path) if resolved_path is not None else None
        name_node = child.child_by_field_name('name')
        sym_names: list[str] = []

        if name_node is not None and name_node.type == 'object_pattern':
            for prop in name_node.children:
                if prop.type == 'shorthand_property_identifier_pattern':
                    sym_names.append(read(prop, source))
                elif prop.type == 'pair_pattern':
                    key = prop.child_by_field_name('key')

                    if key is not None:
                        sym_names.append(read(key, source))
        elif value is not None and value.type == 'member_expression':
            prop = value.child_by_field_name('property')

            if prop is not None:
                sym_names.append(read(prop, source))

        if target_stem is not None:
            for sym in sym_names:
                edges.append({'source': importer_nid, 'target': make(target_stem, sym), 'relation': 'imports', 'context': 'import', 'source_file': str_path, 'source_location': f'L{line}'})

    return found

JSFUNCTIONS = frozenset({'arrow_function', 'function_expression', 'function', 'generator_function'})


def jsnested(container_node, parent_nid: str, *, source: bytes, config, addnode, addedge, callable_def_nids: set | None, local_bound_names: dict | None, function_bodies: list) -> None:
    if container_node is None:
        return

    for child in container_node.children:
        if child.type in ('function_declaration', 'generator_function_declaration'):
            name_node = child.child_by_field_name(config.name_field)

            if name_node is None:
                for c in child.children:
                    if c.type in config.name_fallback_child_types:
                        name_node = c
                        break

            func_name = read(name_node, source) if name_node else None

            if func_name and normalize(func_name):
                line = child.start_point[0] + 1
                nested_nid = make(parent_nid, func_name)
                addnode(nested_nid, f'{func_name}()', line)
                addedge(parent_nid, nested_nid, 'contains', line)

                if callable_def_nids is not None:
                    callable_def_nids.add(nested_nid)

                if local_bound_names is not None:
                    local_bound_names[nested_nid] = jslocals(child, source)
                nested_body = findbody(child, config)

                if nested_body:
                    function_bodies.append((nested_nid, nested_body))
                    jsnested(nested_body, nested_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies)
        elif child.type in JSFUNCTIONS:
            jsnested(findbody(child, config), parent_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies)
        else:
            jsnested(child, parent_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies)


def pynested(container_node, parent_nid: str, *, source: bytes, config, addnode, addedge, callable_def_nids: set | None, local_bound_names: dict | None, function_bodies: list, scope_parents: dict[str, str] | None=None, lexical_nids_by_scope: dict[str, dict[str, str]] | None=None) -> None:
    if container_node is None:
        return

    for child in container_node.children:
        target = child

        if target.type == 'decorated_definition':
            inner = target.child_by_field_name('definition')

            if inner is not None:
                target = inner

        if target.type == 'function_definition':
            name_node = target.child_by_field_name(config.name_field)

            if name_node is None:
                for c in target.children:
                    if c.type in config.name_fallback_child_types:
                        name_node = c
                        break

            func_name = read(name_node, source) if name_node else None

            if func_name and normalize(func_name):
                line = target.start_point[0] + 1
                nested_nid = make(parent_nid, func_name)
                addnode(nested_nid, f'{func_name}()', line)
                addedge(parent_nid, nested_nid, 'contains', line)

                if callable_def_nids is not None:
                    callable_def_nids.add(nested_nid)

                if local_bound_names is not None:
                    local_bound_names[nested_nid] = pylocals(target, source)

                if scope_parents is not None:
                    scope_parents[nested_nid] = parent_nid

                if lexical_nids_by_scope is not None:
                    lexical_nids_by_scope.setdefault(parent_nid, {})[func_name] = nested_nid
                    lexical_nids_by_scope.setdefault(nested_nid, {})[func_name] = nested_nid
                nested_body = findbody(target, config)

                if nested_body:
                    function_bodies.append((nested_nid, nested_body))
                    pynested(nested_body, nested_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies, scope_parents=scope_parents, lexical_nids_by_scope=lexical_nids_by_scope)
        else:
            pynested(child, parent_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies, scope_parents=scope_parents, lexical_nids_by_scope=lexical_nids_by_scope)


def jsclosures(node, out: list) -> None:
    for c in node.children:
        if c.type in JSFUNCTIONS:
            out.append(c)
        else:
            jsclosures(c, out)


def jsmember(left, source: bytes):
    if left is None or left.type != 'member_expression':
        return None

    prop = left.child_by_field_name('property')

    if prop is None:
        return None

    member_name = read(prop, source)

    if not member_name:
        return None

    obj = left.child_by_field_name('object')

    if obj is None:
        return None

    if obj.type == 'this':
        return ('this', None, member_name)

    if obj.type == 'identifier':
        if read(obj, source) == 'exports':
            return ('exports', None, member_name)

        return ('object', read(obj, source), member_name)

    if obj.type == 'member_expression':
        inner_obj = obj.child_by_field_name('object')
        inner_prop = obj.child_by_field_name('property')

        if inner_obj is None or inner_prop is None:
            return None

        inner_prop_name = read(inner_prop, source)

        if inner_obj.type == 'identifier':
            inner_obj_name = read(inner_obj, source)

            if inner_obj_name == 'module' and inner_prop_name == 'exports':
                return ('exports', None, member_name)

            if inner_prop_name == 'prototype':
                return ('prototype', inner_obj_name, member_name)

    return None


def jsscan(body, this_owner_nid: str, source: bytes, *, this_as_method: bool, add_node_fn, add_edge_fn, function_bodies: list) -> None:
    object_bindings: dict[str, object] = {}

    for stmt in body.children:
        if stmt.type not in ('lexical_declaration', 'variable_declaration'):
            continue

        for declarator in stmt.children:
            if declarator.type != 'variable_declarator':
                continue

            name = declarator.child_by_field_name('name')
            value = declarator.child_by_field_name('value')

            if name is not None and name.type == 'identifier' and (value is not None) and (value.type == 'object'):
                object_bindings[read(name, source)] = declarator
    contained_owners: set[str] = set()

    for stmt in body.children:
        if stmt.type != 'expression_statement':
            continue

        assign = next((c for c in stmt.children if c.type == 'assignment_expression'), None)

        if assign is None:
            continue

        val = assign.child_by_field_name('right')

        if val is None or val.type not in JSFUNCTIONS:
            continue

        tgt = jsmember(assign.child_by_field_name('left'), source)

        if tgt is None:
            continue

        as_method = True

        if tgt[0] == 'this':
            owner_nid = this_owner_nid
            as_method = this_as_method
        elif tgt[0] == 'object' and tgt[1] in object_bindings:
            object_name = tgt[1]
            owner_nid = make(this_owner_nid, object_name)
            owner_line = object_bindings[object_name].start_point[0] + 1
            add_node_fn(owner_nid, object_name, owner_line)

            if owner_nid not in contained_owners:
                contained_owners.add(owner_nid)
                add_edge_fn(this_owner_nid, owner_nid, 'contains', owner_line)
        else:
            continue

        m_name = tgt[2]
        m_line = stmt.start_point[0] + 1
        m_nid = make(owner_nid, m_name)

        if as_method:
            add_node_fn(m_nid, f'.{m_name}()', m_line)
            add_edge_fn(owner_nid, m_nid, 'method', m_line)
        else:
            add_node_fn(m_nid, f'{m_name}()', m_line)
            add_edge_fn(owner_nid, m_nid, 'contains', m_line)
        m_body = val.child_by_field_name('body')

        if m_body:
            function_bodies.append((m_nid, m_body))


def jsexports(program_node, source: bytes) -> set[str]:
    if program_node is None or program_node.type != 'program':
        return set()

    exported = set()

    for child in program_node.children:
        if child.type == 'export_statement':
            for c in child.children:
                if c.type == 'identifier':
                    exported.add(read(c, source))
                elif c.type == 'export_clause':
                    for sc in c.children:
                        if sc.type == 'export_specifier':
                            name_node = sc.child_by_field_name('name')

                            if name_node:
                                exported.add(read(name_node, source))
                elif c.type in ('variable_declaration', 'lexical_declaration'):
                    for decl in c.children:
                        if decl.type == 'variable_declarator':
                            name_node = decl.child_by_field_name('name')

                            if name_node and name_node.type == 'identifier':
                                exported.add(read(name_node, source))
        elif child.type in ('variable_declaration', 'lexical_declaration'):
            for decl in child.children:
                if decl.type != 'variable_declarator':
                    continue

                name_node = decl.child_by_field_name('name')
                val_node = decl.child_by_field_name('value')

                if name_node and name_node.type == 'identifier' and val_node:
                    name_str = read(name_node, source)
                    curr = val_node

                    while curr and curr.type == 'assignment_expression':
                        left = curr.child_by_field_name('left')

                        if left:
                            left_str = read(left, source)

                            if left_str in ('module.exports', 'exports') or left_str.startswith(('module.exports.', 'exports.')):
                                exported.add(name_str)
                                break

                        curr = curr.child_by_field_name('right')

                    if curr:
                        val_str = read(curr, source)

                        if val_str in ('module.exports', 'exports'):
                            exported.add(name_str)
        elif child.type == 'expression_statement':
            assign = next((c for c in child.children if c.type == 'assignment_expression'), None)

            if assign:
                left = assign.child_by_field_name('left')
                right = assign.child_by_field_name('right')

                if left:
                    left_str = read(left, source)

                    if left_str in ('module.exports', 'exports') or left_str.startswith(('module.exports.', 'exports.')):
                        curr = right

                        while curr and curr.type == 'assignment_expression':
                            l = curr.child_by_field_name('left')

                            if l and l.type == 'identifier':
                                exported.add(read(l, source))
                            curr = curr.child_by_field_name('right')

                        if curr and curr.type == 'identifier':
                            exported.add(read(curr, source))
                        elif curr and curr.type == 'object':
                            for prop in curr.children:
                                if prop.type == 'shorthand_property_identifier':
                                    exported.add(read(prop, source))
                                elif prop.type == 'pair':
                                    v = prop.child_by_field_name('value')

                                    if v and v.type == 'identifier':
                                        exported.add(read(v, source))

    return exported


def jswalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, callable_def_nids: set | None=None, local_bound_names: dict | None=None, closure_locals_by_body: dict | None=None, config=None) -> bool:
    if node.type == 'expression_statement':
        call_stmt = next((c for c in node.children if c.type in ('call_expression', 'new_expression')), None)

        if call_stmt is not None:
            _stmt_closures: list = []
            jsclosures(call_stmt, _stmt_closures)

            for closure in _stmt_closures:
                closure_body = closure.child_by_field_name('body')

                if closure_body is not None:
                    jsscan(closure_body, file_nid, source, this_as_method=False, add_node_fn=add_node_fn, add_edge_fn=add_edge_fn, function_bodies=function_bodies)
                    function_bodies.append((file_nid, closure_body))
        assign = next((c for c in node.children if c.type == 'assignment_expression'), None)

        if assign is not None:
            value = assign.child_by_field_name('right')

            if value is not None:
                target = jsmember(assign.child_by_field_name('left'), source)

                if target is not None:
                    kind, owner_name, member_name = target
                    line = node.start_point[0] + 1

                    if value.type in JSFUNCTIONS:
                        handled = False

                        if kind == 'exports':
                            nid = make(stem, member_name)
                            add_node_fn(nid, f'{member_name}()', line)
                            add_edge_fn(file_nid, nid, 'contains', line)
                            handled = True
                        elif kind == 'prototype':
                            owner_nid = make(stem, owner_name)
                            nid = make(owner_nid, member_name)
                            add_node_fn(nid, f'.{member_name}()', line)
                            add_edge_fn(owner_nid, nid, 'method', line)
                            handled = True
                        elif kind == 'object':
                            is_exported = False

                            if node.parent is not None and node.parent.type == 'program':
                                exported_names = jsexports(node.parent, source)
                                is_exported = owner_name in exported_names

                            if is_exported:
                                owner_nid = make(stem, owner_name)
                                nid = make(owner_nid, member_name)

                                if owner_nid not in seen_ids:
                                    add_node_fn(owner_nid, owner_name, line)
                                    add_edge_fn(file_nid, owner_nid, 'contains', line)
                                add_node_fn(nid, f'.{member_name}()', line)
                                add_edge_fn(owner_nid, nid, 'method', line)
                                handled = True

                        if handled:
                            if callable_def_nids is not None:
                                callable_def_nids.add(nid)

                            if local_bound_names is not None:
                                local_bound_names[nid] = jslocals(value, source)
                            body = value.child_by_field_name('body')

                            if body:
                                function_bodies.append((nid, body))

                            return True
                    elif kind == 'exports':
                        inner = value

                        while inner is not None and inner.type in ('as_expression', 'satisfies_expression'):
                            inner = inner.named_children[0] if inner.named_children else None

                        if inner is not None and inner.type in ('call_expression', 'new_expression'):
                            closures: list = []
                            jsclosures(inner, closures)

                            if closures:
                                nid = make(stem, member_name)
                                add_node_fn(nid, f'{member_name}()', line)
                                add_edge_fn(file_nid, nid, 'contains', line)

                                if callable_def_nids is not None:
                                    callable_def_nids.add(nid)

                                for closure in closures:
                                    body = closure.child_by_field_name('body')

                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = jslocals(closure, source)
                                        function_bodies.append((nid, body))

                                return True

    if parent_class_nid and node.type in ('field_definition', 'public_field_definition'):
        prop = node.child_by_field_name('property') or node.child_by_field_name('name')
        value = node.child_by_field_name('value')

        if prop is not None and value is not None and (value.type in JSFUNCTIONS):
            field_name = read(prop, source)

            if field_name:
                line = node.start_point[0] + 1
                nid = make(parent_class_nid, field_name)
                add_node_fn(nid, f'.{field_name}()', line)
                add_edge_fn(parent_class_nid, nid, 'method', line)

                if callable_def_nids is not None:
                    callable_def_nids.add(nid)

                if local_bound_names is not None:
                    local_bound_names[nid] = jslocals(value, source)
                body = value.child_by_field_name('body')

                if body:
                    function_bodies.append((nid, body))

                return True

    if node.type in ('lexical_declaration', 'variable_declaration'):
        require_found = jsrequire(node, source, file_nid, stem, edges, str_path)
        parent = node.parent
        is_exported = parent is not None and parent.type == 'export_statement'
        is_module_level = parent is not None and (parent.type == 'program' or (is_exported and parent.parent is not None and (parent.parent.type == 'program')))
        arrow_found = False
        const_found = False

        if node.type == 'lexical_declaration' and is_module_level:
            for child in node.children:
                if child.type == 'variable_declarator':
                    value = child.child_by_field_name('value')
                    name_node = child.child_by_field_name('name')
                    is_exported_scalar_binding = is_exported and name_node is not None and (name_node.type == 'identifier') and bool(normalize(read(name_node, source)))

                    if value and value.type in JSFUNCTIONS:
                        if name_node:
                            func_name = read(name_node, source)
                            line = child.start_point[0] + 1

                            if not normalize(func_name):
                                continue

                            func_nid = make(stem, func_name)
                            add_node_fn(func_nid, f'{func_name}()', line)
                            add_edge_fn(file_nid, func_nid, 'contains', line)

                            if callable_def_nids is not None:
                                callable_def_nids.add(func_nid)

                            if local_bound_names is not None:
                                local_bound_names[func_nid] = jslocals(value, source)
                            body = value.child_by_field_name('body')

                            if body:
                                function_bodies.append((func_nid, body))
                                jsnested(body, func_nid, source=source, config=config, addnode=add_node_fn, addedge=add_edge_fn, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies)
                                jsscan(body, func_nid, source, this_as_method=True, add_node_fn=add_node_fn, add_edge_fn=add_edge_fn, function_bodies=function_bodies)
                            arrow_found = True
                    elif value and (is_exported_scalar_binding or value.type in ('object', 'array', 'as_expression', 'satisfies_expression', 'call_expression', 'new_expression')):
                        if name_node is not None and name_node.type == 'object_pattern' and is_exported:
                            line = child.start_point[0] + 1

                            for prop in name_node.named_children:
                                if prop.type == 'shorthand_property_identifier_pattern':
                                    export_name = read(prop, source)
                                elif prop.type == 'pair_pattern':
                                    key_node = prop.child_by_field_name('key')
                                    export_name = read(key_node, source) if key_node else None
                                else:
                                    export_name = None

                                if not export_name or not normalize(export_name):
                                    continue

                                prop_nid = make(stem, export_name)
                                add_node_fn(prop_nid, export_name, line)
                                add_edge_fn(file_nid, prop_nid, 'contains', line)
                            const_found = True
                        elif name_node:
                            const_name = read(name_node, source)
                            line = child.start_point[0] + 1
                            const_nid = make(stem, const_name)
                            add_node_fn(const_nid, const_name, line)
                            add_edge_fn(file_nid, const_nid, 'contains', line)
                            const_found = True
                            inner = value

                            while inner is not None and inner.type in ('as_expression', 'satisfies_expression'):
                                inner = inner.named_children[0] if inner.named_children else None

                            if inner is not None and inner.type in ('call_expression', 'new_expression'):
                                closures: list = []
                                jsclosures(inner, closures)

                                for closure in closures:
                                    body = closure.child_by_field_name('body')

                                    if body:
                                        if closure_locals_by_body is not None:
                                            closure_locals_by_body[id(body)] = jslocals(closure, source)
                                        function_bodies.append((const_nid, body))

        if arrow_found:
            return True

        if const_found:
            return True

        if require_found:
            return True

    return False


def tswalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, walk_fn) -> bool:
    if parent_class_nid and node.parent is not None and (node.parent.type == 'enum_body') and (node.type in ('property_identifier', 'enum_assignment')):
        name_node = node if node.type == 'property_identifier' else node.child_by_field_name('name')
        member_name = ''

        if name_node is not None:
            member_name = read(name_node, source)

            if name_node.type == 'string':
                member_name = member_name.strip('\'"`')

        if member_name:
            line = node.start_point[0] + 1
            member_nid = make(parent_class_nid, member_name)

            if member_nid not in seen_ids:
                add_node_fn(member_nid, member_name, line)
                add_edge_fn(parent_class_nid, member_nid, 'case_of', line)

        if node.type == 'enum_assignment':
            value_node = node.child_by_field_name('value')

            if value_node is not None:
                walk_fn(value_node, parent_class_nid)

        return True

    if node.is_named and node.type in ('internal_module', 'module'):
        name_node = node.child_by_field_name('name')

        if name_node is None:
            for child in node.children:
                if child.is_named and child.type in ('identifier', 'nested_identifier', 'string'):
                    name_node = child
                    break

        body = node.child_by_field_name('body')

        if body is None:
            for child in node.children:
                if child.type == 'statement_block':
                    body = child
                    break

        if name_node is not None:
            ns_name = read(name_node, source)

            if name_node.type == 'string':
                ns_name = ns_name.strip('\'"`')

            if ns_name:
                ns_nid = make(stem, ns_name)
                line = node.start_point[0] + 1
                add_node_fn(ns_nid, ns_name, line)
                add_edge_fn(file_nid, ns_nid, 'contains', line)

        if body is not None:
            for child in body.children:
                walk_fn(child, parent_class_nid)

        return True

    return False


def csscope(node) -> str:
    row, column = node.start_point

    return f's{row}:{column}'


def csnamespace(node, source: bytes) -> str:
    name_node = node.child_by_field_name('name')

    if name_node is not None:
        return read(name_node, source).strip()

    for child in node.children:
        if child.type in ('identifier', 'qualified_name'):
            return read(child, source).strip()

    return ''


def cswalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, walk_fn, namespace_stack: list[str], scope_stack: list[str]) -> bool:
    if node.type == 'enum_member_declaration' and parent_class_nid:
        name_node = node.child_by_field_name('name')

        if name_node is None:
            return True

        member_name = read(name_node, source)

        if not member_name:
            return True

        line = node.start_point[0] + 1
        member_nid = make(parent_class_nid, member_name)

        if member_nid not in seen_ids:
            add_node_fn(member_nid, member_name, line)
            add_edge_fn(parent_class_nid, member_nid, 'case_of', line)

        return True

    if node.type == 'namespace_declaration':
        ns_name = csnamespace(node, source)
        pushed = False

        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(csscope(node))
            pushed = True
            ns_label = '.'.join(namespace_stack)
            ns_nid = csnsid(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type='namespace', metadata={'kind': 'csharp_namespace'})
            add_edge_fn(file_nid, ns_nid, 'contains', line)
        body = node.child_by_field_name('body')

        if body:
            try:
                for child in body.children:
                    walk_fn(child, parent_class_nid)
            finally:
                if pushed:
                    namespace_stack.pop()
                    scope_stack.pop()
        elif pushed:
            namespace_stack.pop()
            scope_stack.pop()

        return True

    if node.type == 'file_scoped_namespace_declaration':
        ns_name = csnamespace(node, source)

        if ns_name:
            namespace_stack.append(ns_name)
            scope_stack.append(csscope(node))
            ns_label = '.'.join(namespace_stack)
            ns_nid = csnsid(ns_label)
            line = node.start_point[0] + 1
            add_node_fn(ns_nid, ns_label, line, node_type='namespace', metadata={'kind': 'csharp_namespace'})
            add_edge_fn(file_nid, ns_nid, 'contains', line)

        return True

    if parent_class_nid and node.type.startswith('preproc_'):
        for child in node.children:
            walk_fn(child, parent_class_nid)

        return True

    return False


def swiftwalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, ensure_named_node_fn) -> bool:
    if node.type == 'enum_entry' and parent_class_nid:
        line = node.start_point[0] + 1

        for child in node.children:
            if child.type == 'simple_identifier':
                case_name = read(child, source)
                case_nid = make(parent_class_nid, case_name)
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, 'case_of', line)

        for child in node.children:
            if child.type != 'enum_type_parameters':
                continue

            for grand in child.children:
                if not grand.is_named:
                    continue

                refs: list[tuple[str, str]] = []
                swifttypes(grand, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'type'
                    target_nid = ensure_named_node_fn(ref_name, line)

                    if target_nid != parent_class_nid:
                        add_edge_fn(parent_class_nid, target_nid, 'references', line, context=ctx)

        return True

    return False


def scalawalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, walk_fn) -> bool:
    if node.type == 'enum_case_definitions' and parent_class_nid:
        for case in node.children:
            if case.type not in ('simple_enum_case', 'full_enum_case'):
                continue

            name_node = next((c for c in case.children if c.type == 'identifier'), None)

            if name_node is None:
                continue

            case_name = read(name_node, source)

            if not case_name:
                continue

            line = case.start_point[0] + 1
            case_nid = make(parent_class_nid, case_name)

            if case_nid not in seen_ids:
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, 'case_of', line)

        return True

    return False


def cppwalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn) -> bool:
    if node.type == 'enumerator' and parent_class_nid:
        name_node = node.child_by_field_name('name')

        if name_node is None:
            name_node = next((c for c in node.children if c.type == 'identifier'), None)

        if name_node is None:
            return True

        member_name = read(name_node, source)

        if member_name:
            line = node.start_point[0] + 1
            member_nid = make(parent_class_nid, member_name)

            if member_nid not in seen_ids:
                add_node_fn(member_nid, member_name, line)
                add_edge_fn(parent_class_nid, member_nid, 'case_of', line)

        return True

    return False


def phpwalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn) -> bool:
    if node.type == 'enum_case' and parent_class_nid:
        name_node = node.child_by_field_name('name')

        if name_node is None:
            name_node = next((c for c in node.children if c.type == 'name'), None)

        if name_node is None:
            return True

        case_name = read(name_node, source)

        if case_name:
            line = node.start_point[0] + 1
            case_nid = make(parent_class_nid, case_name)

            if case_nid not in seen_ids:
                add_node_fn(case_nid, case_name, line)
                add_edge_fn(parent_class_nid, case_nid, 'case_of', line)

        return True

    return False


def javawalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, walk_fn) -> bool:
    if node.type == 'enum_constant' and parent_class_nid:
        name_node = node.child_by_field_name('name')

        if name_node is None:
            return True

        const_name = read(name_node, source)
        line = node.start_point[0] + 1
        const_nid = make(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, 'case_of', line)

        for child in node.children:
            if child.type == 'class_body':
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)

        return True

    return False


def ktwalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, add_node_fn, add_edge_fn, walk_fn) -> bool:
    if node.type == 'enum_entry' and parent_class_nid:
        name_node = None

        for child in node.children:
            if child.type in ('simple_identifier', 'identifier'):
                name_node = child
                break

        if name_node is None:
            return True

        const_name = read(name_node, source)
        line = node.start_point[0] + 1
        const_nid = make(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, 'case_of', line)

        for child in node.children:
            if child.type == 'class_body':
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)

        return True

    return False


def ktpackage(root, source: bytes) -> str | None:
    for child in root.children:
        if child.type != 'package_header':
            continue

        for c in child.children:
            if c.type in ('qualified_identifier', 'identifier'):
                pkg = read(c, source).strip()

                return pkg or None

        return None

    return None


def ktsegments(nav, source: bytes) -> list[str] | None:
    segments: list[str] = []
    node = nav

    while node is not None and node.type == 'navigation_expression':
        named = [c for c in node.children if c.is_named]

        if len(named) != 2:
            return None

        head, tail = named

        if tail.type not in ('simple_identifier', 'identifier'):
            return None

        segments.append(read(tail, source))
        node = head

    if node is None or node.type not in ('simple_identifier', 'identifier'):
        return None

    segments.append(read(node, source))
    segments.reverse()

    return segments


def firsterror(root) -> int:
    node = root

    while True:
        if node.type == 'ERROR' or node.is_missing:
            return node.start_point[0] + 1

        child = next((c for c in node.children if c.has_error), None)

        if child is None:
            return node.start_point[0] + 1

        node = child


def multilineerror(root) -> bool:
    stack = [root]

    while stack:
        n = stack.pop()

        if n.type == 'ERROR' and n.end_point[0] > n.start_point[0]:
            return True

        stack.extend((c for c in n.children if c.has_error))

    return False


def csbare(name_node, source: bytes) -> str:
    if name_node.type == 'generic_name':
        for child in name_node.children:
            if child.type == 'identifier':
                return read(child, source)

    return read(name_node, source)


def csparts(fn_node):
    if fn_node is None:
        return None

    if fn_node.type == 'member_access_expression':
        return (fn_node.child_by_field_name('name'), fn_node.child_by_field_name('expression'))

    if fn_node.type == 'conditional_access_expression':
        for child in fn_node.named_children:
            if child.type == 'member_binding_expression':
                return (child.child_by_field_name('name'), fn_node.child_by_field_name('condition'))

    return None


def cstypename(node, source: bytes) -> tuple[str, bool, str] | None:
    if node is None:
        return None

    if node.type in ('identifier', 'predefined_type'):
        return (read(node, source), False, '')

    if node.type == 'qualified_name':
        prefix, _, tail = read(node, source).rpartition('.')
        tail = tail.split('<', 1)[0]

        return (tail, True, prefix)

    if node.type == 'generic_name':
        name_node = node.child_by_field_name('name')

        if name_node is not None:
            qualified = name_node.type == 'qualified_name'
            prefix, _, tail = read(name_node, source).rpartition('.')

            return (tail, qualified, prefix if qualified else '')

    for child in node.children:
        if not child.is_named:
            continue

        result = cstypename(child, source)

        if result:
            return result

    return None


def rubynewclass(node, source: bytes) -> str | None:
    if node is None or node.type != 'call':
        return None

    recv = node.child_by_field_name('receiver')
    meth = node.child_by_field_name('method')

    if recv is None or meth is None:
        return None

    if recv.type != 'constant' or read(meth, source) != 'new':
        return None

    return read(recv, source)


def pyclassref(node, source: bytes) -> str | None:
    if node is None:
        return None

    kind = node.type

    if kind == 'type':
        named = [c for c in node.children if c.is_named]

        return pyclassref(named[0], source) if len(named) == 1 else None

    if kind == 'identifier':
        name = read(node, source)

        return name if name[:1].isupper() else None

    if kind == 'attribute':
        return pyclassref(node.child_by_field_name('attribute'), source)

    if kind == 'string':
        name = read(node, source).strip('"\'').split('.')[-1].strip()

        return name if name.isidentifier() and name[:1].isupper() else None

    if kind == 'subscript':
        value = node.child_by_field_name('value')
        subs = node.children_by_field_name('subscript')

        if value is not None and read(value, source).split('.')[-1] == 'Optional' and (len(subs) == 1):
            return pyclassref(subs[0], source)

        return None

    if kind == 'generic_type':
        named = [c for c in node.children if c.is_named]

        if len(named) == 2 and named[0].type == 'identifier' and (read(named[0], source) == 'Optional') and (named[1].type == 'type_parameter'):
            inner = [c for c in named[1].children if c.is_named]

            return pyclassref(inner[0], source) if len(inner) == 1 else None

        return None

    if kind == 'binary_operator':
        left, right = (node.child_by_field_name('left'), node.child_by_field_name('right'))

        if right is not None and right.type == 'none':
            return pyclassref(left, source)

        if left is not None and left.type == 'none':
            return pyclassref(right, source)

    return None


def pylocalclasses(body_node, source: bytes) -> dict[str, str | None]:
    bindings: dict[str, str | None] = {}
    poison_only = False

    def bind(name: str, cls: str | None) -> None:
        if poison_only or cls is None or bindings.get(name, cls) != cls:
            bindings[name] = None
        elif name not in bindings:
            bindings[name] = cls

    def poison(target) -> None:
        names: set[str] = set()
        pytargets(target, source, names)

        for name in names:
            bindings[name] = None

    def poisonidents(node) -> None:
        stack = [node]

        while stack:
            cur = stack.pop()

            if cur.type == 'identifier':
                bindings[read(cur, source)] = None
            stack.extend(cur.children)
    func = body_node.parent
    params = func.child_by_field_name('parameters') if func is not None and func.type == 'function_definition' else None

    for name in pynames(params, source):
        bindings[name] = None

    if params is not None:
        for child in params.children:
            if child.type in ('typed_parameter', 'typed_default_parameter'):
                name_n = child.child_by_field_name('name') or next((c for c in child.children if c.type == 'identifier'), None)

                if name_n is not None:
                    name = read(name_n, source)
                    bindings.pop(name, None)
                    bind(name, pyclassref(child.child_by_field_name('type'), source))

    def visit(n) -> None:
        nonlocal poison_only

        for child in n.children:
            if child.type == 'function_definition':
                stack = [child]

                while stack:
                    cur = stack.pop()

                    if cur.type == 'nonlocal_statement':
                        poisonidents(cur)
                    stack.extend(cur.children)
                continue

            if child.type in ('lambda', 'class_definition'):
                params = child.child_by_field_name('parameters')

                if child.type == 'lambda' and params is not None:
                    poisonidents(params)
                outer, poison_only = (poison_only, True)
                visit(child)
                poison_only = outer
                continue

            if child.type in ('case_pattern', 'import_statement', 'import_from_statement'):
                poisonidents(child)
                continue

            if child.type == 'assignment':
                left = child.child_by_field_name('left')

                if left is not None and left.type == 'identifier':
                    name = read(left, source)
                    annotation = child.child_by_field_name('type')
                    right = child.child_by_field_name('right')

                    if annotation is not None:
                        bind(name, pyclassref(annotation, source))
                    elif right is not None and right.type == 'call':
                        bind(name, pyclassref(right.child_by_field_name('function'), source))
                    else:
                        bindings[name] = None
                else:
                    poison(left)
            elif child.type == 'augmented_assignment':
                poison(child.child_by_field_name('left'))
            elif child.type == 'as_pattern':
                alias = child.child_by_field_name('alias')
                target = next((c for c in alias.children if c.type == 'identifier'), None) if alias is not None else None
                value = next((c for c in child.children if c.is_named), None)

                if target is not None:
                    name = read(target, source)

                    if child.parent is not None and child.parent.type == 'with_item' and (value is not None) and (value.type == 'call'):
                        bind(name, pyclassref(value.child_by_field_name('function'), source))
                    else:
                        bindings[name] = None
                elif alias is not None:
                    poisonidents(alias)
            elif child.type in ('for_statement', 'for_in_clause'):
                poison(child.child_by_field_name('left'))
            elif child.type == 'named_expression':
                poison(child.child_by_field_name('name'))
            elif child.type in ('global_statement', 'nonlocal_statement'):
                for c in child.children:
                    if c.type == 'identifier':
                        bindings[read(c, source)] = None
            visit(child)
    visit(body_node)

    return bindings


def rubylocalclasses(body_node, source: bytes) -> dict[str, str | None]:
    bindings: dict[str, str | None] = {}
    boundary = {'method', 'singleton_method'}

    def visit(n) -> None:
        for child in n.children:
            if child.type in boundary:
                continue

            if child.type == 'assignment':
                left = child.child_by_field_name('left')
                right = child.child_by_field_name('right')

                if left is not None and left.type == 'identifier':
                    var = read(left, source)
                    cls = rubynewclass(right, source) if right is not None else None

                    if cls is None:
                        if var in bindings:
                            bindings[var] = None
                    elif var in bindings:
                        if bindings[var] != cls:
                            bindings[var] = None
                    else:
                        bindings[var] = cls
            visit(child)
    visit(body_node)

    return bindings


def rubylocals(body_node, source: bytes) -> frozenset[str]:
    names: set[str] = set()
    boundary = {'method', 'singleton_method'}
    param_lists = {'method_parameters', 'block_parameters', 'lambda_parameters', 'bare_parameters'}
    param_wrappers = {'optional_parameter', 'splat_parameter', 'hash_splat_parameter', 'keyword_parameter', 'block_parameter', 'destructured_parameter', 'forward_parameter'}

    def firstident(n):
        if n.type == 'identifier':
            return n

        for c in n.children:
            found = firstident(c)

            if found is not None:
                return found

        return None

    def addparams(param_list) -> None:
        for child in param_list.children:
            if child.type == 'identifier':
                names.add(read(child, source))
            elif child.type in param_wrappers:
                ident = firstident(child)

                if ident is not None:
                    names.add(read(ident, source))

    def addtargets(n) -> None:
        if n.type == 'identifier':
            names.add(read(n, source))
            return

        for c in n.children:
            addtargets(c)
    parent = body_node.parent

    if parent is not None:
        for child in parent.children:
            if child.type in param_lists:
                addparams(child)

    def visit(n) -> None:
        for child in n.children:
            if child.type in boundary:
                continue

            if child.type in ('assignment', 'operator_assignment'):
                left = child.child_by_field_name('left')

                if left is not None:
                    if left.type == 'identifier':
                        names.add(read(left, source))
                    elif left.type not in ('call', 'element_reference'):
                        addtargets(left)
            elif child.type in param_lists:
                addparams(child)
            elif child.type == 'exception_variable':
                ident = firstident(child)

                if ident is not None:
                    names.add(read(ident, source))
            elif child.type == 'for':
                for sub in child.children:
                    if sub.type == 'identifier':
                        names.add(read(sub, source))
                    elif sub.type == 'left_assignment_list':
                        addtargets(sub)
            visit(child)
    visit(body_node)

    return frozenset(names)


def rubyconstlast(node, source: bytes) -> str:
    if node is None:
        return ''

    if node.type == 'constant':
        return read(node, source)

    if node.type == 'scope_resolution':
        consts = [c for c in node.children if c.type == 'constant']

        if consts:
            return read(consts[-1], source)

    return ''


def rubyconstfull(node, source: bytes) -> str:
    if node is None or node.type not in ('constant', 'scope_resolution'):
        return ''

    return read(node, source).strip()

RUBYMUTATORS = frozenset({'alias_method', 'attr', 'attr_accessor', 'attr_reader', 'attr_writer', 'define_method', 'define_singleton_method', 'include', 'extend', 'module_function', 'prepend', 'refine', 'remove_method', 'undef_method', 'using'})
RUBYOWNERS = RUBYMUTATORS | frozenset({'class_eval', 'class_exec', 'module_eval', 'module_exec'})


def rubybarrier(body_node, source: bytes) -> bool:
    method_scopes = {'method', 'singleton_method'}

    def visit(node, *, direct: bool=False) -> bool:
        for child in node.children:
            if child.type in {'class', 'module'}:
                continue

            if child.type == 'singleton_class':
                if not direct:
                    return True

                continue

            if child.type in method_scopes:
                if not direct:
                    return True

                continue

            if child.type in {'alias', 'undef'}:
                return True

            if child.type == 'call':
                receiver = child.child_by_field_name('receiver')
                method = child.child_by_field_name('method')

                if (receiver is None or read(receiver, source) == 'self') and method is not None and (read(method, source) in RUBYMUTATORS):
                    return True

            if visit(child):
                return True

        return False

    return visit(body_node, direct=True)


def rubyrefinement(root_node, source: bytes) -> bool:

    def visit(node) -> bool:
        if node.type == 'call':
            method = node.child_by_field_name('method')

            if method is not None and read(method, source) in {'refine', 'using'}:
                return True

        return any((visit(child) for child in node.children))

    return visit(root_node)


def rubyowners(root_node, source: bytes) -> list[str]:
    owners: set[str] = set()

    def constrefs(node) -> set[str]:
        if node.type in {'constant', 'scope_resolution'}:
            raw = rubyconstfull(node, source)

            return {raw} if raw else set()

        if node.type not in {'left_assignment_list', 'parenthesized_statements', 'splat_argument'}:
            return set()

        refs: set[str] = set()

        for child in node.children:
            if child.is_named:
                refs.update(constrefs(child))

        return refs

    def aliasrefs(node, *, allow_array: bool=False) -> set[str]:
        if node.type in {'constant', 'scope_resolution'}:
            raw = rubyconstfull(node, source)

            return {raw} if raw else set()

        if node.type in {'parenthesized_statements', 'right_assignment_list'} or (allow_array and node.type == 'array'):
            refs: set[str] = set()

            for child in node.children:
                if child.is_named:
                    refs.update(aliasrefs(child, allow_array=allow_array))

            return refs

        return set()

    def prefixmarker(raw: str) -> str:
        return f'{raw}::*'

    def visit(node) -> None:
        if node.type in {'assignment', 'operator_assignment'}:
            left = node.child_by_field_name('left')
            lhs_refs = constrefs(left) if left is not None else set()

            if lhs_refs:
                owners.update((prefixmarker(raw) for raw in lhs_refs))
            right = node.child_by_field_name('right')

            if node.type == 'assignment' and lhs_refs and (right is not None):
                owners.update((prefixmarker(raw) for raw in aliasrefs(right, allow_array=left is not None and left.type == 'left_assignment_list')))

        if node.type in {'class', 'module'}:
            name = node.child_by_field_name('name')

            if name is not None and name.type == 'scope_resolution':
                raw = rubyconstfull(name, source)

                if raw:
                    owners.add(raw)

        if node.type == 'singleton_method':
            owner = node.child_by_field_name('object')

            if owner is not None and read(owner, source) != 'self':
                raw = rubyconstfull(owner, source)

                if raw:
                    owners.add(raw)
            return

        if node.type == 'singleton_class':
            owner = node.child_by_field_name('value')

            if owner is not None and read(owner, source) != 'self':
                raw = rubyconstfull(owner, source)

                if raw:
                    owners.add(raw)

        if node.type == 'call':
            receiver = node.child_by_field_name('receiver')
            method = node.child_by_field_name('method')

            if receiver is not None and receiver.type in {'constant', 'scope_resolution'} and (method is not None) and (read(method, source) in RUBYOWNERS):
                raw = rubyconstfull(receiver, source)

                if raw:
                    owners.add(raw)

        if node.type == 'method':
            return

        for child in node.children:
            visit(child)
    visit(root_node)

    return sorted(owners)

RUBYFACTORIES = frozenset({('Struct', 'new'), ('Class', 'new'), ('Data', 'define')})


def rubywalk(node, source: bytes, file_nid: str, stem: str, str_path: str, nodes: list, edges: list, seen_ids: set, function_bodies: list, parent_class_nid: str | None, addnode, addedge, walk, callable_def_nids: set, callable_class_nids: set, ruby_namespace: list, ruby_lexical_scopes: list) -> bool:
    if node.type != 'assignment':
        return False

    left = node.child_by_field_name('left')
    right = node.child_by_field_name('right')

    if left is None or right is None or left.type != 'constant' or (right.type != 'call'):
        return False

    recv = right.child_by_field_name('receiver')
    meth = right.child_by_field_name('method')

    if recv is None or meth is None or recv.type != 'constant':
        return False

    if (read(recv, source), read(meth, source)) not in RUBYFACTORIES:
        return False

    const_name = read(left, source)

    if not const_name:
        return False

    const_segments = const_name.split('::')
    const_name = '::'.join(ruby_namespace + const_segments)
    line = node.start_point[0] + 1
    class_nid = make(stem, const_name)
    addnode(class_nid, const_name, line, metadata={'ruby_lookup_unsafe': True})
    callable_def_nids.add(class_nid)
    callable_class_nids.add(class_nid)
    addedge(file_nid, class_nid, 'contains', line)

    if read(recv, source) == 'Class':
        args = next((c for c in right.children if c.type == 'argument_list'), None)

        if args is not None:
            for arg in args.children:
                if arg.type in ('constant', 'scope_resolution'):
                    base = rubyconstlast(arg, source)
                    raw_base = rubyconstfull(arg, source)

                    if base:
                        base_nid = make(stem, base)

                        if base_nid not in seen_ids:
                            base_nid = make(base)

                            if base_nid not in seen_ids:
                                nodes.append({'id': base_nid, 'label': base, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': str_path})
                                seen_ids.add(base_nid)
                        addedge(class_nid, base_nid, 'inherits', line, metadata={'ruby_superclass_ref': raw_base, 'ruby_lexical_scopes': list(reversed(ruby_lexical_scopes))})
                    break

    block = next((c for c in right.children if c.type in ('do_block', 'block')), None)

    if block is not None:
        body = next((c for c in block.children if c.type == 'body_statement'), block)
        ruby_namespace.extend(const_segments)
        ruby_lexical_scopes.append(const_name)

        try:
            for child in body.children:
                walk(child, parent_class_nid=class_nid)
        finally:
            ruby_lexical_scopes.pop()
            del ruby_namespace[-len(const_segments):]

    return True


def luarequire(node, source: bytes) -> bool:
    name_node = node.child_by_field_name('name')

    if name_node is None or name_node.type != 'identifier':
        return False

    return read(name_node, source) == 'require'

PHPVERBS = frozenset({'get', 'post', 'put', 'patch', 'delete', 'options', 'any', 'match', 'map'})
PHPCONSTRUCTS = frozenset({'array', 'die', 'empty', 'eval', 'exit', 'isset', 'list', 'unset'})


def phproute(closure_node, src: bytes) -> str | None:
    prefixes = []
    verb = None
    curr = closure_node.parent

    while curr is not None:
        if curr.type in ('function_definition', 'method_declaration', 'class_declaration'):
            break

        if curr.type == 'argument':
            arg_list = curr.parent

            if arg_list is not None and arg_list.type == 'arguments':
                call = arg_list.parent

                if call is not None and call.type in ('member_call_expression', 'function_call_expression', 'scoped_call_expression'):
                    name_node = call.child_by_field_name('name')

                    if name_node is None:
                        name_node = call.child_by_field_name('function')
                    raw_method = (read(name_node, src) if name_node else '').lower()
                    path_text = None

                    for sibling in arg_list.children:
                        if sibling is curr:
                            break

                        target = sibling

                        if sibling.type == 'argument':
                            for c in sibling.children:
                                if c.type in ('string', 'encapsed_string'):
                                    target = c
                                    break

                        if target.type in ('string', 'encapsed_string'):
                            path_text = read(target, src).strip('\'"')
                            break

                    if verb is None:
                        if path_text is not None and path_text.startswith('/') and (raw_method in PHPVERBS):
                            verb = raw_method.upper()
                            prefixes.append(path_text)
                        else:
                            return None
                    elif path_text is not None and path_text:
                        prefixes.append(path_text if path_text.startswith('/') else '/' + path_text)
                    fluent = call.child_by_field_name('object')

                    while fluent is not None and fluent.type == 'member_call_expression':
                        f_args = fluent.child_by_field_name('arguments')

                        if f_args:
                            for c in f_args.children:
                                if c.type == 'argument':
                                    for cc in c.children:
                                        if cc.type in ('string', 'encapsed_string'):
                                            f_path = read(cc, src).strip('\'"')

                                            if f_path:
                                                prefixes.append(f_path if f_path.startswith('/') else '/' + f_path)
                                            break

                        fluent = fluent.child_by_field_name('object')
                    curr = call.parent
                    continue
        elif curr.type in ('anonymous_function', 'arrow_function'):
            if verb is None:
                return None

            curr = curr.parent
            continue

        curr = curr.parent

    if verb and prefixes:
        full_path = '/' + '/'.join((p.strip('/') for p in reversed(prefixes) if p.strip('/')))

        return f'{verb} {full_path}'

    return None


def generic(path: Path, config: LanguageConfig, *, source_override: bytes | None=None, scan_root: Path | None=None) -> dict:
    try:
        mod = importlib.import_module(config.ts_module)
        from tree_sitter import Language, Parser
        lang_fn = getattr(mod, config.ts_language_fn, None)

        if lang_fn is None:
            lang_fn = getattr(mod, 'language', None)

        if lang_fn is None:
            return {'nodes': [], 'edges': [], 'error': f'No language function in {config.ts_module}'}

        language = Language(lang_fn())
    except ImportError:
        return {'nodes': [], 'edges': [], 'error': f'{config.ts_module} not installed'}
    except TypeError as e:
        hint = f'tree-sitter version mismatch for {config.ts_module}: {e}. Try: pip install --upgrade tree-sitter tree-sitter-languages'

        return {'nodes': [], 'edges': [], 'error': hint}
    except Exception as e:
        return {'nodes': [], 'edges': [], 'error': str(e)}

    try:
        parser = Parser(language)
        source = base.bytes(path) if source_override is None else source_override

        if source and (not source.endswith(b'\n')):
            source = source + b'\n'
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {'nodes': [], 'edges': [], 'error': str(e)}

    stem = base.stem(path)
    str_path = str(path)
    js_external_imports: set[str] = jsimports(root, source, str_path) if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') else set()
    python_receiver_shadowed = pyshadow(root, source) if config.ts_module == 'tree_sitter_python' else None
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    namespace_stack: list[str] = []
    ruby_namespace: list[str] = []
    ruby_lexical_scopes: list[str] = []
    scope_stack: list[str] = []
    function_bodies: list[tuple[str, object]] = []
    callable_def_nids: set[str] = set()
    callable_class_nids: set[str] = set()
    local_bound_names: dict[str, set[str]] = {}
    scope_parents: dict[str, str] = {}
    lexical_nids_by_scope: dict[str, dict[str, str]] = {}
    closure_locals_by_body: dict[int, set[str]] = {}
    php_closure_counts: dict[str, int] = {}
    pending_listen_edges: list[tuple[str, str, int]] = []
    swift_extensions: list[dict] = []
    initializer_nodes: list[tuple[str, object]] = []
    _ruby_mixin_calls: list[dict] = []
    ruby_singleton_context_depth = 0
    ruby_method_kinds: dict[str, set[str]] = {}
    ruby_method_counts: dict[str, int] = {}
    type_table: dict[str, str] = {}
    swift_factory_bindings: dict[str, tuple[str, str]] = {}
    java_field_types: dict[str, dict[str, str]] = {}
    java_method_scopes: dict[int, tuple[object, str]] = {}
    csharp_field_types: dict[str, dict[str, str]] = {}
    csharp_method_scopes: dict[int, tuple[object, str]] = {}
    csharp_interface_names: set[str] = set()

    if config.ts_module == 'tree_sitter_c_sharp':
        csharp_interface_names = csinterfaces(root, source)
    swift_protocol_names: set[str] = set()
    swift_class_names: set[str] = set()

    if config.ts_module == 'tree_sitter_swift':
        swift_protocol_names, swift_class_names = swiftscan(root, source)
    python_underscore_groups: dict[str, set[str]] = {}

    if config.ts_module == 'tree_sitter_python':
        python_underscore_groups = pycollisions(root, source, stem)

    def addnode(nid: str, label: str, line: int, *, node_type: str | None=None, metadata: dict | None=None) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        merged = dict(metadata or {})

        if namespace_stack:
            merged.setdefault('namespace', '.'.join(namespace_stack))

        if scope_stack and node_type != 'namespace':
            merged.setdefault('scope_chain', list(scope_stack))
        node = {'id': nid, 'label': label, 'file_type': 'code', 'source_file': str_path, 'source_location': f'L{line}'}

        if node_type:
            node['type'] = node_type

        if merged:
            node['metadata'] = metadata(merged)
        nodes.append(node)

    def addedge(src: str, tgt: str, relation: str, line: int, context: str | None=None, metadata: dict | None=None) -> None:
        edge = {'source': src, 'target': tgt, 'relation': relation, 'source_file': str_path, 'source_location': f'L{line}'}

        if context:
            edge['context'] = context

        if metadata:
            edge['metadata'] = metadata(metadata)
        edges.append(edge)

    def ensurenode(name: str, line: int) -> str:
        nid = make(stem, '.'.join(namespace_stack), name)

        if nid in seen_ids:
            return nid

        nid = make(name)

        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append({'id': nid, 'label': name, 'file_type': 'code', 'source_file': '', 'source_location': '', 'origin_file': str_path})

        return nid

    file_nid = make(str(path))
    addnode(file_nid, path.name, 1)

    if not source:
        next((n for n in nodes if n['id'] == file_nid))['source_location'] = None

    if config.ts_module == 'tree_sitter_ruby':
        file_node = next((n for n in nodes if n['id'] == file_nid))
        file_metadata = {'ruby_resolution_schema': 1}

        if rubybarrier(root, source) or rubyrefinement(root, source):
            file_metadata['ruby_lookup_unsafe'] = True
        external_owners = rubyowners(root, source)

        if external_owners:
            file_metadata['ruby_external_method_owners'] = external_owners
        file_node['metadata'] = metadata(file_metadata)

    def walk(node, parent_class_nid: str | None=None) -> None:
        nonlocal ruby_singleton_context_depth
        t = node.type

        if config.ts_module == 'tree_sitter_lua' and t == 'function_call' and config.import_handler and luarequire(node, source):
            config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack)
            return

        if t in config.import_types:
            if config.import_handler:
                if config.ts_module == 'tree_sitter_python':
                    imported_modules = config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack, scan_root)
                else:
                    imported_modules = config.import_handler(node, source, file_nid, stem, edges, str_path, scope_stack)

                if imported_modules:
                    line = node.start_point[0] + 1

                    for mod_nid, mod_label in imported_modules:
                        if mod_nid not in seen_ids:
                            seen_ids.add(mod_nid)
                            nodes.append({'id': mod_nid, 'label': mod_label, 'file_type': 'code', 'type': 'module', 'source_file': str_path, 'source_location': f'L{line}'})

            if t == 'export_statement':
                has_source = any((c.type == 'string' for c in node.children))

                if not has_source:
                    for child in node.children:
                        walk(child, parent_class_nid)
            return

        if t in config.class_types:
            name_node = node.child_by_field_name(config.name_field)

            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break

            if not name_node:
                return
            class_name = read(name_node, source)
            ruby_absolute_declaration = config.ts_module == 'tree_sitter_ruby' and class_name.startswith('::')
            ruby_segments: list[str] = []

            if config.ts_module == 'tree_sitter_ruby':
                ruby_segments = class_name.split('::')
                class_name = '::'.join(ruby_namespace + ruby_segments)
            class_nid = make(stem, '.'.join(namespace_stack), class_name)

            if config.ts_module == 'tree_sitter_python' and parent_class_nid:
                class_nid = make(parent_class_nid, class_name)
            line = node.start_point[0] + 1
            metadata = None
            ruby_reopened = config.ts_module == 'tree_sitter_ruby' and class_nid in seen_ids

            if config.ts_module == 'tree_sitter_c_sharp':
                if parent_class_nid:
                    metadata = {'is_nested_type': True}

                if t in ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration') and any((c.type == 'modifier' and read(c, source) == 'partial' for c in node.children)):
                    metadata = dict(metadata or {})
                    metadata['is_partial'] = True

            if config.ts_module == 'tree_sitter_ruby':
                ruby_body = findbody(node, config)

                if ruby_absolute_declaration or ruby_reopened or (ruby_body is not None and rubybarrier(ruby_body, source)):
                    metadata = dict(metadata or {})

                    if ruby_absolute_declaration:
                        metadata['ruby_lookup_unsafe'] = True

                    if ruby_reopened:
                        metadata['ruby_reopened'] = True

                    if ruby_body is not None and rubybarrier(ruby_body, source):
                        metadata['ruby_lookup_unsafe'] = True

            if config.ts_module == 'tree_sitter_python':
                _py_bases = node.child_by_field_name('superclasses')

                if _py_bases is not None and any((c.is_named and c.type not in ('identifier', 'keyword_argument', 'comment') for c in _py_bases.children)):
                    metadata = dict(metadata or {})
                    metadata['python_opaque_bases'] = True
            addnode(class_nid, class_name, line, metadata=metadata)

            if config.ts_module == 'tree_sitter_ruby' and metadata:
                class_node = next((n for n in nodes if n['id'] == class_nid))
                class_metadata = dict(class_node.get('metadata') or {})
                class_metadata.update(metadata)
                class_node['metadata'] = metadata(class_metadata)
            callable_def_nids.add(class_nid)
            callable_class_nids.add(class_nid)

            if parent_class_nid and parent_class_nid != class_nid:
                addedge(parent_class_nid, class_nid, 'contains', line)
            else:
                addedge(file_nid, class_nid, 'contains', line)

            if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                tsedges(node, class_nid, stem, source, ensurenode, addedge)

            if config.ts_module == 'tree_sitter_swift' and any((c.type == 'extension' for c in node.children)):
                swift_extensions.append({'nid': class_nid, 'label': class_name})

            if config.ts_module == 'tree_sitter_python':
                args = node.child_by_field_name('superclasses')

                if args:
                    for arg in args.children:
                        if arg.type == 'identifier':
                            base = read(arg, source)
                            base_nid = ensurenode(base, line)
                            addedge(class_nid, base_nid, 'inherits', line)

            if config.ts_module == 'tree_sitter_swift':
                swift_kind = swiftkeyword(node) if t == 'class_declaration' else 'protocol'
                seen_swift_base = False

                for child in node.children:
                    if child.type != 'inheritance_specifier':
                        continue

                    base_name: str | None = None
                    user_type_node = None

                    for sub in child.children:
                        if sub.type == 'user_type':
                            user_type_node = sub
                            base_name = swiftuser(sub, source)
                            break

                        if sub.type == 'type_identifier':
                            base_name = read(sub, source) or None
                            break

                    if not base_name:
                        continue

                    base_nid = make(stem, base_name)

                    if base_nid not in seen_ids:
                        base_nid = make(base_name)

                        if base_nid not in seen_ids:
                            nodes.append({'id': base_nid, 'label': base_name, 'file_type': 'code', 'source_file': '', 'source_location': ''})
                            seen_ids.add(base_nid)

                    if t == 'protocol_declaration':
                        relation = 'inherits'
                    else:
                        relation = swiftclassify(base_name, swift_kind, not seen_swift_base, swift_protocol_names, swift_class_names)
                    seen_swift_base = True
                    addedge(class_nid, base_nid, relation, line)

                    if user_type_node is not None:
                        for arg_child in user_type_node.children:
                            if arg_child.type != 'type_arguments':
                                continue

                            for arg in arg_child.children:
                                if not arg.is_named:
                                    continue

                                refs: list[tuple[str, str]] = []
                                swifttypes(arg, source, True, refs)

                                for ref_name, _role in refs:
                                    target = ensurenode(ref_name, line)
                                    addedge(class_nid, target, 'references', line, context='generic_arg')

            if config.ts_module == 'tree_sitter_php':

                def phpbase(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = make(stem, base_name)

                    if base_nid not in seen_ids:
                        base_nid = make(base_name)

                        if base_nid not in seen_ids:
                            nodes.append({'id': base_nid, 'label': base_name, 'file_type': 'code', 'source_file': '', 'source_location': ''})
                            seen_ids.add(base_nid)
                    addedge(class_nid, base_nid, rel, at_line)

                for child in node.children:
                    if child.type == 'base_clause':
                        for sub in child.children:
                            if sub.type in ('name', 'qualified_name'):
                                phpbase(phpname(sub, source) or '', 'inherits', child.start_point[0] + 1)
                    elif child.type == 'class_interface_clause':
                        for sub in child.children:
                            if sub.type in ('name', 'qualified_name'):
                                phpbase(phpname(sub, source) or '', 'implements', child.start_point[0] + 1)
                body = node.child_by_field_name('body')

                if body is None:
                    for c in node.children:
                        if c.type == 'declaration_list':
                            body = c
                            break

                if body is not None:
                    for member in body.children:
                        if member.type != 'use_declaration':
                            continue

                        for sub in member.children:
                            if sub.type in ('name', 'qualified_name'):
                                phpbase(phpname(sub, source) or '', 'mixes_in', member.start_point[0] + 1)

            if config.ts_module == 'tree_sitter_kotlin':
                for child in node.children:
                    if child.type != 'delegation_specifiers':
                        continue

                    for spec in child.children:
                        if spec.type != 'delegation_specifier':
                            continue

                        relation = 'implements'
                        user_type_node = None

                        for sub in spec.children:
                            if sub.type == 'constructor_invocation':
                                relation = 'inherits'

                                for inner in sub.children:
                                    if inner.type == 'user_type':
                                        user_type_node = inner
                                        break

                                break

                            if sub.type == 'user_type':
                                user_type_node = sub
                                break

                            if sub.type == 'explicit_delegation':
                                for inner in sub.children:
                                    if inner.type == 'user_type':
                                        user_type_node = inner
                                        break

                                break

                        if user_type_node is None:
                            continue

                        base = ktuser(user_type_node, source)

                        if not base:
                            continue

                        base_nid = ensurenode(base, line)
                        addedge(class_nid, base_nid, relation, line)

                        for arg_child in user_type_node.children:
                            if arg_child.type != 'type_arguments':
                                continue

                            for arg in arg_child.children:
                                if arg.type == 'type_projection':
                                    for inner in arg.children:
                                        if not inner.is_named:
                                            continue

                                        refs: list[tuple[str, str]] = []
                                        kttypes(inner, source, True, refs)

                                        for ref_name, _role in refs:
                                            target = ensurenode(ref_name, line)
                                            addedge(class_nid, target, 'references', line, context='generic_arg')
                annotation_targets: set[str] = set()

                for anno_name, anno_raw in ktannotations(node, source):
                    target_nid = ensurenode(anno_name, line)

                    if target_nid != class_nid and target_nid not in annotation_targets:
                        addedge(class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                for ref_name in ktliterals(node, source):
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != class_nid and target_nid not in annotation_targets:
                        addedge(class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                for c in node.children:
                    if c.type == 'primary_constructor':
                        for cp_list in c.children:
                            if cp_list.type == 'class_parameters':
                                for cp in cp_list.children:
                                    if cp.type != 'class_parameter':
                                        continue

                                    has_val_var = False

                                    for sub in cp.children:
                                        if sub.type in ('val', 'var'):
                                            has_val_var = True
                                            break

                                    if not has_val_var:
                                        continue

                                    ptype = None

                                    for sub in cp.children:
                                        if sub.type in ('user_type', 'nullable_type', 'type_reference'):
                                            ptype = sub
                                            break

                                    if ptype is not None:
                                        cp_line = cp.start_point[0] + 1
                                        refs: list[tuple[str, str]] = []
                                        kttypes(ptype, source, False, refs)

                                        for ref_name, role in refs:
                                            ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                            target_nid = ensurenode(ref_name, cp_line)

                                            if target_nid != class_nid:
                                                addedge(class_nid, target_nid, 'references', cp_line, context=ctx)
                                        param_annotation_targets: set[str] = set()

                                        for anno_name, anno_raw in ktannotations(cp, source):
                                            target_nid = ensurenode(anno_name, cp_line)

                                            if target_nid != class_nid and target_nid not in param_annotation_targets:
                                                addedge(class_nid, target_nid, 'references', cp_line, context='attribute')
                                                param_annotation_targets.add(target_nid)

                                        for ref_name in ktliterals(cp, source):
                                            target_nid = ensurenode(ref_name, cp_line)

                                            if target_nid != class_nid and target_nid not in param_annotation_targets:
                                                addedge(class_nid, target_nid, 'references', cp_line, context='attribute')
                                                param_annotation_targets.add(target_nid)

            if config.ts_module == 'tree_sitter_ruby':
                sup = node.child_by_field_name('superclass')

                if sup is not None:
                    base = ''
                    raw_base = ''

                    for sub in sup.children:
                        if sub.type == 'constant':
                            base = read(sub, source)
                            raw_base = base
                            break

                        if sub.type == 'scope_resolution':
                            raw_base = read(sub, source).strip()
                            consts = [c for c in sub.children if c.type == 'constant']

                            if consts:
                                base = read(consts[-1], source)
                            break

                    if base:
                        base_nid = ensurenode(base, line)
                        addedge(class_nid, base_nid, 'inherits', line, metadata={'ruby_superclass_ref': raw_base, 'ruby_lexical_scopes': list(reversed(ruby_lexical_scopes))})
                _rb_body = findbody(node, config)

                if _rb_body is not None:
                    for _stmt in _rb_body.children:
                        if _stmt.type != 'call' or _stmt.child_by_field_name('receiver') is not None:
                            continue

                        _m = _stmt.child_by_field_name('method')

                        if _m is None or read(_m, source) not in ('include', 'extend', 'prepend'):
                            continue

                        _args = _stmt.child_by_field_name('arguments')

                        if _args is None:
                            continue

                        for _arg in _args.children:
                            if _arg.type not in ('constant', 'scope_resolution'):
                                continue

                            _mod = rubyconstfull(_arg, source)

                            if _mod:
                                _ruby_mixin_calls.append({'caller_nid': class_nid, 'callee': _mod, 'is_mixin': True, 'source_file': str_path, 'source_location': f'L{_stmt.start_point[0] + 1}'})

            if config.ts_module == 'tree_sitter_c_sharp':
                csharp_type_params = csparams(node, source)

                for child in node.children:
                    if child.type != 'base_list':
                        continue

                    for sub in child.children:
                        if sub.type not in ('identifier', 'generic_name', 'qualified_name'):
                            continue

                        base_info = cstypename(sub, source)

                        if base_info is None:
                            continue

                        base, qualified, qualifier = base_info

                        if not base or base in csharp_type_params:
                            continue

                        base_nid = make(stem, '.'.join(namespace_stack), base)

                        if base_nid not in seen_ids:
                            base_nid = make(base)

                            if base_nid not in seen_ids:
                                nodes.append({'id': base_nid, 'label': base, 'file_type': 'code', 'source_file': '', 'source_location': ''})
                                seen_ids.add(base_nid)

                        if t == 'interface_declaration':
                            relation = 'inherits'
                        else:
                            relation = csclassify(base, csharp_interface_names)
                        metadata = {'ref_token': base}

                        if qualified:
                            metadata['qualified'] = True

                        if qualifier:
                            metadata['ref_qualifier'] = qualifier
                        addedge(class_nid, base_nid, relation, line, metadata=metadata)

                        if sub.type == 'generic_name':
                            for tal in sub.children:
                                if tal.type != 'type_argument_list':
                                    continue

                                for arg in tal.children:
                                    if not arg.is_named:
                                        continue

                                    refs: list[tuple[str, str, bool, str]] = []
                                    cstypes(arg, source, True, refs, csharp_type_params)

                                    for ref_name, _role, ref_qualified, ref_qualifier in refs:
                                        target = ensurenode(ref_name, line)
                                        metadata = {'ref_token': ref_name}

                                        if ref_qualified:
                                            metadata['qualified'] = True

                                        if ref_qualifier:
                                            metadata['ref_qualifier'] = ref_qualifier
                                        addedge(class_nid, target, 'references', line, context='generic_arg', metadata=metadata)

            if config.ts_module in ('tree_sitter_java', 'tree_sitter_groovy'):

                def javaparent(base_name: str, rel: str, at_line: int) -> None:
                    if not base_name:
                        return
                    base_nid = make(stem, base_name)

                    if base_nid not in seen_ids:
                        base_nid = make(base_name)

                        if base_nid not in seen_ids:
                            nodes.append({'id': base_nid, 'label': base_name, 'file_type': 'code', 'source_file': '', 'source_location': ''})
                            seen_ids.add(base_nid)
                    addedge(class_nid, base_nid, rel, at_line)

                def javaparenttype(type_node, rel: str, at_line: int) -> None:
                    refs: list[tuple[str, str]] = []
                    javatypes(type_node, source, False, refs)
                    parent_emitted = False

                    for ref_name, role in refs:
                        if role == 'type' and (not parent_emitted):
                            javaparent(ref_name, rel, at_line)
                            parent_emitted = True
                        elif role == 'generic_arg':
                            target_nid = ensurenode(ref_name, at_line)

                            if target_nid != class_nid:
                                addedge(class_nid, target_nid, 'references', at_line, context='generic_arg')
                sup = node.child_by_field_name('superclass')

                if sup is not None:
                    for sub in sup.children:
                        if sub.is_named:
                            javaparenttype(sub, 'inherits', line)
                            break

                ifs = node.child_by_field_name('interfaces')

                if ifs is not None:
                    for sub in ifs.children:
                        if sub.type == 'type_list':
                            for tid in sub.children:
                                if tid.is_named:
                                    javaparenttype(tid, 'implements', line)

                if t == 'interface_declaration':
                    for child in node.children:
                        if child.type == 'extends_interfaces':
                            for sub in child.children:
                                if sub.type == 'type_list':
                                    for tid in sub.children:
                                        if tid.is_named:
                                            javaparenttype(tid, 'inherits', line)
                annotation_targets: set[str] = set()

                for anno_name, anno_raw in javaannotations(node, source):
                    if '.' in anno_raw and config.ts_module == 'tree_sitter_java':
                        anno_name = anno_raw
                    target_nid = ensurenode(anno_name, line)

                    if target_nid != class_nid and target_nid not in annotation_targets:
                        addedge(class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                for ref_name in javaliterals(node, source):
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != class_nid and target_nid not in annotation_targets:
                        addedge(class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                if t == 'record_declaration':
                    components = node.child_by_field_name('parameters')

                    if components is not None:
                        for component in components.children:
                            if component.type == 'formal_parameter':
                                type_node = component.child_by_field_name('type')
                            elif component.type == 'spread_parameter':
                                type_node = next((child for child in component.children if child.is_named and child.type not in ('modifiers', 'variable_declarator')), None)
                            else:
                                continue

                            refs: list[tuple[str, str]] = []
                            javatypes(type_node, source, False, refs)
                            component_line = component.start_point[0] + 1

                            for ref_name, role in refs:
                                ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                target_nid = ensurenode(ref_name, component_line)

                                if target_nid != class_nid:
                                    addedge(class_nid, target_nid, 'references', component_line, context=ctx)

            if config.ts_module == 'tree_sitter_scala':
                extend = node.child_by_field_name('extend')

                if extend is None:
                    for c in node.children:
                        if c.type == 'extends_clause':
                            extend = c
                            break

                if extend is not None:
                    bases: list[tuple[str, int]] = []

                    def scalabase(type_node) -> str | None:
                        if type_node.type == 'type_identifier':
                            return read(type_node, source)

                        if type_node.type == 'stable_type_identifier':
                            tail = next((child for child in reversed(type_node.children) if child.type in ('type_identifier', 'identifier')), None)

                            return read(tail, source) if tail is not None else None

                        if type_node.type == 'generic_type':
                            base = type_node.child_by_field_name('type')

                            if base is None:
                                base = next((child for child in type_node.children if child.type in ('type_identifier', 'stable_type_identifier')), None)

                            return scalabase(base) if base is not None else None

                        return None

                    for c in extend.children:
                        base_name = scalabase(c)

                        if base_name is not None:
                            bases.append((base_name, c.start_point[0] + 1))

                    for idx, (base_name, base_line) in enumerate(bases):
                        rel = 'inherits' if idx == 0 else 'mixes_in'
                        base_nid = ensurenode(base_name, base_line)

                        if base_nid != class_nid:
                            addedge(class_nid, base_nid, rel, base_line)

                for c in node.children:
                    if c.type != 'class_parameters':
                        continue

                    for cp in c.children:
                        if cp.type != 'class_parameter':
                            continue

                        ptype = cp.child_by_field_name('type')

                        if ptype is None:
                            continue

                        cp_line = cp.start_point[0] + 1
                        refs: list[tuple[str, str]] = []
                        scalatypes(ptype, source, False, refs)

                        for ref_name, role in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                            target_nid = ensurenode(ref_name, cp_line)

                            if target_nid != class_nid:
                                addedge(class_nid, target_nid, 'references', cp_line, context=ctx)

                for c in node.children:
                    if c.type != 'type_parameters':
                        continue

                    bound_refs: list[tuple[str, str]] = []
                    scalabounds(c, source, bound_refs)

                    for ref_name, role in bound_refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'type_bound'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != class_nid:
                            addedge(class_nid, target_nid, 'references', line, context=ctx)

            if config.ts_module == 'tree_sitter_c_sharp' and t in ('class_declaration', 'record_declaration', 'struct_declaration'):
                csharp_type_params = csparams(node, source)

                for c in node.children:
                    if c.type != 'parameter_list':
                        continue

                    for param in c.children:
                        if param.type != 'parameter':
                            continue

                        ptype = param.child_by_field_name('type')

                        if ptype is None:
                            continue

                        pname = param.child_by_field_name('name')
                        p_line = param.start_point[0] + 1
                        recv = csreceiver(ptype, source)

                        if pname is not None and recv and recv[:1].isupper() and (recv not in csharp_type_params):
                            csharp_field_types.setdefault(class_nid, {})[read(pname, source)] = recv
                        refs = []
                        cstypes(ptype, source, False, refs, csharp_type_params)

                        for ref_name, role, qualified, qualifier in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                            target_nid = ensurenode(ref_name, p_line)

                            if target_nid != class_nid:
                                metadata = {'ref_token': ref_name}

                                if qualified:
                                    metadata['qualified'] = True

                                if qualifier:
                                    metadata['ref_qualifier'] = qualifier
                                addedge(class_nid, target_nid, 'references', p_line, context=ctx, metadata=metadata)

            if config.ts_module == 'tree_sitter_cpp':
                for child in node.children:
                    if child.type != 'base_class_clause':
                        continue

                    for sub in child.children:
                        base = ''
                        template_args_node = None

                        if sub.type == 'type_identifier':
                            base = read(sub, source)
                        elif sub.type == 'qualified_identifier':
                            tail = sub.child_by_field_name('name')
                            base = read(tail, source) if tail else read(sub, source)
                        elif sub.type == 'template_type':
                            tname = sub.child_by_field_name('name')
                            base = read(tname, source) if tname else read(sub, source)
                            template_args_node = sub.child_by_field_name('arguments')
                        else:
                            continue

                        if not base:
                            continue

                        base_nid = ensurenode(base, line)
                        addedge(class_nid, base_nid, 'inherits', line)

                        if template_args_node is not None:
                            arg_refs: list[tuple[str, str]] = []

                            for arg in template_args_node.children:
                                if arg.is_named:
                                    cpptypes(arg, source, True, arg_refs)

                            for ref_name, _role in arg_refs:
                                target_nid = ensurenode(ref_name, line)

                                if target_nid != class_nid:
                                    addedge(class_nid, target_nid, 'references', line, context='generic_arg')
            body = findbody(node, config)

            if body:
                ruby_namespace.extend(ruby_segments)

                if config.ts_module == 'tree_sitter_ruby':
                    ruby_lexical_scopes.append(class_name)
                previous_singleton_depth = ruby_singleton_context_depth

                if config.ts_module == 'tree_sitter_ruby':
                    ruby_singleton_context_depth = 0

                try:
                    for child in body.children:
                        walk(child, parent_class_nid=class_nid)
                finally:
                    ruby_singleton_context_depth = previous_singleton_depth

                    if config.ts_module == 'tree_sitter_ruby':
                        ruby_lexical_scopes.pop()

                    if ruby_segments:
                        del ruby_namespace[-len(ruby_segments):]
            return

        if t == 'property_declaration' and parent_class_nid and config.event_listener_properties:
            handled_event_listener = False

            for element in node.children:
                if element.type != 'property_element':
                    continue

                prop_name: str | None = None
                array_node = None

                for c in element.children:
                    if c.type == 'variable_name':
                        for sc in c.children:
                            if sc.type == 'name':
                                prop_name = read(sc, source)
                                break
                    elif c.type == 'array_creation_expression':
                        array_node = c

                if prop_name is None or prop_name not in config.event_listener_properties or array_node is None:
                    continue

                handled_event_listener = True

                for entry in array_node.children:
                    if entry.type != 'array_element_initializer':
                        continue

                    event_cls: str | None = None
                    listener_arr = None

                    for sub in entry.children:
                        if sub.type == 'class_constant_access_expression' and event_cls is None:
                            for sc in sub.children:
                                if sc.is_named and sc.type in ('name', 'qualified_name'):
                                    event_cls = read(sc, source)
                                    break
                        elif sub.type == 'array_creation_expression':
                            listener_arr = sub

                    if not event_cls or listener_arr is None:
                        continue

                    for listener_entry in listener_arr.children:
                        if listener_entry.type != 'array_element_initializer':
                            continue

                        for item in listener_entry.children:
                            if item.type != 'class_constant_access_expression':
                                continue

                            for sc in item.children:
                                if sc.is_named and sc.type in ('name', 'qualified_name'):
                                    listener_cls = read(sc, source)
                                    line_no = item.start_point[0] + 1
                                    pending_listen_edges.append((event_cls, listener_cls, line_no))
                                    break

                            break

            if handled_event_listener:
                return

        if config.ts_module == 'tree_sitter_c_sharp' and t == 'field_declaration' and parent_class_nid:
            type_node = node.child_by_field_name('type')

            if type_node is None:
                for child in node.children:
                    if child.type == 'variable_declaration':
                        type_node = child.child_by_field_name('type')

                        if type_node is not None:
                            break

            type_info = cstypename(type_node, source)

            if type_info:
                type_name, qualified, qualifier = type_info
                csharp_type_params = csparams(type_node if type_node is not None else node, source)

                if not type_name or type_name in csharp_type_params:
                    return

                if type_name[:1].isupper():
                    fields = csharp_field_types.setdefault(parent_class_nid, {})

                    for child in node.children:
                        if child.type != 'variable_declaration':
                            continue

                        for declarator in child.children:
                            if declarator.type != 'variable_declarator':
                                continue

                            name_node = declarator.child_by_field_name('name') or next((g for g in declarator.children if g.type == 'identifier'), None)

                            if name_node is not None:
                                fields[read(name_node, source)] = type_name
                line = node.start_point[0] + 1
                refs: list[tuple[str, str, bool, str]] = []
                cstypes(type_node, source, False, refs, csharp_type_params)

                for ref_name, role, ref_qualified, ref_qualifier in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        metadata = {'ref_token': ref_name}

                        if ref_qualified:
                            metadata['qualified'] = True

                        if ref_qualifier:
                            metadata['ref_qualifier'] = ref_qualifier
                        addedge(parent_class_nid, target_nid, 'references', line, context=ctx, metadata=metadata)
            return

        if config.ts_module == 'tree_sitter_c_sharp' and t == 'property_declaration' and parent_class_nid:
            prop_node_name = node.child_by_field_name('name')

            if prop_node_name is not None:
                property_name = read(prop_node_name, source)

                if property_name:
                    property_line = node.start_point[0] + 1
                    property_nid = make(parent_class_nid, property_name)

                    if property_nid not in seen_ids:
                        addnode(property_nid, property_name, property_line)
                        addedge(parent_class_nid, property_nid, 'defines', property_line, context='field')
            type_node = node.child_by_field_name('type')

            if type_node is not None:
                prop_name_node = node.child_by_field_name('name')
                prop_type = csreceiver(type_node, source)

                if prop_name_node is not None and prop_type:
                    csharp_field_types.setdefault(parent_class_nid, {})[read(prop_name_node, source)] = prop_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str, bool, str]] = []
                cstypes(type_node, source, False, refs)

                for ref_name, role, qualified, qualifier in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        metadata = {'ref_token': ref_name}

                        if qualified:
                            metadata['qualified'] = True

                        if qualifier:
                            metadata['ref_qualifier'] = qualifier
                        addedge(parent_class_nid, target_nid, 'references', line, context=ctx, metadata=metadata)
            return

        if config.ts_module == 'tree_sitter_java' and t == 'field_declaration' and parent_class_nid:
            type_node = node.child_by_field_name('type')

            if type_node is not None:
                receiver_type = javareceiver(type_node, source)

                if receiver_type:
                    fields = java_field_types.setdefault(parent_class_nid, {})

                    for field_name in javadeclarators(node, source):
                        fields[field_name] = receiver_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                javatypes(type_node, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        addedge(parent_class_nid, target_nid, 'references', line, context=ctx)
            return

        if config.ts_module == 'tree_sitter_java' and t == 'annotation_type_element_declaration' and parent_class_nid:
            type_node = node.child_by_field_name('type')
            line = node.start_point[0] + 1
            refs: list[tuple[str, str]] = []
            javatypes(type_node, source, False, refs, preserve_qualified=True)

            for ref_name, role in refs:
                ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                target_nid = ensurenode(ref_name, line)

                if target_nid != parent_class_nid:
                    addedge(parent_class_nid, target_nid, 'references', line, context=ctx)
            return

        if config.ts_module == 'tree_sitter_php' and t == 'property_declaration' and parent_class_nid:
            for c in node.children:
                if c.type not in ('named_type', 'primitive_type', 'nullable_type', 'union_type', 'intersection_type', 'optional_type'):
                    continue

                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                phptypes(c, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        addedge(parent_class_nid, target_nid, 'references', line, context=ctx)
                break

            return

        if config.ts_module == 'tree_sitter_kotlin' and t == 'property_declaration':
            if parent_class_nid:
                line = node.start_point[0] + 1
                type_node = ktproperty(node)

                if type_node is not None:
                    refs: list[tuple[str, str]] = []
                    kttypes(type_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != parent_class_nid:
                            addedge(parent_class_nid, target_nid, 'references', line, context=ctx)
                annotation_targets: set[str] = set()

                for anno_name, anno_raw in ktannotations(node, source):
                    target_nid = ensurenode(anno_name, line)

                    if target_nid != parent_class_nid and target_nid not in annotation_targets:
                        addedge(parent_class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                for ref_name in ktliterals(node, source):
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid and target_nid not in annotation_targets:
                        addedge(parent_class_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)
            owner_nid = parent_class_nid or file_nid
            seen_eq = False

            for child in node.children:
                if not child.is_named:
                    seen_eq = seen_eq or child.type == '='
                    continue

                if seen_eq:
                    initializer_nodes.append((owner_nid, child))
                elif child.type == 'property_delegate':
                    for sub in child.children:
                        if sub.is_named:
                            initializer_nodes.append((owner_nid, sub))
            return

        if config.ts_module == 'tree_sitter_swift' and t == 'property_declaration' and parent_class_nid:
            line = node.start_point[0] + 1
            prop_type: str | None = None
            type_anno = swiftproperty(node)

            if type_anno is not None:
                refs: list[tuple[str, str]] = []
                swifttypes(type_anno, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        addedge(parent_class_nid, target_nid, 'references', line, context=ctx)

                    if prop_type is None and role == 'type':
                        prop_type = ref_name
            pending_factory: tuple[str, str] | None = None

            for child in node.children:
                if child.type in config.call_types:
                    initializer_nodes.append((parent_class_nid, child))

                    if prop_type is None:
                        ctor = swiftconstructor(child, source)

                        if ctor is not None:
                            prop_type = ctor
                        else:
                            pending_factory = swiftfactory(child, source)
                elif child.type == 'navigation_expression' and prop_type is None:
                    head = child.children[0] if child.children else None

                    if head is not None and head.type == 'simple_identifier':
                        htext = read(head, source)

                        if htext and htext[:1].isupper():
                            prop_type = htext

            if prop_type is None:
                prop_type = swiftattribute(node, source)
            prop_name = swiftpropname(node, source)

            if prop_name and prop_type:
                type_table[prop_name] = prop_type
            elif prop_name and pending_factory is not None and (prop_name not in swift_factory_bindings):
                swift_factory_bindings[prop_name] = pending_factory
            comp_bodies = [c for c in node.children if c.type in ('computed_property', 'willset_didset_block')]

            if comp_bodies and prop_name:
                prop_nid = make(parent_class_nid, prop_name)
                addnode(prop_nid, f'.{prop_name}', line)
                addedge(parent_class_nid, prop_nid, 'method', line)

                for body_block in comp_bodies:
                    function_bodies.append((prop_nid, body_block))
            return

        if config.ts_module == 'tree_sitter_scala' and t in ('val_definition', 'var_definition', 'type_definition'):
            owner_nid = parent_class_nid or file_nid
            type_node = node.child_by_field_name('type')

            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                scalatypes(type_node, source, False, refs)

                for ref_name, role in refs:
                    ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != owner_nid:
                        addedge(owner_nid, target_nid, 'references', line, context=ctx)

        if config.ts_module == 'tree_sitter_scala' and t == 'self_type' and parent_class_nid:
            named = [c for c in node.children if c.is_named]
            type_node = named[1] if len(named) >= 2 else None

            if type_node is not None:
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                scalatypes(type_node, source, False, refs)

                for ref_name, role in refs:
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != parent_class_nid:
                        addedge(parent_class_nid, target_nid, 'requires', line)
            return

        if config.ts_module == 'tree_sitter_cpp' and t == 'field_declaration' and parent_class_nid:
            decls = list(node.children_by_field_name('declarator'))
            is_method = any((d.type == 'function_declarator' or (d.type in ('pointer_declarator', 'reference_declarator') and any((c.type == 'function_declarator' for c in d.children))) for d in decls))
            type_node = node.child_by_field_name('type')
            is_nested_type = type_node is not None and type_node.type in config.class_types and (type_node.child_by_field_name('body') is not None)

            if is_nested_type:
                walk(type_node, parent_class_nid)

            if not is_method and (not is_nested_type):
                if type_node is not None:
                    line = node.start_point[0] + 1
                    refs: list[tuple[str, str]] = []
                    cpptypes(type_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'field'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != parent_class_nid:
                            addedge(parent_class_nid, target_nid, 'references', line, context=ctx)

            for decl in decls:
                name = cppfunc(decl, source)

                if not name:
                    continue

                line = decl.start_point[0] + 1
                member_nid = make(parent_class_nid, name)
                decl_is_method = decl.type == 'function_declarator' or (decl.type in ('pointer_declarator', 'reference_declarator') and any((c.type == 'function_declarator' for c in decl.children)))

                if decl_is_method:
                    addnode(member_nid, f'.{name}()', line)
                    addedge(parent_class_nid, member_nid, 'method', line)
                    callable_def_nids.add(member_nid)
                else:
                    addnode(member_nid, name, line)
                    addedge(parent_class_nid, member_nid, 'defines', line, context='field')
            return

        if config.ts_module == 'tree_sitter_ruby' and t == 'singleton_class' and parent_class_nid:
            value = node.child_by_field_name('value')
            body = node.child_by_field_name('body')

            if value is not None and read(value, source) == 'self' and body:
                if rubybarrier(body, source):
                    class_node = next((n for n in nodes if n['id'] == parent_class_nid))
                    class_metadata = dict(class_node.get('metadata') or {})
                    class_metadata['ruby_lookup_unsafe'] = True
                    class_node['metadata'] = metadata(class_metadata)
                ruby_singleton_context_depth += 1

                try:
                    for child in body.children:
                        walk(child, parent_class_nid=parent_class_nid)
                finally:
                    ruby_singleton_context_depth -= 1
                return

        if t in config.function_types:
            if t == 'deinit_declaration':
                func_name: str | None = 'deinit'
            elif t == 'subscript_declaration':
                func_name = 'subscript'
            elif config.resolve_function_name_fn is not None:
                declarator = node.child_by_field_name('declarator')
                func_name = None

                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)

                if name_node is None:
                    for child in node.children:
                        if child.type in config.name_fallback_child_types:
                            name_node = child
                            break

                func_name = read(name_node, source) if name_node else None

            if not func_name:
                if config.ts_module == 'tree_sitter_php' and t in ('anonymous_function', 'arrow_function'):
                    route_name = phproute(node, source)

                    if route_name:
                        func_name = route_name
                    else:
                        _scope_key = parent_class_nid or stem
                        php_closure_counts[_scope_key] = php_closure_counts.get(_scope_key, 0) + 1
                        func_name = '{closure#' + str(php_closure_counts[_scope_key]) + '}'
                else:
                    return
            sanitized_name = config.sanitize_symbol_name_fn(func_name) if config.sanitize_symbol_name_fn is not None else func_name

            if not normalize(sanitized_name):
                return
            line = node.start_point[0] + 1
            ruby_method_kind = None

            if config.ts_module == 'tree_sitter_ruby' and parent_class_nid:
                if t == 'singleton_method':
                    singleton_object = node.child_by_field_name('object')

                    if ruby_singleton_context_depth == 0 and singleton_object is not None and (read(singleton_object, source) == 'self'):
                        ruby_method_kind = 'singleton'
                    else:
                        ruby_method_kind = 'ambiguous'
                elif ruby_singleton_context_depth == 1:
                    ruby_method_kind = 'singleton'
                elif ruby_singleton_context_depth == 0:
                    ruby_method_kind = 'instance'
                else:
                    ruby_method_kind = 'ambiguous'

            if parent_class_nid:
                func_nid = make(parent_class_nid, sanitized_name)

                if config.ts_module == 'tree_sitter_python':
                    func_nid = pysalted(func_nid, sanitized_name, python_underscore_groups)
                ruby_method_metadata = None

                if ruby_method_kind is not None:
                    kinds = ruby_method_kinds.setdefault(func_nid, set())
                    kinds.add(ruby_method_kind)
                    ruby_method_counts[func_nid] = ruby_method_counts.get(func_nid, 0) + 1
                    stored_kind = next(iter(kinds)) if len(kinds) == 1 and ruby_method_counts[func_nid] == 1 and (next(iter(kinds)) in {'instance', 'singleton'}) else 'ambiguous'
                    ruby_method_metadata = {'ruby_method_kind': stored_kind}
                    existing_method = next((n for n in nodes if n['id'] == func_nid), None)

                    if existing_method is not None:
                        existing_metadata = dict(existing_method.get('metadata') or {})
                        existing_metadata.update(ruby_method_metadata)
                        existing_method['metadata'] = metadata(existing_metadata)
                addnode(func_nid, f'.{func_name}()', line, metadata=ruby_method_metadata)
                addedge(parent_class_nid, func_nid, 'method', line)
            else:
                func_nid = make(stem, sanitized_name)

                if config.ts_module == 'tree_sitter_python':
                    func_nid = pysalted(func_nid, sanitized_name, python_underscore_groups)
                addnode(func_nid, f'{func_name}()', line)
                addedge(file_nid, func_nid, 'contains', line)
            callable_def_nids.add(func_nid)

            if config.ts_module == 'tree_sitter_python':
                local_bound_names[func_nid] = pylocals(node, source)
            elif config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                local_bound_names[func_nid] = jslocals(node, source)

            if config.ts_module == 'tree_sitter_python':
                params_node = node.child_by_field_name('parameters')

                for ref_name, role in pyparams(params_node, source):
                    ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != func_nid:
                        edges.append(semantic(func_nid, target_nid, ctx, str_path, line))
                return_type_node = node.child_by_field_name('return_type')

                if return_type_node is not None:
                    return_refs: list[tuple[str, str]] = []
                    pytypes(return_type_node, source, False, return_refs)

                    for ref_name, role in return_refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            edges.append(semantic(func_nid, target_nid, ctx, str_path, line))

            if config.ts_module == 'tree_sitter_c_sharp':
                csharp_type_params = csparams(node, source)
                params_node = node.child_by_field_name('parameters')

                if params_node is not None:
                    for p in params_node.children:
                        if p.type != 'parameter':
                            continue

                        type_node = p.child_by_field_name('type')
                        refs: list[tuple[str, str, bool, str]] = []
                        cstypes(type_node, source, False, refs, csharp_type_params)

                        for ref_name, role, qualified, qualifier in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                            target_nid = ensurenode(ref_name, line)

                            if target_nid != func_nid:
                                metadata = {'ref_token': ref_name}

                                if qualified:
                                    metadata['qualified'] = True

                                if qualifier:
                                    metadata['ref_qualifier'] = qualifier
                                addedge(func_nid, target_nid, 'references', line, context=ctx, metadata=metadata)
                return_node = node.child_by_field_name('returns')

                if return_node is not None:
                    refs: list[tuple[str, str, bool, str]] = []
                    cstypes(return_node, source, False, refs, csharp_type_params)

                    for ref_name, role, qualified, qualifier in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            metadata = {'ref_token': ref_name}

                            if qualified:
                                metadata['qualified'] = True

                            if qualifier:
                                metadata['ref_qualifier'] = qualifier
                            addedge(func_nid, target_nid, 'references', line, context=ctx, metadata=metadata)

                for attr_name, qualified, qualifier in csattributes(node, source):
                    target_nid = ensurenode(attr_name, line)

                    if target_nid != func_nid:
                        metadata = {'ref_token': attr_name}

                        if qualified:
                            metadata['qualified'] = True

                        if qualifier:
                            metadata['ref_qualifier'] = qualifier
                        addedge(func_nid, target_nid, 'references', line, context='attribute', metadata=metadata)

            if config.ts_module == 'tree_sitter_java':
                params_node = node.child_by_field_name('parameters')

                if params_node is not None:
                    for p in params_node.children:
                        if p.type != 'formal_parameter':
                            continue

                        type_node = p.child_by_field_name('type')
                        refs = []
                        javatypes(type_node, source, False, refs)

                        for ref_name, role in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                            target_nid = ensurenode(ref_name, line)

                            if target_nid != func_nid:
                                addedge(func_nid, target_nid, 'references', line, context=ctx)
                return_node = node.child_by_field_name('type')

                if return_node is not None:
                    refs = []
                    javatypes(return_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)
                annotation_targets: set[str] = set()

                for anno_name, anno_raw in javaannotations(node, source):
                    target_nid = ensurenode(anno_raw if '.' in anno_raw else anno_name, line)

                    if target_nid != func_nid and target_nid not in annotation_targets:
                        addedge(func_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

                for ref_name in javaliterals(node, source):
                    target_nid = ensurenode(ref_name, line)

                    if target_nid != func_nid and target_nid not in annotation_targets:
                        addedge(func_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

            if config.ts_module == 'tree_sitter_php':
                params_container = None

                for c in node.children:
                    if c.type == 'formal_parameters':
                        params_container = c
                        break

                if params_container is not None:
                    for p in params_container.children:
                        if p.type not in ('simple_parameter', 'property_promotion_parameter'):
                            continue

                        is_promoted = p.type == 'property_promotion_parameter'
                        type_node = None

                        for sub in p.children:
                            if sub.type in ('named_type', 'primitive_type', 'nullable_type', 'union_type', 'intersection_type', 'optional_type'):
                                type_node = sub
                                break

                        refs: list[tuple[str, str]] = []
                        phptypes(type_node, source, False, refs)

                        for ref_name, role in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                            target_nid = ensurenode(ref_name, line)

                            if target_nid != func_nid:
                                addedge(func_nid, target_nid, 'references', line, context=ctx)

                            if is_promoted and parent_class_nid and (target_nid != parent_class_nid):
                                fctx = 'generic_arg' if role == 'generic_arg' else 'field'
                                addedge(parent_class_nid, target_nid, 'references', line, context=fctx)
                return_node = phpreturn(node)

                if return_node is not None:
                    refs = []
                    phptypes(return_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)

            if config.ts_module == 'tree_sitter_kotlin':
                params_container = None

                for c in node.children:
                    if c.type == 'function_value_parameters':
                        params_container = c
                        break

                if params_container is not None:
                    for p in params_container.children:
                        if p.type != 'parameter':
                            continue

                        param_type_node = None

                        for sub in p.children:
                            if sub.type in ('user_type', 'nullable_type', 'type_reference'):
                                param_type_node = sub
                                break

                        refs: list[tuple[str, str]] = []
                        kttypes(param_type_node, source, False, refs)

                        for ref_name, role in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                            target_nid = ensurenode(ref_name, line)

                            if target_nid != func_nid:
                                addedge(func_nid, target_nid, 'references', line, context=ctx)
                return_type_node = ktreturn(node)

                if return_type_node is not None:
                    refs = []
                    kttypes(return_type_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)
                annotation_targets: set[str] = set()

                for anno_name, anno_raw in ktannotations(node, source):
                    target_nid = ensurenode(anno_name, line)

                    if target_nid != func_nid and target_nid not in annotation_targets:
                        addedge(func_nid, target_nid, 'references', line, context='attribute')
                        annotation_targets.add(target_nid)

            if config.ts_module == 'tree_sitter_swift':
                for p in node.children:
                    if p.type != 'parameter':
                        continue

                    type_node = p.child_by_field_name('type')
                    refs: list[tuple[str, str]] = []
                    swifttypes(type_node, source, False, refs)
                    param_type: str | None = None

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)

                        if param_type is None and role == 'type':
                            param_type = ref_name

                    if param_type:
                        name_node = p.child_by_field_name('name')
                        pname = read(name_node, source) if name_node else None

                        if pname:
                            type_table[pname] = param_type
                return_node = node.child_by_field_name('return_type')

                if return_node is not None:
                    refs = []
                    swifttypes(return_node, source, False, refs)
                    plain_return = return_node.type == 'user_type' and sum((1 for _, r in refs if r == 'type')) == 1

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx, metadata={'swift_plain_return': True} if plain_return and role == 'type' else None)

            if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and func_name == 'constructor':
                params_node = node.child_by_field_name('parameters')

                if params_node is not None:
                    for p in params_node.children:
                        if p.type != 'required_parameter':
                            continue

                        has_modifier = any((c.type in ('accessibility_modifier', 'readonly') for c in p.children))

                        if not has_modifier:
                            continue

                        name_n = p.child_by_field_name('pattern')
                        type_n = p.child_by_field_name('type')

                        if name_n is None or type_n is None:
                            continue

                        pname = read(name_n, source)

                        for tc in type_n.children:
                            if tc.type == 'type_identifier':
                                ptype = read(tc, source)

                                if pname and ptype:
                                    type_table[pname] = ptype
                                break

            if config.ts_module in ('tree_sitter_c', 'tree_sitter_cpp'):
                collect = cpptypes if config.ts_module == 'tree_sitter_cpp' else ctypes
                return_node = node.child_by_field_name('type')

                if return_node is not None:
                    refs: list[tuple[str, str]] = []
                    collect(return_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)
                decl = node.child_by_field_name('declarator')

                while decl is not None and decl.type in ('pointer_declarator', 'reference_declarator'):
                    decl = decl.child_by_field_name('declarator')

                if decl is not None and decl.type == 'function_declarator':
                    params_node = decl.child_by_field_name('parameters')

                    if params_node is not None:
                        for p in params_node.children:
                            if p.type != 'parameter_declaration':
                                continue

                            ptype = p.child_by_field_name('type')

                            if ptype is None:
                                continue

                            refs = []
                            collect(ptype, source, False, refs)

                            for ref_name, role in refs:
                                ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                                target_nid = ensurenode(ref_name, line)

                                if target_nid != func_nid:
                                    addedge(func_nid, target_nid, 'references', line, context=ctx)

            if config.ts_module == 'tree_sitter_scala':
                params_node = None

                for c in node.children:
                    if c.type == 'parameters':
                        params_node = c
                        break

                if params_node is not None:
                    for p in params_node.children:
                        if p.type != 'parameter':
                            continue

                        ptype = p.child_by_field_name('type')

                        if ptype is None:
                            continue

                        refs: list[tuple[str, str]] = []
                        scalatypes(ptype, source, False, refs)

                        for ref_name, role in refs:
                            ctx = 'generic_arg' if role == 'generic_arg' else 'parameter_type'
                            target_nid = ensurenode(ref_name, line)

                            if target_nid != func_nid:
                                addedge(func_nid, target_nid, 'references', line, context=ctx)
                return_node = node.child_by_field_name('return_type')

                if return_node is not None:
                    refs = []
                    scalatypes(return_node, source, False, refs)

                    for ref_name, role in refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'return_type'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)

                for c in node.children:
                    if c.type != 'type_parameters':
                        continue

                    bound_refs: list[tuple[str, str]] = []
                    scalabounds(c, source, bound_refs)

                    for ref_name, role in bound_refs:
                        ctx = 'generic_arg' if role == 'generic_arg' else 'type_bound'
                        target_nid = ensurenode(ref_name, line)

                        if target_nid != func_nid:
                            addedge(func_nid, target_nid, 'references', line, context=ctx)
            body = findbody(node, config)

            if body is not None and config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                function_owner_nid = parent_class_nid if parent_class_nid else func_nid
                jsscan(body, function_owner_nid, source, this_as_method=True, add_node_fn=addnode, add_edge_fn=addedge, function_bodies=function_bodies)

            if body:
                if config.ts_module == 'tree_sitter_java' and parent_class_nid:
                    java_method_scopes[id(body)] = (node, parent_class_nid)

                if config.ts_module == 'tree_sitter_c_sharp' and parent_class_nid:
                    csharp_method_scopes[id(body)] = (node, parent_class_nid)
                function_bodies.append((func_nid, body))

                if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                    jsnested(body, func_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies)

                if config.ts_module == 'tree_sitter_python':
                    pynested(body, func_nid, source=source, config=config, addnode=addnode, addedge=addedge, callable_def_nids=callable_def_nids, local_bound_names=local_bound_names, function_bodies=function_bodies, scope_parents=scope_parents, lexical_nids_by_scope=lexical_nids_by_scope)

                if config.ts_module == 'tree_sitter_php':
                    walk(body, parent_class_nid=parent_class_nid)

                if config.ts_module == 'tree_sitter_kotlin':
                    _kt_literals = []
                    _kt_stack = list(body.children)

                    while _kt_stack:
                        _kt_node = _kt_stack.pop()

                        if _kt_node.type == 'function_declaration':
                            continue

                        if _kt_node.type == 'object_literal':
                            _kt_literals.append(_kt_node)
                            continue

                        _kt_stack.extend(_kt_node.children)
                    _kt_literals.sort(key=lambda n: n.start_byte)

                    for lit in _kt_literals:
                        lit_line = lit.start_point[0] + 1
                        lit_bases: list[tuple[str, str]] = []

                        for dchild in lit.children:
                            if dchild.type != 'delegation_specifiers':
                                continue

                            for spec in dchild.children:
                                if spec.type != 'delegation_specifier':
                                    continue

                                relation = 'implements'
                                user_type_node = None

                                for sub in spec.children:
                                    if sub.type == 'constructor_invocation':
                                        relation = 'inherits'

                                        for inner in sub.children:
                                            if inner.type == 'user_type':
                                                user_type_node = inner
                                                break

                                        break

                                    if sub.type == 'user_type':
                                        user_type_node = sub
                                        break

                                    if sub.type == 'explicit_delegation':
                                        for inner in sub.children:
                                            if inner.type == 'user_type':
                                                user_type_node = inner
                                                break

                                        break

                                base = ktuser(user_type_node, source)

                                if base:
                                    lit_bases.append((base, relation))
                        obj_label = lit_bases[0][0] if lit_bases else f'object@L{lit_line}'
                        obj_nid = make(func_nid, f'object:{obj_label}', f'L{lit_line}')
                        addnode(obj_nid, obj_label, lit_line)
                        addedge(func_nid, obj_nid, 'contains', lit_line)
                        callable_def_nids.add(obj_nid)
                        callable_class_nids.add(obj_nid)

                        for base, relation in lit_bases:
                            base_nid = ensurenode(base, lit_line)

                            if base_nid != obj_nid:
                                addedge(obj_nid, base_nid, relation, lit_line)
                        lit_body = next((c for c in lit.children if c.type == 'class_body'), None)

                        if lit_body is not None:
                            for child in lit_body.children:
                                walk(child, parent_class_nid=obj_nid)

                if config.ts_module in ('tree_sitter_java', 'tree_sitter_groovy'):
                    _jv_anon = []
                    _jv_stack = list(body.children)

                    while _jv_stack:
                        _jv_node = _jv_stack.pop()

                        if _jv_node.type in ('method_declaration', 'lambda_expression'):
                            continue

                        if _jv_node.type == 'object_creation_expression' and any((c.type == 'class_body' for c in _jv_node.children)):
                            _jv_anon.append(_jv_node)
                            continue

                        _jv_stack.extend(_jv_node.children)
                    _jv_anon.sort(key=lambda n: n.start_byte)

                    for anon in _jv_anon:
                        anon_line = anon.start_point[0] + 1
                        type_node = anon.child_by_field_name('type')
                        base = read(type_node, source).strip() if type_node else ''
                        obj_label = base or f'object@L{anon_line}'
                        obj_nid = make(func_nid, f'object:{obj_label}', f'L{anon_line}')
                        addnode(obj_nid, obj_label, anon_line)
                        addedge(func_nid, obj_nid, 'contains', anon_line)
                        callable_def_nids.add(obj_nid)
                        callable_class_nids.add(obj_nid)
                        anon_body = next((c for c in anon.children if c.type == 'class_body'), None)

                        if anon_body is not None:
                            for child in anon_body.children:
                                walk(child, parent_class_nid=obj_nid)
            return

        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
            if jswalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, callable_def_nids, local_bound_names, closure_locals_by_body, config=config):
                return

        if config.ts_module == 'tree_sitter_typescript':
            if tswalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk):
                return

        if config.ts_module == 'tree_sitter_c_sharp':
            if cswalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk, namespace_stack, scope_stack):
                return

        if config.ts_module == 'tree_sitter_swift':
            if swiftwalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, ensurenode):
                return

        if config.ts_module in ('tree_sitter_java', 'tree_sitter_groovy'):
            if javawalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk):
                return

        if config.ts_module == 'tree_sitter_kotlin':
            if ktwalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk):
                return

        if config.ts_module == 'tree_sitter_php':
            if phpwalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge):
                return

        if config.ts_module == 'tree_sitter_scala':
            if scalawalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk):
                return

        if config.ts_module == 'tree_sitter_cpp':
            if cppwalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge):
                return

        if config.ts_module == 'tree_sitter_ruby':
            if rubywalk(node, source, file_nid, stem, str_path, nodes, edges, seen_ids, function_bodies, parent_class_nid, addnode, addedge, walk, callable_def_nids, callable_class_nids, ruby_namespace, ruby_lexical_scopes):
                return

        if t == 'decorated_definition':
            if config.ts_module == 'tree_sitter_python':
                inner = node.child_by_field_name('definition')
                inner_name = None

                if inner is not None:
                    name_node = inner.child_by_field_name('name')
                    inner_name = read(name_node, source) if name_node else None

                if inner_name and normalize(inner_name):
                    if inner.type in config.class_types:
                        owner_nid = make(stem, '.'.join(namespace_stack), inner_name)

                        if config.ts_module == 'tree_sitter_python' and parent_class_nid:
                            owner_nid = make(parent_class_nid, inner_name)
                    elif parent_class_nid:
                        owner_nid = make(parent_class_nid, inner_name)
                    else:
                        owner_nid = make(stem, inner_name)

                    for child in node.children:
                        if child.type != 'decorator':
                            continue

                        deco_name = pydecorator(child, source)

                        if not deco_name or deco_name in PYDECORATORS:
                            continue

                        deco_line = child.start_point[0] + 1
                        target = ensurenode(deco_name, deco_line)

                        if target != owner_nid:
                            addedge(owner_nid, target, 'references', deco_line, context='decorator')

            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        if config.ts_module == 'tree_sitter_kotlin' and t == 'companion_object':
            for child in node.children:
                if child.type == 'class_body':
                    for member in child.children:
                        walk(member, parent_class_nid=parent_class_nid)
                else:
                    walk(child, parent_class_nid=parent_class_nid)
            return

        if t == 'enum_body_declarations':
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        if t == 'ERROR':
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return

        for child in node.children:
            walk(child, parent_class_nid=None)
    walk(root)
    label_to_nid: dict[str, str] = {}
    label_to_nid_ci: dict[str, str] = {}
    nid_to_sf: dict[str, str] = {}
    lua_self_table: dict[str, str] = {}

    for n in nodes:
        nid_to_sf[n['id']] = str(n.get('source_file') or '')

        if n.get('type') == 'namespace':
            continue

        raw = n['label']
        normalised = raw.strip('()').lstrip('.')

        if config.ts_module == 'tree_sitter_lua':
            sep = ':' if ':' in normalised else '.' if '.' in normalised else None

            if sep:
                lua_self_table[n['id']] = normalised.rsplit(sep, 1)[0]

        if n['id'] not in scope_parents:
            label_to_nid[normalised] = n['id']
            label_to_nid_ci[normalised.lower()] = n['id']
        else:
            label_to_nid.setdefault(normalised, n['id'])
            label_to_nid_ci.setdefault(normalised.lower(), n['id'])
    seen_call_pairs: set[tuple[str, str]] = set()
    seen_indirect_pairs: set[tuple[str, str]] = set()
    seen_dyn_import_pairs: set[tuple[str, str]] = set()
    seen_static_ref_pairs: set[tuple[str, str, str]] = set()
    seen_helper_ref_pairs: set[tuple[str, str, str]] = set()
    seen_bind_pairs: set[tuple[str, str, str]] = set()
    raw_calls: list[dict] = []
    ruby_var_types: dict[str, dict[str, str | None]] = {}
    python_var_types: dict[str, dict[str, str | None]] = {}
    ruby_local_names: dict[str, frozenset[str]] = {}
    _local_bases: dict[str, list[str]] = {}

    for _e in edges:
        if _e.get('relation') == 'inherits':
            _local_bases.setdefault(_e['source'], []).append(_e['target'])
    method_owner: dict[str, str] = {}
    methods_by_owner: dict[tuple[str, str], str] = {}

    if config.ts_module in SELFLANGS:
        label_by_nid = {n['id']: n['label'] for n in nodes}

        for e in edges:
            if e['relation'] == 'method':
                method_owner[e['target']] = e['source']
                name = label_by_nid.get(e['target'], '').strip('()').lstrip('.')

                if config.ts_module == 'tree_sitter_php':
                    name = name.casefold()
                methods_by_owner.setdefault((e['source'], name), e['target'])

    def fieldschain(tables: dict, class_nid) -> dict:
        if not class_nid:
            return {}

        merged: dict = {}
        seen: set = set()
        queue = [class_nid]

        while queue:
            cls = queue.pop(0)

            if cls in seen:
                continue

            seen.add(cls)

            for _name, _tname in tables.get(cls, {}).items():
                merged.setdefault(_name, _tname)
            queue.extend(_local_bases.get(cls, []))

        return merged

    java_receiver_types = {body_id: javamethods(method_node, source, fieldschain(java_field_types, class_nid)) for body_id, (method_node, class_nid) in java_method_scopes.items()}
    csharp_receiver_types = {body_id: csmethods(method_node, source, fieldschain(csharp_field_types, class_nid)) for body_id, (method_node, class_nid) in csharp_method_scopes.items()}

    def indirectname(ident_name: str, loc_node, scope_nid: str, context: str) -> None:
        ref_nid = label_to_nid.get(ident_name)

        if ref_nid is None or (ref_nid not in callable_def_nids and nid_to_sf.get(ref_nid, '') != str_path):
            raw_calls.append({'caller_nid': scope_nid, 'callee': ident_name, 'is_member_call': False, 'indirect': True, 'context': context, 'source_file': str_path, 'source_location': f'L{loc_node.start_point[0] + 1}'})
            return

        if ref_nid == scope_nid or ref_nid not in callable_def_nids:
            return

        if ref_nid in callable_class_nids:
            return

        if (scope_nid, ref_nid) in seen_call_pairs:
            return

        if (scope_nid, ref_nid) in seen_indirect_pairs:
            return
        seen_indirect_pairs.add((scope_nid, ref_nid))
        edges.append({'source': scope_nid, 'target': ref_nid, 'relation': 'indirect_call', 'context': context, 'source_file': str_path, 'source_location': f'L{loc_node.start_point[0] + 1}'})

    def indirectref(ident, scope_nid: str, enclosing_locals, context: str) -> None:
        if ident is None or ident.type not in ('identifier', 'shorthand_property_identifier'):
            return
        ident_name = read(ident, source)

        if ident_name in enclosing_locals or ident_name in ('self', 'cls'):
            return

        if ident_name in js_external_imports:
            return
        indirectname(ident_name, ident, scope_nid, context)

    def pydispatch(coll_node):
        if coll_node.type == 'dictionary':
            for pair in coll_node.children:
                if pair.type == 'pair':
                    val = pair.child_by_field_name('value')

                    if val is not None and val.type == 'identifier':
                        yield val
        else:
            for el in coll_node.children:
                if el.type == 'identifier':
                    yield el

    def pyrefvalues(value_node):
        if value_node is None:
            return

        if value_node.type == 'identifier':
            yield value_node
        elif value_node.type == 'expression_list':
            for ch in value_node.children:
                if ch.type == 'identifier':
                    yield ch

    def getattrname(call_node):
        fn = call_node.child_by_field_name('function')

        if fn is None or fn.type != 'identifier' or read(fn, source) != 'getattr':
            return None

        args = call_node.child_by_field_name('arguments')

        if args is None:
            return None

        positional = [c for c in args.children if c.is_named and c.type not in ('keyword_argument', 'comment')]

        if len(positional) < 2:
            return None

        name_node = positional[1]

        if name_node.type != 'string' or any((ch.type == 'interpolation' for ch in name_node.children)):
            return None

        content = next((ch for ch in name_node.children if ch.type == 'string_content'), None)

        if content is None:
            return None

        return (read(content, source), name_node)

    def phpconstscope(n) -> str | None:
        scope = n.child_by_field_name('scope')

        if scope is None:
            for c in n.children:
                if c.is_named and c.type in ('name', 'qualified_name', 'identifier'):
                    scope = c
                    break

        if scope is None:
            return None

        return read(scope, source)

    _tracked_body_ids: set[object] = set()
    _JS_CLOSURE_TYPES = ('arrow_function', 'function_expression')
    _JS_DESCEND_TYPES = _JS_CLOSURE_TYPES + ('function_declaration', 'generator_function_declaration', 'generator_function')

    def walkcalls(node, caller_nid: str, receiver_types: dict[str, str] | tuple | None=None, extra_locals: frozenset[str]=frozenset()) -> None:
        if node.type in config.function_boundary_types:
            if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type in _JS_DESCEND_TYPES:
                body = node.child_by_field_name('body')

                if body is not None and body not in _tracked_body_ids:
                    closure_locals = extra_locals | jslocals(node, source)

                    for child in node.children:
                        walkcalls(child, caller_nid, receiver_types, closure_locals)
            return

        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type in ('lexical_declaration', 'variable_declaration'):
            jsrequire(node, source, caller_nid, stem, edges, str_path)
        _ruby_bare_self_send = False

        if config.ts_module == 'tree_sitter_ruby' and node.type == 'identifier':
            _parent = node.parent
            _call_field = _parent is not None and _parent.type == 'call' and (node in (_parent.child_by_field_name('method'), _parent.child_by_field_name('receiver')))

            if not _call_field:
                _bare_name = read(node, source)

                if _bare_name and _bare_name not in ruby_local_names.get(caller_nid, frozenset()) and (_bare_name not in extra_locals):
                    _ruby_bare_self_send = True

        if node.type in config.call_types or _ruby_bare_self_send:
            if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                if jsdynamic(node, source, caller_nid, str_path, edges, seen_dyn_import_pairs):
                    for child in node.children:
                        walkcalls(child, caller_nid, receiver_types, extra_locals)
                    return
            callee_name: str | None = None
            is_member_call: bool = False
            is_this_field_call: bool = False
            self_receiver: str | None = None
            swift_receiver: str | None = None
            member_receiver: str | None = None
            kotlin_qualified_prefix: str | None = None
            kotlin_object_receiver: str | None = None
            csharp_qualified_prefix: str | None = None
            lua_self_qualified: bool = False

            if _ruby_bare_self_send:
                callee_name = read(node, source)
                self_receiver = 'self'
            elif config.ts_module == 'tree_sitter_swift':
                first = node.children[0] if node.children else None

                if first:
                    if first.type == 'simple_identifier':
                        callee_name = read(first, source)
                        self_receiver = 'self'
                    elif first.type == 'navigation_expression':
                        is_member_call = True

                        for child in first.children:
                            if child.type == 'navigation_suffix':
                                for sc in child.children:
                                    if sc.type == 'simple_identifier':
                                        callee_name = read(sc, source)
                        recv_node = first.children[0] if first.children else None
                        swift_receiver = swiftreceiver(recv_node, source)

                        if recv_node is not None and recv_node.type == 'self_expression':
                            self_receiver = 'self'
                        elif recv_node is not None and recv_node.type == 'super_expression':
                            self_receiver = 'super'
            elif config.ts_module == 'tree_sitter_kotlin':
                first = node.children[0] if node.children else None

                if first:
                    if first.type in ('simple_identifier', 'identifier'):
                        callee_name = read(first, source)
                    elif first.type == 'navigation_expression':
                        is_member_call = True

                        for child in reversed(first.children):
                            if child.type in ('simple_identifier', 'identifier'):
                                callee_name = read(child, source)
                                break

                        segments = ktsegments(first, source)

                        if segments is not None and len(segments) >= 3:
                            kotlin_qualified_prefix = '.'.join(segments[:-1])
                        elif segments is not None and len(segments) == 2 and segments[0][:1].isupper():
                            kotlin_object_receiver = segments[0]
            elif config.ts_module == 'tree_sitter_scala':
                first = node.children[0] if node.children else None

                if first:
                    if first.type == 'identifier':
                        callee_name = read(first, source)
                    elif first.type == 'field_expression':
                        is_member_call = True
                        field = first.child_by_field_name('field')

                        if field:
                            callee_name = read(field, source)
                        else:
                            for child in reversed(first.children):
                                if child.type == 'identifier':
                                    callee_name = read(child, source)
                                    break
            elif config.ts_module == 'tree_sitter_php' and node.type == 'object_creation_expression':
                for _oce_child in node.children:
                    if _oce_child.type in ('name', 'qualified_name'):
                        _oce_text = read(_oce_child, source).rsplit('\\', 1)[-1]

                        if _oce_text.lower() not in ('self', 'static', 'parent'):
                            callee_name = _oce_text
                        break
            elif config.ts_module == 'tree_sitter_c_sharp' and node.type == 'object_creation_expression':
                type_info = cstypename(node.child_by_field_name('type'), source)

                if type_info and type_info[0]:
                    callee_name = type_info[0]

                    if type_info[1] and type_info[2]:
                        csharp_qualified_prefix = type_info[2]
            elif config.ts_module == 'tree_sitter_c_sharp' and node.type == 'invocation_expression':
                fn_node = node.child_by_field_name('function')
                member_parts = csparts(fn_node)

                if member_parts is not None:
                    mname, recv = member_parts

                    if mname is not None:
                        callee_name = csbare(mname, source)
                        is_member_call = True

                        if recv is not None and recv.type == 'identifier':
                            member_receiver = read(recv, source)
                        elif recv is not None and recv.type in ('this', 'this_expression'):
                            member_receiver = 'this'
                        elif recv is not None and recv.type in ('base', 'base_expression'):
                            member_receiver = 'base'
                        elif recv is not None and recv.type == 'member_access_expression':
                            inner = recv.child_by_field_name('expression')
                            fname = recv.child_by_field_name('name')

                            if inner is not None and inner.type in ('this', 'this_expression') and (fname is not None) and (fname.type == 'identifier'):
                                member_receiver = read(fname, source)
                        elif recv is not None and recv.type == 'parenthesized_expression':
                            inner = recv.named_children[0] if recv.named_children else None
                            cast_type = None

                            if inner is not None and inner.type == 'cast_expression':
                                cast_type = inner.child_by_field_name('type')
                            elif inner is not None and inner.type == 'as_expression':
                                cast_type = inner.child_by_field_name('right')
                            type_info = cstypename(cast_type, source)

                            if type_info and type_info[0]:
                                member_receiver = type_info[0]
                elif fn_node is not None and fn_node.type == 'identifier':
                    callee_name = read(fn_node, source)
                elif fn_node is not None and fn_node.type == 'generic_name':
                    callee_name = csbare(fn_node, source)
                else:
                    name_node = node.child_by_field_name('name')

                    if name_node:
                        callee_name = read(name_node, source)
                    else:
                        for child in node.children:
                            if child.is_named:
                                raw = read(child, source)

                                if '.' in raw:
                                    callee_name = raw.split('.')[-1]
                                    is_member_call = True
                                    parts = raw.split('.')

                                    if len(parts) == 2 and parts[0]:
                                        member_receiver = parts[0]
                                else:
                                    callee_name = raw
                                break

                if fn_node is not None:
                    call_tal = None

                    if member_parts is not None:
                        ma_name = member_parts[0]

                        if ma_name is not None and ma_name.type == 'generic_name':
                            for tal_child in ma_name.children:
                                if tal_child.type == 'type_argument_list':
                                    call_tal = tal_child
                                    break
                    elif fn_node.type == 'generic_name':
                        for tal_child in fn_node.children:
                            if tal_child.type == 'type_argument_list':
                                call_tal = tal_child
                                break

                    if call_tal is not None:
                        call_type_params = csparams(node, source)
                        call_line = node.start_point[0] + 1

                        for call_arg in call_tal.children:
                            if not call_arg.is_named:
                                continue

                            call_refs: list[tuple[str, str, bool, str]] = []
                            cstypes(call_arg, source, True, call_refs, call_type_params)

                            for call_ref_name, _call_role, call_qualified, call_qualifier in call_refs:
                                call_target = ensurenode(call_ref_name, call_line)

                                if call_target == caller_nid:
                                    continue

                                call_meta = {'ref_token': call_ref_name}

                                if call_qualified:
                                    call_meta['qualified'] = True

                                if call_qualifier:
                                    call_meta['ref_qualifier'] = call_qualifier
                                addedge(caller_nid, call_target, 'references', call_line, context='generic_arg', metadata=call_meta)
            elif config.ts_module == 'tree_sitter_php':
                if node.type == 'function_call_expression':
                    func_node = node.child_by_field_name('function')

                    if func_node:
                        callee_name = read(func_node, source)

                        if callee_name.lower() in PHPCONSTRUCTS:
                            callee_name = None
                elif node.type == 'scoped_call_expression':
                    scope_node = node.child_by_field_name('scope')

                    if scope_node:
                        callee_name = read(scope_node, source)
                else:
                    is_member_call = True
                    name_node = node.child_by_field_name('name')

                    if name_node:
                        callee_name = read(name_node, source)
                    receiver = node.child_by_field_name('object')

                    if receiver is not None and read(receiver, source) == '$this' and callee_name and (callee_name.casefold() in GLOBALS):
                        self_receiver = 'this'
            elif config.ts_module == 'tree_sitter_cpp':
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None

                if func_node:
                    if func_node.type == 'identifier':
                        callee_name = read(func_node, source)
                    elif func_node.type == 'field_expression':
                        is_member_call = True
                        name = func_node.child_by_field_name('field')

                        if name:
                            callee_name = read(name, source)
                        obj = func_node.child_by_field_name('argument')

                        if obj is not None and obj.type == 'identifier':
                            member_receiver = read(obj, source)
                        elif obj is not None and obj.type == 'this':
                            member_receiver = 'this'
                    elif func_node.type == 'qualified_identifier':
                        is_member_call = True
                        name = func_node.child_by_field_name('name')

                        if name:
                            callee_name = read(name, source)
                        scope = func_node.child_by_field_name('scope')

                        if scope is not None:
                            member_receiver = read(scope, source)
            elif config.ts_module == 'tree_sitter_java':
                if node.type == 'object_creation_expression':
                    type_node = node.child_by_field_name('type')

                    if type_node is not None:
                        raw = read(type_node, source).split('<', 1)[0].strip()

                        if raw:
                            callee_name = raw.rsplit('.', 1)[-1]
                elif node.type == 'method_invocation':
                    name_node = node.child_by_field_name('name')

                    if name_node is not None:
                        callee_name = read(name_node, source)
                    receiver = node.child_by_field_name('object')

                    if receiver is not None:
                        is_member_call = True

                        if receiver.type == 'identifier':
                            member_receiver = read(receiver, source)
                        elif receiver.type in ('this', 'super'):
                            member_receiver = receiver.type
                        elif receiver.type == 'field_access':
                            owner = receiver.child_by_field_name('object')
                            field = receiver.child_by_field_name('field')

                            if owner is not None and owner.type == 'this' and (field is not None):
                                member_receiver = f'this.{read(field, source)}'
                                is_this_field_call = True
            elif config.ts_module == 'tree_sitter_ruby':
                meth = node.child_by_field_name('method')

                if meth is not None:
                    callee_name = read(meth, source)
                recv = node.child_by_field_name('receiver')

                if recv is None or recv.type == 'self':
                    self_receiver = 'self'

                if recv is not None:
                    is_member_call = True

                    if recv.type in ('identifier', 'constant'):
                        member_receiver = read(recv, source)
                    elif recv.type == 'scope_resolution':
                        member_receiver = rubyconstfull(recv, source) or None
            else:
                func_node = node.child_by_field_name(config.call_function_field) if config.call_function_field else None

                if func_node is None and node.type == 'new_expression':
                    func_node = node.child_by_field_name('constructor')

                if node.type in JSXELEMENTS:
                    func_node = node.child_by_field_name('name')

                    if func_node is None or func_node.type != 'identifier' or read(func_node, source)[:1].islower():
                        func_node = None

                if func_node:
                    if func_node.type == 'identifier':
                        callee_name = read(func_node, source)
                    elif func_node.type in config.call_accessor_node_types:
                        is_member_call = True

                        if config.call_accessor_field:
                            attr = func_node.child_by_field_name(config.call_accessor_field)

                            if attr:
                                callee_name = read(attr, source)

                        if config.call_accessor_object_field:
                            obj = func_node.child_by_field_name(config.call_accessor_object_field)

                            if obj is not None and obj.type == 'identifier':
                                member_receiver = read(obj, source)
                            elif obj is not None and obj.type in ('this', 'super') and (config.ts_module in SELFLANGS):
                                self_receiver = obj.type
                            elif config.ts_module == 'tree_sitter_python' and obj is not None and (obj.type == 'call'):
                                receiver_func = obj.child_by_field_name('function')

                                if receiver_func is not None and receiver_func.type == 'identifier' and (read(receiver_func, source) == 'super'):
                                    member_receiver = 'super'
                            elif obj is not None and obj.type in config.call_accessor_node_types and config.call_accessor_object_field:
                                inner_obj = obj.child_by_field_name(config.call_accessor_object_field)

                                if inner_obj is not None and inner_obj.type == 'this':
                                    inner_prop = obj.child_by_field_name(config.call_accessor_field)

                                    if inner_prop is not None:
                                        member_receiver = read(inner_prop, source)
                                        is_this_field_call = True
                    else:
                        callee_name = read(func_node, source)

            if config.ts_module == 'tree_sitter_lua' and is_member_call and (member_receiver == 'self') and callee_name:
                _self_table = lua_self_table.get(caller_nid)

                if _self_table:
                    _dot = f'{_self_table}.{callee_name}'
                    callee_name = f'{_self_table}:{callee_name}'

                    if callee_name not in label_to_nid and _dot in label_to_nid:
                        callee_name = _dot
                    member_receiver = None
                    lua_self_qualified = True
            _builtin_member_call = is_member_call and callee_name in GLOBALS
            php_builtin_self_call = config.ts_module == 'tree_sitter_php' and self_receiver == 'this'

            if callee_name and (callee_name not in GLOBALS or _builtin_member_call):
                _csharp_defer = config.ts_module == 'tree_sitter_c_sharp' and is_member_call and member_receiver
                _python_defer = config.ts_module == 'tree_sitter_python' and is_member_call and (member_receiver not in {'self', 'cls', 'super'})
                _java_defer = config.ts_module == 'tree_sitter_java' and is_member_call
                _lua_member_defer = config.ts_module == 'tree_sitter_lua' and is_member_call and (not lua_self_qualified)

                if _python_defer or _java_defer or (_builtin_member_call and (not php_builtin_self_call)) or _lua_member_defer or (is_member_call and member_receiver and (member_receiver[:1].isupper() or is_this_field_call or _csharp_defer)):
                    tgt_nid = None
                else:
                    if config.ts_module == 'tree_sitter_python' and (not is_member_call):
                        curr_scope = caller_nid
                        tgt_nid = None

                        while curr_scope:
                            if curr_scope in lexical_nids_by_scope and callee_name in lexical_nids_by_scope[curr_scope]:
                                tgt_nid = lexical_nids_by_scope[curr_scope][callee_name]
                                break

                            curr_scope = scope_parents.get(curr_scope)

                        if not tgt_nid:
                            tgt_nid = label_to_nid.get(callee_name)
                    elif self_receiver or (config.ts_module == 'tree_sitter_python' and is_member_call and (member_receiver in ('self', 'cls', 'super'))):
                        tgt_nid = selftarget(caller_nid, callee_name.casefold() if php_builtin_self_call else callee_name, self_receiver or member_receiver or '', label_to_nid, scope_parents, method_owner, methods_by_owner, _local_bases, walk_bases=config.ts_module not in ('tree_sitter_javascript', 'tree_sitter_typescript'), require_method_owner=php_builtin_self_call)

                        if php_builtin_self_call and tgt_nid not in method_owner:
                            tgt_nid = None
                    else:
                        tgt_nid = label_to_nid.get(callee_name)

                    if csharp_qualified_prefix and tgt_nid and (not nid_to_sf.get(tgt_nid)):
                        tgt_nid = None
                external_stub_target = bool(tgt_nid and (not nid_to_sf.get(tgt_nid)))

                if tgt_nid and (not external_stub_target):
                    pair = (caller_nid, tgt_nid)

                    if pair not in seen_call_pairs:
                        seen_call_pairs.add(pair)
                        line = node.start_point[0] + 1
                        edges.append({'source': caller_nid, 'target': tgt_nid, 'relation': 'calls', 'context': 'call', 'source_file': str_path, 'source_location': f'L{line}'})
                elif callee_name and (not tgt_nid or external_stub_target):
                    is_py_local_data = config.ts_module == 'tree_sitter_python' and (not is_member_call) and (callee_name in local_bound_names.get(caller_nid, frozenset()) | extra_locals)

                    if not is_py_local_data:
                        rc_entry = {'caller_nid': caller_nid, 'callee': callee_name, 'is_member_call': is_member_call, 'source_file': str_path, 'source_location': f'L{node.start_point[0] + 1}', 'receiver': swift_receiver or member_receiver}

                        if python_receiver_shadowed is not None and is_member_call:
                            rc_entry['_python_receiver_shadowed'] = python_receiver_shadowed(node, member_receiver or '')

                        if config.ts_module == 'tree_sitter_python' and is_member_call:
                            _fn = node.child_by_field_name('function')
                            _recv = _fn.child_by_field_name('object') if _fn is not None else None
                            _fields = python_attr_types.get(method_owner.get(caller_nid, ''), {})
                            _attr_type = _fields.get(pyself(_recv, source) or '')

                            if _attr_type:
                                rc_entry['_python_self_attr_type'] = _attr_type

                        if external_stub_target:
                            rc_entry['via_stub'] = True

                        if member_receiver and config.ts_module == 'tree_sitter_ruby':
                            rc_entry['receiver_type'] = ruby_var_types.get(caller_nid, {}).get(member_receiver)

                        if member_receiver and config.ts_module == 'tree_sitter_python':
                            _py_type = python_var_types.get(caller_nid, {}).get(member_receiver)

                            if _py_type:
                                rc_entry['receiver_type'] = _py_type

                        if member_receiver == 'super' and config.ts_module == 'tree_sitter_python':
                            _sfn = node.child_by_field_name('function')
                            _sobj = _sfn.child_by_field_name('object') if _sfn is not None else None
                            _sargs = _sobj.child_by_field_name('arguments') if _sobj is not None else None

                            if _sargs is not None and any((c.is_named for c in _sargs.children)):
                                rc_entry['_python_super_args'] = True

                        if config.ts_module == 'tree_sitter_cpp':
                            rc_entry['lang'] = 'cpp'

                        if config.ts_module == 'tree_sitter_c_sharp':
                            rc_entry['lang'] = 'csharp'

                            if node.type == 'object_creation_expression':
                                rc_entry['csharp_new'] = True

                            if csharp_qualified_prefix:
                                rc_entry['qualified_prefix'] = csharp_qualified_prefix
                            receiver_type = csscoped(receiver_types, member_receiver, node.start_byte)

                            if receiver_type:
                                rc_entry['receiver_type'] = receiver_type

                        if config.ts_module == 'tree_sitter_java':
                            rc_entry['lang'] = 'java'
                            receiver_type = (receiver_types or {}).get(member_receiver or '')

                            if receiver_type:
                                rc_entry['receiver_type'] = receiver_type

                        if kotlin_qualified_prefix:
                            rc_entry['lang'] = 'kotlin'
                            rc_entry['qualified_prefix'] = kotlin_qualified_prefix

                        if kotlin_object_receiver:
                            rc_entry['lang'] = 'kotlin'
                            rc_entry['kotlin_object_receiver'] = kotlin_object_receiver
                        raw_calls.append(rc_entry)

            if config.ts_module == 'tree_sitter_python':
                args_node = node.child_by_field_name('arguments')

                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals

                    for arg in args_node.children:
                        if arg.type == 'identifier':
                            indirectref(arg, caller_nid, enclosing_locals, 'argument')
                        elif arg.type == 'keyword_argument':
                            indirectref(arg.child_by_field_name('value'), caller_nid, enclosing_locals, 'argument')
                getattr_ref = getattrname(node)

                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    indirectname(ref_name, loc, caller_nid, 'getattr')
            elif config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
                args_node = node.child_by_field_name('arguments')

                if args_node is not None:
                    enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals

                    for arg in args_node.children:
                        if arg.type == 'identifier':
                            indirectref(arg, caller_nid, enclosing_locals, 'argument')

            if callee_name and callee_name in config.helper_fn_names:
                args_node = node.child_by_field_name('arguments')
                first_key: str | None = None

                if args_node:
                    for arg in args_node.children:
                        if arg.type != 'argument':
                            continue

                        for inner in arg.children:
                            if inner.type == 'string':
                                for sc in inner.children:
                                    if sc.type == 'string_content':
                                        first_key = read(sc, source)
                                        break

                                break

                        if first_key:
                            break

                if first_key:
                    segment = first_key.split('.')[0]
                    tgt_nid = label_to_nid_ci.get(segment.lower()) or label_to_nid_ci.get(f'{segment}.php'.lower())

                    if tgt_nid and tgt_nid != caller_nid:
                        relation = f'uses_{callee_name}'
                        pair3 = (caller_nid, tgt_nid, relation)

                        if pair3 not in seen_helper_ref_pairs:
                            seen_helper_ref_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({'source': caller_nid, 'target': tgt_nid, 'relation': relation, 'source_file': str_path, 'source_location': f'L{line}'})

            if node.type == 'member_call_expression' and callee_name and (callee_name in config.container_bind_methods):
                args_node = node.child_by_field_name('arguments')
                class_args: list[str] = []

                if args_node:
                    for arg in args_node.children:
                        if arg.type != 'argument':
                            continue

                        for inner in arg.children:
                            if inner.type == 'class_constant_access_expression':
                                cls = phpconstscope(inner)

                                if cls:
                                    class_args.append(cls)
                                break

                        if len(class_args) >= 2:
                            break

                if len(class_args) == 2:
                    contract_name, impl_name = class_args
                    contract_nid = label_to_nid_ci.get(contract_name.lower())
                    impl_nid = label_to_nid_ci.get(impl_name.lower())

                    if contract_nid and impl_nid and (contract_nid != impl_nid):
                        pair3 = (contract_nid, impl_nid, 'bound_to')

                        if pair3 not in seen_bind_pairs:
                            seen_bind_pairs.add(pair3)
                            line = node.start_point[0] + 1
                            edges.append({'source': contract_nid, 'target': impl_nid, 'relation': 'bound_to', 'source_file': str_path, 'source_location': f'L{line}'})

        if node.type in config.static_prop_types:
            scope_node = node.child_by_field_name('scope')

            if scope_node is None:
                for child in node.children:
                    if child.is_named and child.type in ('name', 'qualified_name', 'identifier'):
                        scope_node = child
                        break

            if scope_node is not None:
                class_name = read(scope_node, source)
                tgt_nid = label_to_nid_ci.get(class_name.lower())

                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, 'uses_static_prop')

                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({'source': caller_nid, 'target': tgt_nid, 'relation': 'uses_static_prop', 'source_file': str_path, 'source_location': f'L{line}'})

        if config.ts_module == 'tree_sitter_php' and node.type == 'class_constant_access_expression':
            class_name = phpconstscope(node)

            if class_name:
                tgt_nid = label_to_nid_ci.get(class_name.lower())

                if tgt_nid and tgt_nid != caller_nid:
                    pair3 = (caller_nid, tgt_nid, 'references_constant')

                    if pair3 not in seen_static_ref_pairs:
                        seen_static_ref_pairs.add(pair3)
                        line = node.start_point[0] + 1
                        edges.append({'source': caller_nid, 'target': tgt_nid, 'relation': 'references_constant', 'source_file': str_path, 'source_location': f'L{line}'})

        if config.ts_module == 'tree_sitter_python' and node.type in ('dictionary', 'list', 'set', 'tuple'):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals

            for ident in pydispatch(node):
                indirectref(ident, caller_nid, enclosing_locals, 'collection')
        elif config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type in ('object', 'array'):
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals

            for ident in jsdispatch(node):
                indirectref(ident, caller_nid, enclosing_locals, 'collection')

        if config.ts_module == 'tree_sitter_python' and node.type == 'assignment':
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals

            for ident in pyrefvalues(node.child_by_field_name('right')):
                indirectref(ident, caller_nid, enclosing_locals, 'assignment')
        elif config.ts_module == 'tree_sitter_python' and node.type == 'return_statement':
            enclosing_locals = local_bound_names.get(caller_nid, frozenset()) | extra_locals
            value = next((c for c in node.children if c.is_named), None)

            for ident in pyrefvalues(value):
                indirectref(ident, caller_nid, enclosing_locals, 'return')

        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type == 'catch_clause':
            param = node.child_by_field_name('parameter')

            if param is not None:
                caught: set[str] = set()
                jspatterns(param, source, caught)
                extra_locals = extra_locals | frozenset(caught)

        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type in ('statement_block', 'for_statement'):
            block_locals = jslexical(node, source)

            if block_locals:
                extra_locals = extra_locals | block_locals

        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript') and node.type == 'for_in_statement':
            left = node.child_by_field_name('left')

            if left is not None:
                loop_locals: set[str] = set()
                jspatterns(left, source, loop_locals)

                if loop_locals:
                    extra_locals = extra_locals | frozenset(loop_locals)

        for child in node.children:
            walkcalls(child, caller_nid, receiver_types, extra_locals)
    python_attr_types: dict[str, dict[str, str | None]] = {}

    if config.ts_module == 'tree_sitter_python':
        for caller_nid, body_node in function_bodies:
            if caller_nid in method_owner:
                fields = python_attr_types.setdefault(method_owner[caller_nid], {})

                for attr, type_name in pybindings(body_node, source):
                    fields[attr] = type_name if fields.get(attr, type_name) == type_name else None

    if config.ts_module == 'tree_sitter_ruby':
        for caller_nid, body_node in function_bodies:
            ruby_var_types[caller_nid] = rubylocalclasses(body_node, source)
            ruby_local_names[caller_nid] = rubylocals(body_node, source)

    if config.ts_module == 'tree_sitter_python':
        for caller_nid, body_node in function_bodies:
            python_var_types[caller_nid] = pylocalclasses(body_node, source)

    if config.ts_module == 'tree_sitter_cpp':
        for _caller_nid, body_node in function_bodies:
            cpplocals(body_node, source, type_table)

    if config.ts_module == 'tree_sitter_swift':
        for _caller_nid, body_node in function_bodies:
            swiftlocals(body_node, source, type_table, factory=swift_factory_bindings)
    _tracked_body_ids.update((b for _, b in function_bodies))
    receiver_types_by_body = {**java_receiver_types, **csharp_receiver_types}

    for caller_nid, body_node in function_bodies:
        walkcalls(body_node, caller_nid, receiver_types_by_body.get(id(body_node)), frozenset(closure_locals_by_body.get(id(body_node), ())))

    for owner_nid, init_node in initializer_nodes:
        walkcalls(init_node, owner_nid)
    seen_listen_pairs: set[tuple[str, str]] = set()

    for event_name, listener_name, line in pending_listen_edges:
        event_nid = label_to_nid_ci.get(event_name.lower())
        listener_nid = label_to_nid_ci.get(listener_name.lower())

        if not event_nid or not listener_nid or event_nid == listener_nid:
            continue

        pair2 = (event_nid, listener_nid)

        if pair2 in seen_listen_pairs:
            continue

        seen_listen_pairs.add(pair2)
        edges.append({'source': event_nid, 'target': listener_nid, 'relation': 'listened_by', 'source_file': str_path, 'source_location': f'L{line}'})

    if config.ts_module == 'tree_sitter_python':
        module_bound = pymodules(root, source)

        def scanmodule(n) -> None:
            if n.type in ('function_definition', 'class_definition'):
                return

            if n.type in ('dictionary', 'list', 'set', 'tuple'):
                for ident in pydispatch(n):
                    indirectref(ident, file_nid, module_bound, 'collection')
            elif n.type == 'assignment':
                for ident in pyrefvalues(n.child_by_field_name('right')):
                    indirectref(ident, file_nid, module_bound, 'assignment')
            elif n.type == 'call':
                getattr_ref = getattrname(n)

                if getattr_ref is not None:
                    ref_name, loc = getattr_ref
                    indirectname(ref_name, loc, file_nid, 'getattr')

            for c in n.children:
                scanmodule(c)
        scanmodule(root)
    elif config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
        js_module_bound = jsmodules(root, source)

        def scanjsmodule(n) -> None:
            if n.type in JSBOUNDARY:
                return

            if n.type in ('object', 'array'):
                for ident in jsdispatch(n):
                    indirectref(ident, file_nid, js_module_bound, 'collection')
            elif n.type in ('call_expression', 'new_expression'):
                margs = n.child_by_field_name('arguments')

                if margs is not None:
                    for marg in margs.children:
                        if marg.type == 'identifier':
                            indirectref(marg, file_nid, js_module_bound, 'argument')

            for c in n.children:
                scanjsmodule(c)
        scanjsmodule(root)
    valid_ids = seen_ids
    clean_edges = []
    _seen_edge_payloads: set[str] = set()

    for edge in edges:
        src, tgt = (edge['source'], edge['target'])

        if src in valid_ids and (tgt in valid_ids or edge['relation'] in ('imports', 'imports_from', 're_exports')):
            payload = json.dumps(edge, sort_keys=True, default=str)

            if payload in _seen_edge_payloads:
                continue

            _seen_edge_payloads.add(payload)
            clean_edges.append(edge)

    if _ruby_mixin_calls:
        raw_calls.extend(_ruby_mixin_calls)
    result = {'nodes': nodes, 'edges': clean_edges, 'raw_calls': raw_calls}
    _field_table_export = [{'lang': _lang, 'class_label': _n.get('label'), 'source_file': _n.get('source_file'), 'fields': dict(_tbl)} for _lang, _tables in (('java', java_field_types), ('csharp', csharp_field_types)) for _cls, _tbl in _tables.items() if _tbl for _n in (next((x for x in nodes if x['id'] == _cls), None),) if _n is not None and _n.get('label')]

    if _field_table_export:
        result['member_field_tables'] = _field_table_export

    if root.has_error:
        result['parse_errors'] = {'first_error_line': firsterror(root), 'multiline_error': multilineerror(root)}

    if config.ts_module == 'tree_sitter_kotlin':
        _pkg = ktpackage(root, source)

        if _pkg:
            result['kotlin_package'] = _pkg

    if callable_def_nids:
        for n in nodes:
            if n['id'] in callable_def_nids:
                n['_callable'] = True

                if n['id'] in callable_class_nids:
                    n['_callable_class'] = True

    if swift_extensions:
        result['swift_extensions'] = swift_extensions

    if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
        tsreceivers(root, source, type_table)

    if config.ts_module == 'tree_sitter_swift':
        if type_table or swift_factory_bindings:
            result['swift_type_table'] = {'path': str_path, 'table': type_table}

            if swift_factory_bindings:
                result['swift_type_table']['factory'] = {k: list(v) for k, v in swift_factory_bindings.items()}
    elif type_table:
        if config.ts_module in ('tree_sitter_javascript', 'tree_sitter_typescript'):
            result['ts_type_table'] = {'path': str_path, 'table': type_table}
        elif config.ts_module == 'tree_sitter_cpp':
            result['cpp_type_table'] = {'path': str_path, 'table': type_table}

    return result


def pydecorator(deco_node, source: bytes) -> str | None:
    for child in deco_node.children:
        if not child.is_named:
            continue

        target = child

        if target.type == 'call':
            target = target.child_by_field_name('function') or target

        if target.type == 'attribute':
            attr = target.child_by_field_name('attribute')

            return read(attr, source) if attr else None

        if target.type == 'identifier':
            return read(target, source)

        return None

    return None


def tsdecorator(deco_node, source: bytes) -> str | None:
    for child in deco_node.children:
        if not child.is_named:
            continue

        target = child

        if target.type == 'call_expression':
            target = target.child_by_field_name('function') or target

        if target.type == 'member_expression':
            prop = target.child_by_field_name('property')

            return read(prop, source) if prop else None

        if target.type == 'identifier':
            return read(target, source)

        return None

    return None


def tsmethod(method_node, source: bytes) -> str | None:
    name_node = method_node.child_by_field_name('name')

    return read(name_node, source) if name_node else None


def tsdescendants(node) -> list:
    out: list = []

    def rec(n, top: bool) -> None:
        for child in n.children:
            ct = child.type

            if ct == 'decorator':
                out.append(child)
            elif ct in ('class_declaration', 'abstract_class_declaration'):
                continue
            elif ct == 'method_definition' and (not top):
                continue
            else:
                rec(child, False)
    rec(node, True)

    return out


def tsedges(class_node, class_nid: str, stem: str, source: bytes, ensurenode, addedge) -> None:

    def emit(deco_node, owner_nid: str) -> None:
        name = tsdecorator(deco_node, source)

        if not name:
            return
        line = deco_node.start_point[0] + 1
        target = ensurenode(name, line)

        if target != owner_nid:
            addedge(owner_nid, target, 'references', line, context='decorator')

    for child in class_node.children:
        if child.type == 'decorator':
            emit(child, class_nid)
    parent = class_node.parent

    if parent is not None and parent.type == 'export_statement':
        for child in parent.children:
            if child.type == 'decorator':
                emit(child, class_nid)
            elif child.type in ('class_declaration', 'abstract_class_declaration'):
                break

    body = next((c for c in class_node.children if c.type == 'class_body'), None)

    if body is None:
        return

    for member in body.children:
        mt = member.type

        if mt == 'decorator':
            owner = class_nid
            sib = member.next_named_sibling

            while sib is not None and sib.type == 'decorator':
                sib = sib.next_named_sibling

            if sib is not None and sib.type == 'method_definition':
                mname = tsmethod(sib, source)

                if mname:
                    owner = make(class_nid, mname)
            emit(member, owner)
        elif mt == 'method_definition':
            mname = tsmethod(member, source)
            m_nid = make(class_nid, mname) if mname else class_nid

            for deco in tsdescendants(member):
                emit(deco, m_nid)
        else:
            for deco in tsdescendants(member):
                emit(deco, class_nid)