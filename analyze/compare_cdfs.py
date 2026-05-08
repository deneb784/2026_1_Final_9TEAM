from pathlib import Path
from xml.sax.saxutils import escape


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SERIES = [
    ("DCTCP", ROOT_DIR / "TrafficGenerator/conf/DCTCP_CDF.txt", "#1f77b4"),
    ("UNI1", ROOT_DIR / "TrafficGenerator/conf/UNI1_CDF.txt", "#d62728"),
]


def load_cdf(path: Path) -> list[tuple[float, float]]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            value, cdf = line.split()[:2]
            rows.append((float(value), float(cdf)))
    return rows


def nice_number(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000:
        return f"{value / 1_000:g}K"
    return f"{value:g}"


def polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def x_linear(value: float, min_x: float, max_x: float, left: float, width: float) -> float:
    return left + (value - min_x) / (max_x - min_x) * width


def x_log(value: float, min_x: float, max_x: float, left: float, width: float) -> float:
    import math

    value = max(value, min_x)
    return left + (math.log10(value) - math.log10(min_x)) / (math.log10(max_x) - math.log10(min_x)) * width


def y_scale(cdf: float, top: float, height: float) -> float:
    return top + (1.0 - cdf) * height


def draw_panel(
    title: str,
    series: list[tuple[str, list[tuple[float, float]], str]],
    left: float,
    top: float,
    width: float,
    height: float,
    log_x: bool,
    normalize: str | None = None,
) -> list[str]:
    normalized_series = []
    for name, rows, color in series:
        if normalize == "max":
            denom = max(value for value, _ in rows)
        elif normalize == "p99":
            p99_values = [value for value, cdf in rows if cdf >= 0.99]
            denom = p99_values[0] if p99_values else max(value for value, _ in rows)
        else:
            denom = 1.0
        normalized_series.append((name, [(value / denom, cdf) for value, cdf in rows], color))

    series = normalized_series
    all_values = [value for _, rows, _ in series for value, _ in rows if value > 0 or not log_x]
    min_x = 1 if log_x else 0
    if normalize is not None:
        min_x = 0.001 if log_x else 0
    max_x = max(all_values)
    if normalize is not None:
        max_x = 1.0
    x_fn = x_log if log_x else x_linear
    elements = []

    elements.append(f'<text x="{left}" y="{top - 18}" class="title">{escape(title)}</text>')
    elements.append(f'<rect x="{left}" y="{top}" width="{width}" height="{height}" class="plot-bg"/>')
    elements.append(f'<line x1="{left}" y1="{top + height}" x2="{left + width}" y2="{top + height}" class="axis"/>')
    elements.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + height}" class="axis"/>')

    for cdf in [0.0, 0.25, 0.5, 0.75, 1.0]:
        y = y_scale(cdf, top, height)
        elements.append(f'<line x1="{left}" y1="{y}" x2="{left + width}" y2="{y}" class="grid"/>')
        elements.append(f'<text x="{left - 10}" y="{y + 4}" class="tick" text-anchor="end">{cdf:g}</text>')

    if normalize is not None:
        ticks = [0.001, 0.01, 0.1, 1.0] if log_x else [0, 0.25, 0.5, 0.75, 1.0]
    elif log_x:
        ticks = [1, 10, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000, 100_000_000]
        ticks = [tick for tick in ticks if tick <= max_x]
    else:
        ticks = [0, max_x * 0.25, max_x * 0.5, max_x * 0.75, max_x]

    for tick in ticks:
        x = x_fn(tick, min_x, max_x, left, width)
        elements.append(f'<line x1="{x}" y1="{top + height}" x2="{x}" y2="{top + height + 5}" class="axis"/>')
        label = f"{tick:g}" if normalize is not None else nice_number(tick)
        elements.append(f'<text x="{x}" y="{top + height + 22}" class="tick" text-anchor="middle">{label}</text>')

    if normalize == "max":
        x_label = "Normalized flow size, value / max"
    elif normalize == "p99":
        x_label = "Normalized flow size, value / p99"
    else:
        x_label = "Flow size bytes"
    elements.append(f'<text x="{left + width / 2}" y="{top + height + 48}" class="label" text-anchor="middle">{x_label}</text>')
    elements.append(f'<text x="{left - 45}" y="{top + height / 2}" class="label" text-anchor="middle" transform="rotate(-90 {left - 45} {top + height / 2})">CDF</text>')

    for name, rows, color in series:
        points = [(x_fn(value, min_x, max_x, left, width), y_scale(cdf, top, height)) for value, cdf in rows if value > 0 or not log_x]
        elements.append(f'<polyline points="{polyline(points)}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        for value, cdf in rows:
            if log_x and value <= 0:
                continue
            x = x_fn(value, min_x, max_x, left, width)
            y = y_scale(cdf, top, height)
            title_value = f"{value:g}" if normalize is not None else f"{nice_number(value)} bytes"
            elements.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{color}"><title>{escape(name)} {title_value}, cdf={cdf:g}</title></circle>')

    return elements


def build_svg(output_path: Path) -> None:
    series = [(name, load_cdf(path), color) for name, path, color in DEFAULT_SERIES]
    width = 1180
    height = 1440
    elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text { font-family: Arial, sans-serif; fill: #222; }",
        ".title { font-size: 20px; font-weight: 700; }",
        ".label { font-size: 13px; }",
        ".tick { font-size: 11px; fill: #555; }",
        ".axis { stroke: #333; stroke-width: 1; }",
        ".grid { stroke: #ddd; stroke-width: 1; }",
        ".plot-bg { fill: #fff; stroke: #ccc; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="40" y="34" class="title">DCTCP CDF vs UNI1 CDF</text>',
    ]

    elements.extend(draw_panel("Raw bytes, linear x-axis", series, 80, 80, 1020, 250, log_x=False))
    elements.extend(draw_panel("Raw bytes, log x-axis", series, 80, 430, 1020, 250, log_x=True))
    elements.extend(draw_panel("Normalized by max, linear x-axis", series, 80, 780, 1020, 250, log_x=False, normalize="max"))
    elements.extend(draw_panel("Normalized by p99, log x-axis", series, 80, 1130, 1020, 250, log_x=True, normalize="p99"))

    legend_x = 880
    legend_y = 32
    for idx, (name, _, color) in enumerate(series):
        y = legend_y + idx * 22
        elements.append(f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 30}" y2="{y}" stroke="{color}" stroke-width="3"/>')
        elements.append(f'<text x="{legend_x + 40}" y="{y + 5}" class="label">{escape(name)}</text>')

    elements.append("</svg>")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(elements), encoding="utf-8")


if __name__ == "__main__":
    build_svg(ROOT_DIR / "analyze/cdf_comparison.svg")
    print("wrote analyze/cdf_comparison.svg")
