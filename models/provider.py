from pathlib import Path
import yaml
from openai import OpenAI as _OpenAIClient
from anthropic import Anthropic as _AnthropicClient


# Load configuration
def config(config_path=None):
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.yml"
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# OpenAI provider
class OpenAI:
    def __init__(self, key=None, url=None, model=None, agent_name=None, config_path=None):
        # Load config
        cfg = config(config_path)
        provider_cfg = cfg.get("provider", {}) or {}
        self.agent_name = agent_name or ""

        # Setup url
        self.url = url or provider_cfg.get("url") or None
        if self.url and not str(self.url).strip():
            self.url = None

        # Setup key
        self.key = key or provider_cfg.get("key") or ""

        # Setup model
        if model:
            self.model = model
        elif agent_name and "agents" in cfg and agent_name in cfg["agents"]:
            agent_cfg = cfg["agents"][agent_name] or {}
            self.model = agent_cfg.get("model", "")
        else:
            self.model = ""

        # Initialize client
        self.client = _OpenAIClient(api_key=self.key or "none", base_url=self.url, timeout=60.0)

    def chat(self, messages, **kwargs):
        call_kwargs = dict(kwargs)
        if self.url and ("google" in self.url or "localhost" in self.url):
            try:
                from utils import apply
                call_kwargs = apply(call_kwargs)
            except Exception:
                pass

        # Send request
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **call_kwargs
        )
        if not response or not getattr(response, "choices", None):
            resp_dict = getattr(response, "model_dump", None) and response.model_dump() or {}
            inner_resp = resp_dict.get("response") if isinstance(resp_dict, dict) else None
            pf = inner_resp.get("promptFeedback") if isinstance(inner_resp, dict) else None
            if pf and pf.get("blockReason"):
                return "[!] Block by policy"
            return "[!] Block by policy" if not getattr(response, "choices", None) else ""

        # Track usage
        usage = getattr(response, "usage", None)
        if usage and self.agent_name:
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            pt_details = getattr(usage, "prompt_tokens_details", None)
            cached_tokens = getattr(pt_details, "cached_tokens", 0) or 0 if pt_details else 0
            try:
                from models import tracker
                tracker.tracker.track(
                    self.agent_name,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens=cached_tokens,
                    model=self.model,
                )
            except Exception:
                pass

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        msg = choice.message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        if reasoning and not content.startswith("THOUGHT:"):
            content = f"THOUGHT: {reasoning}\n\n{content}"
        elif not content and reasoning:
            content = reasoning

        if not content and finish_reason in ("content_filter", "safety"):
            return "[!] Block by policy"

        return content

    def __call__(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


# Anthropic provider
class Anthropic:
    def __init__(self, key=None, url=None, model=None, agent_name=None, config_path=None):
        # Load config
        cfg = config(config_path)
        provider_cfg = cfg.get("provider", {}) or {}
        self.agent_name = agent_name or ""

        # Setup url
        self.url = url or provider_cfg.get("url") or None
        if self.url and not str(self.url).strip():
            self.url = None

        # Setup key
        self.key = key or provider_cfg.get("key") or ""

        # Setup model
        if model:
            self.model = model
        elif agent_name and "agents" in cfg and agent_name in cfg["agents"]:
            agent_cfg = cfg["agents"][agent_name] or {}
            self.model = agent_cfg.get("model", "")
        else:
            self.model = ""

        # Initialize client
        self.client = _AnthropicClient(api_key=self.key or "none", base_url=self.url)

    def chat(self, messages, **kwargs):
        # Format messages
        system_prompt = None
        formatted_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                if system_prompt:
                    system_prompt += "\n" + content
                else:
                    system_prompt = content
            else:
                formatted_messages.append({"role": role, "content": content})

        # Setup kwargs
        call_kwargs = dict(kwargs)
        if "max_tokens" not in call_kwargs:
            call_kwargs["max_tokens"] = 4096

        if system_prompt:
            call_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if formatted_messages:
            last_msg = formatted_messages[-1]
            last_content = last_msg.get("content", "")
            if isinstance(last_content, str):
                formatted_messages[-1] = {
                    "role": last_msg["role"],
                    "content": [
                        {
                            "type": "text",
                            "text": last_content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            elif isinstance(last_content, list) and len(last_content) > 0 and isinstance(last_content[-1], dict):
                last_content[-1]["cache_control"] = {"type": "ephemeral"}

        # Send request
        response = self.client.messages.create(
            model=self.model,
            messages=formatted_messages,
            **call_kwargs
        )

        # Track usage
        usage = getattr(response, "usage", None)
        if usage and self.agent_name:
            prompt_tokens = getattr(usage, "input_tokens", 0) or 0
            completion_tokens = getattr(usage, "output_tokens", 0) or 0
            cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
            try:
                from models import tracker
                tracker.tracker.track(
                    self.agent_name,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens=cached_tokens,
                    model=self.model,
                )
            except Exception:
                pass

        # Extract response
        text_parts = [block.text for block in response.content if hasattr(block, "text")]
        return "".join(text_parts)

    def __call__(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


__all__ = ["OpenAI", "Anthropic", "config"]