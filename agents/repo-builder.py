from pathlib import Path
from agents.base import BaseAgent
from models.provider import OpenAI


class RepoBuilderAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="repo-builder",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )

    def manifest(self, repo_path):
        target = Path(repo_path).resolve()
        manifest_names = {
            "Makefile", "makefile", "GNUmakefile",
            "CMakeLists.txt", "configure.ac", "configure.in", "configure",
            "meson.build", "build.ninja", "build.sh",
            "package.json", "setup.py", "pyproject.toml", "requirements.txt",
            "Cargo.toml", "go.mod", "pom.xml", "build.gradle"
        }
        found = []
        if target.exists():
            for item in target.iterdir():
                if item.name in manifest_names:
                    found.append(item.name)
        elif self.sandbox:
            code, out = self.execute(f"ls -a {repo_path}")
            if code == 0:
                for name in out.split():
                    if name in manifest_names:
                        found.append(name)
        return sorted(found)

    def run(self, repo_path):
        # Reset memory
        self.memory.clear()
        self.step = 0
        self.cmd_history = []

        manifests = self.manifest(repo_path)

        # Build prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
            manifests=", ".join(manifests) if manifests else "none detected",
        )

        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": initial_task},
        ]

        def finish(thought, reply):
            extracted = self.extract_json(reply)
            if extracted and isinstance(extracted, dict):
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation="Repo builder completed")
                return True, extracted
            return False, None

        return self.loop(finish=finish, default=False)

    def explain_failure(self):
        query = (
            "The repository build failed or could not be completed. "
            "Please explain concisely why the target failed to build, "
            "what specific errors or missing dependencies occurred, and "
            "what is needed to make it build successfully."
        )
        if not self.messages:
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": query},
            ]
        else:
            self.messages.append({"role": "user", "content": query})

        reply = self.call(self.messages)
        return reply


__all__ = ["RepoBuilderAgent"]