# Equalyzer

A QGIS plugin that splits polygons into equal parts, into parts of a given
area, or into parking bays. Developed and tested on QGIS 4.2; QGIS 3.16 and
later are supported through a compatibility layer, but see the note under
*Tests*.

![Equalyzer in action](screencapture.gif)

## Install

Download the repository as a ZIP and use *Plugins ▸ Manage and Install
Plugins ▸ Install from ZIP*. GitHub's download unpacks to a folder named
`Equalyzer-main`; that works as it is, or rename it to `Equalyzer` first.

## Three modes

### Equal parts

1. Start *Split into Equal Parts*. Select a polygon first, or select nothing
   and let the direction line pick one (see below).
2. Draw a direction line on the map. Cuts run parallel to it.
3. Optionally click a start side; parts are numbered from there.
4. Enter the number of parts, preview, apply.

The parts have exactly equal area, to within rounding. The one exception is
a concave polygon cut across its arms; see *How the splitting works*.

### Equal areas

The same, with a target area per part instead of a count. The last part
takes the remainder.

### Parking bays

Select the strips, or select nothing and draw a line over the strip you
mean, then press *Split into Parking Bays*. There is nothing to draw or
type after that:

- each strip is measured along its own axis, so a whole selection can be
  cut in one go
- the short side decides the orientation: a strip about 2.5 m deep holds
  bays head to tail and is cut every 5 m; a strip about 5 m deep holds them
  side by side and is cut every 2.5 m
- the count follows from the length, with a minimum bay size: a 17.86 m
  strip becomes three bays of 5.95 m, not four of 4.47 m
- the strip is then divided into that many equal parts, so drawing slack
  is spread out instead of left as a sliver

The dialog shows the plan per strip before anything is created and flags a
length that is not close to a whole number of bays, a depth that matches
neither bay size (you pick the orientation yourself), and a strip that is
too curved for a straight axis. Bay sizes and minimums are configurable and
remembered.

## Picking a polygon with the line

In every mode, if nothing is selected when you draw the direction line, the
polygon the line runs through over the greatest length is selected in the
layer, so you can see what was picked before previewing. Ties go to the
smaller polygon. Redrawing the line over another polygon picks that one
instead. An existing selection always wins.

After the line is drawn, the map tool you were using before, typically *Add
Polygon Feature*, is active again, and so are your own snapping settings;
Equalyzer only switches vertex snapping on while you draw its line.

## Output

Parts go to a memory layer named `Split Parts – <source layer>`. A later
split of the same source layer is appended to it, so a session's work stays
in one place. The layer is recognised by a custom property, so renaming it
is fine.

Each part gets `source_fid`, `part_id` (numbered per source polygon),
`area_val` and `area_txt`, followed by a copy of every attribute of the
source feature. A source field whose name clashes with one of those four is
prefixed with `src_`.

In parking-bay mode you can instead replace the strip in its own layer by
its bays; the bays inherit every attribute of the strip (except fields the
provider owns, such as a GeoPackage `fid`) and are converted to the layer's
geometry type. Equalyzer never saves this for you: the change goes into the
layer's edit buffer as one named step, so a single Ctrl+Z takes it back and
*Save Layer Edits* keeps it. Edit mode is switched on if it was off. A strip
that holds only one bay is left as it is. The choice is remembered and
defaults to collecting in the Split Parts layer.

The Split Parts layer is a temporary layer. Save it with *Make Permanent* if
you want to keep it outside the project.

After a split the layer you split is the active layer again.

## Coordinate systems

Angles and distances only mean something in coordinates that are ground
metres. In degrees they are not: at 52° north a degree of longitude covers
62 % of the ground distance of a degree of latitude, so a right angle in the
coordinates is not a right angle on the ground. Web Mercator (EPSG:3857)
keeps angles but stretches lengths by 1/cos(latitude), 1.6 times in the
Netherlands, and a CRS in feet measures bays in the wrong unit.

Equalyzer checks this per selection by comparing a short segment's length
in the coordinates with its length on the ellipsoid. If they differ by more
than half a percent, each feature is transformed into the UTM zone of the
data, measured and cut there, and transformed back; the direction you drew
is converted along with it. Projected metric CRSs such as RD New or UTM pass
the check and are used as they are.

Areas follow the project's ellipsoid setting. When that is *None* but the
layer's coordinates are not ground metres, an ellipsoid is used anyway, so
labels never show square degrees or inflated Mercator areas.

## How the splitting works

After rotating the polygon so the cuts are horizontal, the width of a
cross-section is linear between consecutive vertex heights, so the area
below a cut is piecewise quadratic and every cut position is one
closed-form solve. Parts are produced by intersecting the polygon with band
polygons, which tile the plane, so they always add up to the original. If
the project measures on the ellipsoid, the cuts are refined with a few
Newton steps using dA/dy = width(y).

A part that would consist of several separate pieces (a concave polygon cut
across its arms) is handed to a connected partitioner instead, and the
result message says so. For parking strips the axis is the direction that
makes the strip narrowest, found on the polygon's convex hull.

Both engines have pure-Python cores in `equal_parts.py` and `parking.py`.

## Tests

From the plugin folder:

    python -m unittest discover -s tests -v

This needs shapely, which serves as ground truth, and does not need QGIS.
The tests in `test_engine.py` import the plugin itself against a stubbed
QGIS and also need PyQt6 or PyQt5; without one they are skipped.

The stubs model the QGIS API as the plugin uses it, so they catch logic
errors but not API differences between QGIS versions. The plugin has been
run on QGIS 4.2; if you use it on QGIS 3, please report problems. Inside
QGIS, `tests/qgis_console_check.py` runs the real engine on synthetic
polygons; instructions are at the top of that file.

## Diagnostics

Every step of a split can be logged to the *Equalyzer* tab of the Log
Messages panel. Enable it from the QGIS Python console and reload the
plugin:

    QSettings().setValue("Equalyzer/debug", True)

Errors always land in that panel with a full traceback.

## Known limitations

- Angled parking (schuinparkeren) is not recognised: a strip of such bays is
  treated as perpendicular and cut every 2.5 m along the kerb.
- A curved strip has no straight axis. The dialog warns when a strip fills
  less than 90 % of its bounding rectangle; split such a strip into straight
  pieces first.
- The metric work frame uses UTM, which is undefined beyond 84° north or
  south; there the layer's own coordinates are used.
- The animation at the top shows the original version of the plugin.

## Credits and licence

Originally written by Abel Koszeghy
(https://github.com/danzig666/Equalyzer). This fork rewrote the splitting
engine, added parking-bay mode and QGIS 4 support. MIT licence, see
`LICENSE`.
