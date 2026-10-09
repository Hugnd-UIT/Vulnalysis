from __future__ import annotations

import re
import unicodedata


def normalize(val: str) -> str:
    cur = val

    for _ in range(6):
        nxt = unicodedata.normalize('NFKC', cur.casefold())

        if nxt == cur:
            break

        cur = nxt

    cur = re.sub(r'[^\w]+', '_', cur, flags=re.UNICODE)
    cur = re.sub(r'_+', '_', cur)

    return cur.strip('_')


def make(*parts: str) -> str:
    return normalize('_'.join((p.strip('_.') for p in parts if p)))