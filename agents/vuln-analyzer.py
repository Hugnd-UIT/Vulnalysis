import json
from agents.base import BaseAgent
from models.provider import OpenAI


class VulnAnalyzerAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="vuln-analyzer",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )
        self.messages = []

    def run(self, 
        repo, 
        taint_flow, 
        hypotheses="hypotheses.json", 
        feedback=None
    ):
        feedback_str = json.dumps(feedback, indent=2, ensure_ascii=False) if feedback else ""

        # Feedback
        if self.messages and feedback:
            obs = self.render(
                self.react_prompt,
                feedback=feedback_str,
                code=0,
                output="",
            )
            self.messages.append({"role": "user", "content": obs})
        else:
            self.memory.clear()
            self.step = 0
            flow_str = json.dumps(taint_flow, indent=2, ensure_ascii=False)
            initial_task = self.render(
                self.user_prompt,
                repo=str(repo),
                taint_flow=flow_str,
                hypotheses=hypotheses,
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
                if not extracted and hypotheses:
                    code, content = self.execute(f"cat {hypotheses} 2>/dev/null")
                    if content:
                        extracted = self.extract_json(content)
                if extracted and isinstance(extracted, dict):
                    self.log_action("finish")
                    self.log(thought=thought, action="finish", observation="Analysis completed")
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


__all__ = ["VulnAnalyzerAgent"]
