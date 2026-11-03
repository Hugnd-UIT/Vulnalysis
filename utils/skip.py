import re

NON_PROD = re.compile(
    r'(^|/)\.?[^/]*test[^/]*/'
    r'|'
    r'(^|/)\.?[^/]*(?:mock|fake|stub)[^/]*/'
    r'|'
    r'(^|/)\.?[^/]*(?:fixture|fuzz|benchmark|bench)[^/]*/'
    r'|'
    r'(^|/)\.?(?:docs?|documentation|site|manuals?)(/|$)'
    r'|'
    r'(^|/)[^/]*[-_.](?:test|spec|mock)\.[^/]+$'
    r'|'
    r'(^|/)(?:test|mock)[-_][^/]+$'
    r'|'
    r'(^|/)conftest\.py$',
    re.IGNORECASE,
)


def is_non_prod(path):
    if not path:
        return False
    p = str(path).replace("\\", "/").strip().lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    return bool(NON_PROD.search(p))


__all__ = ["NON_PROD", "is_non_prod"]