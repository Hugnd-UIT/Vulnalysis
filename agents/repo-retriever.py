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
        # Reset memory
        self.memory.clear()
        self.step = 0
        self.cmd_history = []

        files = self.scan(repo_path)
        graph = self.load_graph(repo_path, graph_path)
        nodes_count = len(graph.get("nodes", []))
        edges_count = len(graph.get("edges", []))
        call_graph_file = graph_path or "/outputs/call-graph.json"
        call_flows_file = flows_path or "/outputs/call-flows.jsonl"
        taint_flows = taint_path or "/outputs/taint-flows.jsonl"

        # Build prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
            files=len(files),
            nodes=nodes_count,
            edges=edges_count,
            call_graph=str(call_graph_file),
            call_flows=str(call_flows_file),
            taint_flows=str(taint_flows),
        )

        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_task},
        ]

        def finish(thought, reply):
            extracted = self.extract_json(reply)
            code, _ = self.execute(f"test -s {taint_flows}")
            if code == 0:
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation="Repo retriever completed")
                return True, taint_flows
            if extracted:
                items = extracted if isinstance(extracted, list) else [extracted]
                lines = "\n".join(json.dumps(item) for item in items if isinstance(item, dict))
                if lines:
                    self.execute(f"cat << 'EOF' >> {taint_flows}\n{lines}\nEOF")
                    self.log_action("finish")
                    self.log(thought=thought, action="finish", observation="Repo retriever completed")
                    return True, taint_flows
            return False, None

        return self.loop(finish=finish, default=taint_flows)


__all__ = ["RepoRetrieverAgent"]