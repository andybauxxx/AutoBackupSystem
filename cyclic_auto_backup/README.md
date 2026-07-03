# AutoBackupSystem

This Blender add-on creates a rotating `.blend` backup after a configurable number of meaningful edits. By default it backs up every 50 meaningful edits and keeps five files; the sixth backup deletes the actual oldest backup after the new numbered file is created.

## Meaningful edits

The add-on prioritizes completed undoable Blender operations and uses scene-data monitoring to detect direct property changes:

- Modeling, object transforms, materials, nodes, animation, and similar content changes are counted.
- Continuous updates from one drag, slider adjustment, or sculpt stroke are grouped into one edit.
- Undo and Redo each count as one edit.
- Selection, viewport navigation, animation playback, frame changes, and interface actions are not counted.
- Cancelled operations, automatic backups, and normal saves are not counted.

Blender does not expose one universal “operation completed” event across every mode and third-party add-on. Direct property changes are therefore recognized with a short merge delay. This delay only identifies one complete edit and never triggers a time-based backup.

## Installation

1. Open `Edit > Preferences > Add-ons` in Blender.
2. Choose `Install from Disk...`.
3. Select `auto_backup_system-1.4.6.zip`.
4. Enable `AutoBackupSystem`.
5. Open the `Auto Backup` tab in the 3D Viewport sidebar (`N`).

Supported Blender versions: 4.2 and later.

## Per-file settings

AutoBackupSystem settings are saved inside each `.blend` file. Each project can have its own enabled state, backup location, edit target, retention count, and edit merge delay.

When a new unsaved file is opened, AutoBackupSystem shows a small setup dialog so the file can start with its own backup settings. If the dialog is dismissed, the same settings are still available in the `Auto Backup` sidebar panel.

## Backup location

- Click `Choose Backup Location` in the add-on panel to select a folder, or enter a path directly.
- If no location is set when the meaningful-edit target is reached, a folder picker opens automatically. The backup continues after a folder is selected.
- If the picker is cancelled, the counter is retained and the add-on asks again after the next meaningful edit.
- `Use Default Location` selects an `AutoBackups` folder beside a saved main file, or Blender's temporary folder for an unsaved file.

Backup filenames use this format:

```text
Project_abs_01.blend
Project_abs_02.blend
Project_abs_03.blend
Project_abs_04.blend
Project_abs_05.blend
```

Sequence numbers always increase. For example, if `Backups to Keep` is 3, the fourth backup is named `Project_abs_04.blend`; after it is saved successfully, the actual oldest backup is removed so only three files remain. The next backup is `_abs_05`, not `_abs_01`.

The add-on always saves a copy and never changes the current main-file path. The meaningful edit counter resets only after a successful automatic backup. `Back Up Now` creates a manual test backup without resetting the counter.
