# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "geopandas>=1.0",
#     "pyogrio>=0.10",
#     "tabulate",
# ]
# ///

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pyogrio
from _budget import check_vector
from _safe_io import stream_vector
from _table import tabulate


def resolve_path(raw: str, must_exist: bool = True) -> Path:
    p = Path(raw).expanduser().resolve()
    if must_exist and not p.exists():
        sys.exit(f"Error: file not found: {p}")
    return p


def is_gdb(path: Path) -> bool:
    return path.suffix.lower() == ".gdb" or str(path).lower().endswith(".gdb")


def gdal_gdb_support() -> str:
    """What the running GDAL can do with File GDB, for refusal messages."""
    version = pyogrio.__gdal_version_string__
    caps = pyogrio.list_drivers().get("OpenFileGDB")
    if caps and "w" in caps:
        return f"GDAL {version} here can write File GDB (OpenFileGDB driver), but gis-kit has no File GDB output accepted against a delivery template yet, so gpkg.py keeps File GDB read-only."
    if caps:
        return f"GDAL {version} here can only read File GDB (writing through OpenFileGDB needs GDAL >= 3.6)."
    return f"GDAL {version} here has no OpenFileGDB driver."


def guard_gdb_write(path: Path, operation: str, hint: str) -> None:
    """Refuse a write to File GDB; ``operation`` reads as "<operation> File GDB"."""
    if is_gdb(path):
        sys.exit(f"Error: {operation} File GDB is refused: gpkg.py does not write File GDB. {gdal_gdb_support()} {hint}")


def copy_layer(source: Path, source_layer: str, target: Path, driver: str, *, layer: str | None = None, mode: str = "w") -> int:
    """Write one layer with ``GeoDataFrame.to_file`` semantics; layers larger than one chunk are streamed."""
    layer_kw = {"layer": layer} if layer else {}

    def write(frame, first, **options):
        frame.to_file(target, driver=driver, engine="pyogrio", mode=mode if first else "a", **layer_kw, **options)

    count = stream_vector(source, write, driver=driver, layer=source_layer)
    if count is None:
        check_vector(source, source_layer)
        gdf = gpd.read_file(source, layer=source_layer, engine="pyogrio")
        gdf.to_file(target, driver=driver, engine="pyogrio", mode=mode, **layer_kw)
        count = len(gdf)
    return count


def refuse_existing_layer(target: Path, layer: str, name_option: str) -> None:
    """Writing into an existing layer would add its rows a second time, so that is never done quietly."""
    if not target.exists():
        return
    existing = {name.lower(): name for name, _ in pyogrio.list_layers(target)}  # SQLite table names ignore case
    if layer.lower() in existing:
        sys.exit(f"Error: layer '{existing[layer.lower()]}' already exists in {target}; nothing was written. "
                 f"gpkg.py does not append to or replace a layer: use {name_option} for a new name, "
                 "remove the old layer with gpkg.py drop, or combine the data with merge.py.")


def looks_like_sql(query: str) -> bool:
    return query.lstrip().upper().startswith(("SELECT", "WITH"))


def cmd_layers(args: argparse.Namespace) -> None:
    path = resolve_path(args.input)
    try:
        info = pyogrio.list_layers(path)
    except Exception as e:
        sys.exit(f"Error reading layers: {e}")

    rows = []
    for name, geom_type in info:
        try:
            meta = pyogrio.read_info(path, layer=name)
            rows.append({
                "layer": name,
                "features": meta.get("features", "?"),
                "geometry_type": geom_type or "None",
                "crs": str(meta.get("crs", "Unknown")),
            })
        except Exception:
            rows.append({
                "layer": name,
                "features": "?",
                "geometry_type": geom_type or "None",
                "crs": "?",
            })

    if not rows:
        print("No layers found.")
        return
    print(tabulate(rows, headers="keys", tablefmt="simple"))


def cmd_import(args: argparse.Namespace) -> None:
    source = resolve_path(args.source)
    target = resolve_path(args.target, must_exist=False)
    guard_gdb_write(target, "import to", "Import into a .gpkg target instead, e.g. --target out.gpkg.")

    source_layer = args.source_layer
    if source_layer is None:
        layers = pyogrio.list_layers(source)
        if len(layers) == 0:
            sys.exit("Error: no layers found in source file")
        source_layer = layers[0][0]

    target_layer = args.target_layer or source_layer

    try:
        refuse_existing_layer(target, target_layer, "--target-layer")
        count = copy_layer(source, source_layer, target, "GPKG", layer=target_layer, mode="a" if target.exists() else "w")
    except Exception as e:
        sys.exit(f"Error importing layer '{source_layer}': {e}")

    print(f"Imported '{source_layer}' -> '{target_layer}' ({count} features)")


def cmd_export(args: argparse.Namespace) -> None:
    input_path = resolve_path(args.input)
    output_path = resolve_path(args.output, must_exist=False)
    guard_gdb_write(output_path, "export to", "Export to .gpkg, .shp, .geojson, .fgb or .parquet instead.")

    layer = args.layer
    if layer is None:
        layers = pyogrio.list_layers(input_path)
        if len(layers) == 0:
            sys.exit("Error: no layers found in input file")
        layer = layers[0][0]

    driver_map = {
        ".shp": "ESRI Shapefile",
        ".geojson": "GeoJSON",
        ".json": "GeoJSON",
        ".gpkg": "GPKG",
        ".fgb": "FlatGeobuf",
        ".parquet": None,
    }

    suffix = output_path.suffix.lower()
    if suffix == ".parquet":
        try:
            check_vector(input_path, layer)
            gdf = gpd.read_file(input_path, layer=layer, engine="pyogrio")
        except Exception as e:
            sys.exit(f"Error reading layer '{layer}': {e}")
        try:
            gdf.to_parquet(output_path)
        except Exception as e:
            sys.exit(f"Error writing parquet: {e}")
        count = len(gdf)
    else:
        driver = driver_map.get(suffix)
        if driver is None:
            driver = "GPKG"
        try:
            count = copy_layer(input_path, layer, output_path, driver)
        except Exception as e:
            sys.exit(f"Error exporting layer '{layer}': {e}")

    print(f"Exported '{layer}' -> {output_path} ({count} features)")


def cmd_drop(args: argparse.Namespace) -> None:
    input_path = resolve_path(args.input)
    guard_gdb_write(input_path, "layer deletion from", "Copy the layers you want to keep into a .gpkg (gpkg.py copy --source in.gdb --source-layer NAME --target out.gpkg), or delete the layer in ArcGIS.")

    layer = args.layer
    layers = pyogrio.list_layers(input_path)
    names = [l[0] for l in layers]
    if layer not in names:
        sys.exit(f"Error: layer '{layer}' not found. Available: {', '.join(names)}")

    if len(names) == 1:
        sys.exit("Error: cannot drop the only layer in the file. Delete the file instead.")

    try:
        _drop_gpkg_layer(input_path, layer)
    except Exception as e:
        sys.exit(f"Error dropping layer: {e}")

    print(f"Dropped layer '{layer}' from {input_path}")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rewrite_trigger_sql(sql: str, idents: dict[str, str], literals: dict[str, str]) -> str:
    """Rename identifiers and exact-match string literals in trigger SQL.

    Walks the statement token by token so names inside other literals,
    comments or longer identifiers are left alone. Bare OLD./NEW. are the
    trigger pseudo-rows, never a table reference.
    """
    ident_map = {k.lower(): v for k, v in idents.items()}
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            value = sql[i + 1:j].replace("''", "'")
            if value in literals:
                out.append("'" + literals[value].replace("'", "''") + "'")
            else:
                out.append(sql[i:j + 1])
            i = j + 1
        elif ch in '"`[':
            close = {'"': '"', '`': '`', '[': ']'}[ch]
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != ']' and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            value = sql[i + 1:j]
            if close != ']':
                value = value.replace(close * 2, close)
            target = ident_map.get(value.lower())
            out.append(quote_ident(target) if target is not None else sql[i:j + 1])
            i = j + 1
        elif sql.startswith('--', i):
            j = sql.find('\n', i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
        elif sql.startswith('/*', i):
            j = sql.find('*/', i + 2)
            j = n if j == -1 else j + 2
            out.append(sql[i:j])
            i = j
        elif ch.isalpha() or ch == '_':
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in '_$'):
                j += 1
            word = sql[i:j]
            target = ident_map.get(word.lower())
            follow = sql[j:].lstrip()[:1]
            pseudo_row = word.lower() in ('old', 'new') and follow == '.'
            if target is not None and not pseudo_row and follow != '(':
                out.append(quote_ident(target))
            else:
                out.append(word)
            i = j
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def _table_exists(cursor, name: str) -> bool:
    return cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _rename_gpkg_layer(path: Path, old_name: str, new_name: str) -> None:
    """Rename a GPKG feature/attribute table in place, as GDAL's GPKG driver does."""
    import sqlite3

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            clash = cursor.execute(
                "SELECT name FROM sqlite_master WHERE lower(name) = lower(?)", (new_name,)
            ).fetchone()
            if clash:
                raise ValueError(f"an object named '{clash[0]}' already exists")

            row = cursor.execute(
                "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (old_name,)
            ).fetchone()
            geom_col = row[0] if row else None
            old_rtree = f"rtree_{old_name}_{geom_col}" if geom_col else None
            new_rtree = f"rtree_{new_name}_{geom_col}" if geom_col else None
            has_rtree = bool(old_rtree) and _table_exists(cursor, old_rtree)
            if has_rtree:
                for candidate in (new_rtree, *(f"{new_rtree}_{s}" for s in ("rowid", "node", "parent"))):
                    clash = cursor.execute(
                        "SELECT name FROM sqlite_master WHERE lower(name) = lower(?)", (candidate,)
                    ).fetchone()
                    if clash:
                        raise ValueError(f"spatial index object '{clash[0]}' already exists")

            # Triggers on the feature table embed the old table/rtree names;
            # drop them first so ALTER TABLE does not have to re-parse bodies
            # calling ST_* functions that plain sqlite3 does not provide.
            triggers = cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (old_name,)
            ).fetchall()
            idents = {old_name: new_name}
            if has_rtree:
                idents[old_rtree] = new_rtree
            renamed_triggers = []
            for trig_name, trig_sql in triggers:
                new_trig = trig_name
                if has_rtree and trig_name.startswith(old_rtree + '_'):
                    new_trig = new_rtree + trig_name[len(old_rtree):]
                elif trig_name.endswith('_feature_count_' + old_name):
                    new_trig = trig_name[:-len(old_name)] + new_name
                if new_trig != trig_name:
                    clash = cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE lower(name) = lower(?)", (new_trig,)
                    ).fetchone()
                    if clash:
                        raise ValueError(f"trigger '{new_trig}' already exists")
                renamed_triggers.append((trig_name, new_trig, trig_sql))
            for trig_name, new_trig, trig_sql in renamed_triggers:
                cursor.execute(f"DROP TRIGGER {quote_ident(trig_name)}")

            cursor.execute(f"ALTER TABLE {quote_ident(old_name)} RENAME TO {quote_ident(new_name)}")
            if has_rtree:
                cursor.execute(f"ALTER TABLE {quote_ident(old_rtree)} RENAME TO {quote_ident(new_rtree)}")

            for trig_name, new_trig, trig_sql in renamed_triggers:
                trig_idents = dict(idents)
                trig_idents[trig_name] = new_trig
                cursor.execute(_rewrite_trigger_sql(trig_sql, trig_idents, {old_name: new_name}))

            cursor.execute(
                "UPDATE gpkg_contents SET identifier = ? WHERE table_name = ? AND identifier = ?",
                (new_name, old_name, old_name),
            )
            for table, columns in (
                ("gpkg_contents", ("table_name",)),
                ("gpkg_geometry_columns", ("table_name",)),
                ("gpkg_extensions", ("table_name",)),
                ("gpkg_ogr_contents", ("table_name",)),
                ("gpkg_data_columns", ("table_name",)),
                ("gpkg_metadata_reference", ("table_name",)),
                ("gpkgext_relations", ("base_table_name", "related_table_name", "mapping_table_name")),
            ):
                if not _table_exists(cursor, table):
                    continue
                for column in columns:
                    cursor.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new_name, old_name)
                    )
            cursor.execute("COMMIT")
        except BaseException:
            cursor.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _drop_gpkg_layer(path: Path, layer: str) -> None:
    """Remove a GPKG table with its spatial index and registry rows in one transaction."""
    import sqlite3

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            row = cursor.execute(
                "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (layer,)
            ).fetchone()
            cursor.execute(f"DROP TABLE {quote_ident(layer)}")
            if row and _table_exists(cursor, f"rtree_{layer}_{row[0]}"):
                cursor.execute(f"DROP TABLE {quote_ident(f'rtree_{layer}_{row[0]}')}")
            for table, columns in (
                ("gpkg_geometry_columns", ("table_name",)),
                ("gpkg_extensions", ("table_name",)),
                ("gpkg_ogr_contents", ("table_name",)),
                ("gpkg_data_columns", ("table_name",)),
                ("gpkg_metadata_reference", ("table_name",)),
                ("gpkgext_relations", ("base_table_name", "related_table_name", "mapping_table_name")),
                ("layer_styles", ("f_table_name",)),
                ("gpkg_contents", ("table_name",)),
            ):
                if not _table_exists(cursor, table):
                    continue
                for column in columns:
                    cursor.execute(f"DELETE FROM {table} WHERE {column} = ?", (layer,))
            cursor.execute("COMMIT")
        except BaseException:
            cursor.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def cmd_rename(args: argparse.Namespace) -> None:
    input_path = resolve_path(args.input)
    guard_gdb_write(input_path, "layer rename in", "Copy the layer into a .gpkg under the new name (gpkg.py copy --source in.gdb --source-layer OLD --target out.gpkg --target-layer NEW), or rename it in ArcGIS.")

    old_name = args.layer
    new_name = args.new_name

    layers = pyogrio.list_layers(input_path)
    names = [l[0] for l in layers]
    if old_name not in names:
        sys.exit(f"Error: layer '{old_name}' not found. Available: {', '.join(names)}")
    if new_name in names:
        sys.exit(f"Error: layer '{new_name}' already exists")
    if not new_name:
        sys.exit("Error: new layer name must not be empty")
    if input_path.suffix.lower() != ".gpkg":
        sys.exit("Error: layer rename is only supported for GeoPackage (.gpkg) files")

    try:
        _rename_gpkg_layer(input_path, old_name, new_name)
    except Exception as e:
        sys.exit(f"Error renaming layer '{old_name}': {e}")

    print(f"Renamed '{old_name}' -> '{new_name}' in {input_path}")


def cmd_copy(args: argparse.Namespace) -> None:
    source = resolve_path(args.source)
    target = resolve_path(args.target, must_exist=False)
    guard_gdb_write(target, "copy to", "Copy into a .gpkg target instead, e.g. --target out.gpkg.")

    source_layer = args.source_layer
    if source_layer is None:
        layers = pyogrio.list_layers(source)
        if len(layers) == 0:
            sys.exit("Error: no layers found in source file")
        source_layer = layers[0][0]

    target_layer = args.target_layer or source_layer

    try:
        refuse_existing_layer(target, target_layer, "--target-layer")
        count = copy_layer(source, source_layer, target, "GPKG", layer=target_layer, mode="a" if target.exists() else "w")
    except Exception as e:
        sys.exit(f"Error copying layer '{source_layer}': {e}")

    print(f"Copied '{source_layer}' -> '{target_layer}' ({count} features)")


def cmd_sql(args: argparse.Namespace) -> None:
    input_path = resolve_path(args.input)
    query = args.query
    if not looks_like_sql(query):
        sys.exit("Error: --query must be a full SQL statement, e.g. SELECT * FROM layer WHERE ...")

    try:
        sql_dialect = "OGRSQL" if is_gdb(input_path) else None
        gdf = pyogrio.read_dataframe(input_path, sql=query, sql_dialect=sql_dialect)
    except Exception as e:
        sys.exit(f"Error executing query: {e}")

    if args.output:
        output_path = resolve_path(args.output, must_exist=False)
        guard_gdb_write(output_path, "SQL output to", "Save the result to a .gpkg instead, e.g. --output result.gpkg.")
        output_layer = args.output_layer or "sql_result"
        mode = "a" if output_path.exists() and output_path.suffix.lower() == ".gpkg" else "w"
        driver = "GPKG" if output_path.suffix.lower() == ".gpkg" else None
        try:
            if driver:
                refuse_existing_layer(output_path, output_layer, "--output-layer")
                gdf.to_file(output_path, layer=output_layer, driver=driver, engine="pyogrio", mode=mode)
            else:
                gdf.to_file(output_path, engine="pyogrio")
        except Exception as e:
            sys.exit(f"Error writing SQL result: {e}")
        print(f"Saved {len(gdf)} features to {output_path} (layer: {output_layer})")
    else:
        if hasattr(gdf, "geometry") and gdf.geometry is not None:
            df = gdf.drop(columns=gdf.geometry.name, errors="ignore")
        else:
            df = gdf
        print(tabulate(df, headers="keys", tablefmt="simple", showindex=False))
        print(f"\n({len(gdf)} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gpkg",
        description="GeoPackage and File GDB management tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    layers_p = sub.add_parser("layers", help="List all layers")
    layers_p.add_argument("input", help="GPKG or GDB file")

    import_p = sub.add_parser("import", help="Import a layer into a GPKG")
    import_p.add_argument("--source", required=True, help="Source file")
    import_p.add_argument("--source-layer", default=None, help="Source layer name")
    import_p.add_argument("--target", required=True, help="Target GPKG file")
    import_p.add_argument("--target-layer", default=None, help="Target layer name")

    export_p = sub.add_parser("export", help="Export a layer to another format")
    export_p.add_argument("--input", required=True, help="Input GPKG or GDB file")
    export_p.add_argument("--layer", default=None, help="Layer to export")
    export_p.add_argument("--output", required=True, help="Output file path")

    drop_p = sub.add_parser("drop", help="Delete a layer from a GPKG")
    drop_p.add_argument("--input", required=True, help="GPKG file")
    drop_p.add_argument("--layer", required=True, help="Layer to drop")

    rename_p = sub.add_parser("rename", help="Rename a layer in a GPKG")
    rename_p.add_argument("--input", required=True, help="GPKG file")
    rename_p.add_argument("--layer", required=True, help="Current layer name")
    rename_p.add_argument("--new-name", required=True, help="New layer name")

    copy_p = sub.add_parser("copy", help="Copy a layer between GPKG/GDB files")
    copy_p.add_argument("--source", required=True, help="Source file")
    copy_p.add_argument("--source-layer", default=None, help="Source layer name")
    copy_p.add_argument("--target", required=True, help="Target GPKG file")
    copy_p.add_argument("--target-layer", default=None, help="Target layer name")

    sql_p = sub.add_parser("sql", help="Run SQL query against GPKG/GDB")
    sql_p.add_argument("--input", required=True, help="Input file")
    sql_p.add_argument("--query", required=True, help="SQL query string")
    sql_p.add_argument("--output", default=None, help="Save results to file")
    sql_p.add_argument("--output-layer", default=None, help="Output layer name")

    args = parser.parse_args()

    dispatch = {
        "layers": cmd_layers,
        "import": cmd_import,
        "export": cmd_export,
        "drop": cmd_drop,
        "rename": cmd_rename,
        "copy": cmd_copy,
        "sql": cmd_sql,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    import _stdio
    _stdio.utf8()
    main()
