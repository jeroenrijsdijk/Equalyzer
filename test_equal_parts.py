"""
Self-test for the exact equal-parts splitter.

Run it from the QGIS Python console (Plugins > Python Console):

    exec(open(r'<path to plugin folder>/test_equal_parts.py').read())

No layer or project is needed: the test builds its own polygons and measures
planar areas, so it checks the geometry maths rather than your data.
"""

import math

from qgis.core import QgsDistanceArea, QgsGeometry, QgsPointXY

try:
    from Equalyzer.equal_parts import split_into_equal_parts
except ImportError:                      # plugin folder has another name
    import importlib, os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    split_into_equal_parts = importlib.import_module("equal_parts").split_into_equal_parts

TOLERANCE = 1e-6                         # relative area deviation we accept


def poly(*rings):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]
                                      for ring in rings])


def multipoly(*polygons):
    return QgsGeometry.fromMultiPolygonXY(
        [[[QgsPointXY(x, y) for x, y in ring] for ring in rings] for rings in polygons]
    )


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def circle(radius, segments=400):
    return [(math.cos(i / segments * 2 * math.pi) * radius,
             math.sin(i / segments * 2 * math.pi) * radius)
            for i in range(segments + 1)]


CASES = {
    "rectangle": poly(rect(0, 0, 100, 40)),
    "triangle": poly([(0, 0), (100, 0), (50, 60), (0, 0)]),
    "L-shape": poly([(0, 0), (60, 0), (60, 20), (20, 20), (20, 80), (0, 80), (0, 0)]),
    "U-shape": poly([(0, 0), (90, 0), (90, 70), (70, 70), (70, 25),
                     (20, 25), (20, 70), (0, 70), (0, 0)]),
    "donut": poly(rect(0, 0, 100, 100), rect(30, 30, 70, 70)),
    "two islands": multipoly([rect(0, 0, 30, 40)], [rect(50, 10, 90, 60)]),
    "400-gon": poly(circle(500)),
}

ANGLES = (0.0, 17.0, 45.0, 90.0, -33.0)
COUNTS = (2, 3, 5, 8)


def run():
    da = QgsDistanceArea()               # no ellipsoid: planar CRS units
    failures = 0
    checks = 0

    for name, geom in CASES.items():
        if not geom.isGeosValid():
            print(f"SKIP {name}: test geometry is invalid")
            continue
        total = da.measureArea(geom)
        for angle in ANGLES:
            for n in COUNTS:
                checks += 1
                try:
                    result = split_into_equal_parts(geom, n, angle, da.measureArea)
                except Exception as exc:
                    print(f"FAIL {name:<12} angle={angle:>6} n={n}: raised {exc!r}")
                    failures += 1
                    continue

                areas = [da.measureArea(p) for p in result.parts]
                problems = []
                if len(result.parts) != n:
                    problems.append(f"{len(result.parts)} parts instead of {n}")
                if abs(sum(areas) - total) > total * 1e-9:
                    problems.append(f"parts sum to {sum(areas):.6g}, polygon is {total:.6g}")
                if result.max_deviation > TOLERANCE:
                    problems.append(f"deviation {result.max_deviation:.3e}")

                if problems:
                    failures += 1
                    print(f"FAIL {name:<12} angle={angle:>6} n={n}: " + "; ".join(problems))
                elif result.disconnected:
                    # not an error: a concave polygon can genuinely fall apart
                    print(f"note {name:<12} angle={angle:>6} n={n}: "
                          f"{len(result.disconnected)} part(s) in separate pieces")

    print(f"\n{checks - failures}/{checks} checks passed"
          + ("" if failures == 0 else f", {failures} FAILED"))
    return failures == 0


run()
