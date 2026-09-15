"""
Turning a parking strip into individual bays.

A bay is about 5 m long and 2.5 m wide, so the short side of the strip says
which of the two runs along its length:

    depth ~ 2.5  ->  bays lie head to tail   -> cut every 5.0 m  (parallel)
    depth ~ 5.0  ->  bays lie side by side   -> cut every 2.5 m  (perpendicular)

The length alone cannot decide this: a multiple of 5 is also a multiple of 2.5.

Once the spacing is known the number of bays is round(length / spacing) and the
strip is divided into that many equal parts, so drawing slack of a few
centimetres is spread out instead of left as a sliver at one end.

plan_bays() is pure arithmetic and is unit-tested; strip_axis() is the only
part that needs QGIS.
"""

import math

DEFAULT_BAY_LENGTH = 5.0        # metres, along the car
DEFAULT_BAY_WIDTH = 2.5         # metres, across the car

# Below these a bay stops being usable, so rounding down to fewer, roomier bays
# beats rounding to the nearest whole number.
DEFAULT_MIN_BAY_LENGTH = 4.80
DEFAULT_MIN_BAY_WIDTH = 2.40

# Upper sanity bound, so a strip can never collapse into one huge bay just to
# satisfy the minimum.
MAX_BAY_FACTOR = 1.5

PARALLEL = "parallel"           # langsparkeren
PERPENDICULAR = "perpendicular"  # haaksparkeren
AMBIGUOUS = "ambiguous"

_DEPTH_TOLERANCE = 0.20         # relative; 2.5 m +/- 0.5, 5.0 m +/- 1.0
_REMAINDER_WARNING = 0.15       # fraction of a bay left over before we complain
_FIT_WARNING = 0.90             # polygon area / bounding box area


class BayPlan:
    """What to do with one strip, and what looks wrong about it."""

    __slots__ = ("orientation", "spacing", "count", "actual", "length",
                 "depth", "fit", "warnings")

    def __init__(self, orientation, spacing, count, actual, length, depth, fit, warnings):
        self.orientation = orientation
        self.spacing = spacing
        self.count = count
        self.actual = actual
        self.length = length
        self.depth = depth
        self.fit = fit
        self.warnings = warnings

    @property
    def ok(self):
        return self.orientation != AMBIGUOUS and self.count >= 1

    def describe(self, unit="m"):
        if self.orientation == AMBIGUOUS:
            return (f"{self.length:.1f} x {self.depth:.1f} {unit}: depth matches "
                    f"neither a bay width nor a bay length")
        kind = "parallel" if self.orientation == PARALLEL else "perpendicular"
        across = self.depth
        return (f"{self.length:.1f} x {self.depth:.1f} {unit} -> {self.count} "
                f"{kind} bays of {self.actual:.2f} x {across:.2f} {unit}")


def choose_count(length, spacing, minimum, maximum):
    """How many bays to cut, and whether the result is acceptable.

    Plain rounding gives whichever count is nearest, which on a 17.86 m strip
    means four bays of 4.47 m where three of 5.95 m are what anyone would
    actually paint. So both neighbouring counts are considered and the ones
    that land inside [minimum, maximum] win; the nominal size only decides
    between them. If neither fits, the size closest to nominal is used and the
    caller is told.
    """
    if length <= 0.0 or spacing <= 0.0:
        return 1, False

    raw = length / spacing
    candidates = sorted({max(1, int(math.floor(raw))), max(1, int(math.ceil(raw)))})

    acceptable = [c for c in candidates if minimum <= length / c <= maximum]
    if acceptable:
        # among acceptable counts, the one whose bays are closest to nominal
        best = min(acceptable, key=lambda c: abs(length / c - spacing))
        return best, True

    best = min(candidates, key=lambda c: abs(length / c - spacing))
    return best, False


def plan_bays(length, depth, bay_length=DEFAULT_BAY_LENGTH,
              bay_width=DEFAULT_BAY_WIDTH, fit=None, orientation=None,
              min_bay_length=DEFAULT_MIN_BAY_LENGTH,
              min_bay_width=DEFAULT_MIN_BAY_WIDTH):
    """Work out how many bays fit in a strip of `length` by `depth`.

    length, depth    the long and short side of the strip, in map units (metres)
    orientation      force PARALLEL or PERPENDICULAR instead of inferring it
    fit              polygon area divided by bounding box area, if known
    min_bay_*        shortest bay still worth painting, per direction
    """
    warnings = []

    if length <= 0.0 or depth <= 0.0:
        return BayPlan(AMBIGUOUS, None, 0, 0.0, length, depth, fit,
                       ["Strip has no measurable size."])

    if orientation in (PARALLEL, PERPENDICULAR):
        chosen = orientation
    else:
        off_parallel = abs(depth - bay_width) / bay_width
        off_perpendicular = abs(depth - bay_length) / bay_length
        parallel_ok = off_parallel <= _DEPTH_TOLERANCE
        perpendicular_ok = off_perpendicular <= _DEPTH_TOLERANCE

        if parallel_ok and not perpendicular_ok:
            chosen = PARALLEL
        elif perpendicular_ok and not parallel_ok:
            chosen = PERPENDICULAR
        else:
            return BayPlan(
                AMBIGUOUS, None, 0, 0.0, length, depth, fit,
                [f"Depth {depth:.2f} m sits between a bay width ({bay_width:.2f} m) "
                 f"and a bay length ({bay_length:.2f} m); pick the orientation "
                 f"yourself."]
            )

    if chosen == PARALLEL:
        spacing, minimum = bay_length, min_bay_length
    else:
        spacing, minimum = bay_width, min_bay_width
    maximum = spacing * MAX_BAY_FACTOR

    raw = length / spacing
    count, within_limits = choose_count(length, spacing, minimum, maximum)
    actual = length / count

    if not within_limits:
        warnings.append(
            f"Strip is {length:.2f} m, which is {raw:.2f} bays of {spacing:.2f} m. "
            f"No whole number of bays lands between {minimum:.2f} and "
            f"{maximum:.2f} m, so they become {actual:.2f} m; check the polygon."
        )
    elif abs(actual - spacing) > spacing * _REMAINDER_WARNING:
        warnings.append(
            f"Strip is {length:.2f} m, so {count} bays of {actual:.2f} m instead of "
            f"the nominal {spacing:.2f} m."
        )
    if count == 1:
        warnings.append(f"Strip holds only one bay of {actual:.2f} m.")
    if fit is not None and fit < _FIT_WARNING:
        warnings.append(
            f"Strip fills only {fit * 100.0:.0f}% of its bounding rectangle, so it "
            f"is probably curved or irregular; draw the direction line by hand."
        )

    return BayPlan(chosen, spacing, count, actual, length, depth, fit, warnings)



# ---------------------------------------------------------------------------
# Strip axis: the direction that makes the strip narrowest
# ---------------------------------------------------------------------------

def convex_hull(points):
    """Andrew's monotone chain. Returns the hull counter-clockwise, open."""
    pts = sorted(set((round(x, 9), round(y, 9)) for x, y in points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_width_axis(hull):
    """Direction along which the shape is longest, by minimising its width.

    The minimum-width strip around a convex polygon always has one side flush
    with a hull edge, so trying every edge is enough.  For a parking strip that
    flush edge is its long kerb, which is exactly the axis we want: bays are
    then cut across it.

    Returns (along, across, length, width, base) with `along` and `across` unit
    vectors and `base` a point on the axis.
    """
    if len(hull) < 3:
        if len(hull) < 2:
            return None
        (x0, y0), (x1, y1) = hull[0], hull[1]
        span = math.hypot(x1 - x0, y1 - y0)
        if span <= 0:
            return None
        along = ((x1 - x0) / span, (y1 - y0) / span)
        return along, (-along[1], along[0]), span, 0.0, hull[0]

    best = None
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        ex, ey = bx - ax, by - ay
        edge_len = math.hypot(ex, ey)
        if edge_len <= 0.0:
            continue
        ux, uy = ex / edge_len, ey / edge_len
        nx, ny = -uy, ux

        along_min = along_max = 0.0
        width = 0.0
        for px, py in hull:
            dx, dy = px - ax, py - ay
            along_min = min(along_min, dx * ux + dy * uy)
            along_max = max(along_max, dx * ux + dy * uy)
            width = max(width, abs(dx * nx + dy * ny))

        if best is None or width < best[0]:
            best = (width, (ux, uy), (nx, ny), along_max - along_min, (ax, ay))

    if best is None:
        return None
    width, along, across, length, base = best
    return along, across, length, width, base


# ---------------------------------------------------------------------------
# QGIS-facing part
# ---------------------------------------------------------------------------

from qgis.core import QgsPointXY  # noqa: E402


def strip_axis(geom, measure=None):
    """Measure a strip and give the direction its bays should be cut in.

    Returns (cut_points, length, depth, fit):

      cut_points  a pair of QgsPointXY along the direction of the cuts, which
                  is across the strip
      length      size along the strip, in metres
      depth       size across the strip, in metres
      fit         polygon area over bounding rectangle area; well below 1 means
                  the strip is curved or ragged and a straight axis is a poor
                  description of it

    The axis comes from the polygon's own convex hull rather than from
    QgsGeometry.orientedMinimumBoundingBox(), so the behaviour is the same
    everywhere and can be tested outside QGIS.  `measure` converts a pair of
    points to metres; without it coordinates are taken as metres.

    Returns None if the shape cannot be measured.
    """
    try:
        vertices = _vertices_of(geom)
    except Exception:
        return None
    if len(vertices) < 3:
        return None

    axis = min_width_axis(convex_hull(vertices))
    if axis is None:
        return None
    along, across, length_units, depth_units, base = axis
    if length_units <= 0.0 or depth_units <= 0.0:
        return None

    bx, by = base
    cut_points = [
        QgsPointXY(bx, by),
        QgsPointXY(bx + across[0] * depth_units, by + across[1] * depth_units),
    ]

    if measure is None:
        length, depth = length_units, depth_units
    else:
        try:
            length = measure(
                QgsPointXY(bx, by),
                QgsPointXY(bx + along[0] * length_units, by + along[1] * length_units),
            )
            depth = measure(cut_points[0], cut_points[1])
        except Exception:
            length, depth = length_units, depth_units

    fit = None
    try:
        rectangle = length_units * depth_units
        if rectangle > 0.0:
            fit = geom.area() / rectangle
    except Exception:
        pass

    return cut_points, length, depth, fit


def axis_angle_degrees(cut_points):
    """Angle of the cut direction, for logging."""
    a, b = cut_points
    return math.degrees(math.atan2(b.y() - a.y(), b.x() - a.x()))


def _vertices_of(geom):
    """Every vertex of a (multi)polygon as (x, y) tuples."""
    polygons = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    points = []
    for polygon in polygons:
        for ring in polygon:
            for point in ring:
                points.append((point.x(), point.y()))
    return points
