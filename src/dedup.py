def _match_op(op, ops):
    if not op:
        return False
    op_lower = op.lower()
    for k in ops:
        if k == op_lower or op_lower.endswith(f".{k}") or op_lower.endswith(f"::{k}") or f"_{k}" in op_lower or f"{k}_" in op_lower:
            return True
    return False


def score(flow):
    if "primary_vector" in flow:
        return flow["primary_vector"].get("score", 0)
    sink = flow.get("sink", {}) or {}
    op = str(sink.get("func") or "").lower()
    sink_file = str(sink.get("file") or "").lower()
    src = flow.get("source", {}) or {}
    ep = str(src.get("func") or "").lower()
    var = str(src.get("variable") or "").lower()
    chain = flow.get("chain") or flow.get("flow", {}).get("chain", []) or []

    pts = 2

    conf = flow.get("confidence") or 0
    fid = str(flow.get("id") or "").lower()
    if conf >= 0.7 or fid.startswith(("tf", "ct")) or flow.get("verdict") == "VULNERABLE":
        pts += 10

    rce_ops = (
        "system", "exec", "popen", "eval", "spawn", "passthru",
        "subprocess", "shell_exec", "check_output", "render_template",
        "compile", "command", "process", "processbuilder", "runtime",
        "child_process", "execsync", "spawnsync", "fork", "runincontext",
        "proc_open", "pcntl_exec", "assert", "create_function", "open3",
        "execl", "execv", "execle", "execve", "execvp"
    )

    memory_ops = (
        "strcpy", "strncpy", "strcat", "strncat", "sprintf", "vsprintf",
        "snprintf", "vsnprintf", "gets", "memcpy", "memmove", "free",
        "malloc", "calloc", "realloc", "alloca", "valloc", "reallocarray",
        "memset", "bcopy", "unsafe", "transmute", "from_raw_parts",
        "scanf", "sscanf", "fscanf", "asprintf", "dprintf",
        "talloc_free", "talloc_const_free", "fr_radius_packet_free",
        "freeaddrinfo", "lt_const_free", "lt_malloc"
    )

    is_c_cpp = any(sink_file.endswith(ext) for ext in (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"))
    fmt_ops = ("printf", "fprintf", "vprintf", "vfprintf", "syslog", "asprintf", "dprintf", "snprintf", "sprintf")

    if _match_op(op, rce_ops):
        pts += 10
    elif is_c_cpp and _match_op(op, fmt_ops):
        pts += 8
    elif _match_op(op, memory_ops):
        pts += 8

    serial_ops = (
        "pickle", "yaml", "shelve", "marshal", "unserialize",
        "readobject", "readunshared", "binaryformatter", "xmldecoder",
        "kryo", "hessian", "fastjson", "objectinputstream", "node-serialize"
    )
    if _match_op(op, serial_ops):
        pts += 7

    leak_ops = (
        "open", "fopen", "read", "send_file", "readfile", "writefile",
        "remove", "unlink", "shutil", "file", "fileinputstream",
        "fileoutputstream", "filereader", "filewriter", "paths.get",
        "file_get_contents", "file_put_contents", "include", "require"
    )
    if _match_op(op, leak_ops):
        pts += 6

    web_ops = (
        "query", "select", "sql", "cursor", "execute",
        "requests", "urlopen", "fetch", "render", "template",
        "sqlite3", "mysql", "pqexec", "preparestatement", "executequery",
        "initialcontext", "lookup", "axios", "curl_exec", "findone"
    )
    if _match_op(op, web_ops):
        pts += 5

    src_keys = (
        "argv", "param", "query", "request", "body", "stdin",
        "recv", "header", "cookie", "input", "args", "form",
        "upload", "file", "url", "socket", "req"
    )
    if any(k in ep for k in src_keys) or any(k in var for k in src_keys):
        pts += 3

    chain_len = len(chain)
    if 0 < chain_len <= 4:
        pts += 2
    elif 5 <= chain_len <= 9:
        pts += 1
    elif chain_len >= 10:
        pts -= 1

    return pts


def dedup(flows):
    clusters = {}
    for f in flows:
        sink = f.get("sink", {}) or {}
        key = f"{sink.get('file')}:{sink.get('func')}:{sink.get('line')}"
        cur_pts = score(f)
        if key not in clusters or cur_pts > clusters[key][0]:
            clusters[key] = (cur_pts, f)
    return [item[1] for item in clusters.values()]


def rank(flows):
    return sorted(flows, key=lambda f: score(f), reverse=True)


def _parse_line(val):
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else None


def combine(flows):
    flat_flows = []
    for f in flows:
        if "primary_vector" in f and isinstance(f["primary_vector"], dict):
            sink = f.get("sink", {}) or {}
            pv = dict(f["primary_vector"])
            pv["sink"] = sink
            for meta_k in ("verdict", "confidence", "title", "cwe"):
                if meta_k in f and meta_k not in pv:
                    pv[meta_k] = f[meta_k]
            flat_flows.append(pv)
            for av in f.get("alternative_vectors", []):
                if isinstance(av, dict):
                    av_item = dict(av)
                    av_item["sink"] = sink
                    flat_flows.append(av_item)
        else:
            flat_flows.append(f)

    clusters = {}
    for f in flat_flows:
        sink = f.get("sink", {}) or {}
        file_val = str(sink.get("file") or "").strip()
        line_val = _parse_line(sink.get("line"))
        func_val = str(sink.get("func") or "").strip()
        var_val = str(sink.get("variable") or "").strip()

        key = (file_val, line_val, func_val)
        cur_score = score(f)
        vector = {
            "id": f.get("id", ""),
            "score": cur_score,
            "source": f.get("source", {}) or {},
            "chain": f.get("chain", []) or []
        }
        if f.get("transforms"):
            vector["transforms"] = f.get("transforms")
        if f.get("confidence") is not None:
            vector["confidence"] = f.get("confidence")

        if key not in clusters:
            clusters[key] = {
                "sink": {
                    "file": file_val,
                    "line": line_val,
                    "func": func_val,
                    "variable": var_val
                },
                "vectors": []
            }
        elif not clusters[key]["sink"].get("variable") and var_val:
            clusters[key]["sink"]["variable"] = var_val

        clusters[key]["vectors"].append(vector)

    cluster_list = list(clusters.values())
    for item in cluster_list:
        item["vectors"].sort(key=lambda v: v.get("score", 0), reverse=True)
    cluster_list.sort(key=lambda item: item["vectors"][0].get("score", 0) if item["vectors"] else 0, reverse=True)

    combined = []
    for idx, item in enumerate(cluster_list, 1):
        if not item["vectors"]:
            continue
        pv = item["vectors"][0]
        alts = item["vectors"][1:]
        combined.append({
            "id": f"CT{idx:02d}",
            "sink": item["sink"],
            "source": pv.get("source", {}) or {},
            "chain": pv.get("chain", []) or [],
            "verdict": pv.get("verdict", "BENIGN"),
            "confidence": pv.get("confidence", 0.1),
            "primary_vector": pv,
            "alternative_vectors": alts
        })

    return combined


SAFE_FUNCS = {
    "isinstance", "issubclass", "len", "type", "repr", "id", "callable",
    "hasattr", "getattr", "setattr", "sys.version", "format", "strip",
    "split", "join", "replace", "lower", "upper", "startswith", "endswith",
    "int", "float", "bool", "list", "dict", "set", "tuple", "range",
    "enumerate", "zip", "min", "max", "abs", "sum", "round",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "StopIteration", "super", "super.__setitem__",
    "logging.basicConfig", "logging.getLogger", "logger.debug", "logger.info",
    "print", "sys.exit", "encode", "decode", "items", "keys", "values",
    "append", "extend", "pop", "insert", "clear", "update", "rstrip", "lstrip",
    "sleep", "strftime", "datetime", "now", "utcnow", "basename", "dirname",
    "RawConfigParser", "ConfigParser", "ArgumentParser", "add_argument", "parse_args",
    "sizeof", "strlen", "isdigit", "isalpha", "isalnum", "isspace",
    "isupper", "islower", "toupper", "tolower", "abs", "labs",
    "strcmp", "strncmp", "memcmp", "atoi", "atol", "atof", "strchr",
    "strrchr", "strstr",
    "getClass", "equals", "hashCode", "toString", "valueOf", "length",
    "size", "isEmpty", "contains", "compareTo", "Math.abs", "Math.min",
    "typeof", "Array.isArray", "Object.keys", "Object.values",
    "Object.entries", "console.log", "console.debug", "console.info",
    "Boolean", "Number", "String", "parseInt", "parseFloat", "trim",
    "cap", "make", "new", "strings.EqualFold", "strings.Contains",
    "strings.HasPrefix", "strings.HasSuffix", "strings.TrimSpace",
    "is_array", "is_string", "is_int", "is_numeric", "is_bool", "is_null",
    "empty", "isset", "strtolower", "strtoupper", "strpos",
    "size_of", "align_of", "clone", "to_string", "is_empty", "unwrap",
    "GetType", "Math.Abs", "Math.Min", "Math.Max",
    "is_a?", "kind_of?", "nil?", "empty?", "to_s", "to_i"
}

DANGEROUS_STEMS = (
    "printf", "sprintf", "snprintf", "vsnprintf", "asprintf", "syslog",
    "free", "alloc", "malloc", "realloc", "calloc", "memcpy", "memmove",
    "strcpy", "strcat", "gets", "system", "exec", "popen", "spawn",
    "eval", "sql", "query", "cursor", "open", "read", "write", "unlink",
    "remove", "pickle", "yaml", "unserialize", "readobject"
)


def filter(flows):
    import json
    cleaned = []
    seen = {}
    for f in flows:
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except Exception:
                continue
        if not isinstance(f, dict):
            continue

        src = f.get("src", {}) or {}
        dst = f.get("dst", {}) or {}
        func = str(src.get("func") or src.get("id") or "").strip()
        fn_lower = func.lower()
        dst_file = str(dst.get("file") or "").lower()

        is_dangerous = any(s in fn_lower for s in DANGEROUS_STEMS)
        is_internal = any(dst_file.endswith(ext) for ext in (".c", ".cpp", ".cc", ".h")) and (
            func.startswith(("fr_", "rad_", "rlm_", "eap_", "tls_", "pair_"))
            or any(func.endswith(p) for p in ("_exec", "_eval", "_send", "_recv", "_parse", "_alloc", "_free"))
        )

        if not is_dangerous and not is_internal:
            last_part = func.split(".")[-1]
            if func in SAFE_FUNCS or fn_lower in SAFE_FUNCS or last_part in SAFE_FUNCS or last_part.lower() in SAFE_FUNCS:
                continue

        key = (dst.get("file"), dst.get("line"), func)
        if key in seen:
            existing = seen[key]
            cur_paths = existing.setdefault("paths", [])
            for p in f.get("paths", []):
                if p not in cur_paths:
                    cur_paths.append(p)
        else:
            seen[key] = f
            cleaned.append(f)

    return cleaned


__all__ = ["score", "dedup", "rank", "combine", "filter"]
