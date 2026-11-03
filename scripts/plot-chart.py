#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "matplotlib>=3.8",
#     "pyyaml>=6.0",
#     "numpy",
# ]
# ///
"""Statistical chart generation from GIS or tabular data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _sandbox import configure_writable_caches

configure_writable_caches()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _theme import apply_chart_style, get_chinese_font, load_template, load_palette
except ImportError:
    def get_chinese_font() -> str:
        import matplotlib.font_manager as fm
        for name in ["Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", "SimHei"]:
            if name in {f.name for f in fm.fontManager.ttflist}:
                return name
        return "DejaVu Sans"

    def apply_chart_style(fig, ax, template=None):
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()

    def load_template(category: str, style: str = "default") -> dict:
        import yaml
        scripts_dir = Path(__file__).resolve().parent
        p = scripts_dir.parent / "templates" / category / f"{style}.yaml"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def load_palette(name: str) -> list[str]:
        return ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

from _budget import check_vector


def _setup_font():
    font = get_chinese_font()
    plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return font


def _parse_figsize(s: str) -> tuple[float, float]:
    w, h = s.split(",")
    return float(w), float(h)


def _read_data(path: str, layer: str | None = None):
    import pandas as pd
    p = Path(path)
    if p.suffix.lower() in (".csv", ".tsv"):
        sep = "\t" if p.suffix.lower() == ".tsv" else ","
        return pd.read_csv(path, sep=sep)
    import geopandas as gpd
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def _get_colors(n: int, palette_name: str | None, template: dict) -> list[str]:
    if palette_name:
        colors = load_palette(palette_name)
    else:
        name = template.get("style", {}).get("palette", "categorical_10")
        try:
            colors = load_palette(name)
        except (FileNotFoundError, KeyError):
            colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                       "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
    while len(colors) < n:
        colors = colors + colors
    return colors[:n]


def chart_bar(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (10, 6)
    fig, ax = plt.subplots(figsize=figsize)
    bar_cfg = template.get("style", {}).get("bar", {})

    if args.group_by and args.group_by in df.columns:
        grouped = df.groupby(args.group_by)[args.field].sum()
        if args.sort:
            grouped = grouped.sort_values(ascending=False)
        if args.top:
            grouped = grouped.head(args.top)
        c = _get_colors(len(grouped), args.palette, template)
        grouped.plot.bar(
            ax=ax, color=c,
            edgecolor=bar_cfg.get("edgecolor", "#2C3E50"),
            linewidth=bar_cfg.get("linewidth", 0.5),
            alpha=bar_cfg.get("alpha", 0.85),
        )
    else:
        counts = df[args.field].value_counts()
        if args.sort:
            counts = counts.sort_values(ascending=False)
        if args.top:
            counts = counts.head(args.top)
        c = _get_colors(len(counts), args.palette, template)
        counts.plot.bar(
            ax=ax, color=c,
            edgecolor=bar_cfg.get("edgecolor", "#2C3E50"),
            linewidth=bar_cfg.get("linewidth", 0.5),
            alpha=bar_cfg.get("alpha", 0.85),
        )

    ax.set_xlabel(args.xlabel or "")
    ax.set_ylabel(args.ylabel or "count")
    ax.set_title(args.title or args.field)
    plt.xticks(rotation=45, ha="right", fontname=font_name)
    return fig, ax


def chart_hbar(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (10, 6)
    fig, ax = plt.subplots(figsize=figsize)
    bar_cfg = template.get("style", {}).get("bar", {})

    counts = df[args.field].value_counts()
    if args.sort:
        counts = counts.sort_values(ascending=True)
    if args.top:
        counts = counts.tail(args.top)
    c = _get_colors(len(counts), args.palette, template)

    counts.plot.barh(
        ax=ax, color=c,
        edgecolor=bar_cfg.get("edgecolor", "#2C3E50"),
        linewidth=bar_cfg.get("linewidth", 0.5),
        alpha=bar_cfg.get("alpha", 0.85),
    )
    ax.set_xlabel(args.xlabel or "count")
    ax.set_ylabel(args.ylabel or "")
    ax.set_title(args.title or args.field)
    return fig, ax


def chart_pie(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (8, 8)
    fig, ax = plt.subplots(figsize=figsize)
    pie_cfg = template.get("style", {}).get("pie", {})

    counts = df[args.field].value_counts()
    if args.top:
        top = counts.head(args.top)
        rest = counts.iloc[args.top:].sum()
        if rest > 0:
            import pandas as pd
            top = pd.concat([top, pd.Series({"其他": rest})])
        counts = top

    c = _get_colors(len(counts), args.palette, template)
    counts.plot.pie(
        ax=ax, colors=c,
        startangle=pie_cfg.get("startangle", 90),
        autopct=pie_cfg.get("autopct", "%1.1f%%"),
        shadow=pie_cfg.get("shadow", False),
        textprops={"fontname": font_name, "fontsize": 9},
    )
    ax.set_ylabel("")
    ax.set_title(args.title or args.field)
    return fig, ax


def chart_hist(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (10, 6)
    fig, ax = plt.subplots(figsize=figsize)
    hist_cfg = template.get("style", {}).get("hist", {})

    data = df[args.field].dropna()
    ax.hist(
        data,
        bins=hist_cfg.get("bins", 20),
        color=colors[0],
        edgecolor=hist_cfg.get("edgecolor", "#FFFFFF"),
        linewidth=hist_cfg.get("linewidth", 0.8),
        alpha=hist_cfg.get("alpha", 0.8),
    )
    ax.set_xlabel(args.xlabel or args.field)
    ax.set_ylabel(args.ylabel or "count")
    ax.set_title(args.title or args.field)
    return fig, ax


def chart_scatter(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (10, 8)
    fig, ax = plt.subplots(figsize=figsize)

    x_field = args.x_field
    if not x_field:
        sys.exit("--x-field is required for scatter chart")
    if x_field not in df.columns:
        sys.exit(f"x-field not found: {x_field}")

    if args.group_by and args.group_by in df.columns:
        groups = df.groupby(args.group_by)
        c = _get_colors(len(groups), args.palette, template)
        for i, (name, group) in enumerate(groups):
            ax.scatter(group[x_field], group[args.field], c=c[i], label=str(name),
                       alpha=0.7, edgecolors="white", linewidth=0.5)
        ax.legend(prop={"family": font_name, "size": 9})
    else:
        ax.scatter(df[x_field], df[args.field], c=colors[0],
                   alpha=0.7, edgecolors="white", linewidth=0.5)

    ax.set_xlabel(args.xlabel or x_field)
    ax.set_ylabel(args.ylabel or args.field)
    ax.set_title(args.title or f"{args.field} vs {x_field}")
    return fig, ax


def chart_box(df, args, template, colors, font_name):
    figsize = _parse_figsize(args.figsize) if args.figsize else (10, 6)
    fig, ax = plt.subplots(figsize=figsize)

    if args.group_by and args.group_by in df.columns:
        groups = df[args.group_by].unique()
        data = [df[df[args.group_by] == g][args.field].dropna().values for g in groups]
        bp = ax.boxplot(data, patch_artist=True, labels=[str(g) for g in groups])
        c = _get_colors(len(groups), args.palette, template)
        for patch, color in zip(bp["boxes"], c):
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
        plt.xticks(rotation=45, ha="right", fontname=font_name)
    else:
        data = df[args.field].dropna().values
        bp = ax.boxplot(data, patch_artist=True)
        bp["boxes"][0].set_facecolor(colors[0])
        bp["boxes"][0].set_alpha(0.7)

    ax.set_ylabel(args.ylabel or args.field)
    ax.set_title(args.title or args.field)
    return fig, ax


_CHART_DISPATCH = {
    "bar": chart_bar,
    "hbar": chart_hbar,
    "pie": chart_pie,
    "hist": chart_hist,
    "scatter": chart_scatter,
    "box": chart_box,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plot-chart", description="Statistical chart generation")
    p.add_argument("input", help="input file (GIS vector or CSV)")
    p.add_argument("--output", "-o", required=True, help="output image file (PNG/PDF)")
    p.add_argument("--layer", default=None, help="layer name (for GIS files)")
    p.add_argument("--field", required=True, help="field for the chart")
    p.add_argument("--type", required=True, choices=list(_CHART_DISPATCH.keys()), help="chart type")
    p.add_argument("--group-by", default=None, help="field to group by")
    p.add_argument("--x-field", default=None, help="X axis field (for scatter)")
    p.add_argument("--title", default=None, help="chart title")
    p.add_argument("--xlabel", default=None, help="X axis label")
    p.add_argument("--ylabel", default=None, help="Y axis label")
    p.add_argument("--style", default="default", help="chart template style")
    p.add_argument("--top", type=int, default=None, help="show only top N values")
    p.add_argument("--figsize", default=None, help="figure size: width,height")
    p.add_argument("--dpi", type=int, default=300, help="output DPI")
    p.add_argument("--sort", action="store_true", help="sort bars by value")
    p.add_argument("--palette", default=None, help="color palette name from palettes.yaml")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.field not in (df := _read_data(args.input, args.layer)).columns:
        sys.exit(f"field not found: {args.field}")

    font_name = _setup_font()
    template = load_template("chart", args.style)
    colors = _get_colors(20, args.palette, template)

    chart_fn = _CHART_DISPATCH[args.type]
    fig, ax = chart_fn(df, args, template, colors, font_name)

    apply_chart_style(fig, ax, template)
    fig.savefig(args.output, dpi=args.dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"saved: {args.output}")


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
