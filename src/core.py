import json
import logging
import time
import threading
from pathlib import Path

from src.state import PipelineState
from docker.sandbox import init as sandbox_init

from utils.logger import setup_logger

log = setup_logger()


class Orchestrator:
    def __init__(self, repo_path, outputs_path="outputs", config_path=None, provider_cls=None):
        self.repo = Path(repo_path).resolve()
        self.outputs_path = Path(outputs_path).resolve()
        self.config_path = config_path
        self.state = PipelineState(self.outputs_path)

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
        log.info(f"{self.container.name} [{self.container.status}]\n")
        return self.container

    # Phase 1
    def phase_prep(self):
        log.info("=== Preprocessing ===")

        try:
            import graphify
            graph_out = self.outputs_path / "call-graph.json"
            flows_out = self.outputs_path / "call-flows.jsonl"
            graphify.callgraph(str(self.repo), str(graph_out), flow=str(flows_out))
        except Exception as e:
            log.warning(f"Graphify generation skipped: {e}")

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
    def phase_pov(self, hypothesis):
        flow_id = hypothesis.get("id") or hypothesis.get("flow_id") or self.state.flow.get("id", f"flow-{self.state.index}")
        log.info("=== Dynamic analyzing ===")

        from agents import generator, verifier
        feedback = None

        self.state.flow_dir(flow_id)
        self.state.pov_round = 0
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
            gen_agent.run(
                self.repo_container,
                hypothesis,
                pov_script=pov_script,
                pov_result=result_file,
                feedback=feedback,
                harness=harness_data,
            )

            script_code = self.state.load_script(flow_id)
            pov_result = self.state.load_results(flow_id) or {}

            res_data = pov_result.get(f"res_{round_num}") or {
                "exit_code": pov_result.get("exit_code", 0),
                "output": pov_result.get("output", ""),
                "status": "EXECUTED",
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
                log.warning("Verifier produced no verdict")
                feedback = {"decision": "RETRY", "issues": ["No verdict produced"], "suggestions": ["Re-run test"]}
                continue

            last_verdict = verdict
            decision = verdict.get("decision", "").upper()
            if decision in ("CONFIRMED", "FAILED") or ("alignment" in verdict and "issues" not in verdict):
                log.info("Verifier produced report to Coordinator")
                return verdict

            # Retry with feedback
            log.info("Verifier produced feedback to PoV")
            feedback = verdict

        return last_verdict or {"decision": "FAILED", "reason": "PoV rounds exhausted"}

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
                    "title": taint_flow.get("title", f"Flow {flow_id}"),
                    "cwe": taint_flow.get("cwe") or ["CWE-UNKNOWN"],
                    "description": "Analyzer concluded investigation without formulating a vulnerability hypothesis",
                    "source": taint_flow.get("source", {}),
                    "sink": taint_flow.get("sink", {}),
                    "flow": {"chain": taint_flow.get("chain", [])},
                    "status": "UNCONFIRMED",
                }
                self.state.save_hypothesis(flow_id, candidate)
            self.state.set_candidate(candidate)

            feedback = coordinator_agent.run(
                self.repo_container,
                candidate,
                taint_flow,
                analyzer=candidate if round_num > 1 else None,
            )

            if not feedback or not isinstance(feedback, dict):
                log.warning("Coordinator produced no valid feedback")
                break

            decision = feedback.get("decision", "").upper()
            log.info(f"Coordinator: {decision}")

            # Revise
            if decision == "REVISE" or (decision == "REJECT" and round_num == 1):
                prev_feedback = feedback
                continue

            # Reject
            if decision == "REJECT":
                self.state.update_status(flow_id, "FAILED")
                self.state.reject(reason=json.dumps(feedback.get("issues", [])))
                return None

            approved = feedback.get("approved_hypothesis") or candidate
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
                coord_verdict = verdict or {"decision": "FAILED"}

            coord_decision = coord_verdict.get("decision", "").upper()
            if not coord_decision and isinstance(coord_verdict, dict):
                alignment = coord_verdict.get("alignment", {})
                if isinstance(alignment, dict) and alignment.get("location_match") and alignment.get("mechanism_match"):
                    coord_decision = "CONFIRMED"
                    coord_verdict["decision"] = "CONFIRMED"

            log.info(f"Coordinator: {coord_decision}")

            # Coordinator confirms
            if coord_decision in ("CONFIRMED", "APPROVE"):
                self.state.update_status(flow_id, "CONFIRMED")
                self.state.confirm(coord_verdict)
                return coord_verdict

            # Coordinator revision
            if coord_decision in ("REVISE", "REPLAN"):
                prev_feedback = coord_verdict
                continue

            # Coordinator rejects
            self.state.update_status(flow_id, "FAILED")
            self.state.reject(reason=coord_verdict.get("reason", "Coordinator rejected verdict"))
            return None

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
            sink = hyp.get("sink", {}) or {}
            source = hyp.get("source", {}) or {}
            vuln = {
                "id": flow_id,
                "cwe": hyp.get("cwe", "Unknown"),
                "title": hyp.get("title", hyp.get("cwe", "Vulnerability")),
                "description": hyp.get("description", ""),
                "source": source,
                "sink": sink,
                "flow": hyp.get("flow", {}),
                "hint": hyp.get("hint", ""),
                "expected": hyp.get("expected", ""),
                "oracle": hyp.get("oracle", {}) or {},
                "remediation": hyp.get("remediation", ""),
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
        log.info("=" * 60)
        log.info("VULNALYSIS")
        log.info("=" * 60)

        # Init sandbox
        self.init_sandbox()

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
    )
    report = orch.run()

    confirmed = len(report.get("vulnerabilities", []))
    color = "\033[1;32m" if confirmed > 0 else "\033[1;33m"
    print(f"\n{color}[+] Done. Vulnerabilities: {confirmed}\033[0m")


if __name__ == "__main__":
    main()


__all__ = ["Orchestrator", "main"]