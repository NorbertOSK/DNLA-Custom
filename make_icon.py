#!/usr/bin/env python3
"""Generate assets/icon.icns for the DNLA Custom app.

Draws the TV emoji on a rounded dark tile (macOS icon style) at 1024px,
then produces the .icns via sips + iconutil. Run with the build venv's
Python (needs PyObjC): .venv/bin/python make_icon.py
"""

import os
import shutil
import subprocess
import sys

from AppKit import (
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSImage,
    NSMakeRect,
    NSAttributedString,
)

BASE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(BASE, "assets")
ICONSET = os.path.join(ASSETS, "icon.iconset")
MASTER = os.path.join(ASSETS, "icon_1024.png")


def render_master():
    image = NSImage.alloc().initWithSize_((1024, 1024))
    image.lockFocus()
    # Rounded tile with the standard macOS icon margin (~10%).
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.11, 0.17, 1.0).set()
    tile = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(100, 100, 824, 824), 185, 185)
    tile.fill()
    text = NSAttributedString.alloc().initWithString_attributes_(
        "📺", {NSFontAttributeName: NSFont.systemFontOfSize_(540)})
    size = text.size()
    text.drawAtPoint_(((1024 - size.width) / 2, (1024 - size.height) / 2))
    image.unlockFocus()

    rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    if not png.writeToFile_atomically_(MASTER, True):
        sys.exit("could not write master PNG")


def build_icns():
    shutil.rmtree(ICONSET, ignore_errors=True)
    os.makedirs(ICONSET)
    for points in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = points * scale
            suffix = "" if scale == 1 else "@2x"
            out = os.path.join(ICONSET, f"icon_{points}x{points}{suffix}.png")
            subprocess.run(
                ["sips", "-z", str(pixels), str(pixels), MASTER, "--out", out],
                check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["iconutil", "-c", "icns", ICONSET,
         "-o", os.path.join(ASSETS, "icon.icns")],
        check=True)
    shutil.rmtree(ICONSET)


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)
    render_master()
    build_icns()
    print(f"written: {os.path.join(ASSETS, 'icon.icns')}")
