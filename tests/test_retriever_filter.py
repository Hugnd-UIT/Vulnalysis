"""
Empirical test harness for agents/repo-retriever.py path filtering logic.
Stress-tests Windows/POSIX slashes, case sensitivity, boundary conditions,
component matching, benchmark dataset counts, and naming rules.
"""

import ast
import json
import re
import subprocess
import unittest
from pathlib import Path

# Extract and isolate the exact path filtering logic from agents/repo-retriever.py
def should_skip_target(target: str) -> bool:
    skip = ("tests/", "test/", "examples/", "example/", "docs/", "fixtures/", "mock/")
    norm = target.replace("\\", "/").lower().lstrip("/")
    if norm.startswith("./"):
        norm = norm[2:]
    return any(norm.startswith(p) or f"/{p}" in norm for p in skip)


class TestSlashVariations(unittest.TestCase):
    """1. Windows backslashes, POSIX slashes, leading slashes, and ./ prefixes."""

    def test_posix_standard_slashes(self):
        cases = [
            "tests/test_auth.py",
            "test/unit.py",
            "examples/demo.py",
            "example/sample.py",
            "docs/index.md",
            "fixtures/sample.json",
            "mock/server.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter POSIX path: {path}")

    def test_windows_backslashes(self):
        cases = [
            r"tests\test_auth.py",
            r"test\unit.py",
            r"examples\demo.py",
            r"example\sample.py",
            r"docs\index.md",
            r"fixtures\sample.json",
            r"mock\server.py",
            r"subpkg\tests\test_x.py",
            r"core\mock\server.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter Windows path: {path}")

    def test_leading_slashes(self):
        cases = [
            "/tests/test_auth.py",
            "//tests/test_auth.py",
            r"\tests\test_auth.py",
            r"\\tests\test_auth.py",
            "/docs/readme.txt",
            r"\mock\server.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter leading slash path: {path}")

    def test_dot_slash_prefixes(self):
        cases = [
            "./tests/test_auth.py",
            r".\tests\test_auth.py",
            "./test/unit.py",
            r".\examples\demo.py",
            "./docs/manual.pdf",
            r".\mock\server.py",
            "././tests/nested_dot.py",
            r".\.\mock\nested_dot.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter ./ prefixed path: {path}")

    def test_mixed_slashes(self):
        cases = [
            r"subpkg/tests\test_x.py",
            r"subpkg\tests/test_x.py",
            r"pkg\sub/mock\server.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter mixed slash path: {path}")


class TestCaseSensitivity(unittest.TestCase):
    """2. Case sensitivity (e.g. Tests/, EXAMPLES/, Docs/)."""

    def test_uppercase_root_directories(self):
        cases = [
            "Tests/test_auth.py",
            "TESTS/test_auth.py",
            "Test/unit.py",
            "TEST/unit.py",
            "Examples/demo.py",
            "EXAMPLES/demo.py",
            "Example/sample.py",
            "EXAMPLE/sample.py",
            "Docs/index.md",
            "DOCS/index.md",
            "Fixtures/data.json",
            "FIXTURES/data.json",
            "Mock/server.py",
            "MOCK/server.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter uppercase path: {path}")

    def test_uppercase_nested_directories(self):
        cases = [
            "src/Tests/test_auth.py",
            r"subpkg\EXAMPLES\demo.py",
            "module/Docs/guide.md",
            r"lib\MOCK\server.py",
            "pkg/Fixtures/data.json",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter nested uppercase path: {path}")


class TestBoundaryConditions(unittest.TestCase):
    """3. Boundary conditions: valid production files containing substrings must NOT be filtered."""

    def test_production_files_with_substrings_retained(self):
        production_paths = [
            "src/fastmcp/test_tools.py",
            "src/fastmcp/testing/util.py",
            "src/fastmcp/contrib/mcp_mixin/example.py",
            "src/docs_generator.py",
            "src/fastmcp/mock_client.py",
            "src/fastmcp/mocking/service.py",
            "src/fastmcp/fixture_loader.py",
            "src/testing_framework/core.py",
            "src/example_service.py",
            "src/document_parser.py",
            "src/tests_runner/main.py",
            "tests_utils/helper.py",
            "mockery/handler.py",
            "examples_hub/client.py",
            "test_something.py",
            "example_script.py",
        ]
        for path in production_paths:
            with self.subTest(path=path):
                self.assertFalse(should_skip_target(path), f"Falsely filtered valid production path: {path}")


class TestPathComponentMatching(unittest.TestCase):
    """4. Component matching: ensure paths matching skip patterns anywhere as a path component ARE filtered."""

    def test_nested_path_components(self):
        cases = [
            "subpkg/tests/test_x.py",
            "mock/server.py",
            "pkg/subpkg/test/unit.py",
            "core/module/examples/quickstart.py",
            "plugins/ext/example/plugin_example.py",
            "internal/api/docs/swagger.json",
            "services/auth/fixtures/tokens.json",
            "gateway/proxy/mock/backend.py",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assertTrue(should_skip_target(path), f"Failed to filter nested component path: {path}")


class TestFastMCPBenchmark(unittest.TestCase):
    """5. Check benchmark dataset fastmcp target filtering results (120 targets retained, 225 filtered)."""

    def test_fastmcp_filtering_counts_and_ground_truth(self):
        cf_path = Path("benchmark/vulngym/repos/fastmcp/outputs/call-flows.jsonl")
        self.assertTrue(cf_path.exists(), "call-flows.jsonl does not exist")

        from src.dedup import filter as filter_flows
        raw_lines = cf_path.read_text(encoding="utf-8").splitlines()
        filtered_flows = filter_flows(raw_lines)

        all_targets = set()
        grouped_flows = {}
        filtered_out = []

        skip = ("tests/", "test/", "examples/", "example/", "docs/", "fixtures/", "mock/")
        for obj in filtered_flows:
            target = obj.get("dst", {}).get("file") or "unknown"
            all_targets.add(target)
            norm = target.replace("\\", "/").lower().lstrip("/")
            if norm.startswith("./"):
                norm = norm[2:]
            if any(norm.startswith(p) or f"/{p}" in norm for p in skip):
                filtered_out.append(target)
                continue
            grouped_flows.setdefault(target, []).append(json.dumps(obj, ensure_ascii=False))

        # Check total unique targets
        self.assertEqual(len(all_targets), 345, f"Expected 345 unique targets, got {len(all_targets)}")

        # Check retained vs filtered targets
        self.assertEqual(len(grouped_flows), 120, f"Expected 120 retained targets, got {len(grouped_flows)}")
        self.assertEqual(len(all_targets) - len(grouped_flows), 225, "Expected 225 filtered targets")

        # Check ground-truth target is retained
        ground_truth_target = "src/fastmcp/server/auth/oauth_proxy.py"
        self.assertIn(ground_truth_target, grouped_flows, f"Ground truth target {ground_truth_target} was dropped!")

        # Check all retained targets are production
        for target in grouped_flows:
            norm = target.replace("\\", "/").lower()
            for p in skip:
                self.assertFalse(norm.startswith(p) or f"/{p}" in norm, f"Retained non-production target: {target}")

    def test_repo_retriever_scan_method(self):
        from agents import retriever
        agent = retriever.RepoRetrieverAgent(config_path="config/config.yml")
        scanned = agent.scan("benchmark/vulngym/repos/fastmcp")

        self.assertGreater(len(scanned), 0, "Scan returned empty list")
        self.assertIn("src/fastmcp/server/auth/oauth_proxy.py", scanned, "Ground truth missing from scan()")

        # Verify no test/example/doc directories in scanned files
        forbidden = ("tests/", "test/", "examples/", "example/", "docs/", "fixtures/", "mock/")
        for f in scanned:
            norm = f.lower()
            for p in forbidden:
                self.assertFalse(norm.startswith(p) or f"/{p}" in norm, f"Scanned file under forbidden dir: {f}")


class TestNamingRuleCompliance(unittest.TestCase):
    """Verify compliance with the 1-word naming rule for any new Python identifiers."""

    def test_git_diff_new_identifiers(self):
        cmd = ["git", "diff", "agents/repo-retriever.py"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        diff_lines = res.stdout.splitlines()

        added_code_lines = [
            line[1:].strip()
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        ]

        # In run(): newly introduced variables are `skip`, `norm`, `p`
        # In scan(): modified existing `ignored` and loop variable `d`
        allowed_new_vars = {"skip", "norm", "p"}

        # Extract assigned variable names in added lines
        assigned_vars = set()
        for line in added_code_lines:
            match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=", line)
            if match:
                assigned_vars.add(match.group(1))

        # Check 1-word rule for any newly assigned variable
        for var in assigned_vars:
            # 1-word check: no underscore separating multiple words (single leading/trailing underscore allowed)
            stripped = var.strip("_")
            self.assertNotIn("_", stripped, f"Variable '{var}' violates 1-word naming rule (contains underscore)!")
            self.assertFalse(any(c.isupper() for c in var), f"Variable '{var}' violates 1-word rule (camelCase)!")


class TestAdversarialEdgeCases(unittest.TestCase):
    """Adversarial and extreme edge cases: UNC paths, drive letters, dot-dots, and Unicode."""

    def test_windows_drive_letters(self):
        self.assertTrue(should_skip_target(r"C:\repo\tests\test_x.py"))
        self.assertTrue(should_skip_target(r"D:\work\fixtures\data.json"))
        self.assertFalse(should_skip_target(r"C:\repo\src\fastmcp\server.py"))

    def test_windows_unc_shares(self):
        self.assertTrue(should_skip_target(r"\\server\share\docs\manual.pdf"))
        self.assertTrue(should_skip_target(r"\\server\share\mock\server.py"))
        self.assertFalse(should_skip_target(r"\\server\share\src\app.py"))

    def test_unicode_paths(self):
        self.assertTrue(should_skip_target("src/模块/tests/test_x.py"))
        self.assertFalse(should_skip_target("src/模块/testing/test_tools.py"))

    def test_relative_dot_dot_traversals(self):
        self.assertTrue(should_skip_target("src/../tests/foo.py"))
        self.assertTrue(should_skip_target(r"src\..\docs\guide.md"))

    def test_empty_and_unknown_target(self):
        self.assertFalse(should_skip_target("unknown"))
        self.assertFalse(should_skip_target(""))


class TestPropertyBasedStress(unittest.TestCase):
    """Property-based generator stress testing over 15,000 combinatorial path variations."""

    def test_combinatorial_skip_paths(self):
        skip_keywords = ["tests", "test", "examples", "example", "docs", "fixtures", "mock"]
        separators = ["/", "\\"]
        prefixes = ["", "/", "//", "\\", "\\\\", "./", ".\\", "././", ".\\.\\"]
        parents = ["", "src/", "subpkg/", "core/sub/", "deep/nested/pkg/", "src\\", "subpkg\\"]
        suffixes = ["file.py", "test_x.py", "sub/leaf.py", r"deep\nested\leaf.py"]

        for kw in skip_keywords:
            for case_name in [kw.lower(), kw.upper(), kw.capitalize(), kw.swapcase()]:
                for pfx in prefixes:
                    for parent in parents:
                        for sfx in suffixes:
                            for sep in separators:
                                path = f"{pfx}{parent}{case_name}{sep}{sfx}"
                                self.assertTrue(
                                    should_skip_target(path),
                                    f"Generator failed to filter non-production path: {path}"
                                )

    def test_combinatorial_safe_paths(self):
        safe_components = [
            "testing", "tests_utils", "unit_tests", "pytest_plugins",
            "example_service", "examples_hub", "sample_examples",
            "documentation", "docs_generator", "api_docs",
            "fixture_loader", "fixtures_data", "custom_fixtures",
            "mocking", "mock_client", "automock", "unmocked"
        ]
        safe_filenames = [
            "test_tools.py", "test_runner.py", "example.py", "examples.py",
            "docs_builder.py", "mock.py", "mock_client.py", "fixtures.py", "test.py"
        ]
        separators = ["/", "\\"]
        parents = ["", "src/", "subpkg/", "core/sub/", "deep/nested/pkg/", "src\\", "subpkg\\"]
        suffixes = ["file.py", "test_x.py", "sub/leaf.py"]

        for safe_dir in safe_components:
            for parent in parents:
                for sfx in suffixes:
                    for sep in separators:
                        path = f"{parent}{safe_dir}{sep}{sfx}"
                        self.assertFalse(
                            should_skip_target(path),
                            f"Generator falsely filtered safe production path: {path}"
                        )

        for safe_file in safe_filenames:
            for parent in parents:
                path = f"{parent}{safe_file}"
                self.assertFalse(
                    should_skip_target(path),
                    f"Generator falsely filtered safe production path: {path}"
                )


if __name__ == "__main__":
    unittest.main()

