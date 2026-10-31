import json
import logging
import os
import subprocess
import sys
from pathlib import Path

# Setup paths
root = Path(__file__).resolve().parent.parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

import graphify
from agents import retriever
from src.dedup import filter as dedup
from utils.logger import setup_logger

RepoRetrieverAgent = retriever.RepoRetrieverAgent
log = setup_logger()

DELTA = 5


# Normalize path
def norm(val):
    clean = str(val or "").replace("\\", "/").strip().lstrip("/")
    return clean


# Extract source code line
def snippet(repo, file, line):
    if not file or not line:
        return ""
    path = Path(repo) / norm(file)
    if not path.exists() or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if isinstance(line, str) and "-" in line:
            items = [int(x) for x in line.split("-")]
            start = max(1, items[0])
            end = min(len(lines), items[-1])
            return "\n".join(lines[start - 1 : end]).strip()
        idx = int(line) - 1
        if 0 <= idx < len(lines):
            return lines[idx].strip()
    except Exception:
        pass
    return ""


# Calculate line distance
def dist(pred, ref):
    if pred is None or ref is None:
        return 999999
    try:
        if isinstance(ref, str) and "-" in ref:
            items = [int(x) for x in ref.split("-")]
            start = items[0]
            end = items[-1]
            pos = int(pred)
            if start <= pos <= end:
                return 0
            return min(abs(pos - start), abs(pos - end))
        return abs(int(pred) - int(ref))
    except Exception:
        return 999999


# Match code location
def match(sf, sl, rf, rl, delta=DELTA):
    src = norm(sf)
    ref = norm(rf)
    f_ok = (src == ref) or src.endswith(ref) or ref.endswith(src)
    if not f_ok:
        return False, False
    l_ok = dist(sl, rl) <= delta
    return f_ok, l_ok


# Clone repo
def clone(url, dest):
    dest = Path(dest)
    if not dest.exists():
        log.info(f"Cloning {url} to {dest}...")
        subprocess.run(["git", "clone", url, str(dest)], check=True)
    return dest


# Checkout commit
def checkout(repo, commit):
    log.info(f"Checking {commit}...")
    subprocess.run(["git", "-C", str(repo), "checkout", "-f", commit], check=True)


# Generate call graph and flows using graphify
def graph(repo, graphs, flows):
    graphify.callgraph(str(repo), str(graphs), flow=str(flows))

    if flows.exists():
        raw = flows.read_text(encoding="utf-8").splitlines()
        clean = dedup(raw)
        flows.write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in clean) + "\n",
            encoding="utf-8",
        )


# Run retriever
def retrieve(repo, graphs, flows, taints, box=None, entries=None, commit=None):
    config = root / "config" / "config.yml"
    agent = RepoRetrieverAgent(sandbox=box, config_path=str(config))

    target = "/data" if hasattr(box, "attrs") else str(repo)
    g_path = "/outputs/call-graph.json" if hasattr(box, "attrs") else str(graphs)
    f_path = "/outputs/call-flows.jsonl" if hasattr(box, "attrs") else str(flows)
    t_path = "/outputs/taint-flows.jsonl" if hasattr(box, "attrs") else str(taints)

    agent.run(
        target,
        graph_path=g_path,
        flows_path=f_path,
        taint_path=t_path,
        entries=entries,
        commit=commit,
    )
    return taints


def convert(flow, repo, url, commit):
    src = flow.get("source") or {}
    snk = flow.get("sink") or {}

    sf = src.get("file")
    sl = src.get("line")
    kf = snk.get("file")
    kl = snk.get("line")

    ep = {
        "file": sf,
        "line": sl,
        "code": snippet(repo, sf, sl),
    }

    co = {
        "file": kf,
        "line": kl,
        "code": snippet(repo, kf, kl),
    }

    # Full trace = [EP] + transforms + [CO]
    trace = [dict(ep)]
    if "transforms" in flow and isinstance(flow["transforms"], list):
        for t in flow["transforms"]:
            if isinstance(t, dict):
                tf = t.get("file")
                tl = t.get("line")
                trace.append({
                    "file": tf,
                    "line": tl,
                    "code": snippet(repo, tf, tl),
                })
    trace.append(dict(co))

    item = {
        "repo_url": url,
        "commit": commit,
        "entry_point": ep,
        "critical_operation": co,
        "trace": trace,
    }
    return item


# Export mapped findings to JSONL
def export(flows, dest, repo, url, commit):
    lines = []
    for f in flows:
        item = convert(f, repo, url, commit)
        lines.append(json.dumps(item, ensure_ascii=False))

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    log.info(f"Exported {len(lines)} mapped findings to {dest}")
    return dest


# Evaluate predictions against ground truth entries
def evaluate(taints, entries, commit):
    flows = []
    if isinstance(taints, (list, tuple)):
        flows = list(taints)
    elif Path(taints).exists():
        for line in Path(taints).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    flows.append(json.loads(line))
                except Exception:
                    pass

    target = [e for e in entries if e.get("commit") == commit]
    if not target:
        target = entries

    log.info(f"Evaluating {len(flows)} flows against {len(target)} ground truth entries...")

    hits = 0
    ep_files = 0
    ep_lines = 0
    co_files = 0
    co_lines = 0

    results = []

    for entry in target:
        eid = entry.get("entry_id")
        ep = entry.get("entry_point") or {}
        co = entry.get("critical_operation") or {}

        ef = ep.get("file")
        el = ep.get("line")
        cf = co.get("file")
        cl = co.get("line")

        hit_ef = False
        hit_el = False
        hit_cf = False
        hit_cl = False
        hit = False

        for flow in flows:
            src = flow.get("source") or {}
            snk = flow.get("sink") or {}

            sf = src.get("file")
            sl = src.get("line")
            kf = snk.get("file")
            kl = snk.get("line")

            f_ef, l_el = match(sf, sl, ef, el)
            f_cf, l_cl = match(kf, kl, cf, cl)

            if f_ef:
                hit_ef = True
            if l_el:
                hit_el = True
            if f_cf:
                hit_cf = True
            if l_cl:
                hit_cl = True
            if l_el and l_cl:
                hit = True

        if hit_ef:
            ep_files += 1
        if hit_el:
            ep_lines += 1
        if hit_cf:
            co_files += 1
        if hit_cl:
            co_lines += 1
        if hit:
            hits += 1

        results.append({
            "entry": eid,
            "ep_file": hit_ef,
            "ep_line": hit_el,
            "co_file": hit_cf,
            "co_line": hit_cl,
            "hit": hit,
        })

    total = len(target)
    summary = {
        "total": total,
        "hits": hits,
        "er": (hits / total) if total else 0.0,
        "ep_fr": (ep_files / total) if total else 0.0,
        "ep_lr": (ep_lines / total) if total else 0.0,
        "co_fr": (co_files / total) if total else 0.0,
        "co_lr": (co_lines / total) if total else 0.0,
        "details": results,
    }
    return summary


def main():
    bench = Path(__file__).resolve().parent
    meta = bench / "entries.jsonl"
    if not meta.exists():
        log.error("entries.jsonl not found!")
        return

    entries = [
        json.loads(l) for l in meta.read_text(encoding="utf-8").splitlines() if l.strip()
    ]

    # Target repository
    url = "https://github.com/jlowin/fastmcp"
    repo = bench / "repos" / "fastmcp"
    commit = "6bade1cbd973cbbbca26a84ed7c4cc58ecfda5b3"

    # Step 1: Clone
    clone(url, repo)

    # Step 2: Checkout commit
    checkout(repo, commit)

    # Step 3: Setup output dir INSIDE target repo
    outs = repo / "outputs"
    outs.mkdir(parents=True, exist_ok=True)
    graphs = outs / "call-graph.json"
    flows = outs / "call-flows.jsonl"
    taints = outs / "taint-flows.jsonl"
    findings = outs / "findings.jsonl"
    report = outs / "eval.json"

    # Step 4: Graphify
    if not graphs.exists() or not flows.exists():
        graph(repo, graphs, flows)

    # Step 5: Sandbox
    from docker.sandbox import init as sandbox
    box = sandbox(repo_path=str(repo), outputs_path=str(outs))

    # Step 6: Retrieve
    retrieve(repo, graphs, flows, taints, box=box, entries=entries, commit=commit)

    # Step 7: Map output
    raw = []
    if taints.exists():
        for line in taints.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    raw.append(json.loads(line))
                except Exception:
                    pass
    export(raw, findings, repo, url, commit)

    # Step 8: Evaluate
    res = evaluate(taints, entries, commit)
    report.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 55)
    print("        VULNGYM BENCHMARK")
    print("=" * 55)
    print(f"Target repo       : fastmcp")
    print(f"Target commit     : {commit}")
    print(f"Directory         : {outs}")
    print(f"Findings          : {findings}")
    print(f"Total entries     : {res['total']}")
    print(f"Entries Hit (ER)  : {res['hits']} ({res['er'] * 100:.2f}%)")
    print(f"EP File Recall    : {res['ep_fr'] * 100:.2f}%")
    print(f"EP Line Recall    : {res['ep_lr'] * 100:.2f}%")
    print(f"CO File Recall    : {res['co_fr'] * 100:.2f}%")
    print(f"CO Line Recall    : {res['co_lr'] * 100:.2f}%")
    print("=" * 55)


if __name__ == "__main__":
    main()
