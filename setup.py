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
                 "WebKit", "AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"],
    "packages": ["rumps", "watchdog", "jinja2", "dotenv", "docx", "yaml", "requests",
                 "certifi", "urllib3", "charset_normalizer", "idna"],
}

setup(app=APP, data_files=DATA_FILES, options={"py2app": OPTIONS},
      setup_requires=["py2app"])
