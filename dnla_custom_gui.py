#!/usr/bin/env python3
"""DNLA Custom — native macOS GUI for the dlnacast DLNA renderer.

Shows a small window with Start / Stop / Quit buttons. Start begins
announcing this Mac as a DLNA renderer (same as running ./dlnacast in a
terminal); Stop withdraws it and closes VLC; Quit exits the app.

Run with --nogui to start casting headless (used for smoke-testing the
packaged binary).
"""

import sys
import time

from dlnacast import RendererService, VERSION


def run_headless():
    service = RendererService()
    rt = service.start()
    print(f"headless: {rt.name} at http://{rt.ip}:{rt.http_port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


def run_gui():
    import objc
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSButton,
        NSFont,
        NSMakeRect,
        NSObject,
        NSTextField,
        NSWindow,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
    )
    from PyObjCTools import AppHelper

    service = RendererService()

    class Controller(NSObject):
        def initWithWindow_(self, window):
            self = objc.super(Controller, self).init()
            if self is None:
                return None
            self.window = window
            return self

        def setFields_(self, fields):
            self.status_field, self.hint_field, self.start_btn, self.stop_btn = fields

        def startPressed_(self, sender):
            try:
                rt = service.start()
            except RuntimeError as exc:
                self.showError_(str(exc))
                return
            self.status_field.setStringValue_(f"● Casting as \"{rt.name}\"")
            self.hint_field.setStringValue_(
                f"http://{rt.ip}:{rt.http_port} — pick this device "
                "in your phone's cast menu")
            self.start_btn.setEnabled_(False)
            self.stop_btn.setEnabled_(True)

        def stopPressed_(self, sender):
            service.stop()
            self.status_field.setStringValue_("○ Stopped")
            self.hint_field.setStringValue_("Press Start to begin casting")
            self.start_btn.setEnabled_(True)
            self.stop_btn.setEnabled_(False)

        def quitPressed_(self, sender):
            service.stop()
            NSApplication.sharedApplication().terminate_(None)

        def windowWillClose_(self, notification):
            service.stop()
            NSApplication.sharedApplication().terminate_(None)

        def showError_(self, message):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("DNLA Custom")
            alert.setInformativeText_(message)
            alert.runModal()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskMiniaturizable
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 440, 180), style, NSBackingStoreBuffered, False)
    window.setTitle_(f"DNLA Custom {VERSION}")
    window.center()

    controller = Controller.alloc().initWithWindow_(window)
    window.setDelegate_(controller)
    content = window.contentView()

    status = NSTextField.labelWithString_("○ Stopped")
    status.setFont_(NSFont.boldSystemFontOfSize_(16))
    status.setFrame_(NSMakeRect(20, 120, 400, 26))
    content.addSubview_(status)

    hint = NSTextField.labelWithString_("Press Start to begin casting")
    hint.setFont_(NSFont.systemFontOfSize_(12))
    hint.setFrame_(NSMakeRect(20, 90, 400, 20))
    content.addSubview_(hint)

    def button(title, x, action):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, 24, 120, 34))
        btn.setTitle_(title)
        btn.setBezelStyle_(1)  # NSBezelStyleRounded
        btn.setTarget_(controller)
        btn.setAction_(action)
        content.addSubview_(btn)
        return btn

    start_btn = button("Start", 20, "startPressed:")
    stop_btn = button("Stop", 160, "stopPressed:")
    quit_btn = button("Quit", 300, "quitPressed:")
    stop_btn.setEnabled_(False)

    controller.setFields_((status, hint, start_btn, stop_btn))

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()


def main():
    if "--nogui" in sys.argv:
        run_headless()
    else:
        run_gui()


if __name__ == "__main__":
    main()
