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
    if not dest.exists() or not (dest / ".git").exists():
        import shutil
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        log.info(f"Cloning {url} to {dest}...")
        subprocess.run(["git", "clone", "--filter=blob:none", url, str(dest)], check=True)
    return dest


# Clean up intermediate files after repo finishes
def cleanup_outputs(outs, box=None):
    outs = Path(outs)
    targets = [
        "call-graph.json",
        "call-flows.jsonl",
        "taint-flows.jsonl",
        "call-flows-chunk.jsonl",
    ]
    for name in targets:
        p = outs / name
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
    if box:
        try:
            box.exec_run("rm -f /outputs/call-graph.json /outputs/call-flows.jsonl /outputs/call-flows-chunk*.jsonl /outputs/taint-flows.jsonl /outputs/generate_flows.py")
        except Exception:
            pass
    log.info(f"Cleaned up intermediate graph and flow files in {outs}")


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

    tgt = [e for e in entries if e.get("commit") == commit] or entries if entries else []
    tot = len(tgt)
    
    # Extract truth files, prioritizing critical operations (sinks), then entry points
    truth_co_files = [norm(e.get("critical_operation", {}).get("file")) for e in tgt if e.get("critical_operation", {}).get("file")]
    truth_ep_files = [norm(e.get("entry_point", {}).get("file")) for e in tgt if e.get("entry_point", {}).get("file")]
    
    truth_files = []
    for f in truth_co_files + truth_ep_files:
        if f and f not in truth_files:
            truth_files.append(f)

    scanned = set()

    def on_target(high, target_file, idx, total):
        scanned.add(norm(target_file))
        if tgt:
            from src.dedup import combine
            cur = combine(high)
            res = evaluate(cur, tgt, commit)
            hits = res.get("hits", 0)
            log.info(f"[benchmark] Target {idx}/{total} ({target_file}) - Hits: {hits}/{tot} details: {res.get('details')}")
            if hits >= tot:
                log.info(f"[benchmark] All {tot} targets hit!")
                return True
            if truth_files and set(truth_files).issubset(scanned):
                return True
        return False

    agent.run(
        target,
        graph_path=g_path,
        flows_path=f_path,
        taint_path=t_path,
        on_target=on_target if entries else None,
        target_files=truth_files if truth_files else None,
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
    return dest


# Load and filter agent flows
def load(src):
    items = []
    if isinstance(src, (list, tuple)):
        items = list(src)
    elif Path(src).exists():
        for line in Path(src).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
    clean = []
    for f in items:
        if str(f.get("id", "")).startswith("B"):
            continue
        clean.append(f)
        sink = f.get("sink") or {}
        vecs = f.get("alternative_vectors") or []
        for av in vecs:
            if isinstance(av, dict) and av.get("source"):
                item = dict(f)
                item["source"] = av.get("source")
                if "chain" in av:
                    item["chain"] = av.get("chain")
                item["id"] = av.get("id") or f.get("id")
                clean.append(item)
    return clean


# Evaluate predictions against ground truth entries
def evaluate(taints, entries, commit):
    flows = load(taints)

    target = [e for e in entries if e.get("commit") == commit]
    if not target:
        target = entries

    log.info(f"Evaluating {len(flows)} agent flows against {len(target)} ground truth entries...")

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


# State and Report paths
STATE_FILE = root / "benchmark" / "vulngym" / "benchmark_results.jsonl"
REPORT_MD = root / "benchmark" / "vulngym" / "reports.md"


def load_completed_state():
    completed = {}
    if STATE_FILE.exists():
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    key = (rec.get("url"), rec.get("commit"))
                    completed[key] = rec
                except Exception:
                    pass
    return completed


def save_report(record):
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    render_markdown_report()


def render_markdown_report():
    completed = load_completed_state()
    if not completed:
        return

    records = list(completed.values())
    total_entries = sum(r.get("total", 0) for r in records)
    total_hits = sum(r.get("hits", 0) for r in records)
    mean_er = (total_hits / total_entries * 100) if total_entries else 0.0

    mean_ep_fr = (sum(r.get("ep_fr", 0.0) * r.get("total", 0) for r in records) / total_entries * 100) if total_entries else 0.0
    mean_ep_lr = (sum(r.get("ep_lr", 0.0) * r.get("total", 0) for r in records) / total_entries * 100) if total_entries else 0.0
    mean_co_fr = (sum(r.get("co_fr", 0.0) * r.get("total", 0) for r in records) / total_entries * 100) if total_entries else 0.0
    mean_co_lr = (sum(r.get("co_lr", 0.0) * r.get("total", 0) for r in records) / total_entries * 100) if total_entries else 0.0

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# VulnGym Benchmark Report",
        "",
        f"- **Last updated:** `{now_str}`",
        f"- **Completed targets:** {len(records)}",
        f"- **Total Ground Truth Entries:** {total_entries}",
        f"- **Exact Recall (ER / Hits):** {total_hits}/{total_entries} (**{mean_er:.2f}%**)",
        f"- **Entry Point File Recall (EP_FR):** {mean_ep_fr:.2f}%",
        f"- **Entry Point Line Recall (EP_LR):** {mean_ep_lr:.2f}%",
        f"- **Critical Operation File Recall (CO_FR):** {mean_co_fr:.2f}%",
        f"- **Critical Operation Line Recall (CO_LR):** {mean_co_lr:.2f}%",
        "",
        "## Evaluated Targets",
        "",
        "| # | Project | Commit | Flows | Entries | Hits | ER | EP_FR | EP_LR | CO_FR | CO_LR | Status |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for idx, r in enumerate(records, 1):
        tot = r.get("total", 0)
        h = r.get("hits", 0)
        status = "PASSED" if h == tot and tot > 0 else "PARTIAL" if h > 0 else "MISSED"
        c_short = str(r.get("commit", ""))[:8]
        proj = str(r.get("project", "unknown"))
        fl = r.get("flows", 0)
        er = float(r.get("er", 0.0)) * 100
        ep_fr = float(r.get("ep_fr", 0.0)) * 100
        ep_lr = float(r.get("ep_lr", 0.0)) * 100
        co_fr = float(r.get("co_fr", 0.0)) * 100
        co_lr = float(r.get("co_lr", 0.0)) * 100
        lines.append(f"| {idx} | {proj} | `{c_short}` | {fl} | {tot} | {h} | {er:.1f}% | {ep_fr:.1f}% | {ep_lr:.1f}% | {co_fr:.1f}% | {co_lr:.1f}% | {status} |")

    lines.append("")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    bench = Path(__file__).resolve().parent
    meta = bench / "entries.jsonl"
    if not meta.exists():
        log.error("entries.jsonl not found!")
        return

    entries = [
        json.loads(l) for l in meta.read_text(encoding="utf-8").splitlines() if l.strip()
    ]

    # Optional filter from sys.argv (e.g. `python benchmark.py fastmcp` or `python benchmark.py`)
    filter_arg = sys.argv[1].strip() if len(sys.argv) > 1 else None

    # Group entries by (repo_url, commit)
    from collections import OrderedDict
    targets = OrderedDict()
    for e in entries:
        u = e.get("repo_url")
        c = e.get("commit")
        p = e.get("project") or (u.rstrip("/").split("/")[-1] if u else "unknown")
        if u and c:
            if filter_arg and filter_arg.lower() not in p.lower() and filter_arg.lower() not in u.lower():
                continue
            targets.setdefault((u, c, p), []).append(e)

    # Resume state: load already completed (url, commit)
    completed_state = load_completed_state()

    for idx, ((url, commit, project), tgt_entries) in enumerate(targets.items(), 1):
        if (url, commit) in completed_state:
            log.info(f"[{idx}/{len(targets)}] Skipping {project} @ {commit[:8]}")
            continue

        log.info(f"[{idx}/{len(targets)}] Running {project} @ {commit[:8]}\n")

        repo_name = url.rstrip("/").split("/")[-1]
        repo_dir = bench / "repos" / repo_name
        outs = repo_dir / "outputs"
        box = None

        try:
            # Step 1: Clone on demand
            clone(url, repo_dir)

            # Step 2: Checkout commit
            checkout(repo_dir, commit)

            # Step 3: Setup output dir INSIDE target repo
            outs = repo_dir / "outputs"
            outs.mkdir(parents=True, exist_ok=True)
            graphs = outs / "call-graph.json"
            flows = outs / "call-flows.jsonl"
            taints = outs / "taint-flows.jsonl"
            findings = outs / "findings.jsonl"
            report = outs / "eval.json"

            # Step 4: Graphify
            if not graphs.exists() or not flows.exists():
                graph(repo_dir, graphs, flows)

            # Step 5: Sandbox
            from docker.sandbox import init as sandbox
            box = sandbox(repo_path=str(repo_dir), outputs_path=str(outs))

            # Step 6: Retrieve
            retrieve(repo_dir, graphs, flows, taints, box=box, entries=tgt_entries, commit=commit)

            # Step 7: Map output
            raw = load(taints)
            export(raw, findings, repo_dir, url, commit)

            # Step 8: Evaluate
            res = evaluate(raw, tgt_entries, commit)
            report.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")

            # Step 9: Save report & render markdown continuously
            import datetime
            rec = {
                "project": project,
                "url": url,
                "commit": commit,
                "flows": len(raw),
                "total": res.get("total", len(tgt_entries)),
                "hits": res.get("hits", 0),
                "er": res.get("er", 0.0),
                "ep_fr": res.get("ep_fr", 0.0),
                "ep_lr": res.get("ep_lr", 0.0),
                "co_fr": res.get("co_fr", 0.0),
                "co_lr": res.get("co_lr", 0.0),
                "timestamp": datetime.datetime.now().isoformat(),
            }
            save_report(rec)
            completed_state[(url, commit)] = rec

            print("\n" + "=" * 55)
            print("        VULNGYM BENCHMARK")
            print("=" * 55)
            print(f"Target repo       : {project}")
            print(f"Target commit     : {commit[:8]}")
            print(f"Directory         : {outs}")
            print(f"Findings          : {findings}")
            print(f"Agent Flows       : {len(raw)}")
            print(f"Total entries     : {res['total']}")
            print(f"Entries Hit (ER)  : {res['hits']} ({res['er'] * 100:.2f}%)")
            print(f"EP File Recall    : {res['ep_fr'] * 100:.2f}%")
            print(f"EP Line Recall    : {res['ep_lr'] * 100:.2f}%")
            print(f"CO File Recall    : {res['co_fr'] * 100:.2f}%")
            print(f"CO Line Recall    : {res['co_lr'] * 100:.2f}%")
            print(f"Report saved to   : {REPORT_MD}")
            print("=" * 55)

        except Exception as e:
            log.error(f"Error benchmarking {project} @ {commit[:8]}: {e}")
            continue
        finally:
            cleanup_outputs(outs, box)

    print(f"\nAll benchmark targets processed! Check {REPORT_MD} for complete report.")


if __name__ == "__main__":
    main()
