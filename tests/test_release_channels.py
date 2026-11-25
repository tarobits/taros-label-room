"""Semantic selection and persistent channels across failures and active requests."""

import json
from threading import Event

import pytest
from test_releases import complete
from test_update import deployment as deployment
from test_update import git

from app.config import Settings
from app.maintenance.release_policy import release_version, select_release
from app.maintenance.releases import ReleaseChecker
from companion.web import available_update, station_update
from scripts import update


def release(tag, prerelease=False, **extra):
    return {
        "tag_name": tag,
        "draft": False,
        "published_at": "2026-01-01",
        "prerelease": prerelease,
        **extra,
    }


def test_semver_selection_ignores_dates_and_wrong_stable_flags():
    releases = [
        release("v1.9.0", published_at="2026-12-01"),
        release("v2.0.0"),
        release("v2.1.0-preview.2", True),
        release("v2.1.0-preview.10", True),
        release("v9.0.0-rc.1"),
        release("v99.0.0", draft=True),
        release("not-a-version"),
    ]
    assert select_release(lambda page: releases, "stable") == "v2.0.0"
    assert select_release(lambda page: releases, "beta") == "v9.0.0-rc.1"
    assert release_version("v2.1.0-preview.10") > release_version("v2.1.0-preview.2")
    assert release_version("v0.23.0-preview.6") > release_version("v0.23.0-pre5")
    assert release_version("v1.0.0+build.10") == release_version("v1.0.0+build.1")
    assert release_version("v2.1.0") > release_version("v2.1.0-preview.99")


@pytest.mark.parametrize(
    "value",
    [
        "1.01.0",
        "1.0.0-01",
        "1.0.0+build..2",
        "1.0.0+_bad",
        "1.0.0+",
        "v1.0",
        "../v1.0.0",
        None,
        {},
        "1.0.0\n",
    ],
)
def test_malformed_versions_are_rejected(value):
    with pytest.raises(ValueError):
        release_version(value)


def test_policy_matches_app_and_updater_for_all_precedence_cases():
    values = ["0.1.0-pre5", "0.1.0-preview.6", "0.1.0-preview.10", "0.1.0-rc.1", "0.1.0", "1.0.0"]
    policy = update.release_policy()
    assert [policy.release_version(v) for v in values] == [release_version(v) for v in values]


def test_channel_persistence_is_atomic_and_check_is_read_only(tmp_path, monkeypatch):
    root = tmp_path / "install"
    data = root / "state"
    data.mkdir(parents=True)
    args = update.options(["--mode", "native", "--checkout", str(root), "--channel", "beta"])
    update.remember_channel(args, data, None, "v2.0.0-preview.2")
    assert update.options(["--mode", "native", "--checkout", str(root)]).channel == "beta"
    previous = (root / update.CHANNEL_STATE).read_bytes()
    original = update.atomic_state

    def failed(path, content):
        if path == data / update.CHANNEL_STATE:
            raise OSError("disk full")
        original(path, content)

    args.channel = "stable"
    with monkeypatch.context() as patch:
        patch.setattr(update, "atomic_state", failed)
        with pytest.raises(OSError):
            update.remember_channel(args, data, None, "v2.0.0-preview.2")
    assert (root / update.CHANNEL_STATE).read_bytes() == previous
    assert (data / update.CHANNEL_STATE).read_bytes() == previous
    update.remember_channel(args, data, None, "v2.0.0-preview.2")
    assert update.installed_channel(root) == "stable"


def test_stale_release_request_cannot_restore_previous_channel_result(tmp_path):
    settings = Settings(data_dir=tmp_path)
    state = tmp_path / update.CHANNEL_STATE
    state.write_text(json.dumps({"schema": 1, "channel": "beta"}))
    started, finish = Event(), Event()

    def fetch():
        started.set()
        assert finish.wait(3)
        return "v99.0.0-preview.1"

    checker = ReleaseChecker(settings, fetch)
    try:
        checker.status()
        assert started.wait(1)
        state.write_text(json.dumps({"schema": 1, "channel": "stable"}))
        checker.fetch = lambda: "v2.0.0"
        assert complete(checker)["latest_version"] == "v2.0.0"
    finally:
        finish.set()
    assert complete(checker)["latest_version"] == "v2.0.0"


def test_companion_notice_uses_its_channel_and_rejects_stale_channel(tmp_path):
    from pydantic import SecretStr

    from companion.config import Settings as CompanionSettings

    settings = CompanionSettings(
        server_url="http://testserver", api_key=SecretStr("test-only"), data_dir=tmp_path
    )
    value = "v99.0.0-preview.10"
    assert available_update(value) is None
    state = tmp_path / update.CHANNEL_STATE
    state.write_text(json.dumps({"schema": 1, "channel": "beta"}))
    notice = station_update({"latest_release": value, "release_channel": "beta"}, settings)
    assert notice is not None and notice["version"] == value
    state.write_text(json.dumps({"schema": 1, "channel": "stable"}))
    assert station_update({"latest_release": value, "release_channel": "beta"}, settings) is None
    state.write_text("broken")
    assert station_update({"latest_release": value}, settings) is None


def test_docker_channel_state_uses_actual_database_subdirectory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    args = SimpleNamespace(install_dir=tmp_path, channel="beta")
    captured = []
    monkeypatch.setattr(
        update, "docker_data_command", lambda args, config, code: captured.append(code)
    )
    update.remember_channel(args, None, {"data_relative": "companion"}, "v2.0.0-preview.1")
    assert "/update-data/companion/.label-release-channel.json" in captured[0]
    assert "os.replace" in captured[0]
    assert "finally:" in captured[0]


@pytest.mark.parametrize("target", ["v2.0.0", "v3.0.0-preview.2", None])
@pytest.mark.parametrize("check", [False, True])
def test_return_to_stable_never_fetches_or_stops_and_check_never_saves(
    deployment, monkeypatch, target, check
):
    root, data, previous, _ = deployment
    args = update.options(["--mode", "native", "--checkout", str(root), "--channel", "beta"])
    current = "v3.0.0-preview.2"
    update.remember_channel(args, data, None, current)
    args = update.options(
        ["--mode", "native", "--checkout", str(root), "--channel", "stable"]
        + (["--check"] if check else [])
    )
    monkeypatch.setattr(update, "native_configuration", lambda _: (data, "labels", {}))

    def select(*args):
        if target is None:
            raise update.NoRelease("No stable release")
        return target

    monkeypatch.setattr(update, "latest_release", select)
    update.update(args)
    assert update.installed_channel(root) == ("beta" if check else "stable")
    assert update.installed_release(root) == current
    assert git(root, "rev-parse", "HEAD") == previous
    assert not (root / ".git/FETCH_HEAD").exists()
    assert not args.backup_dir.exists()


def test_waiting_channel_rejects_interrupted_update_and_stale_discovery(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    args = update.options(["--mode", "native", "--checkout", str(tmp_path), "--channel", "beta"])
    update.remember_channel(args, data, None, "v2.0.0-preview.1")
    args.channel = "stable"
    monkeypatch.setattr(update, "channel_destination", lambda _: (data, None))
    update.validation_marker(args, data, None, True)
    with pytest.raises(update.UpdateError, match="unfinished update"):
        update.save_waiting_channel(args, "v2.0.0-preview.1")
    assert update.installed_channel(tmp_path) == "beta"
    update.validation_marker(args, data, None, False)
    with pytest.raises(update.UpdateError, match="changed during"):
        update.save_waiting_channel(args, "v1.0.0")
    update.save_waiting_channel(args, "v2.0.0-preview.1")
    assert update.installed_channel(tmp_path) == "stable"


@pytest.mark.parametrize("version", ["bad", None, "1.0.0-01"])
def test_invalid_recorded_version_cannot_bypass_downgrade_protection(tmp_path, version):
    (tmp_path / update.CHANNEL_STATE).write_text(
        json.dumps({"schema": 1, "channel": "stable", "version": version})
    )
    with pytest.raises(update.UpdateError):
        update.installed_release(tmp_path)


def test_old_handoff_can_bootstrap_missing_policy_without_writes(deployment, monkeypatch):
    root, _, previous, _ = deployment
    monkeypatch.setattr(update, "__file__", str(root / "scripts/update.py"))
    policy = root / update.RELEASE_POLICY_FILE
    policy.unlink()
    assert update.release_policy().release_version("v2.0.0-preview.10") == release_version(
        "v2.0.0-preview.10"
    )
    assert not policy.exists()
    assert git(root, "rev-parse", "HEAD") == previous


def test_release_changed_before_update_lock_is_rejected_before_fetch(deployment, monkeypatch):
    from contextlib import contextmanager

    root, data, previous, _ = deployment
    args = update.options(["--mode", "native", "--checkout", str(root)])
    monkeypatch.setattr(update, "latest_release", lambda *args: "v2.0.0")
    monkeypatch.setattr(update.shutil, "which", lambda name: "/test/" + name)
    monkeypatch.setattr(update, "native_configuration", lambda _: (data, "labels", {}))

    @contextmanager
    def changed_while_waiting(path):
        update.remember_channel(args, data, None, "v3.0.0")
        yield

    monkeypatch.setattr(update, "update_lock", changed_while_waiting)
    with pytest.raises(update.UpdateError, match="changed during"):
        update.update(args)
    assert git(root, "rev-parse", "HEAD") == previous
    assert not (root / ".git/FETCH_HEAD").exists()
    assert update.installed_release(root) == "v3.0.0"
