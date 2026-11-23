"""Escaped, progressively enhanced email markup with a self-contained illustration."""

from functools import lru_cache
from html import escape
from importlib.resources import files
from string import Template
from typing import Literal
from urllib.parse import urlsplit

from app.i18n import Language
from app.notifications.notification_types import PRINT_COMPLETION_KINDS

ILLUSTRATION_CID = "label-printer@taros-label-room"
EmailIllustration = Literal["default", "check", "warning", "password"]
_ILLUSTRATION_FILES: dict[EmailIllustration, str] = {
    "default": "email-printer.png",
    "check": "email-printer-check.png",
    "warning": "email-printer-warning.png",
    "password": "email-printer-password.png",
}


@lru_cache(maxsize=4)
def illustration(variant: EmailIllustration = "default") -> bytes:
    # Only known packaged assets can be selected; never interpolate a caller's path.
    if variant not in _ILLUSTRATION_FILES:
        raise ValueError("Unknown email illustration")
    return files("app.notifications").joinpath("assets", _ILLUSTRATION_FILES[variant]).read_bytes()


def notification_illustration(kind: str) -> EmailIllustration:
    """Use event types, never translated titles or user-supplied message text."""
    if kind in PRINT_COMPLETION_KINDS:
        return "check"
    if kind in {"empty_tape", "printer_blocked", "batch_review", "tape_waiting"}:
        return "warning"
    return "default"


@lru_cache(maxsize=1)
def _template() -> Template:
    return Template(files("app.notifications").joinpath("templates/email.html").read_text("utf-8"))


def render_email(
    heading: str,
    body: str,
    language: Language = "en",
    *,
    action_url: str | None = None,
    action_label: str | None = None,
    note: str | None = None,
    footer: str | None = None,
) -> str:
    """Only explicit HTTP(S) actions become links; user content remains literal text."""
    action = ""
    if action_url is not None:
        url = urlsplit(action_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or any(char.isspace() or ord(char) < 32 or char == "\\" for char in action_url)
            or not action_label
        ):
            raise ValueError("Invalid email action")
        # Inline colors and table layout survive clients that discard the stylesheet.
        action = (
            '<table role="presentation" cellspacing="0" cellpadding="0" border="0"'
            ' style="margin:24px 0 0;max-width:100%"><tr>'
            '<td class="action" bgcolor="#4f46e5" style="border-radius:6px;'
            'background-color:#4f46e5;text-align:center;mso-padding-alt:14px 22px">'
            f'<a class="action" href="{escape(action_url, quote=True)}" '
            'style="display:inline-block;padding:14px 22px;border-radius:6px;'
            "background-color:#4f46e5;color:#ffffff;font-size:16px;font-weight:600;"
            'line-height:24px;text-decoration:none;overflow-wrap:anywhere">'
            f"{escape(action_label)}</a></td></tr></table>"
        )
    elif action_label is not None:
        raise ValueError("Email action label requires a URL")

    def paragraphs(value: str) -> str:
        return "".join(
            '<p style="margin:0 0 16px">' + escape(paragraph).replace("\n", "<br>") + "</p>"
            for paragraph in value.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
            if paragraph
        )

    return _template().substitute(
        language=escape(language, quote=True),
        heading=escape(heading),
        body=paragraphs(body),
        illustration_cid=ILLUSTRATION_CID,
        action=action,
        note=(
            '<p class="muted" style="margin:24px 0 0;color:#60616c;font-size:14px;'
            f'line-height:22px">{escape(note)}</p>'
            if note
            else ""
        ),
        footer=(
            '<tr><td class="footer muted" style="padding:24px 32px 28px;'
            "border-top:2px dashed #d8d5d0;color:#60616c;font-size:13px;"
            f'line-height:21px;overflow-wrap:anywhere;word-break:break-word">{escape(footer)}</td></tr>'
            if footer
            else '<tr><td class="perforation" height="20" style="height:20px;'
            'border-top:2px dashed #d8d5d0;font-size:0;line-height:0">&nbsp;</td></tr>'
        ),
    )
