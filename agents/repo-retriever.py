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
            code, out = self.execute(f"find {repo_path} -type f -not -path '*/.*' -not -path '*/node_modules/*' -not -path '*/venv/*'")
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
            ignored = {".git", "__pycache__", "venv", ".venv", "node_modules", "build", "dist"}
            for root, dirs, names in os.walk(target):
                dirs[:] = [d for d in dirs if d not in ignored]
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

    def run(self, repo_path, graph_path=None, flows_path=None, taint_path=None):
        taint_flows = taint_path or "/outputs/taint-flows.jsonl"
        flows_file = Path(flows_path) if flows_path and Path(flows_path).exists() else Path("outputs/call-flows.jsonl")

        content = ""
        if flows_file.exists():
            content = flows_file.read_text(encoding="utf-8")
        elif self.sandbox:
            code, out = self.execute(f"cat {flows_path or '/outputs/call-flows.jsonl'} 2>/dev/null")
            if code == 0:
                content = out

        # Group call flows
        grouped_flows = {}
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                target = obj.get("dst", {}).get("file") or "unknown"
                grouped_flows.setdefault(target, []).append(line)
            except Exception:
                pass

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

        sorted_targets = sorted(
            grouped_flows.keys(),
            key=lambda f: (0 if f.startswith("src/") else 1, -len(grouped_flows[f]))
        )

        files_count = len(self.scan(repo_path))
        graph = self.load_graph(repo_path, graph_path)
        nodes_count = len(graph.get("nodes", []))
        edges_count = len(graph.get("edges", []))
        call_graph_file = graph_path or "/outputs/call-graph.json"
        current_flows_file = "/outputs/current-call-flows.jsonl"

        import logging
        logger = logging.getLogger("vulnalysis")
        total_count = sum(len(v) for v in grouped_flows.values())
        logger.info(f"[{self.name}] Sliced {total_count} call flows into {len(sorted_targets)} destination targets")

        for idx, target_file in enumerate(sorted_targets, 1):
            lines = grouped_flows[target_file]
            logger.info(f"[{self.name}] === Target {idx}/{len(sorted_targets)}: {len(lines)} flows for {target_file} ===")

            flows_content = "\n".join(lines)
            if self.sandbox:
                self.execute(f"cat << 'EOF' > {current_flows_file}\n{flows_content}\nEOF")
            else:
                p = Path(current_flows_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(flows_content, encoding="utf-8")

            # Reset memory & steps for this target
            self.memory.clear()
            self.step = 0
            self.cmd_history = []

            initial_task = self.render(
                self.user_prompt,
                repo=str(repo_path),
                files=files_count,
                nodes=nodes_count,
                edges=edges_count,
                call_graph=str(call_graph_file),
                call_flows=str(current_flows_file),
                taint_flows=str(taint_flows),
            )

            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": initial_task},
            ]

            def finish(thought, reply):
                extracted = self.extract_json(reply)
                if extracted:
                    items = extracted if isinstance(extracted, list) else [extracted]
                    valid = [
                        json.dumps(x, ensure_ascii=False)
                        for x in items
                        if isinstance(x, dict) and x.get("sink") and x.get("source")
                    ]
                    if valid:
                        lines_to_add = "\n".join(valid)
                        if self.sandbox:
                            self.execute(f"cat << 'EOF' >> {taint_flows}\n{lines_to_add}\nEOF")
                        else:
                            with open(taint_flows, "a", encoding="utf-8") as tf:
                                tf.write(lines_to_add + "\n")
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation=f"Target {idx}/{len(sorted_targets)} completed")
                return True, True

            self.loop(finish=finish, default=True)

        return taint_flows


__all__ = ["RepoRetrieverAgent"]