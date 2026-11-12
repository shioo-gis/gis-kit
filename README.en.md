<div align="center">

# gis-kit

**Everyday GIS operations as scripts, easy for AI agents to call.**

A skill for Claude Code, Codex and other agents.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](requirements-tested.txt)
[![Tests](https://github.com/shioo-gis/gis-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/shioo-gis/gis-kit/actions/workflows/tests.yml)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-SKILL.md-8A2BE2.svg)](SKILL.md)

[中文](README.md) · English

</div>

---

## About

This is the GIS data-processing toolbox I use myself. I have written the operations I find useful as scripts, close to 50 of them now, behind a single entry point that an agent can use to discover and combine them.

```bash
python scripts/gis.py list                         # tool catalogue (standard library only)
python scripts/gis.py list --search cad
python scripts/gis.py cad --help                   # parameter reference
python scripts/gis.py capabilities --json          # capability and contract probe (standard library only)
python scripts/gis.py describe parcels.gpkg --json # read-only header metadata; no features or pixels are read
```

[SKILL.md](SKILL.md) records the basic judgements for handling spatial data, and the scripts add guards where things tend to go wrong silently:

- **Staging, read-back and atomic publishing**: output is written to a temporary file or staging directory, read back to check feature counts, fields, CRS, geometry or pixels, and only then published atomically. `record.json` and `manifest.json` record the SHA-256 of the inputs, the script implementation and the products. Existing outputs are refused by default; `gpkg.py` import and copy refuse a layer name that already exists instead of appending silently; `split.py` keeps rows with null values and refuses up front when several values would map to the same file name.
- **Reprojection and measurement fail loudly on distortion**: vector reprojection checks for poles, projection seams and invalid coordinates in one place, and reports an error with the row number instead of flipping or returning empty results; Web Mercator raster reprojection is trimmed to the world extent. Area and distance measurement checks axis units and ground scale, switches to a local UTM zone when a projection such as Web Mercator is too far off scale, and when the data spans zones it stops and asks for an explicit `--analysis-crs`.
- **No invented facts; uncertain items stay under review**: a CAD coordinate without a zone number is not guessed into an EPSG code, text matched to several values or lying on a shared edge is listed as a conflict, field casts keep nulls (no `"nan"` strings, no wrongly converted booleans), encoding repair leaves non-ASCII text without mojibake signs alone, and no population coverage rate is reported without demand weights. Results that are not confirmed keep a `candidate` or `hold` status.
- **Read-time memory budget and the same behaviour across platforms**: by default a read is checked against 80% of the memory available (`--memory-budget`, see [memory budget](references/memory-budget.md)), and large table conversions and raster resampling run by chunk or by window. Parameters, manifests and piped output are UTF-8 on macOS, Linux and Windows.

I use it for ordinary-sized tasks. For larger data or specialised algorithms, an agent can use the optional DuckDB spatial SQL or call back ends such as QGIS, GRASS and PySAL.

## Tools

### 1. Vectors, table joins and indicators

- **Everyday vector work, joins and indicators**: clipping, dissolving, merging, buffers (including concentric service rings), field handling and versioned result bundles ([basic workflows](references/vector-workflows.md), [daily vector](references/daily-vector.md)). Besides spatial joins, `join.py` attaches Excel or CSV indicator tables to parcels by normalised parcel number (full-width and half-width characters, dashes and leading zeros in each segment are evened out) and lists unmatched, duplicate and colliding keys on both sides ([measurement and joins](references/vector-metrics.md)); `topo.py` checks gaps, overlaps and slivers ([topology diagnostics](references/vector-topology.md)); `indicators.py` computes building area, floor area ratio and building density.

### 2. Package and style conversion

- **Unpacking and reading styles** (`package.py`): `unpack` checks archive paths and unpacked size, then safely opens ArcGIS layer and map packages (`.lpk`, `.lpkx`, `.mpk`, `.mpkx`), inventories the datasets and matches them to layer definitions ([ArcGIS package inventory](references/esri-package.md)). `read` reads QGIS `.qml`, ArcGIS Pro `.lyrx` and `.stylx` symbol libraries into a platform-neutral `map-style` document and records each symbol that is approximated or cannot be converted ([reading platform styles](references/style-read.md)).
- **Packing a map** (`package.py pack`): turns a `map-style` map and its data (selected by sheet, clipped, or kept whole) into a package for the target platform: a QGIS GeoPackage with the layer order, QML styles and preset label points embedded ([QGIS packing](references/qgis-package.md)); an ArcGIS Pro layer package `.lpkx` with a file geodatabase and CIM layer files ([ArcGIS Pro packing](references/arcgis-pro-package.md)); or an ArcMap layer package `.lpk` whose binary `.lyr` files are assembled from ArcObjects skeletons ([ArcMap packing](references/arcmap-package.md)).

### 3. Rasters, DEM terrain and road-network coverage

- **Rasters and terrain** (`raster.py`, `terrain.py`, `terrain-detail.py`): reprojection, grid alignment, zonal statistics, rasterisation, windowed local computation, patch clean-up, mass-preserving allocation by zone and COG output ([numeric rasters](references/raster-numeric.md), [raster clean-up](references/raster-cleanup.md)). From a DEM they compute slope, aspect, relief, analytical hillshade, contours and profiles; hydrology, visibility and anisotropic walking cost can use a GRASS back end ([terrain diagnostics](references/terrain-analysis.md), [professional analysis](references/professional-analysis.md)).
- **Network coverage and spatial statistics** (`network.py`, `suitability.py`, `spatial-inference.py`): builds a directed network from source-node topology, distinguishes levels, one-way segments, permitted access areas and barriers, and computes OD, reachable segments and facility coverage scenarios, with covered, uncovered and unknown reported separately ([network and demand coverage](references/network-access.md)). It also offers multi-scenario suitability scoring, Gaussian kernel density and optional PySAL spatial autocorrelation tests.

### 4. Spatial SQL for large tables, and repeated runs

- **Large tables** (`spatial-sql.py`): runs a single native DuckDB Spatial `SELECT` and writes read-back-verified GeoParquet directly, without loading the detail rows into GeoPandas ([Spatial SQL](references/spatial-sql.md)).
- **Traces and recipes** (`gis.py --trace`, `recipe.py`, `semantic-check.py`): record the steps actually run and file fingerprints, and turn them into a sequential recipe with content caches for the next batch of data. `semantic-check` checks whether named semantic fields or JSON pointers lose protected states such as `unknown`, `candidate` or `hold` between steps ([harness](references/harness.md), [repeated delivery](references/repeated-delivery.md)).

### 5. Display derivatives and the hand-off to gis-composer

gis-composer is another project of mine, dedicated to producing maps: it renders map PNGs mainly with MapLibre, renders map videos from a Storyboard deterministically, and can use Blender for DEM terrain maps and videos. It is still in private development; I intend to make it public once it is in good shape, with no date yet.

gis-kit does spatial computation and diagnostic check plots, and leaves finished maps, Scene / Storyboard and map video to gis-composer. The two exchange only files and JSON contracts and never import each other's code. Without gis-composer, `plot.py`, `cartographic-map.py`, `atlas.py` and `plot-chart.py` still produce fallback maps, object atlases and statistical charts. With gis-composer, gis-kit publishes colour-free class schemes (`scheme.py`), numerically coded display rasters (`raster.py display`), display vectors generalised on shared edges for the map's view ([display-vector](references/display-vector.md)), a site's administrative chain ([admin-chain](references/admin-chain.md)) and allow-listed tile snapshots ([tile-snapshot](references/tile-snapshot.md)) atomically into the project workspace's `inputs/`; see [Composer display delivery](references/composer-display.md).

### 6. Semantic vectorisation and georeferencing of scanned maps (experimental)

A multimodal model reads the image and draws points, polylines and Bézier curves directly in the original pixel space. `raster-annotate.py` handles local cropping, coordinate conversion, incremental versions, geometry checks, overlay review packs and export; unclosed or invalid polygons are never closed automatically. Every product is a candidate that needs human review. Drawing and georeferencing are separate steps: an ungeoreferenced result stays in a pixel project space with no geographic CRS and is never published with invented latitude and longitude; when geographic coordinates are needed, `raster-georef.py` fits control points and accepts the result against independent check points, and the existing drawing can be carried over without redrawing. See the [annotation workflow](references/raster-annotation.md), the [object and record contract](references/raster-schema.md) and [map georeferencing](references/raster-georeferencing.md).

### 7. Xiangyuan control-plan drawings to parcels (a specific adaptation)

This part is fairly niche. It was built specifically for drawings from **Xiangyuan (湘源) control detailed plans** (`cad.py`, see [CAD intake](references/cad-intake.md)); it is not a general CAD-to-GIS tool. Drawings from other software can only use the generic extraction and polygonising path below, must be checked drawing by drawing, and are not guaranteed to work out of the box.

- **Xiangyuan plan sheets**: `xiangyuan` reads the land-use code and other attributes from the extended data on the parcel polylines, and also extracts the Excel indicator tables (OLE) and CAD tables embedded in the drawing.
- **Generic DXF path**: converts DWG to DXF with an ODA File Converter or LibreDWG already installed on the machine; inspects layers, units and outlier frames and compares candidate CRSs; after removing drawing frames and legends it extracts entities with explicit tolerances, polygonises them, attaches text by YAML rules, and compares hatch fills with `compare-fills`.
- **Coordinates and checks**: once the coordinate system is confirmed, `assign-crs` assigns the CRS (coordinates without a zone number must first pass a check against a reference boundary or known points); local city coordinates are converted with a four-parameter or affine transform by `vector-georef.py` ([vector georeferencing](references/vector-georeferencing.md)); finally `overlay` overlays reference layers such as already-supplied land, measures the intersection areas and draws a check plot.

## Companion: gis-plugin

gis-kit only handles data processing and does not touch the agent's runtime environment. The cross-tool and cross-engine work lives in another project, gis-plugin: a very thin shell that contains no code that reads or writes GIS data and calls the command lines of gis-kit and other engines whenever it needs data. So far it provides:

- **Hooks**: when the agent reads GIS data, a metadata card and field semantics are injected automatically; registered source data is protected from being overwritten or rewritten by commands; when a project has a style convention, the hook says where it is; and freshly written SVG, PPTX and HTML are checked for defensive, explanatory or disclaimer wording. Hooks inject facts, not lists of rules, use only the Python standard library, and work in both Claude Code and Codex.
- **Contracts and diagnostics**: the file formats the engines exchange, and `gis-doctor`, which checks whether the engines are installed and compatible.
- **Evaluation**: comparing how well an agent completes GIS tasks with and without the shell.

gis-plugin is still in private development. I intend to keep refining it and make it public once it is in good shape; there is no date yet. gis-kit does not depend on it and is fully usable on its own.

## Quick start

**1. Install**: clone into the agent's skills directory and keep the directory name `gis-kit`.

```bash
# Claude Code
git clone https://github.com/shioo-gis/gis-kit.git ~/.claude/skills/gis-kit
# Codex
git clone https://github.com/shioo-gis/gis-kit.git ~/.codex/skills/gis-kit
```

**2. Set up the Python environment** (Python ≥ 3.10)

```bash
python3.10 -m venv ~/.venvs/gis-kit
~/.venvs/gis-kit/bin/python -m pip install -r ~/.claude/skills/gis-kit/requirements-full.txt
~/.venvs/gis-kit/bin/python ~/.claude/skills/gis-kit/scripts/daily.py environment
```

**3. Just tell the agent**

> Attach `indicators.xlsx` to `parcels.gpkg` by parcel number, list the numbers that did not match on either side, then check the parcels for overlaps and gaps.
>
> Unpack this `.lpkx` and list the layers and data inside; then pack `example.map-style.json` into a QGIS package and an ArcGIS Pro package.
>
> Compute slope and aspect for this DEM and give the mean slope of each administrative district.
>
> Compute 15-minute drive coverage for these hospitals, summarise coverage by neighbourhood population weights, and list the neighbourhoods that are uncovered or unknown.

The agent reads [SKILL.md](SKILL.md), consults the reference documents in `references/` as needed, chooses the tools and parameters itself, and reads the results back.

## Environment and dependencies

1. `requirements-core.txt`: core dependencies for vector, raster, measurement and check plots.
2. `requirements-full.txt`: adds CAD drawings (`ezdxf`, `olefile`), Excel tables (`openpyxl`), networks, statistics, image enhancement and test tools on top of the core set.
3. `requirements-tested.txt`: the exact versions with which the full regression tests pass under Python 3.10.
4. `requirements-spatial-sql.txt`: optional DuckDB spatial SQL dependency.

If installing the GDAL-related packages with pip is difficult, use `environment.yml` to create a conda-forge environment. Within one task the agent should always use the same Python interpreter; see [runtime environment](references/runtime-environment.md).

External programs and professional back ends are installed separately as needed: DWG to DXF uses a local ODA File Converter (set its path with `GIS_KIT_ODA`) or LibreDWG `dwg2dxf`; reading and writing 7z layer packages uses the system `bsdtar` or 7-Zip; QGIS, GRASS and PySAL are optional back ends. If gis-composer is installed, point to it with `GIS_COMPOSER_HOME`.

Chinese map text uses the first installed font in the order listed in `config/user.yaml`, Noto Sans CJK SC by default. If none of the listed fonts is installed it falls back to DejaVu Sans, and Chinese will not display correctly.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

`tests/test_*.py` are self-contained regression tests using synthetic or hand-computed data.

`tests/run_*_acceptance.py` check complete processing chains with real files. External data such as OSM, Natural Earth or a DEM must be prepared first, and the output directory should be outside the repository. The inputs each script needs are listed at its top.

## Data and privacy

The scripts run locally, but what the agent reads may enter the context of a remote model. `--trace` records contain file paths and task parameters; check them before sharing.

The repository does not ship complete third-party datasets. The test images in the repository use a cropped part of Tan Qixiang's *Historical Atlas of China*; the base map is copyrighted by its original rights holders and is not covered by this repository's code licence. When using OSM (ODbL), Natural Earth (public domain) and online tile services, follow their licences, caching terms and attribution requirements.

## Licence

[Apache License 2.0](LICENSE)

<sub>Keywords: GIS · geospatial · AI agent · Claude Code skill · Codex skill · Agent Skills · LLM · CAD · DXF · land use · QGIS · ArcGIS · GeoPackage · GeoPandas · Rasterio · GDAL · Shapely · DuckDB · DEM · terrain analysis · network analysis · georeferencing · 空间分析 · 城乡规划 · 地理信息 · 地图配准</sub>
