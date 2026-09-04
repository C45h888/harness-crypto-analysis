"""Market-stage inference (Wyckoff-style) over a 4h window.

Lifted from legacy ``long_term_flow.py`` stage-inference block. Consumes
quantitative inputs (price/OI change, up-bar volume %, keystone migration
direction, institutional cluster composition, funding) and maps a score to
ACCUMULATION / MARKUP / DISTRIBUTION / MARKDOWN / TRANSITION.

Pure function; deterministic and bias-free.
"""

from __future__ import annotations


def vol_direction_split(klines: list[list]) -> tuple[float, float, float]:
    """Split kline volume into up-bar vs down-bar and the up-bar percentage."""
    up_vol = down_vol = 0.0
    for k in klines:
        o, c, v = float(k[1]), float(k[4]), float(k[5])
        if c > o:
            up_vol += v
        elif c < o:
            down_vol += v
    total = up_vol + down_vol
    return up_vol, down_vol, (up_vol / total * 100 if total > 0 else 0.0)


def infer_stage(
    px_chg_4h: float,
    oi_chg_4h: float,
    up_pct_4h: float,
    up_steps: int,
    down_steps: int,
    funding_bps: float | None = None,
    top_long_pct: float | None = None,
    global_long_pct: float | None = None,
    distribution_clusters: float = 0.0,
    absorption_clusters: float = 0.0,
) -> dict:
    """Map 4h quantitative inputs to a market stage + confidence + score.

    Legacy source: long_term_flow stage-scoring block. Returns
    {score, reasons, stage, confidence}.
    """
    score = 0
    reasons: list[str] = []

    if abs(px_chg_4h) < 1.0:
        score += 1
        reasons.append("Tight 4h range (<1%) — accumulation signature")
    if 50 < up_pct_4h < 70:
        score += 1
        reasons.append("Balanced volume direction (50-70% up-bars) — neutral")

    if px_chg_4h > 1.0 and oi_chg_4h > 0.5:
        score += 2
        reasons.append("Price + OI rising — markup (longs entering)")
    if up_steps > down_steps:
        score += 1
        reasons.append("Keystone migrating UP — buyers advancing")

    if px_chg_4h < -0.5 and abs(oi_chg_4h) < 0.5:
        score += 2
        reasons.append("Price DOWN but OI flat — distribution (longs exiting)")
    if down_steps > up_steps:
        score += 2
        reasons.append("Keystone migrating DOWN — buyers retreating")
    if up_pct_4h < 50:
        score += 1
        reasons.append("More volume on DOWN-bars — sellers dominant")
    if distribution_clusters > absorption_clusters * 2:
        score += 2
        reasons.append("DISTRIBUTION clusters 2x ABSORPTION — institutional supply")

    if px_chg_4h < -1.0 and oi_chg_4h > 0.5:
        score += 2
        reasons.append("Price DOWN + OI UP — markdown (fresh shorts entering)")

    joined = "\n".join(reasons)
    if score >= 4 and "Keystone migrating DOWN" in joined and "Price DOWN but OI flat" in joined:
        stage, conf = "DISTRIBUTION (late stage — high conviction)", "HIGH"
    elif score >= 3 and "Keystone migrating DOWN" in joined:
        stage, conf = "DISTRIBUTION (early/mid stage)", "MEDIUM"
    elif score >= 3 and "Price + OI rising" in joined:
        stage, conf = "MARKUP", "MEDIUM"
    elif score >= 3 and "Price DOWN + OI UP" in joined:
        stage, conf = "MARKDOWN", "MEDIUM"
    elif score <= 2 and abs(px_chg_4h) < 1.0:
        stage, conf = "ACCUMULATION", "LOW-MEDIUM"
    else:
        stage, conf = "TRANSITION / UNCLEAR", "LOW"

    return {
        "score": score,
        "reasons": reasons,
        "stage": stage,
        "confidence": conf,
        "inputs": {"px_chg_4h": px_chg_4h, "oi_chg_4h": oi_chg_4h,
                   "up_bar_vol_pct": up_pct_4h, "keystone_up": up_steps,
                   "keystone_down": down_steps, "funding_bps": funding_bps,
                   "top_long_pct": top_long_pct, "global_long_pct": global_long_pct},
    }
