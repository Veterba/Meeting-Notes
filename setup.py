"""py2app build: python setup.py py2app   (build.sh is the friendlier way in)"""
from setuptools import setup

APP = ["menubar.py"]

DATA_FILES = [
    ("ui", ["ui/index.html"]),
    ("assets", ["assets/icon.png", "assets/icon-paused.png"]),
    ("prompts", ["prompts/map.md", "prompts/reduce.md"]),
    ("templates", ["templates/email.html"]),
    ("config", ["config/people.yaml"]),
    ("", [".env.example"]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/AppIcon.icns",
    "plist": {
        "CFBundleName": "Meeting Notes",
        "CFBundleDisplayName": "Meeting Notes",
        "CFBundleIdentifier": "local.meetingnotes.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        # No Dock icon: the menu bar item is the whole app.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # The UI is served from 127.0.0.1, which needs an ATS exception.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSAppleEventsUsageDescription":
            "Meeting Notes creates the email draft in Outlook for you.",
    },
    # Traced automatically, but these are imported lazily or by name.
    "includes": ["paths", "parser", "llm", "mailer", "main", "server", "window",
                 "history", "menu",
                 "WebKit", "AppKit", "Foundation", "objc", "PyObjCTools.AppHelper",
                 # jaraco is a namespace package, so py2app cannot take it whole.
                 "jaraco.classes.properties", "jaraco.context", "jaraco.functools"],
    "packages": ["rumps", "watchdog", "jinja2", "dotenv", "docx", "yaml", "requests",
                 "certifi", "urllib3", "charset_normalizer", "idna",
                 # keyring finds its backends through entry points, which do not
                 # survive the bundle — mailer picks the macOS one by hand.
                 "keyring", "importlib_metadata"],
    # keyring ships test helpers that import pytest, which drags in numpy and
    # pygments: 70 MB of nothing, and --deep signing then fails on numpy's dylibs.
    "excludes": ["keyring.testing", "pytest", "_pytest", "numpy", "pygments"],
}

setup(app=APP, data_files=DATA_FILES, options={"py2app": OPTIONS},
      setup_requires=["py2app"])
