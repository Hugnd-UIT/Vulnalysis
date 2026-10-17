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
        self.messages = []

    def manifest(self, repo_path):
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

    def run(self, repo_path):
        # Reset memory
        self.memory.clear()
        self.step = 0

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

        # ReAct loop
        while not self.is_step_exceeded() and not self.is_cost_exceeded():
            # Call llm
            reply = self.call(self.messages)
            thought, action, command = self.parse(reply)

            # Thought
            self.log_thought(thought)
            self.messages.append({"role": "assistant", "content": reply})

            # Finish
            if action == "finish":
                extracted = self.extract_json(reply)
                if extracted and isinstance(extracted, dict):
                    self.log_action("finish")
                    self.log(thought=thought, action="finish", observation="Repo builder completed")
                    return extracted
                self.log_error("Invalid json")
                self.messages.append({"role": "user", "content": self.error_prompt})
                continue

            # Execute
            if action == "bash" and command:
                self.log_action("bash", command)
                code, output = self.execute(command)

                # Log
                self.log(thought=thought, action="bash", command=command, observation=output[:500])

                # Observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                if self.step >= self.step_limit - 1:
                    obs_text += "\n[NOTICE: Step limit reached. You MUST output finish with your final result now.]"
                self.messages.append({"role": "user", "content": obs_text})
            else:
                # Error
                self.log_error("Invalid action")
                self.messages.append({"role": "user", "content": self.error_prompt})

        return False


__all__ = ["RepoBuilderAgent"]