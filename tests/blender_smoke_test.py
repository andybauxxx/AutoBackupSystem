"""Run with Blender in background mode; not distributed in the extension zip."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import time
from pathlib import Path

import bpy


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
addon = importlib.import_module("cyclic_auto_backup")
addon.register()

assert addon._operator_is_excluded("TRANSFORM_OT_resize") is False

with tempfile.TemporaryDirectory(prefix="cyclic_backup_test_") as temporary_dir:
    root = Path(temporary_dir)
    main_file = root / "SmokeTest.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(main_file), check_existing=False)

    custom_backup_directory = root / "ChosenBackupLocation"
    original_config = addon._config

    def custom_config(context=None):
        config = original_config(context)
        config["backup_directory"] = str(custom_backup_directory)
        config["backup_slots"] = 3
        return config

    addon._config = custom_config

    state = bpy.context.window_manager.cyclic_auto_backup_state
    for _index in range(50):
        addon._record_effective_operation("smoke_test")

    assert state.operation_count == 50
    assert addon._RUNTIME["backup_requested"] is True
    assert addon._perform_backup(reset_counter=True) is True
    assert state.operation_count == 0
    assert bpy.data.filepath == str(main_file)

    for _index in range(5):
        time.sleep(0.02)
        assert addon._perform_backup(reset_counter=False) is True

    backups = sorted(custom_backup_directory.glob("SmokeTest_abs_*.blend"))
    assert len(backups) == 3
    assert [path.name for path in backups] == [
        "SmokeTest_abs_04.blend",
        "SmokeTest_abs_05.blend",
        "SmokeTest_abs_06.blend",
    ]
    assert all(path.stat().st_size > 0 for path in backups)

    bpy.ops.wm.open_mainfile(filepath=str(main_file))
    reloaded_state = bpy.context.window_manager.cyclic_auto_backup_state
    assert reloaded_state.last_message == "Monitoring"
    load_handler_ok = reloaded_state.last_message == "Monitoring"
    assert addon._perform_backup(reset_counter=False) is True
    backups = sorted(custom_backup_directory.glob("SmokeTest_abs_*.blend"))
    assert [path.name for path in backups] == [
        "SmokeTest_abs_05.blend",
        "SmokeTest_abs_06.blend",
        "SmokeTest_abs_07.blend",
    ]

    result = {
        "blender": bpy.app.version_string,
        "backup_count": len(backups),
        "backup_names": [path.name for path in backups],
        "counter_after_automatic_backup": 0,
        "load_handler_ok": load_handler_ok,
        "main_file_unchanged": bpy.data.filepath == str(main_file),
        "custom_backup_directory": all(
            path.parent == custom_backup_directory for path in backups
        ),
    }
    print("CYCLIC_BACKUP_SMOKE_TEST=" + json.dumps(result, ensure_ascii=False))

addon.unregister()
