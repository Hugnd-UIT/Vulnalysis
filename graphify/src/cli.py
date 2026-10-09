from __future__ import annotations
import json
import os
import re
import sys
import time
from graphify.paths import GRAPHIFY_OUT as _GRAPHIFY_OUT
from pathlib import Path, PurePosixPath, PureWindowsPath
_SEARCH_NUDGE = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': 'MANDATORY: graphify-out/graph.json exists. You MUST run `graphify query "<question>"` before grepping raw files. Only grep after graphify has oriented you, or to modify/debug specific lines.'}}, ensure_ascii=False, separators=(',', ':')) + '\n'
_READ_NUDGE = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': 'MANDATORY: graphify-out/graph.json exists. You MUST run graphify before reading source files. Use: `graphify query "<question>"` (scoped subgraph), `graphify explain "<concept>"`, or `graphify path "<A>" "<B>"`. Only read raw files after graphify has oriented you, or to modify/debug specific lines. This rule applies to subagents too — include it in every subagent prompt involving code exploration.'}}, ensure_ascii=False, separators=(',', ':')) + '\n'
_READ_NUDGE_STALE = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': 'graphify-out/graph.json exists but may be STALE for this file (the file changed after the last build). Prefer `graphify query "<question>"` for orientation, and run `graphify update` to refresh the graph. Reading the file directly is fine.'}}, ensure_ascii=False, separators=(',', ':')) + '\n'
_READ_DENY = json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': 'graphify strict mode: this project has a fresh knowledge graph that covers this file. Run `graphify query "<your question>"` (or `graphify explain` / `graphify path`) FIRST to orient yourself, then re-issue this Read — it will be allowed. This block fires at most once per session; reading raw files to modify or debug specific lines is fine after one query. Apply the same rule in any subagent prompt that explores code.'}}, ensure_ascii=False, separators=(',', ':')) + '\n'
_HOOK_SOURCE_EXTS = ('.py', '.js', '.cjs', '.ts', '.tsx', '.jsx', '.astro', '.vue', '.svelte', '.go', '.rs', '.java', '.rb', '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.kt', '.swift', '.php', '.scala', '.lua', '.sh', '.md', '.rst', '.txt', '.mdx')
_GEMINI_NUDGE_TEXT = 'graphify: knowledge graph at graphify-out/. For focused questions, run `graphify query "<question>"` (scoped subgraph, usually much smaller than GRAPH_REPORT.md) instead of grepping raw files. Read GRAPH_REPORT.md only for broad architecture context.'
_DEFAULT_NUDGE_GRAPH = 'graphify-out/graph.json'


def nudge(nudge: str) -> str:
    from graphify.paths import out_path

    try:
        ref = out_path('graph.json').as_posix()
    except Exception:
        return nudge

    if ref == _DEFAULT_NUDGE_GRAPH:
        return nudge

    d = json.loads(nudge)
    out = d['hookSpecificOutput']
    out['additionalContext'] = out['additionalContext'].replace(_DEFAULT_NUDGE_GRAPH, ref)

    return json.dumps(d, ensure_ascii=False, separators=(',', ':')) + '\n'


def cloudcta(out_dir: 'Path') -> None:
    if os.environ.get('GRAPHIFY_NO_TIPS') or os.environ.get('GRAPHIFY_NO_CTA'):
        return

    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    marker = out_dir / '.cloud-cta-shown'

    try:
        if marker.exists():
            return
    except Exception:
        return
    print()
    print('  Graphify Cloud: faster indexing, fewer tokens, cross-repo search,')
    print('  and PR review, with your whole SDLC mapped and always current.')
    print('  Connect a repo at https://app.graphify.com')

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('1', encoding='utf-8')
    except Exception:
        pass


def defaultpath() -> str:
    return str(Path(_GRAPHIFY_OUT) / 'graph.json')


def stampedfiles(files_by_type: dict[str, list[str]], sem_result: dict, root: Path, partial_source_files: 'set[str] | None'=None, failed_ast_sources: 'set[str] | list[str] | None'=None, unverified_semantic_sources: 'set[str] | list[str] | None'=None) -> dict[str, list[str]]:
    root = Path(root)

    def resolve(value: str) -> Path:
        p = Path(value)

        if not p.is_absolute():
            p = root / p

        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return p

    sem_extracted: set[Path] = set()

    for coll in ('nodes', 'hyperedges'):
        for item in sem_result.get(coll, []):
            sf = item.get('source_file', '')

            if sf:
                sem_extracted.add(resolve(sf))
    partial_resolved = {resolve(p) for p in partial_source_files or set()}
    unverified_resolved = {resolve(p) for p in unverified_semantic_sources or set()}
    failed_ast_resolved = {resolve(p) for p in failed_ast_sources or []}
    sem_types = {'document', 'paper', 'image'}

    return {ftype: [f for f in flist if resolve(f) not in failed_ast_resolved and (ftype not in sem_types or (resolve(f) in sem_extracted and resolve(f) not in partial_resolved and (resolve(f) not in unverified_resolved)))] for ftype, flist in files_by_type.items()}


def handleshrink(unverified_shrink, *, cli_allow_partial: bool, files_by_type, sem_result, target, partial_semantic_files, failed_ast_sources, semantic_files):
    if not unverified_shrink:
        return None

    incomplete = False
    manifest_files = None
    cleared_semantic = None

    if not cli_allow_partial:
        incomplete = True
        unverified_sources = set(unverified_shrink.keys())
        manifest_files = stampedfiles(files_by_type, sem_result, target, partial_source_files=partial_semantic_files, failed_ast_sources=failed_ast_sources, unverified_semantic_sources=unverified_sources)
        stamped = {f for _flist in manifest_files.values() for f in _flist}
        cleared_semantic = {str(p) for p in semantic_files} - stamped
    details = ', '.join((f"'{sf}' ({prior} -> {fresh} nodes)" for sf, (prior, fresh) in sorted(unverified_shrink.items())))
    print(f'[graphify extract] semantic extraction is incomplete: unverified semantic shrink detected for {details}. The shrink guard stays armed for this write; pass --allow-partial to overwrite a larger existing graph anyway.', file=sys.stderr)

    return (incomplete, manifest_files, cleared_semantic)


def stalesources(graph_path: Path, scan_root: Path, seen_files: set[str], detection: dict | None=None) -> list[str]:
    from graphify.paths import nfc

    try:
        data = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    out_base = graph_path.parent.parent

    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass

    def withinroot(p: Path) -> bool:
        try:
            p.relative_to(root_res)

            return True
        except ValueError:
            pass

        try:
            p.resolve().relative_to(root_res)

            return True
        except (ValueError, OSError, RuntimeError):
            return False

    seen_nfc = {nfc(s) for s in seen_files}
    seen_basenames = {nfc(os.path.basename(s)) for s in seen_files}

    def inseen(p: Path) -> bool:
        if nfc(str(p)) in seen_nfc:
            return True

        try:
            return nfc(str(p.resolve())) in seen_nfc
        except (OSError, RuntimeError):
            return False

    excluded_exact: set[str] = set()
    excluded_prefixes: list[str] = []

    if detection:
        for entry in list(detection.get('ignored', [])) + list(detection.get('pruned_noise_dirs', [])):
            e = nfc(str(entry))

            if e.endswith(os.sep) or e.endswith('/'):
                excluded_prefixes.append(e)
            else:
                excluded_exact.add(e)

        for entry in detection.get('skipped_sensitive', []):
            excluded_exact.add(nfc(str(entry).split(' [', 1)[0]))

    def excluded(c: Path) -> bool:
        spellings = [nfc(str(c))]

        try:
            spellings.append(nfc(str(c.resolve())))
        except (OSError, RuntimeError):
            pass

        for s in spellings:
            if s in excluded_exact:
                return True

            if any((s.startswith(pref) for pref in excluded_prefixes)):
                return True

        try:
            from graphify.detect import sensitive

            if sensitive(c):
                return True
        except Exception:
            pass

        return False

    stale: list[str] = []
    kept_alive: list[str] = []
    checked: set[str] = set()

    for n in data.get('nodes', []):
        if not isinstance(n, dict):
            continue

        sf = n.get('source_file')

        if not sf or not isinstance(sf, str) or sf in checked:
            continue

        checked.add(sf)

        if '://' in sf:
            continue

        p = Path(sf)

        if p.is_absolute():
            candidates = [p]
        else:
            rel = sf.replace('\\', '/')
            bases = [root_res]

            if out_base != root_res:
                bases.append(out_base)
            candidates = [Path(os.path.normpath(str(base / rel))) for base in bases]
        in_root = [c for c in candidates if withinroot(c)]

        if not in_root:
            continue

        if any((inseen(c) for c in in_root)):
            continue

        alive = []

        for c in in_root:
            try:
                if c.exists():
                    alive.append(c)
            except OSError:
                pass

        if alive:
            if all((excluded(c) for c in alive)):
                stale.append(sf)
            else:
                kept_alive.append(sf)
            continue

        rel_sf = sf.replace('\\', '/')

        if '/' not in rel_sf and nfc(rel_sf) in seen_basenames:
            kept_alive.append(sf)
            continue

        stale.append(sf)

    if kept_alive:
        print(f'[graphify] fail-closed: kept node(s) from {len(kept_alive)} source file(s) that left the scan corpus but still exist on disk (ignore rules or filters changed?). Run a full re-extraction to purge them if the exclusion is intentional.', file=sys.stderr)

    return stale


def zerocode(graph_path: Path, scan_root: Path, unchanged_code: list[str]) -> list[str]:
    if not unchanged_code:
        return []

    from graphify.paths import nfc

    try:
        data = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    out_base = graph_path.parent.parent

    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass
    present: set[str] = set()

    for n in data.get('nodes', []):
        if not isinstance(n, dict):
            continue

        sf = n.get('source_file')

        if not sf or not isinstance(sf, str):
            continue

        present.add(nfc(sf))
        p = Path(sf)

        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace('\\', '/')

            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))
    from graphify.extract import extractor
    healed: list[str] = []

    for f in unchanged_code:
        p = Path(f)

        if extractor(p) is None:
            continue

        spellings = {nfc(str(p))}

        try:
            spellings.add(nfc(str(p.resolve())))
        except (OSError, RuntimeError):
            pass

        try:
            spellings.add(nfc(p.resolve().relative_to(root_res).as_posix()))
        except (ValueError, OSError, RuntimeError):
            pass

        if spellings & present:
            continue

        healed.append(f)

    return healed


def zerosemantic(graph_path: Path, scan_root: Path, unchanged_semantic: list[str]) -> list[str]:
    if not unchanged_semantic:
        return []

    from graphify.paths import nfc

    try:
        data = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    try:
        root_res = scan_root.resolve()
    except (OSError, RuntimeError):
        root_res = scan_root
    out_base = graph_path.parent.parent

    try:
        out_base = out_base.resolve()
    except (OSError, RuntimeError):
        pass
    present: set[str] = set()

    for n in data.get('nodes', []):
        if not isinstance(n, dict):
            continue

        sf = n.get('source_file')

        if not sf or not isinstance(sf, str):
            continue

        present.add(nfc(sf))
        p = Path(sf)

        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace('\\', '/')

            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))
    hyper_items = list(data.get('hyperedges', []) or [])

    if isinstance((data.get('graph') or {}).get('hyperedges'), list):
        hyper_items.extend(data['graph']['hyperedges'])

    for h in hyper_items:
        if not isinstance(h, dict):
            continue

        sf = h.get('source_file')

        if not sf or not isinstance(sf, str):
            continue

        present.add(nfc(sf))
        p = Path(sf)

        if p.is_absolute():
            try:
                present.add(nfc(str(p.resolve())))
            except (OSError, RuntimeError):
                pass
        else:
            rel = sf.replace('\\', '/')

            for base in (root_res, out_base):
                present.add(nfc(os.path.normpath(str(base / rel))))
    healed: list[str] = []

    for f in unchanged_semantic:
        p = Path(f)
        spellings = {nfc(str(p))}

        try:
            spellings.add(nfc(str(p.resolve())))
        except (OSError, RuntimeError):
            pass

        try:
            spellings.add(nfc(p.resolve().relative_to(root_res).as_posix()))
        except (ValueError, OSError, RuntimeError):
            pass

        if spellings & present:
            continue

        healed.append(f)

    return healed


def prunesources(graph_path: Path, stale_sources: list[str]) -> int:
    try:
        data = json.loads(graph_path.read_text(encoding='utf-8'))
    except Exception:
        return 0

    if not isinstance(data, dict):
        return 0

    stale = set(stale_sources)
    links_key = 'links' if 'links' in data else 'edges'
    nodes = [n for n in data.get('nodes', []) if isinstance(n, dict)]
    kept_nodes = [n for n in nodes if n.get('source_file') not in stale]
    removed_ids = {n.get('id') for n in nodes if n.get('source_file') in stale}
    n_removed = len(nodes) - len(kept_nodes)
    kept_edges = [e for e in data.get(links_key, []) if isinstance(e, dict) and e.get('source_file') not in stale and (e.get('source') not in removed_ids) and (e.get('target') not in removed_ids)]
    kept_hyper = [h for h in data.get('hyperedges', []) if isinstance(h, dict) and h.get('source_file') not in stale]

    if n_removed == 0 and len(kept_edges) == len(data.get(links_key, [])) and (len(kept_hyper) == len(data.get('hyperedges', []))):
        return 0

    data['nodes'] = kept_nodes
    data[links_key] = kept_edges

    if 'hyperedges' in data:
        data['hyperedges'] = kept_hyper
    from graphify.paths import backup
    backup(graph_path.parent)
    from graphify.paths import json as write_json
    write_json(graph_path, data, indent=2)

    return n_removed


class _StageTimer:

    def __init__(self, enabled: bool) -> None:
        import time
        self._now = time.perf_counter
        self.enabled = enabled
        self.start = self._now()
        self._last = self.start

    def mark(self, stage: str) -> None:
        now = self._now()

        if self.enabled:
            print(f'[graphify timing] {stage}: {now - self._last:.1f}s', file=sys.stderr)
        self._last = now

    def total(self) -> None:
        if self.enabled:
            print(f'[graphify timing] total: {self._now() - self.start:.1f}s', file=sys.stderr)


def enforcecap(gp: Path) -> None:
    from graphify.security import check

    try:
        check(gp)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        sys.exit(1)


def hookstrict(flag: bool) -> bool:
    v = os.environ.get('GRAPHIFY_HOOK_STRICT', '').strip().lower()

    if v in ('1', 'true', 'yes', 'on'):
        return True

    if v in ('0', 'false', 'no', 'off'):
        return False

    return flag


def touchstamp(graph_path: 'Path') -> None:
    try:
        from graphify.paths import text as write_text
        stamp = Path(graph_path).parent / 'cache' / 'last_query_stamp'
        stamp.parent.mkdir(parents=True, exist_ok=True)
        write_text(stamp, str(time.time()))
    except Exception:
        pass


def stampfresh() -> bool:
    from graphify.paths import out_path

    try:
        ttl = float(os.environ.get('GRAPHIFY_HOOK_STRICT_TTL', '1800'))

        return time.time() - out_path('cache', 'last_query_stamp').stat().st_mtime < ttl
    except Exception:
        return False


def markdenied(session_id: str) -> bool:
    from graphify.paths import out_path
    sid = re.sub('[^A-Za-z0-9_-]', '_', str(session_id))[:64]

    if not sid:
        return False

    try:
        d = out_path('cache', 'hook_sessions')
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / f'{sid}.denied'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 420)
        os.close(fd)

        try:
            cutoff = time.time() - 86400

            for entry in os.scandir(d):
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
        except OSError:
            pass

        return True
    except FileExistsError:
        return False
    except Exception:
        return False

_SEARCH_COMMANDS = frozenset({'grep', 'egrep', 'fgrep', 'zgrep', 'rg', 'ripgrep', 'find', 'fd', 'ack', 'ag'})
_COMMAND_WRAPPERS = frozenset({'sudo', 'command', 'exec', 'nohup', 'time', 'nice', 'ionice', 'env', 'xargs', 'timeout', 'stdbuf', 'doas'})
_HEREDOC_OPEN_RE = re.compile('<<-?\\s*([\'\\"]?)(\\w+)\\1')


def bashsearch(cmd_str: str) -> bool:
    text = cmd_str
    m = _HEREDOC_OPEN_RE.search(text)

    while m:
        nl_idx = text.find('\n', m.end())

        if nl_idx == -1:
            break

        term = re.compile('^\\s*' + re.escape(m.group(2)) + '\\s*$', re.MULTILINE)
        t = term.search(text, nl_idx + 1)

        if t is None:
            text = text[:nl_idx + 1]
            break

        text = text[:nl_idx + 1] + text[t.end():]
        m = _HEREDOC_OPEN_RE.search(text, nl_idx + 1)
    text = re.sub("'[^']*'", ' ', text)
    text = re.sub('"[^"]*"', ' ', text)

    for segment in re.split('[|;&\\n]|\\$\\(|`|\\(|\\)|\\{|\\}', text):
        tokens = segment.split()
        i = 0

        while i < len(tokens):
            tok = tokens[i]

            if '=' in tok.split('/')[-1] and (not tok.startswith(('-', '/'))):
                i += 1
                continue

            name = tok.replace('\\', '/').rsplit('/', 1)[-1].lower()
            name = name[:-4] if name.endswith('.exe') else name

            if name in _COMMAND_WRAPPERS:
                i += 1

                while i < len(tokens) and tokens[i].startswith('-'):
                    i += 1
                continue

            if name in _SEARCH_COMMANDS:
                return True

            if name == 'git' and any((t2 == 'grep' for t2 in tokens[i + 1:i + 4] if not t2.startswith('-'))):
                return True

            break

    return False


def hookguard(kind: str, strict: bool=False) -> None:
    from graphify.paths import out_path

    if kind == 'gemini':
        payload = {'decision': 'allow'}

        try:
            if out_path('graph.json').is_file():
                payload['additionalContext'] = _GEMINI_NUDGE_TEXT
        except Exception:
            pass
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        return

    try:
        d = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))
    except Exception:
        return

    if not isinstance(d, dict):
        return
    t = d.get('tool_input', d)

    if not isinstance(t, dict):
        return

    try:
        if kind == 'search':
            cmd_str = str(t.get('command', '') or '')
            is_grep_tool = not cmd_str and bool(t.get('pattern'))
            is_bash_search = bool(cmd_str) and bashsearch(cmd_str)

            if (is_grep_tool or is_bash_search) and out_path('graph.json').is_file():
                sys.stdout.write(nudge(_SEARCH_NUDGE))
        elif kind == 'read':
            vals = [str(t.get('file_path') or ''), str(t.get('pattern') or ''), str(t.get('path') or '')]
            tails = ['.' + seg.rsplit('.', 1)[-1] for v in vals if v for seg in [v.lower().replace('\\', '/').rsplit('/', 1)[-1]] if '.' in seg]

            if not any((tl in _HOOK_SOURCE_EXTS for tl in tails)):
                return
            root = Path(os.environ.get('CLAUDE_PROJECT_DIR') or os.getcwd())

            try:
                root = root.resolve()
            except (OSError, RuntimeError):
                pass
            out_dirs: list[Path] = []

            try:
                cfg_out = out_path()

                if isrelative(str(cfg_out)):
                    cfg_out = root / cfg_out
                out_dirs.append(cfg_out.resolve())
            except (OSError, RuntimeError):
                pass

            try:
                def_out = (root / 'graphify-out').resolve()

                if def_out not in out_dirs:
                    out_dirs.append(def_out)
            except (OSError, RuntimeError):
                pass

            def underout(target: str) -> bool:
                if not target:
                    return False

                try:
                    norm = target.replace('\\', '/')
                    p = Path(norm)

                    if isrelative(norm):
                        p = root / p
                    resolved = p.resolve()

                    for od in out_dirs:
                        try:
                            resolved.relative_to(od)

                            return True
                        except ValueError:
                            pass

                        try:
                            rel = os.path.relpath(str(resolved).lower(), str(od).lower())

                            if rel == '.' or (not rel.startswith('..' + os.sep) and rel != '..' and (not os.path.isabs(rel))):
                                return True
                        except (ValueError, OSError):
                            pass
                except (ValueError, OSError, RuntimeError):
                    pass

                return False

            out_candidates = [str(t.get('file_path') or ''), str(t.get('path') or ''), str(t.get('pattern') or '')]

            if t.get('path') and t.get('pattern'):
                out_candidates.append(f"{t.get('path')}/{t.get('pattern')}")

            if any((underout(c) for c in out_candidates if c)):
                return
            path_vals = [str(t.get('file_path') or ''), str(t.get('path') or '')]
            explicit = [v for v in path_vals if v]

            if explicit:
                in_project = False

                for v in explicit:
                    p = Path(v)

                    if isrelative(v):
                        in_project = True
                        break

                    try:
                        p.resolve().relative_to(root)
                        in_project = True
                        break
                    except (ValueError, OSError, RuntimeError):
                        continue

                if not in_project:
                    return

            try:
                gmtime = os.stat(str(out_path('graph.json'))).st_mtime
            except OSError:
                return
            stale = False
            fp = str(t.get('file_path') or '')

            if fp:
                try:
                    stale = os.stat(fp).st_mtime > gmtime
                except OSError:
                    stale = False

            try:
                if out_path('needs_update').exists():
                    stale = True
            except Exception:
                pass

            if stale:
                sys.stdout.write(nudge(_READ_NUDGE_STALE))
                return
            tool_name = d.get('tool_name')

            if hookstrict(strict) and tool_name in (None, 'Read') and (not stampfresh()) and isindexed(fp, root) and markdenied(str(d.get('session_id') or '')):
                sys.stdout.write(_READ_DENY)
                return
            sys.stdout.write(nudge(_READ_NUDGE))
    except Exception:
        pass


def isrelative(value: str) -> bool:
    pure = PureWindowsPath(value) if os.name == 'nt' else PurePosixPath(value)

    return not pure.root and (not pure.drive)


def isindexed(file_path: str, root: 'Path') -> bool:
    from graphify.paths import out_path

    if not file_path:
        return True

    try:
        mp = out_path('manifest.json')
        st = mp.stat()

        if st.st_size > 2000000:
            return True

        manifest = json.loads(mp.read_text(encoding='utf-8'))

        if not isinstance(manifest, dict) or not manifest:
            return True

        p = Path(file_path)
        rels = set()

        try:
            rels.add(p.resolve().relative_to(root).as_posix())
        except (ValueError, OSError, RuntimeError):
            pass
        rels.add(p.name)
        keys = {str(k).replace('\\', '/') for k in manifest}
        abskey = str(p).replace('\\', '/')

        return abskey in keys or any((r and (r in keys or any((k.endswith('/' + r) or k == r for k in keys))) for r in rels))
    except Exception:
        return True


def clonerepo(url: str, branch: str | None=None, out_dir: Path | None=None) -> Path:
    import subprocess
    import re
    url = url.rstrip('/')

    if not url.endswith('.git'):
        git_url = url + '.git'
    else:
        git_url = url
        url = url[:-4]
    m = re.search('github\\.com[:/]([^/]+)/([^/]+?)(?:\\.git)?$', url)

    if not m:
        print(f'error: not a recognised GitHub URL: {url}', file=sys.stderr)
        sys.exit(1)
    owner, repo = (m.group(1), m.group(2))

    if out_dir:
        dest = out_dir
    else:
        dest = Path.home() / '.graphify' / 'repos' / owner / repo

    if branch and branch.startswith('-'):
        print(f'error: invalid branch name: {branch!r}', file=sys.stderr)
        sys.exit(1)

    if dest.exists():
        print(f'Repo already cloned at {dest} - pulling latest...', flush=True)
        cmd = ['git', '-C', str(dest), 'pull']

        if branch:
            cmd += ['origin', '--', branch]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f'warning: git pull failed:\n{result.stderr}', file=sys.stderr)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f'Cloning {url} -> {dest} ...', flush=True)
        cmd = ['git', 'clone', '--depth', '1']

        if branch:
            cmd += ['--branch', branch]
        cmd += ['--', git_url, str(dest)]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f'error: git clone failed:\n{result.stderr}', file=sys.stderr)
            sys.exit(1)
    print(f'Ready at: {dest}', flush=True)

    return dest


def reenter() -> None:
    from graphify.__main__ import main
    main()


def dispatch(cmd: str) -> None:
    if cmd == 'provider':
        print('Provider management is disabled (pure static AST mode).')
        return
    elif cmd == 'prs':
        from graphify.prs import cmd_prs
        cmd_prs(sys.argv[2:])
    elif cmd == 'hook':
        from graphify.hooks import install as hook_install, uninstall as hook_uninstall, status as hook_status
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ''

        if subcmd == 'install':
            print(hook_install(Path('.')))
        elif subcmd == 'uninstall':
            print(hook_uninstall(Path('.')))
        elif subcmd == 'status':
            print(hook_status(Path('.')))
        else:
            print('Usage: graphify hook [install|uninstall|status]', file=sys.stderr)
            sys.exit(1)
    elif cmd == 'query':
        if len(sys.argv) < 3:
            print('Usage: graphify query "<question>" [--dfs] [--context C] [--budget N] [--graph path]', file=sys.stderr)
            sys.exit(1)
        from graphify.serve import _query_graph_text
        from graphify.security import label
        from networkx.readwrite import json_graph
        from graphify import querylog
        question = sys.argv[2]
        use_dfs = '--dfs' in sys.argv
        budget = 2000
        graph_path = defaultpath()
        context_filters: list[str] = []
        args = sys.argv[3:]
        i = 0

        while i < len(args):
            if args[i] == '--budget' and i + 1 < len(args):
                try:
                    budget = int(args[i + 1])
                except ValueError:
                    print(f'error: --budget must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith('--budget='):
                try:
                    budget = int(args[i].split('=', 1)[1])
                except ValueError:
                    print(f'error: --budget must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 1
            elif args[i] == '--context' and i + 1 < len(args):
                context_filters.append(args[i + 1])
                i += 2
            elif args[i].startswith('--context='):
                context_filters.append(args[i].split('=', 1)[1])
                i += 1
            elif args[i] == '--graph' and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            else:
                i += 1
        gp = Path(graph_path).resolve()

        if not gp.exists():
            print(f'error: graph file not found: {gp}', file=sys.stderr)
            sys.exit(1)

        if not gp.suffix == '.json':
            print(f'error: graph file must be a .json file', file=sys.stderr)
            sys.exit(1)
        enforcecap(gp)

        try:
            import json
            import networkx
            _raw = json.loads(gp.read_text(encoding='utf-8'))

            if 'links' not in _raw and 'edges' in _raw:
                _raw = dict(_raw, links=_raw['edges'])
            _raw = dict(_raw, links=[{**link, '_src': link.get('_src', link.get('source')), '_tgt': link.get('_tgt', link.get('target'))} for link in _raw.get('links', [])])

            try:
                G = json_graph.node_link_graph(_raw, edges='links')
            except TypeError:
                G = json_graph.node_link_graph(_raw)

            try:
                from graphify.build import graph_has_legacy_ids as _legacy

                if _legacy(_raw.get('nodes', [])):
                    print('[graphify] note: this graph uses the pre-#1504 node-ID scheme; rebuild with `graphify extract --force` to get path-qualified IDs (fixes same-name-file collisions).', file=sys.stderr)
            except Exception:
                pass
        except Exception as exc:
            print(f'error: could not load graph: {exc}', file=sys.stderr)
            sys.exit(1)
        import time
        _t0 = time.perf_counter()
        _mode = 'dfs' if use_dfs else 'bfs'
        _result = _query_graph_text(G, question, mode=_mode, depth=2, token_budget=budget, context_filters=context_filters, graph_path=str(gp))
        querylog.log_query(kind='query', question=question, corpus=str(gp), result=_result, mode=_mode, depth=2, token_budget=budget, duration_ms=(time.perf_counter() - _t0) * 1000)
        touchstamp(gp)
        print(_result)
    elif cmd == 'affected':
        if len(sys.argv) < 3:
            print('Usage: graphify affected "<node-or-label>" [--relation R] [--depth N] [--graph path]', file=sys.stderr)
            sys.exit(1)
        from graphify.affected import DEFAULT_AFFECTED_RELATIONS, format_affected, load_graph
        query = sys.argv[2]
        graph_path = defaultpath()
        depth = 2
        relations: list[str] = []
        args = sys.argv[3:]
        i = 0

        while i < len(args):
            if args[i] == '--graph' and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            elif args[i].startswith('--graph='):
                graph_path = args[i].split('=', 1)[1]
                i += 1
            elif args[i] == '--depth' and i + 1 < len(args):
                try:
                    depth = int(args[i + 1])
                except ValueError:
                    print('error: --depth must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith('--depth='):
                try:
                    depth = int(args[i].split('=', 1)[1])
                except ValueError:
                    print('error: --depth must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 1
            elif args[i] == '--relation' and i + 1 < len(args):
                relations.append(args[i + 1])
                i += 2
            elif args[i].startswith('--relation='):
                relations.append(args[i].split('=', 1)[1])
                i += 1
            else:
                i += 1
        gp = Path(graph_path).resolve()

        if not gp.exists():
            print(f'error: graph file not found: {gp}', file=sys.stderr)
            sys.exit(1)

        if not gp.suffix == '.json':
            print('error: graph file must be a .json file', file=sys.stderr)
            sys.exit(1)

        try:
            graph = load_graph(gp)
        except Exception as exc:
            print(f'error: could not load graph: {exc}', file=sys.stderr)
            sys.exit(1)
        from graphify.paths import GRAPHIFY_OUT_NAME
        graph_root = gp.parent.parent if gp.parent.name == GRAPHIFY_OUT_NAME else gp.parent
        print(format_affected(graph, query, relations=relations or DEFAULT_AFFECTED_RELATIONS, depth=depth, root=graph_root))
    elif cmd in ('god-nodes', 'god_nodes'):
        from graphify.affected import load_graph
        from graphify.analyze import god_nodes as _god_nodes
        from graphify.security import label
        graph_path = defaultpath()
        top_n = 10
        gn_exclude_hubs: float | None = None
        as_json = '--json' in sys.argv
        args = sys.argv[2:]
        i = 0

        while i < len(args):
            if args[i] == '--graph' and i + 1 < len(args):
                graph_path = args[i + 1]
                i += 2
            elif args[i].startswith('--graph='):
                graph_path = args[i].split('=', 1)[1]
                i += 1
            elif args[i] == '--top' and i + 1 < len(args):
                try:
                    top_n = int(args[i + 1])
                except ValueError:
                    print('error: --top must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith('--top='):
                try:
                    top_n = int(args[i].split('=', 1)[1])
                except ValueError:
                    print('error: --top must be an integer', file=sys.stderr)
                    sys.exit(1)
                i += 1
            elif args[i] == '--exclude-hubs' and i + 1 < len(args):
                try:
                    gn_exclude_hubs = float(args[i + 1])
                except ValueError:
                    print('error: --exclude-hubs must be a number (percentile 0-100)', file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif args[i].startswith('--exclude-hubs='):
                try:
                    gn_exclude_hubs = float(args[i].split('=', 1)[1])
                except ValueError:
                    print('error: --exclude-hubs must be a number (percentile 0-100)', file=sys.stderr)
                    sys.exit(1)
                i += 1
            else:
                i += 1
        gp = Path(graph_path).resolve()

        if not gp.exists():
            print(f'error: graph file not found: {gp}', file=sys.stderr)
            sys.exit(1)

        if not gp.suffix == '.json':
            print('error: graph file must be a .json file', file=sys.stderr)
            sys.exit(1)

        try:
            G = load_graph(gp)
        except Exception as exc:
            print(f'error: could not load graph: {exc}', file=sys.stderr)
            sys.exit(1)
        gods = _god_nodes(G, top_n=top_n, exclude_hubs_percentile=gn_exclude_hubs)

        if as_json:
            print(json.dumps(gods, indent=2))
        else:
            print('God nodes (most connected):')

            for rank, n in enumerate(gods, 1):
                print(f"  {rank}. {label(str(n['label']))} - {n['degree']} edges")
    elif cmd == 'save-result':
        import argparse
        p = argparse.ArgumentParser(prog='graphify save-result')
        p.add_argument('--question', required=True)
        p.add_argument('--answer', default=None)
        p.add_argument('--answer-file', dest='answer_file', default=None)
        p.add_argument('--type', dest='query_type', default='query')
        p.add_argument('--nodes', nargs='*', default=[])
        p.add_argument('--outcome', choices=('useful', 'dead_end', 'corrected'), default=None)
        p.add_argument('--correction', default=None)
        p.add_argument('--memory-dir', default=str(Path(_GRAPHIFY_OUT) / 'memory'))
        opts = p.parse_args(sys.argv[2:])

        if opts.answer_file:
            opts.answer = Path(opts.answer_file).read_text(encoding='utf-8').strip()
        elif not opts.answer:
            p.error('--answer or --answer-file is required')
        from graphify.ingest import save_query_result as _sqr
        out = _sqr(question=opts.question, answer=opts.answer, memory_dir=Path(opts.memory_dir), query_type=opts.query_type, source_nodes=opts.nodes or None, outcome=opts.outcome, correction=opts.correction)
        print(f'Saved to {out}')
    elif cmd == 'reflect':
        import argparse
        p = argparse.ArgumentParser(prog='graphify reflect')
        p.add_argument('--memory-dir', default=str(Path(_GRAPHIFY_OUT) / 'memory'))
        p.add_argument('--out', default=str(Path(_GRAPHIFY_OUT) / 'reflections' / 'LESSONS.md'))
        p.add_argument('--graph', default=None)
        p.add_argument('--analysis', default=None)
        p.add_argument('--labels', default=None)
        p.add_argument('--half-life-days', type=float, default=30.0, help='signal weight halves every N days (default 30)')
        p.add_argument('--min-corroboration', type=int, default=2, help='distinct useful results to promote a node to preferred (default 2)')
        p.add_argument('--if-stale', action='store_true', help='skip when LESSONS.md is already newer than every input (e.g. the git hook just refreshed it)')
        opts = p.parse_args(sys.argv[2:])
        from graphify.reflect import reflect as _reflect, lessons_fresh as _lessons_fresh
        graph_arg = opts.graph

        if graph_arg is None:
            default_graph = Path(_GRAPHIFY_OUT) / 'graph.json'

            if default_graph.exists():
                graph_arg = str(default_graph)
        _gp = Path(graph_arg) if graph_arg else None
        _analysis_path = None
        _labels_path = None

        if _gp is not None:
            _analysis_path = Path(opts.analysis) if opts.analysis else _gp.parent / '.graphify_analysis.json'
            _labels_path = Path(opts.labels) if opts.labels else _gp.parent / '.graphify_labels.json'

        if opts.if_stale and _lessons_fresh(Path(opts.out), Path(opts.memory_dir), _gp, _analysis_path, _labels_path):
            print(f'Lessons already up to date -> {opts.out} (skipped; omit --if-stale to force)')
        else:
            out_path, agg = _reflect(memory_dir=Path(opts.memory_dir), out_path=Path(opts.out), graph_path=_gp, analysis_path=_analysis_path, labels_path=_labels_path, half_life_days=opts.half_life_days, min_corroboration=opts.min_corroboration)
            c = agg['counts']
            print(f"Reflected {agg['total']} memories ({c['useful']} useful, {c['dead_end']} dead ends, {c['corrected']} corrected) -> {out_path}")
    elif cmd == 'path':
        if len(sys.argv) < 4:
            print('Usage: graphify path "<source>" "<target>" [--graph path] [--directed|--undirected]', file=sys.stderr)
            sys.exit(1)
        from graphify.serve import _resolve_path_endpoint
        from networkx.readwrite import json_graph
        import networkx
        source_label = sys.argv[2]
        target_label = sys.argv[3]
        graph_path = defaultpath()
        args = sys.argv[4:]
        direction_flag = None

        for i, a in enumerate(args):
            if a == '--graph' and i + 1 < len(args):
                graph_path = args[i + 1]
            elif a == '--directed':
                if direction_flag == 'undirected':
                    print('error: --directed and --undirected are mutually exclusive', file=sys.stderr)
                    sys.exit(1)
                direction_flag = 'directed'
            elif a == '--undirected':
                if direction_flag == 'directed':
                    print('error: --directed and --undirected are mutually exclusive', file=sys.stderr)
                    sys.exit(1)
                direction_flag = 'undirected'
        undirected = direction_flag == 'undirected'
        gp = Path(graph_path).resolve()

        if not gp.exists():
            print(f'error: graph file not found: {gp}', file=sys.stderr)
            sys.exit(1)
        enforcecap(gp)
        _raw = json.loads(gp.read_text(encoding='utf-8'))

        if 'links' not in _raw and 'edges' in _raw:
            _raw = dict(_raw, links=_raw['edges'])
        _raw = {**_raw, 'directed': True, 'multigraph': True}

        try:
            G = json_graph.node_link_graph(_raw, edges='links')
        except TypeError:
            G = json_graph.node_link_graph(_raw)
        src_nid, src_scored, src_err = _resolve_path_endpoint(G, source_label)
        tgt_nid, tgt_scored, tgt_err = _resolve_path_endpoint(G, target_label)

        for _label, _nid, _err in ((source_label, src_nid, src_err), (target_label, tgt_nid, tgt_err)):
            if _err or _nid is None:
                print(_err or f"No node matching '{_label}' found.", file=sys.stderr)
                sys.exit(1)

        if src_nid == tgt_nid:
            print(f"'{source_label}' and '{target_label}' both resolved to the same node '{src_nid}'. Use a more specific label or the exact node ID.", file=sys.stderr)
            sys.exit(1)

        for _name, _scored, _nid in (('source', src_scored, src_nid), ('target', tgt_scored, tgt_nid)):
            if len(_scored) >= 2 and _nid == _scored[0][1]:
                _top, _runner = (_scored[0][0], _scored[1][0])

                if _top > 0 and (_top - _runner) / _top < 0.1:
                    print(f'warning: {_name} match was ambiguous (top score {_top:g}, runner-up {_runner:g})', file=sys.stderr)

        try:
            if undirected:
                _und = networkx.Graph()
                _und.add_nodes_from(sorted(G.nodes))
                _und.add_edges_from(sorted(((min(u, v), max(u, v)) for u, v in G.edges())))
                path_nodes = networkx.shortest_path(_und, src_nid, tgt_nid)
            else:
                _dg = networkx.DiGraph()
                _dg.add_nodes_from(sorted(G.nodes))
                _dg.add_edges_from(sorted(((d.get('_src', u), d.get('_tgt', v)) for u, v, d in G.edges(data=True))))
                _dg.add_edges_from(sorted(((d.get('_tgt', v), d.get('_src', u)) for u, v, d in G.edges(data=True) if d.get('relation') == 'contains')))
                path_nodes = networkx.shortest_path(_dg, src_nid, tgt_nid)
        except (networkx.NetworkXNoPath, networkx.NodeNotFound):
            if undirected:
                print(f"No path found between '{source_label}' and '{target_label}'.")
            else:
                print(f"No directed path found between '{source_label}' and '{target_label}'. Re-run with --undirected to search ignoring edge direction.")
            sys.exit(0)
        hops = len(path_nodes) - 1
        segments = []
        from graphify.build import edge_datas

        for i in range(len(path_nodes) - 1):
            u, v = (path_nodes[i], path_nodes[i + 1])
            fwd, bwd = ([], [])

            for a, b in ((u, v), (v, u)):
                if G.has_edge(a, b):
                    for d in edge_datas(G, a, b):
                        (fwd if d.get('_src', a) == u else bwd).append(d)
            datas = fwd or bwd
            forward = bool(fwd)
            rels = sorted({d.get('relation') for d in datas if d.get('relation')})
            if forward:
                segments.append(f"--{rel}--> {G.nodes[v].get('label', v)}")
            else:
                segments.append(f"<--{rel}-- {G.nodes[v].get('label', v)}")
        print(f'Shortest path ({hops} hops):\n  ' + ' '.join(segments))
        from graphify import querylog
        querylog.log_query(kind='path', question=f'{sys.argv[2]} -> {sys.argv[3]}', corpus=str(gp), nodes_returned=hops)
        touchstamp(gp)
    elif cmd == 'explain':
        if len(sys.argv) < 3:
            print('Usage: graphify explain "<node>" [--graph path]', file=sys.stderr)
            sys.exit(1)
        from graphify.serve import _ambiguity_message, _find_node, find_node_ambiguity
        from networkx.readwrite import json_graph
        label = sys.argv[2]
        graph_path = defaultpath()
        args = sys.argv[3:]

        for i, a in enumerate(args):
            if a == '--graph' and i + 1 < len(args):
                graph_path = args[i + 1]
        gp = Path(graph_path).resolve()

        if not gp.exists():
            print(f'error: graph file not found: {gp}', file=sys.stderr)
            sys.exit(1)
        enforcecap(gp)
        _raw = json.loads(gp.read_text(encoding='utf-8'))

        if 'links' not in _raw and 'edges' in _raw:
            _raw = dict(_raw, links=_raw['edges'])
        _raw = {**_raw, 'directed': True}

        try:
            G = json_graph.node_link_graph(_raw, edges='links')
        except TypeError:
            G = json_graph.node_link_graph(_raw)
        matches = _find_node(G, label)

        if not matches:
            print(f"No node matching '{label}' found.")
            sys.exit(0)
        rivals = find_node_ambiguity(G, label)

        if rivals:
            print(_ambiguity_message(G, label, rivals))
            sys.exit(1)
        nid = matches[0]
        d = G.nodes[nid]
        print(f"Node: {d.get('label', nid)}")
        print(f'  ID:        {nid}')
        print(f"  Source:    {d.get('source_file', '')} {d.get('source_location', '')}".rstrip())
        print(f"  Type:      {d.get('file_type', '')}")
        print(f"  Community: {d.get('community_name') or d.get('community', '')}")

        try:
            from graphify.reflect import load_learning_overlay as _llo
            from graphify.security import label
            _overlay = _llo(gp)
            _entry = _overlay.get(str(nid))

            if _entry:
                _status = label(str(_entry.get('status', '')))

                if _status == 'contested':
                    _line = f"  Lesson: contested (useful {_entry.get('uses', 0)} / dead-end {_entry.get('neg', 0)})"
                elif _status == 'preferred':
                    _line = f"  Lesson: preferred source (start here) — {_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}"
                else:
                    _line = f"  Lesson: {_status or 'tentative'} — {_entry.get('uses', 0)} useful, score={_entry.get('score', 0)}"

                if _entry.get('stale'):
                    _line += ' [code changed since — re-verify]'
                print(_line)
        except Exception:
            pass
        print(f'  Degree:    {G.degree(nid)}')
        from graphify.build import edge_data
        connections: list[tuple[str, str, dict]] = []

        for nb in G.successors(nid):
            _ed = edge_data(G, nid, nb)
            connections.append(('out' if _ed.get('_src', nid) == nid else 'in', nb, _ed))

        for nb in G.predecessors(nid):
            _ed = edge_data(G, nb, nid)
            connections.append(('in' if _ed.get('_src', nb) == nb else 'out', nb, _ed))

        if connections:
            print(f'\nConnections ({len(connections)}):')
            for direction, nb, edata in connections[:20]:
                rel = edata.get('relation', '')
                arrow = '-->' if direction == 'out' else '<--'
                loc = edata.get('source_location') or ''
                sfile = edata.get('source_file') or ''
                at = f' {sfile}:{loc}' if loc else ''
                print(f"  {arrow} {G.nodes[nb].get('label', nb)} [{rel}]{at}")

            if len(connections) > 20:
                remainder = connections[20:]
                print(f'  ... and {len(remainder)} more')
                by_file: dict[tuple[str, str], int] = {}

                for direction, _nb, edata in remainder:
                    sfile = edata.get('source_file') or '(unknown file)'
                    key = (direction, sfile)
                    by_file[key] = by_file.get(key, 0) + 1
                grouped = sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0]))
                print('  Grouped by file:')

                for (direction, sfile), count in grouped[:20]:
                    arrow = '-->' if direction == 'out' else '<--'
                    noun = 'connection' if count == 1 else 'connections'
                    print(f'    {arrow} {sfile}: {count} {noun}')

                if len(grouped) > 20:
                    print(f'    ... and {len(grouped) - 20} more files')
        from graphify import querylog
        querylog.log_query(kind='explain', question=sys.argv[2], corpus=str(gp), nodes_returned=len(connections))
        touchstamp(gp)
    elif cmd == 'diagnose':
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ''

        if subcmd != 'multigraph':
            print('Usage: graphify diagnose multigraph [--graph path] [--json] [--max-examples N] [--directed] [--undirected] [--extract-path path]', file=sys.stderr)
            sys.exit(1)
        graph_path = Path(defaultpath())
        max_examples = 5
        directed: bool | None = None
        direction_flag: str | None = None
        json_output = False
        extract_path: Path | None = None
        i = 3

        while i < len(sys.argv):
            arg = sys.argv[i]

            if arg == '--graph':
                i += 1

                if i >= len(sys.argv):
                    print('error: --graph requires a path', file=sys.stderr)
                    sys.exit(1)
                graph_path = Path(sys.argv[i])
            elif arg == '--json':
                json_output = True
            elif arg == '--max-examples':
                i += 1

                if i >= len(sys.argv):
                    print('error: --max-examples requires an integer', file=sys.stderr)
                    sys.exit(1)

                try:
                    max_examples = int(sys.argv[i])
                except ValueError:
                    print('error: --max-examples requires an integer', file=sys.stderr)
                    sys.exit(1)

                if max_examples < 0:
                    print('error: --max-examples must be >= 0', file=sys.stderr)
                    sys.exit(1)
            elif arg == '--directed':
                if direction_flag == 'undirected':
                    print('error: --directed and --undirected are mutually exclusive', file=sys.stderr)
                    sys.exit(1)
                direction_flag = 'directed'
                directed = True
            elif arg == '--undirected':
                if direction_flag == 'directed':
                    print('error: --directed and --undirected are mutually exclusive', file=sys.stderr)
                    sys.exit(1)
                direction_flag = 'undirected'
                directed = False
            elif arg == '--extract-path':
                i += 1

                if i >= len(sys.argv):
                    print('error: --extract-path requires a path', file=sys.stderr)
                    sys.exit(1)
                extract_path = Path(sys.argv[i])
            else:
                print(f'error: unknown diagnose option {arg}', file=sys.stderr)
                sys.exit(1)
            i += 1
        from graphify.diagnostics import diagnose_file, format_diagnostic_json, format_diagnostic_report

        try:
            summary = diagnose_file(graph_path, directed=directed, root=Path('.').resolve(), max_examples=max_examples, extract_path=extract_path)
        except Exception as exc:
            print(f'error: {exc}', file=sys.stderr)
            sys.exit(1)

        if json_output:
            print(json.dumps(format_diagnostic_json(summary), indent=2))
        else:
            print(format_diagnostic_report(summary))
    elif cmd == 'add':
        if len(sys.argv) < 3:
            print('Usage: graphify add <url> [--author Name] [--contributor Name] [--dir ./raw]', file=sys.stderr)
            sys.exit(1)
        from graphify.ingest import ingest as _ingest
        url = sys.argv[2]
        author: str | None = None
        contributor: str | None = None
        target_dir = Path('raw')
        args = sys.argv[3:]
        i = 0

        while i < len(args):
            if args[i] == '--author' and i + 1 < len(args):
                author = args[i + 1]
                i += 2
            elif args[i] == '--contributor' and i + 1 < len(args):
                contributor = args[i + 1]
                i += 2
            elif args[i] == '--dir' and i + 1 < len(args):
                target_dir = Path(args[i + 1])
                i += 2
            else:
                i += 1

        try:
            saved = _ingest(url, target_dir, author=author, contributor=contributor)
            print(f'Saved to {saved}')
            print('Run /graphify --update in your AI assistant to update the graph.')
        except Exception as exc:
            print(f'error: {exc}', file=sys.stderr)
            sys.exit(1)
    elif cmd == 'watch':
        watch_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('.')

        if not watch_path.exists():
            print(f'error: path not found: {watch_path}', file=sys.stderr)
            sys.exit(1)
        from graphify.watch import watch as _watch

        try:
            _watch(watch_path)
        except ImportError as exc:
            print(f'error: {exc}', file=sys.stderr)
            sys.exit(1)
    elif cmd in ('cluster-only', 'label'):
        force_relabel = cmd == 'label'
        no_viz = '--no-viz' in sys.argv
        no_label = '--no-label' in sys.argv
        missing_only = '--missing-only' in sys.argv
        co_timing = '--timing' in sys.argv
        _backend_arg = next((a for a in sys.argv if a.startswith('--backend=')), None)
        label_backend = _backend_arg.split('=', 1)[1] if _backend_arg else None
        _model_arg = next((a for a in sys.argv if a.startswith('--model=')), None)
        label_model = _model_arg.split('=', 1)[1] if _model_arg else None
        _min_cs_arg = next((a for a in sys.argv if a.startswith('--min-community-size=')), None)
        min_community_size = int(_min_cs_arg.split('=')[1]) if _min_cs_arg else 3
        args = sys.argv[2:]
        watch_path: Path | None = None
        graph_override: Path | None = None
        co_resolution: float = 1.0
        co_exclude_hubs: float | None = None
        label_max_concurrency: int = 4
        label_batch_size: int = 100
        label_max_concurrency_explicit = False
        label_batch_size_explicit = False
        i_arg = 0

        while i_arg < len(args):
            a = args[i_arg]

            if a == '--graph' and i_arg + 1 < len(args):
                graph_override = Path(args[i_arg + 1])
                i_arg += 2
            elif a == '--backend' and i_arg + 1 < len(args):
                label_backend = args[i_arg + 1]
                i_arg += 2
            elif a.startswith('--backend='):
                label_backend = a.split('=', 1)[1]
                i_arg += 1
            elif a == '--model' and i_arg + 1 < len(args):
                label_model = args[i_arg + 1]
                i_arg += 2
            elif a.startswith('--model='):
                label_model = a.split('=', 1)[1]
                i_arg += 1
            elif a == '--resolution' and i_arg + 1 < len(args):
                co_resolution = float(args[i_arg + 1])
                i_arg += 2
            elif a.startswith('--resolution='):
                co_resolution = float(a.split('=', 1)[1])
                i_arg += 1
            elif a == '--exclude-hubs' and i_arg + 1 < len(args):
                co_exclude_hubs = float(args[i_arg + 1])
                i_arg += 2
            elif a.startswith('--exclude-hubs='):
                co_exclude_hubs = float(a.split('=', 1)[1])
                i_arg += 1
            elif a == '--max-concurrency' and i_arg + 1 < len(args):
                label_max_concurrency = int(args[i_arg + 1])
                label_max_concurrency_explicit = True
                i_arg += 2
            elif a.startswith('--max-concurrency='):
                label_max_concurrency = int(a.split('=', 1)[1])
                label_max_concurrency_explicit = True
                i_arg += 1
            elif a == '--batch-size' and i_arg + 1 < len(args):
                label_batch_size = int(args[i_arg + 1])
                label_batch_size_explicit = True
                i_arg += 2
            elif a.startswith('--batch-size='):
                label_batch_size = int(a.split('=', 1)[1])
                label_batch_size_explicit = True
                i_arg += 1
            elif a in ('--no-viz', '--missing-only') or a.startswith('--min-community-size='):
                i_arg += 1
            elif a.startswith('--'):
                i_arg += 1
            elif watch_path is None:
                watch_path = Path(a)
                i_arg += 1
            else:
                i_arg += 1

        if watch_path is None:
            watch_path = Path('.')
        graph_json = graph_override if graph_override is not None else watch_path / _GRAPHIFY_OUT / 'graph.json'

        if not graph_json.exists():
            print(f'error: no graph found at {graph_json} — run /graphify first', file=sys.stderr)
            sys.exit(1)
        from networkx.readwrite import json_graph
        from graphify.build import buildjson
        from graphify.cluster import cluster, score_all, remap_communities_to_previous
        from graphify.analyze import god_nodes, surprising_connections, suggest_questions
        from graphify.report import generate
        from graphify.export import to_json, to_html
        stages = _StageTimer(co_timing)
        print('Loading existing graph...')
        from graphify.security import check

        try:
            check(graph_json)
        except ValueError:
            try:
                _over_cap_bytes = graph_json.stat().st_size
            except OSError:
                _over_cap_bytes = -1
            print(f'warning: graph.json exceeds cap ({_over_cap_bytes} bytes); continuing with best-effort visualization', file=sys.stderr)
        _raw = json.loads(graph_json.read_text(encoding='utf-8'))
        _directed = bool(_raw.get('directed', False))
        G = buildjson(_raw, directed=_directed)
        print(f'Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges')
        stages.mark('load')
        print('Re-clustering...')
        communities = cluster(G, resolution=co_resolution, exclude_hubs_percentile=co_exclude_hubs)
        previous_node_community = {n['id']: n['community'] for n in _raw.get('nodes', []) if n.get('community') is not None and n.get('id') is not None}

        if previous_node_community:
            communities = remap_communities_to_previous(communities, previous_node_community)
        stages.mark('cluster')
        cohesion = score_all(G, communities)
        gods = god_nodes(G, exclude_hubs_percentile=co_exclude_hubs)
        surprises = surprising_connections(G, communities)
        stages.mark('analyze')
        _out_name = Path(_GRAPHIFY_OUT).name

        if graph_override is not None and graph_json.parent.name == _out_name:
            out = graph_json.parent
        else:
            out = watch_path / _GRAPHIFY_OUT
        out.mkdir(parents=True, exist_ok=True)
        labels_path = out / '.graphify_labels.json'
        existing_labels: dict[int, str] = {}

        if labels_path.exists():
            try:
                existing_labels = {int(k): v for k, v in json.loads(labels_path.read_text(encoding='utf-8')).items() if isinstance(v, str)}
            except Exception:
                existing_labels = {}
        label_token_usage = {'input': 0, 'output': 0}
        placeholder_only = False

        if labels_path.exists() and (not force_relabel):
            _ignored_label_flags = [flag for flag, given in (('--backend', label_backend is not None), ('--model', label_model is not None), ('--batch-size', label_batch_size_explicit), ('--max-concurrency', label_max_concurrency_explicit)) if given]

            if _ignored_label_flags:
                print(f"[graphify] warning: {'/'.join(_ignored_label_flags)} ignored: reusing saved labels at {labels_path}. Run `graphify label` to relabel, or delete the labels file.", file=sys.stderr)
            from graphify.cluster import community_member_sigs, label_communities_by_hub
            sig_path = labels_path.parent / (labels_path.name + '.sig')
            saved_sigs: dict[int, str] = {}

            if sig_path.exists():
                try:
                    saved_sigs = {int(k): v for k, v in json.loads(sig_path.read_text(encoding='utf-8')).items() if isinstance(v, str)}
                except Exception:
                    saved_sigs = {}
            cur_sigs = community_member_sigs(communities)
            count_mismatch = len(existing_labels) != len(communities)
            labels = {}
            hub_labels: dict[int, str] | None = None
            changed = 0

            for cid in communities:
                have_label = cid in existing_labels and existing_labels[cid] != f'Community {cid}'

                if saved_sigs:
                    fresh = have_label and saved_sigs.get(cid) == cur_sigs.get(cid)
                else:
                    fresh = have_label and (not count_mismatch)

                if fresh:
                    labels[cid] = existing_labels[cid]
                else:
                    if hub_labels is None:
                        hub_labels = label_communities_by_hub(G, communities)
                    labels[cid] = hub_labels[cid]

                    if have_label:
                        changed += 1

            if changed:
                print(f'[graphify] community set changed since labeling ({len(existing_labels)} saved labels, {len(communities)} communities now; renamed {changed} community(ies) by their hub). Run `graphify label` to refresh names with the LLM.', file=sys.stderr)
        elif no_label and (not force_relabel):
            labels = {cid: f'Community {cid}' for cid in communities}
            placeholder_only = True
        else:
            from graphify.cluster import label_communities_by_hub
            print('Labeling communities...')
            labels = label_communities_by_hub(G, communities)
        stages.mark('label')
        questions = suggest_questions(G, communities, labels)
        _commit = _raw.get('built_at_commit')

        if not _commit:
            from graphify.watch import _git_head as _gh
            _commit = _gh(cwd=watch_path)
        from graphify.paths import backup
        from graphify.exporters.html import _HTML_STALE_MARKER
        backup(out)
        html_stale_marker = out / _HTML_STALE_MARKER

        def clearmarker() -> None:
            try:
                html_stale_marker.unlink(missing_ok=True)
            except OSError as exc:
                print(f'warning: graph.html stale marker could not be cleared; regeneration may be retried: {exc}', file=sys.stderr)
        stale_marker_preexisted = html_stale_marker.exists()
        html_stale_marker.touch()

        if not to_json(G, communities, str(out / 'graph.json'), community_labels=labels, built_at_commit=_commit, original_links=_raw.get('links') or _raw.get('edges') or []):
            if not stale_marker_preexisted:
                clearmarker()
            print('graph.json NOT written: refusing to overwrite (see warning above). GRAPH_REPORT.md, .graphify_labels.json and .graphify_analysis.json left untouched.', file=sys.stderr)
            sys.exit(1)
        tokens = label_token_usage
        from graphify.report import load_learning_for_report as _llfr
        report = generate(G, communities, cohesion, labels, gods, surprises, {'warning': 'cluster-only mode — file stats not available'}, tokens, str(watch_path), suggested_questions=questions, min_community_size=min_community_size, built_at_commit=_commit, learning=_llfr(out / 'graph.json'))
        (out / 'GRAPH_REPORT.md').write_text(report, encoding='utf-8')
        stages.mark('report')
        analysis = {'communities': {str(k): v for k, v in communities.items()}, 'cohesion': {str(k): v for k, v in cohesion.items()}, 'gods': gods, 'surprises': surprises, 'questions': questions}
        (out / '.graphify_analysis.json').write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding='utf-8')

        if not placeholder_only:
            from graphify.paths import json as write_json
            write_json(labels_path, {str(k): v for k, v in labels.items()}, ensure_ascii=False)
            from graphify.cluster import community_member_sigs as _cms
            write_text(labels_path.parent / (labels_path.name + '.sig'), json.dumps({str(k): v for k, v in _cms(communities).items()}))
        html_target = out / 'graph.html'

        if no_viz:
            if html_target.exists():
                html_target.unlink()
            clearmarker()
            stages.mark('export')
            stages.total()
            print(f'Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated (--no-viz; graph.html removed).')
        else:
            html_written = False
            skip_reason: str | None = None

            try:
                from graphify.exporters.html import _viz_node_limit
                viz_limit = _viz_node_limit()

                if viz_limit <= 0:
                    html_target.unlink(missing_ok=True)
                    clearmarker()
                    skip_reason = 'GRAPHIFY_VIZ_NODE_LIMIT=0 disables HTML visualization'
                else:
                    html_written = to_html(G, communities, str(html_target), community_labels=labels or None, node_limit=viz_limit)

                    if html_written:
                        clearmarker()
                    else:
                        skip_reason = 'no useful community aggregation could be generated'

                        if html_target.exists():
                            skip_reason += '; existing graph.html left unchanged'
            except ValueError as viz_err:
                skip_reason = str(viz_err)

                if html_target.exists():
                    skip_reason += '; existing graph.html left unchanged'

            if skip_reason:
                print(f'Skipped graph.html: {skip_reason}')
            stages.mark('export')
            stages.total()

            if html_written:
                print(f'Done - {len(communities)} communities. GRAPH_REPORT.md, graph.json and graph.html updated.')
            else:
                print(f'Done - {len(communities)} communities. GRAPH_REPORT.md and graph.json updated.')
    elif cmd == 'update':
        force = os.environ.get('GRAPHIFY_FORCE', '').lower() in ('1', 'true', 'yes')
        no_cluster = False
        args = sys.argv[2:]
        watch_arg: str | None = None

        for a in args:
            if a == '--force':
                force = True
                continue

            if a == '--no-cluster':
                no_cluster = True
                continue

            if a.startswith('-'):
                print(f'error: unknown update option: {a}', file=sys.stderr)
                sys.exit(2)

            if watch_arg is not None:
                print('error: update accepts at most one path argument', file=sys.stderr)
                sys.exit(2)
            watch_arg = a

        if watch_arg is not None:
            watch_path = Path(watch_arg)
        else:
            saved = Path(_GRAPHIFY_OUT) / '.graphify_root'

            if saved.exists():
                watch_path = Path(saved.read_text(encoding='utf-8-sig').strip())
            else:
                watch_path = Path('.')

        if not watch_path.exists():
            print(f'error: path not found: {watch_path}', file=sys.stderr)
            sys.exit(1)
        from graphify.watch import _rebuild_code
        print(f'Re-extracting code files in {watch_path} (no LLM needed)...')
        ok = _rebuild_code(watch_path, force=force, no_cluster=no_cluster, block_on_lock=True)

        if ok:
            print('Code graph updated. For doc/paper/image changes run /graphify --update in your AI assistant.')

            if not (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or os.environ.get('MOONSHOT_API_KEY') or os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('GRAPHIFY_NO_TIPS')):
                print('Tip: set GEMINI_API_KEY or GOOGLE_API_KEY to use Gemini for semantic extraction.')
        else:
            print('Nothing to update or rebuild failed — check output above.', file=sys.stderr)
            sys.exit(1)
    elif cmd == 'hook-check':
        sys.exit(0)
    elif cmd == 'hook-guard':
        hookguard(sys.argv[2] if len(sys.argv) > 2 else '', strict='--strict' in sys.argv[3:])
        sys.exit(0)
    elif cmd == 'check-update':
        if len(sys.argv) < 3:
            print('Usage: graphify check-update <path>', file=sys.stderr)
            sys.exit(1)
        from graphify.watch import check_update
        check_update(Path(sys.argv[2]).resolve())
        sys.exit(0)
    elif cmd == 'tree':
        from typing import Optional
        from graphify.tree_html import write_tree_html, DEFAULT_MAX_CHILDREN
        graph_path = Path(_GRAPHIFY_OUT) / 'graph.json'
        output_path: 'Optional[Path]' = None
        root: 'Optional[str]' = None
        max_children = DEFAULT_MAX_CHILDREN
        top_k_edges = 0
        project_label: 'Optional[str]' = None
        args = sys.argv[2:]
        i_arg = 0

        while i_arg < len(args):
            a = args[i_arg]

            if a == '--graph' and i_arg + 1 < len(args):
                graph_path = Path(args[i_arg + 1])
                i_arg += 2
            elif a == '--output' and i_arg + 1 < len(args):
                output_path = Path(args[i_arg + 1])
                i_arg += 2
            elif a == '--root' and i_arg + 1 < len(args):
                root = args[i_arg + 1]
                i_arg += 2
            elif a == '--max-children' and i_arg + 1 < len(args):
                max_children = int(args[i_arg + 1])
                i_arg += 2
            elif a == '--top-k-edges' and i_arg + 1 < len(args):
                top_k_edges = int(args[i_arg + 1])
                i_arg += 2
            elif a == '--label' and i_arg + 1 < len(args):
                project_label = args[i_arg + 1]
                i_arg += 2
            elif a in ('-h', '--help'):
                print('Usage: graphify tree [--graph PATH] [--output HTML]')
                print('  --graph PATH         path to graph.json (default graphify-out/graph.json)')
                print('  --output HTML        output path (default graphify-out/GRAPH_TREE.html)')
                print('  --root PATH          filesystem root (default: longest common dir of all source_files)')
                print('  --max-children N     cap visible children per node (default 200)')
                print('  --top-k-edges N      pre-compute top-K outbound edges per symbol (default 12)')
                print('  --label NAME         project label shown in the page header')
                return
            else:
                i_arg += 1

        if not graph_path.is_file():
            print(f'error: graph.json not found at {graph_path}', file=sys.stderr)
            sys.exit(1)
        enforcecap(graph_path)

        if output_path is None:
            output_path = graph_path.parent / 'GRAPH_TREE.html'

        try:
            out = write_tree_html(graph_path=graph_path, output_path=output_path, root=root, max_children=max_children, top_k_edges=top_k_edges, project_label=project_label)
        except ValueError as exc:
            print(f'error: {exc}', file=sys.stderr)
            sys.exit(1)
        size_kb = out.stat().st_size / 1024
        print(f'wrote {out} ({size_kb:.1f} KB)')
        print(f'open with: xdg-open {out}  (or file://{out.resolve()})')
        sys.exit(0)
    elif cmd == 'merge-driver':
        if len(sys.argv) < 5:
            print('Usage: graphify merge-driver <base> <current> <other>', file=sys.stderr)
            sys.exit(1)
        _base_path, _current_path, _other_path = (sys.argv[2], sys.argv[3], sys.argv[4])
        _MERGE_MAX_BYTES = 50 * 1024 * 1024
        _MERGE_MAX_NODES = 100000
        import networkx
        from networkx.readwrite import json_graph

        def loadgraph(p: str):
            path_obj = Path(p)

            try:
                size = path_obj.stat().st_size
            except OSError as exc:
                raise RuntimeError(f'cannot stat {p}: {exc}') from exc

            if size > _MERGE_MAX_BYTES:
                raise RuntimeError(f'graph.json {p} is {size} bytes, exceeds {_MERGE_MAX_BYTES}-byte cap')
            data = json.loads(path_obj.read_text(encoding='utf-8'))
            from graphify.paths import load as load_graph_json

            return (load_graph_json(data), data)

        try:
            G_cur, _ = loadgraph(_current_path)
            G_oth, _ = loadgraph(_other_path)
        except Exception as exc:
            print(f'[graphify merge-driver] error loading graphs: {exc}', file=sys.stderr)
            sys.exit(1)
        merged = networkx.compose(G_cur, G_oth)

        if merged.number_of_nodes() > _MERGE_MAX_NODES:
            print(f'[graphify merge-driver] merged graph has {merged.number_of_nodes()} nodes, exceeds {_MERGE_MAX_NODES}-node cap; aborting merge.', file=sys.stderr)
            sys.exit(1)

        try:
            out_data = json_graph.node_link_data(merged, edges='links')
        except TypeError:
            out_data = json_graph.node_link_data(merged)

        for _link in out_data.get('links', []):
            _ts, _tt = (_link.pop('_src', None), _link.pop('_tgt', None))

            if _ts is not None and _tt is not None:
                _link['source'], _link['target'] = (_ts, _tt)
        from graphify.paths import json as write_json
        write_json(_current_path, out_data, indent=2)
        sys.exit(0)
    elif cmd == 'merge-graphs':
        args = sys.argv[2:]
        graph_paths: list[Path] = []
        out_path = Path(_GRAPHIFY_OUT) / 'merged-graph.json'
        previous_path: 'Path | None' = None
        i = 0

        while i < len(args):
            if args[i] == '--out' and i + 1 < len(args):
                out_path = Path(args[i + 1])
                i += 2
            elif args[i] == '--previous' and i + 1 < len(args):
                previous_path = Path(args[i + 1])
                i += 2
            else:
                graph_paths.append(Path(args[i]))
                i += 1

        if len(graph_paths) < 2:
            print('Usage: graphify merge-graphs <graph1.json> <graph2.json> [...] [--out merged.json] [--previous previous-merged-graph.json]', file=sys.stderr)
            sys.exit(1)
        previous_community: dict[str, int] = {}

        if previous_path is not None:
            if not previous_path.exists():
                print(f'error: --previous file not found: {previous_path}', file=sys.stderr)
                sys.exit(1)

            try:
                _prev_raw = json.loads(previous_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
                print(f'error: --previous file is not readable JSON: {previous_path} ({e})', file=sys.stderr)
                sys.exit(1)

            for _n in _prev_raw.get('nodes', []):
                if isinstance(_n, dict) and isinstance(_n.get('community'), int) and _n.get('id'):
                    previous_community[_n['id']] = _n['community']
        import networkx
        from networkx.readwrite import json_graph
        from graphify.build import prefix, repotags
        graphs = []

        for gp in graph_paths:
            if not gp.exists():
                print(f'error: not found: {gp}', file=sys.stderr)
                sys.exit(1)
            enforcecap(gp)
            data = json.loads(gp.read_text(encoding='utf-8'))

            if 'links' not in data and 'edges' in data:
                data = dict(data, links=data['edges'])
            data = dict(data, links=[{**link, '_src': link.get('_src', link.get('source')), '_tgt': link.get('_tgt', link.get('target'))} for link in data.get('links', [])])

            try:
                G = json_graph.node_link_graph(data, edges='links')
            except TypeError:
                G = json_graph.node_link_graph(data)

            if 'hyperedges' not in G.graph and isinstance(data.get('hyperedges'), list):
                G.graph['hyperedges'] = data['hyperedges']
            graphs.append(G)

        def tosimple(g: 'networkx.Graph') -> 'networkx.Graph':
            if type(g) is not networkx.Graph:
                return networkx.Graph(g)

            return g

        repo_tags = repotags(graph_paths)
        naive_tags = [gp.parent.parent.name for gp in graph_paths]

        if len(set(naive_tags)) != len(naive_tags):
            print(f"  note: repo dir names collide; using distinct tags: {', '.join(repo_tags)}")
        merged = networkx.Graph()
        collected_hyperedges: list = []
        community_offset = 0

        for G, repo_tag in zip(graphs, repo_tags):
            prefixed = tosimple(prefix(G, repo_tag, community_offset=community_offset))
            hes = prefixed.graph.get('hyperedges')

            if isinstance(hes, list):
                collected_hyperedges.extend((h for h in hes if isinstance(h, dict)))
            cids = [d['community'] for _, d in prefixed.nodes(data=True) if isinstance(d.get('community'), int)]

            if cids:
                community_offset = max(community_offset, max(cids) + 1)
            merged = networkx.compose(merged, prefixed)
        from graphify.types import foldnodes
        shared_links = foldnodes(merged)

        if shared_links:
            print(f'  linked {shared_links} type declaration(s) shared across repos')
        from graphify.calls import foldedges
        call_links = foldedges(merged)

        if call_links:
            print(f'  resolved {call_links} member call(s) across repos')

        if previous_path is not None:
            _restored = 0

            for _nid, _data in merged.nodes(data=True):
                if _nid in previous_community:
                    _data['community'] = previous_community[_nid]
                    _restored += 1
                else:
                    _data.pop('community', None)
            print(f'  restored the previous merged community on {_restored} node(s) from {previous_path}')
        merged.graph.pop('hyperedges', None)

        if collected_hyperedges:
            from graphify.export import hyperedges
            hyperedges(merged, collected_hyperedges)

        try:
            out_data = json_graph.node_link_data(merged, edges='links')
        except TypeError:
            out_data = json_graph.node_link_data(merged)

        for link in out_data.get('links', []):
            tsrc = link.pop('_src', None)
            ttgt = link.pop('_tgt', None)

            if tsrc is not None and ttgt is not None:
                link['source'] = tsrc
                link['target'] = ttgt
        out_data['hyperedges'] = merged.graph.get('hyperedges', [])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from graphify.paths import json as write_json
        write_json(out_path, out_data, indent=2)
        print(f'Merged {len(graphs)} graphs -> {merged.number_of_nodes()} nodes, {merged.number_of_edges()} edges')
        print(f'Written to: {out_path}')
    elif cmd == 'clone':
        if len(sys.argv) < 3:
            print('Usage: graphify clone <github-url> [--branch <branch>] [--out <dir>]', file=sys.stderr)
            sys.exit(1)
        url = sys.argv[2]
        branch: str | None = None
        out_dir: Path | None = None
        args = sys.argv[3:]
        i = 0

        while i < len(args):
            if args[i] == '--branch' and i + 1 < len(args):
                branch = args[i + 1]
                i += 2
            elif args[i] == '--out' and i + 1 < len(args):
                out_dir = Path(args[i + 1])
                i += 2
            else:
                i += 1
        local_path = clonerepo(url, branch=branch, out_dir=out_dir)
        print(local_path)
    elif cmd == 'export':
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ''

        if subcmd not in ('html', 'callflow-html', 'obsidian', 'wiki', 'svg', 'graphml', 'neo4j', 'falkordb'):
            print('Usage: graphify export <format>', file=sys.stderr)
            print('  html      [--graph PATH] [--labels PATH] [--node-limit N] [--no-viz]', file=sys.stderr)
            print('  callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH] [--report PATH] [--sections PATH] [--output HTML]', file=sys.stderr)
            print('            [--lang auto|zh-CN|en] [--max-sections N] [--diagram-scale N]', file=sys.stderr)
            print('  obsidian  [--graph PATH] [--labels PATH] [--dir PATH]', file=sys.stderr)
            print('  wiki      [--graph PATH] [--labels PATH]', file=sys.stderr)
            print('  svg       [--graph PATH] [--labels PATH]', file=sys.stderr)
            print('  graphml   [--graph PATH]', file=sys.stderr)
            print('  neo4j     [--graph PATH] [--push URI] [--user U] [--password P]', file=sys.stderr)
            print('            (or set NEO4J_PASSWORD instead of --password to keep it off argv)', file=sys.stderr)
            print('  falkordb  [--graph PATH] [--push URI] [--user U] [--password P]', file=sys.stderr)
            print('            (or set FALKORDB_PASSWORD instead of --password to keep it off argv)', file=sys.stderr)
            sys.exit(1)
        args = sys.argv[3:]
        graph_path = Path(_GRAPHIFY_OUT) / 'graph.json'
        graph_path_explicit = False
        labels_path = Path(_GRAPHIFY_OUT) / '.graphify_labels.json'
        labels_path_explicit = False
        report_path = Path(_GRAPHIFY_OUT) / 'GRAPH_REPORT.md'
        report_path_explicit = False
        sections_path: Path | None = None
        callflow_output: Path | None = None
        callflow_lang = 'auto'
        callflow_max_sections = 15
        callflow_diagram_scale = 1.0
        callflow_max_diagram_nodes = 18
        callflow_max_diagram_edges = 24
        analysis_path = Path(_GRAPHIFY_OUT) / '.graphify_analysis.json'
        node_limit = 5000
        no_viz = False
        obsidian_dir = Path(_GRAPHIFY_OUT) / 'obsidian'
        push_uri: str | None = None
        push_user = 'neo4j'
        push_password: str | None = (os.environ.get('FALKORDB_PASSWORD') if subcmd == 'falkordb' else os.environ.get('NEO4J_PASSWORD')) or None
        i = 0

        while i < len(args):
            a = args[i]

            if a == '--graph' and i + 1 < len(args):
                graph_path = Path(args[i + 1])
                graph_path_explicit = True
                i += 2
            elif a == '--labels' and i + 1 < len(args):
                labels_path = Path(args[i + 1])
                labels_path_explicit = True
                i += 2
            elif a == '--report' and i + 1 < len(args):
                report_path = Path(args[i + 1])
                report_path_explicit = True
                i += 2
            elif a == '--sections' and i + 1 < len(args):
                sections_path = Path(args[i + 1])
                i += 2
            elif a == '--output' and i + 1 < len(args):
                callflow_output = Path(args[i + 1]).expanduser()

                if not callflow_output.is_absolute():
                    callflow_output = Path.cwd() / callflow_output
                i += 2
            elif a == '--lang' and i + 1 < len(args):
                callflow_lang = args[i + 1]
                i += 2
            elif a == '--max-sections' and i + 1 < len(args):
                callflow_max_sections = int(args[i + 1])
                i += 2
            elif a == '--diagram-scale' and i + 1 < len(args):
                callflow_diagram_scale = float(args[i + 1])
                i += 2
            elif a == '--max-diagram-nodes' and i + 1 < len(args):
                callflow_max_diagram_nodes = int(args[i + 1])
                i += 2
            elif a == '--max-diagram-edges' and i + 1 < len(args):
                callflow_max_diagram_edges = int(args[i + 1])
                i += 2
            elif a in ('-h', '--help') and subcmd == 'callflow-html':
                print('Usage: graphify export callflow-html [GRAPH|DIR] [--graph PATH] [--labels PATH]')
                print('  --report PATH          path to GRAPH_REPORT.md')
                print('  --sections PATH        JSON section definitions')
                print('  --output HTML          output path (default graphify-out/<project>-callflow.html)')
                print('  --lang LANG            auto, zh-CN, en, etc. (default auto)')
                print('  --max-sections N       maximum auto-derived sections (default 15)')
                print('  --diagram-scale N      Mermaid diagram scale (default 1.0)')
                print('  --max-diagram-nodes N  representative nodes per section (default 18)')
                print('  --max-diagram-edges N  representative edges per section (default 24)')
                sys.exit(0)
            elif a == '--node-limit' and i + 1 < len(args):
                node_limit = int(args[i + 1])
                i += 2
            elif a == '--no-viz':
                no_viz = True
                i += 1
            elif a == '--dir' and i + 1 < len(args):
                obsidian_dir = Path(args[i + 1])
                i += 2
            elif a == '--push' and i + 1 < len(args):
                push_uri = args[i + 1]
                i += 2
            elif a == '--user' and i + 1 < len(args):
                push_user = args[i + 1]
                i += 2
            elif a == '--password' and i + 1 < len(args):
                push_password = args[i + 1]
                i += 2
            elif subcmd == 'callflow-html' and (not a.startswith('-')) and (not graph_path_explicit):
                candidate = Path(a)

                if candidate.name == 'graph.json' or candidate.suffix.lower() == '.json':
                    graph_path = candidate
                elif (candidate / 'graph.json').exists():
                    graph_path = candidate / 'graph.json'
                else:
                    graph_path = candidate / _GRAPHIFY_OUT / 'graph.json'
                graph_path_explicit = True
                i += 1
            else:
                i += 1
        graph_path = graph_path.expanduser()

        if graph_path_explicit:
            graph_out_dir = graph_path.parent

            if not labels_path_explicit:
                labels_path = graph_out_dir / '.graphify_labels.json'

            if not report_path_explicit:
                report_path = graph_out_dir / 'GRAPH_REPORT.md'
        labels_path = labels_path.expanduser()
        report_path = report_path.expanduser()

        if not graph_path.exists():
            print(f'error: graph not found: {graph_path}. Run /graphify <path> first.', file=sys.stderr)
            sys.exit(1)

        if subcmd == 'callflow-html':
            from graphify.callflow_html import write_callflow_html as _write_callflow_html
            out = _write_callflow_html(graph=graph_path, report=report_path, labels=labels_path, sections=sections_path, output=callflow_output, lang=callflow_lang, max_sections=callflow_max_sections, diagram_scale=callflow_diagram_scale, max_diagram_nodes=callflow_max_diagram_nodes, max_diagram_edges=callflow_max_diagram_edges, verbose=True)
            print(f'callflow HTML written - open in any browser: {out}')
            sys.exit(0)
        from networkx.readwrite import json_graph
        from graphify.build import buildjson
        from graphify.security import check
        _over_cap = False

        try:
            check(graph_path)
        except ValueError as _cap_err:
            if subcmd == 'html':
                _over_cap = True

                try:
                    _over_cap_bytes = graph_path.stat().st_size
                except OSError:
                    _over_cap_bytes = -1
                print(f'warning: graph.json exceeds cap ({_over_cap_bytes} bytes); falling back to community-aggregation view (node_limit=5000)', file=sys.stderr)
            else:
                print(f'error: {_cap_err}', file=sys.stderr)
                sys.exit(1)
        _raw = json.loads(graph_path.read_text(encoding='utf-8'))

        if 'links' not in _raw and 'edges' in _raw:
            _raw = dict(_raw, links=_raw['edges'])
        from graphify.paths import restore
        _raw = restore(_raw)

        try:
            G = json_graph.node_link_graph(_raw, edges='links')
        except TypeError:
            G = json_graph.node_link_graph(_raw)

        if isinstance(_raw.get('hyperedges'), list):
            G.graph['hyperedges'] = _raw['hyperedges']
        communities: dict[int, list[str]] = {}

        if analysis_path.exists():
            _an = json.loads(analysis_path.read_text(encoding='utf-8'))
            communities = {int(k): v for k, v in _an.get('communities', {}).items()}
            cohesion: dict[int, float] = {int(k): v for k, v in _an.get('cohesion', {}).items()}
            gods_data = _an.get('gods', [])
        else:
            cohesion = {}
            gods_data = []
        reconstructed: dict[int, list[str]] = {}

        for node_id, data in G.nodes(data=True):
            cid_raw = data.get('community')

            if cid_raw is None:
                continue

            try:
                cid = int(cid_raw)
            except (TypeError, ValueError):
                continue

            reconstructed.setdefault(cid, []).append(str(node_id))

        if not communities:
            if reconstructed:
                communities = reconstructed
        elif reconstructed:
            sidecar_partition = {frozenset((str(n) for n in nodes)) for nodes in communities.values()}
            fresh_partition = {frozenset(nodes) for nodes in reconstructed.values()}

            if sidecar_partition != fresh_partition:
                sidecar_node_count = len({n for block in sidecar_partition for n in block})
                fresh_node_count = len({n for block in fresh_partition for n in block})
                print(f'warning: {analysis_path} is stale ({sidecar_node_count} node(s) recorded vs {fresh_node_count} in graph.json) — reconstructing communities from graph.json instead. Run `graphify cluster-only .` to refresh the sidecar and its cohesion/god-node data.', file=sys.stderr)
                communities = reconstructed
                from graphify.cluster import score_all as _score_all_export
                from graphify.analyze import god_nodes as _god_nodes_export
                cohesion = _score_all_export(G, communities)
                gods_data = _god_nodes_export(G)
        labels: dict[int, str] = {}

        if labels_path.exists():
            labels = {int(k): v for k, v in json.loads(labels_path.read_text(encoding='utf-8')).items()}
        out_dir = graph_path.parent

        if subcmd == 'html':
            from graphify.export import to_html as _to_html

            if no_viz:
                html_target = out_dir / 'graph.html'

                if html_target.exists():
                    html_target.unlink()
                print('--no-viz: skipped graph.html')
            else:
                _effective_node_limit = 5000 if _over_cap else node_limit
                _to_html(G, communities, str(out_dir / 'graph.html'), community_labels=labels or None, node_limit=_effective_node_limit)

                if G.number_of_nodes() <= _effective_node_limit:
                    print(f'graph.html written - open in any browser, no server needed')

                if _over_cap:
                    sys.exit(0)
        elif subcmd == 'obsidian':
            from graphify.export import to_obsidian as _to_obsidian, to_canvas as _to_canvas
            n = _to_obsidian(G, communities, str(obsidian_dir), community_labels=labels or None, cohesion=cohesion or None)
            print(f'Obsidian vault: {n} notes in {obsidian_dir}/')
            _to_canvas(G, communities, str(obsidian_dir / 'graph.canvas'), community_labels=labels or None)
            print(f'Canvas: {obsidian_dir}/graph.canvas')
            print(f'Open {obsidian_dir}/ as a vault in Obsidian.')
        elif subcmd == 'wiki':
            from graphify.wiki import to_wiki as _to_wiki
            from graphify.analyze import god_nodes as _god_nodes

            if not communities:
                print('error: .graphify_analysis.json is missing or empty — refusing to export wiki to prevent data loss.\nRun `graphify extract .` (or `graphify cluster-only .`) to regenerate community data first.', file=sys.stderr)
                sys.exit(1)

            if not gods_data:
                gods_data = _god_nodes(G)
            n = _to_wiki(G, communities, str(out_dir / 'wiki'), community_labels=labels or None, cohesion=cohesion or None, god_nodes_data=gods_data)
            print(f'Wiki: {n} articles written to {out_dir}/wiki/')
            print(f'  {out_dir}/wiki/index.md  ->  agent entry point')
        elif subcmd == 'svg':
            from graphify.export import to_svg as _to_svg
            _to_svg(G, communities, str(out_dir / 'graph.svg'), community_labels=labels or None)
            print(f'graph.svg written - embeds in Obsidian, Notion, GitHub READMEs')
        elif subcmd == 'graphml':
            from graphify.export import to_graphml as _to_graphml
            _to_graphml(G, communities, str(out_dir / 'graph.graphml'))
            print(f'graph.graphml written - open in Gephi, yEd, or any GraphML tool')
        elif subcmd == 'neo4j':
            if push_uri:
                from graphify.export import push_to_neo4j as _push

                if push_password is None:
                    print('error: --password required for --push', file=sys.stderr)
                    sys.exit(1)
                result = _push(G, uri=push_uri, user=push_user, password=push_password, communities=communities)
                print(f"Pushed to Neo4j: {result['nodes']} nodes, {result['edges']} edges")
            else:
                from graphify.export import to_cypher as _to_cypher
                _to_cypher(G, str(out_dir / 'cypher.txt'))
                print(f'cypher.txt written - import with: cypher-shell < {out_dir}/cypher.txt')
        elif subcmd == 'falkordb':
            if push_uri:
                from graphify.export import push_to_falkordb as _push
                result = _push(G, uri=push_uri, user=push_user, password=push_password, communities=communities)
                print(f"Pushed to FalkorDB: {result['nodes']} nodes, {result['edges']} edges")
            else:
                from graphify.export import to_cypher as _to_cypher
                _to_cypher(G, str(out_dir / 'cypher.txt'))
                print(f"cypher.txt written ({out_dir}/cypher.txt) - statements are OpenCypher. FalkorDB's GRAPH.QUERY runs one statement at a time (no bulk script import), so load a graph with: graphify export falkordb --push falkordb://localhost:6379")
    elif cmd == 'benchmark':
        from graphify.benchmark import run_benchmark, print_benchmark
        graph_path = sys.argv[2] if len(sys.argv) > 2 else defaultpath()
        enforcecap(Path(graph_path))
        corpus_words = None
        detect_path = Path('.graphify_detect.json')

        if detect_path.exists():
            try:
                detect_data = json.loads(detect_path.read_text(encoding='utf-8'))
                corpus_words = detect_data.get('total_words')
            except Exception:
                pass
        result = run_benchmark(graph_path, corpus_words=corpus_words)
        print_benchmark(result)
    elif cmd == 'global':
        subcmd = sys.argv[2] if len(sys.argv) > 2 else ''
        from graphify.graph import global_add as _global_add, global_remove as _global_remove, global_list as _global_list, global_path as _global_path

        if subcmd == 'add':
            args = sys.argv[3:]
            source = None
            tag = None
            i = 0

            while i < len(args):
                if args[i] == '--as' and i + 1 < len(args):
                    tag = args[i + 1]
                    i += 2
                elif not source:
                    source = Path(args[i])
                    i += 1
                else:
                    i += 1

            if not source:
                print('Usage: graphify global add <graph.json> [--as <repo-tag>]', file=sys.stderr)
                sys.exit(1)

            if not tag:
                from graphify.build import repotags
                tag = repotags([source.absolute()])[0]

            try:
                result = _global_add(source, tag)

                if result['skipped']:
                    print(f"'{tag}' unchanged since last add - global graph not modified.")
                else:
                    print(f"Added '{tag}' to global graph: +{result['nodes_added']} nodes, -{result['nodes_removed']} pruned. Global: {_global_path()}")

                    if result.get('cross_repo_calls'):
                        print(f"  resolved {result['cross_repo_calls']} member call(s) across repos")
            except Exception as exc:
                print(f'error: {exc}', file=sys.stderr)
                sys.exit(1)
        elif subcmd == 'remove':
            if len(sys.argv) <= 3:
                print('Usage: graphify global remove <repo-tag>', file=sys.stderr)
                sys.exit(1)
            tag = sys.argv[3]

            try:
                removed = _global_remove(tag)
                print(f"Removed '{tag}' from global graph ({removed} nodes pruned).")
            except KeyError as exc:
                print(f'error: {exc}', file=sys.stderr)
                sys.exit(1)
        elif subcmd == 'list':
            repos = _global_list()

            if not repos:
                print("Global graph is empty. Use 'graphify global add' to add a project.")
            else:
                print(f'Global graph: {_global_path()}')

                for tag, info in repos.items():
                    print(f"  {tag}: {info.get('node_count', '?')} nodes, added {info.get('added_at', '?')[:10]}")
        elif subcmd == 'path':
            print(_global_path())
        else:
            print('Usage: graphify global [add|remove|list|path]', file=sys.stderr)
            sys.exit(1)
    elif cmd == 'extract':
        if len(sys.argv) < 3:
            print('Usage: graphify extract <path> [--backend gemini|kimi|claude|openai|deepseek|ollama] [--model M] [--mode deep] [--out DIR|--output DIR] [--google-workspace] [--no-cluster] [--no-gitignore] [--code-only] [--no-dedup] [--max-workers N] [--token-budget N] [--max-concurrency N] [--api-timeout S] [--postgres DSN] [--cargo] [--allow-partial] [--allow-dedup-shrink] [--timing]', file=sys.stderr)
            sys.exit(1)
        has_path = True

        if sys.argv[2].startswith('-'):
            has_path = False
            target = Path('.').resolve()
        else:
            target = Path(sys.argv[2]).resolve()

            if not target.exists():
                print(f'error: path not found: {target}', file=sys.stderr)
                sys.exit(1)
        backend: str | None = None
        model: str | None = None
        extract_mode: str | None = None
        out_dir: Path | None = None
        cli_postgres_dsn: str | None = None
        cli_cargo: bool = False
        cli_allow_partial: bool = False
        cli_allow_dedup_shrink: bool = False
        no_cluster = False
        dedup_llm = False
        no_dedup = False
        google_workspace = False
        global_merge = False
        code_only = False
        no_gitignore = False
        global_repo_tag: str | None = None
        cli_max_workers: int | None = None
        cli_token_budget: int | None = None
        cli_max_concurrency: int | None = None
        cli_api_timeout: float | None = None
        cli_resolution: float = 1.0
        cli_exclude_hubs: float | None = None
        cli_excludes: list[str] = []
        cli_timing: bool = False
        force = os.environ.get('GRAPHIFY_FORCE', '').lower() in ('1', 'true', 'yes')

        def parseint(name: str, raw: str) -> int:
            try:
                v = int(raw)
            except ValueError:
                print(f'error: {name} must be a positive integer (got {raw!r})', file=sys.stderr)
                sys.exit(2)

            if v <= 0:
                print(f'error: {name} must be > 0 (got {v})', file=sys.stderr)
                sys.exit(2)

            return v

        def parsefloat(name: str, raw: str) -> float:
            try:
                v = float(raw)
            except ValueError:
                print(f'error: {name} must be a positive number (got {raw!r})', file=sys.stderr)
                sys.exit(2)

            if v <= 0:
                print(f'error: {name} must be > 0 (got {v})', file=sys.stderr)
                sys.exit(2)

            return v

        args = sys.argv[3:] if has_path else sys.argv[2:]
        i = 0

        while i < len(args):
            a = args[i]

            if a == '--backend' and i + 1 < len(args):
                backend = args[i + 1]
                i += 2
            elif a.startswith('--backend='):
                backend = a.split('=', 1)[1]
                i += 1
            elif a == '--model' and i + 1 < len(args):
                model = args[i + 1]
                i += 2
            elif a.startswith('--model='):
                model = a.split('=', 1)[1]
                i += 1
            elif a == '--mode' and i + 1 < len(args):
                extract_mode = args[i + 1]
                i += 2
            elif a.startswith('--mode='):
                extract_mode = a.split('=', 1)[1]
                i += 1
            elif a in ('--out', '--output') and i + 1 < len(args):
                out_dir = Path(args[i + 1])
                i += 2
            elif a.startswith(('--out=', '--output=')):
                out_dir = Path(a.split('=', 1)[1])
                i += 1
            elif a == '--no-cluster':
                no_cluster = True
                i += 1
            elif a == '--dedup-llm':
                dedup_llm = True
                i += 1
            elif a == '--no-dedup':
                no_dedup = True
                i += 1
            elif a == '--code-only':
                code_only = True
                i += 1
            elif a == '--google-workspace':
                google_workspace = True
                i += 1
            elif a == '--no-gitignore':
                no_gitignore = True
                i += 1
            elif a == '--global':
                global_merge = True
                i += 1
            elif a == '--as' and i + 1 < len(args):
                global_repo_tag = args[i + 1]
                i += 2
            elif a == '--max-workers' and i + 1 < len(args):
                cli_max_workers = parseint('--max-workers', args[i + 1])
                i += 2
            elif a.startswith('--max-workers='):
                cli_max_workers = parseint('--max-workers', a.split('=', 1)[1])
                i += 1
            elif a == '--token-budget' and i + 1 < len(args):
                cli_token_budget = parseint('--token-budget', args[i + 1])
                i += 2
            elif a.startswith('--token-budget='):
                cli_token_budget = parseint('--token-budget', a.split('=', 1)[1])
                i += 1
            elif a == '--max-concurrency' and i + 1 < len(args):
                cli_max_concurrency = parseint('--max-concurrency', args[i + 1])
                i += 2
            elif a.startswith('--max-concurrency='):
                cli_max_concurrency = parseint('--max-concurrency', a.split('=', 1)[1])
                i += 1
            elif a == '--api-timeout' and i + 1 < len(args):
                cli_api_timeout = parsefloat('--api-timeout', args[i + 1])
                i += 2
            elif a.startswith('--api-timeout='):
                cli_api_timeout = parsefloat('--api-timeout', a.split('=', 1)[1])
                i += 1
            elif a == '--resolution' and i + 1 < len(args):
                cli_resolution = parsefloat('--resolution', args[i + 1])
                i += 2
            elif a.startswith('--resolution='):
                cli_resolution = parsefloat('--resolution', a.split('=', 1)[1])
                i += 1
            elif a == '--exclude-hubs' and i + 1 < len(args):
                cli_exclude_hubs = float(args[i + 1])
                i += 2
            elif a.startswith('--exclude-hubs='):
                cli_exclude_hubs = float(a.split('=', 1)[1])
                i += 1
            elif a == '--exclude' and i + 1 < len(args):
                cli_excludes.append(args[i + 1])
                i += 2
            elif a.startswith('--exclude='):
                cli_excludes.append(a.split('=', 1)[1])
                i += 1
            elif a == '--postgres' and i + 1 < len(args):
                cli_postgres_dsn = args[i + 1]
                i += 2
            elif a.startswith('--postgres='):
                cli_postgres_dsn = a.split('=', 1)[1]
                i += 1
            elif a == '--cargo':
                cli_cargo = True
                i += 1
            elif a == '--force':
                force = True
                i += 1
            elif a == '--allow-partial':
                cli_allow_partial = True
                i += 1
            elif a == '--allow-dedup-shrink':
                cli_allow_dedup_shrink = True
                i += 1
            elif a == '--timing':
                cli_timing = True
                i += 1
            else:
                i += 1

        if not has_path and cli_postgres_dsn is None:
            print('error: must specify a path to scan or a --postgres DSN', file=sys.stderr)
            sys.exit(1)

        if no_dedup and dedup_llm:
            print('error: --no-dedup and --dedup-llm are mutually exclusive (--dedup-llm is a tiebreaker inside the dedup pass)', file=sys.stderr)
            sys.exit(2)
        _VALID_MODES = {'deep'}

        if extract_mode is not None and extract_mode not in _VALID_MODES:
            print(f"error: unknown --mode '{extract_mode}'. Available: {', '.join(sorted(_VALID_MODES))}", file=sys.stderr)
            sys.exit(2)
        deep_mode = extract_mode == 'deep'

        if deep_mode:
            print('[graphify extract] deep mode enabled: richer semantic extraction')

        if cli_api_timeout is not None:
            os.environ['GRAPHIFY_API_TIMEOUT'] = str(cli_api_timeout)

        if cli_max_workers is not None:
            os.environ['GRAPHIFY_MAX_WORKERS'] = str(cli_max_workers)
        out_root = out_dir.resolve() if out_dir else target
        graphify_out = out_root / _GRAPHIFY_OUT
        graphify_out.mkdir(parents=True, exist_ok=True)
        from graphify.watch import _write_build_config as _write_build_cfg, _read_build_excludes as _read_build_ex, _read_build_gitignore as _read_build_gi
        _effective_gitignore = False if no_gitignore else _read_build_gi(graphify_out)
        _effective_excludes = cli_excludes or _read_build_ex(graphify_out)
        _write_build_cfg(graphify_out, excludes=cli_excludes or None, gitignore=False if no_gitignore else None)
        stages = _StageTimer(cli_timing)
        from graphify.detect import detect, incremental, save as save_manifest
        manifest_path = graphify_out / 'manifest.json'
        existing_graph_path = graphify_out / 'graph.json'
        incremental_mode = existing_graph_path.exists() if has_path else False
        incremental_mode = incremental_mode and (not force)
        merge_existing_graph = incremental_mode or (code_only and existing_graph_path.exists())

        if force and code_only and existing_graph_path.exists():
            print('[graphify extract] --force --code-only: full AST re-scan, existing semantic layer preserved (no semantic pass this run)')

        if force:
            print('[graphify extract] --force: full re-scan, semantic cache reads skipped')
        elif incremental_mode and (not manifest_path.exists()):
            print("[graphify extract] manifest.json missing; using existing graph.json as the incremental baseline (all files re-checked; nodes for files outside this run's scope are preserved)")

        if not has_path:
            detection = {}
            code_files = []
            doc_files = []
            paper_files = []
            image_files = []
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0
            files_by_type = {}
        elif incremental_mode:
            print(f'[graphify extract] incremental scan of {target}')
            detection = incremental(target, manifest_path=str(manifest_path), google_workspace=google_workspace or None, extra_excludes=_effective_excludes or None, gitignore=_effective_gitignore, cache_root=out_root)
            files_by_type = detection.get('files', {})
            new_by_type = detection.get('new_files', {})
            code_files = [Path(p) for p in new_by_type.get('code', [])]
            doc_files = [Path(p) for p in new_by_type.get('document', [])]
            paper_files = [Path(p) for p in new_by_type.get('paper', [])]
            image_files = [Path(p) for p in new_by_type.get('image', [])]
            deleted_files = list(detection.get('deleted_files', []))
            excluded_files = list(detection.get('excluded_files', []))
            unchanged_total = sum((len(v) for v in detection.get('unchanged_files', {}).values()))
            _seen_files = {f for _fl in files_by_type.values() for f in _fl}
            _seen_files.update(detection.get('unclassified', []))
            graph_stale_sources = stalesources(existing_graph_path, target, _seen_files, detection=detection)
            _healed_sources = zerocode(existing_graph_path, target, detection.get('unchanged_files', {}).get('code', []))

            if _healed_sources:
                print(f'[graphify extract] re-queuing {len(_healed_sources)} manifest-stamped code file(s) with no nodes in graph.json (prior failed extraction, #2543)')
                code_files.extend((Path(p) for p in _healed_sources))
            _unchanged_sem: list[str] = []

            for _k in ('document', 'paper', 'image'):
                _unchanged_sem.extend(detection.get('unchanged_files', {}).get(_k, []))
            _healed_sem_sources = zerosemantic(existing_graph_path, target, _unchanged_sem)

            if _healed_sem_sources:
                print(f'[graphify extract] re-queuing {len(_healed_sem_sources)} manifest-stamped semantic file(s) with no nodes or hyperedges in graph.json (prior empty/edge-only extraction, #2927)')
                _healed_sem_set = set(_healed_sem_sources)

                for _p in detection.get('unchanged_files', {}).get('document', []):
                    if _p in _healed_sem_set:
                        doc_files.append(Path(_p))

                for _p in detection.get('unchanged_files', {}).get('paper', []):
                    if _p in _healed_sem_set:
                        paper_files.append(Path(_p))

                for _p in detection.get('unchanged_files', {}).get('image', []):
                    if _p in _healed_sem_set:
                        image_files.append(Path(_p))
        else:
            print(f'[graphify extract] scanning {target}')
            detection = detect(target, google_workspace=google_workspace or None, extra_excludes=_effective_excludes or None, cache_root=out_root, gitignore=_effective_gitignore)
            files_by_type = detection.get('files', {})
            code_files = [Path(p) for p in files_by_type.get('code', [])]
            doc_files = [Path(p) for p in files_by_type.get('document', [])]
            paper_files = [Path(p) for p in files_by_type.get('paper', [])]
            image_files = [Path(p) for p in files_by_type.get('image', [])]
            deleted_files = []
            excluded_files = []
            graph_stale_sources = []
            unchanged_total = 0

            if existing_graph_path.exists():
                _seen_files = {f for _fl in files_by_type.values() for f in _fl}
                _seen_files.update(detection.get('unclassified', []))
                graph_stale_sources = stalesources(existing_graph_path, target, _seen_files, detection=detection)
        semantic_files = doc_files + paper_files + image_files

        if code_only and semantic_files:
            print(f'[graphify extract] --code-only: skipping {len(semantic_files)} non-code file(s) ({len(doc_files)} docs, {len(paper_files)} papers, {len(image_files)} images) — no LLM extraction')
            semantic_files = []
            doc_files = []
            paper_files = []
            image_files = []

        if deep_mode and incremental_mode and (not code_only):
            _deep_all = [Path(p) for _ftype in ('document', 'paper', 'image') for p in files_by_type.get(_ftype, [])]

            if len(_deep_all) != len(semantic_files):
                print(f'[graphify extract] deep mode: widening semantic pass from {len(semantic_files)} changed to {len(_deep_all)} live doc/paper/image file(s); the deep semantic cache decides what is re-extracted')
            semantic_files = _deep_all

        if incremental_mode:
            _excl_note = f'; {len(excluded_files)} excluded' if excluded_files else ''
            print(f'[graphify extract] {len(code_files)} code, {len(doc_files)} docs, {len(paper_files)} papers, {len(image_files)} images changed; {unchanged_total} unchanged; {len(deleted_files)} deleted{_excl_note}')
        else:
            print(f'[graphify extract] found {len(code_files)} code, {len(doc_files)} docs, {len(paper_files)} papers, {len(image_files)} images')
        _unclassified = detection.get('unclassified', []) if isinstance(detection, dict) else []

        if _unclassified:
            _names = ', '.join(sorted({Path(p).name for p in _unclassified})[:6])
            _more = f' (+{len(_unclassified) - 6} more)' if len(_unclassified) > 6 else ''
            print(f'[graphify extract] {len(_unclassified)} file(s) not classified (no supported extension or shebang), skipped: {_names}{_more}')
        _sensitive = detection.get('skipped_sensitive', []) if isinstance(detection, dict) else []
        _sec = [s for s in _sensitive if ' [' not in s]

        if _sec:
            _snames = ', '.join(sorted({Path(p).name for p in _sec})[:6])
            _smore = f' (+{len(_sec) - 6} more)' if len(_sec) > 6 else ''
            print(f'[graphify extract] {len(_sec)} file(s) skipped as potentially sensitive (rename or move if wrongly flagged): {_snames}{_smore}')
        stages.mark('detect')
        _extraction_incomplete = False

        if detection.get('walk_errors'):
            _extraction_incomplete = True

        if incremental_mode:
            from graphify.langs import terraform
            code_files = terraform.refresh(code_files, [Path(p) for p in files_by_type.get('code', [])], [Path(p) for p in [*deleted_files, *excluded_files, *graph_stale_sources]])
        ast_result: dict = {'nodes': [], 'edges': [], 'input_tokens': 0, 'output_tokens': 0}

        if code_files:
            from graphify.extract import extract
            ast_kwargs: dict = {'cache_root': out_root, 'root': target}

            if cli_max_workers is not None:
                ast_kwargs['max_workers'] = cli_max_workers

            if incremental_mode and existing_graph_path.exists():
                _ctx_nodes: list[dict] = []
                _ctx_edges: list[dict] = []

                try:
                    from graphify.build import isasttier
                    from graphify.security import check
                    check(existing_graph_path)
                    _ctx_graph = json.loads(existing_graph_path.read_text(encoding='utf-8'))
                    _ctx_root = Path(os.path.abspath(target))

                    def identity(source_file) -> str | None:
                        if not source_file:
                            return None

                        _p = Path(str(source_file))

                        if not _p.is_absolute():
                            _p = _ctx_root / _p

                        return Path(os.path.abspath(_p)).as_posix()

                    _ctx_live = {identity(f) for _flist in detection.get('unchanged_files', {}).values() for f in _flist}
                    _ctx_live.discard(None)
                    _ctx_live.difference_update((identity(p) for p in code_files))

                    for _node in _ctx_graph.get('nodes', []):
                        if not _node.get('id') or not isasttier(_node):
                            continue

                        _sf = _node.get('source_file')

                        if not _sf or identity(_sf) not in _ctx_live:
                            continue

                        _ctx_node = {'id': _node['id'], 'label': _node.get('label'), 'source_file': _sf, 'file_type': _node.get('file_type'), 'type': _node.get('type')}

                        for _marker in ('_callable', '_callable_class', '_elixir_module', '_rust_impl_key', '_rust_declaration_count'):
                            if _node.get(_marker):
                                _ctx_node[_marker] = _node[_marker]
                        _metadata = _node.get('metadata')

                        if isinstance(_metadata, dict):
                            _fwd_metadata = {key: _metadata[key] for key in ('ruby_resolution_schema', 'ruby_method_kind', 'ruby_lookup_unsafe', 'python_opaque_bases', 'ruby_reopened', 'ruby_external_method_owners', 'language', 'kind', 'module', 'name', 'arity') if key in _metadata}

                            if _fwd_metadata:
                                _ctx_node['metadata'] = _fwd_metadata
                        _ctx_nodes.append(_ctx_node)

                    for _edge in _ctx_graph.get('links', _ctx_graph.get('edges', [])):
                        if _edge.get('relation') not in ('contains', 'method', 'inherits'):
                            continue

                        if not isasttier(_edge):
                            continue

                        _sf = _edge.get('source_file')

                        if not _sf or identity(_sf) not in _ctx_live:
                            continue

                        _ctx_edge = {'source': _edge.get('source'), 'target': _edge.get('target'), 'relation': _edge.get('relation'), 'source_file': _sf}
                        _edge_metadata = _edge.get('metadata')

                        if isinstance(_edge_metadata, dict) and isinstance(_edge_metadata.get('ruby_superclass_ref'), str):
                            _ctx_edge['metadata'] = {'ruby_superclass_ref': _edge_metadata['ruby_superclass_ref']}
                            _lexical_scopes = _edge_metadata.get('ruby_lexical_scopes')

                            if isinstance(_lexical_scopes, list) and all((isinstance(_scope, str) for _scope in _lexical_scopes)):
                                _ctx_edge['metadata']['ruby_lexical_scopes'] = list(_lexical_scopes)
                        _ctx_edges.append(_ctx_edge)
                except Exception:
                    _ctx_nodes, _ctx_edges = ([], [])

                if _ctx_nodes:
                    ast_kwargs['resolution_context_nodes'] = _ctx_nodes

                if _ctx_edges:
                    ast_kwargs['resolution_context_edges'] = _ctx_edges
            print(f'[graphify extract] AST extraction on {len(code_files)} code files...')

            try:
                ast_result = extract(code_files, **ast_kwargs)
            except Exception as exc:
                print(f'[graphify extract] AST extraction failed: {exc}', file=sys.stderr)

                if not cli_allow_partial:
                    sys.exit(1)
                ast_result = {'nodes': [], 'edges': [], 'input_tokens': 0, 'output_tokens': 0}
                _extraction_incomplete = True
        stages.mark('AST extract')
        from graphify.cache import check as check_semantic_cache, prune as prune_semantic_cache, save as save_semantic_cache, scope as scope_semantic_result
        sem_result: dict = {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0}
        _partial_semantic_files: set[str] = set()
        sem_cache_hits = 0
        sem_cache_misses = 0
        sem_cache_mode = 'deep' if deep_mode else None
        from graphify.llm import _extraction_system as _sem_prompt_for
        sem_prompt = _sem_prompt_for(deep=deep_mode)

        if semantic_files:
            sem_paths_str = [str(p) for p in semantic_files]

            if force:
                cached_nodes, cached_edges, cached_hyperedges = ([], [], [])
                uncached_paths = list(sem_paths_str)
            else:
                cached_nodes, cached_edges, cached_hyperedges, uncached_paths = check_semantic_cache(sem_paths_str, root=target, cache_root=out_root, mode=sem_cache_mode, prompt=sem_prompt)
            sem_cache_hits = len(semantic_files) - len(uncached_paths)
            sem_cache_misses = len(uncached_paths)
            sem_result['nodes'].extend(cached_nodes)
            sem_result['edges'].extend(cached_edges)
            sem_result['hyperedges'].extend(cached_hyperedges)

            if sem_cache_hits:
                print(f'[graphify extract] semantic cache: {sem_cache_hits} hit / {sem_cache_misses} miss')

            if uncached_paths:
                print(f'[graphify extract] semantic extraction on {len(uncached_paths)} files via {backend}...')
                corpus_kwargs: dict = {'backend': backend, 'model': model, 'root': target, 'cache_root': out_root}

                if deep_mode:
                    corpus_kwargs['deep_mode'] = True

                if cli_token_budget is not None:
                    corpus_kwargs['token_budget'] = cli_token_budget

                if cli_max_concurrency is not None:
                    corpus_kwargs['max_concurrency'] = cli_max_concurrency
                _chunk_stats = {'total': 0, 'succeeded': 0}

                def progress(idx: int, total: int, _result: dict) -> None:
                    _chunk_stats['total'] = total
                    _chunk_stats['succeeded'] += 1
                    print(f'[graphify extract] chunk {idx + 1}/{total} done', flush=True)
                corpus_kwargs['on_chunk_done'] = progress

                try:
                    fresh = _extract_corpus_parallel([Path(p) for p in uncached_paths], **corpus_kwargs)
                except ImportError as exc:
                    print(f'error: {exc}', file=sys.stderr)
                    sys.exit(1)
                except Exception as exc:
                    print(f'[graphify extract] semantic extraction failed: {exc}', file=sys.stderr)
                    fresh = {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0}
                    _extraction_incomplete = True

                if uncached_paths and _chunk_stats['succeeded'] == 0:
                    print(f"[graphify extract] error: all semantic chunks failed for backend '{backend}' ({len(uncached_paths)} uncached files) - see per-chunk errors above. If you see 'requires the X package', run `pip install X` and retry.", file=sys.stderr)
                    sys.exit(1)

                if _chunk_stats['total'] and _chunk_stats['succeeded'] < _chunk_stats['total']:
                    _extraction_incomplete = True
                _dropped_files, _dropped_items = scope_semantic_result(fresh, target, uncached_paths)

                if _dropped_files:
                    print(f"[graphify extract] dropped {_dropped_items} out-of-scope item(s) attributed to {len(_dropped_files)} file(s) not dispatched this run: {', '.join(sorted(_dropped_files))}")
                from graphify.llm import _partial_source_files as _partial_sf, _strip_partial_markers as _strip_partial
                _partial_semantic_files = set(_partial_sf(fresh))
                _omitted_files = list(fresh.get('uncovered_files') or [])

                if _omitted_files or _partial_semantic_files:
                    _extraction_incomplete = True
                    print(f'[graphify extract] semantic extraction is incomplete: {len(_omitted_files)} dispatched file(s) produced no nodes and {len(_partial_semantic_files)} came back truncated or hollow. The shrink guard stays armed for this write; pass --allow-partial to overwrite a larger existing graph anyway.', file=sys.stderr)

                try:
                    save_semantic_cache(fresh.get('nodes', []), fresh.get('edges', []), fresh.get('hyperedges', []), root=target, cache_root=out_root, allowed_source_files=uncached_paths, mode=sem_cache_mode, prompt=sem_prompt, partial_source_files=_partial_semantic_files or None)
                except Exception as exc:
                    print(f'[graphify extract] warning: could not write semantic cache: {exc}', file=sys.stderr)
                _strip_partial(fresh)
                sem_result['nodes'].extend(fresh.get('nodes', []))
                sem_result['edges'].extend(fresh.get('edges', []))
                sem_result['hyperedges'].extend(fresh.get('hyperedges', []))
                sem_result['input_tokens'] += fresh.get('input_tokens', 0)
                sem_result['output_tokens'] += fresh.get('output_tokens', 0)

        try:
            from graphify.cache import hash as file_hash
            _live_hashes: set[str] = set()

            for _kind in ('document', 'paper', 'image'):
                for _fp in files_by_type.get(_kind, []):
                    _abs = Path(_fp)

                    if not _abs.is_absolute():
                        _abs = Path(target) / _abs

                    if not _abs.is_file():
                        continue

                    try:
                        _live_hashes.add(file_hash(_abs, target, cache_root=out_root))
                    except OSError:
                        pass

            if has_path:
                prune_semantic_cache(out_root, _live_hashes)
        except Exception as exc:
            print(f'[graphify extract] warning: could not prune semantic cache: {exc}', file=sys.stderr)
        stages.mark('semantic extract')
        pg_result: dict = {'nodes': [], 'edges': []}

        if cli_postgres_dsn is not None:
            from graphify.pg_introspect import introspect_postgres
            print(f'[graphify extract] introspecting PostgreSQL schema...')

            try:
                pg_result = introspect_postgres(cli_postgres_dsn)
            except (ConnectionError, ImportError) as exc:
                print(f'error: {exc}', file=sys.stderr)
                sys.exit(1)
            print(f"[graphify extract] PostgreSQL: {len(pg_result['nodes'])} nodes, {len(pg_result['edges'])} edges")
        cargo_result: dict = {'nodes': [], 'edges': []}

        if cli_cargo:
            from graphify.langs import rust
            print('[graphify extract] introspecting Cargo workspace...')

            try:
                cargo_result = rust.cargo(target)
            except FileNotFoundError:
                print('[graphify extract] --cargo: no Cargo.toml at scan root, skipping crate edges')
            except (ConnectionError, ImportError, OSError) as exc:
                print(f'error: {exc}', file=sys.stderr)
                sys.exit(1)
            print(f"[graphify extract] Cargo: {len(cargo_result['nodes'])} nodes, {len(cargo_result['edges'])} edges")
        merged: dict = {'nodes': list(ast_result.get('nodes', [])) + list(sem_result.get('nodes', [])) + list(pg_result.get('nodes', [])) + list(cargo_result.get('nodes', [])), 'edges': list(ast_result.get('edges', [])) + list(sem_result.get('edges', [])) + list(pg_result.get('edges', [])) + list(cargo_result.get('edges', [])), 'hyperedges': list(sem_result.get('hyperedges', [])), 'input_tokens': ast_result.get('input_tokens', 0) + sem_result.get('input_tokens', 0), 'output_tokens': ast_result.get('output_tokens', 0) + sem_result.get('output_tokens', 0), 'extracted_sources': list(ast_result.get('extracted_sources', []))}
        graph_json_path = graphify_out / 'graph.json'
        analysis_path = graphify_out / '.graphify_analysis.json'
        _failed_ast_sources = list(ast_result.get('failed_sources') or [])
        _manifest_files = stampedfiles(files_by_type, sem_result, target, partial_source_files=_partial_semantic_files, failed_ast_sources=_failed_ast_sources)
        _stamped_semantic = {f for _flist in _manifest_files.values() for f in _flist}
        _cleared_semantic = {str(p) for p in semantic_files} - _stamped_semantic
        _cleared_ast = set(_failed_ast_sources)
        _scan_corpus = {f for _fl in files_by_type.values() for f in _fl} if has_path else None

        def invalidatemanifest() -> None:
            if has_path:
                return

            try:
                manifest_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f'error: could not invalidate file manifest: {exc}', file=sys.stderr)
                sys.exit(1)

        if no_cluster:
            from graphify.build import dedupeedges, dedupenodes
            from graphify.paths import backup

            if incremental_mode and (not code_files) and (not semantic_files) and (not deleted_files) and (not pg_result.get('nodes')) and (not pg_result.get('edges')) and (not cargo_result.get('nodes')) and (not cargo_result.get('edges')):
                if graph_stale_sources:
                    _n_pruned = prunesources(existing_graph_path, graph_stale_sources)

                    if _n_pruned:
                        print(f'[graphify extract] pruned {_n_pruned} node(s) from {len(graph_stale_sources)} source file(s) no longer in the scan (deleted or excluded).')
                print('[graphify extract] no incremental changes detected (--no-cluster); outputs left untouched.')

                try:
                    save_manifest(_manifest_files, manifest_path=str(manifest_path), kind='both', root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
                except Exception as exc:
                    print(f'[graphify extract] warning: could not write manifest: {exc}', file=sys.stderr)
                stages.total()
                sys.exit(0)

            if merge_existing_graph:
                from graphify.build import mergeraw
                _raw_prune_sources: list[str] = list(deleted_files)

                for _src in list(excluded_files) + graph_stale_sources:
                    if _src not in _raw_prune_sources:
                        _raw_prune_sources.append(_src)

                try:
                    merged = mergeraw(merged, graph_path=existing_graph_path, prune_sources=_raw_prune_sources or None, root=target)
                except RuntimeError as exc:
                    print(f'error: {exc}', file=sys.stderr)
                    sys.exit(1)
                _shrink = handleshrink(merged.get('_unverified_semantic_shrink'), cli_allow_partial=cli_allow_partial, files_by_type=files_by_type, sem_result=sem_result, target=target, partial_semantic_files=_partial_semantic_files, failed_ast_sources=_failed_ast_sources, semantic_files=semantic_files)

                if _shrink is not None and _shrink[0]:
                    _extraction_incomplete = True
                    _manifest_files = _shrink[1]
                    _stamped_semantic = {f for _flist in _manifest_files.values() for f in _flist}
                    _cleared_semantic = _shrink[2]
            merged['nodes'] = dedupenodes(merged['nodes'])
            merged['edges'] = dedupeedges(merged['edges'])
            from graphify.build import disambiguatenodes
            disambiguatenodes(merged['nodes'])
            from graphify.build import finalize
            finalize(merged)
            _node_sf = {n.get('id'): n.get('source_file') for n in merged['nodes']}

            for _e in merged['edges']:
                if not _e.get('source_file'):
                    _e['source_file'] = _node_sf.get(_e.get('source')) or _node_sf.get(_e.get('target')) or ''

            if _extraction_incomplete and (not cli_allow_partial):
                from graphify.export import MALFORMED_GRAPH as _MALFORMED_GRAPH
                _existing_n = _existing_graph_node_count(graph_json_path)
                _malformed = _existing_n is _MALFORMED_GRAPH
                _shrinks = isinstance(_existing_n, int) and len(merged['nodes']) < _existing_n

                if _malformed or _shrinks:
                    _detail = f'the existing {graph_json_path} is present but unparseable (corrupt or a mid-write), so a shrink cannot be ruled out' if _malformed else f"smaller than the existing {graph_json_path} ({len(merged['nodes'])} < {_existing_n} nodes)"
                    print(f'[graphify extract] error: extraction was incomplete (an AST/semantic pass failed) and the resulting --no-cluster graph is {_detail}. Refusing to overwrite a complete graph with a partial one. Re-run after fixing the failures, or pass --allow-partial to overwrite anyway.', file=sys.stderr)
                    sys.exit(1)
            backup(graphify_out)
            invalidatemanifest()
            from graphify.paths import json as write_json
            from graphify.extract import RUNKEYS
            write_json(graph_json_path, {k: v for k, v in merged.items() if k not in RUNKEYS}, indent=2)

            try:
                (graphify_out / '.graphify_root').write_text(str(Path(target).resolve()), encoding='utf-8')
            except OSError:
                pass
            stages.mark('write')
            cost = _estimate_cost(backend, merged['input_tokens'], merged['output_tokens'])
            print(f"[graphify extract] wrote {graph_json_path} — {len(merged['nodes'])} nodes, {len(merged['edges'])} edges (no clustering)")

            if merged['input_tokens'] or merged['output_tokens']:
                print(f"[graphify extract] tokens: {merged['input_tokens']:,} in / {merged['output_tokens']:,} out, est. cost: ${cost:.4f}")

            try:
                if has_path:
                    save_manifest(_manifest_files, manifest_path=str(manifest_path), kind='both', root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
            except Exception as exc:
                print(f'[graphify extract] warning: could not write manifest: {exc}', file=sys.stderr)

            if global_merge:
                from graphify.graph import global_add as _global_add
                _tag = global_repo_tag or target.name

                try:
                    result = _global_add(graphify_out / 'graph.json', _tag)

                    if result['skipped']:
                        print(f"[graphify global] '{_tag}' unchanged since last add - skipped.")
                    else:
                        print(f"[graphify global] '{_tag}' merged into global graph (+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
                except Exception as exc:
                    print(f'[graphify global] warning: failed to merge into global graph: {exc}', file=sys.stderr)
            stages.total()
            sys.exit(0)
        from graphify.build import build, buildjson, buildmerge
        from graphify.cluster import cluster as _cluster, score_all as _score_all
        from graphify.export import to_json as _to_json
        from graphify.analyze import god_nodes as _god_nodes, surprising_connections as _surprising
        dedup_backend = backend if dedup_llm else None

        if merge_existing_graph:
            _prune_sources: list[str] = list(deleted_files)

            for _src in list(excluded_files) + graph_stale_sources:
                if _src not in _prune_sources:
                    _prune_sources.append(_src)

            try:
                G = buildmerge([merged], graph_path=existing_graph_path, prune_sources=_prune_sources or None, dedup=not no_dedup, dedup_llm_backend=dedup_backend, root=target)
                _shrink = handleshrink(G.graph.get('_unverified_semantic_shrink') if hasattr(G, 'graph') else None, cli_allow_partial=cli_allow_partial, files_by_type=files_by_type, sem_result=sem_result, target=target, partial_semantic_files=_partial_semantic_files, failed_ast_sources=_failed_ast_sources, semantic_files=semantic_files)

                if _shrink is not None and _shrink[0]:
                    _extraction_incomplete = True
                    _manifest_files = _shrink[1]
                    _stamped_semantic = {f for _flist in _manifest_files.values() for f in _flist}
                    _cleared_semantic = _shrink[2]
            except ValueError as exc:
                print(f'[graphify extract] {exc}', file=sys.stderr)
                sys.exit(1)
        else:
            G = build([merged], dedup=not no_dedup, dedup_llm_backend=dedup_backend, root=target)
        stages.mark('build')

        if G.number_of_nodes() == 0:
            print('[graphify extract] graph is empty — extraction produced no nodes. Possible causes: all files skipped, binary-only corpus, or LLM returned no edges.', file=sys.stderr)
            sys.exit(1)
        communities = _cluster(G, resolution=cli_resolution, exclude_hubs_percentile=cli_exclude_hubs)
        stages.mark('cluster')
        cohesion = _score_all(G, communities)

        try:
            gods = _god_nodes(G, exclude_hubs_percentile=cli_exclude_hubs)
        except Exception:
            gods = []

        try:
            surprises = _surprising(G, communities)
        except Exception:
            surprises = []
        stages.mark('analyze')
        from graphify.paths import backup
        backup(graphify_out)
        invalidatemanifest()
        from graphify.build import entry as take_shrink_accounting
        _dedup_collapsed, _pruned_nodes = take_shrink_accounting(G)
        _force_write = cli_allow_partial or not _extraction_incomplete
        _dedup_shrink_counts: tuple[int, int] | None = None

        if not _extraction_incomplete and (not no_dedup) and (not cli_allow_dedup_shrink) and (_dedup_collapsed > 0):
            from graphify.export import existing_graph_node_count as _existing_graph_node_count
            _existing_n = _existing_graph_node_count(graph_json_path)
            _new_n = G.number_of_nodes()

            if isinstance(_existing_n, int) and _new_n < _existing_n - _pruned_nodes:
                _dedup_shrink_counts = (_existing_n, _new_n)

        if _dedup_shrink_counts is not None:
            _old_n, _new_n = _dedup_shrink_counts
            print(f'[graphify extract] error: dedup merged {_dedup_collapsed} node(s); writing would shrink the graph from {_old_n} nodes to {_new_n} nodes. Refusing to overwrite {graph_json_path}. Pass --allow-dedup-shrink to write the smaller graph.', file=sys.stderr)
            sys.exit(1)
        from graphify.watch import _git_head as _gh_target
        _wrote = _to_json(G, communities, str(graph_json_path), force=_force_write, built_at_commit=_gh_target(cwd=Path(target).resolve()))

        if not _wrote:
            print(f'[graphify extract] error: extraction was incomplete (an AST/semantic pass failed) and the resulting graph is smaller than the existing {graph_json_path}. Refusing to overwrite a complete graph with a partial one. Re-run after fixing the failures, or pass --allow-partial to overwrite anyway.', file=sys.stderr)
            sys.exit(1)

        try:
            (graphify_out / '.graphify_root').write_text(str(Path(target).resolve()), encoding='utf-8')
        except OSError:
            pass
        stages.mark('export')

        if merged.get('output_tokens', 0) > 0:
            (graphify_out / '.graphify_semantic_marker').write_text(json.dumps({'output_tokens': merged['output_tokens']}), encoding='utf-8')

        if global_merge:
            from graphify.graph import global_add as _global_add
            _tag = global_repo_tag or target.name

            try:
                result = _global_add(graphify_out / 'graph.json', _tag)

                if result['skipped']:
                    print(f"[graphify global] '{_tag}' unchanged since last add - skipped.")
                else:
                    print(f"[graphify global] '{_tag}' merged into global graph (+{result['nodes_added']} nodes, -{result['nodes_removed']} pruned).")
            except Exception as exc:
                print(f'[graphify global] warning: failed to merge into global graph: {exc}', file=sys.stderr)
        analysis = {'communities': {str(k): v for k, v in communities.items()}, 'cohesion': {str(k): v for k, v in cohesion.items()}, 'gods': gods, 'surprises': surprises, 'tokens': {'input': merged['input_tokens'], 'output': merged['output_tokens']}}
        from graphify.paths import json as write_json
        write_json(analysis_path, analysis, indent=2)

        try:
            if has_path:
                save_manifest(_manifest_files, manifest_path=str(manifest_path), kind='both', root=target, scan_corpus=_scan_corpus, clear_semantic=_cleared_semantic, clear_ast=_cleared_ast or None)
        except Exception as exc:
            print(f'[graphify extract] warning: could not write manifest: {exc}', file=sys.stderr)
        cost = _estimate_cost(backend, merged['input_tokens'], merged['output_tokens'])
        print(f'[graphify extract] wrote {graph_json_path}: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(communities)} communities')
        print(f'[graphify extract] wrote {analysis_path}')

        if incremental_mode:
            _excl_note = f', {len(excluded_files)} excluded' if excluded_files else ''
            print(f'[graphify extract] incremental summary: {sem_cache_hits + unchanged_total} files cached/unchanged, {len(code_files) + sem_cache_misses} re-extracted, {len(deleted_files)} deleted{_excl_note}')
        elif sem_cache_hits:
            print(f'[graphify extract] semantic cache: {sem_cache_hits} cached, {sem_cache_misses} re-extracted')

        print(f'[graphify extract] next: run `graphify cluster-only {graphify_out.parent}` to generate GRAPH_REPORT.md and name communities')
        cloudcta(graphify_out)
        stages.total()
    elif cmd == 'cache-check':
        from graphify.cache import check_semantic_cache

        if len(sys.argv) < 3:
            print('Usage: graphify cache-check <files_from> [--root <dir>] [--mode <m> | --deep] [--prompt-file <path>]', file=sys.stderr)
            sys.exit(1)
        files_from = Path(sys.argv[2])
        root = Path('.')
        cache_mode: str | None = None
        prompt_file: str | None = None
        i = 3

        while i < len(sys.argv):
            if sys.argv[i] == '--root' and i + 1 < len(sys.argv):
                root = Path(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--mode' and i + 1 < len(sys.argv):
                cache_mode = sys.argv[i + 1]
                i += 2
            elif sys.argv[i].startswith('--mode='):
                cache_mode = sys.argv[i].split('=', 1)[1]
                i += 1
            elif sys.argv[i] == '--deep':
                cache_mode = 'deep'
                i += 1
            elif sys.argv[i] == '--prompt-file' and i + 1 < len(sys.argv):
                prompt_file = sys.argv[i + 1]
                i += 2
            elif sys.argv[i].startswith('--prompt-file='):
                prompt_file = sys.argv[i].split('=', 1)[1]
                i += 1
            else:
                i += 1
        files = [f for f in files_from.read_text(encoding='utf-8').splitlines() if f.strip()]
        cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(files, root, mode=cache_mode, prompt_file=prompt_file)
        out = root / _GRAPHIFY_OUT
        out.mkdir(parents=True, exist_ok=True)

        if cached_nodes or cached_edges or cached_hyperedges:
            (out / '.graphify_cached.json').write_text(json.dumps({'nodes': cached_nodes, 'edges': cached_edges, 'hyperedges': cached_hyperedges}, ensure_ascii=False), encoding='utf-8')
        (out / '.graphify_uncached.txt').write_text('\n'.join(uncached), encoding='utf-8')
        print(f'Cache: {len(files) - len(uncached)} hit, {len(uncached)} miss')
    elif cmd == 'merge-chunks':
        import glob

        if len(sys.argv) < 3:
            print('Usage: graphify merge-chunks <chunk_files...> --out <path>', file=sys.stderr)
            sys.exit(1)
        out_path: Path | None = None
        chunk_args: list[str] = []
        i = 2

        while i < len(sys.argv):
            if sys.argv[i] == '--out' and i + 1 < len(sys.argv):
                out_path = Path(sys.argv[i + 1])
                i += 2
            else:
                chunk_args.append(sys.argv[i])
                i += 1

        if not out_path:
            print('error: --out <path> required', file=sys.stderr)
            sys.exit(1)
        chunk_files: list[str] = []

        for arg in chunk_args:
            expanded = glob.glob(arg)
            chunk_files.extend(sorted(expanded) if expanded else [arg])
        merged: dict = {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0}
        seen_ids: set[str] = set()
        valid_chunks = 0

        for cf in chunk_files:
            try:
                chunk = json.loads(Path(cf).read_text(encoding='utf-8'))
                _chunk_errs = []
            except Exception as e:
                chunk, _chunk_errs = {}, [str(e)]

            if _chunk_errs:
                print(f"[graphify merge-chunks] warning: skipping invalid chunk {cf}: {'; '.join(_chunk_errs[:3])}", file=sys.stderr)
                continue

            valid_chunks += 1

            for n in chunk.get('nodes', []):
                if n.get('id') not in seen_ids:
                    seen_ids.add(n['id'])
                    merged['nodes'].append(n)
            merged['edges'].extend(chunk.get('edges', []))
            merged['hyperedges'].extend(chunk.get('hyperedges', []))

            for _tok in ('input_tokens', 'output_tokens'):
                _v = chunk.get(_tok, 0)
                merged[_tok] += _v if isinstance(_v, (int, float)) else 0

        if not valid_chunks:
            print(f'[graphify merge-chunks] error: no valid chunks to merge; refusing to write {out_path}', file=sys.stderr)
            sys.exit(1)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from graphify.paths import json as write_json
        write_json(out_path, merged, ensure_ascii=False)
        chunk_summary = f'{valid_chunks} chunks' if valid_chunks == len(chunk_files) else f'{valid_chunks} of {len(chunk_files)} chunks'
        print(f"Merged {chunk_summary}: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges, {merged['input_tokens']:,} in / {merged['output_tokens']:,} out tokens")
    elif cmd == 'merge-semantic':
        if len(sys.argv) < 3:
            print('Usage: graphify merge-semantic --cached <path> --new <path> --out <path>', file=sys.stderr)
            sys.exit(1)
        cached_path: Path | None = None
        new_path: Path | None = None
        out_path2: Path | None = None
        i = 2

        while i < len(sys.argv):
            if sys.argv[i] == '--cached' and i + 1 < len(sys.argv):
                cached_path = Path(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--new' and i + 1 < len(sys.argv):
                new_path = Path(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == '--out' and i + 1 < len(sys.argv):
                out_path2 = Path(sys.argv[i + 1])
                i += 2
            else:
                i += 1

        if not out_path2:
            print('error: --out <path> required', file=sys.stderr)
            sys.exit(1)
        empty: dict = {'nodes': [], 'edges': [], 'hyperedges': []}
        cached_data = json.loads(cached_path.read_text(encoding='utf-8')) if cached_path and cached_path.exists() else empty
        new_data = json.loads(new_path.read_text(encoding='utf-8')) if new_path and new_path.exists() else empty
        seen_ids2: set[str] = set()
        all_nodes: list[dict] = []

        for n in cached_data.get('nodes', []) + new_data.get('nodes', []):
            if n.get('id') not in seen_ids2:
                seen_ids2.add(n['id'])
                all_nodes.append(n)
        merged2 = {'nodes': all_nodes, 'edges': cached_data.get('edges', []) + new_data.get('edges', []), 'hyperedges': cached_data.get('hyperedges', []) + new_data.get('hyperedges', [])}
        out_path2.parent.mkdir(parents=True, exist_ok=True)
        from graphify.paths import json as write_json
        write_json(out_path2, merged2, ensure_ascii=False)
        print(f"Merged: {len(merged2['nodes'])} nodes, {len(merged2['edges'])} edges")
    elif Path(cmd).exists() or cmd in ('.', '..') or cmd.startswith(('./', '../', '/', '~')):
        sys.argv.insert(2, sys.argv[1])
        sys.argv[1] = 'extract'
        reenter()
    else:
        print(f"error: unknown command '{cmd}'", file=sys.stderr)
        print("Run 'graphify --help' for usage.", file=sys.stderr)
        sys.exit(1)