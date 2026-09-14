# Equalyzer

Equalyzer is a QGIS Python plugin for splitting selected polygon features into equal-area parts or into a requested number of parts.

## Requirements

- In this fork: Claude.AI rewrote parts of this plugin to make it qgis4 compatible
  > 
- A polygon or multipolygon vector layer
- A projected CRS is recommended for accurate area work

## Usage

1. Select one or more polygon features.
2. Start either `Equal Area` or `Equal Parts` from the plugin menu or toolbar.
3. Draw a direction line on the map. Cut lines are created parallel to this line.
4. Optionally pick the side to start from.
5. Set the target area or number of parts.
6. Use `Preview` to inspect the result, then `Apply` to create the output layer.

The plugin creates a new temporary layer with these fields:

- `source_fid`
- `part_id`
- `area_val`
- `area_txt`

## Splitting behavior

The primary splitter uses straight cuts and `QgsGeometry.splitGeometry()` so the original polygon boundary is preserved except where a real cut intersects it.

For area mode, full parts are created at the requested target area. Any remaining area is kept as the final part.

For difficult concave polygons, a straight cut may not be able to keep both sides as single connected polygons. In that case Equalyzer falls back to the connected splitter. The fallback is reported in Preview, Apply, and the QGIS message log. Fallback output can contain extra short boundary segments because it uses a strip-based connected partition.

## Notes

- Area values are measured with QGIS `QgsDistanceArea`.
- If ellipsoid measurement is enabled in the project, areas are measured using the project ellipsoid.
- Higher precision affects the connected fallback splitter, not the strict straight-cut path.

