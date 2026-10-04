"""Render the email from JSON and deliver it: Outlook draft or Gmail SMTP."""
import os
import smtplib
import subprocess
import tempfile
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

import yaml
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

from paths import CODE, ENV_FILE, PEOPLE_FILE

load_dotenv(ENV_FILE)

BASE = CODE
MAIL_MODE = os.getenv("MAIL_MODE", "outlook_draft")

_env = Environment(loader=FileSystemLoader(CODE / "templates"),
                   autoescape=select_autoescape(["html"]))


def load_people() -> dict:
    path = PEOPLE_FILE if PEOPLE_FILE.exists() else CODE / "config" / "people.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).strip(): str(v).strip() for k, v in (data.get("people") or {}).items()}


def as_entry(name: str, addr: str) -> str:
    """('Ivan Petrov', 'i@x.com') -> 'Ivan Petrov <i@x.com>'.

    The name stays unicode: email.message encodes it for the header, and Outlook
    takes it as is.
    """
    name, addr = (name or "").strip(), (addr or "").strip()
    if not name:
        return addr
    if any(c in name for c in '<>,;:"\\'):
        name = '"' + name.replace("\\", "").replace('"', "") + '"'
    return f"{name} <{addr}>"


def split_addresses(raw: str) -> list:
    """'a@b.com, Name <c@d.com>; e@f.com' -> ['a@b.com', 'Name <c@d.com>', 'e@f.com']"""
    pairs = getaddresses([(raw or "").replace(";", ",")])
    return [as_entry(name, addr) for name, addr in pairs if addr.strip()]


def email_of(entry: str) -> str:
    """'Name <a@b.com>' -> 'a@b.com'. For comparison and for Outlook."""
    return parseaddr(entry)[1].strip()


def resolve_recipients(participants) -> list:
    """RECIPIENTS from .env plus participants matched in people.yaml.

    Both a bare address and the "Name <address>" form are accepted everywhere.
    """
    out, seen = [], set()

    def add(entry: str):
        addr = email_of(entry).lower()
        if addr and addr not in seen:
            seen.add(addr)
            out.append(entry)

    for entry in split_addresses(os.getenv("RECIPIENTS", "")):
        add(entry)

    people = load_people()
    lowered = {k.lower(): (k, v) for k, v in people.items()}
    for name in participants or []:
        key = str(name).strip().lower()
        hit = lowered.get(key)
        if not hit:  # partial match: first or last name alone
            for pname, pair in lowered.items():
                if key and (key in pname or pname in key):
                    hit = pair
                    break
        if hit:
            pretty, value = hit
            # bare address on the right — take the display name from the key
            add(value if email_of(value) != value else as_entry(pretty, value))
    return out


def render(data: dict) -> tuple:
    """-> (subject, html)"""
    subject = f"Meeting notes: {data.get('title') or 'meeting'}"
    if data.get("date"):
        subject += f" ({data['date']})"
    html = _env.get_template("email.html").render(**data)
    return subject, html


def _osa_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def create_outlook_draft(subject: str, html: str, recipients: list) -> str:
    """Create a draft in Outlook for Mac. Never sends.

    The script goes through a temp file because the email HTML is full of quotes
    and newlines that would not survive being passed as a command line argument.
    """
    lines = [
        'tell application "Microsoft Outlook"',
        '  activate',
        f'  set newMsg to make new outgoing message with properties '
        f'{{subject:"{_osa_escape(subject)}", content:"{_osa_escape(html)}"}}',
    ]
    for entry in recipients:
        name, addr = parseaddr(entry)
        props = f'address:"{_osa_escape(addr)}"'
        if name:
            props = f'name:"{_osa_escape(name)}", ' + props
        lines.append(
            f'  make new recipient at newMsg with properties '
            f'{{email address:{{{props}}}}}')
    lines += ['  open newMsg', 'end tell']
    script = "\n".join(lines)

    with tempfile.NamedTemporaryFile("w", suffix=".applescript",
                                     encoding="utf-8", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"osascript: {r.stderr.strip()}")
        return "Draft created in Outlook"
    finally:
        os.unlink(path)


def send_smtp(subject: str, html: str, recipients: list) -> str:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("SMTP_USER / SMTP_PASSWORD are not set in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", user)
    msg["To"] = ", ".join(recipients)
    msg.set_content("This message is HTML. Open it in a client that renders HTML.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(host, port, timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)
    return f"Sent over SMTP to {len(recipients)} recipients"


def deliver(data: dict, recipients: list, mode: str = None) -> str:
    """The single delivery point. Only ever called from a button in the UI."""
    mode = mode or MAIL_MODE
    if not recipients:
        raise RuntimeError("Recipient list is empty")
    bad = [r for r in recipients if "@" not in email_of(r)]
    if bad:
        raise RuntimeError("These do not look like addresses: " + ", ".join(bad))
    subject, html = render(data)
    if mode == "outlook_draft":
        return create_outlook_draft(subject, html, recipients)
    if mode == "smtp":
        return send_smtp(subject, html, recipients)
    raise RuntimeError(f"Unknown MAIL_MODE: {mode}")
