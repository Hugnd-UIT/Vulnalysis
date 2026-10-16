def score(flow):
    sink = flow.get("sink", {}) or {}
    op = str(sink.get("operation") or sink.get("function") or "").lower()
    src = flow.get("source", {}) or {}
    ep = str(src.get("ep") or src.get("function") or "").lower()
    chain = flow.get("chain") or flow.get("flow", {}).get("chain", []) or []

    pts = 2
    rce_ops = (
        "system", "exec", "popen", "eval", "spawn", "passthru",
        "subprocess", "shell_exec", "check_output", "render_template",
        "compile", "command", "process"
    )
    memory_ops = (
        "strcpy", "strcat", "sprintf", "gets", "memcpy", "free",
        "malloc", "realloc", "unsafe", "transmute", "from_raw_parts"
    )
    serial_ops = (
        "pickle", "yaml", "shelve", "marshal", "unserialize",
        "readobject", "binaryformatter", "xmldecoder"
    )
    leak_ops = (
        "open", "read", "send_file", "readfile", "writefile",
        "remove", "unlink", "shutil", "file"
    )
    web_ops = (
        "query", "select", "sql", "cursor", "execute",
        "requests", "urlopen", "fetch", "render", "template"
    )

    if any(k in op for k in rce_ops):
        pts += 10
    elif any(k in op for k in memory_ops):
        pts += 8
    elif any(k in op for k in serial_ops):
        pts += 7
    elif any(k in op for k in leak_ops):
        pts += 6
    elif any(k in op for k in web_ops):
        pts += 5

    src_keys = (
        "argv", "param", "query", "request", "body", "stdin",
        "recv", "header", "cookie", "input", "args", "form",
        "upload", "file", "url", "socket", "req"
    )
    if any(k in ep for k in src_keys):
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
        key = f"{sink.get('file')}:{sink.get('function')}:{sink.get('line')}:{sink.get('operation')}"
        cur_pts = score(f)
        if key not in clusters or cur_pts > clusters[key][0]:
            clusters[key] = (cur_pts, f)
    return [item[1] for item in clusters.values()]


def rank(flows):
    return sorted(flows, key=lambda f: score(f), reverse=True)


__all__ = ["score", "dedup", "rank"]
