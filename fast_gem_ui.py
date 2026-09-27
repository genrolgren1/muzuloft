from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None


ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "gem_templates"

TEMPLATE_THRESHOLDS = {
    "world_search_button": 0.86,
    "exit_game_notice": 0.94,
    "leave_city_map": 0.72,
    "leave_city_castle": 0.68,
    "tree_action_reference": 0.68,
    "gem_node": 0.57,
    "gather_button": 0.76,
    "new_troop_button": 0.76,
    "new_troop_button_v2": 0.66,
    "march_button": 0.75,
    "march_button_v2": 0.68,
    "new_troop_screen_title": 0.68,
    "march_slot_reference": 0.68,
    "occupied_axe_badge": 0.55,
}

UI_SCALES = (
    0.68, 0.74, 0.80, 0.86, 0.92, 0.97, 1.00, 1.04,
    1.10, 1.18, 1.28, 1.38,
)

# RoK resource nodes change size heavily with world-map zoom.
# The user's live screenshot requires ~0.45, while older references were near 1.0.
GEM_SCALES = (
    0.28, 0.32, 0.36, 0.40, 0.45, 0.50, 0.56, 0.62,
    0.68, 0.74, 0.80, 0.86, 0.92, 0.97, 1.00, 1.06,
    1.14, 1.24, 1.34, 1.45,
)

GEM_SELECTOR_SCALES = (
    0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26,
)


DETECTOR_PROFILES = {
    # Accuracy-first default. Borderline results may be AI verified.
    "strict": {
        "accept": 0.765,
        "borderline": 0.640,
        "min_template": 0.600,
        "min_hist": 0.46,
        "min_red_match": 0.43,
        "min_edge": 0.43,
        "axe_reject": 0.36,
        "green_reject": 0.0052,
    },
    "balanced": {
        "accept": 0.710,
        "borderline": 0.605,
        "min_template": 0.575,
        "min_hist": 0.39,
        "min_red_match": 0.34,
        "min_edge": 0.36,
        "axe_reject": 0.39,
        "green_reject": 0.0060,
    },
    "fast": {
        "accept": 0.650,
        "borderline": 0.575,
        "min_template": 0.555,
        "min_hist": 0.31,
        "min_red_match": 0.26,
        "min_edge": 0.30,
        "axe_reject": 0.42,
        "green_reject": 0.0070,
    },
}


class FastGemUI:
    def __init__(self):
        self.available = cv2 is not None and np is not None
        self.templates: dict[str, Any] = {}
        self.templates_bgr: dict[str, Any] = {}
        self.template_features: dict[str, Any] = {}
        self._frame_bytes = None
        self._frame_bgr = None
        self._frame_gray = None
        self._scaled_templates = {}
        if not self.available:
            return

        for name in TEMPLATE_THRESHOLDS:
            path = TEMPLATE_DIR / f"{name}.png"
            if not path.exists():
                continue

            raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if raw is None:
                continue

            if raw.ndim == 3 and raw.shape[2] == 4:
                bgr = raw[:, :, :3]
            elif raw.ndim == 2:
                bgr = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
            else:
                bgr = raw

            self.templates_bgr[name] = bgr
            self.templates[name] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        if "gem_node" in self.templates_bgr:
            self.template_features["gem_node"] = self._reference_features(
                self.templates_bgr["gem_node"]
            )

    def _screen_bgr(self, png: bytes):
        if not self.available:
            return None
        if png is not self._frame_bytes:
            self._frame_bytes = png
            self._frame_bgr = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            self._frame_gray = None
        return self._frame_bgr

    def _screen_gray(self, png: bytes):
        bgr = self._screen_bgr(png)
        if bgr is None:
            return None
        if self._frame_gray is None:
            self._frame_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return self._frame_gray

    @staticmethod
    def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
        ax1, ay1 = a["x"], a["y"]
        ax2, ay2 = ax1 + a["w"], ay1 + a["h"]
        bx1, by1 = b["x"], b["y"]
        bx2, by2 = bx1 + b["w"], by1 + b["h"]
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        return inter / float(area_a + area_b - inter)

    @staticmethod
    def _red_ratio(bgr) -> float:
        if bgr is None or bgr.size == 0:
            return 0.0
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        lo1 = np.array([0, 95, 65], dtype=np.uint8)
        hi1 = np.array([13, 255, 255], dtype=np.uint8)
        lo2 = np.array([166, 95, 65], dtype=np.uint8)
        hi2 = np.array([179, 255, 255], dtype=np.uint8)
        mask = cv2.bitwise_or(
            cv2.inRange(hsv, lo1, hi1),
            cv2.inRange(hsv, lo2, hi2),
        )
        return float(np.count_nonzero(mask)) / float(mask.size)

    @staticmethod
    def _hs_hist(bgr):
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)
        return hist

    @classmethod
    def _reference_features(cls, bgr) -> dict[str, Any]:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return {
            "red_ratio": cls._red_ratio(bgr),
            "hist": cls._hs_hist(bgr),
            "edge": cv2.Canny(gray, 60, 145),
            "shape": (bgr.shape[1], bgr.shape[0]),
        }

    def find_all(
        self,
        png: bytes,
        name: str,
        *,
        threshold: float | None = None,
        region: tuple[float, float, float, float] | None = None,
        max_results: int = 8,
        nms_iou: float = 0.34,
        scales: tuple[float, ...] | list[float] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.available:
            return []
        template = self.templates.get(name)
        if template is None:
            return []
        screen = self._screen_gray(png)
        if screen is None:
            return []

        sh, sw = screen.shape[:2]
        rx1 = ry1 = 0
        rx2, ry2 = sw, sh
        if region is not None:
            x1, y1, x2, y2 = region
            rx1 = max(0, min(sw - 1, int(sw * x1)))
            ry1 = max(0, min(sh - 1, int(sh * y1)))
            rx2 = max(rx1 + 1, min(sw, int(sw * x2)))
            ry2 = max(ry1 + 1, min(sh, int(sh * y2)))

        haystack = screen[ry1:ry2, rx1:rx2]
        hh, hw = haystack.shape[:2]
        th0, tw0 = template.shape[:2]
        required = float(
            threshold
            if threshold is not None
            else TEMPLATE_THRESHOLDS.get(name, 0.75)
        )
        candidates: list[dict[str, Any]] = []

        red_integral = None
        if name == 'gem_node':
            color = self._screen_bgr(png)[ry1:ry2, rx1:rx2]
            hsv = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)
            red = (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 168)) &
                   (hsv[:, :, 1] >= 90) & (hsv[:, :, 2] >= 65)).astype(np.uint8)
            red_integral = cv2.integral(red)

        active_scales = tuple(scales) if scales else (
            GEM_SCALES if name == "gem_node" else UI_SCALES
        )

        for scale in active_scales:
            tw = max(8, int(tw0 * scale))
            th = max(8, int(th0 * scale))
            if tw > hw or th > hh:
                continue

            key = (name, tw, th)
            resized = self._scaled_templates.get(key)
            if resized is None:
                resized = cv2.resize(template, (tw, th),
                    interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC)
                if len(self._scaled_templates) >= 512:
                    self._scaled_templates.clear()
                self._scaled_templates[key] = resized
            result = cv2.matchTemplate(
                haystack,
                resized,
                cv2.TM_CCOEFF_NORMED,
            )
            if red_integral is not None:
                sums = (red_integral[th:, tw:] - red_integral[:-th, tw:]
                        - red_integral[th:, :-tw] + red_integral[:-th, :-tw])
                result[sums < (tw * th * 0.02)] = -1
            ys, xs = np.where(result >= required)
            if len(xs) == 0:
                continue

            # Bound Python allocation on repetitive/flat backgrounds.
            if len(xs) > 100:
                top = np.argpartition(result[ys, xs], -100)[-100:]
                xs, ys = xs[top], ys[top]
            scored = [(float(result[y, x]), int(x), int(y)) for x, y in zip(xs, ys)]
            scored.sort(reverse=True)

            for score, lx, ly in scored[:100]:
                x, y = rx1 + lx, ry1 + ly
                candidates.append(
                    {
                        "name": name,
                        "score": score,
                        "x": x,
                        "y": y,
                        "w": tw,
                        "h": th,
                        "cx": x + tw // 2,
                        "cy": y + th // 2,
                        "scale": float(scale),
                    }
                )

        candidates.sort(key=lambda item: item["score"], reverse=True)
        kept: list[dict[str, Any]] = []
        for candidate in candidates:
            if any(
                self._iou(candidate, existing) >= nms_iou
                for existing in kept
            ):
                continue
            kept.append(candidate)
            if len(kept) >= max_results:
                break
        return kept

    def find(
        self,
        png: bytes,
        name: str,
        *,
        threshold: float | None = None,
        region: tuple[float, float, float, float] | None = None,
    ) -> dict[str, Any] | None:
        hits = self.find_all(
            png,
            name,
            threshold=threshold,
            region=region,
            max_results=1,
        )
        return hits[0] if hits else None

    def find_gem_selector_icons(
        self,
        png: bytes,
        *,
        min_icons: int = 3,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Detect the horizontal row of small red gem choices."""
        if not self.available:
            return []

        bgr = self._screen_bgr(png)
        if bgr is None:
            return []

        h_img, w_img = bgr.shape[:2]

        hits = self.find_all(
            png,
            "gem_node",
            threshold=0.43,
            region=(0.01, 0.01, 0.99, 0.99),
            max_results=20,
            nms_iou=0.24,
            scales=GEM_SELECTOR_SCALES,
        )

        tolerance = max(12, int(h_img * 0.04))

        def cluster_rows(items):
            rows = []
            for hit in sorted(items, key=lambda x: (x["cy"], x["cx"])):
                placed = False
                for row in rows:
                    avg_y = sum(x["cy"] for x in row) / len(row)
                    if abs(hit["cy"] - avg_y) <= tolerance:
                        row.append(hit)
                        placed = True
                        break
                if not placed:
                    rows.append([hit])
            valid = []
            for row in rows:
                # Multi-scale matches of one pile must not count as extra choices.
                distinct = []
                for hit in sorted(row, key=lambda x: x["score"], reverse=True):
                    if all(abs(hit["cx"] - other["cx"]) > max(18, 0.75 * max(hit["w"], other["w"])) for other in distinct):
                        distinct.append(hit)
                distinct.sort(key=lambda x: x["cx"])
                if len(distinct) < min_icons:
                    continue
                gaps = [b["cx"] - a["cx"] for a, b in zip(distinct, distinct[1:])]
                span = distinct[-1]["cx"] - distinct[0]["cx"]
                if span < w_img * 0.18 or max(gaps) > min(gaps) * 3:
                    continue
                valid.append(distinct)
            valid.sort(
                key=lambda row: (
                    len(row),
                    row[-1]["cx"] - row[0]["cx"],
                ),
                reverse=True,
            )
            return valid

        valid = cluster_rows(hits)
        if valid:
            return valid[0][:max_results]

        # Red-color fallback when the finder art differs from the map reference.
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.bitwise_or(
            cv2.inRange(
                hsv,
                np.array([0, 115, 70], dtype=np.uint8),
                np.array([13, 255, 255], dtype=np.uint8),
            ),
            cv2.inRange(
                hsv,
                np.array([166, 115, 70], dtype=np.uint8),
                np.array([179, 255, 255], dtype=np.uint8),
            ),
        )

        kx = max(3, int(w_img * 0.012)) | 1
        ky = max(3, int(h_img * 0.012)) | 1
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (kx, ky),
        )
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            kernel,
            iterations=2,
        )

        num, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        blobs = []
        min_area = max(24, int(w_img * h_img * 0.00005))
        max_area = max(min_area + 1, int(w_img * h_img * 0.04))

        for idx in range(1, num):
            x, y, w, h, area = stats[idx].tolist()
            if not (min_area <= area <= max_area):
                continue
            if w < 8 or h < 8:
                continue
            aspect = w / max(1.0, float(h))
            if not 0.35 <= aspect <= 2.8:
                continue

            cx, cy = cents[idx].tolist()
            blobs.append(
                {
                    "name": "gem_selector_red",
                    "score": min(1.0, area / max(1.0, w * h)),
                    "x": int(x),
                    "y": int(y),
                    "w": int(w),
                    "h": int(h),
                    "cx": int(cx),
                    "cy": int(cy),
                    "scale": 0.0,
                }
            )

        valid = cluster_rows(blobs)
        return valid[0][:max_results] if valid else []

    def occupied_axe_badge_score(
        self,
        png: bytes,
        gem_hit: dict[str, Any],
        *,
        expand: float = 0.90,
    ) -> float:
        if not self.available:
            return 0.0
        template = self.templates.get("occupied_axe_badge")
        if template is None:
            return 0.0

        bgr = self._screen_bgr(png)
        if bgr is None:
            return 0.0

        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h_img, w_img = gray.shape[:2]
        pad_x = int(gem_hit["w"] * expand)
        pad_y = int(gem_hit["h"] * expand)

        x1 = max(0, int(gem_hit["x"] - pad_x))
        y1 = max(0, int(gem_hit["y"] - pad_y))
        x2 = min(w_img, int(gem_hit["x"] + gem_hit["w"] + pad_x))
        y2 = min(h_img, int(gem_hit["y"] + gem_hit["h"] + pad_y))
        crop = gray[y1:y2, x1:x2]
        if crop.size == 0:
            return 0.0

        crop_edge = cv2.Canny(crop, 65, 145)
        template_edge0 = cv2.Canny(template, 65, 145)
        best = 0.0
        th0, tw0 = template_edge0.shape[:2]
        hh, hw = crop_edge.shape[:2]

        for scale in (0.62, 0.70, 0.78, 0.86, 0.94, 1.00, 1.08, 1.18, 1.30, 1.44, 1.58):
            tw = max(8, int(tw0 * scale))
            th = max(8, int(th0 * scale))
            if tw > hw or th > hh:
                continue
            resized = cv2.resize(
                template_edge0,
                (tw, th),
                interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
            )
            result = cv2.matchTemplate(
                crop_edge,
                resized,
                cv2.TM_CCOEFF_NORMED,
            )
            _, score, _, _ = cv2.minMaxLoc(result)
            best = max(best, float(score))
        return best

    def green_activity_score(
        self,
        png: bytes,
        hit: dict[str, Any],
        *,
        expand: float = 0.45,
    ) -> float:
        if not self.available:
            return 0.0
        bgr = self._screen_bgr(png)
        if bgr is None:
            return 0.0
        h_img, w_img = bgr.shape[:2]
        pad_x = int(hit["w"] * expand)
        pad_y = int(hit["h"] * expand)
        x1 = max(0, int(hit["x"] - pad_x))
        y1 = max(0, int(hit["y"] - pad_y))
        x2 = min(w_img, int(hit["x"] + hit["w"] + pad_x))
        y2 = min(h_img, int(hit["y"] + hit["h"] + pad_y))
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return 0.0
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        lower = np.array([40, 110, 100], dtype=np.uint8)
        upper = np.array([94, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
        return float(np.count_nonzero(mask)) / float(mask.size)

    def gem_candidate_features(
        self,
        png: bytes,
        hit: dict[str, Any],
    ) -> dict[str, float]:
        """
        Multi-signal gem confidence.

        This intentionally does not trust grayscale template confidence alone.
        A candidate must also resemble the user's actual red crystal color and
        edge structure.
        """
        if not self.available:
            return {
                "confidence": 0.0,
                "template": float(hit.get("score") or 0.0),
                "hist": 0.0,
                "red_match": 0.0,
                "edge": 0.0,
                "red_ratio": 0.0,
            }

        screen = self._screen_bgr(png)
        reference = self.templates_bgr.get("gem_node")
        ref_features = self.template_features.get("gem_node")
        if screen is None or reference is None or ref_features is None:
            return {
                "confidence": 0.0,
                "template": float(hit.get("score") or 0.0),
                "hist": 0.0,
                "red_match": 0.0,
                "edge": 0.0,
                "red_ratio": 0.0,
            }

        h_img, w_img = screen.shape[:2]
        x1 = max(0, int(hit["x"]))
        y1 = max(0, int(hit["y"]))
        x2 = min(w_img, int(hit["x"] + hit["w"]))
        y2 = min(h_img, int(hit["y"] + hit["h"]))
        crop = screen[y1:y2, x1:x2]
        if crop.size == 0:
            return {
                "confidence": 0.0,
                "template": float(hit.get("score") or 0.0),
                "hist": 0.0,
                "red_match": 0.0,
                "edge": 0.0,
                "red_ratio": 0.0,
            }

        ref_h, ref_w = reference.shape[:2]
        resized = cv2.resize(
            crop,
            (ref_w, ref_h),
            interpolation=cv2.INTER_AREA
            if crop.shape[1] > ref_w
            else cv2.INTER_CUBIC,
        )

        red_ratio = self._red_ratio(resized)
        ref_red = float(ref_features["red_ratio"])
        red_tolerance = max(0.025, ref_red * 0.95)
        red_match = max(
            0.0,
            min(1.0, 1.0 - abs(red_ratio - ref_red) / red_tolerance),
        )

        hist = self._hs_hist(resized)
        hist_corr = float(
            cv2.compareHist(
                ref_features["hist"],
                hist,
                cv2.HISTCMP_CORREL,
            )
        )
        hist_score = max(0.0, min(1.0, (hist_corr + 1.0) / 2.0))

        edge = cv2.Canny(
            cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY),
            60,
            145,
        )
        ref_edge = ref_features["edge"]
        edge_raw = float(
            cv2.matchTemplate(
                edge,
                ref_edge,
                cv2.TM_CCOEFF_NORMED,
            )[0, 0]
        )
        edge_score = max(0.0, min(1.0, (edge_raw + 1.0) / 2.0))

        template_score = max(
            0.0,
            min(1.0, float(hit.get("score") or 0.0)),
        )

        confidence = (
            template_score * 0.36
            + hist_score * 0.24
            + red_match * 0.24
            + edge_score * 0.16
        )

        return {
            "confidence": float(confidence),
            "template": float(template_score),
            "hist": float(hist_score),
            "red_match": float(red_match),
            "edge": float(edge_score),
            "red_ratio": float(red_ratio),
            "reference_red_ratio": float(ref_red),
        }

    @staticmethod
    def detector_profile(mode: str) -> dict[str, float]:
        return dict(
            DETECTOR_PROFILES.get(
                str(mode or "strict").lower(),
                DETECTOR_PROFILES["strict"],
            )
        )

    def classify_gem_candidate(
        self,
        png: bytes,
        hit: dict[str, Any],
        *,
        mode: str = "strict",
    ) -> dict[str, Any]:
        features = self.gem_candidate_features(png, hit)
        axe = self.occupied_axe_badge_score(png, hit)
        green = self.green_activity_score(png, hit)
        profile = self.detector_profile(mode)

        reasons: list[str] = []

        if axe >= profile["axe_reject"]:
            return {
                **features,
                "axe": axe,
                "green": green,
                "verdict": "occupied",
                "reason": f"axe badge {axe:.3f}",
                "profile": mode,
            }

        # Green terrain is common around FREE nodes. Color alone cannot prove
        # occupancy; retain it as telemetry, require an actual axe badge above.

        if features["template"] < profile["min_template"]:
            reasons.append("low template")
        if features["hist"] < profile["min_hist"]:
            reasons.append("color mismatch")
        if features["red_match"] < profile["min_red_match"]:
            reasons.append("red-crystal mismatch")
        if features["edge"] < profile["min_edge"]:
            reasons.append("shape mismatch")

        if reasons:
            verdict = "reject"
        elif features["confidence"] >= profile["accept"]:
            verdict = "accept"
        elif features["confidence"] >= profile["borderline"]:
            verdict = "borderline"
        else:
            verdict = "reject"
            reasons.append("confidence below threshold")

        return {
            **features,
            "axe": float(axe),
            "green": float(green),
            "verdict": verdict,
            "reason": ", ".join(reasons) if reasons else "multi-signal agreement",
            "profile": mode,
            "accept_threshold": profile["accept"],
            "borderline_threshold": profile["borderline"],
        }

    def crop_candidate(
        self,
        png: bytes,
        hit: dict[str, Any],
        *,
        expand: float = 0.65,
    ) -> bytes:
        """Small candidate crop for fast borderline AI classification."""
        bgr = self._screen_bgr(png)
        if bgr is None:
            return png

        h_img, w_img = bgr.shape[:2]
        pad_x = int(hit["w"] * expand)
        pad_y = int(hit["h"] * expand)
        x1 = max(0, int(hit["x"] - pad_x))
        y1 = max(0, int(hit["y"] - pad_y))
        x2 = min(w_img, int(hit["x"] + hit["w"] + pad_x))
        y2 = min(h_img, int(hit["y"] + hit["h"] + pad_y))
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return png
        ok, encoded = cv2.imencode(".png", crop)
        return encoded.tobytes() if ok else png
