import json
from pathlib import Path


from src.dedup import score, dedup, rank


class PipelineState:
    score = staticmethod(score)
    dedup = staticmethod(dedup)
    rank = staticmethod(rank)

    def __init__(self, outputs_path="outputs"):
        self.outputs_path = Path(outputs_path)
        self.outputs_path.mkdir(parents=True, exist_ok=True)

        self.flows = []
        self.index = 0
        self.flow = None

        self.candidate = None
        self.target = None
        self.debate_round = 0
        self.max_debate = 10

        self.pov_round = 0
        self.max_pov = 10

        self.confirmed = []
        self.rejected = []
        self.harness = None

    def flow_dir(self, flow_id):
        p = self.outputs_path / str(flow_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def load_flows(self, path=None):
        p = Path(path) if path else self.outputs_path / "taint-flows.jsonl"
        raw = []
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        raw.append(json.loads(line))
        self.flows = rank(dedup(raw))
        self.index = 0
        return len(self.flows)

    def next_flow(self):
        if self.index >= len(self.flows):
            self.flow = None
            return None
        self.flow = self.flows[self.index]
        self.index += 1
        self.candidate = None
        self.target = None
        self.debate_round = 0
        self.pov_round = 0
        return self.flow

    def has_flows(self):
        return self.index < len(self.flows)

    def set_candidate(self, hypothesis):
        self.candidate = hypothesis

    def approve(self, hypothesis):
        self.target = hypothesis

    def reject(self, reason=""):
        hyp = self.candidate or self.target
        if hyp:
            self.rejected.append({"hypothesis": hyp, "reason": reason})
        self.candidate = None
        self.target = None

    def confirm(self, verdict):
        self.confirmed.append({
            "flow": self.flow,
            "hypothesis": self.target,
            "verdict": verdict,
        })

    def inc_debate(self):
        self.debate_round += 1
        return self.debate_round

    def debate_exceeded(self):
        return self.debate_round >= self.max_debate

    def inc_pov(self):
        self.pov_round += 1
        return self.pov_round

    def pov_exceeded(self):
        return self.pov_round >= self.max_pov

    def save_hypothesis(self, flow_id, hypothesis):
        p = self.flow_dir(flow_id) / "hypotheses.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(hypothesis, f, indent=2, ensure_ascii=False)
        return str(p)

    def load_hypothesis(self, flow_id):
        p = self.flow_dir(flow_id) / "hypotheses.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_script(self, flow_id, code):
        p = self.flow_dir(flow_id) / "scripts.py"
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        return str(p)

    def load_script(self, flow_id):
        p = self.flow_dir(flow_id) / "scripts.py"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def record(self, flow_id, attempt_num, script_code, exec_result, status="RETRYING"):
        p = self.flow_dir(flow_id) / "results.json"
        data = {}
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data["status"] = status
        data["total"] = attempt_num
        data[f"pov_{attempt_num}"] = script_code
        data[f"res_{attempt_num}"] = exec_result

        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(p)

    def update_status(self, flow_id, status):
        p = self.flow_dir(flow_id) / "results.json"
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["status"] = status
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    def load_results(self, flow_id):
        p = self.flow_dir(flow_id) / "results.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_json(self, name, data):
        path = self.outputs_path / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(path)

    def load_json(self, name):
        path = self.outputs_path / name
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def summary(self):
        return {
            "total_flows": len(self.flows),
            "processed": self.index,
            "confirmed": len(self.confirmed),
            "rejected": len(self.rejected),
        }


__all__ = ["PipelineState"]