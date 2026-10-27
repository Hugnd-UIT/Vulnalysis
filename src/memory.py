import json
import logging
import time
from pathlib import Path

log = logging.getLogger("vulnalysis")

PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools or emit markdown bash blocks.

Your task is to create a detailed summary of the execution trajectory so far, paying close attention to the assigned task and previous actions.
This summary should capture technical details, code patterns, and decisions essential for continuing execution without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts. In your analysis process:
1. Chronologically analyze each step: identify the assigned task, actions taken, technical details, files examined or modified, errors encountered, and progress made.
2. Ensure technical accuracy and keep the summary factual and concise (under 250 words).

Wrap your final summary in <summary> tags with the following sections:

1. Primary Request and Intent: Core objective and assigned task for this agent.
2. Key Technical Concepts: Important technical details, tools, configurations, or verified findings.
3. Files and Code Sections: Specific files, functions, scripts, or commands examined, modified, or executed.
4. Problem Solving: Errors encountered, failed attempts, and how issues were addressed (to avoid repeating mistakes).
5. Current Work and Next Steps: Exactly what was being worked on right before this summary and the immediate next step.

Previous summary (if any):
{previous}

Trajectory to compact:
{trajectory}

Output:"""


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

    # Check if messages need compact
    def check(self, messages):
        if not messages or len(messages) < 4:
            return False

        start = 3 if (len(messages) > 2 and "<summary>" in messages[2].get("content", "")) else 2
        span = len(messages) - start

        steps = span // 2
        return steps >= self.threshold

    # Compact messages
    def compact(self, messages, llm=None, log=None):
        if not self.check(messages):
            return messages

        tail = self.recent * 2
        has_summary = len(messages) > 2 and "<summary>" in messages[2].get("content", "")

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

        update = self._summarize(text, prev, llm=llm)
        if update:
            self.summary = update
            self.cycles += 1

        content = (
            f"<summary>\n"
            f"[Progress summary - cycle {self.cycles}]\n"
            f"{self.summary}\n"
            f"</summary>\n"
            f"Continue your investigation based on the above progress summary and recent observations below."
        )

        item = {"role": "user", "content": content}
        res = [prefix[0], prefix[1], item] + suffix

        return res

    # Format middle messages
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

    # Generate summary with LLM
    def _summarize(self, text, prev="", llm=None, tries=3):
        if not llm or not hasattr(llm, "chat"):
            return ""

        body = PROMPT.format(
            previous=prev if prev else "None (Initial cycle)",
            trajectory=text,
        )
        req = [{"role": "user", "content": body}]
        for attempt in range(tries):
            try:
                res = llm.chat(req, temperature=0.0)
                if res and not str(res).startswith("[!]"):
                    clean = str(res).strip()
                    if "<summary>" in clean and "</summary>" in clean:
                        clean = clean.split("<summary>")[1].split("</summary>")[0].strip()
                    elif "</analysis>" in clean:
                        clean = clean.split("</analysis>")[1].strip()
                    return clean
            except Exception as e:
                if attempt < tries - 1:
                    delay = 2 * (attempt + 1)
                    time.sleep(delay)
                else:
                    raise e
        return ""

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