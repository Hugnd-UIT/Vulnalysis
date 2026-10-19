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

    def run(self, 
        repo, 
        taint_flow, 
        hypotheses="hypotheses.json", 
        feedback=None
    ):
        feedback_str = json.dumps(feedback, indent=2, ensure_ascii=False) if feedback else ""

        # Feedback
        if self.messages and feedback:
            self.step = 0
            self.cmd_history = []
            obs = self.render(
                self.react_prompt,
                coordinator=feedback_str,
                code=0,
                output="",
            )
            self.messages.append({"role": "user", "content": obs})
        else:
            self.memory.clear()
            self.step = 0
            self.cmd_history = []
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

        def finish(thought, reply):
            extracted = self.extract_json(reply)
            if not extracted and hypotheses:
                code, content = self.execute(f"cat {hypotheses} 2>/dev/null")
                if content:
                    extracted = self.extract_json(content)
            if extracted and isinstance(extracted, dict):
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation="Analysis completed")
                return True, extracted
            return False, None

        return self.loop(finish=finish, default=False)


__all__ = ["VulnAnalyzerAgent"]
