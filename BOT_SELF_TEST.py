from __future__ import annotations

import io
import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fast_gem_ui import FastGemUI
from search_brain import SearchBrain
from random_map_search import SearchPoint


def main() -> int:
    print("Kingdom Services GemOps 3.4 built-in finder self-test")
    ui = FastGemUI()
    if not ui.available:
        raise RuntimeError("OpenCV is unavailable")

    required = {
        "leave_city_map", "leave_city_castle", "gem_node", "gather_button", "new_troop_button",
        "march_button", "march_button_v2", "new_troop_screen_title", "occupied_axe_badge", "new_troop_button_v2",
    }
    missing = sorted(required - set(ui.templates))
    if missing:
        raise RuntimeError("Missing templates: " + ", ".join(missing))
    print(f"[PASS] {len(ui.templates)} local UI templates loaded")

    # Self-match each core template on a padded screen.
    for name in sorted(required):
        p = ROOT / "gem_templates" / f"{name}.png"
        ref = Image.open(p).convert("RGB")
        canvas = Image.new("RGB", (max(640, ref.width + 300), max(400, ref.height + 220)), (105, 115, 86))
        canvas.paste(ref, (140, 100))
        b = io.BytesIO(); canvas.save(b, "PNG")
        threshold = 0.74 if name == "occupied_axe_badge" else 0.82
        if name == "occupied_axe_badge":
            # Badge is normally checked via edge score, not gem template search.
            continue
        hit = ui.find(b.getvalue(), name, threshold=threshold)
        if hit is None:
            raise RuntimeError(f"Template self-test failed: {name}")
    print("[PASS] local template matcher")

    # Simulate the new finder UI: many small gem piles in one row.
    gem_selector_ref = Image.open(
        ROOT / "gem_templates" / "gem_node.png"
    ).convert("RGB")
    selector = Image.new("RGB", (800, 320), (235, 224, 182))
    tiny_w = max(24, int(gem_selector_ref.width * 0.18))
    tiny_h = max(20, int(gem_selector_ref.height * 0.18))
    tiny = gem_selector_ref.resize(
        (tiny_w, tiny_h),
        Image.Resampling.LANCZOS,
    )

    for i in range(6):
        selector.paste(
            tiny,
            (95 + i * 105, 210),
        )

    b = io.BytesIO()
    selector.save(b, "PNG")

    choices = ui.find_gem_selector_icons(
        b.getvalue(),
        min_icons=3,
        max_results=10,
    )

    if len(choices) != 6:
        raise RuntimeError(
            f"Built-in gem selector row failed: {len(choices)} choices"
        )

    print(
        f"[PASS] multi-gem finder row choices={len(choices)}"
    )

    # Multi-signal free-gem detector.
    gem_ref = Image.open(ROOT / "gem_templates" / "gem_node.png").convert("RGB")
    canvas = Image.new("RGB", (760, 520), (145, 153, 83))
    canvas.paste(gem_ref, (250, 150))
    b = io.BytesIO(); canvas.save(b, "PNG")
    gem_png = b.getvalue()
    hit = ui.find(gem_png, "gem_node", threshold=0.90)
    if hit is None:
        raise RuntimeError("Could not locate known free gem reference")
    analysis = ui.classify_gem_candidate(gem_png, hit, mode="strict")
    if analysis["verdict"] not in {"accept", "borderline"}:
        raise RuntimeError(f"Known gem rejected: {analysis}")
    print(
        "[PASS] free-gem multi-signal detector "
        f"conf={analysis['confidence']:.3f} "
        f"red={analysis['red_match']:.3f} "
        f"color={analysis['hist']:.3f} "
        f"edge={analysis['edge']:.3f}"
    )

    # Obvious red rectangle should not pass strict multi-signal validation even
    # when we deliberately give it a misleadingly decent template score.
    fake = Image.new("RGB", (760, 520), (135, 150, 84))
    from PIL import ImageDraw
    d = ImageDraw.Draw(fake)
    d.rounded_rectangle((280, 170, 445, 330), radius=18, fill=(205, 28, 35))
    b = io.BytesIO(); fake.save(b, "PNG")
    fake_png = b.getvalue()
    fake_hit = {
        "x": 280, "y": 170, "w": 165, "h": 160,
        "cx": 362, "cy": 250, "score": 0.67,
    }
    fake_analysis = ui.classify_gem_candidate(
        fake_png,
        fake_hit,
        mode="strict",
    )
    if fake_analysis["verdict"] not in {"reject", "occupied"}:
        raise RuntimeError(
            f"False-positive red object passed strict detector: {fake_analysis}"
        )
    print(
        "[PASS] synthetic red false-positive rejected "
        f"conf={fake_analysis['confidence']:.3f} "
        f"reason={fake_analysis['reason']}"
    )

    # Search brain must stay bounded and generate coverage.
    brain = SearchBrain("smart", seed=3374)
    point = SearchPoint()
    for i in range(1000):
        d = brain.choose_step(point, 200, elapsed=i / 10, timeout=240)
        point = d.point
        brain.record_position(point)
        if (point.x ** 2 + point.y ** 2) ** 0.5 > 2.00001:
            raise RuntimeError("Search brain escaped radius")
    print(f"[PASS] bounded smart search; coverage={brain.coverage_percent(200):.1f}%")
    print("[PASS] GemOps 3.4 built-in finder core ready")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
