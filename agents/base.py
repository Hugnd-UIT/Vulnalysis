import random
import re
import time
from pathlib import Path
import yaml
from models.provider import OpenAI, Anthropic
from models import tracker
from src.memory import Memory


class BaseAgent:
    def __init__(self, name, sandbox=None, config_path=None, provider_cls=OpenAI):
        self.name = name
        self.sandbox = sandbox
        self.step = 0
        self.memory = Memory()

        # Setup config
        self.prompt_cfg = self.prompt()
        agent_cfg = self.prompt_cfg.get("agent", {}) or {}
        env_cfg = self.prompt_cfg.get("environment", {}) or {}
        model_cfg = self.prompt_cfg.get("model", {}) or {}

        # Setup parameters
        self.system_prompt = agent_cfg.get("system-prompt", "")
        self.user_prompt = agent_cfg.get("user-prompt", "")
        self.react_prompt = agent_cfg.get("react-prompt", "")
        self.error_prompt = agent_cfg.get("error-prompt", "")
        self.step_limit = int(agent_cfg.get("step-limit", 200))
        self.cost_limit = float(agent_cfg.get("cost-limit", 15.0))
        self.timeout = int(env_cfg.get("timeout", 300))
        self.temperature = float(model_cfg.get("temperature", 0.0))

        # Initialize provider
        self.llm = provider_cls(agent_name=self.name, config_path=config_path)

    # Load prompt
    def prompt(self):
        prompt_file = Path(__file__).resolve().parent.parent / "config" / "prompts" / f"{self.name}.yml"
        if prompt_file.exists():
            with open(prompt_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    # Check limits
    def is_step_exceeded(self):
        return self.step >= self.step_limit

    def is_cost_exceeded(self):
        return tracker.tracker.is_exceeded(self.name, self.cost_limit)

    # Execute command
    def execute(self, command, timeout=None):
        if not self.sandbox:
            return -1, "Sandbox not initialized"

        # Run command in sandbox
        t = timeout or self.timeout
        try:
            cmd = f"/bin/bash -c {command!r}"
            res = self.sandbox.exec_run(cmd, stdout=True, stderr=True)
            output = res.output.decode("utf-8", errors="ignore") if isinstance(res.output, bytes) else str(res.output or "")
            return res.exit_code, output
        except Exception as e:
            return -1, str(e)

    # Parse response
    def parse(self, text):
        thought = ""
        action = None
        command = ""

        # Extract thought
        thought_match = re.search(r"THOUGHT:\s*(.*?)(?=(```|bash|finish|$))", text, re.DOTALL | re.IGNORECASE)
        if thought_match:
            thought = thought_match.group(1).strip()

        # Extract action
        bash_match = re.search(r"```(?:bash|sh)?\s*(.*?)\s*```", text, re.DOTALL)
        if bash_match:
            action = "bash"
            command = bash_match.group(1).strip()
        elif "finish" in text.lower():
            action = "finish"

        return thought, action, command

    # Call llm
    def call(self, messages, **kwargs):
        # Check budget limit
        if self.is_cost_exceeded():
            return "[!] Cost limit exceeded"

        # Check step limit
        if self.is_step_exceeded():
            return "[!] Step limit exceeded"

        call_kwargs = dict(kwargs)
        if "temperature" not in call_kwargs:
            call_kwargs["temperature"] = self.temperature

        last_err = None
        for attempt in range(5):
            try:
                reply = self.llm.chat(messages, **call_kwargs)
                if reply:
                    self.step += 1
                    return reply
                last_err = "Empty response"
                time.sleep(1 + random.random())
            except Exception as e:
                last_err = e
                wait = min(2 ** attempt, 20) + random.random()
                if messages and isinstance(messages[-1], dict) and "content" in messages[-1]:
                    messages[-1]["content"] += " "
                time.sleep(wait)

        return f"[!] LLM Exception: {last_err}"

    # Log step
    def log(self, *args, **kwargs):
        if args and isinstance(args[0], dict):
            return self.memory.add(**args[0])
        return self.memory.add(*args, **kwargs)


__all__ = ["BaseAgent"]