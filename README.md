# SMART VAC DUPLICATE REMOVER

**v0.0.2**

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Version](https://img.shields.io/badge/version-0.0.1-green.svg)

A robust Windows tool to find and delete duplicate files — deletions go to the Recycle Bin (recoverable) — with a clean UI, SHA-256 hash checking, and detailed logging.

[🤍 Support Developer](https://buymeacoffee.com/vacuum34)

## Version
0.0.1

## Features
- **SHA-256 Hashing**: Identifies identical files accurately, regardless of name.
- **Recycle-Bin Deletion**: Deletions are sent to the Windows Recycle Bin and can be restored. On non-Windows systems there is no Recycle Bin, and the confirmation dialog says so before deleting permanently.
- **Empty Folder Cleanup**: Easily prune leftover empty directories (the selected root is never removed).
- **Detailed Logging**: Records every deletion to `deleted_log.txt` for peace of mind.
- **Safe by default**: At least one verified copy of every duplicate group is always kept; files changed since scanning are never deleted.

## Installation
1. Clone this repository
2. Run `python delete_duplicates_gui.py` with Python 3.10+
3. Launch the GUI and pick a folder to scan

No executable is committed to the repository. Build one yourself with the
PyInstaller recipe below, or download it from a tagged GitHub release when one
is published.

## Usage
- Select a target directory
- Click **Find Duplicates** to scan recursively (SHA-256)
- Click **Stop Scan** to cancel a long scan
- Review detected duplicates in the result tree
- Optionally toggle deletion logging
- Delete selected duplicates (sent to the Recycle Bin, recoverable)
- Use **Delete Empty Folders** to prune empty directories

## Build from source (W2-004)
The Windows executable is produced with PyInstaller from `delete_duplicates_gui.spec`:

```
pyinstaller delete_duplicates_gui.spec
```

Releases should be built from a clean, tagged commit. Record the application `VERSION`, the source commit SHA, the PyInstaller/tool versions, and the produced executable SHA-256 in release metadata so the binary can be reproduced and verified.

## Requirements
- Windows
- Python 3.10+ (the app runs from source; there is no committed binary)

## License
MIT License

## Support
For issues and feature requests, please use the GitHub repository.
