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

        # ReAct loop
        while not self.is_step_exceeded() and not self.is_cost_exceeded():
            # Call llm
            reply = self.call(self.messages)
            if not reply or reply.startswith("[!]"):
                break
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
                self.messages.append({"role": "user", "content": self.error_prompt + self.check_exceeded()})
                continue

            # Execute
            if action == "bash" and command:
                self.log_action("bash", command)
                code, output = self.execute(command)

                # Log
                self.log(thought=thought, action="bash", command=command, observation=output[:500])

                # Check stuck
                is_stuck, warning = self.check_stuck(command, code)
                if is_stuck:
                    self.log_error(f"Stuck: {warning}. Aborting build.")
                    break

                # Observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                obs_text += self.check_exceeded()
                self.messages.append({"role": "user", "content": obs_text})
                if warning:
                    self.messages.append({"role": "user", "content": warning})
            else:
                # Error
                self.log_error("Invalid action")
                self.messages.append({"role": "user", "content": self.error_prompt + self.check_exceeded()})

        return False

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