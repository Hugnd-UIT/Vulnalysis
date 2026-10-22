import json
from pathlib import Path
import yaml


def config(config_path=None):
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.yml"
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


class QuotaTracker:
    def __init__(self, config_path=None):
        # Load configuration
        self.cfg = config(config_path)

        # Setup agent configs
        self.agent_configs = {}
        agents_cfg = self.cfg.get("agents", {}) or {}
        for agent_name, agent_info in agents_cfg.items():
            if isinstance(agent_info, dict):
                input_rate = agent_info.get("input-rate")
                output_rate = agent_info.get("output-rate")
                self.agent_configs[agent_name] = {
                    "model": agent_info.get("model", "") or "",
                    "input_rate": float(input_rate) if input_rate is not None else 0.0,
                    "output_rate": float(output_rate) if output_rate is not None else 0.0,
                }

        # Initialize storage
        self.agents = {}
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens = 0
        self.total_requests = 0
        self.total_cost = 0.0

    # Estimate cost
    def estimate(self, agent_name, input_tokens, output_tokens, cached_tokens=0):
        # Read rates
        rates = self.agent_configs.get(agent_name, {})
        input_rate = rates.get("input_rate", 0.0)
        output_rate = rates.get("output_rate", 0.0)

        # Cached tokens get a 90% discount (Anthropic cache read is 10% rate)
        regular_input = max(0, input_tokens - cached_tokens)
        input_cost = (regular_input / 1_000_000) * input_rate
        cached_cost = (cached_tokens / 1_000_000) * (input_rate * 0.1)
        output_cost = (output_tokens / 1_000_000) * output_rate
        return input_cost + cached_cost + output_cost

    # Track usage
    def track(self, agent_name, input_tokens, output_tokens, cached_tokens=0, model=None):
        # Resolve model
        cfg_entry = self.agent_configs.get(agent_name, {})
        target_model = model or cfg_entry.get("model", "")
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        cached_tokens = int(cached_tokens or 0)

        # Calculate cost
        cost = self.estimate(agent_name, input_tokens, output_tokens, cached_tokens=cached_tokens)

        # Update totals
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cached_tokens += cached_tokens
        self.total_requests += 1
        self.total_cost += cost

        # Initialize agent record
        if agent_name not in self.agents:
            self.agents[agent_name] = {
                "model": target_model,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "requests": 0,
                "cost": 0.0,
            }

        # Update agent stats
        entry = self.agents[agent_name]
        entry["model"] = target_model
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["cached_tokens"] = entry.get("cached_tokens", 0) + cached_tokens
        entry["requests"] += 1
        entry["cost"] += cost

        return cost

    # Check cost limit
    def is_exceeded(self, agent_name=None, cost_limit=15.0):
        if agent_name and agent_name in self.agents:
            return self.agents[agent_name]["cost"] >= cost_limit
        return self.total_cost >= cost_limit

    # Get agent stats
    def get_agent_stats(self, agent_name):
        return self.agents.get(
            agent_name,
            {
                "model": self.agent_configs.get(agent_name, {}).get("model", ""),
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "requests": 0,
                "cost": 0.0,
            },
        )

    # Get summary
    def summary(self):
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_requests": self.total_requests,
            "total_cost": round(self.total_cost, 4),
            "agents": self.agents,
        }

    # Reset stats
    def reset(self):
        self.agents.clear()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens = 0
        self.total_requests = 0
        self.total_cost = 0.0

    # Save report
    def save(self, filepath):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=2)


# Default instance
tracker = QuotaTracker()

__all__ = ["QuotaTracker", "tracker"]