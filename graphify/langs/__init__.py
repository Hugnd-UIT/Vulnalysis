from __future__ import annotations

from . import apex, bash, c, cpp, csharp, dart, elixir, erlang, go, json, julia, markdown, objc, ocaml, powershell, python, r, razor, ruby, rust, sln, solidity, sql, swift, terraform, zig


extractors = {
    'apex': apex.extract,
    'bash': bash.extract,
    'c': c.extract,
    'cpp': cpp.extract,
    'csharp': csharp.extract,
    'dart': dart.extract,
    'elixir': elixir.extract,
    'erlang': erlang.extract,
    'go': go.extract,
    'json': json.extract,
    'julia': julia.extract,
    'markdown': markdown.extract,
    'objc': objc.extract,
    'ocaml': ocaml.extract,
    'powershell': powershell.extract,
    'python': python.extract,
    'r': r.extract,
    'razor': razor.extract,
    'ruby': ruby.extract,
    'rust': rust.extract,
    'sln': sln.extract,
    'solidity': solidity.extract,
    'sql': sql.extract,
    'swift': swift.extract,
    'terraform': terraform.extract,
    'zig': zig.extract,
}