import json
from agents.base import BaseAgent
from models.provider import OpenAI


class VulnPlannerAgent(BaseAgent):
    def __init__(self, sandbox=None, config_path=None, provider_cls=OpenAI):
        
        # Initialize base agent
        super().__init__(
            name="vuln-planner",
            sandbox=sandbox,
            config_path=config_path,
            provider_cls=provider_cls,
        )
        self.messages = []

    def run(self, 
        repo, 
        hypotheses, 
        taint_flow, 
        analyzer=None, 
        verifier=None
    ):
        hyp_str = json.dumps(hypotheses, indent=2, ensure_ascii=False) if isinstance(hypotheses, (dict, list)) else str(hypotheses or "")
        flow_str = json.dumps(taint_flow, indent=2, ensure_ascii=False) if isinstance(taint_flow, (dict, list)) else str(taint_flow or "")

        if self.messages and (analyzer or verifier):
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
            initial_task = self.render(
                self.user_prompt,
                repo=str(repo),
                hypotheses=hyp_str,
                candidate=hyp_str,
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

        # ReAct loop
        while not self.is_step_exceeded() and not self.is_cost_exceeded():
            # Call llm
            reply = self.call(self.messages)
            thought, action, command = self.parse(reply)

            # Thought
            self.messages.append({"role": "assistant", "content": reply})

            # Finish
            if action == "finish" or (not action and "finish" in reply.lower()):
                self.log(thought=thought, action="finish", observation="Coordinator completed")
                extracted = self.extract_json(reply)
                return extracted or {"decision": "APPROVE"}

            # Execute
            if action == "bash" and command:
                code, output = self.execute(command)

                # Log
                self.log(thought=thought, action="bash", command=command, observation=output[:500])

                # Observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                self.messages.append({"role": "user", "content": obs_text})
            else:
                # Error
                self.messages.append({"role": "user", "content": self.error_prompt})

        return False


__all__ = ["VulnPlannerAgent"]
