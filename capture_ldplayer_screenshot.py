from __future__ import annotations

import argparse
import io
import os
import subprocess
import time
from pathlib import Path

from PIL import Image

from ldplayer_backend import ensure_ldplayer, ld_screenshot_bytes, serial_for_index

KS_HOME = Path(
    os.environ.get("KS_HOME")
    or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "KingdomServices")
)


def hidden_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def capture_png_for_slot(slot: int, *, attempts: int = 3) -> bytes:
    slot = max(0, int(slot))
    ensure_ldplayer(slot, launch_game=False)
    last_error = ""
    for _ in range(max(1, int(attempts))):
        try:
            data = ld_screenshot_bytes(slot)
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                if im.width < 100 or im.height < 100:
                    raise RuntimeError(
                        f"Screenshot dimensions look invalid: {im.width}x{im.height}"
                    )
            return data
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(
        f"Could not capture a valid screenshot from LDPlayer index {slot}: "
        f"{last_error or 'unknown error'}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="RoK LDPlayer screenshot")
    p.add_argument("--slot", type=int, default=0)
    p.add_argument("--output", default="")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    slot = max(0, int(args.slot))
    target = serial_for_index(slot)
    output = Path(args.output or f"gem_bot_slot_{slot}.png").resolve()

    print(f"[SCREENSHOT] LDPlayer target: {target}", flush=True)

    try:
        data = capture_png_for_slot(slot, attempts=3)
    except Exception as exc:
        print(f"[ERROR] {exc}", flush=True)
        return 1

    # Only write the file AFTER the PNG has fully passed validation.
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_bytes(data)

    # Re-open the just-written temp file once more before replacing any older,
    # known-good screenshot.
    try:
        with Image.open(tmp) as im:
            im.load()
            size = im.size
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"[ERROR] Captured image failed disk validation: {exc}", flush=True)
        return 1

    tmp.replace(output)
    print(
        f"[PASS] Saved valid PNG: {output} "
        f"({size[0]}x{size[1]}, {len(data):,} bytes)",
        flush=True,
    )

    if not args.no_open and os.name == "nt":
        try:
            os.startfile(str(output))
        except Exception as exc:
            print(f"[WARN] Image saved but Windows could not open it: {exc}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
