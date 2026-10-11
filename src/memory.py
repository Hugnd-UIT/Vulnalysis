import json
from pathlib import Path


class Memory:
    def __init__(self, max_window=10):
        # Initialize storage
        self.steps = []
        self.max_window = max_window

    # Add step
    def add(self, thought="", action=None, command="", observation="", **kwargs):
        step_number = len(self.steps) + 1
        entry = {
            "step": step_number,
            "thought": thought,
            "action": action,
            "command": command,
            "observation": observation,
        }
        entry.update(kwargs)
        self.steps.append(entry)
        return entry

    # Get recent window
    def window(self, size=None):
        limit = size or self.max_window
        return self.steps[-limit:] if limit > 0 else list(self.steps)

    # Get last step
    def last(self):
        return self.steps[-1] if self.steps else None

    # Format window for prompt
    def format(self, size=None):
        recent = self.window(size)
        if not recent:
            return ""

        # Build formatted text
        lines = []
        for s in recent:
            lines.append(f"Step {s['step']}:")
            if s.get("thought"):
                lines.append(f"THOUGHT: {s['thought']}")
            if s.get("command"):
                lines.append(f"ACTION: {s.get('action', 'bash')} `{s['command']}`")
            elif s.get("action"):
                lines.append(f"ACTION: {s['action']}")
            if s.get("observation"):
                lines.append(f"OBSERVATION: {s['observation']}")
            lines.append("")

        return "\n".join(lines).strip()

    # Get all steps
    def all(self):
        return list(self.steps)

    # Clear memory
    def clear(self):
        self.steps.clear()

    # Save memory to file
    def save(self, filepath):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.steps, f, indent=2)

    # Load memory from file
    def load(self, filepath):
        path = Path(filepath)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self.steps = data

    def __len__(self):
        return len(self.steps)


__all__ = ["Memory"]