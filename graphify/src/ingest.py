from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import uuid

from graphify.security import fetch, text as fetch_text, validate


def escape(val: str) -> str:
    if val is None:
        return ''

    out: list[str] = []

    for ch in str(val):
        cp = ord(ch)

        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif ch == '\x00':
            out.append('\\0')
        elif cp == 8232:
            out.append('\\L')
        elif cp == 8233:
            out.append('\\P')
        elif cp < 32 or cp == 127:
            out.append(f'\\x{cp:02x}')
        else:
            out.append(ch)

    return ''.join(out)


def filename(url: str, ext: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = parsed.netloc + parsed.path
    name = re.sub(r'[^\w\-]', '_', name).strip('_')
    name = re.sub(r'_+', '_', name)[:80]

    return name + ext


def match(host: str, *domains: str) -> bool:
    return any((host == d or host.endswith(f'.{d}') for d in domains))


def kind(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or '').rstrip('.')

    if match(host, 'twitter.com', 'x.com'):
        return 'tweet'

    if match(host, 'arxiv.org'):
        return 'arxiv'

    if match(host, 'github.com'):
        return 'github'

    if match(host, 'youtube.com', 'youtu.be'):
        return 'youtube'

    path = parsed.path.lower()

    if path.endswith('.pdf'):
        return 'pdf'

    if any((path.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.webp', '.gif'))):
        return 'image'

    return 'webpage'


def html(url: str) -> str:
    return fetch_text(url)


def markdown(body: str, url: str) -> str:
    body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE)

    try:
        from markdownify import markdownify

        return markdownify(body, heading_style='ATX', bullets='-', strip=['img'])
    except ImportError:
        text = re.sub(r'<[^>]+>', ' ', body)
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:8000]


def tweet(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or '').rstrip('.')

    if match(host, 'x.com'):
        host = host[:-len('x.com')] + 'twitter.com'
        netloc = f'{host}:{parts.port}' if parts.port else host
        parts = parts._replace(netloc=netloc)

    target = urllib.parse.urlunsplit(parts)
    api = f'https://publish.twitter.com/oembed?url={urllib.parse.quote(target)}&omit_script=true'

    try:
        data = json.loads(fetch_text(api))
        body = re.sub(r'<[^>]+>', '', data.get('html', '')).strip()
        writer = data.get('author_name', 'unknown')
    except Exception:
        body = f'Tweet at {url}'
        writer = 'unknown'

    now = datetime.now(timezone.utc).isoformat()
    text = f'---\nsource_url: "{escape(url)}"\ntype: tweet\nauthor: "{escape(writer)}"\ncaptured_at: {now}\ncontributor: "{escape(contributor or author or "unknown")}"\n---\n\n# Tweet by @{writer}\n\n{body}\n\nSource: {url}\n'
    dest = filename(url, '.md')

    return (text, dest)


def webpage(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    body = html(url)
    match_title = re.search(r'<title[^>]*>(.*?)</title>', body, re.IGNORECASE | re.DOTALL)
    title = re.sub(r'\s+', ' ', match_title.group(1)).strip() if match_title else url
    md = markdown(body, url)
    now = datetime.now(timezone.utc).isoformat()
    text = f'---\nsource_url: "{escape(url)}"\ntype: webpage\ntitle: "{escape(title)}"\ncaptured_at: {now}\ncontributor: "{escape(contributor or author or "unknown")}"\n---\n\n# {title}\n\nSource: {url}\n\n---\n\n{md[:12000]}\n'
    dest = filename(url, '.md')

    return (text, dest)


def arxiv(url: str, author: str | None, contributor: str | None) -> tuple[str, str]:
    found = re.search(r'(\d{4}\.\d{4,5})', url)

    if found:
        api = f'https://export.arxiv.org/abs/{found.group(1)}'

        try:
            body = html(api)
            m_abs = re.search(r'class="abstract[^"]*"[^>]*>(.*?)</blockquote>', body, re.DOTALL | re.IGNORECASE)
            abstract = re.sub(r'<[^>]+>', '', m_abs.group(1)).strip() if m_abs else ''
            m_title = re.search(r'class="title[^"]*"[^>]*>(.*?)</h1>', body, re.DOTALL | re.IGNORECASE)
            title = re.sub(r'\s+', ' ', m_title.group(1)).strip() if m_title else found.group(1)
            m_auth = re.search(r'class="authors"[^>]*>(.*?)</div>', body, re.DOTALL | re.IGNORECASE)
            writers = re.sub(r'<[^>]+>', '', m_auth.group(1)).strip() if m_auth else ''
        except Exception:
            title, abstract, writers = (found.group(1), '', '')
    else:
        return webpage(url, author, contributor)

    now = datetime.now(timezone.utc).isoformat()
    text = f'---\nsource_url: "{escape(url)}"\narxiv_id: "{escape(found.group(1) if found else "")}"\ntype: paper\ntitle: "{escape(title)}"\npaper_authors: "{escape(writers)}"\ncaptured_at: {now}\ncontributor: "{escape(contributor or author or "unknown")}"\n---\n\n# {title}\n\n**Authors:** {writers}\n**arXiv:** {(found.group(1) if found else url)}\n\n## Abstract\n\n{abstract}\n\nSource: {url}\n'
    dest = f"arxiv_{found.group(1).replace('.', '_')}.md" if found else filename(url, '.md')

    return (text, dest)


def download(url: str, ext: str, target: Path) -> Path:
    dest = target / filename(url, ext)
    dest.write_bytes(fetch(url))

    return dest


def ingest(url: str, target: Path, author: str | None = None, contributor: str | None = None) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    mode = kind(url)

    try:
        validate(url)
    except ValueError as exc:
        raise ValueError(f'ingest: {exc}') from exc

    try:
        if mode == 'pdf':
            return download(url, '.pdf', target)

        if mode == 'image':
            ext = Path(urllib.parse.urlparse(url).path).suffix or '.jpg'

            return download(url, ext, target)

        if mode == 'youtube':
            from graphify.transcribe import download_audio

            return download_audio(url, target)

        if mode == 'tweet':
            body, dest = tweet(url, author, contributor)
        elif mode == 'arxiv':
            body, dest = arxiv(url, author, contributor)
        else:
            body, dest = webpage(url, author, contributor)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f'ingest: failed to fetch {url!r}: {exc}') from exc

    loc = target / dest
    idx = 1

    while loc.exists() and idx < 1000:
        base = Path(dest).stem
        loc = target / f'{base}_{idx}.md'
        idx += 1

    loc.write_text(body, encoding='utf-8')

    return loc


OUTCOMES = ('useful', 'dead_end', 'corrected')


def record(question: str, answer: str, dest: Path, mode: str = 'query', nodes: list[str] | None = None, outcome: str | None = None, correction: str | None = None) -> Path:
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f'outcome must be one of {OUTCOMES}')

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    slug = re.sub(r'[^\w]', '_', question.lower())[:50].strip('_')
    name = f"query_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{slug}.md"
    head = ['---', f'type: "{mode}"', f'date: "{now.isoformat()}"', f'question: "{escape(question)}"', 'contributor: "graphify"']

    if outcome:
        head.append(f'outcome: "{escape(outcome)}"')

    if correction:
        head.append(f'correction: "{escape(correction)}"')

    if nodes:
        node_str = ', '.join((f'"{escape(n)}"' for n in nodes[:10]))
        head.append(f'source_nodes: [{node_str}]')

    head.append('---')
    body = ['', f'# Q: {question}', '', '## Answer', '', answer]

    if outcome or correction:
        body += ['', '## Outcome', '']

        if outcome:
            body.append(f'- Signal: {outcome}')

        if correction:
            body.append(f'- Correction: {correction}')

    if nodes:
        body += ['', '## Source Nodes', '']
        body += [f'- {n}' for n in nodes]

    final = '\n'.join(head + body)
    loc = dest / name
    loc.write_text(final, encoding='utf-8')

    return loc