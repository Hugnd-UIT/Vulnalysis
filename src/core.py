import json
import logging
import sys
import time
import threading
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.state import State
from docker.sandbox import init as sandbox_init

from utils.logger import setup_logger

log = setup_logger()


class Orchestrator:
    def __init__(self, repo_path, outputs_path="outputs", config_path=None, provider_cls=None, skip_prep=False):
        self.repo = Path(repo_path).resolve()
        self.outputs_path = Path(outputs_path).resolve()
        self.config_path = config_path
        self.state = State(self.outputs_path)
        self.skip_prep = skip_prep

        # Provider config
        if provider_cls is None:
            from models.provider import OpenAI
            self.provider_cls = OpenAI
        else:
            self.provider_cls = provider_cls

        # Container config
        self.repo_container = "/data"
        self.outputs_container = "/outputs"
        self.container = None

    # Init sandbox
    def init_sandbox(self):
        self.container = sandbox_init(repo_path=str(self.repo), outputs_path=str(self.outputs_path))
        log.info(f"{self.container.name} [{self.container.status}]")
        return self.container

    # Phase 1
    def phase_prep(self):
        log.info("=== Preprocessing ===")

        try:
            import graphify
            graph_out = self.outputs_path / "call-graph.json"
            flows_out = self.outputs_path / "call-flows.jsonl"
            graphify.callgraph(str(self.repo), str(graph_out), flow=str(flows_out))
            if flows_out.exists():
                from src.dedup import filter as filter_flows
                raw_lines = flows_out.read_text(encoding="utf-8").splitlines()
                cleaned = filter_flows(raw_lines)
                flows_out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in cleaned) + "\n", encoding="utf-8")
        except Exception as e:
            log.warning(f"Graphify: {e}")

        retriever_result = [None]
        builder_result = [None]
        builder_agent_ref = [None]

        # Retriever thread
        def run_retriever():
            from agents import retriever
            agent = retriever.RepoRetrieverAgent(
                sandbox=self.container,
                config_path=self.config_path,
                provider_cls=self.provider_cls,
            )
            graph_path = f"{self.outputs_container}/call-graph.json"
            flows_path = f"{self.outputs_container}/call-flows.jsonl"
            taint_path = f"{self.outputs_container}/taint-flows.jsonl"
            retriever_result[0] = agent.run(
                self.repo_container,
                graph_path=graph_path,
                flows_path=flows_path,
                taint_path=taint_path,
            )
            log.info(f"Repo retriever finished: {retriever_result[0]}\n")

        # Builder thread
        def run_builder():
            from agents import builder
            agent = builder.RepoBuilderAgent(
                sandbox=self.container,
                config_path=self.config_path,
                provider_cls=self.provider_cls,
            )
            builder_agent_ref[0] = agent
            builder_result[0] = agent.run(self.repo_container)
            log.info(f"Repo builder finished: {builder_result[0]}\n")

        t_retriever = threading.Thread(target=run_retriever, name="retriever")
        t_builder = threading.Thread(target=run_builder, name="builder")

        t_retriever.start()
        time.sleep(0.5)
        t_builder.start()

        t_retriever.join()
        t_builder.join()

        # Check harnesses
        import importlib
        fuzz_mod = importlib.import_module("src.fuzz-harness")
        self.state.harness = fuzz_mod.check(self.repo, self.outputs_path)

        b_res = builder_result[0]
        build_ok = False
        if isinstance(b_res, dict) and b_res.get("status", "").upper() == "READY":
            build_ok = True
        elif b_res is True:
            build_ok = True

        if not build_ok:
            log.error("[!] Build failed!")
            explanation = ""
            if builder_agent_ref[0]:
                try:
                    explanation = builder_agent_ref[0].explain_failure()
                except Exception as e:
                    explanation = f"Could not ask builder: {e}"
            print(f"\n\033[1;31m[!] Build failed! Reason from builder:\n{explanation or 'No explanation returned'}\033[0m\n")
            return retriever_result[0], False

        return retriever_result[0], True

    # Phase 3
    def phase_pov(self, hypothesis, feedback=None):
        flow_id = hypothesis.get("id") or (self.state.flow.get("id") if self.state.flow else f"flow-{self.state.index}")
        log.info("=== Dynamic analyzing ===")

        from agents import generator, verifier

        self.state.flow_dir(flow_id)
        pov_script = f"{self.outputs_container}/{flow_id}/scripts.py"
        result_file = f"{self.outputs_container}/{flow_id}/results.json"

        gen_agent = generator.PovGeneratorAgent(
            sandbox=self.container,
            config_path=self.config_path,
            provider_cls=self.provider_cls,
        )
        ver_agent = verifier.PovVerifierAgent(
            sandbox=self.container,
            config_path=self.config_path,
            provider_cls=self.provider_cls,
        )

        last_verdict = None
        while not self.state.pov_exceeded():
            round_num = self.state.inc_pov()

            # Run generator
            harness_data = self.state.harness if self.state.harness and self.state.harness.get("found") else None
            gen_res = gen_agent.run(
                self.repo_container,
                hypothesis,
                pov_script=pov_script,
                pov_result=result_file,
                feedback=feedback,
                harness=harness_data,
            )

            script_code = self.state.load_script(flow_id)
            pov_result = self.state.load_results(flow_id) or {}

            res_data = None
            if isinstance(gen_res, dict) and isinstance(gen_res.get("res"), dict):
                res_data = gen_res["res"]
            elif isinstance(pov_result.get("res"), dict):
                res_data = pov_result["res"]
            elif isinstance(pov_result.get(f"res_{round_num}"), dict):
                res_data = pov_result[f"res_{round_num}"]
            elif isinstance(pov_result, dict) and any(k in pov_result for k in ("exit_code", "output", "status")):
                res_data = {
                    "exit_code": pov_result.get("exit_code"),
                    "output": pov_result.get("output", ""),
                    "status": pov_result.get("status"),
                }
            else:
                res_data = {
                    "exit_code": None,
                    "output": "",
                    "status": None,
                }

            self.state.record(
                flow_id=flow_id,
                attempt_num=round_num,
                script_code=script_code,
                exec_result=res_data,
                status="RETRYING",
            )

            # Run verifier
            verdict = ver_agent.run(
                self.repo_container,
                hypothesis,
                pov_script=pov_script,
                pov_result=res_data,
            )

            if not verdict or not isinstance(verdict, dict):
                log.warning("Verifier no verdict")
                feedback = {"decision": "RETRY", "issues": ["No verdict produced"], "suggestions": ["Re-run test"]}
                continue

            last_verdict = verdict
            decision = verdict.get("decision", "").upper()
            if decision in ("CONFIRMED", "UNCONFIRMED") or ("alignment" in verdict and "issues" not in verdict):
                log.info(f"Verifier feedback to coordinator: {decision}")
                return verdict

            # Retry with feedback
            log.info("Verifier feedback to PoV generator")
            feedback = verdict

        return last_verdict or {"decision": "UNCONFIRMED", "reason": "PoV rounds exhausted"}

    # Phase 2
    def phase_analysis(self, taint_flow):
        flow_id = taint_flow.get("id", f"flow-{self.state.index}")
        log.info("=== Static analyzing ===")

        from agents import analyzer, coordinator
        prev_feedback = None

        self.state.flow_dir(flow_id)
        hyp_file = f"{self.outputs_container}/{flow_id}/hypotheses.json"

        analyzer_agent = analyzer.VulnAnalyzerAgent(
            sandbox=self.container,
            config_path=self.config_path,
            provider_cls=self.provider_cls,
        )
        coordinator_agent = coordinator.CoordinatorAgent(
            sandbox=self.container,
            config_path=self.config_path,
            provider_cls=self.provider_cls,
        )

        while not self.state.debate_exceeded():
            round_num = self.state.inc_debate()

            # Run analyzer
            analyzer_res = analyzer_agent.run(
                self.repo_container,
                taint_flow,
                hypotheses=hyp_file,
                feedback=prev_feedback,
            )

            candidate = None
            if isinstance(analyzer_res, dict):
                candidate = analyzer_res
                self.state.save_hypothesis(flow_id, candidate)
            else:
                candidate = self.state.load_hypothesis(flow_id)

            if not candidate:
                candidate = {
                    "id": flow_id,
                    "verdict": "BENIGN",
                    "title": taint_flow.get("title", ""),
                    "cwe": taint_flow.get("cwe") or [],
                    "description": "",
                    "source": taint_flow.get("source", {}),
                    "sink": taint_flow.get("sink", {}),
                    "flow": taint_flow.get("flow", {"chain": taint_flow.get("chain", [])}),
                    "status": "UNCONFIRMED",
                }
                self.state.save_hypothesis(flow_id, candidate)

            analyzer_verdict = str(candidate.get("verdict", "")).upper()
            if not analyzer_verdict:
                analyzer_verdict = "BENIGN" if candidate.get("status") == "UNCONFIRMED" else "VULNERABLE"

            self.state.set_candidate(candidate)

            feedback = coordinator_agent.run(
                self.repo_container,
                candidate,
                taint_flow,
                analyzer=candidate if round_num > 1 else None,
            )

            if not feedback or not isinstance(feedback, dict):
                log.warning("Coordinator no feedback")
                break

            decision = feedback.get("decision", "").upper()
            log.info(f"Coordinator: {decision}")

            # Coordinator accepts
            if decision == "ACCEPT":
                approved = candidate
                self.state.approve(approved)
                self.state.save_hypothesis(flow_id, approved)

                verdict = self.phase_pov(approved)

                coord_verdict = coordinator_agent.run(
                    self.repo_container,
                    approved,
                    taint_flow,
                    verifier=verdict,
                )

                if not coord_verdict or not isinstance(coord_verdict, dict):
                    coord_verdict = verdict or {"decision": "UNCONFIRMED"}

                coord_decision = coord_verdict.get("decision", "").upper()
                log.info(f"Coordinator finished: {coord_decision}")

                # Coordinator confirms
                if coord_decision == "CONFIRMED":
                    self.state.update_status(flow_id, "CONFIRMED")
                    self.state.confirm(coord_verdict)
                    return coord_verdict

                # Coordinator requests PoV regeneration
                if coord_decision == "REGENERATE":
                    log.info("Coordinator feedback to PoV generator")
                    regen_verdict = self.phase_pov(approved, feedback=coord_verdict)
                    final_coord_verdict = coordinator_agent.run(
                        self.repo_container,
                        approved,
                        taint_flow,
                        verifier=regen_verdict,
                    )
                    final_decision = (final_coord_verdict or {}).get("decision", "").upper()
                    if final_decision == "CONFIRMED":
                        self.state.update_status(flow_id, "CONFIRMED")
                        self.state.confirm(final_coord_verdict)
                        return final_coord_verdict

                # Coordinator revision
                if coord_decision == "REVISE":
                    prev_feedback = coord_verdict
                    continue

                # Coordinator rejects
                self.state.update_status(flow_id, "FAILED")
                self.state.reject(reason=coord_verdict.get("reason", "Coordinator rejected verdict"))
                return None

            # Coordinator rejects
            if decision == "REJECT":
                if analyzer_verdict == "BENIGN":

                    # Consensus
                    self.state.update_status(flow_id, "FAILED")
                    self.state.reject(reason=feedback.get("reason", "Consensus: not vulnerable"))
                    return None
                else:

                    # Disagreement
                    prev_feedback = feedback
                    continue

            # Coordinator revision
            if decision == "REVISE":
                prev_feedback = feedback
                continue

        log.warning("Debate rounds exhausted without approval")
        self.state.update_status(flow_id, "FAILED")
        self.state.reject(reason="Debate rounds exhausted")
        return None

    # Phase 4
    def phase_report(self):
        log.info("=== Reporting ===")

        report = {
            "summary": self.state.summary(),
            "vulnerabilities": [],
        }

        for entry in self.state.confirmed:
            hyp = entry.get("hypothesis", {}) or {}
            flow = entry.get("flow", {}) or {}
            flow_id = hyp.get("id") or flow.get("id", "")
            verdict = entry.get("verdict", {}) or {}
            sink = verdict.get("sink") or hyp.get("sink", {}) or {}
            source = verdict.get("source") or hyp.get("source", {}) or {}

            cwe_raw = verdict.get("cwe") or hyp.get("cwe", "Unknown")
            cwe_str = ", ".join(cwe_raw) if isinstance(cwe_raw, list) else str(cwe_raw)
            title = verdict.get("title") or hyp.get("title", f"Vulnerability {flow_id}")
            description = verdict.get("description") or hyp.get("description", "")
            remediation = verdict.get("remediation") or hyp.get("remediation", "")

            vuln = {
                "id": flow_id,
                "cwe": cwe_str,
                "title": title,
                "description": description,
                "source": source,
                "sink": sink,
                "flow": hyp.get("flow", {}),
                "hint": hyp.get("hint", ""),
                "expected": hyp.get("expected", ""),
                "oracle": hyp.get("oracle", {}) or {},
                "remediation": remediation,
                "script_path": f"/outputs/{flow_id}/scripts.py",
                "results_path": f"/outputs/{flow_id}/results.json",
                "verdict": verdict,
            }
            report["vulnerabilities"].append(vuln)

        # Save JSON
        json_path = self.state.save_json("report.json", report)
        log.info(f"Report JSON saved to {json_path}")

        # Save Markdown
        md_lines = [
            "# Vulnalysis report",
            "",
            "## Summary",
            f"- **Confirmed Vulnerabilities:** {len(self.state.confirmed)}",
            "",
            "## Vulnerabilities",
        ]
        if not self.state.confirmed:
            md_lines.append("\n*No confirmed vulnerabilities found.*")
        else:
            for idx, item in enumerate(report["vulnerabilities"], 1):
                sink_info = f"{item['sink'].get('file', '')}:{item['sink'].get('function', '')}:{item['sink'].get('line', '')}"
                ep_info = item['source'].get('ep', item['source'].get('function', ''))
                chain_info = " -> ".join(item.get("flow", {}).get("chain", [])) or "Direct"
                verdict_info = item.get("verdict", {})
                verdict_str = f"{verdict_info.get('decision', 'CONFIRMED')}: {verdict_info.get('reason', '')}"
                cmd_info = item.get("oracle", {}).get("command", "")
                cmd_str = f"`{cmd_info}`" if cmd_info else "N/A"

                md_lines.extend([
                    f"\n### {idx}. [{item['cwe']}] {item['title']} (Flow: {item['id']})",
                    f"- **Source:** `{ep_info}`",
                    f"- **Sink:** `{sink_info}`",
                    f"- **Flow Chain:** `{chain_info}`",
                    f"- **Description:** {item['description']}",
                    f"- **Hint:** {item['hint']}",
                    f"- **Expected behavior:** {item['expected']}",
                    f"- **Oracle:** {cmd_str}",
                    f"- **Remediation:** {item['remediation']}",
                    f"- **PoV Script:** `{item['script_path']}`",
                    f"- **PoV Results:** `{item['results_path']}`",
                    f"- **Verdict:** {verdict_str}",
                ])

        md_path = self.outputs_path / "report.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))
        log.info(f"Report Markdown saved to {md_path}")

        return report

    def run(self):
        start = time.time()

        # Init sandbox
        self.init_sandbox()

        if self.skip_prep and (self.outputs_path / "taint-flows.jsonl").exists():
            build_ok = True
            taint_path = str(self.outputs_path / "taint-flows.jsonl")
            if not self.state.harness:
                import importlib
                fuzz_mod = importlib.import_module("src.fuzz-harness")
                self.state.harness = fuzz_mod.check(self.repo, self.outputs_path)
        else:
            taint_path, build_ok = self.phase_prep()

        if not build_ok:
            log.error("Pipeline stopped due to target repository build failure.")
            return self.phase_report()

        # Load taint flows
        count = self.state.load_flows()

        if count == 0:
            log.warning("No taint flows found!")
            return self.phase_report()

        while self.state.has_flows():
            flow = self.state.next_flow()
            if not flow:
                break

            flow_id = flow.get("id", f"flow-{self.state.index}")
            score_val = self.state.score(flow)
            log.info(f"\n>>> Processing: {flow_id} - Score: {score_val}")

            try:
                verdict = self.phase_analysis(flow)
                if verdict:
                    log.info(f"  Flow {flow_id}: confirmed")
                else:
                    log.info(f"  Flow {flow_id}: unconfirmed")
            except Exception as e:
                log.error(f"  Flow {flow_id} encountered error: {e}", exc_info=True)
                self.state.reject(reason=f"Pipeline exception: {e}")
                continue

        report = self.phase_report()

        elapsed = time.time() - start
        log.info(f"\nCompleted in {elapsed:.1f}s")

        return report


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Vulnalysis")
    parser.add_argument("repo", help="Path to target repository")
    parser.add_argument("--outputs", default="outputs", help="Path to outputs directory")
    parser.add_argument("--config", default=None, help="Path to config.yml")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic"], help="LLM provider")
    parser.add_argument("--skip-preprogress", "--skip-prep", dest="skip_preprogress", action="store_true", help="Skip preprocessing (repo-builder and repo-retriever) if already done")
    args = parser.parse_args()

    # Select provider
    if args.provider == "anthropic":
        from models.provider import Anthropic
        provider_cls = Anthropic
    else:
        from models.provider import OpenAI
        provider_cls = OpenAI

    orch = Orchestrator(
        repo_path=args.repo,
        outputs_path=args.outputs,
        config_path=args.config,
        provider_cls=provider_cls,
        skip_prep=args.skip_preprogress,
    )
    report = orch.run()

    confirmed = len(report.get("vulnerabilities", []))
    color = "\033[1;32m" if confirmed > 0 else "\033[1;33m"
    print(f"\n{color}[+] Done. Vulnerabilities: {confirmed}\033[0m")


if __name__ == "__main__":
    main()


__all__ = ["Orchestrator", "main"]