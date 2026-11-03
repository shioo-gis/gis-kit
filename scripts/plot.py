#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "matplotlib>=3.8",
#     "pyyaml>=6.0",
# ]
# ///
"""Fallback static maps (location, land use) from YAML templates, for use without gis-composer.

Finished maps go through gis-composer; this tool stays for check plots and for installs without it.
Its templates are frozen: no new styles or visual options (see references/composer-display.md).
Without --style each category uses its default: location/default, landuse/standard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _sandbox import configure_writable_caches

configure_writable_caches()

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _theme import apply_theme, get_chinese_font, load_template, load_palette
except ImportError:
    def get_chinese_font() -> str:
        import matplotlib.font_manager as fm
        for name in ["Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", "SimHei"]:
            if name in {f.name for f in fm.fontManager.ttflist}:
                return name
        return "DejaVu Sans"

    def apply_theme(fig, ax):
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")
        fig.tight_layout()

    def load_template(category: str, style: str = "default") -> dict:
        import yaml
        scripts_dir = Path(__file__).resolve().parent
        p = scripts_dir.parent / "templates" / category / f"{style}.yaml"
        if not p.exists():
            raise FileNotFoundError(f"Template not found: {p}")
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def load_palette(name: str) -> list[str]:
        return ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


_LAYER_ORDER = ["province", "city", "district", "highlight"]
_DEFAULT_STYLE = {"location": "default", "landuse": "standard"}

from _budget import check_vector
from _core import reproject


def _parse_input(spec: str) -> tuple[str, str | None]:
    if ":" in spec and not Path(spec).exists():
        parts = spec.rsplit(":", 1)
        return parts[0], parts[1]
    return spec, None


def _read(path: str, layer: str | None = None) -> gpd.GeoDataFrame:
    kwargs = {"engine": "pyogrio"}
    if layer:
        kwargs["layer"] = layer
    check_vector(path, layer)
    return gpd.read_file(path, **kwargs)


def _setup_font():
    font = get_chinese_font()
    plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return font


def _parse_figsize(s: str) -> tuple[float, float]:
    w, h = s.split(",")
    return float(w), float(h)


def _apply_highlight(gdf: gpd.GeoDataFrame, expr: str) -> gpd.GeoDataFrame:
    try:
        idx = int(expr)
        return gdf.iloc[[idx]]
    except ValueError:
        return gdf.query(expr)


def label_audit(fig, ax):
    """Actual renderer extents after final layout, not anchor-only collision guesses."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = [t for t in ax.texts if t.get_visible() and t.get_text()]
    boxes = [t.get_window_extent(renderer) for t in texts]
    extent = ax.get_window_extent(renderer)
    return {"labels": [{"text": t.get_text(), "bounds_px": list(b.extents),
                         "inside_map": bool(extent.contains(b.x0, b.y0) and extent.contains(b.x1, b.y1))}
                        for t, b in zip(texts, boxes)],
            "overlaps": [[i, j] for i, a in enumerate(boxes) for j, b in enumerate(boxes)
                         if j > i and a.overlaps(b)],
            "scope": "actual rendered text boxes; feature occlusion and semantics require visual review"}


def plot_location(inputs: list[tuple[str, str | None]], template: dict, args):
    font_name = _setup_font()
    figsize = _parse_figsize(args.figsize) if args.figsize else (12, 8)
    fig, ax = plt.subplots(1, 1, figsize=figsize)

    layers_cfg = template.get("layers", {})
    layout = template.get("layout", {})

    ax.set_facecolor(layout.get("background", "#FFFFFF"))

    gdfs = []
    for file_path, layer_name in inputs:
        gdfs.append(_read(file_path, layer_name))

    if gdfs and gdfs[0].crs:
        base_crs = gdfs[0].crs
        for i in range(1, len(gdfs)):
            if gdfs[i].crs and gdfs[i].crs != base_crs:
                gdfs[i] = reproject(gdfs[i], base_crs, purpose='align')

    for i, gdf in enumerate(gdfs):
        layer_key = _LAYER_ORDER[i] if i < len(_LAYER_ORDER) else _LAYER_ORDER[-1]
        style = layers_cfg.get(layer_key, {})

        plot_kwargs = {
            "ax": ax,
            "facecolor": style.get("facecolor", "none"),
            "edgecolor": style.get("edgecolor", "#333333"),
            "linewidth": style.get("linewidth", 1.0),
        }
        if style.get("color_field"):
            import matplotlib
            codes = gdf[style["color_field"]]
            if codes.isna().any() or any(int(v) != v or not 0 <= v < 20 for v in codes):
                raise ValueError("Display color index requires integers 0..19")
            plot_kwargs["facecolor"] = [matplotlib.colormaps["tab20"](int(v)) for v in codes]
        if "alpha" in style:
            plot_kwargs["alpha"] = style["alpha"]

        gdf.plot(**plot_kwargs)

        label_cfg = style.get("label", {})
        label_field = args.label_field or label_cfg.get("field")
        if label_cfg.get("enabled", False) and label_field and label_field in gdf.columns:
            for _, row in gdf.iterrows():
                pt = row.geometry.representative_point()
                ax.text(
                    pt.x, pt.y, str(row[label_field]),
                    fontsize=label_cfg.get("fontsize", 9),
                    fontweight=label_cfg.get("fontweight", "normal"),
                    color=label_cfg.get("color", "#333333"),
                    fontname=font_name,
                    ha="center", va="center",
                )

    if args.highlight and gdfs:
        target = gdfs[-1]
        hl_gdf = _apply_highlight(target, args.highlight)
        hl_style = layers_cfg.get("highlight", {})
        hl_gdf.plot(
            ax=ax,
            facecolor=hl_style.get("facecolor", "#E74C3C"),
            edgecolor=hl_style.get("edgecolor", "#C0392B"),
            linewidth=hl_style.get("linewidth", 2.0),
            alpha=hl_style.get("alpha", 0.6),
        )
        hl_label = hl_style.get("label", {})
        lf = args.label_field or hl_label.get("field")
        if hl_label.get("enabled", False) and lf and lf in hl_gdf.columns:
            for _, row in hl_gdf.iterrows():
                pt = row.geometry.representative_point()
                ax.text(
                    pt.x, pt.y, str(row[lf]),
                    fontsize=hl_label.get("fontsize", 11),
                    fontweight=hl_label.get("fontweight", "bold"),
                    color=hl_label.get("color", "#C0392B"),
                    fontname=font_name,
                    ha="center", va="center",
                )

    ax.set_axis_off()

    title_cfg = layout.get("title", {})
    if args.title:
        ax.set_title(
            args.title,
            fontsize=title_cfg.get("fontsize", 18),
            fontweight=title_cfg.get("fontweight", "bold"),
            color=title_cfg.get("color", "#2C3E50"),
            fontname=font_name,
        )

    padding = layout.get("padding", 0.05)
    xmin, ymin, xmax, ymax = ax.get_xlim()[0], ax.get_ylim()[0], ax.get_xlim()[1], ax.get_ylim()[1]
    dx = (xmax - xmin) * padding
    dy = (ymax - ymin) * padding
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)

    # Optional fixed atlas extent and projected-unit scale; ordinary maps unchanged.
    if layout.get("bounds") is not None:
        xmin, ymin, xmax, ymax = layout["bounds"]
        ax.set_xlim(xmin, xmax)
        ax.set_ylim(ymin, ymax)
    if layout.get("scale_bar"):
        scale = layout["scale_bar"]
        xmin, xmax = ax.get_xlim(); ymin, ymax = ax.get_ylim()
        x = xmin + (xmax-xmin)*.05; y = ymin + (ymax-ymin)*.05
        ax.plot([x, x+scale["length_units"]], [y, y], color="black", linewidth=3)
        ax.text(x, y+(ymax-ymin)*.025, scale["label"], fontsize=9, fontname=font_name)
    if layout.get("legend"):
        ax.legend(handles=[mpatches.Patch(facecolor=v["color"], label=v["label"])
                           for v in layout["legend"]], loc="upper right")

    fig.tight_layout()
    if layout.get("table_rows"):
        ax.set_position([.08, .29, .84, .65])
        summary = fig.add_axes([.08, .03, .84, .19])
        summary.set_axis_off()
        table = summary.table(cellText=layout["table_rows"], colLabels=["Field", "Value"],
                              cellLoc="left", colWidths=[.35,.65], bbox=[0,0,1,1])
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        for cell in table.get_celld().values():
            cell.get_text().set_fontname(font_name)
        summary.set_title("Summary — complete attributes in the JSON dossier", fontsize=9, fontname=font_name)
    if layout.get("fixed_map_axes"):
        ax.set_position(layout["fixed_map_axes"])
    fig.set_dpi(args.dpi)
    audit = label_audit(fig, ax)
    suppressed = []
    if layout.get("label_collision_policy") == "omit":
        kept = []
        renderer = fig.canvas.get_renderer()
        extent = ax.get_window_extent(renderer)
        for text in ax.texts:
            box = text.get_window_extent(renderer)
            if len(kept) >= layout.get("label_max_count", 100000) or not (extent.contains(box.x0, box.y0) and extent.contains(box.x1, box.y1)) or any(box.overlaps(b) for b in kept):
                suppressed.append({"text": text.get_text(), "reason": "budget_or_overlap_or_outside_map"})
                text.set_visible(False)
            else:
                kept.append(box)
        audit = label_audit(fig, ax)
    audit["suppressed"] = suppressed
    if layout.get("fixed_map_axes"):
        extent = ax.get_window_extent(fig.canvas.get_renderer())
        audit["map_width_inches"] = extent.width / fig.dpi
        audit["map_height_inches"] = extent.height / fig.dpi
    fig.savefig(args.output, dpi=args.dpi,
                bbox_inches=None if layout.get("fixed_map_axes") else "tight", facecolor="white")
    plt.close(fig)
    print(f"saved: {args.output}")
    return audit


def plot_landuse(inputs: list[tuple[str, str | None]], template: dict, args):
    font_name = _setup_font()
    figsize = _parse_figsize(args.figsize) if args.figsize else (12, 8)
    fig, ax = plt.subplots(1, 1, figsize=figsize)

    classification = template.get("classification", {})
    style = template.get("style", {})
    layout = template.get("layout", {})

    ax.set_facecolor(layout.get("background", "#FFFFFF"))

    file_path, layer_name = inputs[0]
    gdf = _read(file_path, layer_name)

    color_field = args.color_field
    if not color_field:
        sys.exit("--color-field is required for landuse category")
    if color_field not in gdf.columns:
        sys.exit(f"field not found: {color_field}")

    color_map = {code: info["color"] for code, info in classification.items()}
    name_map = {code: info["name"] for code, info in classification.items()}

    colors = gdf[color_field].map(color_map).fillna("#CCCCCC")

    gdf.plot(
        ax=ax,
        color=colors,
        edgecolor=style.get("edgecolor", "#333333"),
        linewidth=style.get("linewidth", 0.3),
    )

    label_cfg = style.get("label", {})
    label_field = args.label_field or label_cfg.get("field")
    if label_cfg.get("enabled", False) and label_field and label_field in gdf.columns:
        for _, row in gdf.iterrows():
            pt = row.geometry.representative_point()
            ax.text(
                pt.x, pt.y, str(row[label_field]),
                fontsize=label_cfg.get("fontsize", 8),
                fontname=font_name,
                ha="center", va="center",
            )

    if not args.no_legend:
        legend_cfg = style.get("legend", {})
        if legend_cfg.get("enabled", True):
            present = gdf[color_field].unique()
            patches = []
            for code in classification:
                if code in present:
                    patches.append(mpatches.Patch(
                        facecolor=color_map[code],
                        edgecolor=style.get("edgecolor", "#333333"),
                        linewidth=0.5,
                        label=f"{code} - {name_map[code]}",
                    ))
            ax.legend(
                handles=patches,
                loc=legend_cfg.get("loc", "lower right"),
                fontsize=legend_cfg.get("fontsize", 9),
                framealpha=legend_cfg.get("framealpha", 0.9),
                prop={"family": font_name},
            )

    ax.set_axis_off()

    title_cfg = layout.get("title", {})
    if args.title:
        ax.set_title(
            args.title,
            fontsize=title_cfg.get("fontsize", 16),
            fontweight=title_cfg.get("fontweight", "bold"),
            color=title_cfg.get("color", "#333333"),
            fontname=font_name,
        )

    padding = layout.get("padding", 0.03)
    xmin, ymin, xmax, ymax = ax.get_xlim()[0], ax.get_ylim()[0], ax.get_xlim()[1], ax.get_ylim()[1]
    dx = (xmax - xmin) * padding
    dy = (ymax - ymin) * padding
    ax.set_xlim(xmin - dx, xmax + dx)
    ax.set_ylim(ymin - dy, ymax + dy)

    fig.tight_layout()
    fig.savefig(args.output, dpi=args.dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"saved: {args.output}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plot", description="Fallback static maps from frozen templates; finished maps use gis-composer")
    p.add_argument("--inputs", nargs="+", required=True, help="input files (file.gpkg or file.gpkg:layer)")
    p.add_argument("--output", "-o", required=True, help="output image file (PNG/PDF)")
    p.add_argument("--category", default="location", choices=["location", "landuse"], help="map category")
    p.add_argument("--style", default=None,
                   help="frozen template within category (default: location/default, landuse/standard)")
    p.add_argument("--title", default=None, help="map title")
    p.add_argument("--label-field", default=None, help="field for labels")
    p.add_argument("--color-field", default=None, help="field for thematic coloring (landuse)")
    p.add_argument("--figsize", default=None, help="figure size: width,height in inches")
    p.add_argument("--dpi", type=int, default=300, help="output DPI")
    p.add_argument("--no-legend", action="store_true", help="hide legend")
    p.add_argument("--highlight", default=None, help="index or query to highlight features")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    template = load_template(args.category, args.style or _DEFAULT_STYLE[args.category])
    inputs = [_parse_input(spec) for spec in args.inputs]

    if args.category == "location":
        plot_location(inputs, template, args)
    elif args.category == "landuse":
        plot_landuse(inputs, template, args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
