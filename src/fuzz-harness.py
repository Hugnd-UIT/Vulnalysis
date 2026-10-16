import json
from pathlib import Path

EXTS = {
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".go", ".py", ".rs", ".java", ".cs",
    ".js", ".ts", ".rb", ".swift", ".zig"
}

SIGS = [
    ("LLVMFuzzerTestOneInput", "fuzz", "./{bin} {input}"),
    ("LLVMFuzzerInitialize", "fuzz", "./{bin} {input}"),
    ("__AFL_LOOP", "fuzz", "./{bin} < {input}"),
    ("atheris.instrument", "fuzz", "python {file} {input}"),
    ("FuzzedDataProvider", "fuzz", "python {file} {input}"),
    ("@hypothesis.given", "fuzz", "pytest {file}"),
    ("fuzz_target!", "fuzz", "cargo fuzz run {name}"),
    ("func Fuzz", "fuzz", "go test -fuzz={func}"),
    ("@FuzzTest", "fuzz", "mvn test"),
    ("SharpFuzz", "fuzz", "dotnet test"),
    ("TEST(", "test", "./test_runner"),
    ("TEST_F(", "test", "./test_runner"),
    ("def test_", "test", "pytest {file}"),
    ("unittest.TestCase", "test", "python -m unittest {file}"),
    ("func Test", "test", "go test -v {file}"),
    ("#[test]", "test", "cargo test"),
    ("@Test", "test", "mvn test"),
    ("[Test]", "test", "dotnet test"),
    ("[Fact]", "test", "dotnet test"),
    ("describe(", "test", "npm test"),
    ("it(", "test", "npm test"),
    ("XCTestCase", "test", "swift test"),
    ('test "', "test", "zig test {file}"),
]


def scan(repo):
    repo_path = Path(repo).resolve()
    items = []
    seen = set()

    if not repo_path.exists():
        return items

    for p in repo_path.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        parts = p.parts
        if any(d in parts for d in (".git", "node_modules", "venv", ".env", "dist", "build")):
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        rel_path = str(p.relative_to(repo_path)).replace("\\", "/")

        for sig, kind, cmd in SIGS:
            if sig in text:
                key = f"{rel_path}:{sig}"
                if key in seen:
                    continue
                seen.add(key)

                line_no = 1
                for idx, line in enumerate(text.splitlines(), 1):
                    if sig in line:
                        line_no = idx
                        break

                items.append({
                    "type": kind,
                    "file": rel_path,
                    "symbol": sig,
                    "line": line_no,
                    "command": cmd.format(file=rel_path, bin=p.stem, name=p.stem, func=sig, input="{payload}"),
                })

    return items


def check(repo, outputs=None):
    items = scan(repo)
    result = {
        "found": len(items) > 0,
        "count": len(items),
        "items": items,
    }

    if outputs:
        out_dir = Path(outputs)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "harness.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    return result


__all__ = ["scan", "check", "SIGS"]
