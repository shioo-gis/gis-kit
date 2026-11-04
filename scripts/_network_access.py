"""Source-node topology, reviewed access connectors and three-state demand coverage.

No geometric crossing is inferred to be a junction. Every source vertex carries
an identity; coincident vertices with different IDs remain disconnected.
"""
import json
import math

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import substring, unary_union

from _core import reproject
from _daily import stable, number
from _metric import analysis_frame, validate_measurement_geometries


STATES = {'allowed', 'blocked', 'unknown'}


def frame(rows, crs):
    return (gpd.GeoDataFrame(rows, geometry='geometry', crs=crs) if rows else
            gpd.GeoDataFrame({'id': []}, geometry=[], crs=crs))


def state(value):
    if value not in STATES:
        raise ValueError('Access must be allowed, blocked or unknown')
    return value


def text_id(value):
    if pd.isna(value) or not str(value):
        raise ValueError('Missing node or level identity')
    return str(value)


def projected(layer, crs, types):
    if layer.crs is None:
        raise ValueError('Access layers require CRS')
    if not layer.empty:
        validate_measurement_geometries(layer)
        if layer.geometry.has_z.any() or not layer.geom_type.isin(types).all():
            raise ValueError('Unsupported access geometry')
    result = reproject(layer, crs, purpose='measure')
    if not result.empty:
        validate_measurement_geometries(result)
        if not all(math.isfinite(float(v)) for v in result.total_bounds):
            raise ValueError('Non-finite projected access coordinates')
    return result


def analyze(edges, origins, facilities, p, areas, barriers):
    allowed = {'topology', 'edge_id', 'vertex_nodes', 'level', 'access', 'direction',
               'speed_kmh', 'mode', 'analysis_crs', 'turn_restrictions',
               'cost_assumptions', 'travel_mode', 'origin_id', 'facility_id',
               'point_level', 'snap_m', 'budget', 'connector_speed_kmh',
               'demand_weight', 'facility_sets', 'max_pairs', 'max_comparisons',
               'access_assumptions'}
    if set(p) - allowed:
        raise ValueError('Unknown access-network parameter')
    if p['topology'] != 'source_vertices':
        raise ValueError('Unsupported topology')
    if (p['mode'] not in ('shortest', 'fastest') or
            p['turn_restrictions'] != 'not_modelled' or
            not p['cost_assumptions'] or not p['access_assumptions'] or not p['travel_mode']):
        raise ValueError('Explicit cost, access, travel mode and turn assumptions required')
    limit, budget = number(p['snap_m']), number(p['budget'])
    speed = number(p['connector_speed_kmh'])
    if limit < 0 or budget < 0 or speed <= 0:
        raise ValueError('Invalid budget, snap distance or connector speed')
    stable(edges, p['edge_id'])
    work, factor = analysis_frame(edges, p['analysis_crs'])
    if not work.geom_type.eq('LineString').all() or work.geometry.has_z.any():
        raise ValueError('Roads require 2D LineStrings')
    origins = projected(origins, work.crs, ['Point'])
    facilities = projected(facilities, work.crs, ['Point'])
    areas = projected(areas, work.crs, ['Polygon', 'MultiPolygon'])
    barriers = projected(barriers, work.crs, ['Point', 'LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'])
    # Ancillary layers use fixed, generic fields, independent of source road fields.
    for layer in (areas, barriers):
        for _, r in layer.iterrows():
            text_id(r['level']); state(r['access'])
    stable(origins, p['origin_id']); stable(facilities, p['facility_id'])
    max_pairs = int(p.get('max_pairs', 10000))
    if max_pairs < 1 or len(origins) * len(facilities) > max_pairs:
        raise ValueError('OD guard exceeded')
    coords, base = {}, []
    for _, r in work.assign(_key=work[p['edge_id']].astype(str)).sort_values('_key').iterrows():
        nodes = r[p['vertex_nodes']]
        nodes = json.loads(nodes) if isinstance(nodes, str) else nodes
        xy = list(r.geometry.coords)
        if not isinstance(nodes, list) or len(nodes) != len(xy):
            raise ValueError('vertex_nodes must match every source coordinate')
        nodes = [text_id(n) for n in nodes]
        direction = r[p['direction']]
        if direction not in ('both', 'forward', 'reverse'):
            raise ValueError('Direction must be both, forward or reverse')
        access, level = state(r[p['access']]), text_id(r[p['level']])
        road_speed = number(r[p['speed_kmh']]) if p['mode'] == 'fastest' else None
        if road_speed is not None and road_speed <= 0:
            raise ValueError('Positive road speed required')
        for node, pos in zip(nodes, xy):
            node = 'source:' + node
            if node in coords and Point(coords[node]).distance(Point(pos)) * factor > 1e-6:
                raise ValueError('Source node has inconsistent coordinates')
            coords[node] = pos
        for i, (a, b) in enumerate(zip(xy, xy[1:])):
            line = LineString([a, b])
            if line.length <= 0 or nodes[i] == nodes[i+1]:
                raise ValueError('Zero-length or self-loop source segment')
            base.append(dict(id=json.dumps([r['_key'], i], separators=(',', ':')),
                             edge_id=r['_key'], u='source:'+nodes[i], v='source:'+nodes[i+1],
                             level=level, access=access, direction=direction,
                             speed=road_speed, geometry=line, cuts={0.: 'source:'+nodes[i], 1.: 'source:'+nodes[i+1]}))
    comparisons = int(p.get('max_comparisons', 2000000))
    if comparisons < 1 or (len(base) * (len(origins)+len(facilities)+len(barriers)) +
                           (len(origins)+len(facilities)) * (len(areas)+len(barriers))) > comparisons:
        raise ValueError('Access comparison guard exceeded')
    # Obstacles constrain both road travel and off-network connectors. A barrier
    # touching a segment closes that complete source segment (conservative).
    def constrained(line, level, initial):
        result = initial
        for _, b in barriers.iterrows():
            if str(b['level']) == level and line.intersects(b.geometry):
                if b['access'] == 'blocked':
                    return 'blocked'
                if b['access'] == 'unknown' and result != 'blocked':
                    result = 'unknown'
        return result

    for segment in base:
        segment['access'] = constrained(segment['geometry'], segment['level'], segment['access'])

    envelopes = {}
    for level in sorted({str(v) for v in areas.get('level', [])}):
        envelopes[level] = {access: unary_union([r.geometry for _, r in areas.iterrows()
                            if str(r['level']) == level and r['access'] == access])
                            for access in STATES}
    connectors = []
    def attach(points, key, kind):
        result = []
        for _, r in points.assign(_key=points[key].astype(str)).sort_values('_key').iterrows():
            level = text_id(r[p['point_level']]); candidates = []
            # Nearest eligible segment on the declared level; blocked streets
            # are ineligible. Unknown streets remain possible, never confirmed.
            for i, s in enumerate(base):
                if s['level'] != level or s['access'] == 'blocked':
                    continue
                t = s['geometry'].project(r.geometry, normalized=True)
                foot = s['geometry'].interpolate(t, normalized=True)
                distance = foot.distance(r.geometry)*factor
                if distance <= limit:
                    candidates.append((distance, s['id'], i, t, foot))
            node, distance, status, cost = None, None, 'unknown', None
            reason = 'no_eligible_segment_within_limit'
            for distance0, _, i, t, foot in sorted(candidates):
                s = base[i]
                connector = LineString([r.geometry, foot]) if distance0 else r.geometry
                # A supplied polygon is an explicit reviewed permission envelope;
                # outside it, legality is unknown. Never infer permission from
                # absence of a wall/water feature.
                envelope = envelopes.get(level)
                permission = 'unknown'
                if envelope is not None:
                    if envelope['allowed'].covers(connector):
                        permission = 'allowed'
                    if envelope['unknown'].intersects(connector):
                        permission = 'unknown'
                    if envelope['blocked'].intersects(connector):
                        permission = 'blocked'
                permission = constrained(connector, level, permission)
                if permission == 'blocked':
                    reason = 'connectors_blocked'; continue
                node = s['cuts'].get(t)
                if node is None:
                    node = 'split:' + s['id'] + ':' + float(t).hex()
                    s['cuts'][t] = node; coords[node] = (foot.x, foot.y)
                distance = distance0
                status = permission
                cost = distance if p['mode'] == 'shortest' else distance/(speed/3.6)
                reason = 'reviewed_connector' if status == 'allowed' else 'access_unknown'
                if distance:
                    connectors.append(dict(id=kind+':'+r['_key'], source_id=r['_key'], kind=kind,
                                           node=node, access=status, length_m=distance, cost=cost,
                                           geometry=connector))
                break
            result.append(dict(source_id=r['_key'], node=node, snap_m=distance,
                               access=status, connector_cost=cost, reason=reason))
        return result

    osnap = attach(origins, p['origin_id'], 'origin')
    fsnap = attach(facilities, p['facility_id'], 'facility')
    confirmed, possible = nx.MultiDiGraph(), nx.MultiDiGraph()
    confirmed.add_nodes_from(coords); possible.add_nodes_from(coords)
    arcs = {}
    for s in base:
        cuts = sorted(s['cuts'].items())
        for j, ((start, u), (end, v)) in enumerate(zip(cuts, cuts[1:])):
            line = substring(s['geometry'], start, end, normalized=True)
            cost = line.length * factor
            if p['mode'] == 'fastest':
                cost /= s['speed']/3.6
            directions = ([(u, v, False), (v, u, True)] if s['direction'] == 'both' else
                          [(u, v, False)] if s['direction'] == 'forward' else [(v, u, True)])
            for a, b, reverse in directions:
                k = json.dumps([s['id'], j, 'r' if reverse else 'f'], separators=(',', ':'))
                arcs[k] = dict(id=k, u=a, v=b, edge_id=s['edge_id'], level=s['level'],
                               access=s['access'], cost=cost, length_m=line.length*factor,
                               geometry=LineString(list(line.coords)[::-1]) if reverse else line)
                if s['access'] != 'blocked':
                    possible.add_edge(a, b, key=k, cost=cost)
                if s['access'] == 'allowed':
                    confirmed.add_edge(a, b, key=k, cost=cost)
    routes, od, service = [], [], []
    for o in osnap:
        known_dist, paths = (nx.single_source_dijkstra(confirmed, o['node'], weight='cost')
                             if o['node'] is not None and o['access'] == 'allowed' else ({}, {}))
        maybe = (nx.single_source_dijkstra_path_length(possible, o['node'], weight='cost')
                 if o['node'] is not None else {})
        for f in fsnap:
            net = known_dist.get(f['node']) if f['access'] == 'allowed' else None
            optimistic = maybe.get(f['node'])
            access_cost = ((o['connector_cost'] + f['connector_cost'])
                           if o['node'] is not None and f['node'] is not None else None)
            cost = net + access_cost if net is not None else None
            lower = optimistic + access_cost if optimistic is not None else None
            status = ('covered' if cost is not None and cost <= budget else
                      'unknown' if access_cost is None or (lower is not None and lower <= budget) else
                      'uncovered')
            keys = []
            if net is not None:
                path = paths[f['node']]
                keys = [min(confirmed[a][b], key=lambda k: (arcs[k]['cost'], k))
                        for a, b in zip(path, path[1:])]
                if not math.isclose(sum(arcs[k]['cost'] for k in keys), net, rel_tol=1e-12, abs_tol=1e-8):
                    raise ValueError('Route cost readback mismatch')
                if keys:
                    routes.append(dict(id=o['source_id']+':'+f['source_id'], origin_id=o['source_id'],
                                       facility_id=f['source_id'], cost=cost, network_cost=net,
                                       edge_keys=json.dumps(keys), geometry=MultiLineString([arcs[k]['geometry'] for k in keys])))
            od.append(dict(origin_id=o['source_id'], facility_id=f['source_id'], status=status,
                           cost=cost, network_cost=net, connector_cost=access_cost,
                           possible_cost=lower, edge_keys=keys, within_budget=status == 'covered'))
        for k, arc in arcs.items():
            remain = budget - known_dist.get(arc['u'], math.inf) - (o['connector_cost'] or 0)
            if arc['access'] == 'allowed' and remain > 0:
                fraction = min(1., remain/arc['cost'])
                service.append(dict(id=o['source_id']+':'+k, origin_id=o['source_id'], edge_key=k,
                                    fraction=fraction, geometry=substring(arc['geometry'], 0, fraction, normalized=True)))
    weights = None
    if p.get('demand_weight'):
        weights = {str(r[p['origin_id']]): number(r[p['demand_weight']]) for _, r in origins.iterrows()}
        if any(w < 0 for w in weights.values()):
            raise ValueError('Demand weights must be finite and nonnegative')
    ids = {f['source_id'] for f in fsnap}
    sets = p.get('facility_sets', {'all': sorted(ids)})
    coverage = {}
    for name, selected in sets.items():
        if not isinstance(selected, list) or len(set(selected)) != len(selected) or not set(selected) <= ids:
            raise ValueError('Facility sets require unique known IDs')
        rows = []
        for o in osnap:
            entries = [r for r in od if r['origin_id'] == o['source_id'] and r['facility_id'] in selected]
            status = ('covered' if any(r['status'] == 'covered' for r in entries) else
                      'unknown' if any(r['status'] == 'unknown' for r in entries) else 'uncovered')
            rows.append(dict(origin_id=o['source_id'], status=status,
                             weight=weights[o['source_id']] if weights is not None else None))
        summary = dict(demand=rows, counts={s: sum(r['status'] == s for r in rows)
                                         for s in ('covered', 'uncovered', 'unknown')})
        if weights is not None:
            totals = {s: math.fsum(r['weight'] for r in rows if r['status'] == s)
                      for s in ('covered', 'uncovered', 'unknown')}
            total = math.fsum(weights.values())
            summary.update(weights=totals, total_weight=total,
                           coverage_rate=totals['covered']/total if total else None,
                           possible_coverage_rate=(totals['covered']+totals['unknown'])/total if total else None)
        coverage[name] = summary
    details = dict(od=od, coverage=coverage, origin_snaps=osnap, facility_snaps=fsnap,
                   units='metres' if p['mode'] == 'shortest' else 'seconds',
                   demand_status='provided' if weights is not None else 'not_provided',
                   weak_components=nx.number_weakly_connected_components(confirmed),
                   snap_rule='nearest nonblocked segment on declared level; reviewed envelope; distance included',
                   topology='source vertex identities; geometric crossings never imply connections',
                   barrier_rule='any intersection closes or makes unknown the entire source vertex segment',
                   turns='not modelled', service_geometry='origin-outbound reachable lines; no service polygons')
    return frame(routes, work.crs), frame(service, work.crs), details, {
        'graph': frame(list(arcs.values()), work.crs),
        'connectors': frame(connectors, work.crs)}
