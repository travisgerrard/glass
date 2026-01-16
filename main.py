import json
import os
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
    def __init__(self, screen_size_points):
        self.screen_size_points = screen_size_points

    def capture_primary_display(self):
        display_id = Quartz.CGMainDisplayID()
        bounds = Quartz.CGDisplayBounds(display_id)
        image = Quartz.CGWindowListCreateImage(
            bounds,
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

        width_pts, height_pts = self.screen_size_points
        if width_pts:
            scale = width_px / float(width_pts)
        else:
            scale = 1.0

        return image, width_px, height_px, scale

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


class AppController(AppKit.NSObject):
    def init(self):
        self = objc_super(AppController, self).init()
        if self is None:
            return None

        screen = AppKit.NSScreen.mainScreen()
        self.screen_frame = screen.frame()
        self.screen_height = self.screen_frame.size.height
        self.screen_center = (
            self.screen_frame.size.width / 2.0,
            self.screen_frame.size.height / 2.0,
        )
        self.ocr_engine = ScreenOCR(
            (self.screen_frame.size.width, self.screen_frame.size.height)
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
        self._load_macros()
        self._setup_hotkey()
        return self

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

    def _save_macros(self):
        path = self.macros_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"macros": self.macros}, handle, indent=2)

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
        self.command_bar.set_status(f"Recording {name}")

    def _stop_recording(self):
        if self._recording_name is None:
            self.command_bar.set_status("Not recording")
            return
        name = self._recording_name
        self.macros[name] = list(self._recording_steps)
        self._save_macros()
        count = len(self._recording_steps)
        self._recording_name = None
        self._recording_steps = []
        self.command_bar.set_status(f"Saved macro {name} ({count} steps)")

    def _list_macros(self):
        if not self.macros:
            self.command_bar.set_status("No macros saved")
            self.command_bar.show_help("No macros saved")
            return
        names = sorted(self.macros.keys())
        self.command_bar.set_status(f"Macros ({len(names)})")
        self.command_bar.show_help("\n".join(names))

    def _show_macro(self, name):
        name = self._normalize_macro_name(name)
        if not name:
            self.command_bar.set_status("Missing macro name")
            return
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return
        steps = self.macros.get(name) or []
        self.command_bar.set_status(f"Macro {name}")
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
        display_id = Quartz.CGMainDisplayID()
        region = Quartz.CGRectMake(x, y, w, h)
        image = Quartz.CGWindowListCreateImage(
            region,
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

    def _find_image(self, name):
        """Find a saved image template on screen using template matching."""
        name = self._normalize_macro_name(name)
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
        # Load template
        template = cv2.imread(image_path, cv2.IMREAD_COLOR)
        if template is None:
            self.command_bar.set_status(f"Failed to load image: {name}")
            if self._macro_wait_reason == "find-image":
                self._abort_macro(f"Failed to load image: {name}")
            return
        template_h, template_w = template.shape[:2]
        # Capture screen
        display_id = Quartz.CGMainDisplayID()
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
        width = Quartz.CGImageGetWidth(screen_image)
        height = Quartz.CGImageGetHeight(screen_image)
        bytes_per_row = Quartz.CGImageGetBytesPerRow(screen_image)
        data_provider = Quartz.CGImageGetDataProvider(screen_image)
        data = Quartz.CGDataProviderCopyData(data_provider)
        arr = np.frombuffer(data, dtype=np.uint8)
        arr = arr.reshape((height, bytes_per_row // 4, 4))
        # CGWindowListCreateImage returns BGRA format on macOS (little-endian with alpha first)
        # Extract first 3 channels to get BGR, which matches cv2.imread format
        screen_bgr = arr[:, :width, :3].copy()
        # Template matching
        result = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        threshold = 0.8
        locations = np.where(result >= threshold)
        matches = []
        # Convert to screen coordinates (accounting for Retina scale)
        scale = width / self.screen_frame.size.width
        for pt in zip(*locations[::-1]):  # Switch to (x, y)
            x_pt = pt[0] / scale
            y_pt = pt[1] / scale
            w_pt = template_w / scale
            h_pt = template_h / scale
            matches.append({"text": name, "bbox": (x_pt, y_pt, w_pt, h_pt)})
        # Deduplicate overlapping matches
        filtered = []
        for m in matches:
            x, y, w, h = m["bbox"]
            duplicate = False
            for f in filtered:
                fx, fy, fw, fh = f["bbox"]
                if abs(x - fx) < w * 0.5 and abs(y - fy) < h * 0.5:
                    duplicate = True
                    break
            if not duplicate:
                filtered.append(m)
        matches = self._order_matches_by_anchor(filtered[:9])
        self.matches = matches
        self.overlay.show_matches(matches, self.screen_height)
        self.command_bar.set_status(f"Found {len(matches)} matches")
        if self._macro_running and self._macro_wait_reason == "find-image":
            self._macro_step_complete()

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
        steps = self.macros.get(name) or []
        if not steps:
            self.command_bar.set_status(f"Macro empty: {name}")
            return
        expanded = self._expand_macro(name)
        if expanded is None:
            return
        self._macro_name = name
        self._macro_root = name
        self._macro_queue = expanded
        self._macro_running = True
        self._macro_wait_reason = None
        self.command_bar.set_status(f"Running {name}")
        self._run_next_macro_step()

    def _expand_macro(self, name):
        name = self._normalize_macro_name(name)
        if name not in self.macros:
            self.command_bar.set_status("Macro not found")
            return None
        steps = self.macros.get(name) or []
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
        if name == "capture":
            self._macro_wait_reason = "capture"
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
        elif name == "find-image":
            self._macro_wait_reason = "find-image"
            self._find_image(arg)
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
        if message:
            self.command_bar.set_status(message)

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
            return (dx * dx + dy * dy, y, x)

        return sorted(matches, key=sort_key)

    def _install_key_monitor(self):
        if self._key_monitor is not None:
            return

        def handler(event):
            if not self.command_bar.visible:
                return event
            if not self.matches:
                return event
            if self.command_bar.input_text():
                return event
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags != 0:
                return event
            chars = event.characters()
            if not chars or len(chars) != 1:
                return event
            index, button = self._index_and_button_for_char(chars)
            if index is None:
                return event
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
            self._handle_capture()
        elif name == "find":
            if arg:
                self._record_step(f"find {arg}")
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
        elif name == "show":
            self._show_macro(arg)
        elif name == "delete":
            self._delete_macro(arg)
        elif name == "capture-image":
            self._capture_image(arg)
        elif name == "find-image":
            self._find_image(arg)
        elif name == "images":
            self._list_images()
        elif name == "delete-image":
            self._delete_image(arg)
        elif name == "help":
            self.command_bar.set_status("Commands")
            self.command_bar.show_help(
                "capture  - capture primary screen\n"
                "find <text>  - capture + find text\n"
                "click <number>  - left click match\n"
                "rclick <number>  - right click match\n"
                "clear  - close and reset\n"
                "record <name>  - start recording\n"
                "stop  - save recording\n"
                "run <name>  - run macro\n"
                "macros  - list macros\n"
                "show <name>  - show macro steps\n"
                "delete <name>  - remove macro\n"
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
                self._record_step(f"find {command}")
                self._handle_find(command)

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
                    image, width_px, height_px, scale = (
                        self.ocr_engine.capture_primary_display()
                    )
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

    def _handle_find(self, query):
        if not query:
            self.command_bar.set_status("Missing search text")
            return
        if self._ocr_in_progress:
            self._pending_find_query = query
            self.command_bar.set_status("Running OCR...")
            return
        self._pending_find_query = query
        self._handle_capture()

    def _run_find(self, query):
        if not query:
            self.command_bar.set_status("Missing search text")
            return

        norm_query = query.lower()
        matches = []
        ns_query = Foundation.NSString.stringWithString_(query)
        for item in self.ocr_items:
            text = item["text"]
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
                matches.append({"text": text, "bbox": bbox})
                next_location = found.location + max(found.length, 1)
                if next_location >= ns_full.length():
                    break
                search_range = Foundation.NSMakeRange(
                    next_location, ns_full.length() - next_location
                )
        matches = self._order_matches_by_anchor(matches)
        self.matches = matches
        self.overlay.show_matches(matches, self.screen_height)
        self.command_bar.set_status(f"Found {len(matches)} matches")
        if self._macro_wait_reason == "find":
            self._macro_step_complete()

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
            self.command_bar.set_status("Invalid selection")
            return

        if record:
            name = "click" if button == "left" else "rclick"
            self._record_step(f"{name} {index}")
        bbox = self.matches[index - 1]["bbox"]
        x, y, w, h = bbox
        cx = x + (w / 2.0)
        cy = y + (h / 2.0)
        self._click_at(cx, cy, button=button)
        self.last_click_point = (cx, cy)
        self.overlay.clear()
        self.matches = []
        self.command_bar.hide()

    def _click_at(self, x, y, button="left"):
        point = Quartz.CGPointMake(x, y)
        if button == "right":
            event_down = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventRightMouseDown, point, Quartz.kCGMouseButtonRight
            )
            event_up = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventRightMouseUp, point, Quartz.kCGMouseButtonRight
            )
        else:
            event_down = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft
            )
            event_up = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft
            )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event_down)
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
