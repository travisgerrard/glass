import json
import os
import re
import subprocess
import threading
import time
import warnings

import AppKit
import CoreFoundation
import Foundation
import Quartz
import Vision
import objc
import signal
import cv2
import numpy as np


def run_on_main(func):
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(func)


objc_super = objc.super
warnings.filterwarnings("ignore", category=objc.ObjCSuperWarning)


class CommandBarNSWindow(AppKit.NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class NonInteractiveWindow(AppKit.NSWindow):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class CommandInputTextView(AppKit.NSTextView):
    def initWithFrame_controller_(self, frame, controller):
        self = objc_super(CommandInputTextView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.controller = controller
        self.setDrawsBackground_(False)
        self.setRichText_(False)
        self.setImportsGraphics_(False)
        self.setUsesFontPanel_(False)
        self.setAllowsUndo_(True)
        self.setHorizontallyResizable_(False)
        self.setVerticallyResizable_(False)
        self.setEditable_(True)
        self.setSelectable_(True)
        container = self.textContainer()
        if container is not None:
            container.setLineBreakMode_(AppKit.NSLineBreakByClipping)
            container.setWidthTracksTextView_(True)
            container.setHeightTracksTextView_(True)
        return self

    def mouseDragged_(self, event):
        window = self.window()
        if window is not None:
            window.performWindowDragWithEvent_(event)
            # After dragging, treat the window's screen as the active screen.
            # Reset follow mode so a prior manual screen override doesn't block this.
            try:
                self.controller._follow_command_bar = True
                self.controller._sync_active_screen_to_command_bar(announce=True)
            except Exception:
                pass
        else:
            AppKit.NSTextView.mouseDragged_(self, event)

    def setFrame_(self, frame):
        objc_super(CommandInputTextView, self).setFrame_(frame)
        self._update_insets()

    def _update_insets(self):
        font = self.font()
        if font is None:
            return
        line_height = font.ascender() - font.descender() + font.leading()
        frame = self.frame()
        inset = max(0, (frame.size.height - line_height) / 2.0)
        self.setTextContainerInset_(AppKit.NSMakeSize(0, inset))
        container = self.textContainer()
        if container is not None:
            container.setContainerSize_(AppKit.NSMakeSize(frame.size.width, frame.size.height))
            container.setWidthTracksTextView_(True)

    def keyDown_(self, event):
        key_code = event.keyCode()
        if key_code in (36, 76):  # Enter/Return
            text = str(self.string()).strip()
            if text:
                self.controller._command_history.append(text)
                self.controller._history_index = -1
            self.controller.handle_command(text)
            return
        if key_code == 53:  # ESC
            self.controller.clear_and_close()
            return
        if key_code == 126:  # Up arrow
            history = self.controller._command_history
            if history:
                if self.controller._history_index == -1:
                    self.controller._history_index = len(history) - 1
                elif self.controller._history_index > 0:
                    self.controller._history_index -= 1
                self.setString_(history[self.controller._history_index])
                self.setSelectedRange_(Foundation.NSMakeRange(len(self.string()), 0))
            return
        if key_code == 125:  # Down arrow
            history = self.controller._command_history
            if history and self.controller._history_index != -1:
                if self.controller._history_index < len(history) - 1:
                    self.controller._history_index += 1
                    self.setString_(history[self.controller._history_index])
                else:
                    self.controller._history_index = -1
                    self.setString_("")
                self.setSelectedRange_(Foundation.NSMakeRange(len(self.string()), 0))
            return
        AppKit.NSTextView.keyDown_(self, event)


class CommandBarWindow(AppKit.NSObject):
    def initWithController_screenFrame_(self, controller, screen_frame):
        self = objc_super(CommandBarWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self.screen_frame = screen_frame
        self.visible = False
        self.help_visible = False
        self.status_visible = False
        self._build_windows()
        return self

    def _build_windows(self):
        self.width = 640
        self.padding_x = 20
        self.input_height = 22
        self.status_height = 14
        self.help_height = 72
        self.current_help_height = self.help_height
        self.vertical_pad_top = 10
        self.vertical_pad_bottom = 10
        self.inter_spacing = 6
        self.status_spacing = 5

        height = self._compute_height(help_visible=False, status_visible=False)
        rect = self._centered_rect(self.width, height)

        style = AppKit.NSWindowStyleMaskBorderless
        self.window = CommandBarNSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setLevel_(AppKit.NSScreenSaverWindowLevel)
        self.window.setMovableByWindowBackground_(True)
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        self.window.setReleasedWhenClosed_(False)

        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, self.width, height)
        )
        effect.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        effect.setMaterial_(AppKit.NSVisualEffectMaterialDark)
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(AppKit.NSVisualEffectStateActive)
        effect.setWantsLayer_(True)
        effect.layer().setCornerRadius_(12.0)
        effect.layer().setMasksToBounds_(True)
        self.window.setContentView_(effect)

        input_rect = AppKit.NSMakeRect(
            self.padding_x,
            0,
            self.width - (self.padding_x * 2),
            self.input_height,
        )
        self.input_field = CommandInputTextView.alloc().initWithFrame_controller_(
            input_rect, self.controller
        )
        font = AppKit.NSFont.systemFontOfSize_(18)
        self.input_field.setFont_(font)
        self.input_field._update_insets()
        self.input_field.setTextColor_(AppKit.NSColor.whiteColor())
        self.input_field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        effect.addSubview_(self.input_field)

        status_rect = AppKit.NSMakeRect(
            self.padding_x,
            0,
            self.width - (self.padding_x * 2),
            self.status_height,
        )
        self.status_field = AppKit.NSTextField.alloc().initWithFrame_(status_rect)
        self.status_field.setEditable_(False)
        self.status_field.setBordered_(False)
        self.status_field.setDrawsBackground_(False)
        self.status_field.setSelectable_(False)
        self.status_field.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        self.status_field.setTextColor_(
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.7)
        )
        self.status_field.setStringValue_("")
        self.status_field.setHidden_(True)
        effect.addSubview_(self.status_field)

        help_rect = AppKit.NSMakeRect(
            self.padding_x,
            0,
            self.width - (self.padding_x * 2),
            self.help_height,
        )
        self.help_field = AppKit.NSTextField.alloc().initWithFrame_(help_rect)
        self.help_field.setEditable_(False)
        self.help_field.setBordered_(False)
        self.help_field.setDrawsBackground_(False)
        self.help_field.setSelectable_(False)
        self.help_field.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        self.help_field.setTextColor_(
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.75)
        )
        self.help_field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        self.help_field.setMaximumNumberOfLines_(0)
        self.help_field.setAlignment_(AppKit.NSTextAlignmentLeft)
        self.help_field.cell().setUsesSingleLineMode_(False)
        self.help_field.cell().setWraps_(True)
        self.help_field.setHidden_(True)
        effect.addSubview_(self.help_field)

        self._apply_layout(help_visible=False)
        self.dimming_window = self._build_dimming_window()

    def _compute_height(self, help_visible, status_visible):
        help_height = self.current_help_height if help_visible else 0
        status_height = self.status_height if status_visible else 0
        gap_help_status = self.status_spacing if (help_visible and status_visible) else 0
        gap_status_input = self.inter_spacing if status_visible else 0
        status_y = self.vertical_pad_bottom + help_height
        if help_visible:
            status_y += gap_help_status
        input_y = status_y + status_height + gap_status_input
        return input_y + self.input_height + self.vertical_pad_top

    def _centered_rect(self, width, height):
        x = self.screen_frame.origin.x + (self.screen_frame.size.width - width) / 2.0
        y = self.screen_frame.origin.y + (self.screen_frame.size.height - height) / 2.0
        return AppKit.NSMakeRect(x, y, width, height)

    def _apply_layout(self, help_visible=None, status_visible=None):
        if help_visible is None:
            help_visible = self.help_visible
        if status_visible is None:
            status_visible = self.status_visible
        height = self._compute_height(help_visible, status_visible)
        frame = self.window.frame()
        center_x = frame.origin.x + (frame.size.width / 2.0)
        center_y = frame.origin.y + (frame.size.height / 2.0)
        new_origin_x = center_x - (self.width / 2.0)
        new_origin_y = center_y - (height / 2.0)
        new_frame = AppKit.NSMakeRect(new_origin_x, new_origin_y, self.width, height)
        self.window.setFrame_display_(new_frame, True)
        self.window.contentView().setFrame_(AppKit.NSMakeRect(0, 0, self.width, height))

        help_height = self.current_help_height if help_visible else 0
        status_height = self.status_height if status_visible else 0
        help_y = self.vertical_pad_bottom
        status_y = help_y + help_height
        if help_visible and status_visible:
            status_y += self.status_spacing
        input_y = status_y + status_height
        if status_visible:
            input_y += self.inter_spacing

        self.input_field.setFrame_(
            AppKit.NSMakeRect(
                self.padding_x,
                input_y,
                self.width - (self.padding_x * 2),
                self.input_height,
            )
        )
        self.status_field.setFrame_(
            AppKit.NSMakeRect(
                self.padding_x,
                status_y,
                self.width - (self.padding_x * 2),
                self.status_height,
            )
        )
        self.help_field.setFrame_(
            AppKit.NSMakeRect(
                self.padding_x,
                help_y,
                self.width - (self.padding_x * 2),
                help_height,
            )
        )

    def _build_dimming_window(self):
        rect = self.screen_frame
        style = AppKit.NSWindowStyleMaskBorderless
        window = NonInteractiveWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setOpaque_(False)
        window.setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.35)
        )
        window.setLevel_(AppKit.NSStatusWindowLevel + 1)
        window.setIgnoresMouseEvents_(True)
        window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        window.setReleasedWhenClosed_(False)
        return window

    def show(self):
        if self.visible:
            return
        self.visible = True
        self.dimming_window.orderFrontRegardless()
        self.window.orderFrontRegardless()
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.window.makeFirstResponder_(self.input_field)

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self.window.orderOut_(None)
        self.dimming_window.orderOut_(None)

    def set_status(self, text):
        self.status_field.setStringValue_(text)
        visible = bool(text) or self.help_visible
        if visible != self.status_visible:
            self.status_visible = visible
            self.status_field.setHidden_(not visible)
            self._apply_layout()

    def show_help(self, text):
        self.help_visible = True
        lines = text.count("\n") + 1
        # Each line needs ~18 pixels (12pt font + spacing), plus padding
        desired_height = max(self.help_height, (lines * 18) + 10)
        self.current_help_height = min(300, desired_height)
        self.help_field.setStringValue_(text)
        self.help_field.setHidden_(False)
        self.status_visible = True
        self.status_field.setHidden_(False)
        self._apply_layout(help_visible=True, status_visible=True)

    def hide_help(self):
        if not self.help_visible:
            return
        self.help_visible = False
        self.current_help_height = self.help_height
        self.help_field.setHidden_(True)
        self.status_visible = bool(self.status_field.stringValue())
        self.status_field.setHidden_(not self.status_visible)
        self._apply_layout(help_visible=False, status_visible=self.status_visible)

    def clear_input(self):
        self.input_field.setString_("")

    def input_text(self):
        return str(self.input_field.string())


class OverlayView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc_super(OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.draw_items = []
        self.setWantsLayer_(True)
        return self

    def setDrawItems_(self, items):
        self.draw_items = items
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        for item in self.draw_items:
            highlight_rect = item["rect"]
            fill_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.8, 0.0, 0.25
            )
            stroke_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.8, 0.0, 0.9
            )
            path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                highlight_rect, 6, 6
            )
            fill_color.setFill()
            path.fill()
            stroke_color.setStroke()
            path.setLineWidth_(2.0)
            path.stroke()

            label = item["label"]
            font = AppKit.NSFont.boldSystemFontOfSize_(14)
            attrs = {
                AppKit.NSFontAttributeName: font,
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
            }
            ns_label = AppKit.NSString.stringWithString_(label)
            label_size = ns_label.sizeWithAttributes_(attrs)
            pad_x = 6
            pad_y = 2
            label_rect = AppKit.NSMakeRect(
                highlight_rect.origin.x + 4,
                highlight_rect.origin.y + highlight_rect.size.height - label_size.height - 6,
                label_size.width + (pad_x * 2),
                label_size.height + (pad_y * 2),
            )
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.6).setFill()
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                label_rect, 4, 4
            ).fill()
            text_point = AppKit.NSMakePoint(
                label_rect.origin.x + pad_x,
                label_rect.origin.y + pad_y,
            )
            ns_label.drawAtPoint_withAttributes_(text_point, attrs)


class OverlayWindow(AppKit.NSObject):
    def initWithScreenFrame_(self, screen_frame):
        self = objc_super(OverlayWindow, self).init()
        if self is None:
            return None
        self.screen_frame = screen_frame
        self.window = self._build_window()
        self.view = self.window.contentView()
        return self

    def _build_window(self):
        rect = self.screen_frame
        style = AppKit.NSWindowStyleMaskBorderless
        window = NonInteractiveWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setOpaque_(False)
        window.setBackgroundColor_(AppKit.NSColor.clearColor())
        window.setLevel_(AppKit.NSStatusWindowLevel)
        window.setIgnoresMouseEvents_(True)
        window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        window.setReleasedWhenClosed_(False)
        view = OverlayView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, rect.size.width, rect.size.height)
        )
        view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        window.setContentView_(view)
        return window

    def show_matches(self, matches, screen_height):
        items = []
        for index, match in enumerate(matches, start=1):
            x, y, w, h = match["bbox"]
            y_bottom = screen_height - y - h
            rect = AppKit.NSMakeRect(x, y_bottom, w, h)
            label = str(index)
            if index <= 26:
                letter = chr(ord("a") + index - 1)
                label = f"{index} / {letter}"
            items.append({"rect": rect, "index": index, "label": label})
        self.view.setDrawItems_(items)
        if items:
            self.window.orderFrontRegardless()
        else:
            self.window.orderOut_(None)

    def clear(self):
        self.view.setDrawItems_([])
        self.window.orderOut_(None)


class RegionSelectView(AppKit.NSView):
    def initWithFrame_callback_(self, frame, callback):
        self = objc_super(RegionSelectView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._callback = callback
        self._start_point = None
        self._current_point = None
        self._selecting = False
        return self

    def acceptsFirstResponder(self):
        return True

    def mouseDown_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        self._start_point = loc
        self._current_point = loc
        self._selecting = True
        self.setNeedsDisplay_(True)

    def mouseDragged_(self, event):
        if self._selecting:
            loc = self.convertPoint_fromView_(event.locationInWindow(), None)
            self._current_point = loc
            self.setNeedsDisplay_(True)

    def mouseUp_(self, event):
        if self._selecting:
            loc = self.convertPoint_fromView_(event.locationInWindow(), None)
            self._current_point = loc
            self._selecting = False
            self.setNeedsDisplay_(True)
            if self._start_point and self._current_point:
                x1 = min(self._start_point.x, self._current_point.x)
                y1 = min(self._start_point.y, self._current_point.y)
                x2 = max(self._start_point.x, self._current_point.x)
                y2 = max(self._start_point.y, self._current_point.y)
                w = x2 - x1
                h = y2 - y1
                if w > 5 and h > 5:
                    screen_height = self.frame().size.height
                    # Convert from view coords (origin bottom-left) to screen (origin top-left)
                    y_top = screen_height - y2
                    if self._callback:
                        self._callback((x1, y_top, w, h))

    def keyDown_(self, event):
        if event.keyCode() == 53:  # ESC
            if self._callback:
                self._callback(None)

    def drawRect_(self, rect):
        AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.3).set()
        AppKit.NSRectFill(self.bounds())
        if self._start_point and self._current_point:
            x1 = min(self._start_point.x, self._current_point.x)
            y1 = min(self._start_point.y, self._current_point.y)
            x2 = max(self._start_point.x, self._current_point.x)
            y2 = max(self._start_point.y, self._current_point.y)
            sel_rect = AppKit.NSMakeRect(x1, y1, x2 - x1, y2 - y1)
            # Clear selection area
            AppKit.NSColor.clearColor().set()
            AppKit.NSRectFill(sel_rect)
            # Draw border
            AppKit.NSColor.systemBlueColor().set()
            path = AppKit.NSBezierPath.bezierPathWithRect_(sel_rect)
            path.setLineWidth_(2.0)
            path.stroke()


class RegionSelectWindow:
    def __init__(self, screen_frame, callback):
        self.callback = callback
        self.screen_frame = screen_frame
        self._build_window()

    def _build_window(self):
        style = AppKit.NSWindowStyleMaskBorderless
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            self.screen_frame,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setLevel_(AppKit.NSScreenSaverWindowLevel + 1)
        self.window.setIgnoresMouseEvents_(False)

        self.view = RegionSelectView.alloc().initWithFrame_callback_(
            self.screen_frame, self._on_selection
        )
        self.window.setContentView_(self.view)

    def _on_selection(self, bounds):
        self.window.orderOut_(None)
        if self.callback:
            self.callback(bounds)

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self.view)


class ScreenOCR:
    def __init__(self):
        pass

    def capture_display(self, display_id, screen_size_points):
        """Capture a specific display.

        Returns:
          (cg_image, width_px, height_px, scale, display_bounds_px)

        Notes:
        - CGDisplayBounds are in *pixel* coordinates in the global display space.
        - `scale` is pixels-per-point for this capture.
        """
        bounds_px = Quartz.CGDisplayBounds(display_id)
        image = Quartz.CGWindowListCreateImage(
            bounds_px,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image is None:
            raise PermissionError("Screen Recording permission required")

        width_px = Quartz.CGImageGetWidth(image)
        height_px = Quartz.CGImageGetHeight(image)
        if width_px == 0 or height_px == 0:
            raise PermissionError("Screen Recording permission required")

        width_pts, height_pts = screen_size_points
        if width_pts:
            scale = width_px / float(width_pts)
        else:
            scale = 1.0

        return image, width_px, height_px, scale, bounds_px

    def recognize_text(self, cg_image, width_px, height_px, scale):
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, None
        )
        success, error = handler.performRequests_error_([request], None)
        if not success:
            raise RuntimeError(str(error))

        items = []
        results = request.results()
        if results:
            for observation in results:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                vn_text = candidates[0]
                text = vn_text.string()
                if not text:
                    continue
                bbox = observation.boundingBox()
                x_px = bbox.origin.x * width_px
                y_px = bbox.origin.y * height_px
                w_px = bbox.size.width * width_px
                h_px = bbox.size.height * height_px
                # Vision bbox origin is lower-left; convert to top-left points.
                x_pt = x_px / scale
                y_top_pt = (height_px - (y_px + h_px)) / scale
                w_pt = w_px / scale
                h_pt = h_px / scale
                items.append(
                    {
                        "text": text,
                        "bbox": (x_pt, y_top_pt, w_pt, h_pt),
                        "vn_text": vn_text,
                    }
                )
        return items


# ── Step type metadata ────────────────────────────────────────────────────────

_STEP_TYPES = [
    "wait", "key-press", "find", "find-wait",
    "smart-click", "smart-rclick", "smart-dclick",
    "click-at", "rclick-at", "dclick-at", "click-relative",
    "capture", "clear", "run", "find-image", "type", "type-keys",
]

_KEY_NAMES = ["down", "up", "left", "right", "page-down", "page-up"]


def _parse_smart_click_arg(arg):
    """Parse the argument portion of a smart-click step. Returns (query, x_pct, y_pct)."""
    query, x_pct, y_pct = "", None, None
    if not arg:
        return query, x_pct, y_pct
    if arg.startswith('"'):
        end = arg.find('"', 1)
        if end >= 0:
            query = arg[1:end]
            rest = arg[end + 1:].strip()
            parts = rest.split()
            if len(parts) >= 2:
                try:
                    x_pct, y_pct = float(parts[0]), float(parts[1])
                except ValueError:
                    pass
    else:
        parts = arg.split()
        if parts:
            query = parts[0]
            if len(parts) >= 3:
                try:
                    x_pct, y_pct = float(parts[1]), float(parts[2])
                except ValueError:
                    pass
    return query, x_pct, y_pct


class MacroEditorWindow(AppKit.NSObject):
    """Two-panel graphical macro editor."""

    # ── Init ─────────────────────────────────────────────────────────────────

    def initWithController_(self, controller):
        self = objc_super(MacroEditorWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self._macro_names = []
        self._selected_macro = None
        self._steps = []
        self._sheet = None
        self._editing_row = None
        self._editing_insert_at = None
        self._build_window()
        return self

    # ── Window construction ───────────────────────────────────────────────────

    def _build_window(self):
        style = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        rect = AppKit.NSMakeRect(200, 200, 800, 520)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Glass – Macro Editor")
        self.window.setDelegate_(self)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_(AppKit.NSMakeSize(600, 350))

        content = self.window.contentView()
        self._build_content(content)

    def _build_content(self, content):
        LEFT_W = 210
        BTN_H = 24
        BTN_AREA_H = BTN_H + 16   # fixed-height bottom strip for buttons
        SEP_W = 1

        # ── Vertical separator ────────────────────────────────────────────────
        sep = AppKit.NSBox.alloc().initWithFrame_(
            AppKit.NSMakeRect(LEFT_W, 0, SEP_W, 520)
        )
        sep.setBoxType_(AppKit.NSBoxSeparator)
        sep.setAutoresizingMask_(AppKit.NSViewHeightSizable)
        content.addSubview_(sep)

        # ── Left: macro list ──────────────────────────────────────────────────
        macro_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, BTN_AREA_H, LEFT_W, 520 - BTN_AREA_H)
        )
        macro_scroll.setHasVerticalScroller_(True)
        macro_scroll.setAutohidesScrollers_(True)
        macro_scroll.setBorderType_(AppKit.NSNoBorder)
        macro_scroll.setAutoresizingMask_(AppKit.NSViewHeightSizable)

        self.macro_table = AppKit.NSTableView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, LEFT_W, 520 - BTN_AREA_H)
        )
        col = AppKit.NSTableColumn.alloc().initWithIdentifier_("name")
        col.setTitle_("Macros")
        col.setEditable_(False)
        col.setMinWidth_(80)
        self.macro_table.addTableColumn_(col)
        self.macro_table.setHeaderView_(None)
        self.macro_table.setDataSource_(self)
        self.macro_table.setDelegate_(self)
        self.macro_table.setTag_(1)
        self.macro_table.setAllowsEmptySelection_(True)
        self.macro_table.setUsesAlternatingRowBackgroundColors_(True)
        macro_scroll.setDocumentView_(self.macro_table)
        content.addSubview_(macro_scroll)

        # Left buttons
        BY = 8
        new_btn = self._make_button("+ New", AppKit.NSMakeRect(8, BY, 54, BTN_H), "newMacro:")
        ren_btn = self._make_button("Rename", AppKit.NSMakeRect(68, BY, 64, BTN_H), "renameMacro:")
        del_macro_btn = self._make_button("Delete", AppKit.NSMakeRect(138, BY, 60, BTN_H), "deleteMacro:")
        for b in (new_btn, ren_btn, del_macro_btn):
            content.addSubview_(b)

        # ── Right: step list ──────────────────────────────────────────────────
        RX = LEFT_W + SEP_W
        step_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(RX, BTN_AREA_H, 800 - RX, 520 - BTN_AREA_H)
        )
        step_scroll.setHasVerticalScroller_(True)
        step_scroll.setAutohidesScrollers_(True)
        step_scroll.setBorderType_(AppKit.NSNoBorder)
        step_scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        self.step_table = AppKit.NSTableView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 800 - RX, 520 - BTN_AREA_H)
        )
        num_col = AppKit.NSTableColumn.alloc().initWithIdentifier_("num")
        num_col.setTitle_("#")
        num_col.setWidth_(32)
        num_col.setMinWidth_(32)
        num_col.setMaxWidth_(32)
        num_col.setEditable_(False)
        self.step_table.addTableColumn_(num_col)

        step_col = AppKit.NSTableColumn.alloc().initWithIdentifier_("step")
        step_col.setTitle_("Step")
        step_col.setEditable_(False)
        step_col.setMinWidth_(200)
        self.step_table.addTableColumn_(step_col)

        self.step_table.setDataSource_(self)
        self.step_table.setDelegate_(self)
        self.step_table.setTag_(2)
        self.step_table.setAllowsEmptySelection_(True)
        self.step_table.setUsesAlternatingRowBackgroundColors_(True)
        self.step_table.setDoubleAction_("editStep:")
        self.step_table.setTarget_(self)
        step_scroll.setDocumentView_(self.step_table)
        content.addSubview_(step_scroll)

        # Right buttons
        up_btn = self._make_button("↑", AppKit.NSMakeRect(RX + 8, BY, 30, BTN_H), "moveStepUp:")
        dn_btn = self._make_button("↓", AppKit.NSMakeRect(RX + 42, BY, 30, BTN_H), "moveStepDown:")
        add_btn = self._make_button("+ Add Step", AppKit.NSMakeRect(RX + 80, BY, 86, BTN_H), "addStep:")
        edit_btn = self._make_button("Edit", AppKit.NSMakeRect(RX + 172, BY, 50, BTN_H), "editStep:")
        dup_btn = self._make_button("Duplicate", AppKit.NSMakeRect(RX + 228, BY, 76, BTN_H), "duplicateStep:")
        del_step_btn = self._make_button("Delete Step", AppKit.NSMakeRect(RX + 310, BY, 86, BTN_H), "deleteStep:")
        for b in (up_btn, dn_btn, add_btn, edit_btn, dup_btn, del_step_btn):
            b.setAutoresizingMask_(AppKit.NSViewMinXMargin)
            content.addSubview_(b)

    def _make_button(self, title, rect, action):
        btn = AppKit.NSButton.alloc().initWithFrame_(rect)
        btn.setTitle_(title)
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        btn.setButtonType_(AppKit.NSButtonTypeMomentaryPushIn)
        btn.setTarget_(self)
        btn.setAction_(action)
        return btn

    # ── NSTableView data source ───────────────────────────────────────────────

    def numberOfRowsInTableView_(self, table_view):
        if table_view.tag() == 1:
            return len(self._macro_names)
        return len(self._steps)

    def tableView_objectValueForTableColumn_row_(self, table_view, column, row):
        if table_view.tag() == 1:
            if 0 <= row < len(self._macro_names):
                return self._macro_names[row]
            return ""
        # Step table
        col_id = str(column.identifier())
        if col_id == "num":
            return str(row + 1)
        if 0 <= row < len(self._steps):
            return self._steps[row]
        return ""

    # ── NSTableView delegate ──────────────────────────────────────────────────

    def tableViewSelectionDidChange_(self, notification):
        table_view = notification.object()
        if table_view.tag() == 1:
            row = table_view.selectedRow()
            self._selected_macro = self._macro_names[row] if 0 <= row < len(self._macro_names) else None
            self._reload_step_list()

    # ── Reload helpers ────────────────────────────────────────────────────────

    def _reload_macro_list(self, select=None):
        self._macro_names = sorted(self.controller.macros.keys())
        self.macro_table.reloadData()
        target = select or self._selected_macro
        if target and target in self._macro_names:
            idx = self._macro_names.index(target)
            self.macro_table.selectRowIndexes_byExtendingSelection_(
                Foundation.NSIndexSet.indexSetWithIndex_(idx), False
            )
            self._selected_macro = target
        elif self._macro_names:
            self.macro_table.selectRowIndexes_byExtendingSelection_(
                Foundation.NSIndexSet.indexSetWithIndex_(0), False
            )
            self._selected_macro = self._macro_names[0]
        else:
            self._selected_macro = None
        self._reload_step_list()

    def _reload_step_list(self):
        if self._selected_macro and self._selected_macro in self.controller.macros:
            self._steps = list(self.controller._get_macro_steps(self._selected_macro))
        else:
            self._steps = []
        self.step_table.reloadData()
        self.step_table.deselectAll_(None)
        # Update window subtitle to show active macro
        title = f"Glass – Macro Editor"
        if self._selected_macro:
            title += f"  —  {self._selected_macro}"
        self.window.setTitle_(title)

    def _save_current_steps(self):
        """Persist self._steps back to controller.macros and save to disk."""
        if not self._selected_macro:
            return
        macro = self.controller.macros.get(self._selected_macro)
        if macro is None:
            return
        if isinstance(macro, dict):
            macro["steps"] = list(self._steps)
        else:
            self.controller.macros[self._selected_macro] = list(self._steps)
        self.controller._save_macros()
        self.step_table.reloadData()

    def _select_step_row(self, row):
        if 0 <= row < len(self._steps):
            self.step_table.selectRowIndexes_byExtendingSelection_(
                Foundation.NSIndexSet.indexSetWithIndex_(row), False
            )
            self.step_table.scrollRowToVisible_(row)

    # ── Macro actions ─────────────────────────────────────────────────────────

    def newMacro_(self, sender):
        base, i, name = "new-macro", 1, "new-macro"
        while name in self.controller.macros:
            name = f"{base}-{i}"
            i += 1
        self.controller.macros[name] = {"v": 2, "resolution": [], "steps": []}
        self.controller._save_macros()
        self._reload_macro_list(select=name)
        # Immediately prompt for a name
        self.renameMacro_(None)

    def renameMacro_(self, sender):
        if not self._selected_macro:
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Rename macro")
        alert.addButtonWithTitle_("Rename")
        alert.addButtonWithTitle_("Cancel")
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 260, 22))
        field.setStringValue_(self._selected_macro)
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
            new_name = str(field.stringValue()).strip().lower().replace(" ", "-")
            old_name = self._selected_macro
            if new_name and new_name != old_name and new_name not in self.controller.macros:
                self.controller.macros[new_name] = self.controller.macros.pop(old_name)
                self.controller._save_macros()
                self._reload_macro_list(select=new_name)

    def deleteMacro_(self, sender):
        if not self._selected_macro:
            return
        name = self._selected_macro
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f'Delete macro "{name}"?')
        alert.setInformativeText_("This cannot be undone.")
        alert.addButtonWithTitle_("Delete")
        alert.addButtonWithTitle_("Cancel")
        alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
        if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
            del self.controller.macros[name]
            self.controller._save_macros()
            self._selected_macro = None
            self._reload_macro_list()

    # ── Step actions ──────────────────────────────────────────────────────────

    def moveStepUp_(self, sender):
        row = self.step_table.selectedRow()
        if row <= 0 or row >= len(self._steps):
            return
        self._steps[row - 1], self._steps[row] = self._steps[row], self._steps[row - 1]
        self._save_current_steps()
        self._select_step_row(row - 1)

    def moveStepDown_(self, sender):
        row = self.step_table.selectedRow()
        if row < 0 or row >= len(self._steps) - 1:
            return
        self._steps[row], self._steps[row + 1] = self._steps[row + 1], self._steps[row]
        self._save_current_steps()
        self._select_step_row(row + 1)

    def addStep_(self, sender):
        if not self._selected_macro:
            return
        sel = self.step_table.selectedRow()
        insert_at = (sel + 1) if sel >= 0 else len(self._steps)
        self._show_step_editor(row=None, insert_at=insert_at)

    def editStep_(self, sender):
        row = self.step_table.selectedRow()
        if row < 0 or row >= len(self._steps):
            return
        self._show_step_editor(row=row, insert_at=None)

    def deleteStep_(self, sender):
        row = self.step_table.selectedRow()
        if row < 0 or row >= len(self._steps):
            return
        del self._steps[row]
        self._save_current_steps()
        self._select_step_row(min(row, len(self._steps) - 1))

    def duplicateStep_(self, sender):
        row = self.step_table.selectedRow()
        if row < 0 or row >= len(self._steps):
            return
        self._steps.insert(row + 1, self._steps[row])
        self._save_current_steps()
        self._select_step_row(row + 1)

    # ── Step editor sheet ─────────────────────────────────────────────────────

    def _show_step_editor(self, row, insert_at):
        self._editing_row = row
        self._editing_insert_at = insert_at
        existing = self._steps[row] if row is not None else ""

        sheet_w, sheet_h = 500, 240
        sheet_rect = AppKit.NSMakeRect(0, 0, sheet_w, sheet_h)
        self._sheet = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            sheet_rect,
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self._sheet.setTitle_("Edit Step" if row is not None else "Add Step")
        c = self._sheet.contentView()

        def label(text, x, y, w=110):
            f = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 22))
            f.setStringValue_(text)
            f.setEditable_(False)
            f.setBordered_(False)
            f.setDrawsBackground_(False)
            f.setAlignment_(AppKit.NSTextAlignmentRight)
            c.addSubview_(f)
            return f

        def field(x, y, w=340):
            f = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 22))
            c.addSubview_(f)
            return f

        def popup(x, y, w, items):
            p = AppKit.NSPopUpButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 26))
            for item in items:
                p.addItemWithTitle_(item)
            c.addSubview_(p)
            return p

        # Row 1: step type
        label("Step type:", 12, 192)
        self._se_type_popup = popup(128, 190, 220, _STEP_TYPES)
        self._se_type_popup.setAction_("_seTypeChanged:")
        self._se_type_popup.setTarget_(self)

        # Row 2: primary argument (label + text field or dropdown)
        self._se_arg1_label = label("", 12, 152)
        self._se_arg1_field = field(128, 152)
        self._se_key_popup = popup(128, 150, 200, _KEY_NAMES)
        self._se_run_popup = popup(128, 150, 220, sorted(self.controller.macros.keys()))
        images_dir = self.controller.images_path
        img_names = sorted(
            f[:-4] for f in os.listdir(images_dir) if f.endswith(".png")
        ) if os.path.isdir(images_dir) else []
        self._se_img_popup = popup(128, 150, 220, img_names or ["(no images)"])

        # Row 3: X coordinate
        self._se_x_label = label("", 12, 112)
        self._se_x_field = field(128, 112, 120)

        # Row 4: Y coordinate  (shares row 3 line, offset to the right)
        self._se_y_label = label("", 270, 112, 30)
        self._se_y_field = field(306, 112, 120)

        # OK / Cancel
        ok = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(sheet_w - 108, 12, 90, 32))
        ok.setTitle_("OK")
        ok.setBezelStyle_(AppKit.NSBezelStyleRounded)
        ok.setKeyEquivalent_("\r")
        ok.setTarget_(self)
        ok.setAction_("_seOK:")
        c.addSubview_(ok)

        cancel = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(sheet_w - 206, 12, 90, 32))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(AppKit.NSBezelStyleRounded)
        cancel.setKeyEquivalent_("\x1b")
        cancel.setTarget_(self)
        cancel.setAction_("_seCancel:")
        c.addSubview_(cancel)

        self._se_populate(existing)
        self.window.beginSheet_completionHandler_(self._sheet, None)

    def _se_populate(self, step_str):
        """Parse step_str and set sheet fields accordingly."""
        step_str = step_str.strip()
        parts = step_str.split(" ", 1) if step_str else []
        step_type = parts[0].lower() if parts else "wait"
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Select type in popup
        for i in range(self._se_type_popup.numberOfItems()):
            if self._se_type_popup.itemTitleAtIndex_(i) == step_type:
                self._se_type_popup.selectItemAtIndex_(i)
                break

        self._se_apply_type(step_type, arg)

    def _se_apply_type(self, step_type, arg=""):
        """Show/hide sheet fields for the given step type."""
        # Hide all dynamic controls first
        for w in (self._se_arg1_field, self._se_key_popup, self._se_run_popup,
                  self._se_img_popup, self._se_x_field, self._se_y_field):
            w.setHidden_(True)
        for lbl in (self._se_arg1_label, self._se_x_label, self._se_y_label):
            lbl.setStringValue_("")

        if step_type == "wait":
            self._se_arg1_label.setStringValue_("Seconds:")
            self._se_arg1_field.setStringValue_(arg if arg else "1.0")
            self._se_arg1_field.setHidden_(False)

        elif step_type == "key-press":
            self._se_arg1_label.setStringValue_("Key:")
            self._se_key_popup.setHidden_(False)
            self._se_select_popup(self._se_key_popup, arg)

        elif step_type == "find":
            self._se_arg1_label.setStringValue_("Text:")
            self._se_arg1_field.setStringValue_(arg)
            self._se_arg1_field.setHidden_(False)

        elif step_type in ("smart-click", "smart-rclick", "smart-dclick"):
            query, x_pct, y_pct = _parse_smart_click_arg(arg)
            self._se_arg1_label.setStringValue_("Query text:")
            self._se_arg1_field.setStringValue_(query)
            self._se_arg1_field.setHidden_(False)
            self._se_x_label.setStringValue_("X (0–1):")
            self._se_x_field.setStringValue_(f"{x_pct:.4f}" if x_pct is not None else "0.5000")
            self._se_x_field.setHidden_(False)
            self._se_y_label.setStringValue_("Y:")
            self._se_y_field.setStringValue_(f"{y_pct:.4f}" if y_pct is not None else "0.5000")
            self._se_y_field.setHidden_(False)

        elif step_type in ("click-at", "rclick-at", "dclick-at"):
            parts = arg.split()
            self._se_x_label.setStringValue_("X (0–1):")
            self._se_x_field.setStringValue_(parts[0] if parts else "0.5000")
            self._se_x_field.setHidden_(False)
            self._se_y_label.setStringValue_("Y:")
            self._se_y_field.setStringValue_(parts[1] if len(parts) > 1 else "0.5000")
            self._se_y_field.setHidden_(False)

        elif step_type == "click-relative":
            self._se_arg1_label.setStringValue_("Index dX dY:")
            self._se_arg1_field.setStringValue_(arg if arg else "1 0.0000 0.0000")
            self._se_arg1_field.setHidden_(False)

        elif step_type == "run":
            self._se_arg1_label.setStringValue_("Macro:")
            self._se_run_popup.setHidden_(False)
            self._se_select_popup(self._se_run_popup, arg)

        elif step_type == "find-image":
            self._se_arg1_label.setStringValue_("Image:")
            self._se_img_popup.setHidden_(False)
            self._se_select_popup(self._se_img_popup, arg)

        elif step_type == "type":
            self._se_arg1_label.setStringValue_("Text ({date} = today):")
            self._se_arg1_field.setStringValue_(arg.strip('"'))
            self._se_arg1_field.setHidden_(False)

        # capture / clear: no parameters — everything stays hidden

    def _se_select_popup(self, popup, value):
        for i in range(popup.numberOfItems()):
            if popup.itemTitleAtIndex_(i) == value:
                popup.selectItemAtIndex_(i)
                return

    def _seTypeChanged_(self, sender):
        step_type = str(self._se_type_popup.titleOfSelectedItem())
        self._se_apply_type(step_type)

    def _se_build_step(self):
        """Assemble the step string from current sheet field values."""
        t = str(self._se_type_popup.titleOfSelectedItem())

        if t == "wait":
            raw = str(self._se_arg1_field.stringValue()).strip()
            try:
                val = f"{float(raw):.1f}"
            except ValueError:
                val = "1.0"
            return f"wait {val}"

        if t == "key-press":
            return f"key-press {self._se_key_popup.titleOfSelectedItem()}"

        if t in ("find", "type", "type-keys"):
            text = str(self._se_arg1_field.stringValue()).strip()
            return f"{t} {text}" if text else t

        if t in ("smart-click", "smart-rclick", "smart-dclick"):
            query = str(self._se_arg1_field.stringValue()).strip()
            try:
                x = float(str(self._se_x_field.stringValue()).strip())
            except ValueError:
                x = 0.5
            try:
                y = float(str(self._se_y_field.stringValue()).strip())
            except ValueError:
                y = 0.5
            escaped = query.replace('"', '\\"')
            return f'{t} "{escaped}" {x:.4f} {y:.4f}'

        if t in ("click-at", "rclick-at", "dclick-at"):
            try:
                x = float(str(self._se_x_field.stringValue()).strip())
            except ValueError:
                x = 0.5
            try:
                y = float(str(self._se_y_field.stringValue()).strip())
            except ValueError:
                y = 0.5
            return f"{t} {x:.4f} {y:.4f}"

        if t == "click-relative":
            raw = str(self._se_arg1_field.stringValue()).strip()
            return f"{t} {raw}" if raw else t

        if t == "run":
            name = str(self._se_run_popup.titleOfSelectedItem())
            return f"run {name}"

        if t == "find-image":
            name = str(self._se_img_popup.titleOfSelectedItem())
            return f"find-image {name}"

        return t  # capture / clear

    def _seOK_(self, sender):
        step_str = self._se_build_step()
        self.window.endSheet_(self._sheet)
        self._sheet.orderOut_(None)
        self._sheet = None
        if self._editing_row is not None:
            self._steps[self._editing_row] = step_str
            target_row = self._editing_row
        else:
            at = self._editing_insert_at if self._editing_insert_at is not None else len(self._steps)
            self._steps.insert(at, step_str)
            target_row = at
        self._save_current_steps()
        self._select_step_row(target_row)

    def _seCancel_(self, sender):
        self.window.endSheet_(self._sheet)
        self._sheet.orderOut_(None)
        self._sheet = None

    # ── Window delegate ───────────────────────────────────────────────────────

    def windowWillClose_(self, notification):
        pass  # window is reused; no teardown needed

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self, macro_name=None):
        self._reload_macro_list(select=macro_name)
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)


class AppController(AppKit.NSObject):
    def init(self):
        self = objc_super(AppController, self).init()
        if self is None:
            return None

        self.ocr_engine = ScreenOCR()

        # Active screen selection (multi-display)
        # Default: follow wherever the command bar window is.
        self._follow_command_bar = True
        self._active_screen_index = 0
        self._active_display_id = Quartz.CGMainDisplayID()
        self._display_bounds_px = Quartz.CGDisplayBounds(self._active_display_id)
        self.capture_origin_pt = (0.0, 0.0)

        # Initialize geometry from main screen; we'll sync to the command bar on show/drag.
        screen = AppKit.NSScreen.mainScreen()
        self.screen_frame = screen.frame()
        self.screen_height = self.screen_frame.size.height
        self.screen_center = (
            self.screen_frame.size.width / 2.0,
            self.screen_frame.size.height / 2.0,
        )

        self.command_bar = CommandBarWindow.alloc().initWithController_screenFrame_(
            self, self.screen_frame
        )
        self.overlay = OverlayWindow.alloc().initWithScreenFrame_(self.screen_frame)

        self.ocr_items = []
        self.matches = []
        self.last_click_point = None
        self.capture_width_px = None
        self.capture_height_px = None
        self.capture_scale = None
        self._pending_find_query = None
        self._ocr_in_progress = False
        self._last_control_tap = 0.0
        self._event_monitor = None
        self._event_tap = None
        self._event_tap_source = None
        self._event_callback = None
        self._key_monitor = None
        self.macros_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macros.json")
        self.images_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
        os.makedirs(self.images_path, exist_ok=True)
        self.macros = {}
        self._recording_name = None
        self._recording_steps = []
        self._recording_mouse_monitor = None
        self._recording_key_monitor = None
        self._macro_running = False
        self._macro_queue = []
        self._macro_name = None
        self._macro_wait_reason = None
        self._macro_stack = []
        self._macro_root = None
        self._macro_delay = 0.75
        self._region_select = None
        self._pending_image_name = None
        self._command_history = []
        self._history_index = -1
        self._macro_editor = None
        self._load_macros()
        self._macros_mtime_ns = self._macros_file_mtime_ns()
        self._macro_watch_timer = None
        self._start_macro_watch_timer()
        self._setup_hotkey()
        self._status_flash_token = 0
        return self

    def _flash_status(self, message, duration=1.25):
        """Show a temporary status message, then clear it."""
        if not hasattr(self, "command_bar"):
            return
        self._status_flash_token += 1
        token = self._status_flash_token
        self.command_bar.set_status(message)

        def clear_later():
            def do_clear():
                # Only clear if nothing newer replaced it.
                if token != getattr(self, "_status_flash_token", 0):
                    return
                # Don't clobber user typing.
                if not getattr(self.command_bar, "visible", False):
                    return
                if self.command_bar.input_text():
                    return
                self.command_bar.set_status("")

            run_on_main(do_clear)

        threading.Timer(duration, clear_later).start()

    def _screens(self):
        # NSScreen frames are in points in a global coordinate space.
        return list(AppKit.NSScreen.screens() or [])

    def _screen_display_id(self, screen):
        try:
            desc = screen.deviceDescription() or {}
            return int(desc.get("NSScreenNumber"))
        except Exception:
            return None

    def _screen_index_for_point(self, pt):
        # `pt` is in global screen coords (points).
        screens = self._screens()
        for idx, s in enumerate(screens):
            f = s.frame()
            if (
                pt.x >= f.origin.x
                and pt.x < (f.origin.x + f.size.width)
                and pt.y >= f.origin.y
                and pt.y < (f.origin.y + f.size.height)
            ):
                return idx
        return None

    def _screen_index_for_mouse(self):
        return self._screen_index_for_point(AppKit.NSEvent.mouseLocation())

    def _sync_active_screen_to_command_bar(self, announce=False):
        """Make the active screen track the command bar's current screen."""
        if not getattr(self, "_follow_command_bar", True):
            return
        if not hasattr(self, "command_bar"):
            return
        window = getattr(self.command_bar, "window", None)
        if window is None:
            return

        # Best signal: AppKit knows which NSScreen the window is on.
        screen = window.screen()
        screens = self._screens()
        if screen is None:
            # Fallback: use window center point.
            frame = window.frame()
            center = Foundation.NSMakePoint(
                frame.origin.x + frame.size.width / 2.0,
                frame.origin.y + frame.size.height / 2.0,
            )
            idx = self._screen_index_for_point(center)
        else:
            try:
                idx = screens.index(screen)
            except ValueError:
                idx = None

        if idx is None:
            # last resort
            idx = self._screen_index_for_mouse()

        if idx is None:
            return

        if idx != getattr(self, "_active_screen_index", 0):
            self._set_active_screen(idx, announce=announce, rebuild_command_bar=False)
        elif announce and hasattr(self, "command_bar"):
            # Still give feedback when requested, even if unchanged.
            self._flash_status(f"Active screen: {idx + 1}/{len(self._screens())}")

    def _set_active_screen(self, index, announce=True, rebuild_command_bar=True):
        screens = self._screens()
        if not screens:
            return
        if index < 0 or index >= len(screens):
            if hasattr(self, "command_bar"):
                self.command_bar.set_status("Invalid screen")
            return

        screen = screens[index]
        display_id = self._screen_display_id(screen)
        if display_id is None:
            if hasattr(self, "command_bar"):
                self.command_bar.set_status("Could not resolve display id")
            return

        # Update active display metadata
        self._active_screen_index = index
        self._active_display_id = display_id
        self._display_bounds_px = Quartz.CGDisplayBounds(display_id)

        # Screen frame is in points.
        self.screen_frame = screen.frame()
        self.screen_height = self.screen_frame.size.height
        self.screen_center = (
            self.screen_frame.size.width / 2.0,
            self.screen_frame.size.height / 2.0,
        )

        # Reset capture state for new screen
        self.ocr_items = []
        self.matches = []
        if hasattr(self, "overlay"):
            self.overlay.clear()

        # Rebuild/update windows so they are positioned/sized for the active screen.
        # During initial init, windows may not exist yet.
        if hasattr(self, "command_bar") and hasattr(self, "overlay"):
            was_visible = getattr(self.command_bar, "visible", False)

            # Always rebuild overlay for the new screen frame.
            try:
                self.overlay.window.orderOut_(None)
            except Exception:
                pass
            self.overlay = OverlayWindow.alloc().initWithScreenFrame_(self.screen_frame)

            # Optionally rebuild command bar (this recenters it). When we are following
            # the user's dragged command bar, we do NOT want to reset its position.
            if rebuild_command_bar:
                if was_visible:
                    self.command_bar.hide()
                self.command_bar = CommandBarWindow.alloc().initWithController_screenFrame_(
                    self, self.screen_frame
                )

            if announce and hasattr(self, "command_bar"):
                self._flash_status(f"Active screen: {index + 1}/{len(screens)}")
            if was_visible:
                self.command_bar.show()
                self.command_bar.clear_input()

    def _setup_hotkey(self):
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
        self._event_callback = self._event_tap_callback
        self._event_tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._event_callback,
            None,
        )
        if self._event_tap is not None:
            self._event_tap_source = CoreFoundation.CFMachPortCreateRunLoopSource(
                None, self._event_tap, 0
            )
            CoreFoundation.CFRunLoopAddSource(
                CoreFoundation.CFRunLoopGetCurrent(),
                self._event_tap_source,
                CoreFoundation.kCFRunLoopCommonModes,
            )
            Quartz.CGEventTapEnable(self._event_tap, True)
        else:
            print("Hotkey disabled: enable Input Monitoring permission for this app.")
            self._event_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskFlagsChanged, self._handle_flags_changed
            )

    def _event_tap_callback(self, proxy, type_, event, refcon):
        if type_ in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(self._event_tap, True)
            return event
        if type_ != Quartz.kCGEventFlagsChanged:
            return event
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        if keycode not in (59, 62):
            return event
        flags = Quartz.CGEventGetFlags(event)
        control_down = (flags & Quartz.kCGEventFlagMaskControl) != 0
        other_mods = flags & (
            Quartz.kCGEventFlagMaskShift
            | Quartz.kCGEventFlagMaskAlternate
            | Quartz.kCGEventFlagMaskCommand
            | Quartz.kCGEventFlagMaskAlphaShift
            | Quartz.kCGEventFlagMaskSecondaryFn
        )
        if not control_down or other_mods:
            return event
        self._register_control_tap()
        return event

    def _handle_flags_changed(self, event):
        if event.keyCode() not in (59, 62):
            return
        flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
        if flags != AppKit.NSEventModifierFlagControl:
            return
        self._register_control_tap()

    def _register_control_tap(self):
        now = time.time()
        if now - self._last_control_tap <= 0.4:
            self._last_control_tap = 0.0
            run_on_main(self.toggle_command_bar)
        else:
            self._last_control_tap = now

    def toggle_command_bar(self):
        if self.command_bar.visible:
            self._remove_key_monitor()
            self.command_bar.hide()
        else:
            self.command_bar.show()
            # Follow the command bar's current screen as the active screen.
            self._sync_active_screen_to_command_bar(announce=False)
            self.command_bar.clear_input()
            self.command_bar.set_status("")
            self.command_bar.hide_help()
            self._install_key_monitor()

    def clear_and_close(self):
        if self._macro_running:
            self._abort_macro("Macro canceled")
        self.overlay.clear()
        self.matches = []
        self._pending_find_query = None
        self.command_bar.hide_help()
        self.command_bar.clear_input()
        self.command_bar.set_status("")
        self._remove_key_monitor()
        self.command_bar.hide()

    def _load_macros(self):
        path = self.macros_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict) and isinstance(data.get("macros"), dict):
            self.macros = data["macros"]
        elif isinstance(data, dict):
            self.macros = data
        normalized = {}
        for key, value in self.macros.items():
            name = self._normalize_macro_name(key)
            if name not in normalized:
                normalized[name] = value
        self.macros = normalized

    def _macros_file_mtime_ns(self):
        try:
            return os.stat(self.macros_path).st_mtime_ns
        except OSError:
            return None

    def _reload_macros_if_changed(self):
        current_mtime = self._macros_file_mtime_ns()
        if current_mtime is None:
            return False
        if current_mtime == getattr(self, "_macros_mtime_ns", None):
            return False

        editor = getattr(self, "_macro_editor", None)
        selected_macro = getattr(editor, "_selected_macro", None) if editor is not None else None
        self._load_macros()
        self._macros_mtime_ns = current_mtime
        if editor is not None:
            editor._reload_macro_list(select=selected_macro)
        print("DEBUG macros: reloaded macros.json from disk")
        return True

    def _start_macro_watch_timer(self):
        if getattr(self, "_macro_watch_timer", None) is not None:
            return
        self._macro_watch_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "macroWatchTimerFired:", None, True
        )

    def macroWatchTimerFired_(self, timer):
        self._reload_macros_if_changed()

    def _save_macros(self):
        path = self.macros_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"macros": self.macros}, handle, indent=2)
        self._macros_mtime_ns = self._macros_file_mtime_ns()

    def _get_macro_steps(self, name):
        """Get steps from a macro, handling both v1 (array) and v2 (object) formats."""
        macro = self.macros.get(name)
        if macro is None:
            return []
        if isinstance(macro, list):
            # v1 format: macro is just an array of steps
            return macro
        if isinstance(macro, dict):
            # v2 format: macro is {"v": 2, "resolution": [...], "steps": [...]}
            return macro.get("steps", [])
        return []

    def _get_macro_resolution(self, name):
        """Get resolution from a v2 macro, or None for v1 macros."""
        macro = self.macros.get(name)
        if isinstance(macro, dict):
            res = macro.get("resolution")
            if isinstance(res, list) and len(res) == 2:
                return tuple(res)
        return None

    def _get_macro_version(self, name):
        """Get macro version (1 for array format, 2+ for object format)."""
        macro = self.macros.get(name)
        if isinstance(macro, list):
            return 1
        if isinstance(macro, dict):
            return macro.get("v", 2)
        return 1

    def _record_step(self, step):
        if not step:
            return
        if self._recording_name is None:
            return
        if self._macro_running:
            return
        self._recording_steps.append(step)

    def _start_recording(self, name):
        name = self._normalize_macro_name(name)
        print(f"DEBUG _start_recording: name={name!r}")
        if not name:
            self.command_bar.set_status("Missing macro name")
            return
        if self._macro_running:
            self.command_bar.set_status("Macro running")
            return
        if self._recording_name is not None:
            self.command_bar.set_status(f"Already recording {self._recording_name}")
            return
        self._recording_name = name
        self._recording_steps = []
        print(f"DEBUG _start_recording: recording started, _recording_name={self._recording_name}")
        # Capture resolution for v2 format
        self._recording_resolution = (
            int(self.screen_frame.size.width),
            int(self.screen_frame.size.height),
        )
        # Track time for wait insertion
        self._recording_last_action_time = time.time()
        # Start global mouse click and key monitoring
        self._start_recording_mouse_monitor()
        self._start_recording_key_monitor()
        res_str = f"{self._recording_resolution[0]}x{self._recording_resolution[1]}"
        self.command_bar.set_status(f"Recording {name} ({res_str}) - click anywhere")

    def _stop_recording(self):
        print(f"DEBUG _stop_recording: _recording_name={self._recording_name}, steps={self._recording_steps}")
        # Stop mouse and key monitoring first
        self._stop_recording_mouse_monitor()
        self._stop_recording_key_monitor()
        if self._recording_name is None:
            self.command_bar.set_status("Not recording")
            return
        name = self._recording_name
        # Save in v2 format with resolution metadata
        resolution = getattr(self, "_recording_resolution", None)
        if resolution:
            self.macros[name] = {
                "v": 2,
                "resolution": list(resolution),
                "steps": list(self._recording_steps),
            }
        else:
            # Fallback to v1 if no resolution captured
            self.macros[name] = list(self._recording_steps)
        self._save_macros()
        count = len(self._recording_steps)
        self._recording_name = None
        self._recording_steps = []
        self._recording_resolution = None
        self._recording_last_action_time = None
        self.command_bar.set_status(f"Saved macro {name} ({count} steps)")

    def _start_recording_mouse_monitor(self):
        """Start global mouse click monitoring for recording."""
        if self._recording_mouse_monitor is not None:
            return
        mask = AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskRightMouseDown
        self._recording_mouse_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, self._handle_recording_mouse_click
        )
        print("DEBUG: Started recording mouse monitor")

    def _stop_recording_mouse_monitor(self):
        """Stop global mouse click monitoring."""
        if self._recording_mouse_monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._recording_mouse_monitor)
            self._recording_mouse_monitor = None
            print("DEBUG: Stopped recording mouse monitor")

    # Navigation key codes to capture during recording
    _NAV_KEY_CODES = {
        125: "down",
        126: "up",
        123: "left",
        124: "right",
        121: "page-down",
        116: "page-up",
    }

    def _start_recording_key_monitor(self):
        """Start global key monitoring for navigation keys during recording."""
        if self._recording_key_monitor is not None:
            return
        self._recording_key_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, self._handle_recording_key_press
        )
        print("DEBUG: Started recording key monitor")

    def _stop_recording_key_monitor(self):
        """Stop global key monitoring."""
        if self._recording_key_monitor is not None:
            AppKit.NSEvent.removeMonitor_(self._recording_key_monitor)
            self._recording_key_monitor = None
            print("DEBUG: Stopped recording key monitor")

    def _handle_recording_key_press(self, event):
        """Handle a global key press during recording (navigation keys only)."""
        if self._recording_name is None:
            return
        key_code = event.keyCode()
        key_name = self._NAV_KEY_CODES.get(key_code)
        if key_name is None:
            return
        print(f"DEBUG _handle_recording_key_press: key={key_name} (code={key_code})")
        # Insert a wait step if enough time has passed since the last recorded action
        if self._recording_last_action_time is not None:
            elapsed = time.time() - self._recording_last_action_time
            if elapsed > 0.5:
                wait_time = min(10.0, round(elapsed * 2) / 2)
                self._recording_steps.append(f"wait {wait_time:.1f}")
        self._recording_steps.append(f"key-press {key_name}")
        self._recording_last_action_time = time.time()
        step_count = len(self._recording_steps)
        run_on_main(lambda: self.command_bar.set_status(f"Recording {self._recording_name} ({step_count} steps)"))

    def _handle_recording_mouse_click(self, event):
        """Handle a global mouse click during recording."""
        if self._recording_name is None:
            return

        # Debounce - ignore clicks within 100ms of last recorded click
        now = time.time()
        last_click = getattr(self, "_recording_last_click_time", 0)
        if now - last_click < 0.1:
            print("DEBUG: Ignoring click (debounce)")
            return
        self._recording_last_click_time = now

        # Get click location - for global monitor, locationInWindow() gives screen coords
        # in Cocoa coordinate system (origin at bottom-left)
        mouse_loc = AppKit.NSEvent.mouseLocation()

        # Ignore clicks on the command bar window
        if hasattr(self, "command_bar") and self.command_bar.window:
            cmd_frame = self.command_bar.window.frame()
            if AppKit.NSPointInRect(mouse_loc, cmd_frame):
                print("DEBUG: Ignoring click on command bar")
                return

        # Find which screen the click is on
        click_screen = None
        for screen in AppKit.NSScreen.screens():
            frame = screen.frame()
            if AppKit.NSPointInRect(mouse_loc, frame):
                click_screen = screen
                break

        if click_screen is None:
            click_screen = AppKit.NSScreen.mainScreen()

        # Convert to screen-relative coordinates with origin at top-left
        screen_frame = click_screen.frame()
        click_x = mouse_loc.x - screen_frame.origin.x
        click_y = screen_frame.size.height - (mouse_loc.y - screen_frame.origin.y)

        button = "left" if event.type() == AppKit.NSEventTypeLeftMouseDown else "right"
        click_count = event.clickCount()
        print(f"DEBUG _handle_recording_mouse_click: click at ({click_x}, {click_y}), button={button}, clickCount={click_count}")

        # Run OCR and record the click in the background
        def do_record():
            self._record_click_with_ocr(click_x, click_y, button, click_count)

        # Use a thread to avoid blocking
        threading.Thread(target=do_record, daemon=True).start()

    def _record_click_with_ocr(self, click_x, click_y, button="left", click_count=1):
        """Capture screen, run OCR, and record a click at the given position."""
        try:
            # Capture screen
            display_id = self._active_display_id
            screen_size = (self.screen_frame.size.width, self.screen_frame.size.height)
            cg_image, width_px, height_px, scale, bounds_px = self.ocr_engine.capture_display(
                display_id, screen_size
            )

            # Run OCR
            ocr_items = self.ocr_engine.recognize_text(cg_image, width_px, height_px, scale)

            # Find text under the click (with some tolerance)
            tolerance = 10  # pixels tolerance for "under" detection
            text_under_click = None

            for item in ocr_items:
                bbox = item["bbox"]  # (x, y, w, h) in points
                bx, by, bw, bh = bbox
                # Check if click is within or near this text's bounding box
                if (bx - tolerance <= click_x <= bx + bw + tolerance and
                    by - tolerance <= click_y <= by + bh + tolerance):
                    text_under_click = item
                    break

            # Insert wait step if needed
            last_time = getattr(self, "_recording_last_action_time", None)
            if last_time is not None:
                elapsed = time.time() - last_time
                if elapsed > 0.5:
                    wait_time = min(10.0, round(elapsed * 2) / 2)
                    self._recording_steps.append(f"wait {wait_time:.1f}")

            screen_w = self.screen_frame.size.width
            screen_h = self.screen_frame.size.height
            x_pct = click_x / screen_w if screen_w > 0 else 0
            y_pct = click_y / screen_h if screen_h > 0 else 0

            # Determine click type based on button and click count
            is_double = click_count >= 2
            if text_under_click:
                # Record smart-click with text anchor
                query = text_under_click["text"]
                escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')
                if is_double:
                    cmd = "smart-dclick"
                elif button == "left":
                    cmd = "smart-click"
                else:
                    cmd = "smart-rclick"
                step = f'{cmd} "{escaped_query}" {x_pct:.4f} {y_pct:.4f}'
                click_type = "double-click" if is_double else "click"
                print(f"DEBUG: Recorded smart-{click_type} with text: {query}")
            else:
                # No text under click - record absolute coordinates
                if is_double:
                    cmd = "dclick-at"
                elif button == "left":
                    cmd = "click-at"
                else:
                    cmd = "rclick-at"
                step = f'{cmd} {x_pct:.4f} {y_pct:.4f}'
                click_type = "double-click" if is_double else "click"
                print(f"DEBUG: Recorded absolute {click_type} at ({x_pct:.4f}, {y_pct:.4f})")

            self._recording_steps.append(step)
            self._recording_last_action_time = time.time()

            # Update status on main thread
            click_desc = "double-click" if is_double else f"{button} click"
            def update_status():
                if text_under_click:
                    self.command_bar.set_status(f"Recorded: {click_desc} on \"{text_under_click['text'][:20]}\"")
                else:
                    self.command_bar.set_status(f"Recorded: {click_desc} at ({x_pct:.2%}, {y_pct:.2%})")
            run_on_main(update_status)

        except Exception as e:
            print(f"DEBUG: Error recording click: {e}")
            def show_error():
                self.command_bar.set_status(f"Error recording click: {e}")
            run_on_main(show_error)

    def _list_macros(self):
        if not self.macros:
            self.command_bar.set_status("No macros saved")
            self.command_bar.show_help("No macros saved")
            return
        names = sorted(self.macros.keys())
        self.command_bar.set_status(f"Macros ({len(names)})")
        self.command_bar.show_help("\n".join(names))

    def _open_macro_editor(self, macro_name=None):
        """Open (or re-show) the graphical macro editor."""
        if self._macro_editor is None:
            self._macro_editor = MacroEditorWindow.alloc().initWithController_(self)
        name = self._normalize_macro_name(macro_name) if macro_name else None
        self._macro_editor.show(macro_name=name)
        self.command_bar.hide()

    def _show_macro(self, name):
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing macro name")
            return
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return
        steps = self._get_macro_steps(name)
        resolution = self._get_macro_resolution(name)
        version = self._get_macro_version(name)
        status = f"Macro {name}"
        if resolution:
            status += f" (v{version}, {resolution[0]}x{resolution[1]})"
        self.command_bar.set_status(status)
        if steps:
            self.command_bar.show_help("\n".join(steps))
        else:
            self.command_bar.show_help("(empty)")

    def _delete_macro(self, name):
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing macro name")
            return
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return
        del self.macros[name]
        self._save_macros()
        self.command_bar.set_status(f"Deleted macro {name}")

    def _capture_image(self, name):
        """Start region selection to capture an image template."""
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing image name")
            return
        if self._recording_name is None:
            self.command_bar.set_status("Only available while recording")
            return
        self._pending_image_name = name
        self.command_bar.set_status(f"Drag to select region for '{name}'")
        self.command_bar.hide()
        self._region_select = RegionSelectWindow(self.screen_frame, self._on_region_selected)
        self._region_select.show()

    def _on_region_selected(self, bounds):
        """Called when user finishes dragging a selection region."""
        self._region_select = None
        if bounds is None:
            self.command_bar.show()
            self.command_bar.set_status("Capture cancelled")
            return
        x, y, w, h = bounds
        name = self._pending_image_name
        self._pending_image_name = None
        # Capture the screen and crop to selection
        display_id = self._active_display_id
        bounds_px = Quartz.CGDisplayBounds(display_id)
        # Convert selection (points, relative to active screen) to global pixels.
        scale = bounds_px.size.width / float(self.screen_frame.size.width or 1.0)
        region_px = Quartz.CGRectMake(
            bounds_px.origin.x + (x * scale),
            bounds_px.origin.y + (y * scale),
            w * scale,
            h * scale,
        )
        image = Quartz.CGWindowListCreateImage(
            region_px,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image is None:
            self.command_bar.show()
            self.command_bar.set_status("Failed to capture region")
            return
        # Save as PNG
        image_path = os.path.join(self.images_path, f"{name}.png")
        url = Foundation.NSURL.fileURLWithPath_(image_path)
        dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
        if dest:
            Quartz.CGImageDestinationAddImage(dest, image, None)
            Quartz.CGImageDestinationFinalize(dest)
        if not os.path.exists(image_path):
            self.command_bar.show()
            self.command_bar.set_status(f"Failed to save image: {name}")
            return
        # Record the find-image step
        self._record_step(f"find-image {name}")
        self.command_bar.show()
        self.command_bar.set_status(f"Saved image '{name}', searching...")
        # Now run find-image to show matches
        self._find_image(name)

    def _find_image(self, arg_str):
        """Find a saved image template on screen using multi-scale template matching.

        Format: find-image <name> [x_pct y_pct] [--attempts N]
        Optional coordinates select the closest match to that position.
        """
        parts = arg_str.strip().split()
        name = self._normalize_macro_name(parts[0]) if parts else ""
        hint_x, hint_y = None, None
        click_attempts = 2
        idx = 1
        while idx < len(parts):
            token = parts[idx]
            if token == "--attempts" and idx + 1 < len(parts):
                try:
                    click_attempts = max(1, min(5, int(parts[idx + 1])))
                except ValueError:
                    pass
                idx += 2
                continue
            if token.startswith("attempts="):
                try:
                    click_attempts = max(1, min(5, int(token.split("=", 1)[1])))
                except ValueError:
                    pass
                idx += 1
                continue
            if hint_x is None and hint_y is None and idx + 1 < len(parts):
                try:
                    hint_x = float(parts[idx])
                    hint_y = float(parts[idx + 1])
                    idx += 2
                    continue
                except ValueError:
                    pass
            idx += 1
        if not name:
            self.command_bar.set_status("Missing image name")
            return
        image_path = os.path.join(self.images_path, f"{name}.png")
        if not os.path.exists(image_path):
            self.command_bar.set_status(f"Image not found: {name}")
            if self._macro_wait_reason == "find-image":
                self._abort_macro(f"Image not found: {name}")
            return
        self._macro_wait_reason = "find-image"
        self.command_bar.set_status(f"Finding '{name}'...")
        # Load template in grayscale for robustness
        template_color = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if template_color is None:
            self.command_bar.set_status(f"Failed to load image: {name}")
            if self._macro_wait_reason == "find-image":
                self._abort_macro(f"Failed to load image: {name}")
            return
        template_gray = cv2.cvtColor(template_color, cv2.COLOR_BGR2GRAY)
        template_h, template_w = template_gray.shape[:2]
        print(f"DEBUG _find_image: template '{name}' size={template_w}x{template_h}")
        # Capture screen
        display_id = self._active_display_id
        screen_bounds = Quartz.CGDisplayBounds(display_id)
        screen_image = Quartz.CGWindowListCreateImage(
            screen_bounds,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if screen_image is None:
            self.command_bar.set_status("Screen capture failed")
            if self._macro_wait_reason == "find-image":
                self._abort_macro("Screen capture failed")
            return
        # Convert CGImage to numpy array
        px_width = Quartz.CGImageGetWidth(screen_image)
        px_height = Quartz.CGImageGetHeight(screen_image)
        bytes_per_row = Quartz.CGImageGetBytesPerRow(screen_image)
        data_provider = Quartz.CGImageGetDataProvider(screen_image)
        data = Quartz.CGDataProviderCopyData(data_provider)
        arr = np.frombuffer(data, dtype=np.uint8)
        arr = arr.reshape((px_height, bytes_per_row // 4, 4))
        screen_bgr = arr[:, :px_width, :3].copy()
        screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
        print(f"DEBUG _find_image: screen size={px_width}x{px_height}")
        # Multi-scale matching: try a range of scales to handle size differences
        pixel_scale = px_width / self.screen_frame.size.width  # retina scale factor
        scales = [0.5, 0.75, 1.0, 1.5, 2.0]
        threshold = 0.6
        best_val = -1.0
        best_matches = []
        for s in scales:
            tw = max(1, int(template_w * s))
            th = max(1, int(template_h * s))
            if tw > px_width or th > px_height:
                continue
            scaled = cv2.resize(template_gray, (tw, th), interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(screen_gray, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > best_val:
                best_val = max_val
            if max_val < threshold:
                continue
            locations = np.where(result >= threshold)
            for pt in zip(*locations[::-1]):
                best_matches.append((pt, tw, th, s, float(result[pt[1], pt[0]])))
        print(f"DEBUG _find_image: best score={best_val:.3f}, raw hits={len(best_matches)}")
        # Convert pixel coords to point coords and deduplicate
        pt_scale = px_width / self.screen_frame.size.width
        matches = []
        for pt, tw, th, s, score in best_matches:
            x_pt = pt[0] / pt_scale
            y_pt = pt[1] / pt_scale
            w_pt = tw / pt_scale
            h_pt = th / pt_scale
            matches.append({"text": name, "bbox": (x_pt, y_pt, w_pt, h_pt), "query": name, "type": "image", "score": score})
        # Deduplicate overlapping matches
        filtered = []
        for m in sorted(matches, key=lambda x: -x["score"]):
            x, y, w, h = m["bbox"]
            duplicate = False
            for f in filtered:
                fx, fy, fw, fh = f["bbox"]
                if abs(x - fx) < w * 0.5 and abs(y - fy) < h * 0.5:
                    duplicate = True
                    break
            if not duplicate:
                filtered.append(m)
        # If hint coordinates given, sort by proximity to hint; otherwise use anchor
        if hint_x is not None and hint_y is not None:
            target_x = hint_x * self.screen_frame.size.width
            target_y = hint_y * self.screen_frame.size.height
            def dist_to_hint(m):
                bx, by, bw, bh = m["bbox"]
                return (bx + bw/2 - target_x)**2 + (by + bh/2 - target_y)**2
            filtered = sorted(filtered[:9], key=dist_to_hint)
        else:
            filtered = self._order_matches_by_anchor(filtered[:9])
        print(f"DEBUG _find_image: final matches={len(filtered)}")
        self.matches = filtered
        if self._macro_running and self._macro_wait_reason == "find-image":
            if not filtered:
                self._abort_macro(f"Image not found on screen: {name} (best={best_val:.2f})")
                return
            match = filtered[0]
            x, y, w, h = match["bbox"]
            cx, cy = x + w / 2.0, y + h / 2.0
            print(
                f"DEBUG _find_image: auto-clicking match at ({cx:.1f}, {cy:.1f}), "
                f"attempts={click_attempts}"
            )
            self._dispatch_macro_click(cx, cy, reason="find-image-click", attempts=click_attempts)
        else:
            self.overlay.show_matches(filtered, self.screen_height)
            self.command_bar.set_status(f"Found {len(filtered)} matches" if filtered else f"No match (best={best_val:.2f})")

    def _list_images(self):
        """List all saved image templates."""
        if not os.path.exists(self.images_path):
            self.command_bar.set_status("No images saved")
            self.command_bar.show_help("No images saved")
            return
        files = [f[:-4] for f in os.listdir(self.images_path) if f.endswith(".png")]
        if not files:
            self.command_bar.set_status("No images saved")
            self.command_bar.show_help("No images saved")
            return
        self.command_bar.set_status(f"Images ({len(files)})")
        self.command_bar.show_help("\n".join(sorted(files)))

    def _delete_image(self, name):
        """Delete a saved image template."""
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing image name")
            return
        image_path = os.path.join(self.images_path, f"{name}.png")
        if not os.path.exists(image_path):
            self.command_bar.set_status(f"Image not found: {name}")
            return
        os.remove(image_path)
        self.command_bar.set_status(f"Deleted image: {name}")

    def _run_macro(self, name):
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing macro name")
            return
        if self._recording_name is not None:
            self.command_bar.set_status("Stop recording first")
            return
        if self._macro_running:
            self.command_bar.set_status("Macro already running")
            return
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return
        steps = self._get_macro_steps(name)
        if not steps:
            self.command_bar.set_status(f"Macro empty: {name}")
            return
        # Check resolution for v2 macros
        recorded_res = self._get_macro_resolution(name)
        if recorded_res:
            current_res = (
                int(self.screen_frame.size.width),
                int(self.screen_frame.size.height),
            )
            if recorded_res != current_res:
                self.command_bar.set_status(
                    f"Resolution: {recorded_res[0]}x{recorded_res[1]} → {current_res[0]}x{current_res[1]}"
                )
        expanded = self._expand_macro(name)
        if expanded is None:
            return
        # Do not let the first match selection in a new macro inherit
        # a stale click anchor from a previous macro run.
        self.last_click_point = None
        self._macro_name = name
        self._macro_root = name
        self._macro_queue = expanded
        self._macro_running = True
        self._macro_wait_reason = None
        self.command_bar.set_status(f"Running {name}")
        self.command_bar.hide()
        self._run_next_macro_step()

    def _expand_macro(self, name):
        name = self._normalize_macro_name(name)
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return None
        steps = self._get_macro_steps(name)
        if name in self._macro_stack:
            self._abort_macro("Macro recursion detected")
            return None
        if len(self._macro_stack) >= 5:
            self._abort_macro("Macro nesting too deep")
            return None
        self._macro_stack.append(name)
        return list(steps) + [f"__macro_end__ {name}"]

    def _normalize_macro_name(self, name):
        if not name:
            return ""
        cleaned = name.strip()
        if len(cleaned) >= 2:
            pairs = [
                ('"', '"'),
                ("'", "'"),
                ("“", "”"),
                ("‘", "’"),
            ]
            for left, right in pairs:
                if cleaned.startswith(left) and cleaned.endswith(right):
                    cleaned = cleaned[1:-1].strip()
                    break
        return cleaned

    def _run_next_macro_step(self):
        if not self._macro_running:
            return
        if not self._macro_queue:
            name = self._macro_root or "macro"
            self._macro_running = False
            self._macro_name = None
            self._macro_root = None
            self._macro_wait_reason = None
            self._macro_stack = []
            self.command_bar.set_status(f"Macro complete: {name}")
            return
        step = self._macro_queue.pop(0)
        self._execute_macro_step(step)
        if self._macro_running and self._macro_wait_reason is None:
            if self._macro_delay and self._macro_delay > 0:
                def schedule_timer():
                    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                        self._macro_delay, self, "macroDelayFired:", None, False
                    )

                run_on_main(schedule_timer)
            else:
                self._run_next_macro_step()

    def macroDelayFired_(self, timer):
        self._run_next_macro_step()

    def _execute_macro_step(self, step):
        command = step.strip()
        if not command:
            return
        if command.startswith("__macro_end__ "):
            name = command.split(" ", 1)[1].strip()
            if self._macro_stack and self._macro_stack[-1] == name:
                self._macro_stack.pop()
            return
        parts = command.split(" ", 1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        log_label = command if name not in ("find", "find-wait", "smart-click", "smart-rclick", "smart-dclick", "type", "type-keys") else name
        print(f"MACRO STEP: {log_label}  (matches={len(self.matches)}, queue={len(self._macro_queue)} remaining)")
        if name == "capture":
            self._macro_wait_reason = "capture"
            self._sync_active_screen_to_command_bar(announce=False)
            self._handle_capture()
        elif name == "find":
            self._macro_wait_reason = "find"
            self._handle_find(arg)
        elif name == "click":
            self._handle_click(arg, record=False, button="left")
        elif name == "rclick":
            self._handle_click(arg, record=False, button="right")
        elif name == "rightclick":
            self._handle_click(arg, record=False, button="right")
        elif name == "clear":
            self.clear_and_close()
        elif name == "run":
            expanded = self._expand_macro(arg)
            if expanded is None:
                return
            self._macro_queue = expanded + self._macro_queue
        elif name == "find-wait":
            self._macro_wait_reason = "find-wait"
            self._find_wait_query = arg.strip('"')
            self._find_wait_attempts = 0
            self._find_wait_max = 10  # max retries (10 * 1s = 10s timeout)
            self._handle_find(self._find_wait_query)
        elif name == "find-image":
            self._macro_wait_reason = "find-image"
            self._find_image(arg)
        elif name == "find-mrn":
            self._macro_wait_reason = "find-mrn"
            self._handle_find_mrn()
        elif name == "wait":
            self._execute_wait(arg)
        elif name == "smart-click":
            self._macro_wait_reason = "smart-click"
            self._execute_smart_click(arg, button="left")
        elif name == "smart-rclick":
            self._macro_wait_reason = "smart-click"
            self._execute_smart_click(arg, button="right")
        elif name == "click-at":
            self._execute_click_at(arg, button="left")
        elif name == "rclick-at":
            self._execute_click_at(arg, button="right")
        elif name == "click-relative":
            self._execute_click_relative(arg)
        elif name == "smart-dclick":
            self._macro_wait_reason = "smart-click"
            self._execute_smart_click(arg, button="left", click_count=2)
        elif name == "dclick-at":
            self._execute_click_at(arg, button="left", click_count=2)
        elif name == "key-press":
            self._execute_key_press(arg)
        elif name == "type":
            self._execute_type(arg)
        elif name == "type-keys":
            self._execute_type_keys(arg)
        else:
            self._abort_macro(f"Unknown step: {step}")

    def _macro_step_complete(self):
        if not self._macro_running:
            return
        self._macro_wait_reason = None
        self._run_next_macro_step()

    def _abort_macro(self, message):
        if self._macro_running:
            self._macro_running = False
            self._macro_queue = []
            self._macro_name = None
            self._macro_wait_reason = None
            self._macro_root = None
            self._macro_stack = []
            self.command_bar.show()
        if message:
            self.command_bar.set_status(message)

    def _execute_wait(self, arg):
        """Execute a wait command during macro playback (non-blocking)."""
        try:
            seconds = float(arg)
        except (ValueError, TypeError):
            seconds = 1.0
        seconds = max(0.0, min(30.0, seconds))  # Clamp to 0-30s
        if seconds > 0:
            self.command_bar.set_status(f"Waiting {seconds:.1f}s...")
            self._macro_wait_reason = "wait"

            def schedule_wait_timer():
                AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    seconds, self, "waitTimerFired:", None, False
                )

            run_on_main(schedule_wait_timer)
        else:
            # No wait needed, continue immediately
            pass

    def waitTimerFired_(self, timer):
        """Called when wait timer completes."""
        if self._macro_wait_reason == "wait":
            self._macro_step_complete()

    def findWaitRetryFired_(self, timer):
        """Retry find-wait: re-capture screen and search again."""
        if self._macro_wait_reason != "find-wait":
            return
        self._handle_find(self._find_wait_query)

    # Map key names to macOS virtual key codes
    _KEY_PRESS_SPECS = {
        "down": (125, 0),
        "up": (126, 0),
        "left": (123, 0),
        "right": (124, 0),
        "page-down": (121, 0),
        "page-up": (116, 0),
        "line-start": (123, Quartz.kCGEventFlagMaskCommand),
        "line-end": (124, Quartz.kCGEventFlagMaskCommand),
        "text-start": (0, Quartz.kCGEventFlagMaskControl),
        "text-end": (14, Quartz.kCGEventFlagMaskControl),
        "ctrl-a": (0, Quartz.kCGEventFlagMaskControl),
        "ctrl-e": (14, Quartz.kCGEventFlagMaskControl),
        "cmd-left": (123, Quartz.kCGEventFlagMaskCommand),
        "cmd-right": (124, Quartz.kCGEventFlagMaskCommand),
        "command-left": (123, Quartz.kCGEventFlagMaskCommand),
        "command-right": (124, Quartz.kCGEventFlagMaskCommand),
    }

    _KEY_PRESS_SYSTEM_EVENTS = {
        "up-se": (126, []),
        "down-se": (125, []),
        "left-se": (123, []),
        "right-se": (124, []),
        "text-start-se": (0, ["control down"]),
        "text-end-se": (14, ["control down"]),
        "ctrl-a-se": (0, ["control down"]),
        "ctrl-e-se": (14, ["control down"]),
    }

    _TYPE_KEYCODES = {
        "a": 0,
        "s": 1,
        "d": 2,
        "f": 3,
        "h": 4,
        "g": 5,
        "z": 6,
        "x": 7,
        "c": 8,
        "v": 9,
        "b": 11,
        "q": 12,
        "w": 13,
        "e": 14,
        "r": 15,
        "y": 16,
        "t": 17,
        "1": 18,
        "2": 19,
        "3": 20,
        "4": 21,
        "6": 22,
        "5": 23,
        "=": 24,
        "9": 25,
        "7": 26,
        "-": 27,
        "8": 28,
        "0": 29,
        "]": 30,
        "o": 31,
        "u": 32,
        "[": 33,
        "i": 34,
        "p": 35,
        "l": 37,
        "j": 38,
        "'": 39,
        "k": 40,
        ";": 41,
        "\\": 42,
        ",": 43,
        "/": 44,
        "n": 45,
        "m": 46,
        ".": 47,
        "`": 50,
        " ": 49,
    }

    _TYPE_SHIFTED = {
        "_": "-",
        "+": "=",
        ")": "0",
        "(": "9",
        "*": "8",
        "&": "7",
        "^": "6",
        "%": "5",
        "$": "4",
        "#": "3",
        "@": "2",
        "!": "1",
        "}": "]",
        "{": "[",
        ":": ";",
        "\"": "'",
        "|": "\\",
        "<": ",",
        ">": ".",
        "?": "/",
        "~": "`",
    }

    def _execute_key_press(self, arg):
        """Execute a key-press step during macro playback."""
        parts = arg.strip().lower().split()
        if not parts:
            self._abort_macro("Unknown key: ")
            return
        key_name = parts[0]
        repeat = 1
        if len(parts) >= 2:
            try:
                repeat = max(1, min(100, int(parts[1])))
            except ValueError:
                self._abort_macro(f"Invalid key repeat: {arg}")
                return

        system_spec = self._KEY_PRESS_SYSTEM_EVENTS.get(key_name)
        if system_spec is not None:
            key_code, modifiers = system_spec
            mods = f" using {{{', '.join(modifiers)}}}" if modifiers else ""
            if repeat == 1:
                script = f'tell application "System Events" to key code {key_code}{mods}'
            else:
                script = (
                    'tell application "System Events"\n'
                    f'  repeat {repeat} times\n'
                    f'    key code {key_code}{mods}\n'
                    '    delay 0.02\n'
                    '  end repeat\n'
                    'end tell'
                )
            print(
                f"DEBUG _execute_key_press: pressing {arg} via System Events "
                f"(code={key_code}, modifiers={modifiers}, repeat={repeat})"
            )
            try:
                subprocess.run(["osascript", "-e", script], check=True)
            except Exception as exc:
                self._abort_macro(f"Key press failed: {arg} ({exc})")
                return
            self._macro_step_complete()
            return

        key_spec = self._KEY_PRESS_SPECS.get(key_name)
        if key_spec is None:
            self._abort_macro(f"Unknown key: {arg}")
            return
        key_code, flags = key_spec
        print(f"DEBUG _execute_key_press: pressing {arg} (code={key_code}, flags={flags}, repeat={repeat})")
        for _ in range(repeat):
            event_down = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
            event_up = Quartz.CGEventCreateKeyboardEvent(None, key_code, False)
            if flags:
                Quartz.CGEventSetFlags(event_down, flags)
                Quartz.CGEventSetFlags(event_up, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)
            if repeat > 1:
                time.sleep(0.02)
        self._macro_step_complete()

    def _capture_active_screen_bgr(self):
        image, width_px, height_px, scale, _bounds_px = self.ocr_engine.capture_display(
            self._active_display_id,
            (self.screen_frame.size.width, self.screen_frame.size.height),
        )
        bytes_per_row = Quartz.CGImageGetBytesPerRow(image)
        data_provider = Quartz.CGImageGetDataProvider(image)
        data = Quartz.CGDataProviderCopyData(data_provider)
        arr = np.frombuffer(data, dtype=np.uint8)
        arr = arr.reshape((height_px, bytes_per_row // 4, 4))
        screen_bgr = arr[:, :width_px, :3].copy()
        return image, screen_bgr, width_px, height_px, scale

    def _save_button_state(self):
        """Return True when a visible Save button looks enabled, False when it looks disabled, or None if undetermined."""
        try:
            image, screen_bgr, width_px, height_px, scale = self._capture_active_screen_bgr()
            items = self.ocr_engine.recognize_text(image, width_px, height_px, scale)
        except Exception as exc:
            print(f"DEBUG _save_button_state: capture failed: {exc}")
            return None

        save_matches = []
        for item in items:
            text = item["text"].strip()
            norm = re.sub(r"[^a-z]", "", text.lower())
            if norm != "save":
                continue
            x, y, w, h = item["bbox"]
            if y < self.screen_frame.size.height * 0.25:
                continue
            if w > self.screen_frame.size.width * 0.15:
                continue
            if h > self.screen_frame.size.height * 0.08:
                continue
            save_matches.append({"text": text, "bbox": item["bbox"]})
        if not save_matches:
            for item in items:
                text = item["text"].strip()
                norm = re.sub(r"[^a-z]", "", text.lower())
                if norm != "cancel":
                    continue
                x, y, w, h = item["bbox"]
                if y < self.screen_frame.size.height * 0.25:
                    continue
                inferred_bbox = (x + (w * 2.35), y, w, h)
                save_matches.append({"text": "Save(inferred)", "bbox": inferred_bbox})
        if not save_matches:
            print("DEBUG _save_button_state: no Save text found")
            return None

        def save_sort_key(item):
            x, y, w, h = item["bbox"]
            cx = x + (w / 2.0)
            cy = y + (h / 2.0)
            dx = cx - self.screen_center[0]
            dy = cy - (self.screen_center[1] + self.screen_frame.size.height * 0.1)
            return (dx * dx + dy * dy, -y)

        save_matches = sorted(save_matches, key=save_sort_key)
        x, y, w, h = save_matches[0]["bbox"]
        pad_x = max(w * 1.1, 22.0)
        pad_y = max(h * 1.4, 18.0)
        x0 = max(0, int((x - pad_x) * scale))
        y0 = max(0, int((y - pad_y) * scale))
        x1 = min(width_px, int((x + w + pad_x) * scale))
        y1 = min(height_px, int((y + h + pad_y) * scale))
        if x1 <= x0 or y1 <= y0:
            return None

        roi = screen_bgr[y0:y1, x0:x1]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        blue_mask = (
            (hsv[:, :, 0] >= 95) &
            (hsv[:, :, 0] <= 130) &
            (hsv[:, :, 1] >= 60) &
            (hsv[:, :, 2] >= 80)
        )
        blue_ratio = float(np.mean(blue_mask))
        sat_mean = float(np.mean(hsv[:, :, 1]))
        enabled = blue_ratio >= 0.05 or sat_mean >= 40.0
        print(
            f"DEBUG _save_button_state: text='{save_matches[0]['text']}', "
            f"blue_ratio={blue_ratio:.3f}, sat_mean={sat_mean:.1f}, enabled={enabled}"
        )
        return enabled

    def _wait_for_save_enabled(self, timeout=1.5, interval=0.15):
        deadline = time.time() + timeout
        saw_disabled = False
        while time.time() < deadline:
            state = self._save_button_state()
            if state is True:
                return True
            if state is False:
                saw_disabled = True
            time.sleep(interval)
        if saw_disabled:
            return False
        return None

    def _type_via_keycodes(self, text, delay=0.01):
        for ch in text:
            base = ch
            needs_shift = False
            if ch.isalpha() and ch.upper() == ch:
                base = ch.lower()
                needs_shift = True
            elif ch in self._TYPE_SHIFTED:
                base = self._TYPE_SHIFTED[ch]
                needs_shift = True

            key_code = self._TYPE_KEYCODES.get(base)
            if key_code is None:
                self._abort_macro(f"Unsupported typed character: {ch!r}")
                return

            event_down = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
            if needs_shift:
                Quartz.CGEventSetFlags(event_down, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)
            event_up = Quartz.CGEventCreateKeyboardEvent(None, key_code, False)
            if needs_shift:
                Quartz.CGEventSetFlags(event_up, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)
            time.sleep(delay)

    def _type_via_paste(self, text):
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, AppKit.NSPasteboardTypeString)

        cmd_down = Quartz.CGEventCreateKeyboardEvent(None, 55, True)
        v_down = Quartz.CGEventCreateKeyboardEvent(None, 9, True)
        v_up = Quartz.CGEventCreateKeyboardEvent(None, 9, False)
        cmd_up = Quartz.CGEventCreateKeyboardEvent(None, 55, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, cmd_down)
        time.sleep(0.01)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_down)
        time.sleep(0.01)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, v_up)
        time.sleep(0.01)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, cmd_up)

    def _type_via_system_events(self, text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "System Events" to keystroke "{escaped}"'
        subprocess.run(["osascript", "-e", script], check=True)

    def _execute_type(self, arg, allow_paste=True):
        """Type text and verify it by checking whether Save becomes enabled."""
        import datetime
        text = arg.strip('"')
        today = datetime.date.today().strftime("%m/%d/%Y")
        text = text.replace("{date}", today)
        text_len = len(text)

        attempts = []
        if allow_paste:
            attempts.append(("paste", lambda: self._type_via_paste(text)))
        attempts.extend([
            ("system-events", lambda: self._type_via_system_events(text)),
            ("keycodes", lambda: self._type_via_keycodes(text, delay=0.01)),
            ("keycodes-slow", lambda: self._type_via_keycodes(text, delay=0.04)),
        ])
        final_state = None

        for label, sender in attempts:
            print(f"DEBUG _execute_type: trying {label} (len={text_len})")
            try:
                sender()
            except Exception as exc:
                print(f"DEBUG _execute_type: {label} failed (len={text_len}): {exc}")
                continue
            state = self._wait_for_save_enabled()
            final_state = state
            if state is True:
                print(f"DEBUG _execute_type: {label} succeeded (len={text_len})")
                self._macro_step_complete()
                return
            if state is False:
                print(f"DEBUG _execute_type: {label} did not enable Save (len={text_len})")
            else:
                print(f"DEBUG _execute_type: {label} sent text, Save state unknown (len={text_len})")

        if final_state is False:
            self._abort_macro("type failed: Save stayed disabled")
            return
        self._abort_macro("type failed: could not verify Save")

    def _execute_type_keys(self, arg):
        """Type text once using keyboard-style input only, without paste or Save-state retries."""
        import datetime
        text = arg.strip('"')
        today = datetime.date.today().strftime("%m/%d/%Y")
        text = text.replace("{date}", today)
        text_len = len(text)

        print(f"DEBUG _execute_type_keys: trying system-events (len={text_len})")
        try:
            self._type_via_system_events(text)
            self._macro_step_complete()
            return
        except Exception as exc:
            print(f"DEBUG _execute_type_keys: system-events failed (len={text_len}): {exc}")

        print(f"DEBUG _execute_type_keys: falling back to keycodes (len={text_len})")
        self._type_via_keycodes(text, delay=0.03)
        self._macro_step_complete()

    def _execute_smart_click(self, arg, button="left", click_count=1):
        """Execute a smart-click command during macro playback.

        Format: smart-click "query" xPct yPct [--allow-fallback] [--wait [seconds]] [--attempts N]
        """
        # Parse flags first, then parse the core smart-click args.
        wait_seconds = 0.0
        wait_match = re.search(r"--wait(?:\s+(\d+(?:\.\d+)?))?", arg)
        if wait_match:
            try:
                wait_seconds = float(wait_match.group(1) or "10")
            except ValueError:
                wait_seconds = 10.0
            arg = (arg[:wait_match.start()] + arg[wait_match.end():]).strip()

        click_attempts = 1
        attempts_match = re.search(r"--attempts\s+(\d+)", arg)
        if attempts_match:
            try:
                click_attempts = max(1, min(5, int(attempts_match.group(1))))
            except ValueError:
                click_attempts = 1
            arg = (arg[:attempts_match.start()] + arg[attempts_match.end():]).strip()

        allow_fallback = "--allow-fallback" in arg
        arg_clean = arg.replace("--allow-fallback", "").strip()

        # Extract quoted query and coordinates
        query, x_pct, y_pct = self._parse_smart_click_args(arg_clean)
        if not query:
            self._abort_macro(f"Invalid smart-click: {arg}")
            return

        self.command_bar.set_status("Finding text...")

        # Run capture + OCR + find (async)
        self._smart_click_query = query
        self._smart_click_x_pct = x_pct
        self._smart_click_y_pct = y_pct
        self._smart_click_button = button
        self._smart_click_allow_fallback = allow_fallback
        self._smart_click_count = click_count
        self._smart_click_attempts = click_attempts
        self._smart_click_wait_until = (time.time() + wait_seconds) if wait_seconds > 0 else None
        self._smart_click_retry_count = 0

        # Trigger find, which will call _smart_click_after_find when done
        self._pending_find_query = query
        self._handle_capture()

    def _clear_smart_click_state(self):
        self._smart_click_query = None
        self._smart_click_x_pct = None
        self._smart_click_y_pct = None
        self._smart_click_button = None
        self._smart_click_allow_fallback = None
        self._smart_click_count = None
        self._smart_click_attempts = None
        self._smart_click_wait_until = None
        self._smart_click_retry_count = 0

    def _parse_smart_click_args(self, arg):
        """Parse smart-click arguments: "query" xPct yPct"""
        query = None
        x_pct = None
        y_pct = None

        # Handle quoted query
        if arg.startswith('"'):
            # Find closing quote (handle escaped quotes)
            i = 1
            while i < len(arg):
                if arg[i] == '"' and arg[i-1] != '\\':
                    break
                i += 1
            if i < len(arg):
                query = arg[1:i].replace('\\"', '"').replace('\\\\', '\\')
                rest = arg[i+1:].strip()
                parts = rest.split()
                if len(parts) >= 2:
                    try:
                        x_pct = float(parts[0])
                        y_pct = float(parts[1])
                    except ValueError:
                        pass
                elif len(parts) == 0:
                    # No coordinates - just query
                    x_pct = None
                    y_pct = None
        else:
            # Unquoted - split by space
            parts = arg.split()
            if parts:
                query = parts[0]
                if len(parts) >= 3:
                    try:
                        x_pct = float(parts[1])
                        y_pct = float(parts[2])
                    except ValueError:
                        pass

        return query, x_pct, y_pct

    def _execute_click_at(self, arg, button="left", click_count=1):
        """Execute a click-at command during macro playback.

        Format: click-at xPct yPct
        Clicks at absolute screen coordinates (normalized 0-1).
        """
        parts = arg.strip().split()
        if len(parts) < 2:
            self._abort_macro(f"Invalid click-at: {arg}")
            return

        try:
            x_pct = float(parts[0])
            y_pct = float(parts[1])
        except ValueError:
            self._abort_macro(f"Invalid click-at coordinates: {arg}")
            return

        # Convert to screen coordinates
        screen_w = self.screen_frame.size.width
        screen_h = self.screen_frame.size.height
        click_x = x_pct * screen_w
        click_y = y_pct * screen_h

        click_type = "double-clicking" if click_count >= 2 else "clicking"
        print(f"DEBUG _execute_click_at: {click_type} at ({click_x:.1f}, {click_y:.1f})")
        self._click_at(click_x, click_y, button, click_count)
        self._macro_step_complete()

    def _execute_click_relative(self, arg):
        """Click relative to an existing OCR/image match.

        Format: click-relative index dx dy
        where dx and dy are offsets in units of the matched bbox width/height.
        Example: click-relative 1 -0.5 6.0
        """
        parts = arg.strip().split()
        if len(parts) < 3:
            self._abort_macro(f"Invalid click-relative: {arg}")
            return

        try:
            index = int(parts[0])
            dx = float(parts[1])
            dy = float(parts[2])
        except ValueError:
            self._abort_macro(f"Invalid click-relative args: {arg}")
            return

        if index < 1 or index > len(self.matches):
            self._abort_macro(f"click-relative {index}: no match (only {len(self.matches)} found)")
            return

        match = self.matches[index - 1]
        x, y, w, h = match["bbox"]
        anchor_x = x + (w / 2.0)
        anchor_y = y + (h / 2.0)
        click_x = anchor_x + (dx * w)
        click_y = anchor_y + (dy * h)
        print(
            f"DEBUG _execute_click_relative: index={index}, anchor=({anchor_x:.1f}, {anchor_y:.1f}), "
            f"offset=({dx:.3f}w, {dy:.3f}h), click=({click_x:.1f}, {click_y:.1f})"
        )
        self._dispatch_macro_click(
            click_x,
            click_y,
            button="left",
            click_count=1,
            reason="click-relative-click",
            attempts=1,
        )

    def _dispatch_macro_click(self, x, y, button="left", click_count=1, reason="macro-click", attempts=None):
        """Dispatch a macro click on the next runloop cycle."""
        if attempts is None:
            attempts = 1

        self.last_click_point = (x, y)
        self.overlay.clear()
        self.matches = []
        self._macro_wait_reason = reason

        def _do_click():
            print(
                f"DEBUG {reason}: dispatching {button} click_count={click_count} "
                f"at ({x:.1f}, {y:.1f}), attempts={attempts}"
            )
            for attempt in range(attempts):
                self._click_at(x, y, button=button, click_count=click_count)
                if attempt + 1 < attempts:
                    time.sleep(0.3)
            self._macro_step_complete()

        run_on_main(_do_click)

    def _smart_click_after_find(self):
        """Called after OCR completes to finish smart-click execution."""
        query = getattr(self, "_smart_click_query", None)
        x_pct = getattr(self, "_smart_click_x_pct", None)
        y_pct = getattr(self, "_smart_click_y_pct", None)
        button = getattr(self, "_smart_click_button", "left")
        allow_fallback = getattr(self, "_smart_click_allow_fallback", False)
        click_count = getattr(self, "_smart_click_count", 1)
        click_attempts = getattr(self, "_smart_click_attempts", 1) or 1
        wait_until = getattr(self, "_smart_click_wait_until", None)
        retry_count = getattr(self, "_smart_click_retry_count", 0)

        if not self.matches:
            # No matches found
            if wait_until is not None and time.time() < wait_until:
                self._smart_click_retry_count = retry_count + 1
                print(f"DEBUG _smart_click_after_find: no match, retry {self._smart_click_retry_count}")

                def _retry():
                    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                        0.5, self, "smartClickRetryFired:", None, False
                    )

                run_on_main(_retry)
                return
            if allow_fallback and x_pct is not None and y_pct is not None:
                # Fallback to coordinate click
                self.command_bar.set_status("Text not found, using coordinates")
                target_x = x_pct * self.screen_frame.size.width
                target_y = y_pct * self.screen_frame.size.height
                self._clear_smart_click_state()
                self._dispatch_macro_click(
                    target_x,
                    target_y,
                    button=button,
                    click_count=click_count,
                    attempts=click_attempts,
                    reason="smart-click-fallback",
                )
                return
            else:
                # Safe fallback: stop macro
                self._clear_smart_click_state()
                self._abort_macro("Text not found - macro stopped")
                return

        if len(self.matches) == 1 or x_pct is None or y_pct is None:
            # Single match or no coordinates - click first match
            match = self.matches[0]
        else:
            # Multiple matches - find closest to stored coordinates
            target_x = x_pct * self.screen_frame.size.width
            target_y = y_pct * self.screen_frame.size.height

            def distance_to_target(m):
                x, y, w, h = m["bbox"]
                cx = x + (w / 2.0)
                cy = y + (h / 2.0)
                return (cx - target_x) ** 2 + (cy - target_y) ** 2

            match = min(
                self.matches,
                key=lambda m: (m.get("find_priority", (9, 9, 9, 999999)), distance_to_target(m)),
            )

        # Click the match
        bbox = match["bbox"]
        x, y, w, h = bbox
        cx = x + (w / 2.0)
        cy = y + (h / 2.0)
        exact = self._normalize_find_text(match.get("text", "")) == self._normalize_find_text(query or "")
        print(f"DEBUG _smart_click_after_find: selected match at ({cx:.1f}, {cy:.1f}), exact={exact}")
        self._clear_smart_click_state()
        self._dispatch_macro_click(
            cx,
            cy,
            button=button,
            click_count=click_count,
            attempts=click_attempts,
            reason="smart-click-click",
        )

    def smartClickRetryFired_(self, timer):
        if self._macro_wait_reason != "smart-click":
            return
        query = getattr(self, "_smart_click_query", None)
        if not query:
            return
        self._pending_find_query = query
        self._handle_capture()

    def _anchor_point(self):
        if self.last_click_point is not None:
            return self.last_click_point
        return self.screen_center

    def _order_matches_by_anchor(self, matches):
        if not matches:
            return []
        anchor_x, anchor_y = self._anchor_point()

        def sort_key(item):
            x, y, w, h = item["bbox"]
            cx = x + (w / 2.0)
            cy = y + (h / 2.0)
            dx = cx - anchor_x
            dy = cy - anchor_y
            return (item.get("find_priority", (0, 0, 0, 0)), dx * dx + dy * dy, y, x)

        return sorted(matches, key=sort_key)

    def _install_key_monitor(self):
        if self._key_monitor is not None:
            return

        def handler(event):
            chars = event.characters()
            print(f"DEBUG key_handler: char={chars!r}, visible={self.command_bar.visible}, matches={len(self.matches)}, input_text={self.command_bar.input_text()!r}")
            if not self.command_bar.visible:
                return event
            if not self.matches:
                return event
            if self.command_bar.input_text():
                return event
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags != 0:
                return event
            if not chars or len(chars) != 1:
                return event
            index, button = self._index_and_button_for_char(chars)
            if index is None:
                return event
            print(f"DEBUG key_handler: triggering click index={index}")
            self._handle_click(index, record=True, button=button)
            return None

        self._key_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, handler
        )

    def _index_and_button_for_char(self, char):
        if char in "123456789":
            return int(char), "left"
        letter = char.lower()
        if "a" <= letter <= "z":
            return (ord(letter) - ord("a") + 1), "right"
        return None, None

    def _remove_key_monitor(self):
        if self._key_monitor is None:
            return
        AppKit.NSEvent.removeMonitor_(self._key_monitor)
        self._key_monitor = None

    def handle_command(self, text):
        self._reload_macros_if_changed()
        command = text.strip()
        if not command:
            return
        parts = command.split(" ", 1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        self.command_bar.clear_input()

        if name != "help":
            self.command_bar.hide_help()

        if name == "capture":
            self._record_step("capture")
            self._sync_active_screen_to_command_bar(announce=False)
            self._handle_capture()
        elif name == "find":
            # In v2 recording, don't record "find" - smart-click will capture it
            if arg and self._recording_name is None:
                self._record_step(f"find {arg}")
            self._sync_active_screen_to_command_bar(announce=False)
            self._handle_find(arg)
        elif name == "click":
            self._handle_click(arg, record=True, button="left")
        elif name == "rclick":
            self._handle_click(arg, record=True, button="right")
        elif name == "rightclick":
            self._handle_click(arg, record=True, button="right")
        elif name == "clear":
            self._record_step("clear")
            self.clear_and_close()
        elif name == "record":
            self._start_recording(arg)
        elif name == "stop":
            self._stop_recording()
        elif name == "run":
            self._run_macro(arg)
        elif name == "macros":
            self._list_macros()
        elif name == "edit":
            self._open_macro_editor(arg)
        elif name == "show":
            self._show_macro(arg)
        elif name == "delete":
            self._delete_macro(arg)
        elif name == "find-mrn":
            self._sync_active_screen_to_command_bar(announce=False)
            self._handle_find_mrn()
        elif name == "capture-image":
            self._sync_active_screen_to_command_bar(announce=False)
            self._capture_image(arg)
        elif name == "find-image":
            self._sync_active_screen_to_command_bar(announce=False)
            self._find_image(arg)
        elif name == "images":
            self._list_images()
        elif name == "delete-image":
            self._delete_image(arg)
        elif name == "screens":
            self._list_screens()
        elif name == "screen":
            self._handle_screen_command(arg)
        elif name == "help":
            self.command_bar.set_status("Commands")
            self.command_bar.show_help(
                "capture  - capture active screen (follows command bar by default)\n"
                "find <text>  - capture + find text\n"
                "click <number>  - left click match\n"
                "rclick <number>  - right click match\n"
                "screens  - list displays\n"
                "screen <n>|auto  - set active display (auto follows command bar)\n"
                "clear  - close and reset\n"
                "record <name>  - start recording\n"
                "stop  - save recording\n"
                "run <name>  - run macro\n"
                "macros  - list macros\n"
                "edit [name]  - open macro editor GUI\n"
                "show <name>  - show macro steps\n"
                "delete <name>  - remove macro\n"
                "find-mrn  - find MRN on screen and copy to clipboard\n"
                "capture-image <name>  - save region (recording)\n"
                "find-image <name>  - find image (macro)\n"
                "images  - list saved images\n"
                "delete-image <name>  - remove image\n"
                "tip: 1-9 = left click, a-i = right click"
            )
        else:
            macro_name = self._normalize_macro_name(command)
            if macro_name in self.macros:
                self._record_step(f"run {macro_name}")
                self._run_macro(macro_name)
            else:
                # In v2 recording, don't record "find" - smart-click will capture it
                if self._recording_name is None:
                    self._record_step(f"find {command}")
                self._handle_find(command)

    def _list_screens(self):
        screens = self._screens()
        if not screens:
            self.command_bar.set_status("No screens detected")
            self.command_bar.show_help("No screens detected")
            return
        lines = []
        for idx, s in enumerate(screens):
            f = s.frame()
            did = self._screen_display_id(s)
            active = "*" if idx == self._active_screen_index else " "
            # frame origin can be non-zero in multi-display setups
            lines.append(
                f"{active} {idx+1}: {int(f.size.width)}x{int(f.size.height)} pts @ ({int(f.origin.x)},{int(f.origin.y)})  id={did}"
            )
        self.command_bar.set_status(f"Screens ({len(screens)})")
        self.command_bar.show_help("\n".join(lines))

    def _handle_screen_command(self, arg):
        val = (arg or "").strip().lower()
        if not val or val == "auto":
            self._follow_command_bar = True
            self._sync_active_screen_to_command_bar(announce=True)
            self.command_bar.set_status("Active screen: auto (command bar)")
            return
        try:
            idx = int(val) - 1
        except ValueError:
            self.command_bar.set_status("Usage: screen <n>|auto")
            return
        # Manual override disables following the command bar.
        self._follow_command_bar = False
        self._set_active_screen(idx, announce=True, rebuild_command_bar=True)

    def _handle_capture(self):
        if self._ocr_in_progress:
            self.command_bar.set_status("Capturing...")
            return
        self._ocr_in_progress = True
        self.capture_width_px = None
        self.capture_height_px = None
        self.capture_scale = None
        self.overlay.clear()
        self.command_bar.set_status("Capturing...")

        def task():
            with objc.autorelease_pool():
                try:
                    image, width_px, height_px, scale, bounds_px = (
                        self.ocr_engine.capture_display(
                            self._active_display_id,
                            (self.screen_frame.size.width, self.screen_frame.size.height),
                        )
                    )
                    # Store origin (points) in global Quartz space for clicks.
                    self.capture_origin_pt = (
                        bounds_px.origin.x / float(scale or 1.0),
                        bounds_px.origin.y / float(scale or 1.0),
                    )
                    self._display_bounds_px = bounds_px
                except PermissionError:
                    run_on_main(
                        lambda: self.command_bar.set_status(
                            "Screen Recording permission required"
                        )
                    )
                    self.capture_width_px = None
                    self.capture_height_px = None
                    self.capture_scale = None
                    if self._macro_wait_reason is not None:
                        self._abort_macro("Capture blocked by permission")
                    self._pending_find_query = None
                    self._ocr_in_progress = False
                    return
                except Exception as exc:
                    print(f"Capture failed: {exc}")
                    run_on_main(lambda: self.command_bar.set_status("Capture failed"))
                    self.capture_width_px = None
                    self.capture_height_px = None
                    self.capture_scale = None
                    if self._macro_wait_reason is not None:
                        self._abort_macro("Capture failed")
                    self._pending_find_query = None
                    self._ocr_in_progress = False
                    return

                run_on_main(lambda: self.command_bar.set_status("Running OCR..."))
                try:
                    items = self.ocr_engine.recognize_text(
                        image, width_px, height_px, scale
                    )
                except Exception as exc:
                    print(f"OCR failed: {exc}")
                    run_on_main(lambda: self.command_bar.set_status("OCR failed"))
                    if self._macro_wait_reason is not None:
                        self._abort_macro("OCR failed")
                    self._ocr_in_progress = False
                    return

            def finish():
                self.ocr_items = items
                self.matches = []
                self.capture_width_px = width_px
                self.capture_height_px = height_px
                self.capture_scale = scale
                self._ocr_in_progress = False
                self.command_bar.set_status(f"OCR complete: {len(items)} items")
                if self._macro_wait_reason == "capture":
                    self._macro_step_complete()
                if self._pending_find_query:
                    pending = self._pending_find_query
                    self._pending_find_query = None
                    self._run_find(pending)

            run_on_main(finish)

        threading.Thread(target=task, daemon=True).start()

    def _handle_find_mrn(self):
        """Capture screen, OCR it, find MRN pattern, copy to clipboard."""
        if self._ocr_in_progress:
            self.command_bar.set_status("OCR already in progress")
            return
        self._ocr_in_progress = True
        self.command_bar.set_status("Capturing for MRN...")

        def task():
            with objc.autorelease_pool():
                try:
                    image, width_px, height_px, scale, bounds_px = (
                        self.ocr_engine.capture_display(
                            self._active_display_id,
                            (self.screen_frame.size.width, self.screen_frame.size.height),
                        )
                    )
                except Exception as exc:
                    print(f"MRN capture failed: {exc}")
                    run_on_main(lambda: self.command_bar.set_status("Capture failed"))
                    self._ocr_in_progress = False
                    if self._macro_wait_reason == "find-mrn":
                        run_on_main(lambda: self._abort_macro("Capture failed"))
                    return

                run_on_main(lambda: self.command_bar.set_status("Running OCR for MRN..."))
                try:
                    items = self.ocr_engine.recognize_text(image, width_px, height_px, scale)
                except Exception as exc:
                    print(f"MRN OCR failed: {exc}")
                    run_on_main(lambda: self.command_bar.set_status("OCR failed"))
                    self._ocr_in_progress = False
                    if self._macro_wait_reason == "find-mrn":
                        run_on_main(lambda: self._abort_macro("OCR failed"))
                    return

            # Concatenate all OCR text and search for MRN pattern.
            # OCR may split "MRN: 12345" across items, so join everything with a space.
            full_text = " ".join(item["text"] for item in items)
            # Pattern: MRN optionally followed by colon/space(s), then digits (5-12 digits)
            match = re.search(r'MRN[:\s]*(\d{5,12})', full_text, re.IGNORECASE)

            def finish():
                self._ocr_in_progress = False
                if match:
                    mrn_number = match.group(1)
                    pb = AppKit.NSPasteboard.generalPasteboard()
                    pb.clearContents()
                    pb.setString_forType_(mrn_number, AppKit.NSPasteboardTypeString)
                    self.command_bar.set_status(f"MRN copied: {mrn_number}")
                else:
                    self.command_bar.set_status("MRN not found on screen")
                if self._macro_wait_reason == "find-mrn":
                    self._macro_step_complete()

            run_on_main(finish)

        threading.Thread(target=task, daemon=True).start()

    def _handle_find(self, query):
        self._sync_active_screen_to_command_bar(announce=False)
        # Strip surrounding quotes if present
        if len(query) >= 2 and query[0] == '"' and query[-1] == '"':
            query = query[1:-1]
        if not query:
            self.command_bar.set_status("Missing search text")
            return
        if self._ocr_in_progress:
            self._pending_find_query = query
            self.command_bar.set_status("Running OCR...")
            return
        self._pending_find_query = query
        self._handle_capture()

    def _normalize_find_text(self, text):
        return re.sub(r"\s+", " ", (text or "").strip()).lower()

    def _is_find_noise_text(self, text):
        norm = self._normalize_find_text(text)
        if not norm:
            return True
        if norm.startswith("debug ") or norm.startswith("macro step:"):
            return True
        markers = (
            "_run_find",
            "_smart_click_after_find",
            "ocr_items=",
            "matches=",
            "query='",
            'query="',
            "click_count=",
            "queue=",
            "remaining)",
            "dispatching",
        )
        return any(marker in norm for marker in markers)

    def _find_match_priority(self, text, query):
        norm_text = self._normalize_find_text(text)
        norm_query = self._normalize_find_text(query)
        exact_penalty = 0 if norm_text == norm_query else 1
        prefix_penalty = 0 if norm_text.startswith(norm_query) else 1
        whole_penalty = 0 if re.search(rf"(^|[^a-z0-9]){re.escape(norm_query)}([^a-z0-9]|$)", norm_text) else 1
        length_penalty = max(0, len(norm_text) - len(norm_query))
        return (exact_penalty, prefix_penalty, whole_penalty, length_penalty)

    def _run_find(self, query):
        if not query:
            self.command_bar.set_status("Missing search text")
            return

        matches = []
        ns_query = Foundation.NSString.stringWithString_(query)
        for item in self.ocr_items:
            text = item["text"]
            if self._is_find_noise_text(text):
                continue
            ns_full = Foundation.NSString.stringWithString_(text)
            search_range = Foundation.NSMakeRange(0, ns_full.length())
            while True:
                found = ns_full.rangeOfString_options_range_(
                    ns_query, Foundation.NSCaseInsensitiveSearch, search_range
                )
                if found.location == Foundation.NSNotFound:
                    break
                bbox = self._bbox_for_text_range(item, found)
                if bbox is None:
                    bbox = item["bbox"]
                matches.append({
                    "text": text,
                    "bbox": bbox,
                    "query": query,
                    "find_priority": self._find_match_priority(text, query),
                })
                next_location = found.location + max(found.length, 1)
                if next_location >= ns_full.length():
                    break
                search_range = Foundation.NSMakeRange(
                    next_location, ns_full.length() - next_location
                )
        matches = self._order_matches_by_anchor(matches)
        passive_wait = self._macro_wait_reason == "find-wait"
        if passive_wait:
            self.matches = []
            self.overlay.clear()
        else:
            self.matches = matches
            self.overlay.show_matches(matches, self.screen_height)
        print(f"DEBUG _run_find: query_len={len(query)}, ocr_items={len(self.ocr_items)}, matches={len(matches)}")
        if passive_wait:
            self.command_bar.set_status(
                "Ready" if matches else "Waiting for text..."
            )
        else:
            self.command_bar.set_status(f"Found {len(matches)} matches")
        if self._macro_wait_reason == "find-wait":
            if matches:
                self._macro_step_complete()
            else:
                self._find_wait_attempts += 1
                if self._find_wait_attempts >= self._find_wait_max:
                    self._abort_macro("find-wait timeout: text not found")
                else:
                    print(f"DEBUG find-wait: no match, retry {self._find_wait_attempts}/{self._find_wait_max}")
                    self.command_bar.set_status("Waiting for text...")
                    def _retry():
                        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                            1.0, self, "findWaitRetryFired:", None, False
                        )
                    run_on_main(_retry)
        elif self._macro_wait_reason == "find":
            self._macro_step_complete()
        elif self._macro_wait_reason == "smart-click":
            self._smart_click_after_find()

    def _bbox_for_text_range(self, item, text_range):
        vn_text = item.get("vn_text")
        if vn_text is None:
            return None
        if self.capture_width_px is None or self.capture_height_px is None:
            return None
        rect_obs, error = vn_text.boundingBoxForRange_error_(text_range, None)
        if error is not None or rect_obs is None:
            return None
        try:
            rect = rect_obs.boundingBox()
        except AttributeError:
            rect = rect_obs
        if rect is None:
            return None
        width_px = self.capture_width_px
        height_px = self.capture_height_px
        scale = self.capture_scale or 1.0
        x_px = rect.origin.x * width_px
        y_px = rect.origin.y * height_px
        w_px = rect.size.width * width_px
        h_px = rect.size.height * height_px
        x_pt = x_px / scale
        y_top_pt = (height_px - (y_px + h_px)) / scale
        w_pt = w_px / scale
        h_pt = h_px / scale
        return (x_pt, y_top_pt, w_pt, h_pt)

    def _handle_click(self, value, record=True, button="left"):
        if value is None or value == "":
            self.command_bar.set_status("Invalid selection")
            return
        try:
            index = int(value)
        except ValueError:
            self.command_bar.set_status("Invalid selection")
            return
        if index < 1 or index > len(self.matches):
            if self._macro_running:
                self._abort_macro(f"click {index}: no match (only {len(self.matches)} found)")
            else:
                self.command_bar.set_status("Invalid selection")
            return

        match = self.matches[index - 1]
        bbox = match["bbox"]
        x, y, w, h = bbox
        cx = x + (w / 2.0)
        cy = y + (h / 2.0)

        print(f"DEBUG _handle_click: record={record}, _recording_name={self._recording_name}")
        if record and self._recording_name is not None:
            # Smart-click recording: capture query + normalized coordinates
            print(f"DEBUG: calling _record_smart_click")
            self._record_smart_click(match, cx, cy, button)
        elif record:
            # Not in recording mode, use legacy format (for non-recording clicks)
            name = "click" if button == "left" else "rclick"
            self._record_step(f"{name} {index}")

        self.last_click_point = (cx, cy)
        self.overlay.clear()
        self.matches = []
        self.command_bar.hide()
        self._click_at(cx, cy, button=button)
        if self._macro_running:
            self._macro_step_complete()

    def _record_smart_click(self, match, cx, cy, button="left"):
        """Record a smart-click step with query text and normalized coordinates."""
        print(f"DEBUG _record_smart_click: match={match.get('query', match.get('text', ''))}")
        # Insert wait step if needed
        last_time = getattr(self, "_recording_last_action_time", None)
        if last_time is not None:
            elapsed = time.time() - last_time
            if elapsed > 0.5:
                # Round to 0.5s increments, cap at 10s
                wait_time = min(10.0, round(elapsed * 2) / 2)
                self._recording_steps.append(f"wait {wait_time:.1f}")

        # Get query text from match (fallback to full text)
        query = match.get("query", match.get("text", ""))

        # Calculate normalized coordinates (percentage of screen)
        screen_w = self.screen_frame.size.width
        screen_h = self.screen_frame.size.height
        x_pct = cx / screen_w if screen_w > 0 else 0
        y_pct = cy / screen_h if screen_h > 0 else 0

        # Escape quotes in query for storage
        escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')

        # Determine command prefix based on button
        cmd = "smart-click" if button == "left" else "smart-rclick"
        step = f'{cmd} "{escaped_query}" {x_pct:.4f} {y_pct:.4f}'
        self._recording_steps.append(step)

        # Update last action time
        self._recording_last_action_time = time.time()

    def _capture_click_region(self, cx, cy, radius_pt=80):
        """Capture a small region around (cx, cy) in points. Returns grayscale numpy array or None."""
        try:
            display_id = self._active_display_id
            scale = Quartz.CGDisplayBounds(display_id).size.width / self.screen_frame.size.width
            ox, oy = getattr(self, "capture_origin_pt", (0.0, 0.0))
            # Build pixel rect centered on click point
            px = (ox + cx - radius_pt) * scale
            py = (oy + cy - radius_pt) * scale
            pw = radius_pt * 2 * scale
            ph = radius_pt * 2 * scale
            rect = Quartz.CGRectMake(px, py, pw, ph)
            img = Quartz.CGDisplayCreateImageForRect(display_id, rect)
            if img is None:
                return None
            w = Quartz.CGImageGetWidth(img)
            h = Quartz.CGImageGetHeight(img)
            bpr = Quartz.CGImageGetBytesPerRow(img)
            data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
            arr = np.frombuffer(data, dtype=np.uint8).reshape((h, bpr // 4, 4))
            return arr[:, :w, :3].mean(axis=2).astype(np.float32)
        except Exception as e:
            print(f"DEBUG _capture_click_region: failed: {e}")
            return None

    def clickVerifyTimerFired_(self, timer):
        """After 0.5s, check if click registered; retry once if screen unchanged."""
        if self._macro_wait_reason != "click-verify":
            return
        cx = getattr(self, "_click_verify_x", None)
        cy = getattr(self, "_click_verify_y", None)
        button = getattr(self, "_click_verify_button", "left")
        pre = getattr(self, "_click_verify_pre", None)
        self._click_verify_pre = None

        if cx is not None and pre is not None:
            post = self._capture_click_region(cx, cy)
            if post is not None and post.shape == pre.shape:
                diff = float(np.mean(np.abs(post - pre)))
                print(f"DEBUG clickVerify: diff={diff:.2f}")
                if diff < 2.0:
                    print(f"DEBUG clickVerify: no change detected, retrying click at ({cx:.1f}, {cy:.1f})")
                    self._click_at(cx, cy, button=button)
                else:
                    print(f"DEBUG clickVerify: change detected, click registered")
            else:
                print(f"DEBUG clickVerify: could not compare regions")

        self._macro_step_complete()

    def _click_at(self, x, y, button="left", click_count=1):
        # `x,y` are in points relative to the *active screen*.
        # Quartz mouse events expect global display coordinates.
        ox, oy = getattr(self, "capture_origin_pt", (0.0, 0.0))
        point = Quartz.CGPointMake(ox + x, oy + y)

        # Move mouse first so the target app registers the cursor position
        move_event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)
        time.sleep(0.05)

        if button == "right":
            down_type = Quartz.kCGEventRightMouseDown
            up_type = Quartz.kCGEventRightMouseUp
            mouse_button = Quartz.kCGMouseButtonRight
        else:
            down_type = Quartz.kCGEventLeftMouseDown
            up_type = Quartz.kCGEventLeftMouseUp
            mouse_button = Quartz.kCGMouseButtonLeft

        # For double-click, we need to send two click pairs with incrementing click count
        for i in range(1, click_count + 1):
            event_down = Quartz.CGEventCreateMouseEvent(None, down_type, point, mouse_button)
            event_up = Quartz.CGEventCreateMouseEvent(None, up_type, point, mouse_button)
            # Set the click state (1 for single, 2 for double, etc.)
            Quartz.CGEventSetIntegerValueField(event_down, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventSetIntegerValueField(event_up, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)
            time.sleep(0.01)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_up)


class AppDelegate(AppKit.NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self.controller = AppController.alloc().init()


def main():
    app = AppKit.NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    app.run()


if __name__ == "__main__":
    main()
