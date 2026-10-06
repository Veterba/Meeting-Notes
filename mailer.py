"""Render the email from JSON and deliver it: Outlook draft or Gmail SMTP.

The mail account lives in two places on purpose: the address in `.env`, where it
is readable and backed up with everything else, and the app password in the macOS
Keychain, where it is not. Nothing here ever writes a password to `.env`, to the
log or into an API response.
"""
import logging
import os
import smtplib
import subprocess
import tempfile
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import paths
from paths import CODE, PEOPLE_FILE

log = logging.getLogger("pipeline")

_env = Environment(loader=FileSystemLoader(CODE / "templates"),
                   autoescape=select_autoescape(["html"]))

KEYCHAIN_SERVICE = "Meeting Notes"
# Addresses whose SMTP server, port and sender we already know.
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
GMAIL_HOST, GMAIL_PORT = "smtp.gmail.com", 465

# None = not tried yet, False = not usable here, otherwise the keyring module.
_KEYRING = None


def mail_mode() -> str:
    return paths.setting("MAIL_MODE", "outlook_draft")


# ---------------------------------------------------------------- the account

def _keyring():
    """The macOS Keychain, or None if keyring cannot be used here.

    The backend is chosen explicitly rather than by priority: keyring finds its
    backends through entry points, and a py2app bundle does not carry those.
    """
    global _KEYRING
    if _KEYRING is None:
        try:
            import keyring
            from keyring.backends import macOS
            keyring.set_keyring(macOS.Keyring())
            _KEYRING = keyring
        except Exception as e:
            log.error("No Keychain available (%s: %s)", type(e).__name__, e)
            _KEYRING = False
    return _KEYRING or None


def keychain_ready() -> bool:
    """Can the Keychain be used at all? Touches no item, so it cannot prompt.

    Worth knowing at startup: in a built .app this is what says whether keyring
    was packaged properly.
    """
    return _keyring() is not None


def account() -> str:
    """The address the notes are sent from."""
    return paths.setting("SMTP_USER").strip()


def keychain_password(address: str) -> str:
    ring = _keyring()
    if not (ring and address):
        return ""
    try:
        return ring.get_password(KEYCHAIN_SERVICE, address) or ""
    except Exception as e:
        log.error("Could not read the Keychain (%s: %s)", type(e).__name__, e)
        return ""


def set_keychain_password(address: str, secret: str) -> bool:
    ring = _keyring()
    if not ring:
        return False
    try:
        ring.set_password(KEYCHAIN_SERVICE, address, secret)
        return True
    except Exception as e:
        log.error("Could not write to the Keychain (%s: %s)", type(e).__name__, e)
        return False


def password(address: str = None) -> str:
    """The app password: the Keychain first, then .env as a fallback.

    The .env fallback is only there for an account set up before the Keychain was
    used; `move_password_to_keychain` is offered once to get rid of it.
    """
    address = address or account()
    return keychain_password(address) or paths.setting("SMTP_PASSWORD")


def password_in_env() -> bool:
    return bool(paths.setting("SMTP_PASSWORD"))


def save_account(address: str, secret: str) -> str:
    """Store the account and switch to SMTP. Returns "" or what went wrong."""
    address = (address or "").strip()
    if "@" not in address:
        return "That does not look like an email address."
    if secret:
        if not set_keychain_password(address, secret):
            return ("The Keychain could not be written to, so the password was "
                    "not saved. The log has the reason.")
        # It is in the Keychain now, so it has no business being in .env too.
        if password_in_env():
            paths.remove_setting("SMTP_PASSWORD")
    elif not password(address):
        return "There is no password saved for that address yet."
    paths.save_setting("SMTP_USER", address)
    paths.save_setting("MAIL_MODE", "smtp")
    log.info("Mail account set to %s, sending over SMTP", address)
    return ""


def move_password_to_keychain() -> str:
    """The one-time move of an old .env password. Returns "" or what went wrong."""
    address, secret = account(), paths.setting("SMTP_PASSWORD")
    if not (address and secret):
        return "There is no address and password in .env to move."
    if not set_keychain_password(address, secret):
        return "The Keychain could not be written to. The log has the reason."
    paths.remove_setting("SMTP_PASSWORD")
    log.info("Moved the SMTP password for %s out of .env and into the Keychain",
             address)
    return ""


def is_gmail(address: str) -> bool:
    return (address or "").strip().lower().rsplit("@", 1)[-1] in GMAIL_DOMAINS


def smtp_settings(address: str = None) -> dict:
    """Where to send from, and through what.

    A Gmail address implies the server, the port and the sender, which is why the
    sheet asks for nothing else. SMTP_HOST / SMTP_PORT / SMTP_FROM in .env stay as
    overrides for any other provider; they are deliberately not in the UI.
    """
    address = (address or account()).strip()
    host = paths.setting("SMTP_HOST") or (GMAIL_HOST if is_gmail(address) else "")
    port = paths.setting_int("SMTP_PORT", GMAIL_PORT)
    return {"user": address, "host": host, "port": port,
            "sender": paths.setting("SMTP_FROM") or address}


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

    for entry in split_addresses(paths.setting("RECIPIENTS")):
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


def send_smtp(subject: str, html: str, recipients: list, address: str = None,
              secret: str = None, timeout: int = 60) -> str:
    """Send one message. `address` and `secret` are for the test from the sheet,
    where the account has not been saved yet."""
    where = smtp_settings(address)
    user = where["user"]
    secret = secret or password(user)
    if not user:
        raise RuntimeError("No mail account yet — set one in Settings ▸ Mail account…")
    if not secret:
        raise RuntimeError(f"No app password saved for {user}")
    if not where["host"]:
        raise RuntimeError(f"{user} is not a Gmail address, so SMTP_HOST has to be "
                           "set in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = where["sender"]
    msg["To"] = ", ".join(recipients)
    msg.set_content("This message is HTML. Open it in a client that renders HTML.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(where["host"], where["port"], timeout=timeout) as s:
        s.login(user, secret)
        s.send_message(msg)
    return f"Sent over SMTP to {len(recipients)} recipients"


def human_smtp_error(exc: Exception, address: str = "") -> str:
    """One plain sentence about what to do, beside the server's own words."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if is_gmail(address):
            return ("Gmail rejected the password: use an app password from "
                    "myaccount.google.com/apppasswords, not your normal one.")
        return "The server rejected that address and password."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"The server will not send as {address}."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The server would not accept the recipient."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("The server hung up. On port 587 the connection has to start "
                "unencrypted and this only speaks SSL, so try port 465.")
    if isinstance(exc, (TimeoutError, OSError)):
        return "Could not reach the mail server — check the port and your network."
    return "Sending failed before the message went anywhere."


def send_test(address: str, secret: str = None) -> tuple:
    """Send a short message from the account to itself. Returns (ok, what to show).

    Only ever called from the button in the sheet — the tests mock smtplib.
    """
    address = (address or "").strip()
    if "@" not in address:
        return False, "That does not look like an email address."
    try:
        send_smtp("Meeting Notes test",
                  "<p>Meeting Notes can send mail from this account.</p>",
                  [address], address=address, secret=secret, timeout=20)
    except Exception as e:
        log.error("Test email to %s failed (%s)", address, type(e).__name__)
        return False, f"{human_smtp_error(e, address)}\n\n{type(e).__name__}: {e}"
    log.info("Test email sent to %s", address)
    return True, f"Sent. Look for “Meeting Notes test” in {address}."


def deliver(data: dict, recipients: list, mode: str = None) -> str:
    """The single delivery point. Only ever called from a button in the UI."""
    mode = mode or mail_mode()
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
