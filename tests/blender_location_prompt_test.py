"""Verify that a missing backup location opens Blender's folder picker."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import bpy


os.environ["AUTOBACKUP_SUPPRESS_SETUP_PROMPT"] = "1"
WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
addon = importlib.import_module("cyclic_auto_backup")

stage = 0
state = None
results = {}


def finish():
    results["blender"] = bpy.app.version_string
    results["passed"] = (
        results.get("request_returned") is True
        and results.get("file_browser_opened") is True
        and results.get("waiting_after_cancel") is False
        and results.get("count_after_cancel") == 50
    )
    print("LOCATION_PROMPT_TEST=" + json.dumps(results, ensure_ascii=False), flush=True)
    addon.unregister()
    bpy.ops.wm.quit_blender()


def driver():
    global stage, state
    try:
        if stage == 0:
            addon.register()
            state = bpy.context.window_manager.cyclic_auto_backup_state
            state.operation_count = 50
            results["request_returned"] = addon._request_backup_location(
                reset_counter=True
            )
            stage = 1
            return 1.0

        if stage == 1:
            file_browser = None
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "FILE_BROWSER":
                        file_browser = (window, area)
                        break
                if file_browser:
                    break

            results["file_browser_opened"] = file_browser is not None
            if file_browser is None:
                finish()
                return None

            window, area = file_browser
            region = next(region for region in area.regions if region.type == "WINDOW")
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.file.cancel()
            stage = 2
            return 0.8

        if stage == 2:
            results["waiting_after_cancel"] = addon._RUNTIME["waiting_for_location"]
            results["count_after_cancel"] = state.operation_count
            results["message_after_cancel"] = state.last_message
            finish()
            return None
    except Exception as exc:
        results["exception"] = repr(exc)
        finish()
        return None


bpy.app.timers.register(driver, first_interval=1.5, persistent=True)
