# Equalyzer

Equalyzer is a QGIS Python plugin for splitting selected polygon features into equal-area parts or into a requested number of parts.

## Requirements

- QGIS 3.16 or newer, now suitabel for 4.x
- A polygon or multipolygon vector layer
- A projected CRS is recommended for accurate area work

## Usage

1. Select one or more polygon features is optional
2. Start either `Equal Area` or `Equal Parts` or 'parking bay' from the plugin menu or toolbar.
3. Draw a direction line on the map. Cut lines are created parallel to this line.
4. Optionally pick the side to start from.
5. Set the target area or number of parts.
6. Use `Preview` to inspect the result, then `Apply` to create the output layer.

The plugin creates a new temporary layer with these fields:

- `source_fid`
- `part_id`
- `area_val`
- `area_txt`
- and copies  all  fields from source polygon




## Splitting into N equal parts (since 1.6.0)

The cut positions are computed from the polygon's exact cumulative-area
profile: after rotating so the cuts are horizontal, the width of a
cross-section is linear between consecutive vertex ordinates, so the area
below a cut is piecewise quadratic and each cut is one closed-form solve.
The parts themselves are obtained by intersecting the polygon with band
polygons, which tile the plane, so the parts always add up to the original.

If the project measures on the ellipsoid, the cuts are refined with a few
Newton steps using dA/dy = width(y).

Parts that would consist of several separate pieces (a concave polygon cut
across its arms) are handed to the older connected partitioner instead, and
the plugin says so in the result message.

Run `test_equal_parts.py` from the QGIS Python console to check the engine
against a set of synthetic polygons without touching your own data.

## Choosing the polygon (since 1.6.1)

Selecting a polygon first is optional. If nothing is selected when you start
Equalyzer, draw the direction line across the polygon you want to split: the
polygon the line runs through over the greatest length is selected in the
layer, so you can see what was picked before previewing. Ties go to the
smaller polygon. If the line crosses nothing, Equalyzer falls back to the
polygon containing the first click and otherwise asks you to draw again.

An existing selection always wins, so splitting several polygons at once with
one direction keeps working as before.

## Parking bays (since 1.7.0)

A bay is about 5 m long and 2.5 m wide, so the short side of a strip says which
of the two runs along its length: a strip about 2.5 m deep holds bays head to
tail and is cut every 5 m, a strip about 5 m deep holds them side by side and is
cut every 2.5 m. The length cannot decide this on its own, because a multiple of
5 is also a multiple of 2.5.

Select the strips and press "Split into Parking Bays", or press it with nothing
selected and draw a line over the strip you mean: the line only points at the
polygon, the cut direction still comes from the strip itself. Each strip is measured
with its own oriented minimum bounding box, so the cut direction comes from the
strip itself and a whole selection can be processed at once. The number of bays is not simply round(length / spacing): a 17.86 m strip is
3.57 bays of 5 m, and four bays of 4.47 m is not something anyone paints. Both
neighbouring counts are considered, and a count whose bays land between the
minimum (4.80 m along the car, 2.40 m across it, both configurable) and 1.5
times the nominal size wins; the nominal size only decides between two
acceptable counts. If neither fits, the size closest to nominal is used and the
dialog says so. The strip is then divided into that many equal parts, so a few
centimetres of drawing slack are spread out instead of left as a sliver.

The dialog shows the plan per strip before anything is created, and flags:

- a length that is not close to a whole number of bays
- a depth that matches neither a bay width nor a bay length (you pick the
  orientation yourself)
- a strip that fills much less than its bounding rectangle, which usually means
  it is curved; draw the direction line by hand for those

Bay sizes are configurable and remembered between sessions.

## Geographic CRS

Angles and distances are meaningless in degrees: at 52 degrees north a degree of
longitude covers 62% of the ground distance of a degree of latitude, so a shape
that is rectangular on the ground is sheared in the coordinates. A perpendicular
computed there lands up to 25 degrees off, depending on the orientation of the
strip.

When the layer is in a geographic CRS, Equalyzer therefore transforms each
feature into the UTM zone of the data, measures and cuts it there, and
transforms the parts back. Projected layers such as RD New are used as they are.

## Output layer

Parts are written to a memory layer named "Split Parts - <source layer>". A
later split of the same source layer is appended to that layer rather than
creating a new one, so a session's work collects in one place. The layer is
recognised by a custom property, so renaming it is fine; it is tied to the
source layer because it carries that layer's fields.

Each part gets source_fid, part_id (numbered per source polygon), area_val and
area_txt, followed by a copy of every attribute of the source feature. A source
field whose name would clash with one of those four is prefixed with src_.

## Replacing the original

Drawing a strip into the real parking layer and then splitting it leaves the
strip behind. The bay dialog therefore offers three outcomes:

- collect the bays in the Split Parts layer (the original stays)
- collect them there and delete the original polygon
- replace the original in the source layer by its bays

In the third case the bays are written into the source layer itself and inherit
every attribute of the strip, except fields the provider owns such as a
GeoPackage fid. Adding the bays and removing the strip happen inside one named
edit command, so a single Ctrl+Z undoes the whole thing. If the layer was
already in edit mode the change joins that session and you save it yourself;
otherwise Equalyzer opens a session and commits it.

The choice is remembered. It defaults to the first option, because the other two
delete from a real data file.
