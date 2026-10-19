import json
from pathlib import Path


from src.dedup import score, dedup, rank, combine


class PipelineState:
    score = staticmethod(score)
    dedup = staticmethod(dedup)
    rank = staticmethod(rank)
    combine = staticmethod(combine)

    def __init__(self, outputs_path="outputs"):
        self.outputs_path = Path(outputs_path)
        self.outputs_path.mkdir(parents=True, exist_ok=True)

        self.flows_file = None
        self.total_flows = 0
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
                content = f.read().strip()
                if content:
                    decoder = json.JSONDecoder()
                    idx = 0
                    while idx < len(content):
                        while idx < len(content) and content[idx].isspace():
                            idx += 1
                        if idx >= len(content):
                            break
                        try:
                            obj, end = decoder.raw_decode(content, idx)
                            if isinstance(obj, dict):
                                raw.append(obj)
                            elif isinstance(obj, list):
                                raw.extend([x for x in obj if isinstance(x, dict)])
                            idx = end
                        except Exception:
                            idx += 1
        processed = combine(raw)
        self.flows_file = p
        self.total_flows = len(processed)
        self.index = 0
        if p.exists() or processed:
            with open(p, "w", encoding="utf-8") as f:
                for flow in processed:
                    f.write(json.dumps(flow, ensure_ascii=False) + "\n")
        return self.total_flows

    def next_flow(self):
        if self.index >= self.total_flows or not self.flows_file or not self.flows_file.exists():
            self.flow = None
            return None

        target_idx = self.index
        flow = None
        with open(self.flows_file, "r", encoding="utf-8") as f:
            for current_idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                if current_idx == target_idx:
                    try:
                        flow = json.loads(line)
                    except Exception:
                        flow = None
                    break

        self.index += 1
        self.flow = flow
        self.candidate = None
        self.target = None
        self.debate_round = 0
        self.pov_round = 0
        return self.flow

    def has_flows(self):
        return self.index < self.total_flows

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
            "total_flows": self.total_flows,
            "processed": self.index,
            "confirmed": len(self.confirmed),
            "rejected": len(self.rejected),
        }


__all__ = ["PipelineState"]