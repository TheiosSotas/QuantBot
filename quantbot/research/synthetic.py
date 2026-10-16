"""Deterministic software-verification fixture; never historical market evidence."""

import math

from .data import dataset_from_rows


def demo_datasets(*, warmup=600, hours=12):
    if type(warmup) is not int or warmup < 600 or type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError("Demo requires at least 600 warmup hours and 1..168 evaluation hours")
    start = (1_700_000_000_000 // 3_600_000) * 3_600_000
    hourly, minute = [], []

    def price(t):
        h = t / 60
        return 100 + 0.02 * h + 1.8 * math.sin(h * 0.55) + 0.6 * math.sin(t * 0.1)

    for hour in range(warmup + hours):
        children = []
        for step in range(60):
            t = hour * 60 + step
            opening, closing = price(t), price(t + 1)
            children.append(
                [
                    start + t * 60_000,
                    opening,
                    max(opening, closing) + 0.05,
                    min(opening, closing) - 0.05,
                    closing,
                    1 + 0.1 * (t % 7),
                ]
            )
        hourly.append(
            [
                children[0][0],
                children[0][1],
                max(row[2] for row in children),
                min(row[3] for row in children),
                children[-1][4],
                sum(row[5] for row in children),
            ]
        )
        if hour >= warmup:
            minute.extend(children)
    source = {
        "type": "SYNTHETIC",
        "generator": "quantbot-demo-v1",
        "purpose": "software verification only; not historical market or profitability evidence",
    }
    return (
        dataset_from_rows(hourly, source=source),
        dataset_from_rows(minute, interval_ms=60_000, source=source),
    )
