"""The mail account: the address in .env, the password in the Keychain, never both.

Nothing here touches the real Keychain or the network — both are replaced for
every test in the file, so the test-email button cannot send anything.
"""
import json
import logging
from types import SimpleNamespace

import pytest

import mailer
import paths
import server

SECRET = "abcd efgh ijkl mnop"       # what an app password looks like
ADDRESS = "someone@gmail.com"


class FakeKeychain:
    """Stands in for the keyring module."""

    def __init__(self):
        self.store = {}
        self.broken = False

    def get_password(self, service, account):
        if self.broken:
            raise RuntimeError("the Keychain is locked")
        return self.store.get((service, account))

    def set_password(self, service, account, secret):
        if self.broken:
            raise RuntimeError("the Keychain is locked")
        self.store[(service, account)] = secret


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    fake = FakeKeychain()
    monkeypatch.setattr(mailer, "_KEYRING", fake)
    return fake


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    """Replaces smtplib.SMTP_SSL for every test, so nothing can be sent."""
    record = SimpleNamespace(connected=None, login=None, messages=[],
                             fail_login=None, fail_connect=None)

    class Fake:
        def __init__(self, host, port, timeout=None):
            if record.fail_connect:
                raise record.fail_connect
            record.connected = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, secret):
            if record.fail_login:
                raise record.fail_login
            record.login = (user, secret)

        def send_message(self, msg):
            record.messages.append(msg)

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Fake)
    return record


# ---------------------------------------------------------------- the server

def test_a_gmail_address_implies_the_server_the_port_and_the_sender(temp_env):
    for address in ("someone@gmail.com", "someone@googlemail.com", "A@GMail.com"):
        assert mailer.is_gmail(address), address
        where = mailer.smtp_settings(address)
        assert where == {"user": address, "host": "smtp.gmail.com", "port": 465,
                         "sender": address}


def test_another_provider_is_configured_in_env_not_in_the_menu(temp_env, monkeypatch):
    assert not mailer.is_gmail("someone@fastmail.com")
    # Nothing is implied for it, which is what makes the error below honest.
    assert mailer.smtp_settings("someone@fastmail.com")["host"] == ""

    monkeypatch.setenv("SMTP_HOST", "smtp.fastmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "Notes <notes@fastmail.com>")
    where = mailer.smtp_settings("someone@fastmail.com")
    assert where["host"] == "smtp.fastmail.com"
    assert where["port"] == 587
    assert where["sender"] == "Notes <notes@fastmail.com>"
    # ...and an override wins over Gmail's own defaults too.
    assert mailer.smtp_settings(ADDRESS)["host"] == "smtp.fastmail.com"


def test_a_provider_with_no_host_is_refused_before_connecting(temp_env, keychain, smtp):
    keychain.store[(mailer.KEYCHAIN_SERVICE, "someone@fastmail.com")] = SECRET

    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        mailer.send_smtp("s", "<p>h</p>", ["x@example.com"],
                         address="someone@fastmail.com")
    assert smtp.connected is None


# ---------------------------------------------------------------- the password

def test_saving_the_account_puts_the_password_in_the_keychain_only(temp_env, keychain):
    assert mailer.save_account(ADDRESS, SECRET) == ""

    settings = temp_env.read_text(encoding="utf-8")
    assert "SMTP_USER" in settings
    assert ADDRESS in settings
    assert SECRET not in settings
    assert "SMTP_PASSWORD" not in settings
    assert keychain.store == {(mailer.KEYCHAIN_SERVICE, ADDRESS): SECRET}
    assert mailer.password(ADDRESS) == SECRET
    # Saving an account is also saying "send over SMTP".
    assert mailer.mail_mode() == "smtp"


def test_an_address_without_an_at_sign_is_refused(temp_env, keychain):
    assert "email address" in mailer.save_account("not-an-address", SECRET)
    assert keychain.store == {}
    assert mailer.account() == ""


def test_saving_only_the_address_keeps_the_password_that_is_there(temp_env, keychain):
    keychain.store[(mailer.KEYCHAIN_SERVICE, ADDRESS)] = SECRET

    assert mailer.save_account(ADDRESS, "") == ""

    assert mailer.password(ADDRESS) == SECRET
    assert mailer.account() == ADDRESS


def test_a_new_address_with_no_password_anywhere_is_refused(temp_env):
    assert "no password saved" in mailer.save_account(ADDRESS, "")


def test_the_keychain_comes_before_the_env_fallback(temp_env, keychain, monkeypatch):
    paths.save_setting("SMTP_USER", ADDRESS)
    paths.save_setting("SMTP_PASSWORD", "the-old-one-from-env")

    assert mailer.password() == "the-old-one-from-env"      # the fallback
    assert mailer.password_in_env() is True

    keychain.store[(mailer.KEYCHAIN_SERVICE, ADDRESS)] = SECRET
    assert mailer.password() == SECRET                      # the Keychain wins


def test_an_old_env_password_can_be_moved_into_the_keychain(temp_env, keychain):
    paths.save_setting("SMTP_USER", ADDRESS)
    paths.save_setting("SMTP_PASSWORD", SECRET)
    assert SECRET in temp_env.read_text(encoding="utf-8")

    assert mailer.move_password_to_keychain() == ""

    settings = temp_env.read_text(encoding="utf-8")
    assert SECRET not in settings
    assert "SMTP_PASSWORD" not in settings
    assert keychain.store == {(mailer.KEYCHAIN_SERVICE, ADDRESS): SECRET}
    assert mailer.password() == SECRET                      # still works
    assert mailer.password_in_env() is False


def test_nothing_is_lost_when_the_keychain_cannot_be_written_to(temp_env, keychain):
    keychain.broken = True

    problem = mailer.save_account(ADDRESS, SECRET)

    assert "Keychain" in problem
    assert mailer.account() == ""                 # not half-saved
    assert mailer.mail_mode() == "outlook_draft"
    assert SECRET not in temp_env.read_text(encoding="utf-8")


def test_a_missing_keyring_falls_back_to_env_instead_of_breaking(temp_env, monkeypatch):
    monkeypatch.setattr(mailer, "_KEYRING", False)      # not usable here
    paths.save_setting("SMTP_USER", ADDRESS)
    paths.save_setting("SMTP_PASSWORD", "the-old-one-from-env")

    assert mailer.keychain_password(ADDRESS) == ""
    assert mailer.password() == "the-old-one-from-env"
    assert "Keychain" in mailer.save_account(ADDRESS, SECRET)


# ---------------------------------------------------------------- the test email

def test_the_test_email_goes_to_the_account_itself(temp_env, smtp):
    ok, message = mailer.send_test(ADDRESS, SECRET)

    assert ok, message
    assert smtp.connected == ("smtp.gmail.com", 465, 20)
    assert smtp.login == (ADDRESS, SECRET)
    assert len(smtp.messages) == 1
    sent = smtp.messages[0]
    assert sent["To"] == ADDRESS
    assert sent["From"] == ADDRESS
    assert sent["Subject"] == "Meeting Notes test"
    assert ADDRESS in message
    assert SECRET not in message


def test_the_test_email_uses_the_saved_password_when_none_is_typed(temp_env, keychain,
                                                                  smtp):
    keychain.store[(mailer.KEYCHAIN_SERVICE, ADDRESS)] = SECRET

    assert mailer.send_test(ADDRESS)[0] is True
    assert smtp.login == (ADDRESS, SECRET)


def test_a_rejected_password_says_what_to_do_and_what_was_said(temp_env, smtp):
    smtp.fail_login = mailer.smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted. BadCredentials")

    ok, message = mailer.send_test(ADDRESS, SECRET)

    assert ok is False
    assert "app password" in message                     # the human line
    assert "SMTPAuthenticationError" in message          # the exact error
    assert "BadCredentials" in message
    assert SECRET not in message


def test_an_unreachable_server_says_so(temp_env, smtp):
    smtp.fail_connect = TimeoutError("timed out")

    ok, message = mailer.send_test(ADDRESS, SECRET)

    assert ok is False
    assert "Could not reach" in message
    assert "TimeoutError: timed out" in message


def test_a_nonsense_address_never_reaches_the_server(temp_env, smtp):
    ok, message = mailer.send_test("nonsense", SECRET)

    assert ok is False
    assert "email address" in message
    assert smtp.connected is None


# ---------------------------------------------------------------- not anywhere else

def test_the_password_is_never_in_an_api_response(temp_env, keychain):
    mailer.save_account(ADDRESS, SECRET)

    payload = json.dumps(server.state())

    assert set(server.state()) == {"mail_mode", "recipients", "job"}
    assert SECRET not in payload
    assert "password" not in payload.lower()


def test_the_password_is_never_logged(temp_env, keychain, smtp, caplog):
    smtp.fail_login = mailer.smtplib.SMTPAuthenticationError(535, b"nope")
    with caplog.at_level(logging.DEBUG, logger="pipeline"):
        mailer.save_account(ADDRESS, SECRET)
        mailer.send_test(ADDRESS, SECRET)
        paths.save_setting("SMTP_PASSWORD", SECRET)
        mailer.move_password_to_keychain()

    assert SECRET not in caplog.text
    assert ADDRESS in caplog.text          # the address itself is not a secret
