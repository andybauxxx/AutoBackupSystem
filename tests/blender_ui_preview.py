"""Capture a visual preview of the AutoBackupSystem panel layout."""

from __future__ import annotations

import importlib
import time
from pathlib import Path

import bpy


OUTPUT = Path(__file__).resolve().parent / "ui_preview.png"
stage = 0
addon_module = None


class TEST_OT_autobackup_ui_preview(bpy.types.Operator):
    bl_idname = "wm.autobackup_ui_preview"
    bl_label = "AutoBackupSystem"

    def draw(self, context):
        addon_module.CYCLICBACKUP_PT_panel.draw(self, context)

    def execute(self, _context):
        return {"FINISHED"}

    def invoke(self, context, _event):
        return context.window_manager.invoke_popup(self, width=360)


def driver():
    global stage, addon_module
    if stage == 0:
        key = next(
            addon.module
            for addon in bpy.context.preferences.addons
            if "cyclic_auto_backup" in addon.module
        )
        addon_module = importlib.import_module(key)
        prefs = bpy.context.preferences.addons[key].preferences
        prefs.enabled = True
        prefs.backup_directory = "D:\\Blender Backups"
        prefs.operation_target = 50
        prefs.backup_slots = 3
        prefs.show_backup_settings = False

        addon_module._RUNTIME["suppress_until"] = time.monotonic() + 10.0
        state = bpy.context.window_manager.cyclic_auto_backup_state
        state.operation_count = 32
        state.last_message = "Meaningful edit recorded: 32 / 50"
        state.last_backup_path = "D:\\Blender Backups\\Project_abs_12.blend"
        state.last_backup_time = "2026-06-23 10:35:00"

        bpy.utils.register_class(TEST_OT_autobackup_ui_preview)
        window = bpy.context.window_manager.windows[0]
        area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
        region = next(region for region in area.regions if region.type == "WINDOW")
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.wm.autobackup_ui_preview("INVOKE_DEFAULT")
        stage = 1
        return 1.0

    window = bpy.context.window_manager.windows[0]
    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.screenshot(filepath=str(OUTPUT), check_existing=False)
    print(f"UI_PREVIEW={OUTPUT}", flush=True)
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(driver, first_interval=1.5, persistent=True)
