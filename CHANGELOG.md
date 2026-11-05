# Changelog

## 0.2.0 — Auto-update, auto-cast sets, overlay polish

- Focus must be held for a configurable duration (default 1000 ms) before any key is sent; focus is re-checked immediately before SendInput.
- Passive background HUD reading is optional and **off by default** (prevents under-reads that force potions on tab-in).
- Turning off both auto-pot toggles automatically disables HUD monitoring and Tesseract OCR.

- Update check only installs when the remote version is **strictly newer** (prevents downgrading to an older GitHub VERSION).
- Manual check shows “You are up to date” when current; when newer, asks “There is a new version x, do you want to update?” before downloading.
- Visible progress window while downloading/installing an update.
- App data version marker is `%APPDATA%\MSCHelper\VERSION` (legacy `installed_version.txt` is migrated).
- Session log file `latest.log` written under `%APPDATA%\MSCHelper\logs\` (alongside dated rotating logs).
- Removed overlay transparent mode (Tk cannot do reliable 60% background-only transparency without breaking text).
- Overlay minimum size restored to 360×300 for stable layout (no dynamic font scaling).
- Potion keys are never sent while the game is unfocused, and are blocked until the HUD monitor has completed at least one focused scan interval. Auto-cast is unchanged.

- File logging to `%APPDATA%\MSCHelper\logs\` with daily rotating logs.
- Automatic update check against the GitHub `VERSION` file and latest release; optional under **Settings**. Downloads a new executable and restarts when running the packaged `.exe`.
- Auto-cast now cycles ability keys in order (Q → E → R …) with a configurable delay **between** keys and a separate delay **after** a full set.
- Overlay minimum size reduced; optional 60% transparent background (enabled by default).
- Main window uses `icon.ico` more reliably; title shows the version number.
- Minor loop timing tweaks to reduce game stuttering while auto-cast is active.
- Removed unused root-level `health_points.png` / `mana_points.png` assets.
- Documentation and packaging updated for the 0.2.0 release.

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
