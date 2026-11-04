# Changelog

## 0.1.0 — Initial public release

- Renamed project to **MSC Helper** (MapleStory Classic Helper).
- Settings apply immediately on change; removed the “Apply settings” button.
- Dedicated **Overlay** tab.
- “Go to file location” button opens the app data folder (`%APPDATA%\MSCHelper`).
- Enabled-command list reflects live settings instead of a hardcoded summary.
- Distinct status message when MapleStory is not running vs. running in the background.
- Status badge turns **red** when MapleStory is not running.
- Info icons with hover descriptions next to every setting (cursor is neutral, not “clickable”).
- Recommended values live at the end of each info-icon description.
- Movement-arm keys document support for Space, Ctrl, Shift, Alt, arrows, and F-keys.
- Tesseract OCR enabled by default (`auto_detect_max_stats=True`); dismissible banner when Tesseract is missing.
- Fixed toggles not applying (max HP/MP validation and ability-key parsing when auto-cast is off).
- Fixed General tab showing “process ID 0” when MapleStory is not running.
- Settings tab advises keeping advanced options at recommended defaults.
- Added `.gitignore` for PyInstaller outputs and related artifacts.
- Packaging name: `MSCHelper`.
