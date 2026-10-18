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
        self.messages = []

    def run(self, 
        repo, 
        hypothesis, 
        pov_script=None, 
        pov_result=None
    ):
        flow_id = hypothesis.get("id")
        pov_script = pov_script or f"/outputs/{flow_id}/scripts.py"
        pov_result = pov_result if isinstance(pov_result, str) else f"/outputs/{flow_id}/results.json"
        pov_str = json.dumps(pov_result, indent=2, ensure_ascii=False) if isinstance(pov_result, (dict, list)) else str(pov_result or "")

        # Feedback
        if self.messages and pov_result:
            self.step = 0
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
            hyp_str = json.dumps(hypothesis, indent=2, ensure_ascii=False) if isinstance(hypothesis, (dict, list)) else str(hypothesis or "")
            initial_task = self.render(
                self.user_prompt,
                repo=str(repo),
                hypothesis=hyp_str,
                pov_script=pov_script,
                pov_result=pov_result,
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
                    self.log(thought=thought, action="finish", observation="PoV verifier completed")
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

                # Observation
                obs_text = self.render(self.react_prompt, code=code, output=output)
                obs_text += self.check_exceeded()
                self.messages.append({"role": "user", "content": obs_text})
            else:
                # Error
                self.log_error("Invalid action")
                self.messages.append({"role": "user", "content": self.error_prompt + self.check_exceeded()})

        return False


__all__ = ["PovVerifierAgent"]
