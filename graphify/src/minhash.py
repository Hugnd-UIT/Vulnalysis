from __future__ import annotations

import hashlib
import struct
import numpy as np


PRIME = np.uint64((1 << 61) - 1)
MASK = np.uint64(4294967295)
TABLES: dict[int, tuple[np.ndarray, np.ndarray]] = {}
CACHE: dict[tuple[float, int], tuple[int, int]] = {}


def coeffs(perms: int) -> tuple[np.ndarray, np.ndarray]:
    if perms not in TABLES:
        rng = np.random.RandomState(1)
        a = rng.randint(1, int(PRIME), perms, dtype=np.uint64)
        b = rng.randint(0, int(PRIME), perms, dtype=np.uint64)
        TABLES[perms] = (a, b)

    return TABLES[perms]


def integrate(f, lo: float, hi: float, n: int = 128) -> float:
    h = (hi - lo) / n

    return h * sum((f(lo + i * h) for i in range(n)))


def optimal(threshold: float, perms: int) -> tuple[int, int]:
    key = (threshold, perms)

    if key in CACHE:
        return CACHE[key]

    best = (1, 1)
    lowest = float('inf')

    for b in range(1, perms + 1):
        for r in range(1, perms // b + 1):
            fp = integrate(lambda s, _b=float(b), _r=float(r): 1 - (1 - s ** _r) ** _b, 0.0, threshold)
            fn = integrate(lambda s, _b=float(b), _r=float(r): 1 - (1 - (1 - s ** _r) ** _b), threshold, 1.0)
            err = 0.5 * fp + 0.5 * fn

            if err < lowest:
                lowest = err
                best = (b, r)

    CACHE[key] = best

    return best


class MinHash:
    __slots__ = ('perms', 'values', 'a', 'b')

    def __init__(self, perms: int = 128) -> None:
        self.perms = perms
        self.values = np.full(perms, int(MASK), dtype=np.uint64)
        self.a, self.b = coeffs(perms)

    def update(self, val: bytes) -> None:
        hv = np.uint64(struct.unpack('<I', hashlib.sha1(val).digest()[:4])[0])
        phv = np.bitwise_and((self.a * hv + self.b) % PRIME, MASK)
        self.values = np.minimum(self.values, phv)

    @property
    def hashvalues(self) -> np.ndarray:
        return self.values

    @hashvalues.setter
    def hashvalues(self, val: np.ndarray) -> None:
        self.values = val


class MinHashLSH:

    def __init__(self, threshold: float = 0.5, perms: int = 128) -> None:
        self.b, self.r = optimal(threshold, perms)
        self.tables: list[dict[bytes, list[str]]] = [{} for _ in range(self.b)]
        self.keys: set[str] = set()

    def insert(self, key: str, item: MinHash) -> None:
        if key in self.keys:
            raise ValueError(f'Key {key!r} already exists')

        self.keys.add(key)
        hv = item.values

        for i, table in enumerate(self.tables):
            band = hv[i * self.r:(i + 1) * self.r].tobytes()
            table.setdefault(band, []).append(key)

    def query(self, item: MinHash) -> list[str]:
        hv = item.values
        matches: set[str] = set()

        for i, table in enumerate(self.tables):
            band = hv[i * self.r:(i + 1) * self.r].tobytes()
            matches.update(table.get(band, []))

        return list(matches)