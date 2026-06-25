"""Exercise the live monitor from a real Blender window.

This test is intentionally launched without ``--background`` because Blender's
completed-operator stack only exists while a window is running.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import bpy


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
addon = importlib.import_module("cyclic_auto_backup")

test_root = Path(tempfile.mkdtemp(prefix="cyclic_backup_gui_test_"))
main_file = test_root / "GuiDetection.blend"
results = {}
state = None
test_object = None
stage = 0
observed_updates = {}


def debug_depsgraph(_scene, depsgraph):
    batch = []
    for update in depsgraph.updates:
        batch.append(
            {
                "id": update.id.bl_rna.identifier,
                "geometry": update.is_updated_geometry,
                "transform": update.is_updated_transform,
                "shading": update.is_updated_shading,
            }
        )
    if batch:
        observed_updates.setdefault(stage, []).append(batch)


bpy.app.handlers.depsgraph_update_post.append(debug_depsgraph)


def view3d_override():
    window = bpy.context.window_manager.windows[0]
    area = next(area for area in window.screen.areas if area.type == "VIEW_3D")
    region = next(region for region in area.regions if region.type == "WINDOW")
    return bpy.context.temp_override(window=window, area=area, region=region)


def finish():
    results["blender"] = bpy.app.version_string
    results["passed"] = (
        results.get("after_add") == 1
        and results.get("after_selection") == 1
        and results.get("after_direct_edit") == 2
        and results.get("after_undo") == 3
    )
    print("CYCLIC_BACKUP_GUI_TEST=" + json.dumps(results, ensure_ascii=False), flush=True)
    if debug_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(debug_depsgraph)
    addon.unregister()
    bpy.ops.wm.quit_blender()


def driver():
    global stage, state, test_object
    try:
        if stage == 0:
            # Delay setup until the normal startup file and UI have fully loaded.
            bpy.ops.wm.save_as_mainfile(filepath=str(main_file), check_existing=False)
            addon.register()
            state = bpy.context.window_manager.cyclic_auto_backup_state
            state.operation_count = 0
            addon._reset_detection(keep_count=True)
            addon._RUNTIME["suppress_until"] = 0.0
            stage = 1
            return 0.5

        if stage == 1:
            with view3d_override():
                bpy.ops.mesh.primitive_cube_add(location=(2.0, 0.0, 0.0))
                test_object = bpy.context.active_object
                bpy.context.view_layer.update()
            stage = 2
            return 1.2

        if stage == 2:
            addon._monitor_timer()
            results["after_add"] = state.operation_count
            results["debug_after_add"] = {
                "dirty": bpy.data.is_dirty,
                "objects": len(bpy.data.objects),
                "pending": addon._RUNTIME["pending_change"],
                "operators": [
                    operator.bl_idname for operator in bpy.context.window_manager.operators
                ],
            }
            with view3d_override():
                bpy.ops.object.select_all(action="DESELECT")
            stage = 3
            return 1.2

        if stage == 3:
            addon._monitor_timer()
            results["after_selection"] = state.operation_count
            test_object.select_set(True)
            bpy.context.view_layer.objects.active = test_object
            with view3d_override():
                bpy.ops.transform.resize(value=(1.25, 1.25, 1.25))
                bpy.context.view_layer.update()
            stage = 4
            return 1.2

        if stage == 4:
            addon._monitor_timer()
            results["after_direct_edit"] = state.operation_count
            # Python-triggered operators do not enter Blender's interactive undo
            # stack, so exercise the same post-undo hook directly here.
            addon._on_undo_post(None)
            stage = 5
            return 1.2

        if stage == 5:
            addon._monitor_timer()
            results["after_undo"] = state.operation_count
            results["depsgraph_updates_by_stage"] = observed_updates
            finish()
            return None
    except Exception as exc:
        results["exception"] = repr(exc)
        finish()
        return None


bpy.app.timers.register(driver, first_interval=1.5, persistent=True)
