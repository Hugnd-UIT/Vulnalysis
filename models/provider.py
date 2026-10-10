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
        self.client = _OpenAIClient(api_key=self.key, base_url=self.url)

    def chat(self, messages, **kwargs):
        # Send request
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs
        )
        return response.choices[0].message.content or ""

    def __call__(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


# Anthropic provider
class Anthropic:
    def __init__(self, key=None, url=None, model=None, agent_name=None, config_path=None):
        # Load config
        cfg = config(config_path)
        provider_cfg = cfg.get("provider", {}) or {}

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
        self.client = _AnthropicClient(api_key=self.key, base_url=self.url)

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
            call_kwargs["system"] = system_prompt

        # Send request
        response = self.client.messages.create(
            model=self.model,
            messages=formatted_messages,
            **call_kwargs
        )

        # Extract response
        text_parts = [block.text for block in response.content if hasattr(block, "text")]
        return "".join(text_parts)

    def __call__(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


__all__ = ["OpenAI", "Anthropic", "config"]