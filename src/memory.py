import json
import logging
from pathlib import Path

log = logging.getLogger("vulnalysis")

COMPACTION_PROMPT_TEMPLATE = """You are a memory compaction module for an autonomous security audit agent.
Summarize the preceding security investigation trajectory into a concise, high-density factual summary (under 250 words).

CRITICAL REQUIREMENTS:
1. PRESERVE exact target file paths, line numbers, function names, and variable names that were identified.
2. PRESERVE confirmed security facts (e.g., sink location, missing bounds check, vulnerability pattern, CWE).
3. NOTE dead ends and disproven hypotheses so the agent avoids repeating failed paths.
4. NOTE the current operational state and immediate next action.
5. OMIT raw compiler warnings, repetitive grep outputs, and verbose command chatter.

Previous summary (if any):
{previous_summary}

Recent trajectory to compact:
{trajectory}

Concise Progress Summary:"""


class Memory:
    def __init__(self, max_window=10, compact_threshold=8, recent_window=3):
        # Initialize storage
        self.steps = []
        self.max_window = max_window
        self.compact_threshold = compact_threshold
        self.recent_window = recent_window
        self.summary = ""
        self.compact_count = 0

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
        self.summary = ""
        self.compact_count = 0

    # Check if messages need compaction
    def should_compact(self, messages):
        if not messages or len(messages) < 4:
            return False

        # If already compacted once, messages[2] holds the previous summary
        start_idx = 3 if (len(messages) > 2 and "<trajectory-summary>" in messages[2].get("content", "")) else 2
        dialogue_len = len(messages) - start_idx
        # Each step consists of an assistant turn and a user turn (2 messages)
        uncompacted_steps = dialogue_len // 2
        return uncompacted_steps >= self.compact_threshold

    # Compact messages into hybrid structure: Prefix + Compacted Summary + Recent Window
    def compact(self, messages, llm=None, log_fn=None):
        if not self.should_compact(messages):
            return messages

        suffix_len = self.recent_window * 2
        has_prev_summary = len(messages) > 2 and "<trajectory-summary>" in messages[2].get("content", "")

        prev_summary = self.summary
        start_idx = 3 if has_prev_summary else 2

        prefix = messages[:2]  # [system, initial_task]
        middle = messages[start_idx:-suffix_len]
        suffix = messages[-suffix_len:]

        if not middle:
            return messages

        trajectory_text = self._format_trajectory(middle)
        if not trajectory_text:
            return messages

        if log_fn:
            log_fn(f"Compacting {len(middle) // 2} steps into memory summary...")

        new_summary = self._generate_summary(trajectory_text, prev_summary, llm=llm)
        if new_summary:
            self.summary = new_summary
            self.compact_count += 1

        summary_content = (
            f"<trajectory-summary>\n"
            f"[Compacted Progress Summary - Cycle {self.compact_count}]\n"
            f"{self.summary}\n"
            f"</trajectory-summary>\n"
            f"Continue your investigation based on the above progress summary and recent observations below."
        )

        summary_msg = {"role": "user", "content": summary_content}
        compacted_messages = [prefix[0], prefix[1], summary_msg] + suffix

        if log_fn:
            log_fn(f"Compaction completed: reduced from {len(messages)} to {len(compacted_messages)} messages.")

        return compacted_messages

    # Format middle messages into readable text for summarization
    def _format_trajectory(self, middle_messages):
        lines = []
        step_idx = 1
        for msg in middle_messages:
            role = msg.get("role", "")
            content = str(msg.get("content", "")).strip()
            if role == "assistant":
                lines.append(f"\n[Step {step_idx}] Agent:")
                lines.append(content[:1500])
            elif role == "user":
                lines.append(f"[Step {step_idx}] Observation:")
                lines.append(content[:1500])
                step_idx += 1
        return "\n".join(lines).strip()

    # Generate summary with LLM or fallback heuristic
    def _generate_summary(self, trajectory_text, prev_summary="", llm=None):
        if llm and hasattr(llm, "chat"):
            try:
                prompt_content = COMPACTION_PROMPT_TEMPLATE.format(
                    previous_summary=prev_summary if prev_summary else "None (Initial cycle)",
                    trajectory=trajectory_text,
                )
                summary_req = [{"role": "user", "content": prompt_content}]
                res = llm.chat(summary_req, temperature=0.0)
                if res and not str(res).startswith("[!]"):
                    return str(res).strip()
            except Exception as e:
                log.warning(f"Memory compaction LLM call failed ({e}), falling back to heuristic.")

        # Fallback heuristic summary
        return self._heuristic_summary(trajectory_text, prev_summary)

    # Heuristic extractive summary when LLM is unavailable
    def _heuristic_summary(self, trajectory_text, prev_summary=""):
        lines = []
        if prev_summary:
            lines.append(f"Prior Context: {prev_summary[:300]}...")

        for raw_line in trajectory_text.splitlines():
            line = raw_line.strip()
            if any(line.upper().startswith(p) for p in ("THOUGHT:", "ACTION:", "```BASH", "FINDINGS:")):
                lines.append(line)

        extracted = "\n".join(lines[-15:])
        return extracted if extracted else "Investigation in progress along designated taint flow."

    # Save memory to file
    def save(self, filepath):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "summary": self.summary,
            "compact_count": self.compact_count,
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
                    self.compact_count = data.get("compact_count", 0)
                    self.steps = data.get("steps", [])
                elif isinstance(data, list):
                    self.steps = data

    def __len__(self):
        return len(self.steps)


__all__ = ["Memory"]