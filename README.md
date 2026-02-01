# Glass (MVP)

Spotlight-style command bar for capturing the primary display, searching OCR text, and clicking matches.

## Requirements

- macOS 13+
- Python 3.9+
- Screen Recording permission (System Settings -> Privacy & Security -> Screen Recording)
- Input Monitoring permission for the global hotkey (System Settings -> Privacy & Security -> Input Monitoring)

## Setup

1. `python3 -m venv .venv`
2. `source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `python main.py`

## Usage

- Double tap Control to toggle the command bar.
- Commands:
  - `capture`
  - `find <text>` (always performs a fresh capture + OCR)
  - `click <number>` (left click)
  - `rclick <number>` (right click)
  - `clear`
  - `help`
  - `screens` (list displays)
  - `screen <n>|auto` (set active display)
  - `record <name>` (start recording a macro)
  - `stop` (save the current macro)
  - `run <name>`
  - `macros` (list saved macros)
  - `show <name>` (show macro steps)
  - `delete <name>` (remove macro)
- Shortcut: when matches are shown and input is empty, press 1-9 to left click, a-i to right click.

## Notes

- Supports selecting the active display (default: whichever screen the command bar is on). Use `screens` / `screen <n>`.
- Screen capture uses Quartz `CGWindowListCreateImage` (ScreenCaptureKit omitted for simplicity).
- If the hotkey does not respond, enable Input Monitoring for your terminal or Python.
- Match numbering uses closest-first ordering relative to the last click location (fallback: screen center).
- Macros are stored at `~/.glass/macros.json`.
- Macros can call other macros via `run <name>` (nesting limit: 5).
