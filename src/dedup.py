def score(flow):
    if "primary_vector" in flow:
        return flow["primary_vector"].get("score", 0)
    sink = flow.get("sink", {}) or {}
    op = str(sink.get("func") or sink.get("operation") or sink.get("function") or "").lower()
    src = flow.get("source", {}) or {}
    ep = str(src.get("ep") or src.get("function") or "").lower()
    var = str(src.get("variable") or "").lower()
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
        key = f"{sink.get('file')}:{sink.get('function')}:{sink.get('line')}:{sink.get('operation')}"
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
        line_val = _parse_line(sink.get("line") or sink.get("lines"))
        func_val = str(sink.get("func") or sink.get("function") or sink.get("operation") or "").strip()
        var_val = str(sink.get("variable") or "").strip()

        key = (file_val, line_val, func_val)
        cur_score = score(f)
        vector = {
            "id": f.get("id", ""),
            "score": cur_score,
            "source": f.get("source", {}) or {},
            "chain": f.get("chain") or f.get("flow", {}).get("chain", []) or []
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
            "primary_vector": pv,
            "alternative_vectors": alts
        })

    return combined


__all__ = ["score", "dedup", "rank", "combine"]
