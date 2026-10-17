import os
import sys
import logging

# Enable ANSI escape sequences on Windows
if os.name == "nt":
    os.system("")


class ColoredFormatter(logging.Formatter):
    AGENT_COLORS = {
        "vuln-planner": "\033[1;34m",      # Bold Blue
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
    CYAN = "\033[36m"
    BOLD_CYAN = "\033[1;36m"

    def format(self, record):
        time_str = self.formatTime(record, "%H:%M:%S")
        raw = record.getMessage().lstrip("\r\n")

        # Colorize headers
        if raw.startswith("===") and raw.endswith("==="):
            msg = f"{self.BOLD_CYAN}{raw}{self.RESET}"
        elif raw.startswith(">>> Processing:"):
            msg = f"{self.YELLOW}{raw}{self.RESET}"
        elif "Coordinator decision:" in raw:
            parts = raw.split("Coordinator decision:", 1)
            dec = parts[1].strip()
            if dec in ("CONFIRMED", "APPROVE"):
                c_dec = f"{self.GREEN}{dec}{self.RESET}"
            elif dec in ("REVISE", "REPLAN"):
                c_dec = f"{self.YELLOW}{dec}{self.RESET}"
            elif dec:
                c_dec = f"{self.RED}{dec}{self.RESET}"
            else:
                c_dec = f"{self.RED}(none){self.RESET}"
            msg = f"{parts[0]}Coordinator decision: {c_dec}"
        elif "Flow" in raw and "confirmed" in raw:
            msg = f"{self.GREEN}{raw}{self.RESET}"
        elif "Flow" in raw and "unconfirmed" in raw:
            msg = f"{self.RED}{raw}{self.RESET}"
        elif "Verifier produced report to Planner" in raw:
            msg = f"{self.GREEN}{raw}{self.RESET}"
        elif "Verifier produced feedback to PoV" in raw:
            msg = f"{self.YELLOW}{raw}{self.RESET}"
        elif raw.startswith("[+] Done."):
            msg = f"{self.GREEN}{raw}{self.RESET}"
        else:
            msg = raw

        # Colorize agent tag [agent-name]
        for agent, color in self.AGENT_COLORS.items():
            tag = f"[{agent}]"
            if tag in msg:
                msg = msg.replace(tag, f"{color}{tag}{self.RESET}")

        # Colorize Thought:, Action:, Error:
        if "Thought:" in msg:
            msg = msg.replace("Thought:", f"{self.BOLD_WHITE}Thought:{self.RESET}")
        if "Action:" in msg:
            msg = msg.replace("Action:", f"{self.YELLOW}Action:{self.RESET}")
            if "finish" in msg:
                msg = msg.replace("finish", f"{self.GREEN}finish{self.RESET}")
        if "Error:" in msg:
            msg = msg.replace("Error:", f"{self.RED}Error:{self.RESET}")

        # Prepend blank line (\n) for spacing between logs
        return f"\n{self.GREY}[{time_str}]{self.RESET} {msg}"


class HttpFilter(logging.Filter):
    def filter(self, record):
        return "HTTP Request:" not in record.getMessage()


def setup_logger():
    # Mute noisy HTTP libraries
    for lib in ["httpx", "httpx2", "httpcore", "httpcore2", "openai", "urllib3"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicates
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter(datefmt="%H:%M:%S"))
    handler.addFilter(HttpFilter())
    root.addHandler(handler)

    logger = logging.getLogger("vulnalysis")
    logger.setLevel(logging.INFO)
    return logger


__all__ = ["ColoredFormatter", "HttpFilter", "setup_logger"]
