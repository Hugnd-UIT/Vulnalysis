SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]


def apply(kwargs):
    opts = dict(kwargs)
    extra = opts.get("extra_body")
    if extra is None:
        opts["extra_body"] = {"safety_settings": SAFETY_SETTINGS}
    elif isinstance(extra, dict) and "safety_settings" not in extra:
        opts["extra_body"] = {**extra, "safety_settings": SAFETY_SETTINGS}
    return opts


__all__ = ["SAFETY_SETTINGS", "apply"]
