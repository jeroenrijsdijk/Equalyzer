"""
Exact splitting of a polygon into N parts of equal area, with cuts parallel to
a user-given direction.

The method
----------
Rotate the polygon so the cuts become horizontal.  For a valid polygon the
width of a horizontal cross-section, w(y), is *linear* between two consecutive
vertex ordinates: within such a band no edge starts, ends or changes order.
So the cumulative area

    A(y) = integral of w from yMin to y

is piecewise quadratic and exactly known.  Finding the cut that puts a given
area below it is therefore one quadratic solve, not a binary search with a
geometry intersection per iteration.

Cutting itself is done by intersecting the polygon with band rectangles rather
than by QgsGeometry.splitGeometry().  Bands tile the plane, so the parts always
add up to the original polygon and there is no "the split did not happen"
failure mode.

If the project measures areas on the ellipsoid, planar and geodesic areas
differ.  The cut positions are then corrected with a few Newton steps that use
dA/dy = w(y), which converges in one or two passes at any sensible map scale.

The two functions below the "pure geometry" banner have no QGIS dependency and
are meant to be unit-tested on their own.
"""

import bisect
import math

__all__ = [
    "Profile",
    "build_profile",
    "area_below",
    "width_at",
    "solve_cuts",
    "EqualPartsResult",
    "split_into_equal_parts",
]


# ---------------------------------------------------------------------------
# Pure geometry: cumulative area profile in the rotated frame
# ---------------------------------------------------------------------------

class Profile:
    """Piecewise-quadratic cumulative area of a polygon along the y axis.

    bands[i] = (y_lo, y_hi, w_lo, w_hi, area)
    cum[i]   = area below bands[i][0]; cum[-1] == total area
    """

    __slots__ = ("bands", "cum", "y_min", "y_max", "total")

    def __init__(self, bands):
        self.bands = bands
        self.cum = [0.0]
        running = 0.0
        for band in bands:
            running += band[4]
            self.cum.append(running)
        self.total = running
        self.y_min = bands[0][0] if bands else 0.0
        self.y_max = bands[-1][1] if bands else 0.0

    def __repr__(self):
        return (f"Profile(bands={len(self.bands)}, total={self.total:.6g}, "
                f"y=[{self.y_min:.6g}, {self.y_max:.6g}])")


def _cross_width(active, y):
    """Total length of the cross-section at height y, given spanning edges.

    Uses the even-odd rule: sorted crossings pair up as inside intervals.
    Holes need no special treatment, their edges simply join the crossing list.
    """
    xs = []
    for x0, y0, x1, y1 in active:
        # edge spans the band, so y0 != y1 and y lies within it
        xs.append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
    if len(xs) < 2:
        return 0.0
    xs.sort()
    width = 0.0
    for i in range(0, len(xs) - 1, 2):
        width += xs[i + 1] - xs[i]
    return width


def build_profile(rings):
    """Build the cumulative area profile from polygon rings.

    rings: iterable of point sequences [(x, y), ...], exterior and interior
    alike; each ring may be open or closed.  Coordinates must already be in
    the rotated frame where cuts are horizontal.

    Raises ValueError if the rings enclose no area.
    """
    edges = []
    for ring in rings:
        pts = list(ring)
        if len(pts) < 3:
            continue
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if y0 != y1:                      # horizontal edges never span a band
                edges.append((x0, y0, x1, y1))

    if not edges:
        raise ValueError("No non-horizontal edges; geometry encloses no area.")

    boundaries = sorted({e[1] for e in edges} | {e[3] for e in edges})
    if len(boundaries) < 2:
        raise ValueError("Degenerate geometry: all vertices share one ordinate.")

    # Sweep with an active edge list so the cost stays near O(n log n) instead
    # of O(n * bands) on polygons with many vertices.
    by_low = sorted(edges, key=lambda e: min(e[1], e[3]))
    next_edge = 0
    active = []
    bands = []

    for y_lo, y_hi in zip(boundaries, boundaries[1:]):
        while next_edge < len(by_low) and min(by_low[next_edge][1], by_low[next_edge][3]) <= y_lo:
            active.append(by_low[next_edge])
            next_edge += 1
        active = [e for e in active if max(e[1], e[3]) >= y_hi]
        if not active:
            continue
        w_lo = _cross_width(active, y_lo)
        w_hi = _cross_width(active, y_hi)
        area = 0.5 * (w_lo + w_hi) * (y_hi - y_lo)
        if area > 0.0:
            bands.append((y_lo, y_hi, w_lo, w_hi, area))

    if not bands:
        raise ValueError("Geometry encloses no measurable area.")

    return Profile(bands)


def width_at(profile, y):
    """Cross-section width at height y (linear inside a band, 0 outside)."""
    if y <= profile.y_min or y >= profile.y_max:
        return 0.0
    index = bisect.bisect_right([b[0] for b in profile.bands], y) - 1
    index = max(0, min(index, len(profile.bands) - 1))
    y_lo, y_hi, w_lo, w_hi, _ = profile.bands[index]
    if y_hi <= y_lo:
        return w_lo
    t = (y - y_lo) / (y_hi - y_lo)
    return w_lo + (w_hi - w_lo) * max(0.0, min(1.0, t))


def area_below(profile, y):
    """Exact area of the polygon below height y."""
    if y <= profile.y_min:
        return 0.0
    if y >= profile.y_max:
        return profile.total
    index = bisect.bisect_right([b[0] for b in profile.bands], y) - 1
    index = max(0, min(index, len(profile.bands) - 1))
    y_lo, y_hi, w_lo, w_hi, _ = profile.bands[index]
    h = y_hi - y_lo
    if h <= 0.0:
        return profile.cum[index]
    t = max(0.0, min(1.0, (y - y_lo) / h))
    return profile.cum[index] + h * (w_lo * t + (w_hi - w_lo) * t * t / 2.0)


def _solve_band(y_lo, y_hi, w_lo, w_hi, wanted):
    """y inside one band where the area below it equals `wanted`.

    Area(t) = h * (w_lo*t + (w_hi - w_lo)*t^2/2) with t in [0, 1].
    """
    h = y_hi - y_lo
    if h <= 0.0:
        return y_lo
    a = 0.5 * h * (w_hi - w_lo)
    b = h * w_lo
    c = -wanted

    if abs(a) < 1e-12 * max(abs(b), 1.0):
        t = 0.0 if b == 0.0 else -c / b
    else:
        disc = b * b - 4.0 * a * c
        if disc < 0.0:
            disc = 0.0
        sq = math.sqrt(disc)
        # numerically stable pair of roots
        q = -0.5 * (b + math.copysign(sq, b if b != 0.0 else 1.0))
        roots = []
        if q != 0.0:
            roots.append(c / q)
        if a != 0.0 and q != 0.0:
            roots.append(q / a)
        inside = [r for r in roots if -1e-9 <= r <= 1.0 + 1e-9]
        t = min(inside) if inside else (roots[0] if roots else 0.0)

    t = max(0.0, min(1.0, t))
    return y_lo + t * h


def solve_cuts(profile, targets):
    """Cut heights for consecutive target areas.

    targets: areas of the parts, in profile units, in sweep order.
    Returns len(targets) - 1 strictly increasing y values.
    """
    if len(targets) < 2:
        return []

    cuts = []
    running = 0.0
    previous = profile.y_min
    for target in targets[:-1]:
        running += target
        wanted = min(running, profile.total)
        index = bisect.bisect_left(profile.cum, wanted) - 1
        index = max(0, min(index, len(profile.bands) - 1))
        y_lo, y_hi, w_lo, w_hi, _ = profile.bands[index]
        y = _solve_band(y_lo, y_hi, w_lo, w_hi, wanted - profile.cum[index])
        # keep the sequence strictly increasing even under numerical noise
        y = max(y, previous)
        cuts.append(y)
        previous = y
    return cuts


# ---------------------------------------------------------------------------
# QGIS-facing part
# ---------------------------------------------------------------------------

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes  # noqa: E402


class EqualPartsResult:
    """Outcome of one split, including everything needed to report on it."""

    __slots__ = ("parts", "areas", "max_deviation", "iterations",
                 "disconnected", "profile")

    def __init__(self, parts, areas, max_deviation, iterations, disconnected, profile):
        self.parts = parts
        self.areas = areas
        self.max_deviation = max_deviation      # relative, worst part
        self.iterations = iterations            # geodesic correction passes
        self.disconnected = disconnected        # indices of multi-piece parts
        self.profile = profile

    @property
    def ok(self):
        return bool(self.parts) and not self.disconnected

    def summary(self):
        return (f"{len(self.parts)} part(s), worst deviation "
                f"{self.max_deviation * 100.0:.4f}%, "
                f"{self.iterations} correction pass(es), "
                f"{len(self.disconnected)} disconnected")


def _rings_of(geom):
    """All rings of a (multi)polygon as lists of (x, y) tuples."""
    rings = []
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat == QgsWkbTypes.Type.MultiPolygon:
        polygons = geom.asMultiPolygon()
    elif flat == QgsWkbTypes.Type.Polygon:
        polygons = [geom.asPolygon()]
    else:
        raise ValueError(f"Not a polygon geometry (wkbType={geom.wkbType()}).")

    for polygon in polygons:
        for ring in polygon:
            rings.append([(p.x(), p.y()) for p in ring])
    return rings


def _polygonal_parts(geom):
    """Keep only the polygonal content of a geometry, repairing if needed."""
    if geom is None or geom.isEmpty():
        return []
    if not geom.isGeosValid():
        repaired = geom.makeValid()
        if repaired is not None and not repaired.isEmpty():
            geom = repaired
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat in (QgsWkbTypes.Type.Polygon, QgsWkbTypes.Type.MultiPolygon):
        return [geom]
    # GeometryCollection: touching bands can add stray lines or points
    try:
        collected = [QgsGeometry(part) for part in geom.asGeometryCollection()]
    except Exception:
        return []
    keep = []
    for part in collected:
        sub = QgsWkbTypes.flatType(part.wkbType())
        if sub in (QgsWkbTypes.Type.Polygon, QgsWkbTypes.Type.MultiPolygon):
            keep.append(part)
    return keep


def _band_geometry(y_lo, y_hi, x_min, x_max, center, rot_angle):
    """Rectangle covering one band, rotated back into the layer frame."""
    rect = QgsGeometry.fromRect(QgsRectangle(x_min, y_lo, x_max, y_hi))
    rect.rotate(-rot_angle, center)
    return rect


def _slice(geom, cuts, bbox, center, rot_angle):
    """Intersect geom with the bands defined by `cuts`, low to high."""
    margin = max(bbox.width(), bbox.height(), 1.0) * 2.0
    x_min = bbox.xMinimum() - margin
    x_max = bbox.xMaximum() + margin
    edges = [bbox.yMinimum() - margin] + list(cuts) + [bbox.yMaximum() + margin]

    parts = []
    for y_lo, y_hi in zip(edges, edges[1:]):
        band = _band_geometry(y_lo, y_hi, x_min, x_max, center, rot_angle)
        piece = geom.intersection(band)
        polygonal = _polygonal_parts(piece)
        if len(polygonal) == 1:
            parts.append(polygonal[0])
        elif polygonal:
            merged = QgsGeometry.unaryUnion(polygonal)
            parts.append(merged if merged and not merged.isEmpty() else polygonal[0])
        else:
            parts.append(QgsGeometry())
    return parts


def _is_connected(geom):
    if geom is None or geom.isEmpty():
        return False
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat == QgsWkbTypes.Type.Polygon:
        return True
    if flat == QgsWkbTypes.Type.MultiPolygon:
        try:
            return len(geom.asMultiPolygon()) == 1
        except Exception:
            return False
    return False


def split_into_equal_parts(geom, num_parts, rot_angle, measure_area,
                           sweep_from_low=True, tolerance=1e-6, max_passes=8):
    """Split `geom` into `num_parts` parts of equal area.

    geom          QgsGeometry, a valid (multi)polygon in layer CRS
    rot_angle     degrees, QGIS clockwise-positive; rotating the geometry by
                  this angle makes the cut lines horizontal
    measure_area  callable geometry -> area, normally QgsDistanceArea.measureArea
    sweep_from_low  number the parts from the low side of the rotated frame
    tolerance     accepted relative deviation before correction stops

    Returns an EqualPartsResult.  Raises ValueError on unusable input.
    """
    if num_parts < 2:
        return EqualPartsResult([QgsGeometry(geom)], [measure_area(geom)],
                                0.0, 0, [], None)

    center = geom.boundingBox().center()
    rotated = QgsGeometry(geom)
    rotated.rotate(rot_angle, center)
    if rotated.isEmpty():
        raise ValueError("Rotating the geometry produced an empty result.")

    profile = build_profile(_rings_of(rotated))
    bbox = rotated.boundingBox()

    planar_total = profile.total
    measured_total = measure_area(geom)
    if planar_total <= 0.0 or measured_total <= 0.0:
        raise ValueError("Geometry has no usable area.")

    cuts = solve_cuts(profile, [planar_total / num_parts] * num_parts)
    parts = _slice(geom, cuts, bbox, center, rot_angle)

    # Correct for the difference between planar and measured (geodesic) area.
    # dA/dy = w(y), so one Newton step per cut; sequential because cut i only
    # bounds parts i and i+1.  The planar-to-measured ratio is taken locally
    # around each cut rather than once for the whole polygon, which keeps the
    # step honest when the ratio drifts across the shape.
    passes = 0
    areas = [measure_area(p) for p in parts]
    deviation = _worst_deviation(areas)

    while deviation > tolerance and passes < max_passes:
        target = sum(areas) / num_parts
        planar = _planar_slice_areas(profile, cuts)
        running = 0.0
        moved = []
        for i, y in enumerate(cuts):
            running += areas[i]
            width = width_at(profile, y)
            if width <= 0.0:
                moved.append(y)
                continue
            measured_pair = areas[i] + areas[i + 1]
            planar_pair = planar[i] + planar[i + 1]
            if measured_pair > 0.0 and planar_pair > 0.0:
                scale = planar_pair / measured_pair
            else:
                scale = planar_total / measured_total
            shift = (target * (i + 1) - running) * scale / width
            moved.append(y + shift)
        # keep cuts ordered and inside the polygon
        cuts = _monotonic(moved, profile.y_min, profile.y_max)
        parts = _slice(geom, cuts, bbox, center, rot_angle)
        areas = [measure_area(p) for p in parts]
        new_deviation = _worst_deviation(areas)
        passes += 1
        if new_deviation >= deviation:          # not converging, keep best effort
            deviation = new_deviation
            break
        deviation = new_deviation

    if not sweep_from_low:
        parts = list(reversed(parts))
        areas = list(reversed(areas))

    disconnected = [i for i, p in enumerate(parts) if not _is_connected(p)]
    return EqualPartsResult(parts, areas, deviation, passes, disconnected, profile)


def _planar_slice_areas(profile, cuts):
    """Planar area of each slice, straight from the cumulative profile."""
    edges = [profile.y_min] + list(cuts) + [profile.y_max]
    return [max(0.0, area_below(profile, hi) - area_below(profile, lo))
            for lo, hi in zip(edges, edges[1:])]


def _worst_deviation(areas):
    total = sum(areas)
    if total <= 0.0 or not areas:
        return float("inf")
    target = total / len(areas)
    return max(abs(a - target) for a in areas) / target


def _monotonic(values, low, high):
    """Force a strictly increasing sequence strictly inside (low, high)."""
    span = high - low
    step = span * 1e-9 if span > 0 else 1e-9
    out = []
    previous = low
    for value in values:
        value = max(min(value, high - step), low + step)
        if value <= previous:
            value = previous + step
        out.append(value)
        previous = value
    return out
