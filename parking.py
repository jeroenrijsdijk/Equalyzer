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

Strips with slanted ends
------------------------
Where the two ends of a perpendicular strip are straight, parallel and slanted
(a parallelogram), the bays are painted along those ends: every bay is the same
parallelogram. The cuts then follow the ends instead of running square to the
kerb, and the bay width that has to fit a car is measured square to the cuts.
Ends that are slanted but not parallel cannot all be followed; such a strip is
cut square to the kerb and the dialog says so.

plan_bays() and end_slant() are pure arithmetic and are unit-tested;
measure_strip() and plan_strip() are the parts that need QGIS.
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

_END_EDGE_MIN_ANGLE = 20.0      # degrees from the kerb before an edge counts as an end
_END_COVERAGE = 0.80            # a straight end spans at least this much of the depth
_SQUARE_SLANT = 2.0             # degrees; ends closer to square than this are square
_PARALLEL_ENDS = 3.0            # degrees; ends this close to each other are parallel


class BayPlan:
    """What to do with one strip, and what looks wrong about it."""

    __slots__ = ("orientation", "spacing", "count", "actual", "length",
                 "depth", "fit", "warnings", "slant", "bay_depth")

    def __init__(self, orientation, spacing, count, actual, length, depth, fit, warnings):
        self.orientation = orientation
        self.spacing = spacing
        self.count = count
        self.actual = actual
        self.length = length
        self.depth = depth
        self.fit = fit
        self.warnings = warnings
        self.slant = 0.0            # degrees the cuts lean away from square to the kerb
        self.bay_depth = depth      # length of a bay along its cuts

    @property
    def ok(self):
        return self.orientation != AMBIGUOUS and self.count >= 1

    def describe(self, unit="m"):
        if self.orientation == AMBIGUOUS:
            return (f"{self.length:.1f} x {self.depth:.1f} {unit}: depth matches "
                    f"neither a bay width nor a bay length")
        kind = "parallel" if self.orientation == PARALLEL else "perpendicular"
        text = (f"{self.length:.1f} x {self.depth:.1f} {unit} -> {self.count} "
                f"{kind} bays of {self.actual:.2f} x {self.bay_depth:.2f} {unit}")
        if self.slant:
            text += f", along the slanted ends ({abs(self.slant):.0f}°)"
        return text


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
            f"Strip fills only {fit * 100.0:.0f}% of its bounding rectangle: slanted "
            f"ends, a curve or a ragged outline. Check the bays at the ends; a "
            f"curved strip is best split into straight pieces first."
        )

    return BayPlan(chosen, spacing, count, actual, length, depth, fit, warnings)



# ---------------------------------------------------------------------------
# Strip axis: the direction that makes the strip narrowest
# ---------------------------------------------------------------------------

def convex_hull(points):
    """Andrew's monotone chain. Returns the hull counter-clockwise, open."""
    pts = sorted(set((float(x), float(y)) for x, y in points))
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


def end_slant(hull, along, across, depth):
    """How far the strip's ends lean away from square, if they agree.

    hull          convex hull, counter-clockwise, as (x, y) tuples
    along/across  unit vectors along the kerb and across the strip
    depth         kerb-to-kerb width, in the hull's units

    An end is the hull edge on that side of the strip that spans most of the
    depth; it has to be straight (cover at least 80% of the depth) to count.
    Returns (slant, note): slant in degrees, positive when the ends lean
    towards `along`, or None when the ends are square, not straight, or not
    parallel; note explains the last case for the dialog, else None.
    """
    if len(hull) < 3 or depth <= 0.0:
        return None, None

    centre = sum(x * along[0] + y * along[1] for x, y in hull) / len(hull)
    min_cross = math.sin(math.radians(_END_EDGE_MIN_ANGLE))
    ends = {}                                   # side -> (coverage, slant)
    for i in range(len(hull)):
        (ax, ay), (bx, by) = hull[i], hull[(i + 1) % len(hull)]
        ex, ey = bx - ax, by - ay
        size = math.hypot(ex, ey)
        if size <= 0.0:
            continue
        a = (ex * along[0] + ey * along[1]) / size     # cosine with the kerb
        c = (ex * across[0] + ey * across[1]) / size   # cosine with the cut
        if abs(c) < min_cross:
            continue                                   # a kerb edge
        if c < 0:
            a, c = -a, -c
        slant = math.degrees(math.atan2(a, c))
        coverage = size * c / depth
        mid = ((ax + bx) / 2) * along[0] + ((ay + by) / 2) * along[1]
        side = "low" if mid < centre else "high"
        if side not in ends or coverage > ends[side][0]:
            ends[side] = (coverage, slant)

    if len(ends) < 2 or min(cov for cov, _ in ends.values()) < _END_COVERAGE:
        return None, None                              # rounded or ragged ends
    low, high = ends["low"][1], ends["high"][1]
    if abs(low) < _SQUARE_SLANT and abs(high) < _SQUARE_SLANT:
        return None, None                              # square ends
    if abs(low - high) > _PARALLEL_ENDS:
        return None, (f"The ends are not parallel ({abs(low):.0f}° and {abs(high):.0f}° "
                      "from square), so the bays are cut square to the kerb.")
    return (low + high) / 2.0, None


# ---------------------------------------------------------------------------
# QGIS-facing part
# ---------------------------------------------------------------------------

from qgis.core import QgsPointXY  # noqa: E402


class StripShape:
    """A measured strip: its size, and the directions it can be cut in."""

    __slots__ = ("square_cut_points", "end_cut_points", "length", "depth",
                 "fit", "end_fit", "slant", "ends_note")

    def __init__(self, square_cut_points, length, depth, fit,
                 end_cut_points=None, end_fit=None, slant=0.0, ends_note=None):
        self.square_cut_points = square_cut_points   # cuts square to the kerb
        self.end_cut_points = end_cut_points         # cuts along slanted ends, or None
        self.length = length          # usable length along the kerb, metres (area / depth)
        self.depth = depth            # kerb to kerb, metres
        self.fit = fit                # area / bounding rectangle
        self.end_fit = end_fit        # area / bounding parallelogram, for slanted ends
        self.slant = slant            # degrees the ends lean away from square
        self.ends_note = ends_note    # why slanted ends could not be followed


def measure_strip(geom, measure=None):
    """Measure a strip; see StripShape. Returns None if it cannot be measured.

    The kerb axis is the direction in which the strip is narrowest, found on
    its convex hull, so the behaviour does not depend on QGIS's bounding-box
    routines and can be tested without QGIS. `measure` converts a pair of
    points to metres; without it coordinates are taken as metres.

    The usable length is the area divided by the depth, so the length along
    the middle of the strip: slanted ends do not add bays this way.
    """
    try:
        vertices = _vertices_of(geom)
    except Exception:
        return None
    if len(vertices) < 3:
        return None

    hull = convex_hull(vertices)
    axis = min_width_axis(hull)
    if axis is None:
        return None
    along, across, length_units, depth_units, base = axis
    if length_units <= 0.0 or depth_units <= 0.0:
        return None

    bx, by = base
    square_cut_points = [
        QgsPointXY(bx, by),
        QgsPointXY(bx + across[0] * depth_units, by + across[1] * depth_units),
    ]

    try:
        area_units = geom.area()
    except Exception:
        area_units = 0.0
    usable_units = area_units / depth_units if area_units > 0.0 else length_units
    fit = area_units / (length_units * depth_units) if area_units > 0.0 else None

    if measure is None:
        length, depth = usable_units, depth_units
    else:
        try:
            full = measure(
                QgsPointXY(bx, by),
                QgsPointXY(bx + along[0] * length_units, by + along[1] * length_units),
            )
            length = usable_units * full / length_units
            depth = measure(square_cut_points[0], square_cut_points[1])
        except Exception:
            length, depth = usable_units, depth_units

    shape = StripShape(square_cut_points, length, depth, fit)

    slant, note = end_slant(hull, along, across, depth_units)
    shape.ends_note = note
    if slant is not None:
        rad = math.radians(slant)
        dx = across[0] * math.cos(rad) + along[0] * math.sin(rad)
        dy = across[1] * math.cos(rad) + along[1] * math.sin(rad)
        reach = depth_units / math.cos(rad)
        shape.end_cut_points = [QgsPointXY(bx, by), QgsPointXY(bx + dx * reach, by + dy * reach)]
        shape.slant = slant
        # bounding parallelogram: extent square to the cuts times the cut length
        nx, ny = -dy, dx
        spread = [x * nx + y * ny for x, y in hull]
        if area_units > 0.0:
            shape.end_fit = area_units / ((max(spread) - min(spread)) * reach)
    return shape


class StripPlan:
    """How one strip is to be cut: the bay plan and the cut direction."""

    __slots__ = ("shape", "plan", "cut_points")

    def __init__(self, shape, plan, cut_points):
        self.shape = shape
        self.plan = plan
        self.cut_points = cut_points


def plan_strip(geom, measure=None, **settings):
    """Measure a strip and plan its bays. Returns StripPlan, or None.

    `settings` are passed to plan_bays(): bay_length, bay_width, orientation,
    min_bay_length, min_bay_width.

    Perpendicular bays follow slanted ends when the ends are parallel. Each bay
    is then a parallelogram; the width that has to fit a car is the distance
    between two cuts, so the strip's length is taken square to the cuts.
    """
    shape = measure_strip(geom, measure)
    if shape is None:
        return None

    plan = plan_bays(shape.length, shape.depth, fit=shape.fit, **settings)
    if plan.orientation != PERPENDICULAR:
        return StripPlan(shape, plan, shape.square_cut_points)

    if shape.end_cut_points is None:
        if shape.ends_note:
            plan.warnings.append(shape.ends_note)
        return StripPlan(shape, plan, shape.square_cut_points)

    cos_slant = math.cos(math.radians(shape.slant))
    forced = dict(settings, orientation=PERPENDICULAR)
    slanted = plan_bays(shape.length * cos_slant, shape.depth, fit=shape.end_fit, **forced)
    slanted.slant = shape.slant
    slanted.bay_depth = shape.depth / cos_slant
    slanted.length = shape.length      # report the kerb length, as for any strip
    return StripPlan(shape, slanted, shape.end_cut_points)


def strip_axis(geom, measure=None):
    """(square_cut_points, length, depth, fit) of a strip, or None.

    Kept for callers that only need the kerb axis; see measure_strip().
    """
    shape = measure_strip(geom, measure)
    if shape is None:
        return None
    return shape.square_cut_points, shape.length, shape.depth, shape.fit


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
