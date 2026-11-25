"""Cached, server-side channel release checks. Credentials never enter API responses."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from threading import Lock, Thread
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import __version__

from .release_policy import channel_from_directory, release_version, select_release, stable_version

if TYPE_CHECKING:
    from app.config import Settings

REPOSITORY = "tarobits/taros-label-room"


def selected_channel(settings: Settings) -> str:
    return channel_from_directory(settings.state_directory)


def fetch_release(settings: Settings, channel: str | None = None) -> str:
    token = settings.github_token.get_secret_value() if settings.github_token else ""
    if settings.github_token_file:
        token = settings.github_token_file.read_text().strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "label-server"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def page(number: int) -> Any:
        request = Request(
            f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100&page={number}",
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    return select_release(page, channel or selected_channel(settings))


class ReleaseChecker:
    def __init__(
        self, settings: Settings, fetch: Callable[[], str] | None = None, channel: str | None = None
    ):
        self.enabled = settings.check_for_updates
        self.settings = settings
        self.fetch = fetch
        self.fixed_channel = channel
        try:
            self.channel: str | None = channel or selected_channel(settings)
        except (OSError, ValueError, TypeError, AttributeError):
            self.channel = None
        self.generation = 0
        self.lock = Lock()
        self.next_check = 0.0
        self.checking = False
        self.latest: str | None = None
        self.state = "disabled" if not self.enabled else "checking"

    def refresh(self, channel: str | None = None, generation: int | None = None) -> None:
        channel = channel or self.channel
        generation = self.generation if generation is None else generation
        try:
            tag = self.fetch() if self.fetch else fetch_release(self.settings, channel)
            (stable_version if channel == "stable" else release_version)(tag)
        except Exception:
            # Exceptions/HTTP bodies can contain secrets; return only a generic state.
            with self.lock:
                if generation != self.generation:
                    return
                self.state = "unavailable"
                self.next_check = time.monotonic() + 900
                self.checking = False
        else:
            with self.lock:
                if generation != self.generation:
                    return
                self.latest = tag
                self.state = "available"
                self.next_check = time.monotonic() + 3600
                self.checking = False

    def status(self) -> dict[str, Any]:
        try:
            channel = self.fixed_channel or selected_channel(self.settings)
        except (OSError, ValueError, TypeError, AttributeError):
            channel = None
        with self.lock:
            if channel != self.channel:
                self.channel = channel
                self.generation += 1
                self.latest = None
                self.next_check = 0.0
                self.checking = False
            if channel is None:
                self.state = "unavailable"
            if (
                self.enabled
                and channel
                and not self.checking
                and time.monotonic() >= self.next_check
            ):
                self.checking = True
                self.state = "checking"
                Thread(
                    target=self.refresh,
                    args=(channel, self.generation),
                    name="release-check",
                    daemon=True,
                ).start()
            latest = self.latest
            return {
                "current_version": __version__,
                "latest_version": latest,
                "update_available": bool(
                    latest and release_version(latest) > release_version(__version__)
                ),
                "release_url": (
                    f"https://github.com/{REPOSITORY}/releases/tag/{quote(latest, safe='')}"
                    if latest
                    else None
                ),
                "check_status": self.state,
            }
