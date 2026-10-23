import logging
from pathlib import Path
import time
import yaml
from openai import OpenAI as _OpenAIClient
from anthropic import Anthropic as _AnthropicClient

log = logging.getLogger("provider")


def retry(call_fn, agent_name="", max_retries=8, base_delay=4.0):
    for attempt in range(max_retries):
        try:
            return call_fn()
        except Exception as e:
            err_name = type(e).__name__
            err_msg = str(e)
            status_code = getattr(e, "status_code", None) or getattr(e, "code", None)

            non_retryable_codes = {400, 401, 403, 404, 422}
            if status_code in non_retryable_codes:
                raise e

            non_retryable_names = (
                "AuthenticationError",
                "BadRequestError",
                "NotFoundError",
                "PermissionDeniedError",
                "UnsupportedParamsError",
            )
            if any(nr in err_name for nr in non_retryable_names):
                raise e

            if attempt == max_retries - 1:
                log.error(f"[{agent_name or 'provider'}] LLM call failed after {max_retries} attempts: status={status_code} error={e}")
                raise e

            delay = min(60.0, base_delay * (2 ** min(attempt, 4)))
            status_str = f" [HTTP {status_code}]" if status_code else ""
            log.warning(
                f"[{agent_name or 'provider'}] LLM call error{status_str} ({err_name}: {err_msg[:120]}). "
                f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})..."
            )
            time.sleep(delay)


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
        def _send():
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                **call_kwargs
            )

        response = retry(_send, agent_name=self.agent_name)
        if not response or not getattr(response, "choices", None):
            resp_dict = getattr(response, "model_dump", None) and response.model_dump() or {}
            inner_resp = resp_dict.get("response") if isinstance(resp_dict, dict) else None
            pf = inner_resp.get("promptFeedback") if isinstance(inner_resp, dict) else None
            reason = pf.get("blockReason") if pf else "safety filter"
            log.warning(f"[{self.agent_name}] Request blocked by safety policy ({reason}).")
            return "[Blocked by safety policy. Please rephrase or use an alternative approach.]"

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
            log.warning(f"[{self.agent_name}] Response blocked by safety filter.")
            return "[Blocked by safety policy. Please rephrase or use an alternative approach.]"

        if finish_reason == "length":
            log.warning(f"[{self.agent_name}] Response was truncated due to output token limit.")
            content += "\n\n[WARNING: Response was truncated due to output token limit. Please output more concisely or split the command.]"

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
        def _send():
            return self.client.messages.create(
                model=self.model,
                messages=formatted_messages,
                **call_kwargs
            )

        response = retry(_send, agent_name=self.agent_name)

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
        stop_reason = getattr(response, "stop_reason", None)
        text_parts = [block.text for block in response.content if hasattr(block, "text")]
        content = "".join(text_parts)

        if not content and stop_reason in ("content_filter", "safety"):
            log.warning(f"[{self.agent_name}] Anthropic response blocked by safety filter.")
            return "[Blocked by safety policy. Please rephrase or use an alternative approach.]"

        if stop_reason == "max_tokens":
            log.warning(f"[{self.agent_name}] Anthropic response was truncated due to output token limit.")
            content += "\n\n[WARNING: Response was truncated due to output token limit. Please output more concisely or split the command.]"

        return content

    def __call__(self, messages, **kwargs):
        return self.chat(messages, **kwargs)


__all__ = ["OpenAI", "Anthropic", "config", "retry"]