import json
from agents.base import BaseAgent
from models.provider import OpenAI


class PovVerifierAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="pov-verifier",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )

    def run(self, 
        repo, 
        hypothesis, 
        pov_script=None, 
        pov_result=None
    ):
        flow_id = hypothesis.get("id")
        pov_script = pov_script or f"/outputs/{flow_id}/scripts.py"
        default_result_path = f"/outputs/{flow_id}/results.json"
        if pov_result is None:
            pov_result = default_result_path
        pov_str = json.dumps(pov_result, indent=2, ensure_ascii=False) if isinstance(pov_result, (dict, list)) else str(pov_result or "")

        # Clean up any leftover processes before starting
        self.cleanup_processes()

        # Feedback
        if self.messages and pov_result:
            self.step = 0
            self.cmd_history = []
            obs = self.render(
                self.react_prompt,
                generator=pov_str,
                code=0,
                output="",
            )
            self.messages.append({"role": "user", "content": obs})
        else:
            self.memory.clear()
            self.step = 0
            self.cmd_history = []
            hyp_str = json.dumps(hypothesis, indent=2, ensure_ascii=False) if isinstance(hypothesis, (dict, list)) else str(hypothesis or "")
            initial_task = self.render(
                self.user_prompt,
                repo=str(repo),
                hypothesis=hyp_str,
                pov_script=pov_script,
                pov_result=pov_str,
            )
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": initial_task},
            ]

        def finish(thought, reply):
            extracted = self.extract_json(reply)
            if extracted and isinstance(extracted, dict):
                self.log_action("finish")
                self.log(thought=thought, action="finish", observation="PoV verifier completed")
                return True, extracted
            return False, None

        return self.loop(finish=finish, default=False)


__all__ = ["PovVerifierAgent"]
