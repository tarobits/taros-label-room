"""Dependency-free release policy shared by deployment tools and the server."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

Version = tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]


class NoEligibleRelease(ValueError):
    pass


def channel_from_directory(directory: Path) -> str:
    path = directory / ".label-release-channel.json"
    try:
        state = json.loads(path.read_text())
        if (
            path.is_symlink()
            or state.get("schema") != 1
            or state.get("channel") not in ("stable", "beta")
        ):
            raise ValueError("Invalid channel state")
        channel: str = state["channel"]
        return channel
    except FileNotFoundError:
        return "stable"


def release_version(value: Any) -> Version:
    match = (
        re.fullmatch(
            r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
            value,
        )
        if isinstance(value, str)
        else None
    )
    if not match:
        raise ValueError("Expected a semantic version")
    identifiers = match[4].split(".") if match[4] else []
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in identifiers):
        raise ValueError("Numeric prerelease identifiers cannot have leading zeros")
    return (
        int(match[1]),
        int(match[2]),
        int(match[3]),
        int(not identifiers),
        tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers),
    )


def stable_version(value: Any) -> tuple[int, int, int]:
    version = release_version(value)
    if not version[3]:
        raise ValueError("Expected a stable semantic version")
    return version[:3]


def select_release(fetch_page: Callable[[int], Any], channel: str = "stable") -> str:
    if channel not in ("stable", "beta"):
        raise ValueError("Invalid release channel")
    candidates = []
    for page in range(1, 101):
        releases = fetch_page(page)
        if not isinstance(releases, list):
            raise ValueError("Invalid release response")
        for release in releases:
            if not isinstance(release, dict):
                raise ValueError("Invalid release response")
            if release.get("draft") is not False or not release.get("published_at"):
                continue
            if not isinstance(release.get("prerelease"), bool):
                continue
            try:
                version = release_version(release.get("tag_name"))
            except ValueError:
                continue
            if channel == "stable" and (not version[3] or release["prerelease"]):
                continue
            tag: str = release["tag_name"]
            candidates.append((version, tag))
        if len(releases) < 100:
            break
    else:
        raise ValueError("Too many release pages")
    if not candidates:
        raise NoEligibleRelease("No eligible published release")
    # Equal-precedence build metadata has deterministic selection, not an upgrade.
    return max(candidates)[1]
