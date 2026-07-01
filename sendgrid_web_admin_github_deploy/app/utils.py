import re
import random
import socket
from datetime import datetime

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def is_valid_email(email):
    return bool(EMAIL_RE.match((email or "").strip()))


def parse_recipient_line(line):
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if "," in text:
        email, name = text.split(",", 1)
        email = email.strip().lower()
        name = name.strip()
    else:
        parts = text.split()
        email = parts[0].strip().lower()
        name = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
    if not is_valid_email(email):
        return None
    return email, name


def code8():
    return str(random.randint(10000000, 99999999))


def render_vars(text, variables):
    if text is None:
        return None
    result = str(text)
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
        result = result.replace("{{ " + key + " }}", str(value))
    return result


def is_port_free(host, port):
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        return sock.connect_ex((check_host, port)) != 0
    finally:
        sock.close()


def find_available_port(host, start_port, scan_limit, auto_increment):
    if not auto_increment:
        return start_port
    for port in range(start_port, start_port + max(1, scan_limit)):
        if is_port_free(host, port):
            return port
    raise RuntimeError("No available port found.")
