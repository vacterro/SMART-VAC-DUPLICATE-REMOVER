<div align="center">

# SMART VAC Duplicate Remover

**Find true duplicate files by content, review them, and remove extras without sacrificing the last verified copy.**

[![Version](https://img.shields.io/badge/version-0.0.4-D4B86A?style=flat-square)](VERSION)
![Platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

[Changelog](CHANGELOG.md) · [Issues](https://github.com/vacterro/SMART-VAC-DUPLICATE-REMOVER/issues)

</div>

## Why this tool

Filename-based duplicate cleaners are quick and wrong in exactly the situations where deletion matters. SMART VAC Duplicate Remover groups candidates and verifies file content with **SHA-256** before anything is offered for removal.

On Windows, deletions go to the **Recycle Bin**, so ordinary mistakes remain recoverable.

## Safety model

- at least one verified copy from every duplicate group is always kept;
- files changed since the scan are refused at deletion time;
- the selected root itself is never removed during empty-folder cleanup;
- deletion is reviewed in the result tree before execution;
- optional logging records removed paths in `deleted_log.txt`;
- Windows uses Recycle Bin deletion instead of immediate permanent removal.

## Quick start

```powershell
python delete_duplicates_gui.py
```

Requirements: **Windows** and **Python 3.10+**.

No executable is committed to the repository. A tagged release may provide one; otherwise build it locally from the supplied PyInstaller spec.

## Workflow

1. Choose the directory to scan.
2. Click **Find Duplicates**.
3. Review duplicate groups in the result tree.
4. Stop a long scan at any time with **Stop Scan**.
5. Select the copies you want removed.
6. Delete them to the Recycle Bin.
7. Optionally run **Delete Empty Folders** afterward.

## Build

```powershell
pyinstaller delete_duplicates_gui.spec
```

For reproducible releases, record the application [VERSION](VERSION), source commit, PyInstaller/tool versions, and resulting executable SHA-256.

## Repository

| Path | Purpose |
|---|---|
| `delete_duplicates_gui.py` | GUI and duplicate-detection engine |
| `delete_duplicates_gui.spec` | PyInstaller build recipe |
| `test_delete_duplicates.py` | automated test suite |
| `VERSION` | canonical application version |
| `CHANGELOG.md` | release history |

## Languages

[English](README.md) · [Русский](README.ru.md) · [Eesti](README.ee.md) · [Українська](README.uk.md) · [日本語](README.ja.md) · [Дед](README.ded.md)

## License

[MIT](LICENSE)


## Project network

Part of the broader **SAIPEN / vacterro** project ecosystem.

[**Author hub**](https://github.com/vacterro) · [**SAIPEN HQ**](https://github.com/saipenhq) · [**SAIPEN Core**](https://github.com/vacterro/saipen) · [**ZAICODE**](https://github.com/vacterro/zaicode) · [**FastPrompter**](https://github.com/vacterro/FastPrompter) · [**SAIPEN Community**](https://discord.gg/SEYaYkuVgN)

For reproducible bugs and durable feature requests, use [GitHub Issues](https://github.com/vacterro/SMART-VAC-DUPLICATE-REMOVER/issues).

<!-- VACTERRO_SUPPORT:BEGIN -->
---
<sub>If SMART VAC Duplicate Remover is useful to you, optional support: [Buy Me a Coffee](https://buymeacoffee.com/vacuum34) · [Boosty](https://boosty.to/vacuum34/donate) · [PayPal](https://paypal.me/AlexNelin) · [other ways](https://github.com/vacterro/vacterro/blob/main/SUPPORT.md)</sub>
<!-- VACTERRO_SUPPORT:END -->
