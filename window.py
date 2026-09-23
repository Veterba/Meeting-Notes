"""Native macOS window hosting the local UI.

A WKWebView pointed at the loopback server. No browser, no Streamlit — the page
and the API both live inside this process.
"""
from urllib.parse import quote

from AppKit import (NSApp, NSBackingStoreBuffered, NSMakeRect, NSScreen, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration

WIDTH, HEIGHT = 1000, 720
MIN_WIDTH, MIN_HEIGHT = 560, 420


class AppWindow:
    """One reusable window. Closing it hides the window, it never quits the app."""

    def __init__(self, url: str):
        self.url = url
        self.window = None
        self.webview = None

    def _build(self):
        screen = (NSScreen.mainScreen() or NSScreen.screens()[0]).visibleFrame()
        frame = NSMakeRect(
            screen.origin.x + (screen.size.width - WIDTH) / 2,
            screen.origin.y + (screen.size.height - HEIGHT) / 2,
            WIDTH, HEIGHT)

        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Meeting Notes")
        self.window.setMinSize_((MIN_WIDTH, MIN_HEIGHT))
        # Keep the object alive after the red button — we reopen the same window.
        self.window.setReleasedWhenClosed_(False)

        config = WKWebViewConfiguration.alloc().init()
        self.webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), config)
        self.webview.setAutoresizingMask_(1 << 1 | 1 << 4)   # width | height
        self.window.setContentView_(self.webview)

    def show(self, open_path: str = None):
        fresh = self.window is None
        if fresh:
            self._build()
        if fresh or open_path or not self.window.isVisible():
            self.load(open_path)             # reopened: start from fresh state
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def load(self, open_path: str = None):
        url = self.url
        if open_path:
            url += "?open=" + quote(str(open_path), safe="")
        request = NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
        self.webview.loadRequest_(request)
