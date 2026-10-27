import json
import logging
from pathlib import Path

log = logging.getLogger("vulnalysis")

PROMPT = """You are a memory compaction module for an autonomous security audit agent.
Summarize the preceding security investigation trajectory into a concise, high-density factual summary (under 250 words).

CRITICAL REQUIREMENTS:
1. PRESERVE exact target file paths, line numbers, function names, and variable names that were identified.
2. PRESERVE confirmed security facts (e.g., sink location, missing bounds check, vulnerability pattern, CWE).
3. NOTE dead ends and disproven hypotheses so the agent avoids repeating failed paths.
4. NOTE the current operational state and immediate next action.
5. OMIT raw compiler warnings, repetitive grep outputs, and verbose command chatter.

Previous summary (if any):
{previous}

Recent trajectory to compact:
{trajectory}

Concise Progress Summary:"""


class Memory:
    def __init__(self, limit=10, threshold=8, recent=3):
        # Initialize storage
        self.steps = []
        self.limit = limit
        self.threshold = threshold
        self.recent = recent
        self.summary = ""
        self.cycles = 0

    # Add step
    def add(self, thought="", action=None, command="", observation="", **kwargs):
        step = len(self.steps) + 1
        entry = {
            "step": step,
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
        n = size or self.limit
        return self.steps[-n:] if n > 0 else list(self.steps)

    # Get last step
    def last(self):
        return self.steps[-1] if self.steps else None

    # Format window for prompt
    def format(self, size=None):
        items = self.window(size)
        if not items:
            return ""

        # Build formatted text
        lines = []
        for s in items:
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
        self.summary = ""
        self.cycles = 0

    # Check if messages need compaction
    def check(self, messages):
        if not messages or len(messages) < 4:
            return False

        # If already compacted once, messages[2] holds the previous summary
        start = 3 if (len(messages) > 2 and "<trajectory-summary>" in messages[2].get("content", "")) else 2
        span = len(messages) - start
        # Each step consists of an assistant turn and a user turn (2 messages)
        steps = span // 2
        return steps >= self.threshold

    # Compact messages into hybrid structure: Prefix + Compacted Summary + Recent Window
    def compact(self, messages, llm=None, log=None):
        if not self.check(messages):
            return messages

        tail = self.recent * 2
        has_summary = len(messages) > 2 and "<trajectory-summary>" in messages[2].get("content", "")

        prev = self.summary
        start = 3 if has_summary else 2

        prefix = messages[:2]  # [system, initial_task]
        middle = messages[start:-tail]
        suffix = messages[-tail:]

        if not middle:
            return messages

        text = self._render(middle)
        if not text:
            return messages

        if log:
            log(f"Compacting {len(middle) // 2} steps into memory summary...")

        update = self._summarize(text, prev, llm=llm)
        if update:
            self.summary = update
            self.cycles += 1

        content = (
            f"<trajectory-summary>\n"
            f"[Compacted Progress Summary - Cycle {self.cycles}]\n"
            f"{self.summary}\n"
            f"</trajectory-summary>\n"
            f"Continue your investigation based on the above progress summary and recent observations below."
        )

        item = {"role": "user", "content": content}
        res = [prefix[0], prefix[1], item] + suffix

        if log:
            log(f"Compaction completed: reduced from {len(messages)} to {len(res)} messages.")

        return res

    # Format middle messages into readable text for summarization
    def _render(self, messages):
        lines = []
        idx = 1
        for msg in messages:
            role = msg.get("role", "")
            body = str(msg.get("content", "")).strip()
            if role == "assistant":
                lines.append(f"\n[Step {idx}] Agent:")
                lines.append(body[:1500])
            elif role == "user":
                lines.append(f"[Step {idx}] Observation:")
                lines.append(body[:1500])
                idx += 1
        return "\n".join(lines).strip()

    # Generate summary with LLM or fallback heuristic
    def _summarize(self, text, prev="", llm=None):
        if llm and hasattr(llm, "chat"):
            try:
                body = PROMPT.format(
                    previous=prev if prev else "None (Initial cycle)",
                    trajectory=text,
                )
                req = [{"role": "user", "content": body}]
                res = llm.chat(req, temperature=0.0)
                if res and not str(res).startswith("[!]"):
                    return str(res).strip()
            except Exception as e:
                log.warning(f"Memory compaction LLM call failed ({e}), falling back to heuristic.")

        # Fallback heuristic summary
        return self._fallback(text, prev)

    # Heuristic extractive summary when LLM is unavailable
    def _fallback(self, text, prev=""):
        lines = []
        if prev:
            lines.append(f"Prior Context: {prev[:300]}...")

        for raw in text.splitlines():
            s = raw.strip()
            if any(s.upper().startswith(p) for p in ("THOUGHT:", "ACTION:", "```BASH", "FINDINGS:")):
                lines.append(s)

        out = "\n".join(lines[-15:])
        return out if out else "Investigation in progress along designated taint flow."

    # Save memory to file
    def save(self, filepath):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "summary": self.summary,
            "cycles": self.cycles,
            "steps": self.steps,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # Load memory from file
    def load(self, filepath):
        path = Path(filepath)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.summary = data.get("summary", "")
                    self.cycles = data.get("cycles", 0)
                    self.steps = data.get("steps", [])
                elif isinstance(data, list):
                    self.steps = data

    def __len__(self):
        return len(self.steps)


__all__ = ["Memory"]