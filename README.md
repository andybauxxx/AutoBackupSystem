# AutoBackupSystem for Blender

AutoBackupSystem is a Blender add-on that creates rotating `.blend` backups after a configurable number of meaningful edits.

Default behavior:

- Back up every 50 meaningful edits.
- Keep five backup files.
- Continue filename numbering forever, then delete the actual oldest backup when the retention limit is exceeded.
- Ask for a backup folder automatically when a backup is due and no location has been configured.

Supported Blender versions: 4.2 and later.

## Install

1. Download the latest `auto_backup_system-*.zip` from GitHub Releases.
2. In Blender, open `Edit > Preferences > Add-ons`.
3. Choose `Install from Disk...`.
4. Select the zip file.
5. Enable `AutoBackupSystem`.
6. Open the `Auto Backup` tab in the 3D Viewport sidebar (`N`).

## Repository layout

```text
cyclic_auto_backup/          Blender extension source
tests/                       Blender smoke and UI test scripts
auto_backup_system-*.zip     Local build artifacts, published through Releases
```

The extension source and usage details are in [`cyclic_auto_backup/README.md`](cyclic_auto_backup/README.md).

## Build

From the project root, run Blender's extension builder:

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe' --command extension build --source-dir '.\cyclic_auto_backup' --output-filepath '.\auto_backup_system-1.3.2.zip' --verbose
```

## Validate

```powershell
& 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe' --command extension validate '.\auto_backup_system-1.3.2.zip'
```

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
