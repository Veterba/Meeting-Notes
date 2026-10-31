"""The app window: a native macOS window hosting the local UI.

A WKWebView pointed at the loopback server. No browser — the page and the API
both live inside this process. The page draws its own sky right up to the top
edge: the title bar is transparent over it, and a thin native strip on top of
the page is what drags the window, since a web view keeps its clicks.
"""
import re

from AppKit import (NSApp, NSAppearance, NSBackingStoreBuffered, NSColor, NSMakeRect,
                    NSMenu, NSMenuItem, NSScreen, NSView, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskFullSizeContentView,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWindowTitleHidden)
from Foundation import NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration

WIDTH, HEIGHT = 1180, 780
MIN_WIDTH, MIN_HEIGHT = 820, 520
# The sky's navy, behind the page until it has drawn.
NAVY = (0x0e / 255, 0x10 / 255, 0x30 / 255)
TITLEBAR = 28                       # the page keeps this much free at the top


class DragStrip(NSView):
    """An invisible strip over the top of the page that moves the window."""

    def mouseDownCanMoveWindow(self):
        return True

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            self.window().performZoom_(None)
        else:
            self.window().performWindowDragWithEvent_(event)


class AppWindow:
    """One reusable window. Closing it hides the window, it never quits the app."""

    def __init__(self, url: str):
        self.url = url
        self.window = None
        self.webview = None

    def _build(self):
        screen = (NSScreen.mainScreen() or NSScreen.screens()[0]).visibleFrame()
        width = min(WIDTH, screen.size.width - 40)
        height = min(HEIGHT, screen.size.height - 40)
        frame = NSMakeRect(
            screen.origin.x + (screen.size.width - width) / 2,
            screen.origin.y + (screen.size.height - height) / 2,
            width, height)

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Meeting Notes")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
        self.window.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*NAVY, 1))
        self.window.setMinSize_((MIN_WIDTH, MIN_HEIGHT))
        self.window.setFrameAutosaveName_("MeetingNotesWindow")
        # Keep the object alive after the red button — we reopen the same window.
        self.window.setReleasedWhenClosed_(False)

        config = WKWebViewConfiguration.alloc().init()
        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            self.window.contentView().bounds(), config)
        # No white flash before the page has drawn its sky.
        self.webview.setValue_forKey_(False, "drawsBackground")
        self.webview.setAutoresizingMask_(1 << 1 | 1 << 4)   # width | height
        content = self.window.contentView()
        content.addSubview_(self.webview)

        bounds = content.bounds()
        strip = DragStrip.alloc().initWithFrame_(NSMakeRect(
            0, bounds.size.height - TITLEBAR, bounds.size.width, TITLEBAR))
        strip.setAutoresizingMask_(1 << 1 | 1 << 3)          # width | stay at the top
        content.addSubview_(strip)

    def show(self, meeting: str = None):
        """Bring the window up, on one meeting's notes if one is named by hash."""
        if self.window is None:
            self._build()
            self.load(meeting)
        elif meeting:
            self.open_meeting(meeting)
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def load(self, meeting: str = None):
        url = self.url + (f"?meeting={meeting}" if meeting else "")
        self.webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    def open_meeting(self, meeting: str):
        """Switch the page that is already up to this meeting, without a reload."""
        if not re.fullmatch(r"[0-9a-f]{64}", meeting):
            return
        self.webview.evaluateJavaScript_completionHandler_(
            f"window.openMeeting && window.openMeeting('{meeting}')", None)


def main_menu() -> NSMenu:
    """The menu bar of the app itself, while the window is in front.

    Mostly for the Edit menu: without it ⌘C, ⌘V and ⌘Z do nothing in the
    window's text fields, because WKWebView gets them through the menu.
    """
    def menu(title, items):
        m = NSMenu.alloc().initWithTitle_(title)
        for entry in items:
            if entry is None:
                m.addItem_(NSMenuItem.separatorItem())
                continue
            label, action, key = entry
            m.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, action, key))
        holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        holder.setSubmenu_(m)
        return holder

    bar = NSMenu.alloc().init()
    bar.addItem_(menu("Meeting Notes", [
        ("Hide Meeting Notes", "hide:", "h"),
        None,
        ("Quit Meeting Notes", "terminate:", "q")]))
    bar.addItem_(menu("Edit", [
        ("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), None,
        ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a")]))
    bar.addItem_(menu("Window", [
        ("Minimize", "performMiniaturize:", "m"),
        ("Close", "performClose:", "w")]))
    return bar
