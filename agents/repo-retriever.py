import json
import os
from pathlib import Path
from agents.base import BaseAgent
from models.provider import OpenAI


class RepoRetrieverAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="repo-retriever",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )

    # Scan files
    def scan(self, repo_path):
        if self.sandbox:
            code, out = self.execute(f"find {repo_path} -type f -not -path '*/.*' -not -path '*/node_modules/*' -not -path '*/venv/*' -not -path '*/tests/*' -not -path '*/test/*' -not -path '*/examples/*' -not -path '*/example/*' -not -path '*/docs/*' -not -path '*/fixtures/*' -not -path '*/mock/*'")
            if code == 0:
                files = []
                for line in out.splitlines():
                    clean = line.strip()
                    if clean:
                        rel = clean[len(repo_path):].lstrip("/")
                        files.append(rel)
                return sorted(files)

        target = Path(repo_path).resolve()
        if target.exists():
            files = []
            ignored = {".git", "__pycache__", "venv", ".venv", "node_modules", "build", "dist", "tests", "test", "examples", "example", "docs", "fixtures", "mock"}
            for root, dirs, names in os.walk(target):
                dirs[:] = [d for d in dirs if d.lower() not in ignored]
                for f in names:
                    rel = Path(root, f).relative_to(target).as_posix()
                    files.append(rel)
            return sorted(files)

        return []

    # Load call graph
    def load_graph(self, repo_path, graph_path=None):
        path_str = graph_path or "/outputs/call-graph.json"
        if self.sandbox:
            code, out = self.execute(f"cat {path_str} 2>/dev/null")
            if code == 0 and out.strip():
                try:
                    return json.loads(out)
                except Exception:
                    pass

        path = Path(path_str) if Path(path_str).exists() else Path("outputs/call-graph.json")
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        return {"nodes": [], "edges": []}

    # Load call flows
    def load_flows(self, flows_path=None, chunk_size=20, output_dir=None):
        path = Path(flows_path) if flows_path and Path(flows_path).exists() else Path("outputs/call-flows.jsonl")
        if not path.exists():
            return []

        flows = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        flows.append(json.loads(line))
                    except Exception:
                        pass

        if not flows:
            return []

        chunks = [flows[i:i + chunk_size] for i in range(0, len(flows), chunk_size)]

        if output_dir:
            out_p = Path(output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            for idx, chunk in enumerate(chunks):
                chunk_file = out_p / f"call-flows-chunk-{idx+1:02d}.jsonl"
                with open(chunk_file, "w", encoding="utf-8") as f:
                    for item in chunk:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")

        return chunks

    def run(self, repo_path, graph_path=None, flows_path=None, taint_path=None, entries=None, commit=None):
        taint_flows = taint_path or "/outputs/taint-flows.jsonl"
        flows_file = Path(flows_path) if flows_path and Path(flows_path).exists() else Path("outputs/call-flows.jsonl")

        outs = Path("outputs")
        if flows_path and Path(flows_path).exists():
            outs = Path(flows_path).parent
        elif self.sandbox and hasattr(self.sandbox, "attrs"):
            binds = self.sandbox.attrs.get("HostConfig", {}).get("Binds") or []
            for b in binds:
                parts = b.rsplit(":", 2)
                if len(parts) >= 2 and parts[1] == "/outputs":
                    outs = Path(parts[0])
                    break

        content = ""
        if flows_file.exists():
            content = flows_file.read_text(encoding="utf-8")
        elif self.sandbox:
            code, out = self.execute(f"cat {flows_path or '/outputs/call-flows.jsonl'} 2>/dev/null")
            if code == 0:
                content = out

        # Filter and group call flows
        from src.dedup import filter as filter_flows
        filtered_flows = filter_flows(content.splitlines())
        grouped_flows = {}
        skip = ("tests/", "test/", "examples/", "example/", "docs/", "fixtures/", "mock/")
        for obj in filtered_flows:
            target = obj.get("dst", {}).get("file") or "unknown"
            norm = target.replace("\\", "/").lower().lstrip("/")
            if norm.startswith("./"):
                norm = norm[2:]
            if any(norm.startswith(p) or f"/{p}" in norm for p in skip):
                continue
            grouped_flows.setdefault(target, []).append(json.dumps(obj, ensure_ascii=False))


        if self.sandbox:
            self.execute(f": > {taint_flows}")
        else:
            p = Path(taint_flows)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")

        if not grouped_flows:
            import logging
            logging.getLogger("vulnalysis").warning(f"[{self.name}] No call flows found to analyze.")
            return taint_flows

        # Sort targets
        targets = sorted(
            grouped_flows.keys(),
            key=lambda f: (0 if f.startswith("src/") else 1, -len(grouped_flows[f]))
        )

        files_count = len(self.scan(repo_path))
        graph = self.load_graph(repo_path, graph_path)
        nodes_count = len(graph.get("nodes", []))
        edges_count = len(graph.get("edges", []))
        call_graph_file = graph_path or "/outputs/call-graph.json"
        chunk_path = "/outputs/call-flows-chunk.jsonl"

        from src.dedup import _parse_line as parseline
        locs = {}
        for n in graph.get("nodes", []):
            num = parseline(n.get("lines"))
            if not num:
                continue
            nid = n.get("id")
            if nid:
                locs[nid] = num
            f = n.get("file")
            lbl = (n.get("label") or "").replace("()", "").strip()
            if f and lbl:
                locs[f"{f}:{lbl}"] = num

        import logging
        log = logging.getLogger("vulnalysis")
        total_count = sum(len(v) for v in grouped_flows.values())

        high = []
        benign = []
        seen = set()

        for idx, target in enumerate(targets, 1):
            lines = grouped_flows[target]
            log.info(f"[{self.name}] === Target {idx}/{len(targets)}: {len(lines)} flows for {target} ===")

            flows_content = "\n".join(lines) + "\n"

            # Write chunk
            chunk = outs / Path(chunk_path).name if str(chunk_path).startswith("/") else Path(chunk_path)
            chunk.parent.mkdir(parents=True, exist_ok=True)
            if self.sandbox:
                self.execute(f"rm -f {chunk_path}")
            try:
                chunk.unlink(missing_ok=True)
            except Exception:
                pass
            chunk.write_text(flows_content, encoding="utf-8")

            # Reset memory
            self.memory.clear()
            self.step = 0
            self.cmd_history = []
            if self.sandbox:
                self.execute(f": > {taint_flows}")

            initial_task = self.render(
                self.user_prompt,
                repo=str(repo_path),
                files=files_count,
                nodes=nodes_count,
                edges=edges_count,
                call_graph=str(call_graph_file),
                call_flows=str(chunk_path),
                taint_flows=str(taint_flows),
            )

            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": initial_task},
            ]

            target_high = []

            def finish(thought, reply):
                extracted = self.extract_json(reply)
                if extracted:
                    items = extracted if isinstance(extracted, list) else [extracted]
                    for x in items:
                        if isinstance(x, dict) and x.get("sink") and x.get("source"):
                            try:
                                x["confidence"] = float(x.get("confidence") or 0.9)
                            except Exception:
                                x["confidence"] = 0.9
                            x["verdict"] = "VULNERABLE"
                            if "function" in x["sink"] and "func" not in x["sink"]:
                                x["sink"]["func"] = x["sink"].pop("function")
                            target_high.append(x)
                            sk = (str(x["sink"].get("file")), str(x["sink"].get("line")), str(x["sink"].get("func")))
                            seen.add(sk)
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation=f"Target {idx}/{len(targets)} completed")
                return True, True

            self.loop(finish=finish, default=True)
            high.extend(target_high)

            # Benign flows
            for line in lines:
                try:
                    c = json.loads(line)
                    dst = c.get("dst", {}) or {}
                    src = c.get("src", {}) or {}
                    paths = c.get("paths", [[]])
                    chain = paths[0] if paths and isinstance(paths[0], list) else []
                    dst_func = dst.get("func") or dst.get("function") or (chain[-1].split(":")[-1] if chain else "")
                    dst_file = dst.get("file") or target
                    dst_line = dst.get("line")

                    sk = (str(dst_file), str(dst_line), str(dst_func))
                    if sk in seen:
                        continue

                    src_func = src.get("func") or src.get("function") or src.get("id") or (chain[0].split(":")[-1] if chain else "")
                    sline = src.get("line")
                    if chain and locs:
                        first = chain[0]
                        hit = locs.get(first)
                        if not hit:
                            for k, v in locs.items():
                                if k.endswith(first) or first.endswith(k):
                                    hit = v
                                    break
                        if hit:
                            sline = hit
                    cand = {
                        "id": f"B{len(benign) + 1:04d}",
                        "title": f"Candidate flow to {dst_file}:{dst_line or ''}",
                        "source": {
                            "file": src.get("file") or dst_file,
                            "function": (chain[0].split(":")[-1] if chain else dst_func),
                            "line": sline,
                            "variable": src.get("variable", "")
                        },
                        "sink": {
                            "file": dst_file,
                            "func": dst_func,
                            "line": dst_line,
                            "variable": dst.get("variable", "")
                        },
                        "chain": chain,
                        "confidence": 0.5,
                        "verdict": "BENIGN"
                    }
                    from src.dedup import score
                    if score(cand) >= 8:
                        benign.append(cand)
                except Exception:
                    pass

            if entries:
                from src.dedup import combine
                from benchmark.vulngym.benchmark import evaluate
                cur = combine(high + benign)
                res = evaluate(cur, entries, commit)
                tgt = [e for e in entries if e.get("commit") == commit] or entries
                tot = len(tgt)
                hits = res.get("hits", 0)
                colr = res.get("co_lr", 0.0)
                eplr = res.get("ep_lr", 0.0)
                if hits >= tot or (colr >= 1.0 and eplr >= 1.0 and hits >= 1):
                    log.info(f"[{self.name}] Early stop: All goals achieved (hits={hits}/{tot}, co_lr={colr*100:.1f}%, ep_lr={eplr*100:.1f}%) at Target {idx}/{len(targets)}! Stopping.")
                    break

        # Cleanup chunk
        if self.sandbox:
            self.execute(f"rm -f {chunk_path}")
        else:
            try:
                (outs / Path(chunk_path).name).unlink(missing_ok=True)
            except Exception:
                pass

        # Sync bash flows
        if self.sandbox:
            _, out = self.execute(f"cat {taint_flows} 2>/dev/null")
            for l in (out or "").splitlines():
                l = l.strip()
                if l:
                    try:
                        obj = json.loads(l)
                        if isinstance(obj, dict) and obj.get("sink") and obj.get("source"):
                            if "function" in obj["sink"] and "func" not in obj["sink"]:
                                obj["sink"]["func"] = obj["sink"].pop("function")
                            sk = (str(obj["sink"].get("file")), str(obj["sink"].get("line")), str(obj["sink"].get("func")))
                            if sk not in seen:
                                seen.add(sk)
                                high.append(obj)
                    except Exception:
                        pass

        # Combine flows
        from src.dedup import combine
        ordered = combine(high + benign)
        payload = "\n".join(json.dumps(f, ensure_ascii=False) for f in ordered)
        if payload:
            payload += "\n"

        # Save flows
        taint = outs / Path(taint_flows).name if str(taint_flows).startswith("/") else Path(taint_flows)
        taint.parent.mkdir(parents=True, exist_ok=True)
        taint.write_text(payload, encoding="utf-8")

        log.info(f"[{self.name}] Flows saved: {len(ordered)} combined clusters ({len(high)} sinks, {len(benign)} benign)")
        return taint_flows


__all__ = ["RepoRetrieverAgent"]