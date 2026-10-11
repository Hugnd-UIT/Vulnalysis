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

        # Collect via graphify
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

        # Walk directory
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
        # Load from path
        if graph_path and Path(graph_path).exists():
            with open(graph_path, "r", encoding="utf-8") as f:
                return json.load(f)

        # Generate via graphify
        try:
            import graphify
            return graphify.callgraph(repo_path)
        except Exception:
            return {"nodes": [], "edges": []}

    # Extract taint flows
    def extract(self, text):
        flows = []
        if not text:
            return flows

        try:
            import json_repair

            # Try code blocks first
            blocks = re.findall(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", text, re.DOTALL)
            for b in blocks:
                try:
                    data = json_repair.loads(b)
                    if isinstance(data, list):
                        flows.extend(data)
                    elif isinstance(data, dict):
                        if "taint_flows" in data and isinstance(data["taint_flows"], list):
                            flows.extend(data["taint_flows"])
                        elif "candidates" in data and isinstance(data["candidates"], list):
                            flows.extend(data["candidates"])
                        else:
                            flows.append(data)
                except Exception:
                    pass

            # Try full text if no blocks matched
            if not flows:
                data = json_repair.loads(text)
                if isinstance(data, list):
                    flows.extend(data)
                elif isinstance(data, dict):
                    if "taint_flows" in data and isinstance(data["taint_flows"], list):
                        flows.extend(data["taint_flows"])
                    elif "candidates" in data and isinstance(data["candidates"], list):
                        flows.extend(data["candidates"])
                    elif data:
                        flows.append(data)
        except Exception:
            pass

        # Fallback standard json
        if not flows:
            blocks = re.findall(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", text, re.DOTALL)
            for b in blocks:
                try:
                    data = json.loads(b)
                    if isinstance(data, list):
                        flows.extend(data)
                    elif isinstance(data, dict):
                        flows.append(data)
                except Exception:
                    pass

        return flows

    # Run retrieval
    def run(self, repo_path, graph_path=None):
        # Reset memory
        self.memory.clear()
        self.step = 0

        # Collect codebase info
        files = self.scan(repo_path)
        graph = self.load_graph(repo_path, graph_path)
        nodes_count = len(graph.get("nodes", []))
        edges_count = len(graph.get("edges", []))

        # Build initial prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
            files=len(files),
            nodes=nodes_count,
            edges=edges_count,
            samples="\n".join(files[:20]),
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_task},
        ]

        # Start ReAct loop
        while not self.is_step_exceeded() and not self.is_cost_exceeded():
            # Call llm
            reply = self.call(messages)
            thought, action, command = self.parse(reply)

            # Record thought
            messages.append({"role": "assistant", "content": reply})

            # Check finish
            if action == "finish" or (not action and "finish" in reply.lower()):
                flows = self.extract(reply)
                if flows:
                    self.log(thought=thought, action="finish", observation="Retrieval completed")
                    return flows

                # Enforce valid json
                err_text = self.render(
                    self.error_prompt,
                    error="You called finish without a valid JSON payload. Output your taint flows inside a ```json ... ``` block.",
                )
                messages.append({"role": "user", "content": err_text})
                continue

            # Execute bash
            if action == "bash" and command:
                code, output = self.execute(command)

                # Log step
                self.log(thought=thought, action="bash", command=command, observation=output[:500])

                # Feed observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                messages.append({"role": "user", "content": obs_text})
            else:
                # Handle error
                err_text = self.render(
                    self.error_prompt,
                    error="Invalid tool call. Must be bash or finish.",
                )
                messages.append({"role": "user", "content": err_text})

        # Extract final flows
        last_text = messages[-1]["content"] if messages else ""
        return self.extract(last_text)


__all__ = ["RepoRetrieverAgent"]