import os
import sys
import logging

if os.name == "nt":
    os.system("")

AGENT_COLORS = {
    "coordinator": "\033[1;34m",       # Bold Blue
    "vuln-analyzer": "\033[1;36m",     # Bold Cyan
    "pov-generator": "\033[1;35m",     # Bold Magenta
    "pov-verifier": "\033[1;32m",      # Bold Green
    "repo-builder": "\033[1;33m",      # Bold Yellow
    "repo-retriever": "\033[38;5;39m", # Bold Sky Blue
}
RESET = "\033[0m"
GREY = "\033[90m"
BOLD_WHITE = "\033[1;37m"
YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
BOLD_CYAN = "\033[1;36m"


def format_record(record):
    raw = record.getMessage().lstrip("\r\n")

    if raw.startswith("===") and raw.endswith("==="):
        msg = f"{BOLD_CYAN}{raw}{RESET}"
    elif raw.startswith(">>> Processing:"):
        msg = f"{YELLOW}{raw}{RESET}"
    elif "Coordinator:" in raw:
        parts = raw.split("Coordinator: ", 1)
        dec = parts[1].strip()
        if dec in ("CONFIRMED", "ACCEPT"):
            c_dec = f"{GREEN}{dec}{RESET}"
        elif dec in ("REVISE", "REGENERATE"):
            c_dec = f"{YELLOW}{dec}{RESET}"
        elif dec in ("REJECT", "UNCONFIRMED"):
            c_dec = f"{RED}{dec}{RESET}"
        elif dec:
            c_dec = f"{RED}{dec}{RESET}"
        else:
            c_dec = f"{RED}(none){RESET}"
        msg = f"{parts[0]}Coordinator: {c_dec}"
    elif "Flow" in raw and "confirmed" in raw:
        msg = f"{GREEN}{raw}{RESET}"
    elif "Flow" in raw and "unconfirmed" in raw:
        msg = f"{RED}{raw}{RESET}"
    elif "Verifier produced report to Coordinator" in raw:
        msg = f"{GREEN}{raw}{RESET}"
    elif "Verifier produced feedback to PoV" in raw:
        msg = f"{YELLOW}{raw}{RESET}"
    elif raw.startswith("[+] Done."):
        msg = f"{GREEN}{raw}{RESET}"
    else:
        msg = raw

    for agent, color in AGENT_COLORS.items():
        tag = f"[{agent}]"
        if tag in msg:
            msg = msg.replace(tag, f"{color}{tag}{RESET}")

    if "Thought:" in msg:
        msg = msg.replace("Thought:", f"{BOLD_WHITE}Thought:{RESET}")
    if "Action:" in msg:
        msg = msg.replace("Action:", f"{YELLOW}Action:{RESET}")
        if "finish" in msg:
            msg = msg.replace("finish", f"{GREEN}finish{RESET}")
    if "Error:" in msg:
        msg = msg.replace("Error:", f"{RED}Error:{RESET}")

    time_str = logging.Formatter().formatTime(record, "%H:%M:%S")
    return f"\n{GREY}[{time_str}]{RESET} {msg}"


def http_filter(record):
    return "HTTP Request:" not in record.getMessage()


def setup_logger():
    for lib in ["httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter()
    formatter.format = format_record

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(http_filter)
    root.addHandler(handler)

    logger = logging.getLogger("vulnalysis")
    logger.setLevel(logging.INFO)
    return logger


__all__ = ["setup_logger", "format_record", "http_filter"]
