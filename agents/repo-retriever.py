import json
import os
import re
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
        target = Path(repo_path).resolve()

        try:
            from graphify.extract import collect
            non_code = {".md", ".mdx", ".qmd", ".skill", ".json", ".txt", ".rst"}
            return sorted([
                p.relative_to(target).as_posix()
                for p in collect(target)
                if p.suffix.lower() not in non_code
            ])
        except Exception:
            pass

        files = []
        ignored = {
            ".git", "__pycache__", "venv", ".venv",
            "node_modules", ".idea", ".vscode", "build", "dist"
        }
        exts = (
            ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".cu", ".cuh",
            ".py", ".go", ".rs", ".java", ".kt", ".kts", ".scala",
            ".cs", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
            ".php", ".rb", ".rake", ".swift", ".zig", ".m", ".mm",
            ".sh", ".bash", ".ps1", ".psm1", ".dart", ".ex", ".exs",
            ".erl", ".hrl", ".sol", ".jl", ".ml", ".mli", ".r",
            ".sql", ".lua", ".luau", ".cls", ".trigger", ".tf", ".hcl"
        )

        # Walk directory
        for root, dirs, names in os.walk(target):
            dirs[:] = [d for d in dirs if d not in ignored]
            for f in names:
                if f.lower().endswith(exts):
                    rel = Path(root, f).relative_to(target).as_posix()
                    files.append(rel)

        return sorted(files)

    # Load call graph
    def load_graph(self, repo_path, graph_path=None):
        
        path = Path(graph_path) if graph_path else Path("outputs/call-graph.json")
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            import graphify
            return graphify.callgraph(repo_path)
        except Exception:
            return {"nodes": [], "edges": []}

    def run(self, repo_path, graph_path=None, flows_path=None, taint_path=None):
        # Reset memory
        self.memory.clear()
        self.step = 0

        files = self.scan(repo_path)
        graph = self.load_graph(repo_path, graph_path)
        nodes_count = len(graph.get("nodes", []))
        edges_count = len(graph.get("edges", []))
        call_graph_file = graph_path or "/outputs/call-graph.json"
        call_flows_file = flows_path or "/outputs/call-flows.jsonl"
        taint_flows_file = taint_path or "/outputs/taint-flows.jsonl"

        # Build prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
            files=len(files),
            nodes=nodes_count,
            edges=edges_count,
            call_graph=str(call_graph_file),
            call_flows=str(call_flows_file),
            taint_flows=str(taint_flows_file),
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_task},
        ]

        # ReAct loop
        while not self.is_step_exceeded() and not self.is_cost_exceeded():
            # Call llm
            reply = self.call(messages)
            thought, action, command = self.parse(reply)

            # Thought
            messages.append({"role": "assistant", "content": reply})

            # Finish
            if action == "finish" or (not action and "finish" in reply.lower()):
                self.log(thought=thought, action="finish", observation="Repo retriever completed")
                return taint_flows_file

            # Execute
            if action == "bash" and command:
                code, output = self.execute(command)

                # Log
                self.log(thought=thought, action="bash", command=command, observation=output[:500])

                # Observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                messages.append({"role": "user", "content": obs_text})
            else:
                # Error
                messages.append({"role": "user", "content": self.error_prompt})

        return taint_flows_file


__all__ = ["RepoRetrieverAgent"]