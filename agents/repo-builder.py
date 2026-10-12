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

    # Extract response
    def extract(self, text):
        if not text:
            return {}

        if "```json" in text:
            json_str = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            json_str = text.split("```")[1].split("```")[0].strip()
        else:
            json_str = text.strip()

        try:
            import json_repair
            data = json_repair.loads(json_str)
            if isinstance(data, dict):
                return data
            if isinstance(data, list) and data:
                return data[0] if isinstance(data[0], dict) else {}
        except Exception:
            pass

        return {}

    # Run build
    def run(self, repo_path):
        # Reset memory
        self.memory.clear()
        self.step = 0

        # Build prompt
        initial_task = self.render(
            self.user_prompt,
            repo=str(repo_path),
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
                result = self.extract(reply)
                if result and "status" in result:
                    self.log(thought=thought, action="finish", observation="Build completed")
                    return result

                messages.append({"role": "user", "content": self.error_json_prompt})
                continue

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

        last_text = messages[-1]["content"] if messages else ""
        return self.extract(last_text) or {"status": "failure", "notes": "Build step limit exceeded"}


__all__ = ["RepoBuilderAgent"]