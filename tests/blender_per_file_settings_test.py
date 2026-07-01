"""Verify AutoBackupSystem settings are saved per blend file."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import bpy


os.environ["AUTOBACKUP_SUPPRESS_SETUP_PROMPT"] = "1"
WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
addon = importlib.import_module("cyclic_auto_backup")
addon.register()


def settings_snapshot() -> dict[str, object]:
    settings = bpy.context.scene.cyclic_auto_backup_settings
    return {
        "enabled": bool(settings.enabled),
        "operation_target": int(settings.operation_target),
        "backup_slots": int(settings.backup_slots),
        "backup_directory": str(settings.backup_directory),
        "quiet_period": round(float(settings.quiet_period), 2),
    }


with tempfile.TemporaryDirectory(prefix="autobackup_per_file_") as temporary_dir:
    root = Path(temporary_dir)
    file_a = root / "ProjectA.blend"
    file_b = root / "ProjectB.blend"

    settings = bpy.context.scene.cyclic_auto_backup_settings
    settings.enabled = True
    settings.operation_target = 12
    settings.backup_slots = 2
    settings.backup_directory = str(root / "BackupsA")
    settings.quiet_period = 0.4
    bpy.ops.wm.save_as_mainfile(filepath=str(file_a), check_existing=False)

    bpy.ops.wm.read_homefile(use_empty=True)
    settings = bpy.context.scene.cyclic_auto_backup_settings
    settings.enabled = False
    settings.operation_target = 90
    settings.backup_slots = 7
    settings.backup_directory = str(root / "BackupsB")
    settings.quiet_period = 1.25
    bpy.ops.wm.save_as_mainfile(filepath=str(file_b), check_existing=False)

    bpy.ops.wm.open_mainfile(filepath=str(file_a))
    snapshot_a = settings_snapshot()

    bpy.ops.wm.open_mainfile(filepath=str(file_b))
    snapshot_b = settings_snapshot()

    passed = snapshot_a == {
        "enabled": True,
        "operation_target": 12,
        "backup_slots": 2,
        "backup_directory": str(root / "BackupsA"),
        "quiet_period": 0.4,
    } and snapshot_b == {
        "enabled": False,
        "operation_target": 90,
        "backup_slots": 7,
        "backup_directory": str(root / "BackupsB"),
        "quiet_period": 1.25,
    }

    print(
        "PER_FILE_SETTINGS_TEST="
        + json.dumps(
            {
                "blender": bpy.app.version_string,
                "passed": passed,
                "project_a": snapshot_a,
                "project_b": snapshot_b,
            },
            ensure_ascii=False,
        )
    )
    assert passed

addon.unregister()
