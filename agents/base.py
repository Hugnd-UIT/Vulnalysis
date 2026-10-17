import json
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
        self.messages = []

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

    # Render prompt
    def render(self, template, **kwargs):
        if not template:
            return ""
        try:
            from jinja2 import Template
            return Template(template).render(**kwargs)
        except Exception:
            text = template
            for k, v in kwargs.items():
                text = text.replace(f"{{{{{k}}}}}", str(v))
            return text

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
        t = timeout or min(self.timeout, 30)
        try:
            import shlex
            wrapped = f"timeout {t} bash -c {shlex.quote(command)} < /dev/null"
            cmd = ["/bin/bash", "-c", wrapped]
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

        if not text:
            return thought, action, command

        # Extract thought
        m = re.search(r"(?:THOUGHT|Thinking|Reasoning)[\s:\-]+(.*?)(?=(```|\n\s*(?:ACTION|Action)|\Z))", text, re.DOTALL | re.IGNORECASE)
        if m and m.group(1).strip():
            thought = m.group(1).strip()
        else:
            thought = text.split("```")[0].strip() if "```" in text else ""
            if not thought and "```" in text:
                parts = text.split("```")
                if len(parts) >= 3 and parts[-1].strip():
                    thought = parts[-1].strip()

        thought = re.sub(r"^#+\s*(?:Thinking|Thought|Reasoning)[\s:\-]*", "", thought, flags=re.IGNORECASE).strip()
        thought = re.sub(r"\n\s*(?:ACTION|Action)[\s:\-]+.*$", "", thought, flags=re.IGNORECASE).strip()

        # Extract action
        bash_match = re.search(r"```(?:bash|sh|shell)(?:[ \t\r\n]+)(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if not bash_match:
            generic = re.search(r"```\s*((?:#!\/bin\/bash|\$|cat|grep|ls|find|gcc|clang|make|python3?|curl|echo|head|tail|mkdir|cd|\.\/)[\s\S]*?)\s*```", text, re.DOTALL)
            if generic:
                bash_match = generic
        if not bash_match and re.search(r"(?:ACTION|Action)[\s:\-]+(?:bash|shell)\b", text, re.IGNORECASE):
            any_block = re.search(r"```[a-zA-Z0-9_\-]*\s*(.*?)\s*```", text, re.DOTALL)
            if any_block:
                bash_match = any_block

        if bash_match:
            action = "bash"
            command = bash_match.group(1).strip()
            if command.startswith("$"):
                command = command.lstrip("$").strip()
            if command.lower() == "finish":
                action = "finish"
                command = ""
        elif re.search(r"^\s*finish\s*$", text, re.MULTILINE | re.IGNORECASE) or re.search(r"(?:ACTION|Action)[\s:\-]+finish\b", text, re.IGNORECASE) or re.search(r"\bfinish\s*$", text.strip(), re.IGNORECASE):
            action = "finish"
        elif self.extract_json(text):
            action = "finish"

        return thought, action, command

    # Extract json
    def extract_json(self, text):
        if not text:
            return None
        match = re.search(r"```(?:json|finish)?\s*(\{[\s\S]*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
        match = re.search(r"(\{[\s\S]*\})", text)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
        return None

    # Logging helpers
    def log_thought(self, thought):
        import logging
        if thought:
            text = str(thought).strip().replace(chr(10), " ")
            logging.getLogger("vulnalysis").info(f"[{self.name}] Thought: {text}")

    def log_action(self, action, command=""):
        import logging
        logger = logging.getLogger("vulnalysis")
        cmd = str(command or "").strip().replace("\n", " ")
        if action == "bash":
            logger.info(f"  [{self.name}] Action: bash `{cmd}`")
        elif action == "finish":
            logger.info(f"  [{self.name}] Action: finish")
        elif action:
            logger.info(f"  [{self.name}] Action: {action}")

    def log_error(self, msg):
        import logging
        logging.getLogger("vulnalysis").warning(f"  [{self.name}] Error: {msg}")

    # Call llm
    def call(self, messages, **kwargs):
        # Check budget limit
        if self.is_cost_exceeded():
            self.log_error("cost limit exceeded")
            return "[!] Cost limit exceeded"

        # Check step limit
        if self.is_step_exceeded():
            self.log_error("step limit exceeded")
            return "[!] Step limit exceeded"

        call_kwargs = dict(kwargs)
        if "temperature" not in call_kwargs:
            call_kwargs["temperature"] = self.temperature

        last_err = None
        for attempt in range(5):
            try:
                reply = self.llm.chat(messages, **call_kwargs)
                if reply:
                    if reply.startswith("[!]"):
                        err_text = reply.lstrip("[!]").strip()
                        self.log_error(err_text)
                        return reply
                    self.step += 1
                    return reply
                last_err = "Empty response"
                self.log_error(f"Empty response")
                time.sleep(0.5)
            except Exception as e:
                last_err = e
                err_str = str(e)
                if any(k in err_str.lower() for k in ("blocked by", "safety", "filter", "policy")):
                    short_err = "Block by policy"
                elif "timeout" in err_str.lower():
                    short_err = "timeout"
                else:
                    short_err = err_str.split("\n")[0][:80]
                self.log_error(f"{short_err}")
                wait = min(2 ** attempt, 20) + random.random()
                if messages and isinstance(messages[-1], dict) and "content" in messages[-1]:
                    messages[-1]["content"] += " "
                time.sleep(wait)

        err_msg = f"[!] LLM Exception: {last_err}"
        self.log_error(err_msg)
        return err_msg

    # Log step
    def log(self, *args, **kwargs):
        data = args[0] if (args and isinstance(args[0], dict)) else kwargs
        entry = self.memory.add(**data) if (args and isinstance(args[0], dict)) else self.memory.add(*args, **kwargs)
        return entry


__all__ = ["BaseAgent"]