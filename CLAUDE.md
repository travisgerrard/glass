# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Glass is a macOS Spotlight-style command bar for screen automation. It captures the primary display, performs OCR using Apple's Vision framework, and allows users to click on found text matches. It also supports recording and playing back macros, including image-based template matching.

## Development Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
python main.py
```

## Requirements

- macOS 13+
- Python 3.9+
- Screen Recording permission (System Settings -> Privacy & Security -> Screen Recording)
- Input Monitoring permission for global hotkey (System Settings -> Privacy & Security -> Input Monitoring)

## Architecture

The application is a single-file Python app (`main.py`) using PyObjC to interface with macOS frameworks:

- **UI Layer**: Custom NSWindow subclasses for the command bar (`CommandBarWindow`), match overlay (`OverlayWindow`), and region selection (`RegionSelectWindow`)
- **Screen Capture**: Uses Quartz `CGWindowListCreateImage` for screenshots
- **OCR**: Uses Apple Vision framework (`VNRecognizeTextRequest`) for text recognition
- **Image Matching**: Uses OpenCV template matching for `find-image` commands
- **Input Handling**: Global hotkey via `CGEventTap` (double-tap Control), keyboard shortcuts for quick click selection

### Key Classes

- `AppController`: Main application logic, command handling, macro execution
- `ScreenOCR`: Screen capture and Vision OCR wrapper
- `CommandBarWindow`: Floating command input window with status/help display
- `OverlayWindow`: Transparent overlay showing numbered match highlights
- `CommandInputTextView`: Custom text view with command history (up/down arrows)

### Macro System

- Macros stored in `macros.json` in project directory (or `~/.glass/macros.json`)
- Images for `find-image` stored in `images/` directory
- Macros can nest up to 5 levels deep via `run <name>`
- Match ordering uses closest-to-last-click proximity

## Commands

- `find <text>` - Capture screen + OCR + highlight matches
- `click <n>` / `rclick <n>` - Click match by number (left/right)
- `record <name>` / `stop` - Record/save macro
- `run <name>` - Execute macro
- `capture-image <name>` - Save region as template (during recording)
- `find-image <name>` - Find image template on screen
