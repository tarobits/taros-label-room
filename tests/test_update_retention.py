"""Retention only removes this installation's old updater-created directories."""

import json
from types import SimpleNamespace

import pytest
from test_update import deployment as deployment
from test_update import (
    test_updates_exact_tag_preserves_main_and_backups_and_rolls_back_on_failure as run_update,
)

from scripts import update


@pytest.fixture
def retention(tmp_path, monkeypatch):
    monkeypatch.delenv("LABEL_UPDATE_BACKUP_KEEP", raising=False)
    install = tmp_path / "labels"
    install.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    return SimpleNamespace(install_dir=install, backup_dir=backups, role="main")


def backup(args, number, **metadata):
    path = args.backup_dir / f"202601{number:02}T000000Z-{number:08x}"
    path.mkdir()
    (path / "update.json").write_text(
        json.dumps(
            {
                "install_dir": str(args.install_dir),
                "role": args.role,
                "status": "complete",
                **metadata,
            }
        )
    )
    (path / "data.tar.gz").write_bytes(b"backup data")
    return path


@pytest.mark.parametrize("role", ["main", "companion"])
def test_retention_configuration_defaults_and_environment_override(retention, monkeypatch, role):
    retention.role = role
    assert update.backup_retention(retention) == 5
    config = retention.install_dir / update.config_name(retention)
    config.write_text('export LABEL_UPDATE_BACKUP_KEEP="3" # backups\n')
    assert update.backup_retention(retention) == 3
    monkeypatch.setenv("LABEL_UPDATE_BACKUP_KEEP", "7")
    assert update.backup_retention(retention) == 7


@pytest.mark.parametrize("value", ["0", "-1", "", "1.5", "five", "$(touch unwanted)", "'3", "3 4"])
def test_invalid_retention_aborts_before_any_update_operation(retention, monkeypatch, value):
    (retention.install_dir / ".env").write_text(f"LABEL_UPDATE_BACKUP_KEEP={value}\n")
    monkeypatch.setattr(
        update, "command", lambda *a, **kw: pytest.fail("No commands on invalid setting")
    )
    message = (
        "Unterminated quoted environment value" if value == "'3" else "LABEL_UPDATE_BACKUP_KEEP"
    )
    with pytest.raises(update.UpdateError, match=message):
        update.update(retention)
    assert list(retention.backup_dir.iterdir()) == []


def test_retention_ignores_settings_inside_multiline_secrets(retention):
    (retention.install_dir / ".env").write_text(
        'EXAMPLE="first line\nLABEL_UPDATE_BACKUP_KEEP=1\nlast line"\n'
        "# LABEL_UPDATE_BACKUP_KEEP=2\n"
    )
    assert update.backup_retention(retention) == 5


@pytest.mark.parametrize("keep", [1, 3, 5, 10])
def test_keep_newest_by_timestamp_and_always_keep_current(retention, keep):
    # Clock corrections and modified mtimes must not evict the current backup.
    current = backup(retention, 1)
    older = [backup(retention, i) for i in range(2, 9)]
    update.prune_backups(retention, current, keep)
    assert current.exists()
    expected = older[-(keep - 1) :] if keep > 1 else []
    assert [p for p in older if p.exists()] == expected


def test_pruning_preserves_other_installations_unknown_files_and_symlink_targets(
    retention, tmp_path
):
    old = backup(retention, 1)
    current = backup(retention, 9)
    other = backup(retention, 2, install_dir=str(tmp_path / "other"))
    other_role = backup(retention, 3, role="companion")
    malformed = backup(retention, 4)
    (malformed / "update.json").write_text("not json")
    unknown = retention.backup_dir / "manual-backup"
    unknown.mkdir()
    (unknown / "update.json").write_text((old / "update.json").read_text())
    external = tmp_path / "outside"
    external.mkdir()
    precious = external / "keep.txt"
    precious.write_text("preserve")
    (old / "linked-data").symlink_to(external, target_is_directory=True)
    linked = retention.backup_dir / "20260105T000000Z-00000005"
    linked.symlink_to(external, target_is_directory=True)
    linked_record = backup(retention, 6)
    (linked_record / "update.json").unlink()
    (linked_record / "update.json").symlink_to(current / "update.json")
    saved_updater = retention.backup_dir / "updater-old.py"
    saved_updater.write_text("saved updater")
    update.prune_backups(retention, current, 1)
    assert not old.exists()
    for path in [
        current,
        other,
        other_role,
        malformed,
        unknown,
        linked,
        linked_record,
        saved_updater,
        precious,
    ]:
        assert path.exists()


@pytest.mark.parametrize(
    "status",
    ["preparing", "rolled_back", "rollback_failed", "activation_uncertain", None, "unknown"],
)
def test_retention_preserves_failed_incomplete_and_unknown_recovery_records(retention, status):
    protected = backup(retention, 1, status=status)
    completed = backup(retention, 2)
    current = backup(retention, 3)
    update.prune_backups(retention, current, 1)
    assert protected.is_dir()
    assert (protected / "data.tar.gz").read_bytes() == b"backup data"
    assert current.is_dir()
    assert not completed.exists()


def test_cleanup_failure_is_reported_and_a_later_attempt_recovers(retention, monkeypatch, capsys):
    old = backup(retention, 1)
    current = backup(retention, 2)
    remove = update.remove_backup

    def denied(path):
        raise PermissionError("fixture")

    monkeypatch.setattr(update, "remove_backup", denied)
    update.prune_backups(retention, current, 1)
    assert old.exists() and current.exists()
    assert "Could not clean" in capsys.readouterr().err
    monkeypatch.setattr(update, "remove_backup", remove)
    update.prune_backups(retention, current, 1)
    assert not old.exists() and current.exists()


def test_partial_cleanup_keeps_metadata_for_retry(retention, monkeypatch):
    old = backup(retention, 1)
    current = backup(retention, 2)
    directory = old / "compose-files"
    directory.mkdir()
    (directory / "compose.yml").write_text("saved configuration")
    remove = update.shutil.rmtree

    def interrupted(*args, **kwargs):
        (old / "data.tar.gz").unlink(missing_ok=True)
        raise OSError("temporary cleanup failure")

    monkeypatch.setattr(update.shutil, "rmtree", interrupted)
    update.prune_backups(retention, current, 1)
    assert (old / "update.json").is_file()
    assert not (old / "data.tar.gz").exists()
    assert current.is_dir()
    monkeypatch.setattr(update.shutil, "rmtree", remove)
    update.prune_backups(retention, current, 1)
    assert not old.exists()
    assert current.is_dir()


@pytest.mark.parametrize("mode", ["native", "docker"])
@pytest.mark.parametrize("failure", [None, "backup", "migration", "health"])
def test_retention_runs_only_after_success_under_the_installation_lock(
    deployment, monkeypatch, mode, failure
):
    monkeypatch.delenv("LABEL_UPDATE_BACKUP_KEEP", raising=False)
    prune = update.prune_backups
    calls = []

    def checked(args, current, keep):
        calls.append(current)
        assert json.loads((current / "update.json").read_text())["status"] == "complete"
        lock = args.install_dir.parent / f".{args.install_dir.name}.update.lock"
        with pytest.raises(update.UpdateError):
            with update.update_lock(lock):
                pytest.fail("Retention must share the update lock")
        old = [backup(args, i) for i in range(1, 8)]
        prune(args, current, keep)
        assert current.exists()
        assert [p for p in old if p.exists()] == old[-4:]
        # The shared update scenario checks the single newly created recovery
        # record and its archives after returning; remove our extra fixtures.
        for path in old[-4:]:
            update.remove_backup(path)

    monkeypatch.setattr(update, "prune_backups", checked)
    run_update(deployment, monkeypatch, mode, failure)
    assert len(calls) == (0 if failure else 1)
