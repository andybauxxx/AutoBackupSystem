# SPDX-License-Identifier: GPL-3.0-or-later

"""AutoBackupSystem for Blender.

The add-on watches completed undoable operators and meaningful dependency-graph
changes. Continuous updates are debounced into one edit before the counter is
incremented. Once the configured target is reached, a copy of the current blend
file is written to the configured backup folder. Backup sequence numbers keep
increasing while older files are pruned to the configured retention count.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup


bl_info = {
    "name": "AutoBackupSystem",
    "author": "Andy Bau",
    "version": (1, 4, 5),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Auto Backup",
    "description": "Create a rotating .blend backup after a set number of meaningful edits",
    "category": "System",
}


ADDON_PACKAGE = __package__ or __name__
TIMER_INTERVAL = 0.20
DEFAULT_OPERATION_TARGET = 50
DEFAULT_BACKUP_SLOTS = 5
DEFAULT_QUIET_PERIOD = 0.65


_RUNTIME: dict[str, Any] = {
    "registered": False,
    "operators_initialized": False,
    "known_operator_keys": set(),
    "pending_change": False,
    "pending_started_at": 0.0,
    "last_change_at": 0.0,
    "suppress_until": 0.0,
    "backup_requested": False,
    "backup_running": False,
    "waiting_for_location": False,
    "pending_backup_reset_counter": True,
    "animation_playing": False,
    "rendering": False,
    "setup_prompt_pending": False,
    "setup_prompted_session": False,
    "syncing_file_settings": False,
}


# These operators change selection, navigation, UI state, or file state rather
# than the authored scene. They must not advance the meaningful-edit counter.
_EXCLUDED_OPERATOR_EXACT = {
    "CYCLICBACKUP_OT_backup_now",
    "CYCLICBACKUP_OT_choose_backup_directory",
    "CYCLICBACKUP_OT_file_settings",
    "CYCLICBACKUP_OT_open_backup_folder",
    "CYCLICBACKUP_OT_reset_counter",
    "CYCLICBACKUP_OT_use_default_directory",
    "ED_OT_redo",
    "ED_OT_undo",
    "ED_OT_undo_history",
    "OBJECT_OT_editmode_toggle",
    "OBJECT_OT_mode_set",
    "SCREEN_OT_animation_cancel",
    "SCREEN_OT_animation_play",
    "SCREEN_OT_frame_jump",
    "SCREEN_OT_keyframe_jump",
    "SCREEN_OT_space_context_cycle",
    "WM_OT_open_mainfile",
    "WM_OT_quit_blender",
    "WM_OT_read_homefile",
    "WM_OT_recover_auto_save",
    "WM_OT_recover_last_session",
    "WM_OT_save_as_mainfile",
    "WM_OT_save_homefile",
    "WM_OT_save_mainfile",
}

_EXCLUDED_OPERATOR_TOKENS = (
    "_OT_cursor",
    "_OT_dolly",
    "_OT_navigate",
    "_OT_orbit",
    "_OT_pan",
    "_OT_region_",
    "_OT_rotate_view",
    "_OT_ruler",
    "_OT_scroll",
    "_OT_select",
    "_OT_smoothview",
    "_OT_view_",
    "_OT_view3d",
    "_OT_zoom",
)

_EXCLUDED_DATA_TYPES = {
    "KeyConfig",
    "Screen",
    "WindowManager",
    "WorkSpace",
    "Workspace",
}


def _preferences(context: bpy.types.Context | None = None) -> AddonPreferences | None:
    context = context or bpy.context
    preferences = getattr(context, "preferences", None)
    if preferences is None:
        return None

    addon = preferences.addons.get(ADDON_PACKAGE)
    if addon is None:
        # Helpful when the source package is loaded directly during development.
        addon = preferences.addons.get(ADDON_PACKAGE.rsplit(".", 1)[-1])
    return addon.preferences if addon else None


def _blend_filepath() -> str:
    try:
        return str(getattr(bpy.data, "filepath", "") or "")
    except (AttributeError, ReferenceError, RuntimeError):
        return ""


def _all_scenes() -> list[bpy.types.Scene]:
    try:
        return list(getattr(bpy.data, "scenes", []) or [])
    except (AttributeError, ReferenceError, RuntimeError):
        return []


def _settings(context: bpy.types.Context | None = None) -> "CYCLICBACKUP_PG_settings | None":
    context = context or bpy.context
    try:
        scene = getattr(context, "scene", None)
    except (AttributeError, ReferenceError, RuntimeError):
        scene = None
    scenes = _all_scenes()
    if scene is None and scenes:
        scene = scenes[0]
    if scene is None or not hasattr(scene, "cyclic_auto_backup_settings"):
        return None
    return scene.cyclic_auto_backup_settings


def _config(context: bpy.types.Context | None = None) -> dict[str, Any]:
    settings = _settings(context)
    return {
        "enabled": bool(getattr(settings, "enabled", True)),
        "operation_target": int(
            getattr(settings, "operation_target", DEFAULT_OPERATION_TARGET)
        ),
        "backup_slots": int(getattr(settings, "backup_slots", DEFAULT_BACKUP_SLOTS)),
        "backup_directory": str(getattr(settings, "backup_directory", "")),
        "quiet_period": float(getattr(settings, "quiet_period", DEFAULT_QUIET_PERIOD)),
    }


def _state(context: bpy.types.Context | None = None) -> "CYCLICBACKUP_PG_state | None":
    context = context or bpy.context
    window_manager = getattr(context, "window_manager", None)
    if window_manager is None or not hasattr(window_manager, "cyclic_auto_backup_state"):
        return None
    return window_manager.cyclic_auto_backup_state


def _tag_redraw() -> None:
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _clear_pending_change() -> None:
    _RUNTIME["pending_change"] = False
    _RUNTIME["pending_started_at"] = 0.0
    _RUNTIME["last_change_at"] = 0.0


def _operator_key(operator: bpy.types.Operator) -> tuple[int, str]:
    try:
        pointer = int(operator.as_pointer())
    except (AttributeError, ReferenceError, TypeError):
        pointer = id(operator)
    return pointer, str(getattr(operator, "bl_idname", ""))


def _current_operators() -> list[bpy.types.Operator]:
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return []
    try:
        return list(window_manager.operators)
    except (AttributeError, ReferenceError, RuntimeError):
        return []


def _sync_operator_baseline() -> None:
    operators = _current_operators()
    _RUNTIME["known_operator_keys"] = {_operator_key(operator) for operator in operators}
    _RUNTIME["operators_initialized"] = True


def _operator_is_excluded(operator_id: str) -> bool:
    if not operator_id:
        return True
    if operator_id.lower().startswith("cyclicbackup."):
        return True
    if operator_id.upper().startswith("CYCLICBACKUP_OT_"):
        return True
    if operator_id in _EXCLUDED_OPERATOR_EXACT:
        return True
    return any(token in operator_id for token in _EXCLUDED_OPERATOR_TOKENS)


def _suppress_plugin_ui_activity(duration: float = 1.0) -> None:
    """Prevent AutoBackupSystem's own UI from being counted as an edit."""
    _clear_pending_change()
    _RUNTIME["suppress_until"] = max(
        float(_RUNTIME["suppress_until"]), time.monotonic() + duration
    )
    try:
        _sync_operator_baseline()
    except Exception:
        # UI suppression should never break a user-facing button.
        pass


def _operator_is_undoable(operator: bpy.types.Operator) -> bool:
    try:
        options = operator.bl_options
    except (AttributeError, ReferenceError):
        return False
    return any(str(option).startswith("UNDO") for option in options)


def _has_modal_operator() -> bool:
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return False
    for window in window_manager.windows:
        try:
            if len(window.modal_operators) > 0:
                return True
        except (AttributeError, ReferenceError, TypeError):
            continue
    return False


def _is_animation_playing() -> bool:
    if _RUNTIME["animation_playing"]:
        return True
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return False
    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if screen is not None and bool(getattr(screen, "is_animation_playing", False)):
            return True
    return False


def _record_effective_operation(source: str = "change") -> None:
    config = _config()
    if not config["enabled"] or _RUNTIME["backup_running"]:
        return
    if _RUNTIME["backup_requested"] or _RUNTIME["waiting_for_location"]:
        return

    state = _state()
    if state is None:
        return

    target = max(1, config["operation_target"])
    state.operation_count = min(state.operation_count + 1, target)
    state.last_message = f"Meaningful edit recorded: {state.operation_count} / {target}"
    state.last_was_error = False

    if state.operation_count >= target:
        _RUNTIME["backup_requested"] = True
        _RUNTIME["pending_backup_reset_counter"] = True
        state.last_message = f"Reached {target} edits. Preparing backup..."

    _tag_redraw()


def _scan_completed_operators(now: float) -> None:
    operators = _current_operators()
    current_keys = {_operator_key(operator) for operator in operators}

    if not _RUNTIME["operators_initialized"]:
        _RUNTIME["known_operator_keys"] = current_keys
        _RUNTIME["operators_initialized"] = True
        return

    previous_keys = _RUNTIME["known_operator_keys"]
    new_operators = [
        operator for operator in operators if _operator_key(operator) not in previous_keys
    ]
    _RUNTIME["known_operator_keys"] = current_keys

    for operator in new_operators:
        operator_id = str(getattr(operator, "bl_idname", ""))
        if _operator_is_excluded(operator_id):
            # Selection can dirty evaluated data even though it is intentionally
            # not an effective operation. Discard only a very recent batch.
            if (
                _RUNTIME["pending_change"]
                and now - _RUNTIME["last_change_at"] <= 0.35
            ):
                _clear_pending_change()
            continue

        has_related_change = bool(_RUNTIME["pending_change"])
        if _operator_is_undoable(operator) or has_related_change:
            _clear_pending_change()
            _record_effective_operation(operator_id)


def _update_is_significant(depsgraph: bpy.types.Depsgraph) -> bool:
    try:
        updates = depsgraph.updates
    except (AttributeError, ReferenceError):
        return False

    for update in updates:
        try:
            identifier = update.id.bl_rna.identifier
        except (AttributeError, ReferenceError):
            continue
        if identifier in _EXCLUDED_DATA_TYPES:
            continue
        if identifier == "Scene" and not (
            update.is_updated_geometry
            or update.is_updated_transform
            or update.is_updated_shading
        ):
            # Selection-only changes commonly produce a bare Scene update with
            # no authored-data flags. Treating that as an edit would count every
            # object selection.
            continue
        return True
    return False


def _sanitize_stem(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value or "Untitled"


def _project_stem() -> str:
    filepath = _blend_filepath()
    if not filepath:
        return "Untitled"
    return _sanitize_stem(Path(filepath).stem)


def _default_backup_directory() -> Path:
    filepath = _blend_filepath()
    if filepath:
        return Path(filepath).resolve().parent / "AutoBackups"

    return Path(bpy.app.tempdir).resolve() / "Blender_AutoBackups"


def _backup_directory(config: dict[str, Any] | None = None) -> Path:
    config = config or _config()
    configured = config["backup_directory"].strip()
    if configured:
        return Path(bpy.path.abspath(configured)).expanduser().resolve()
    return _default_backup_directory()


def _existing_backups(config: dict[str, Any] | None = None) -> list[tuple[int, Path]]:
    config = config or _config()
    directory = _backup_directory(config)
    stem = _project_stem()
    pattern = re.compile(rf"^{re.escape(stem)}_abs_(\d+)\.blend$", re.IGNORECASE)
    if not directory.exists():
        return []

    backups: list[tuple[int, Path]] = []
    try:
        entries = directory.iterdir()
        for path in entries:
            if not path.is_file():
                continue
            match = pattern.fullmatch(path.name)
            if match:
                backups.append((int(match.group(1)), path))
    except OSError:
        return []
    return backups


def _next_backup_path(config: dict[str, Any] | None = None) -> Path:
    config = config or _config()
    existing = _existing_backups(config)
    sequence = max((number for number, _path in existing), default=0) + 1
    width = max(2, len(str(sequence)))
    return _backup_directory(config) / f"{_project_stem()}_abs_{sequence:0{width}d}.blend"


def _prune_old_backups(config: dict[str, Any], newest: Path) -> list[str]:
    keep_count = max(1, config["backup_slots"])
    existing = _existing_backups(config)
    excess = len(existing) - keep_count
    if excess <= 0:
        return []

    def modified_time(item: tuple[int, Path]) -> tuple[float, int]:
        number, path = item
        try:
            return path.stat().st_mtime, number
        except OSError:
            return float("-inf"), number

    candidates = [item for item in existing if item[1] != newest]
    candidates.sort(key=modified_time)
    errors: list[str] = []
    for _number, path in candidates[:excess]:
        try:
            path.unlink()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return errors


def _remove_incomplete_file(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        pass


def _perform_backup(*, reset_counter: bool) -> bool:
    if _RUNTIME["backup_running"]:
        return False

    state = _state()
    if state is None:
        return False

    config = _config()
    target = _next_backup_path(config)
    temporary = target.with_name(f".{target.stem}.writing.blend")
    original_filepath = _blend_filepath()
    previous_count = state.operation_count

    _RUNTIME["backup_running"] = True
    _RUNTIME["backup_requested"] = False
    _RUNTIME["suppress_until"] = time.monotonic() + 2.0
    _clear_pending_change()

    if reset_counter:
        # Set this before saving so opening a backup starts a fresh cycle.
        state.operation_count = 0

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _remove_incomplete_file(temporary)

        use_compression = bool(
            getattr(bpy.context.preferences.filepaths, "use_file_compression", False)
        )
        result = bpy.ops.wm.save_as_mainfile(
            filepath=str(temporary),
            check_existing=False,
            compress=use_compression,
            relative_remap=True,
            copy=True,
        )
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender save operation did not finish: {sorted(result)}")
        if _blend_filepath() != original_filepath:
            raise RuntimeError("The backup unexpectedly changed the current file path")
        if not temporary.exists():
            raise RuntimeError("Blender did not create the expected temporary backup file")

        os.replace(temporary, target)
        prune_errors = _prune_old_backups(config, target)
        state.last_backup_path = str(target)
        state.last_backup_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if prune_errors:
            state.last_message = (
                f"Backup complete, but old backups could not be removed: {target.name}"
            )
            state.last_was_error = True
            print("[AutoBackupSystem] " + "; ".join(prune_errors))
        else:
            state.last_message = f"Backup complete: {target.name}"
            state.last_was_error = False
        return True
    except Exception as exc:  # Blender operators can raise several runtime errors.
        if reset_counter:
            state.operation_count = previous_count
        state.last_message = f"Backup failed: {exc}"
        state.last_was_error = True
        print(f"[AutoBackupSystem] {state.last_message}")
        _remove_incomplete_file(temporary)
        return False
    finally:
        _RUNTIME["backup_running"] = False
        _RUNTIME["suppress_until"] = time.monotonic() + 1.0
        _sync_operator_baseline()
        _tag_redraw()


def _request_backup_location(*, reset_counter: bool) -> bool:
    _suppress_plugin_ui_activity()
    if _RUNTIME["waiting_for_location"]:
        return True

    state = _state()
    window_manager = getattr(bpy.context, "window_manager", None)
    if state is None or window_manager is None or not window_manager.windows:
        if state is not None:
            state.last_message = "Backup paused: choose a backup location in the panel"
            state.last_was_error = True
        _RUNTIME["backup_requested"] = False
        return False

    _RUNTIME["backup_requested"] = False
    _RUNTIME["waiting_for_location"] = True
    _RUNTIME["pending_backup_reset_counter"] = reset_counter
    state.last_message = "Choose a backup location to continue"
    state.last_was_error = False
    _tag_redraw()

    window = window_manager.windows[0]
    try:
        with bpy.context.temp_override(window=window):
            result = bpy.ops.cyclicbackup.choose_backup_directory(
                "INVOKE_DEFAULT", resume_pending_backup=True
            )
        if "RUNNING_MODAL" not in result:
            raise RuntimeError(f"Folder picker did not open: {sorted(result)}")
        return True
    except Exception as exc:
        _RUNTIME["waiting_for_location"] = False
        state.last_message = f"Unable to open folder picker: {exc}"
        state.last_was_error = True
        _tag_redraw()
        return False


def _schedule_initial_setup_prompt(delay: float = 0.8) -> None:
    if (
        bpy.app.background
        or not _RUNTIME["registered"]
        or os.environ.get("AUTOBACKUP_SUPPRESS_SETUP_PROMPT") == "1"
        or _RUNTIME["setup_prompt_pending"]
        or _RUNTIME["setup_prompted_session"]
        or _blend_filepath()
    ):
        return

    _RUNTIME["setup_prompt_pending"] = True
    if not bpy.app.timers.is_registered(_show_initial_setup_popup):
        bpy.app.timers.register(
            _show_initial_setup_popup, first_interval=delay, persistent=False
        )


def _show_initial_setup_popup() -> float | None:
    if not _RUNTIME["registered"] or bpy.app.background:
        _RUNTIME["setup_prompt_pending"] = False
        return None

    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None or not window_manager.windows:
        return 0.5

    if _blend_filepath() or _RUNTIME["setup_prompted_session"]:
        _RUNTIME["setup_prompt_pending"] = False
        return None

    window = window_manager.windows[0]
    _RUNTIME["setup_prompt_pending"] = False
    _RUNTIME["setup_prompted_session"] = True
    try:
        with bpy.context.temp_override(window=window):
            bpy.ops.cyclicbackup.file_settings("INVOKE_DEFAULT", auto_prompt=True)
    except Exception as exc:
        state = _state()
        if state is not None:
            state.last_message = f"Unable to open setup dialog: {exc}"
            state.last_was_error = True
        print(f"[AutoBackupSystem] Unable to open setup dialog: {exc}")
    return None


def _monitor_timer() -> float | None:
    if not _RUNTIME["registered"]:
        return None

    try:
        config = _config()
        now = time.monotonic()

        if not config["enabled"]:
            _clear_pending_change()
            _RUNTIME["backup_requested"] = False
            _sync_operator_baseline()
            return TIMER_INTERVAL

        if _RUNTIME["backup_running"]:
            return TIMER_INTERVAL

        _scan_completed_operators(now)

        if (
            _RUNTIME["pending_change"]
            and now >= _RUNTIME["suppress_until"]
            and not _is_animation_playing()
            and not _RUNTIME["rendering"]
            and not _has_modal_operator()
            and now - _RUNTIME["last_change_at"] >= config["quiet_period"]
        ):
            _clear_pending_change()
            _record_effective_operation("depsgraph")

        if _RUNTIME["backup_requested"]:
            if config["backup_directory"].strip():
                reset_counter = bool(_RUNTIME["pending_backup_reset_counter"])
                _perform_backup(reset_counter=reset_counter)
                _RUNTIME["pending_backup_reset_counter"] = True
            else:
                _request_backup_location(
                    reset_counter=bool(_RUNTIME["pending_backup_reset_counter"])
                )
    except Exception as exc:
        # Never let an unexpected monitoring error permanently unregister the timer.
        state = _state()
        if state is not None:
            state.last_message = f"Monitoring error: {exc}"
            state.last_was_error = True
        print(f"[AutoBackupSystem] Monitor error: {exc}")

    return TIMER_INTERVAL


def _reset_detection(*, keep_count: bool = True) -> None:
    _clear_pending_change()
    _RUNTIME["operators_initialized"] = False
    _RUNTIME["known_operator_keys"] = set()
    _RUNTIME["backup_requested"] = False
    _RUNTIME["waiting_for_location"] = False
    _RUNTIME["pending_backup_reset_counter"] = True
    _RUNTIME["suppress_until"] = time.monotonic() + 1.0
    _sync_operator_baseline()
    if not keep_count:
        state = _state()
        if state is not None:
            state.operation_count = 0


def _settings_updated(_self: Any, _context: bpy.types.Context) -> None:
    _suppress_plugin_ui_activity()
    if _RUNTIME["syncing_file_settings"]:
        return

    scenes = _all_scenes()
    if _self is not None and scenes:
        _RUNTIME["syncing_file_settings"] = True
        try:
            for scene in scenes:
                if not hasattr(scene, "cyclic_auto_backup_settings"):
                    continue
                other = scene.cyclic_auto_backup_settings
                try:
                    if other.as_pointer() == _self.as_pointer():
                        continue
                except (AttributeError, ReferenceError):
                    pass
                other.enabled = bool(getattr(_self, "enabled", True))
                other.operation_target = int(
                    getattr(_self, "operation_target", DEFAULT_OPERATION_TARGET)
                )
                other.backup_slots = int(
                    getattr(_self, "backup_slots", DEFAULT_BACKUP_SLOTS)
                )
                other.backup_directory = str(getattr(_self, "backup_directory", ""))
                other.quiet_period = float(
                    getattr(_self, "quiet_period", DEFAULT_QUIET_PERIOD)
                )
        finally:
            _RUNTIME["syncing_file_settings"] = False

    _reset_detection(keep_count=True)


def _ui_settings_updated(_self: Any, _context: bpy.types.Context) -> None:
    _suppress_plugin_ui_activity()


@persistent
def _on_depsgraph_update(_scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    now = time.monotonic()
    config = _config()
    if (
        not config["enabled"]
        or _RUNTIME["backup_running"]
        or _RUNTIME["rendering"]
        or _is_animation_playing()
        or now < _RUNTIME["suppress_until"]
        or not _update_is_significant(depsgraph)
    ):
        return

    if not _RUNTIME["pending_change"]:
        _RUNTIME["pending_change"] = True
        _RUNTIME["pending_started_at"] = now
    _RUNTIME["last_change_at"] = now


@persistent
def _on_undo_post(*_args: Any) -> None:
    if _RUNTIME["backup_running"]:
        return
    _clear_pending_change()
    _RUNTIME["suppress_until"] = time.monotonic() + 0.5
    _record_effective_operation("undo")
    _sync_operator_baseline()


@persistent
def _on_redo_post(*_args: Any) -> None:
    if _RUNTIME["backup_running"]:
        return
    _clear_pending_change()
    _RUNTIME["suppress_until"] = time.monotonic() + 0.5
    _record_effective_operation("redo")
    _sync_operator_baseline()


@persistent
def _on_frame_change(*_args: Any) -> None:
    # Frame navigation and playback are intentionally not meaningful edits.
    _RUNTIME["suppress_until"] = max(
        _RUNTIME["suppress_until"], time.monotonic() + 0.25
    )


@persistent
def _on_animation_playback_pre(*_args: Any) -> None:
    _RUNTIME["animation_playing"] = True
    _clear_pending_change()


@persistent
def _on_animation_playback_post(*_args: Any) -> None:
    _RUNTIME["animation_playing"] = False
    _RUNTIME["suppress_until"] = time.monotonic() + 0.5
    _clear_pending_change()


@persistent
def _on_render_pre(*_args: Any) -> None:
    _RUNTIME["rendering"] = True
    _clear_pending_change()


@persistent
def _on_render_post(*_args: Any) -> None:
    _RUNTIME["rendering"] = False
    _RUNTIME["suppress_until"] = time.monotonic() + 0.5
    _clear_pending_change()


@persistent
def _on_load_post(*_args: Any) -> None:
    _RUNTIME["animation_playing"] = False
    _RUNTIME["rendering"] = False
    _RUNTIME["setup_prompt_pending"] = False
    _RUNTIME["setup_prompted_session"] = False
    _reset_detection(keep_count=True)
    state = _state()
    if state is not None:
        state.last_message = "Monitoring"
        state.last_was_error = False
    _schedule_initial_setup_prompt()


class CYCLICBACKUP_PG_settings(PropertyGroup):
    enabled: BoolProperty(
        name="Enable Automatic Backups",
        description="Monitor meaningful edits and create a rotating backup when the target is reached",
        default=True,
        update=_settings_updated,
    )
    operation_target: IntProperty(
        name="Meaningful Edits per Backup",
        description="Number of meaningful edits required to trigger an automatic backup",
        default=DEFAULT_OPERATION_TARGET,
        min=1,
        max=10000,
        update=_settings_updated,
    )
    backup_slots: IntProperty(
        name="Backups to Keep",
        description="Maximum backup files to keep; filename sequence numbers continue increasing",
        default=DEFAULT_BACKUP_SLOTS,
        min=1,
        max=100,
        update=_settings_updated,
    )
    backup_directory: StringProperty(
        name="Backup Directory",
        description="Leave blank to choose a folder when the next backup is triggered",
        default="",
        subtype="DIR_PATH",
        update=_settings_updated,
    )
    quiet_period: FloatProperty(
        name="Edit Merge Delay",
        description="How long continuous changes must stop before being treated as one edit; this never triggers a time-based backup",
        default=DEFAULT_QUIET_PERIOD,
        min=0.20,
        max=3.0,
        step=5,
        precision=2,
        subtype="TIME",
        update=_settings_updated,
    )
    show_backup_settings: BoolProperty(
        name="Show Backup Settings", default=False, update=_ui_settings_updated
    )
    show_advanced: BoolProperty(
        name="Advanced Detection Settings", default=False, update=_ui_settings_updated
    )


class CYCLICBACKUP_PG_state(PropertyGroup):
    operation_count: IntProperty(name="Meaningful Edits", default=0, min=0)
    last_backup_path: StringProperty(name="Latest Backup", default="", subtype="FILE_PATH")
    last_backup_time: StringProperty(name="Backup Time", default="")
    last_message: StringProperty(name="Status", default="Monitoring")
    last_was_error: BoolProperty(name="Error", default=False)


class CYCLICBACKUP_Preferences(AddonPreferences):
    bl_idname = ADDON_PACKAGE

    def draw(self, _context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text="AutoBackupSystem settings are saved per .blend file.")
        layout.label(text="Use 3D Viewport > Sidebar > Auto Backup.")


class CYCLICBACKUP_OT_backup_now(Operator):
    bl_idname = "cyclicbackup.backup_now"
    bl_label = "Back Up Now"
    bl_description = "Create a rotating backup now without resetting the meaningful edit counter"
    bl_options = {"REGISTER"}

    def execute(self, _context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        if not _config()["backup_directory"].strip():
            if _request_backup_location(reset_counter=False):
                self.report({"INFO"}, "Choose a backup location to continue")
                return {"FINISHED"}
            self.report({"ERROR"}, "Unable to open the backup location picker")
            return {"CANCELLED"}

        if _perform_backup(reset_counter=False):
            self.report({"INFO"}, "Backup created; the meaningful edit counter was not reset")
            return {"FINISHED"}
        state = _state()
        message = state.last_message if state else "Unable to create backup"
        self.report({"ERROR"}, message)
        return {"CANCELLED"}


class CYCLICBACKUP_OT_open_backup_folder(Operator):
    bl_idname = "cyclicbackup.open_backup_folder"
    bl_label = "Open Backup Folder"
    bl_description = "Open the current backup folder in the system file browser"
    bl_options = {"INTERNAL"}

    def execute(self, _context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        try:
            directory = _backup_directory()
            directory.mkdir(parents=True, exist_ok=True)
            bpy.ops.wm.path_open(filepath=str(directory))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Unable to open backup folder: {exc}")
            return {"CANCELLED"}


class CYCLICBACKUP_OT_choose_backup_directory(Operator):
    bl_idname = "cyclicbackup.choose_backup_directory"
    bl_label = "Choose Backup Location"
    bl_description = "Choose the folder where rotating backups will be stored"
    bl_options = {"INTERNAL"}

    directory: StringProperty(name="Backup Directory", subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})
    resume_pending_backup: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        settings = _settings(context)
        if settings is None:
            self.report({"ERROR"}, "File-specific settings are unavailable")
            return {"CANCELLED"}

        selected = self.directory.strip()
        if not selected:
            self.report({"ERROR"}, "Please choose a folder")
            return {"CANCELLED"}

        resume_backup = bool(self.resume_pending_backup) or bool(
            _RUNTIME["waiting_for_location"]
        )
        reset_counter = bool(_RUNTIME["pending_backup_reset_counter"])

        try:
            directory = Path(bpy.path.abspath(selected)).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            settings.backup_directory = str(directory)
            _RUNTIME["waiting_for_location"] = False
            if resume_backup:
                _RUNTIME["backup_requested"] = True
                _RUNTIME["pending_backup_reset_counter"] = reset_counter
                state = _state(context)
                if state is not None:
                    state.last_message = "Backup location selected. Preparing backup..."
                    state.last_was_error = False
            self.report({"INFO"}, f"Backup location set to: {directory}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Unable to set backup location: {exc}")
            return {"CANCELLED"}

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        _suppress_plugin_ui_activity()
        initial_directory = _backup_directory()
        if not initial_directory.exists():
            initial_directory = initial_directory.parent
        self.directory = str(initial_directory)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def cancel(self, context: bpy.types.Context) -> None:
        _suppress_plugin_ui_activity()
        if self.resume_pending_backup or _RUNTIME["waiting_for_location"]:
            _RUNTIME["waiting_for_location"] = False
            _RUNTIME["backup_requested"] = False
            state = _state(context)
            if state is not None:
                state.last_message = "Backup postponed: no location was selected"
                state.last_was_error = True
            _tag_redraw()


class CYCLICBACKUP_OT_use_default_directory(Operator):
    bl_idname = "cyclicbackup.use_default_directory"
    bl_label = "Use Default Location"
    bl_description = "Use the AutoBackups folder beside the main file"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        settings = _settings(context)
        if settings is None:
            return {"CANCELLED"}
        try:
            directory = _default_backup_directory()
            directory.mkdir(parents=True, exist_ok=True)
            settings.backup_directory = str(directory)
            self.report({"INFO"}, f"Default backup location set to: {directory}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Unable to set default backup location: {exc}")
            return {"CANCELLED"}


class CYCLICBACKUP_OT_file_settings(Operator):
    bl_idname = "cyclicbackup.file_settings"
    bl_label = "AutoBackup Setup"
    bl_description = "Edit AutoBackupSystem settings saved in this blend file"
    bl_options = {"INTERNAL"}

    auto_prompt: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _settings(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        settings = _settings(context)
        layout = self.layout
        if settings is None:
            layout.label(text="File-specific settings are unavailable", icon="ERROR")
            return

        column = layout.column(align=True)
        column.prop(settings, "enabled", text="Enabled")
        column.prop(settings, "backup_directory", text="Folder")
        column.prop(settings, "operation_target", text="Edit Target")
        column.prop(settings, "backup_slots", text="Keep Files")

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        _suppress_plugin_ui_activity()
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, _context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        _RUNTIME["setup_prompted_session"] = True
        _tag_redraw()
        return {"FINISHED"}

    def cancel(self, _context: bpy.types.Context) -> None:
        _suppress_plugin_ui_activity()
        if (
            self.auto_prompt
            and not bpy.app.background
            and os.environ.get("AUTOBACKUP_SUPPRESS_SETUP_PROMPT") != "1"
            and not _blend_filepath()
        ):
            _RUNTIME["setup_prompted_session"] = False
            _RUNTIME["setup_prompt_pending"] = False
            _schedule_initial_setup_prompt(delay=0.1)
        else:
            _RUNTIME["setup_prompted_session"] = True
        _tag_redraw()


class CYCLICBACKUP_OT_reset_counter(Operator):
    bl_idname = "cyclicbackup.reset_counter"
    bl_label = "Reset Edit Count"
    bl_description = "Reset the current meaningful edit counter to zero"
    bl_options = {"INTERNAL"}

    def invoke(
        self, context: bpy.types.Context, event: bpy.types.Event
    ) -> set[str]:
        _suppress_plugin_ui_activity()
        state = _state(context)
        if state is None or state.operation_count <= 0:
            self.report({"INFO"}, "The meaningful edit counter is already zero")
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Reset Edit Count?",
            message=f"Reset {state.operation_count} meaningful edits to zero?",
            confirm_text="Reset",
            icon="QUESTION",
        )

    def execute(self, _context: bpy.types.Context) -> set[str]:
        _suppress_plugin_ui_activity()
        state = _state()
        if state is None:
            return {"CANCELLED"}
        state.operation_count = 0
        state.last_message = "Meaningful edit counter reset"
        state.last_was_error = False
        _RUNTIME["backup_requested"] = False
        _clear_pending_change()
        _tag_redraw()
        return {"FINISHED"}


class CYCLICBACKUP_PT_panel(Panel):
    bl_label = "AutoBackupSystem"
    bl_idname = "CYCLICBACKUP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Auto Backup"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = _settings(context)
        state = _state(context)

        if settings is None or state is None:
            layout.label(text="AutoBackupSystem settings are not ready", icon="ERROR")
            return

        header = layout.row(align=True)
        header.prop(settings, "enabled", text="Enabled")
        header_status = header.row()
        header_status.alignment = "RIGHT"
        if not settings.enabled:
            header_status.label(text="Paused", icon="PAUSE")
        elif _RUNTIME["waiting_for_location"]:
            header_status.label(text="Needs Location", icon="FILE_FOLDER")
        elif _RUNTIME["backup_running"]:
            header_status.label(text="Backing Up", icon="FILE_TICK")
        elif state.last_was_error:
            header_status.label(text="Attention", icon="ERROR")
        else:
            header_status.label(text="Monitoring", icon="CHECKMARK")

        path_box = layout.box()
        path_box.label(
            text="Backup Location",
            icon="FILE_FOLDER" if settings.backup_directory else "ERROR",
        )
        path_box.prop(settings, "backup_directory", text="")
        path_row = path_box.row(align=True)
        path_row.alert = not bool(settings.backup_directory)
        path_row.operator("cyclicbackup.choose_backup_directory", icon="FILEBROWSER")
        open_row = path_row.row(align=True)
        open_row.enabled = bool(settings.backup_directory)
        open_row.operator(
            "cyclicbackup.open_backup_folder", text="Open", icon="FILE_FOLDER"
        )
        if settings.backup_directory:
            path_box.operator(
                "cyclicbackup.use_default_directory", icon="LOOP_BACK"
            )

        status_box = layout.box()
        target = max(1, settings.operation_target)
        counter_row = status_box.row(align=True)
        counter_row.label(text="Meaningful Edits", icon="REC")
        counter_value = counter_row.row()
        counter_value.alignment = "RIGHT"
        counter_value.label(text=f"{state.operation_count} / {target}")
        status_message = (
            state.last_message if settings.enabled else "Automatic backups are paused"
        )
        status_box.label(
            text=status_message,
            icon="ERROR" if state.last_was_error else "CHECKMARK",
        )
        if state.last_backup_time:
            latest_name = (
                Path(state.last_backup_path).name
                if state.last_backup_path
                else "Backup created"
            )
            status_box.label(text=f"Latest: {latest_name}", icon="FILE_TICK")
            status_box.label(text=state.last_backup_time, icon="TIME")

        settings_box = layout.box()
        settings_header = settings_box.row(align=True)
        settings_header.prop(
            settings,
            "show_backup_settings",
            text="",
            icon="TRIA_DOWN" if settings.show_backup_settings else "TRIA_RIGHT",
            emboss=False,
        )
        settings_header.label(text="Backup Settings")
        settings_header.operator(
            "cyclicbackup.file_settings", text="", icon="PREFERENCES"
        )
        settings_summary = settings_header.row()
        settings_summary.alignment = "RIGHT"
        settings_summary.label(
            text=f"{settings.operation_target} edits / {settings.backup_slots} files"
        )
        if settings.show_backup_settings:
            settings_column = settings_box.column(align=True)
            settings_column.enabled = settings.enabled
            settings_column.prop(settings, "operation_target")
            settings_column.prop(settings, "backup_slots")

        if not settings.backup_directory:
            warning = layout.box()
            warning.label(text="No backup location selected", icon="INFO")
            warning.label(text="A folder picker will open when a backup is triggered.")

        actions = layout.column(align=True)
        actions.scale_y = 1.1
        actions.operator("cyclicbackup.backup_now", icon="FILE_TICK")
        reset_row = layout.row()
        reset_row.enabled = state.operation_count > 0
        reset_row.operator(
            "cyclicbackup.reset_counter",
            text=f"Reset Edit Count: {state.operation_count} → 0",
            icon="LOOP_BACK",
        )


_CLASSES = (
    CYCLICBACKUP_PG_settings,
    CYCLICBACKUP_PG_state,
    CYCLICBACKUP_Preferences,
    CYCLICBACKUP_OT_backup_now,
    CYCLICBACKUP_OT_open_backup_folder,
    CYCLICBACKUP_OT_choose_backup_directory,
    CYCLICBACKUP_OT_use_default_directory,
    CYCLICBACKUP_OT_file_settings,
    CYCLICBACKUP_OT_reset_counter,
    CYCLICBACKUP_PT_panel,
)

_HANDLERS = (
    (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
    (bpy.app.handlers.undo_post, _on_undo_post),
    (bpy.app.handlers.redo_post, _on_redo_post),
    (bpy.app.handlers.frame_change_pre, _on_frame_change),
    (bpy.app.handlers.animation_playback_pre, _on_animation_playback_pre),
    (bpy.app.handlers.animation_playback_post, _on_animation_playback_post),
    (bpy.app.handlers.render_pre, _on_render_pre),
    (bpy.app.handlers.render_post, _on_render_post),
    (bpy.app.handlers.load_post, _on_load_post),
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.cyclic_auto_backup_settings = PointerProperty(
        type=CYCLICBACKUP_PG_settings
    )
    bpy.types.WindowManager.cyclic_auto_backup_state = PointerProperty(
        type=CYCLICBACKUP_PG_state
    )

    for handler_list, handler in _HANDLERS:
        if handler not in handler_list:
            handler_list.append(handler)

    _RUNTIME["registered"] = True
    _RUNTIME["animation_playing"] = False
    _RUNTIME["rendering"] = False
    _reset_detection(keep_count=True)

    # Status text is stored inside the blend file. Reset it on registration so
    # files last saved with an older translated build cannot retain stale UI text.
    state = _state()
    if state is not None:
        state.last_message = "Monitoring"
        state.last_was_error = False

    _schedule_initial_setup_prompt()

    if not bpy.app.timers.is_registered(_monitor_timer):
        bpy.app.timers.register(_monitor_timer, first_interval=TIMER_INTERVAL, persistent=True)


def unregister() -> None:
    _RUNTIME["registered"] = False

    if bpy.app.timers.is_registered(_monitor_timer):
        bpy.app.timers.unregister(_monitor_timer)

    for handler_list, handler in _HANDLERS:
        while handler in handler_list:
            handler_list.remove(handler)

    if hasattr(bpy.types.WindowManager, "cyclic_auto_backup_state"):
        del bpy.types.WindowManager.cyclic_auto_backup_state

    if hasattr(bpy.types.Scene, "cyclic_auto_backup_settings"):
        del bpy.types.Scene.cyclic_auto_backup_settings

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
