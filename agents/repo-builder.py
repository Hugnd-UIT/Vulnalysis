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

    # Detect build manifests
    def detect_manifests(self, repo_path):
        target = Path(repo_path).resolve()
        manifest_names = {
            "Makefile", "makefile", "GNUmakefile",
            "CMakeLists.txt", "configure.ac", "configure.in", "configure",
            "meson.build", "build.ninja",
            "package.json", "setup.py", "pyproject.toml", "requirements.txt",
            "Cargo.toml", "go.mod", "pom.xml", "build.gradle"
        }
        found = []
        if target.exists():
            for item in target.iterdir():
                if item.name in manifest_names:
                    found.append(item.name)
        return sorted(found)

    # Run repo builder
    def run(self, repo_path):
        # Reset memory
        self.memory.clear()
        self.step = 0

        manifests = self.detect_manifests(repo_path)

        # Build prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
            manifests=", ".join(manifests) if manifests else "none detected",
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
                self.log(thought=thought, action="finish", observation="Build environment preparation completed")
                return True

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

        return False


__all__ = ["RepoBuilderAgent"]