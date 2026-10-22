import json
from agents.base import BaseAgent
from models.provider import OpenAI


class CoordinatorAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="coordinator",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )

    def run(self, 
        repo, 
        hypotheses, 
        taint_flow, 
        analyzer=None, 
        verifier=None
    ):
        hyp_str = json.dumps(hypotheses, indent=2, ensure_ascii=False) if isinstance(hypotheses, (dict, list)) else str(hypotheses or "")
        flow_str = json.dumps(taint_flow, indent=2, ensure_ascii=False) if isinstance(taint_flow, (dict, list)) else str(taint_flow or "")

        # Feedback
        if self.messages and (analyzer or verifier):
            self.step = 0
            self.cmd_history = []
            render_kwargs = {"code": 0, "output": ""}
            if analyzer:
                render_kwargs["analyzer"] = json.dumps(analyzer, indent=2, ensure_ascii=False) if isinstance(analyzer, (dict, list)) else str(analyzer)
            if verifier:
                render_kwargs["verifier"] = json.dumps(verifier, indent=2, ensure_ascii=False) if isinstance(verifier, (dict, list)) else str(verifier)
            obs = self.render(self.react_prompt, **render_kwargs)
            self.messages.append({"role": "user", "content": obs})
        else:
            self.memory.clear()
            self.step = 0
            self.cmd_history = []
            initial_task = self.render(
                self.user_prompt,
                repo=str(repo),
                hypotheses=hyp_str,
                taint_flow=flow_str,
            )
            self.messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": initial_task},
            ]
            if analyzer or verifier:
                render_kwargs = {"code": 0, "output": ""}
                if analyzer:
                    render_kwargs["analyzer"] = json.dumps(analyzer, indent=2, ensure_ascii=False) if isinstance(analyzer, (dict, list)) else str(analyzer)
                if verifier:
                    render_kwargs["verifier"] = json.dumps(verifier, indent=2, ensure_ascii=False) if isinstance(verifier, (dict, list)) else str(verifier)
                obs = self.render(self.react_prompt, **render_kwargs)
                self.messages.append({"role": "user", "content": obs})

        def finish(thought, reply):
            extracted = self.extract_json(reply)
            if extracted and isinstance(extracted, dict):
                if "decision" not in extracted and ("issues" in extracted or "suggestions" in extracted):
                    extracted["decision"] = "REVISE"
                if "decision" in extracted:
                    self.log_action("finish")
                    self.log(thought=thought, action="finish", observation="Coordinator completed")
                    return True, extracted
            return False, None

        return self.loop(finish=finish, default=False)


__all__ = ["CoordinatorAgent"]
