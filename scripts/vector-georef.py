#!/usr/bin/env python3
"""Move vector data from a local plane grid to a projected CRS by a checked four-parameter or affine transform.

`fit` estimates the transform from control points whose fit/check roles are given in the CSV, reports fit and
independent check residuals against a threshold declared beforehand, and writes transform.json (pass or hold)
with the fit points' hull. `define` records official or user parameters, checked against points when given.
`apply` transforms every layer (or one) of a vector file, refuses a held transform unless --allow-hold, and marks
features outside the fit hull as extrapolated. Typical use: a CAD drawing in a city grid to CGCS2000. Only
4-parameter (shift, rotation, one scale) and 6-parameter affine models; no higher-order warping.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np

from _budget import check_vector
from _delivery import bundle, digest, write_json

VERSION = '0.1.0'
MODELS = {'helmert4': 3, 'affine6': 4}  # model -> fewest fit points, one more than the unknowns need
COLUMNS = ('id', 'role', 'src_x', 'src_y', 'dst_x', 'dst_y')


def crs_of(text):
    """(CRS or None, label); 'none' means a CRS-less plane in metres, such as cad.py output."""
    from pyproj import CRS
    if text is None or text.lower() == 'none':
        return None, None
    crs = CRS.from_user_input(text)
    unit = crs.axis_info[0].unit_name if crs.axis_info else None
    if not crs.is_projected or unit != 'metre':
        raise ValueError(f'{text} must be a projected CRS in metres; got {crs.name}')
    authority = crs.to_authority(min_confidence=70)
    return crs, ':'.join(authority) if authority else crs.to_wkt()


def read_points(path):
    with open(path, newline='', encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    if not rows or set(COLUMNS) - set(rows[0]):
        raise ValueError(f'{path} needs columns {", ".join(COLUMNS)} (note is optional)')
    ids = [r['id'].strip() for r in rows]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError('Control point ids must be unique and non-empty')
    roles = [r['role'].strip().lower() for r in rows]
    if set(roles) - {'fit', 'check'}:
        raise ValueError('Each control point role must be fit or check')
    try:
        src = np.array([[float(r['src_x']), float(r['src_y'])] for r in rows])
        dst = np.array([[float(r['dst_x']), float(r['dst_y'])] for r in rows])
    except ValueError as error:
        raise ValueError('Control point coordinates must be numbers') from error
    if not (np.isfinite(src).all() and np.isfinite(dst).all()):
        raise ValueError('Control point coordinates must be finite')
    if len({tuple(p) for p in src}) != len(rows):
        raise ValueError('Duplicate source coordinates (a point cannot be both fit and check)')
    return [{'id': i, 'role': role, 'note': (r.get('note') or '').strip() or None} for i, role, r in zip(ids, roles, rows)], src, dst


def estimate(model, src, dst):
    """2x3 matrix [[a, b, c], [d, e, f]] mapping (x, y) to (a x + b y + c, d x + e y + f), fitted on centred coordinates."""
    centre_src, centre_dst = src.mean(axis=0), dst.mean(axis=0)
    s, t = src - centre_src, dst - centre_dst
    if model == 'helmert4':
        # x' = p x - q y, y' = q x + p y on centred coordinates
        design = np.vstack([np.column_stack((s[:, 0], -s[:, 1])), np.column_stack((s[:, 1], s[:, 0]))])
        (p, q), _, rank, _ = np.linalg.lstsq(design, np.concatenate((t[:, 0], t[:, 1])), rcond=None)
        if rank < 2:
            raise ValueError('Fit points are degenerate (all at one place)')
        linear = np.array([[p, -q], [q, p]])
    else:
        design = s
        coefficients, _, rank, _ = np.linalg.lstsq(design, t, rcond=None)
        if rank < 2 or np.linalg.cond(design) > 1e8:
            raise ValueError('Fit points are collinear or ill-conditioned for an affine fit')
        linear = coefficients.T
    if abs(np.linalg.det(linear)) < 1e-15:
        raise ValueError('Fitted transform is not invertible')
    offset = centre_dst - linear @ centre_src
    return np.column_stack((linear, offset))


def matrix_from(model, args):
    if model == 'helmert4':
        if None in (args.tx, args.ty, args.scale, args.rotation):
            raise ValueError('helmert4 needs --tx, --ty, --scale and --rotation (degrees, counter-clockwise)')
        angle = math.radians(args.rotation)
        p, q = args.scale*math.cos(angle), args.scale*math.sin(angle)
        return np.array([[p, -q, args.tx], [q, p, args.ty]])
    if not args.matrix:
        raise ValueError('affine6 needs --matrix a,b,c,d,e,f')
    values = [float(v) for v in args.matrix.split(',')]
    if len(values) != 6:
        raise ValueError('--matrix takes six numbers a,b,c,d,e,f')
    return np.array([values[:3], values[3:]])


def describe(matrix):
    linear = matrix[:, :2]
    sx, sy = math.hypot(linear[0, 0], linear[1, 0]), math.hypot(linear[0, 1], linear[1, 1])
    return {'scale_x': sx, 'scale_y': sy, 'rotation_deg': math.degrees(math.atan2(linear[1, 0], linear[0, 0])),
            'shift': matrix[:, 2].tolist(), 'determinant': float(np.linalg.det(linear))}


def apply_matrix(matrix, xy):
    return xy @ matrix[:, :2].T + matrix[:, 2]


def move_xy(matrix, coordinates):
    """Nx2 or Nx3 coordinates; the transform is planar, so a Z stays as it was."""
    moved = coordinates.copy()
    moved[:, :2] = apply_matrix(matrix, coordinates[:, :2])
    return moved


def residuals(matrix, points, src, dst):
    errors = np.linalg.norm(apply_matrix(matrix, src) - dst, axis=1)
    rows = [{**p, 'src': s.tolist(), 'dst': d.tolist(), 'predicted': apply_matrix(matrix, s[None])[0].tolist(),
             'residual_m': float(e)} for p, s, d, e in zip(points, src, dst, errors)]

    def stats(role):
        mask = np.array([p['role'] == role for p in points])
        if not mask.any():
            return {'count': 0, 'rmse_m': None, 'max_m': None}
        return {'count': int(mask.sum()), 'rmse_m': float(np.sqrt(np.mean(errors[mask]**2))), 'max_m': float(errors[mask].max())}
    return rows, stats('fit'), stats('check')


def gate(check, args):
    reasons = []
    if not check['count']:
        reasons.append('No check points')
    else:
        if check['rmse_m'] > args.max_check_rmse:
            reasons.append(f'Check RMSE {check["rmse_m"]:.3f} m exceeds --max-check-rmse {args.max_check_rmse}')
        if args.max_check_error is not None and check['max_m'] > args.max_check_error:
            reasons.append(f'Check maximum {check["max_m"]:.3f} m exceeds --max-check-error {args.max_check_error}')
    return {'status': 'hold' if reasons else 'pass', 'reasons': reasons,
            'thresholds': {'max_check_rmse_m': args.max_check_rmse, 'max_check_error_m': args.max_check_error},
            'scope': 'declared residual thresholds on the given check points only; overlay with reference data still needed'}


def plot_residuals(rows, path, title, font):
    from _sandbox import configure_writable_caches
    configure_writable_caches()
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = [font, 'DejaVu Sans']
    figure, axis = plt.subplots(figsize=(9, 8))
    dst = np.array([r['dst'] for r in rows])
    error = np.array([np.array(r['dst']) - np.array(r['predicted']) for r in rows])
    span = max(np.ptp(dst[:, 0]), np.ptp(dst[:, 1]), 1.0)
    largest = max(float(np.linalg.norm(error, axis=1).max()), 1e-9)
    factor = 0.1*span/largest
    for role, colour in (('fit', '#1f77b4'), ('check', '#d62728')):
        mask = np.array([r['role'] == role for r in rows])
        if mask.any():
            axis.quiver(dst[mask, 0], dst[mask, 1], error[mask, 0]*factor, error[mask, 1]*factor, color=colour,
                        angles='xy', scale_units='xy', scale=1, width=0.004, label=f'{role} residual')
            axis.scatter(dst[mask, 0], dst[mask, 1], color=colour, s=16)
    for r, (x, y) in zip(rows, dst):
        axis.annotate(f'{r["id"]} {r["residual_m"]:.3f} m', (x, y), fontsize=7, xytext=(4, 4), textcoords='offset points')
    axis.update_datalim(np.vstack([dst, dst + error*factor]))  # quiver arrows do not widen the view themselves
    axis.autoscale_view()
    axis.margins(0.05)
    axis.set_aspect('equal', adjustable='datalim')
    axis.ticklabel_format(useOffset=False, style='plain')
    axis.tick_params(labelsize=7)
    axis.legend(loc='center left', bbox_to_anchor=(1.01, 0.5), fontsize=8, frameon=False)
    axis.set_title(f'{title}\nresidual arrows ×{factor:.3g}', fontsize=10)
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)


def font_name():
    try:
        from _theme import get_chinese_font
        return get_chinese_font()
    except ImportError:
        return 'DejaVu Sans'


def publish(args, model, matrix, origin, points=None, src=None, dst=None):
    from shapely.geometry import MultiPoint, mapping
    source_crs, source_id = crs_of(args.source_crs)
    target_crs, target_id = crs_of(args.target_crs)
    if target_crs is None:
        raise ValueError('--target-crs is required (a projected CRS in metres)')
    if not args.basis or not args.basis.strip():
        raise ValueError('--basis is required: where the parameters or control points come from')
    report = {'transform': 'vector-transform', 'schema_version': 1, 'model': model, 'matrix': matrix.tolist(),
              'parameters': describe(matrix), 'origin': origin, 'basis': args.basis.strip(),
              'source_crs': source_id, 'target_crs': target_id, 'target_crs_name': target_crs.name, 'units': 'metre'}
    rows = None
    if points is not None:
        rows, fit, check = residuals(matrix, points, src, dst)
        fit_src = src[[p['role'] == 'fit' for p in points]]
        report.update(control_points={'path': Path(args.gcp).name, 'sha256': digest(args.gcp), 'points': rows},
                      fit=fit, check=check, acceptance=gate(check, args),
                      fit_hull=mapping(MultiPoint(fit_src).convex_hull) if len(fit_src) >= 3 else None)
    else:
        report.update(control_points=None, fit=None, check=None, fit_hull=None,
                      acceptance={'status': 'hold', 'reasons': ['Parameters not checked against any control point'],
                                  'thresholds': None, 'scope': 'unchecked parameters'})
    with bundle(args.output) as stage:
        write_json(stage / 'transform.json', report)
        files = {'transform.json': digest(stage / 'transform.json')}
        if rows:
            plot_residuals(rows, stage / 'residuals.png', f'{model} · {origin} · {report["acceptance"]["status"]}', font_name())
            files['residuals.png'] = digest(stage / 'residuals.png')
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': f'vector-georef {args.command}',
                                            'version': VERSION,
                                            'parameters': {k: v for k, v in vars(args).items() if k not in ('output', 'command')},
                                            'files': files, 'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    return report


def fit(args):
    points, src, dst = read_points(args.gcp)
    fit_mask = np.array([p['role'] == 'fit' for p in points])
    if fit_mask.sum() < MODELS[args.model] or (~fit_mask).sum() < 1:
        raise ValueError(f'{args.model} needs at least {MODELS[args.model]} fit points (one more than the minimum, so '
                         'residuals mean something) and at least 1 check point')
    if args.max_check_rmse is None or args.max_check_rmse < 0:
        raise ValueError('--max-check-rmse must be given before fitting, in metres')
    matrix = estimate(args.model, src[fit_mask], dst[fit_mask])
    return publish(args, args.model, matrix, 'fitted', points, src, dst)


def define(args):
    matrix = matrix_from(args.model, args)
    if abs(np.linalg.det(matrix[:, :2])) < 1e-15:
        raise ValueError('Transform is not invertible')
    if args.gcp:
        if args.max_check_rmse is None or args.max_check_rmse < 0:
            raise ValueError('--max-check-rmse is required when checking against --gcp')
        points, src, dst = read_points(args.gcp)
        for p in points:  # nothing was fitted here: every given point is an independent check
            p['role'] = 'check'
        return publish(args, args.model, matrix, args.origin, points, src, dst)
    return publish(args, args.model, matrix, args.origin)


def apply(args):
    import warnings as python_warnings
    import geopandas as gpd
    import pyogrio
    import shapely
    from pyproj import CRS
    report = json.loads(Path(args.transform).read_text(encoding='utf-8'))
    if report.get('transform') != 'vector-transform' or report.get('schema_version') != 1:
        raise ValueError(f'{args.transform} is not a vector-georef transform.json v1')
    status = report['acceptance']['status']
    if status != 'pass' and not args.allow_hold:
        raise ValueError('Transform is on hold (' + '; '.join(report['acceptance']['reasons']) + '); pass --allow-hold for diagnostics')
    source = Path(args.source)
    names = [args.layer] if args.layer else [name for name, _ in pyogrio.list_layers(source)]
    matrix = np.array(report['matrix'])
    hull = shapely.geometry.shape(report['fit_hull']) if report.get('fit_hull') else None
    expected = CRS.from_user_input(report['source_crs']) if report.get('source_crs') else None
    target = CRS.from_user_input(report['target_crs'])
    frames, extrapolated, z_features = {}, {}, {}
    for name in names:
        check_vector(source, name)
        frame = gpd.read_file(source, layer=name, engine='pyogrio')
        if frame.crs is not None and (expected is None or not CRS.from_user_input(frame.crs).equals(expected)):
            raise ValueError(f'Layer {name} is in {CRS.from_user_input(frame.crs).name}, but the transform expects '
                             f'{expected.name if expected else "no CRS (a drawing plane)"}')
        if frame.crs is None and expected is not None:
            raise ValueError(f'Layer {name} has no CRS, but the transform expects {expected.name}')
        geometry = frame.geometry.to_numpy()
        if 'extrapolated' in frame.columns:
            raise ValueError(f'Layer {name} already has an "extrapolated" field')
        outside = (~shapely.covers(hull, geometry)) & ~shapely.is_empty(geometry) if hull is not None else np.zeros(len(frame), bool)
        moved = shapely.transform(geometry, lambda coordinates: move_xy(matrix, coordinates), include_z=None)
        out = gpd.GeoDataFrame(frame.drop(columns=frame.geometry.name), geometry=gpd.GeoSeries(moved), crs=target)
        out['extrapolated'] = outside if hull is not None else None
        frames[name], extrapolated[name], z_features[name] = out, int(outside.sum()), int(shapely.has_z(moved).sum())
    with bundle(args.output) as stage:
        target_path = stage / f'{source.stem}.gpkg'
        with python_warnings.catch_warnings():
            python_warnings.filterwarnings('ignore', message='.*geometry column.*')
            for name, frame in frames.items():
                pyogrio.write_dataframe(frame, target_path, layer=name, driver='GPKG')
        counts = {name: len(frame) for name, frame in frames.items()}
        back = {name: int(pyogrio.read_info(target_path, layer=name, force_feature_count=True)['features']) for name in frames}
        if back != counts:
            raise ValueError(f'Readback feature counts {back} differ from {counts}; nothing was published')
        manifest = {'manifest': 'vector-georef-apply', 'schema_version': 1,
                    'input': {'name': source.name, 'sha256': digest(source) if source.is_file() else None, 'layers': names},
                    'transform': {'path': str(Path(args.transform).absolute()), 'sha256': digest(args.transform),
                                  'model': report['model'], 'origin': report['origin'], 'status': status,
                                  'check': report.get('check')},
                    'target_crs': report['target_crs'], 'layers': counts, 'extrapolated': extrapolated, 'z_features': z_features,
                    'allow_hold': bool(args.allow_hold and status != 'pass'),
                    'output': {'path': target_path.name, 'sha256': digest(target_path)}}
        write_json(stage / 'manifest.json', manifest)
        write_json(stage / 'record.json', {'schema_version': 1, 'status': 'complete', 'tool': 'vector-georef apply', 'version': VERSION,
                                            'parameters': {k: v for k, v in vars(args).items() if k not in ('output', 'command')},
                                            'files': {'manifest.json': digest(stage / 'manifest.json'), target_path.name: manifest['output']['sha256']},
                                            'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                            'assumptions': ['X and Y are moved by one 2D transform; Z values are kept as they were, not scaled or shifted; M values are not read',
                                                            'Features not covered by the fit points\' hull are marked extrapolated']})
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    def common(sub):
        sub.add_argument('--model', choices=tuple(MODELS), default='helmert4', help='helmert4 (default) or affine6')
        sub.add_argument('--source-crs', default='none', help='CRS of the source coordinates; none for a CRS-less plane in metres (default)')
        sub.add_argument('--target-crs', required=True, help='Projected CRS in metres, e.g. EPSG:4547')
        sub.add_argument('--basis', required=True, help='Where the points or parameters come from')
        sub.add_argument('--max-check-rmse', type=float, help='Largest acceptable check-point RMSE in metres')
        sub.add_argument('--max-check-error', type=float, help='Largest acceptable single check-point error in metres')
        sub.add_argument('--output', required=True, help='New directory for transform.json, residuals.png and record.json')
    sub = commands.add_parser('fit', help='Fit a transform from control points with fit/check roles')
    sub.add_argument('--gcp', required=True, help='CSV with id, role (fit/check), src_x, src_y, dst_x, dst_y[, note]')
    common(sub)
    sub = commands.add_parser('define', help='Record official or user-given parameters, optionally checked against points')
    sub.add_argument('--origin', required=True, choices=('official', 'user'), help='official parameters or user-provided ones')
    sub.add_argument('--tx', type=float)
    sub.add_argument('--ty', type=float)
    sub.add_argument('--scale', type=float)
    sub.add_argument('--rotation', type=float, help='Degrees, counter-clockwise (helmert4)')
    sub.add_argument('--matrix', help='a,b,c,d,e,f for affine6: x\' = a x + b y + c, y\' = d x + e y + f')
    sub.add_argument('--gcp', help='Optional CSV of points to check the parameters against (all used as checks)')
    common(sub)
    sub = commands.add_parser('apply', help='Transform a vector file with a transform.json')
    sub.add_argument('source', help='Vector file; every layer is transformed unless --layer is given')
    sub.add_argument('--layer')
    sub.add_argument('--transform', required=True, help='transform.json from fit or define')
    sub.add_argument('--allow-hold', action='store_true', help='Apply a held transform anyway (recorded)')
    sub.add_argument('--output', required=True, help='New directory for <source>.gpkg, manifest.json and record.json')
    args = parser.parse_args()
    try:
        result = {'fit': fit, 'define': define, 'apply': apply}[args.command](args)
        if args.command == 'apply':
            summary = {'layers': result['layers'], 'extrapolated': result['extrapolated'], 'z_features': result['z_features'], 'transform_status': result['transform']['status']}
        else:
            summary = {'model': result['model'], 'origin': result['origin'], 'parameters': result['parameters'],
                       'check': result['check'], 'acceptance': result['acceptance']['status']}
        print(json.dumps({'output': str(Path(args.output).absolute()), **summary}, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    import _stdio
    _stdio.utf8()
    main()
