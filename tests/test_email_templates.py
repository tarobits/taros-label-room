"""Email content stays readable, localized and inert in a multipart message."""

import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO
from typing import cast

import pytest
from conftest import install, job, profile
from PIL import Image
from sqlalchemy import select
from test_notification_routing import endpoint

from app.config import Settings
from app.i18n import render
from app.maintenance.recovery_api import deliver_link, password_changed_email
from app.models import PrinterState
from app.notifications.email_delivery import send_email
from app.notifications.email_templates import (
    EmailIllustration,
    illustration,
    notification_illustration,
    render_email,
)
from app.notifications.notification_routing import deliver_routed_pending
from app.printing.queue import record_blocker


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


@pytest.fixture
def smtp_mailbox(monkeypatch):
    sent = []
    closed = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def starttls(self, **kwargs):
            pass

        def send_message(self, message):
            sent.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    return sent, closed


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("purpose", ["verify", "reset"])
def test_account_email_delivers_same_action_in_html_and_plain_text(smtp_mailbox, language, purpose):
    sent, closed = smtp_mailbox
    settings = Settings(
        smtp_host="smtp.invalid",
        smtp_from="sender@example.invalid",
        public_url="https://labels.example.invalid/subpath/",
    )
    deliver_link(settings, "reader@example.invalid", purpose, "test-token", language)
    assert len(sent) == len(closed) == 1
    message = sent[0]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    document = Document(html)
    route = "verify-email" if purpose == "verify" else "reset-password"
    expected_link = f"https://labels.example.invalid/subpath/#/{route}?token=test-token"
    assert expected_link in plain
    assert [attrs["href"] for tag, attrs in document.tags if tag == "a"] == [expected_link]
    assert ("html", {"lang": language}) in document.tags
    assert render("emailMessages.linkExpiry", language) in "".join(document.text)
    assert render("emailMessages.ignoreRequest", language) in "".join(document.text)
    assert render(
        "emailMessages.verifyBody" if purpose == "verify" else "emailMessages.resetBody", language
    ) in "".join(document.text)
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "multipart/related",
    ]
    images = [attrs for tag, attrs in document.tags if tag == "img"]
    assert len(images) == 1 and images[0]["src"].startswith("cid:")
    assert images[0]["alt"] == ""
    parts = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert len(parts) == 1
    assert parts[0].get_payload(decode=True) == illustration(
        "check" if purpose == "verify" else "password"
    )
    assert all(tag not in ("script", "iframe", "form", "link") for tag, _ in document.tags)


@pytest.mark.parametrize("language", ["en", "de"])
def test_password_changed_email_retains_security_notice_without_a_fake_action(
    smtp_mailbox, language
):
    sent, closed = smtp_mailbox
    password_changed_email(
        Settings(smtp_host="smtp.invalid", smtp_from="sender@example.invalid"),
        "reader@example.invalid",
        language,
    )
    assert len(sent) == len(closed) == 1
    html = sent[0].get_body(preferencelist=("html",)).get_content()
    document = Document(html)
    assert render("recoveryMessages.changedBody", language) in "".join(document.text)
    assert not any(tag == "a" for tag, _ in document.tags)
    assert render("recoveryMessages.changedBody", language) in (
        sent[0].get_body(preferencelist=("plain",)).get_content()
    )
    parts = [part for part in sent[0].walk() if part.get_content_type() == "image/png"]
    assert len(parts) == 1 and parts[0].get_payload(decode=True) == illustration("password")


def test_untrusted_content_cannot_inject_markup_or_change_action_destination():
    content = '<img src="https://evil.invalid/track" onerror="alert(1)"> & ${heading}'
    url = 'https://labels.example.invalid/#/jobs?q="hello"&page=2'
    html = render_email(
        content,
        content + "\nnext line\n\nlast paragraph",
        action_url=url,
        action_label=content,
        note=content,
        footer=content,
    )
    document = Document(html)
    assert [attrs["href"] for tag, attrs in document.tags if tag == "a"] == [url]
    assert sum(tag == "img" for tag, _ in document.tags) == 1
    assert not any(key.startswith("on") for _, attrs in document.tags for key in attrs)
    assert "".join(document.text).count(content) == 6  # title, heading, body, action, note, footer
    assert sum(tag == "br" for tag, _ in document.tags) == 1
    assert "last paragraph" in document.text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,example",
        "//evil.invalid/path",
        "/relative",
        "https://",
        "https://user:secret@evil.invalid",
        "https://good.invalid\\@evil.invalid",
        "https://good.invalid/\nInjected",
        " https://good.invalid",
        "https://good.invalid/\x00",
    ],
)
def test_unsafe_actions_fail_before_smtp_and_do_not_prevent_the_next_email(smtp_mailbox, url):
    sent, closed = smtp_mailbox
    settings = Settings(smtp_host="smtp.invalid", smtp_from="sender@example.invalid")
    with pytest.raises(ValueError):
        send_email(
            settings,
            "reader@example.invalid",
            "Notice",
            "Text",
            html_body=render_email(
                "Notice",
                "Text",
                action_url=url,
                action_label="Open",
            ),
        )
    assert sent == closed == []
    send_email(
        settings,
        "reader@example.invalid",
        "Notice",
        "Text",
        html_body=render_email(
            "Notice",
            "Text",
            action_url="https://labels.example.invalid/",
            action_label="Open",
        ),
    )
    assert len(sent) == len(closed) == 1


def test_action_requires_both_destination_and_visible_label():
    with pytest.raises(ValueError):
        render_email("Notice", "Text", action_url="https://labels.example.invalid")
    with pytest.raises(ValueError):
        render_email("Notice", "Text", action_label="Open")
    assert not any(tag == "a" for tag, _ in Document(render_email("Notice", "Text")).tags)


@pytest.mark.parametrize("variant", ["default", "check", "warning", "password"])
def test_embedded_artwork_is_small_transparent_and_packaged(variant):
    data = illustration(variant)
    assert len(data) < 100_000
    with Image.open(BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (360, 240)
        assert image.mode == "RGBA"
        assert image.getchannel("A").getextrema() == (0, 255)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("general", "default"),
        ("approval_digest", "default"),
        ("empty_tape", "warning"),
        ("printer_blocked", "warning"),
        ("batch_review", "warning"),
        ("tape_waiting", "warning"),
        ("job_printed", "check"),
        ("batch_printed", "check"),
        ("future_event", "default"),
        ("Error: password check failed", "default"),
        ("../email-printer-warning.png", "default"),
    ],
)
def test_notification_artwork_uses_known_event_types_and_neutral_fallback(kind, expected):
    assert notification_illustration(kind) == expected


@pytest.mark.parametrize(
    "variant", ["", "../config.py", "/etc/passwd", "https://evil.invalid/a.png"]
)
def test_unknown_artwork_cannot_read_files_or_connect_to_smtp(smtp_mailbox, variant):
    sent, closed = smtp_mailbox
    settings = Settings(smtp_host="smtp.invalid", smtp_from="sender@example.invalid")
    with pytest.raises(ValueError, match="^Unknown email illustration$"):
        send_email(
            settings,
            "reader@example.invalid",
            "Test",
            "Body",
            artwork=cast(EmailIllustration, variant),
        )
    assert sent == closed == []
    send_email(settings, "reader@example.invalid", "Valid", "Body", artwork="check")
    assert len(sent) == len(closed) == 1


def test_concurrent_emails_keep_their_own_artwork_and_unique_content_ids(smtp_mailbox):
    sent, closed = smtp_mailbox
    settings = Settings(smtp_host="smtp.invalid", smtp_from="sender@example.invalid")
    variants: list[EmailIllustration] = ["default", "check", "warning", "password"] * 3

    def send(variant):
        send_email(settings, "reader@example.invalid", variant, "Body", artwork=variant)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send, variants))
    assert len(sent) == len(closed) == len(variants)
    content_ids = set()
    for message in sent:
        parts = [part for part in message.walk() if part.get_content_type() == "image/png"]
        assert len(parts) == 1
        assert parts[0].get_payload(decode=True) == illustration(message["Subject"])
        content_id = parts[0]["Content-ID"]
        assert content_id not in content_ids
        content_ids.add(content_id)
        html = message.get_body(preferencelist=("html",)).get_content()
        assert f'src="cid:{content_id[1:-1]}"' in html


def test_printer_problem_and_later_completion_deliver_different_artwork(client, smtp_mailbox):
    sent, _ = smtp_mailbox
    factory = client.app.state.factory
    settings = Settings(smtp_host="smtp.invalid", smtp_from="sender@example.invalid")
    now = time.time()
    with factory.begin() as session:
        endpoint(
            session,
            "mail",
            transport="email",
            email="reader@example.invalid",
            kinds=["printer_blocked", "batch_printed"],
        )
        record_blocker(session, session.scalar(select(PrinterState)), "Printer disconnected")
    deliver_routed_pending(factory, settings, now + 1)
    assert len(sent) == 1
    warning = next(part for part in sent[0].walk() if part.get_content_type() == "image/png")
    assert warning.get_payload(decode=True) == illustration("warning")
    with factory.begin() as session:
        record_blocker(session, session.scalar(select(PrinterState)), None)
    tape = profile(client, threshold=1)
    install(client, tape["id"])
    job(client, tape["id"])
    client.app.state.fleet.workers["main"].tick()
    deliver_routed_pending(factory, settings, time.time() + 2)
    assert len(sent) == 2
    success = next(part for part in sent[1].walk() if part.get_content_type() == "image/png")
    assert success.get_payload(decode=True) == illustration("check")


def test_smtp_send_failure_closes_connection_and_subsequent_email_is_complete(monkeypatch):
    attempts, closed = [], []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def send_message(self, message):
            attempts.append(message)
            if len(attempts) == 1:
                raise OSError("connection lost")

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    settings = Settings(
        smtp_host="smtp.invalid", smtp_from="sender@example.invalid", smtp_security="plain"
    )
    with pytest.raises(OSError):
        send_email(settings, "reader@example.invalid", "First", "First body")
    assert len(closed) == 1
    send_email(settings, "reader@example.invalid", "Second", "Second body")
    assert len(closed) == 2
    assert attempts[1].get_body(preferencelist=("plain",)).get_content().strip() == "Second body"
    assert "Second body" in attempts[1].get_body(preferencelist=("html",)).get_content()
    assert "First body" not in attempts[1].get_body(preferencelist=("html",)).get_content()
