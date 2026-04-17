"""
Display stretch algorithms for astronomical images.

Each function: ndarray -> (uint8_display, z1_float, z2_float).
"""

import numpy as np


def stretch_minmax(data):
    lo, hi = float(data.min()), float(data.max())
    if hi <= lo:
        return np.zeros(data.shape, np.uint8), lo, hi
    out = np.clip(
        (data.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255
    ).astype(np.uint8)
    return out, lo, hi


def stretch_percent(data, lo_pct=0.5, hi_pct=99.5):
    lo, hi = np.percentile(data, [lo_pct, hi_pct])
    if hi <= lo:
        return stretch_minmax(data)
    out = np.clip(
        (data.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255
    ).astype(np.uint8)
    return out, float(lo), float(hi)


def stretch_zscale(data, nsamples=1000, contrast=0.25):
    """IRAF-style zscale -- good for astronomical data with faint structure."""
    flat = data.ravel()
    stride = max(1, len(flat) // nsamples)
    sample = np.sort(flat[::stride].astype(np.float64))
    ns = len(sample)
    if ns < 10:
        return stretch_minmax(data)

    median = np.median(sample)
    x = np.arange(ns, dtype=np.float64)
    xm = x - x.mean()
    ym = sample - sample.mean()
    denom = (xm * xm).sum()
    slope = (xm * ym).sum() / denom if denom else 0.0

    z1 = max(median - (slope / contrast) * (ns // 2), float(sample[0]))
    z2 = min(median + (slope / contrast) * (ns // 2), float(sample[-1]))
    if z2 <= z1:
        z1, z2 = float(sample[0]), float(sample[-1])
    if z2 <= z1:
        return stretch_minmax(data)

    out = np.clip(
        (data.astype(np.float32) - z1) / (z2 - z1) * 255, 0, 255
    ).astype(np.uint8)
    return out, z1, z2


def stretch_sqrt(data):
    """Square-root stretch -- good for bringing out faint emission."""
    lo, hi = np.percentile(data, [0.5, 99.5])
    if hi <= lo:
        return stretch_minmax(data)
    normed = np.clip((data.astype(np.float32) - lo) / (hi - lo), 0, 1)
    return (np.sqrt(normed) * 255).astype(np.uint8), float(lo), float(hi)


def stretch_log(data):
    """Logarithmic stretch -- good for high dynamic range images."""
    lo, hi = np.percentile(data, [0.5, 99.5])
    if hi <= lo:
        return stretch_minmax(data)
    normed = np.clip((data.astype(np.float32) - lo) / (hi - lo), 0, 1)
    out = (np.log1p(normed * 1000) / np.log1p(1000) * 255).astype(np.uint8)
    return out, float(lo), float(hi)


# Ordered dict for GUI combo box
STRETCH_FUNCS = {
    "99.5%": stretch_percent,
    "MinMax": stretch_minmax,
    "ZScale": stretch_zscale,
    "Sqrt": stretch_sqrt,
    "Log": stretch_log,
}
