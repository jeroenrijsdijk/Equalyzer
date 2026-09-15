"""
Equalyzer - Split Polygons into Equal Areas or Parts
Version 1.12.0
Author: Abel Koszeghy

Improvements in 1.5.0:
- Runs on QGIS 4 (Qt6) as well as QGIS 3 (Qt5)
- All Qt and QGIS enums use their fully scoped, version-neutral spelling
- QgsField creation, snapping configuration and vector file writing go
  through a single compatibility layer near the top of this file

Improvements in 1.4.5:
- Reworked target-area mode as a weighted connected partition
- Target-area mode now produces N full target-sized parts plus one remainder part
  without falling back to equal-count semantics or emitting tiny intermediate slivers

Improvements in 1.4.4:
- Fixed target-area mode so it creates repeated target-sized connected parts
  plus a final leftover part, instead of converting the target area into equal-count mode

Improvements in 1.4.2:
- Added saved Precision setting for concave connected splitting
- Higher precision uses finer strip graphs and more rebalancing iterations
- Saves and restores the dialog window geometry with QGIS settings

Improvements in 1.4.1:
- Removed unsupported QgsGeometry.boundary() call for older QGIS versions
- Uses bbox-overlap + QgsGeometry.distance() for strip adjacency
- Evaluates multiple strip resolutions and keeps the best balanced exact-count result

Improvements in 1.4.0:
- Replaced count-mode concave splitting with connected graph partitioning
- Produces exactly the requested number of connected output polygons
- Added boundary-node rebalancing to improve equal-area results on irregular shapes

Improvements in 1.2.17:
- Reworked split engine for better concave-polygon handling using
  cumulative-area slicing in rotated space
- Reduced topology instability from iterative remainder differencing

Improvements in 1.2.16:
- Apply is now enabled as soon as a valid direction line is set
  (Preview is optional)
- Clearing preview no longer disables Apply when direction is still available

Improvements in 1.2.15:
- Replaced blocking apply flow with signal-driven non-blocking dialog handling
  to bypass exec_ deadlock on Apply
- Split/output pipeline now runs from accepted callback with explicit
  "Apply callback entered" marker

Improvements in 1.2.14:
- Fixed Apply deadlock path by removing preview-band scene cleanup from dialog
  accept/close lifecycle (cleanup is deferred to plugin-level run boundaries)
- Added explicit marker after dialog returns from exec_ to confirm pipeline
  continuation

Improvements in 1.2.13:
- Added hard runtime markers (plugin load, Apply click, split entry) to
  diagnose silent Apply failures
- Made dialog Apply path exception-safe so preview cleanup cannot block
  execution of the split pipeline

Improvements in 1.2.12:
- Added visible apply/output diagnostics to QGIS message bar
- Restored MultiPolygon output workflow and normalized all written geometries
  to multipolygon-safe parts
- Output layer is now inserted into project before writing for deterministic
  visibility on Apply

Improvements in 1.2.11:
- Removed early-terminate apply path that could bypass emergency fallback
  when primary output feature list was empty
- Forced zero-feature apply runs through emergency temporary memory-layer
  creation path

Improvements in 1.2.9:
- Fixed oblique-direction cut orientation by aligning with QGIS clockwise
  rotation semantics
- Added explicit preview cleanup on dialog OK (accept)
- Simplified output layer schema for robust memory-layer feature insertion

Improvements in 1.2.10:
- Added emergency temporary memory-layer fallback if regular output add/write
  path fails
- Switched primary output geometry to Polygon for broader provider compatibility

Improvements in 1.2.8:
- Apply flow now always recomputes split from final dialog parameters
  (independent from preview cache/lifecycle)
- Output memory layer is written before adding to project for deterministic
  creation behavior
- Added explicit direction/cut angle diagnostics to log

Improvements in 1.2.7:
- Aligned cut orientation with drawn direction line (cuts parallel to line)
- Strengthened memory-layer output creation flow (layer added deterministically)
- Expanded global preview cleanup hooks for project open/new/clear

Improvements in 1.2.6:
- Apply now reuses the exact geometries computed in preview
- Improved split robustness for opposite sweep directions
- Hardened output-memory-layer writing for mixed polygon/multipolygon cases
- Added global preview-rubber-band cleanup (including project reset/new project)

Improvements in 1.2.5:
- Added robust output-layer insertion diagnostics to QGIS log
- Added fallback output creation via temporary GeoJSON when memory-layer
  insertion fails
- Added clearer user-facing error messages for write/add failures

Improvements in 1.2.4:
- Fixed apply/output generation by using one consistent split computation
  path for writing features
- Improved equal-area splitting so it does not stop too early
- Added explicit output-write failure checks and user feedback

Improvements in 1.2.3:
- Fixed cut orientation math so split lines are perpendicular to the
  drawn direction line
- Added validation for degenerate/too-short direction lines
- Improved direction status text to show both drawn direction and
  effective cut angle

Improvements in 1.2.2:
- Reworked interactive map picking to avoid nested event loops
- Fixed Windows focus/deadlock behavior where QGIS could freeze after
  selecting the second direction-line point

Improvements in 1.2.1:
- Fixed freeze/unresponsive behavior after selecting the second
  direction-line point

Improvements in 1.2:
- Fixed splitting direction logic (cuts are now perpendicular to the drawn line)
- Fixed CRS handling (transform always defined, applied consistently)
- Fixed starting side logic using projected coordinates in rotated space
- Added interactive preview dialog with rubber band display
- Preview shows split polygons with area labels
- Improved binary search precision (50 iterations)
- Fixed leftover merging threshold
- Better error handling and user feedback
"""

from qgis.PyQt.QtCore import Qt, QVariant, QSettings
from qgis.PyQt.QtWidgets import (
    QMessageBox, QInputDialog, QDialog, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox, QSpinBox,
    QGroupBox, QFormLayout, QDialogButtonBox, QSizePolicy, QFrame,
    QComboBox
)
from qgis.PyQt.QtGui import QIcon, QColor, QFont

# QAction lives in QtWidgets on Qt5 and in QtGui on Qt6.
try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction

try:
    from qgis.PyQt.QtCore import QMetaType
except ImportError:          # pragma: no cover - Qt5 builds without QMetaType
    QMetaType = None
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsGeometry, QgsWkbTypes,
    QgsFeature, QgsUnitTypes, QgsDistanceArea,
    QgsVectorLayerSimpleLabeling, QgsPalLayerSettings,
    QgsTextFormat, QgsRectangle, QgsPointXY, QgsSnappingConfig,
    QgsPoint, QgsTolerance, QgsMapLayer, QgsCoordinateTransform,
    QgsCoordinateReferenceSystem, QgsMessageLog, Qgis,
    QgsVectorFileWriter, QgsField, QgsFeatureRequest
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsSnapIndicator
from qgis.utils import iface

from .equal_parts import split_into_equal_parts
from .parking import (
    plan_bays, strip_axis, axis_angle_degrees,
    DEFAULT_BAY_LENGTH, DEFAULT_BAY_WIDTH,
    DEFAULT_MIN_BAY_LENGTH, DEFAULT_MIN_BAY_WIDTH,
    PARALLEL, PERPENDICULAR,
)
import math
import os
import tempfile
import uuid


# ---------------------------------------------------------------------------
# QGIS 3 / QGIS 4 compatibility layer
#
# QGIS 4 runs on Qt6, where PyQt enums are strictly scoped: Qt.red no longer
# exists, only Qt.GlobalColor.red.  The scoped spelling also works on Qt5, so
# everywhere else in this file enums are simply written in their scoped form.
#
# Only the handful of cases that need a real runtime decision live here.  When
# support for QGIS < 3.38 is eventually dropped, this whole block can go.
# ---------------------------------------------------------------------------

QGIS_VERSION_INT = Qgis.QGIS_VERSION_INT


def _resolve_enum(*candidates):
    """Return the first spelling of an enum value that this QGIS provides.

    Each candidate is (owner, holder_name_or_None, member_name).  QGIS 3.30+
    moved many enums into the Qgis namespace and renamed their members
    (QgsUnitTypes.AreaSquareMeters -> Qgis.AreaUnit.SquareMeters), keeping the
    old names as aliases.  Asking instead of assuming keeps one code path.
    """
    for owner, holder_name, member in candidates:
        holder = owner if holder_name is None else getattr(owner, holder_name, None)
        if holder is not None and hasattr(holder, member):
            return getattr(holder, member)
    raise AttributeError(f"No known spelling for {candidates[0]}")


def _area_unit(new_member, legacy_member):
    return _resolve_enum(
        (Qgis, "AreaUnit", new_member),
        (QgsUnitTypes, "AreaUnit", legacy_member),
        (QgsUnitTypes, None, legacy_member),
    )


def _distance_unit(new_member, legacy_member):
    return _resolve_enum(
        (Qgis, "DistanceUnit", new_member),
        (QgsUnitTypes, "DistanceUnit", legacy_member),
        (QgsUnitTypes, None, legacy_member),
    )


AREA_SQUARE_METERS = _area_unit("SquareMeters", "AreaSquareMeters")
AREA_SQUARE_FEET = _area_unit("SquareFeet", "AreaSquareFeet")
AREA_SQUARE_DEGREES = _area_unit("SquareDegrees", "AreaSquareDegrees")
DISTANCE_METERS = _distance_unit("Meters", "DistanceMeters")
DISTANCE_FEET = _distance_unit("Feet", "DistanceFeet")
DISTANCE_DEGREES = _distance_unit("Degrees", "DistanceDegrees")

# Geometry / WKB / layer types: Qgis.* names since 3.30, legacy names before.
WKB_POLYGON = _resolve_enum(
    (Qgis, "WkbType", "Polygon"),
    (QgsWkbTypes, "Type", "Polygon"),
    (QgsWkbTypes, None, "Polygon"),
)
WKB_MULTIPOLYGON = _resolve_enum(
    (Qgis, "WkbType", "MultiPolygon"),
    (QgsWkbTypes, "Type", "MultiPolygon"),
    (QgsWkbTypes, None, "MultiPolygon"),
)
WKB_GEOMETRYCOLLECTION = _resolve_enum(
    (Qgis, "WkbType", "GeometryCollection"),
    (QgsWkbTypes, "Type", "GeometryCollection"),
    (QgsWkbTypes, None, "GeometryCollection"),
)
GEOM_LINE = _resolve_enum(
    (Qgis, "GeometryType", "Line"),
    (QgsWkbTypes, "GeometryType", "LineGeometry"),
    (QgsWkbTypes, None, "LineGeometry"),
)
GEOM_POLYGON = _resolve_enum(
    (Qgis, "GeometryType", "Polygon"),
    (QgsWkbTypes, "GeometryType", "PolygonGeometry"),
    (QgsWkbTypes, None, "PolygonGeometry"),
)
LAYER_VECTOR = _resolve_enum(
    (Qgis, "LayerType", "Vector"),
    (QgsMapLayer, "LayerType", "VectorLayer"),
    (QgsMapLayer, None, "VectorLayer"),
)

# Diagnostics: writes to the "Equalyzer" tab of the QGIS Log Messages panel.
# Set to False once the splitting problem is understood.
DEBUG = True


_dbg_counters = {}


def dbg_limited(key, limit, message):
    """Log at most `limit` messages per key, so hot loops cannot flood the log."""
    count = _dbg_counters.get(key, 0)
    if count < limit:
        _dbg_counters[key] = count + 1
        dbg(message)
    elif count == limit:
        _dbg_counters[key] = count + 1
        dbg(f"({key}: further messages suppressed)")


def dbg(message):
    if DEBUG:
        try:
            QgsMessageLog.logMessage(str(message), "Equalyzer", Qgis.MessageLevel.Info)
        except Exception:
            pass


# (QMetaType.Type member, QVariant member) per logical field type
_FIELD_TYPE_NAMES = {
    "longlong": ("LongLong", "LongLong"),
    "int": ("Int", "Int"),
    "double": ("Double", "Double"),
    "string": ("QString", "String"),
}


def compat_field(name, type_key):
    """Build a QgsField without depending on QVariant.Type (gone in Qt6).

    QGIS >= 3.38 accepts QMetaType.Type; older releases only accept
    QVariant.Type, which PyQt5 still provides.
    """
    meta_name, variant_name = _FIELD_TYPE_NAMES[type_key]
    if QMetaType is not None and QGIS_VERSION_INT >= 33800:
        return QgsField(name, getattr(QMetaType.Type, meta_name))
    return QgsField(name, getattr(QVariant, variant_name))


def compat_snapping_enums():
    """Return (snapping_mode, vertex_type, tolerance_unit) for this QGIS.

    The Qgis.* spellings exist since 3.26 and are the only ones left in
    QGIS 4; the QgsSnappingConfig.*/QgsTolerance.* ones cover 3.16 - 3.24.
    """
    mode = _resolve_enum(
        (Qgis, "SnappingMode", "AdvancedConfiguration"),
        (QgsSnappingConfig, "SnappingMode", "AdvancedConfiguration"),
        (QgsSnappingConfig, None, "AdvancedConfiguration"),
    )
    vertex = _resolve_enum(
        (Qgis, "SnappingType", "Vertex"),
        (QgsSnappingConfig, "SnappingType", "Vertex"),
        (QgsSnappingConfig, None, "Vertex"),
    )
    unit = _resolve_enum(
        (Qgis, "MapToolUnit", "Pixels"),
        (QgsTolerance, "UnitType", "Pixels"),
        (QgsTolerance, None, "Pixels"),
    )
    return mode, vertex, unit


def pick_feature_under_line(layer, points):
    """The polygon a two-point line runs through the most.

    Longest intersection wins, because that survives a line drawn from outside
    the polygon to outside it. Ties go to the smaller polygon, which is the
    more specific one where features overlap.
    """
    if not points or len(points) < 2:
        return None

    line = QgsGeometry.fromPolylineXY(list(points))
    search = line.boundingBox()
    search.grow(max(search.width(), search.height(), 1.0) * 1e-6)
    request = QgsFeatureRequest().setFilterRect(search)

    best = None
    best_key = None
    for feature in layer.getFeatures(request):
        geom = feature.geometry()
        if geom is None or geom.isEmpty() or geom.type() != GEOM_POLYGON:
            continue
        try:
            shared = line.intersection(geom)
            length = 0.0 if shared is None or shared.isEmpty() else shared.length()
            if length <= 0.0 and not geom.intersects(line):
                continue
        except Exception:
            continue
        key = (-length, geom.area())
        if best_key is None or key < best_key:
            best, best_key = feature, key

    if best is not None:
        return best

    start = QgsGeometry.fromPointXY(points[0])
    for feature in layer.getFeatures(QgsFeatureRequest().setFilterRect(start.boundingBox())):
        geom = feature.geometry()
        if geom is not None and geom.type() == GEOM_POLYGON and geom.contains(start):
            return feature
    return None


PLUGIN_FIELD_NAMES = ("source_fid", "part_id", "area_val", "area_txt")


def make_distance_area(layer):
    """Area calculator that returns square metres for any layer.

    The source CRS must be set before the ellipsoid, or the ellipsoidal
    transform is built against the wrong CRS. For a geographic layer an
    ellipsoid is forced, otherwise measureArea() would return square degrees.
    """
    da = QgsDistanceArea()
    context = QgsProject.instance().transformContext()
    da.setSourceCrs(layer.crs(), context)

    ellipsoid = QgsProject.instance().ellipsoid()
    if layer.crs().isGeographic() and (not ellipsoid or ellipsoid.upper() == "NONE"):
        ellipsoid = layer.crs().ellipsoidAcronym() or "WGS84"
    if ellipsoid:
        try:
            da.setEllipsoid(ellipsoid)
        except Exception as e:
            dbg(f"setEllipsoid({ellipsoid}) failed: {e!r}")
    return da


def build_output_fields(source_layer):
    """Equalyzer's own fields plus a copy of every field of the source layer.

    Returns (fields, mapping) where mapping pairs a source field index with the
    name it got in the output, since a source field may need renaming to avoid
    clashing with one of Equalyzer's own.
    """
    fields = [
        compat_field("source_fid", "longlong"),
        compat_field("part_id", "int"),
        compat_field("area_val", "double"),
        compat_field("area_txt", "string"),
    ]
    taken = set(PLUGIN_FIELD_NAMES)
    mapping = []
    for index, field in enumerate(source_layer.fields()):
        copy = QgsField(field)
        name = field.name()
        if name in taken:
            name = f"src_{name}"
            suffix = 2
            while name in taken:
                name = f"src_{field.name()}_{suffix}"
                suffix += 1
            copy.setName(name)
        taken.add(copy.name())
        mapping.append((index, copy.name()))
        fields.append(copy)
    return fields, mapping


def find_split_parts_layer(source_layer, field_names):
    """An existing Split Parts layer this run can be appended to, if any.

    Tied to the source layer, because the output carries that layer's fields.
    """
    for candidate in QgsProject.instance().mapLayers().values():
        try:
            if candidate.customProperty("equalyzer/split_parts") != "1":
                continue
            if candidate.customProperty("equalyzer/source_layer") != source_layer.id():
                continue
            if candidate.crs().authid() != source_layer.crs().authid():
                continue
            if [f.name() for f in candidate.fields()] != field_names:
                continue
            if not candidate.isValid():
                continue
            return candidate
        except Exception:
            continue
    return None


def metric_work_transforms(layer, sample_point):
    """Transforms to and from a metric frame, or (None, None, None).

    Geometry in a geographic CRS cannot be reasoned about with straight lines:
    at 52 degrees north a degree of longitude is only 62% of a degree of
    latitude, so right angles in the coordinates are not right angles on the
    ground. Everything that involves angles or distances is therefore done in
    the UTM zone of the data and transformed back afterwards.
    """
    crs = layer.crs()
    if not crs.isGeographic():
        return None, None, None
    try:
        lon, lat = sample_point.x(), sample_point.y()
        zone = max(1, min(60, int((lon + 180.0) / 6.0) + 1))
        epsg = (32600 if lat >= 0 else 32700) + zone
        work = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not work.isValid():
            return None, None, None
        context = QgsProject.instance().transformContext()
        return (QgsCoordinateTransform(crs, work, context),
                QgsCoordinateTransform(work, crs, context),
                work)
    except Exception as e:
        dbg(f"metric_work_transforms failed: {e!r}")
        return None, None, None


def to_work_crs(geom, transform):
    """Copy of geom in the work frame (or an unchanged copy without one)."""
    copy = QgsGeometry(geom)
    if transform is not None:
        try:
            copy.transform(transform)
        except Exception as e:
            dbg(f"transform to work CRS failed: {e!r}")
    return copy


def metric_length_measure(layer):
    """Callable (a, b) -> length in metres, whatever the layer CRS is.

    Parking bays are specified in metres, so strip dimensions must be measured
    in metres even when the layer sits in degrees.
    """
    crs = layer.crs()
    da = QgsDistanceArea()
    try:
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
    except Exception:
        pass

    ellipsoid = QgsProject.instance().ellipsoid()
    if crs.isGeographic() and (not ellipsoid or ellipsoid.upper() == "NONE"):
        ellipsoid = crs.ellipsoidAcronym() or "WGS84"
    if ellipsoid and ellipsoid.upper() != "NONE":
        try:
            da.setEllipsoid(ellipsoid)
        except Exception:
            pass

    def measure(point_a, point_b):
        value = da.measureLine(point_a, point_b)
        try:
            unit = da.lengthUnits()
            if unit != DISTANCE_METERS:
                value *= QgsUnitTypes.fromUnitToUnitFactor(unit, DISTANCE_METERS)
        except Exception:
            pass
        return value

    return measure


def compat_write_vector(layer, path, driver="GeoJSON", encoding="UTF-8"):
    """Write a layer to file. Returns (error_code, message).

    writeAsVectorFormatV3() is available since QGIS 3.20; the pre-3.20
    signature is kept as a fallback.
    """
    if hasattr(QgsVectorFileWriter, "writeAsVectorFormatV3"):
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        options.fileEncoding = encoding
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsProject.instance().transformContext(), options
        )
    else:  # pragma: no cover - QGIS < 3.20
        result = QgsVectorFileWriter.writeAsVectorFormat(
            layer, path, encoding, layer.crs(), driver
        )

    if isinstance(result, tuple):
        return result[0], result[1] if len(result) > 1 else ""
    return result, ""


# ---------------------------------------------------------------------------
# Map Tools
# ---------------------------------------------------------------------------

class LineDrawTool(QgsMapToolEmitPoint):
    """Interactive tool: user clicks two points to define a direction line."""

    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, GEOM_LINE)
        self.rubber_band.setColor(QColor(Qt.GlobalColor.red))
        self.rubber_band.setWidth(2)

        self.snapping_utils = canvas.snappingUtils()
        self.snapping_config = QgsSnappingConfig()
        try:
            snap_mode, snap_vertex, snap_unit = compat_snapping_enums()
            self.snapping_config.setMode(snap_mode)
            self.snapping_config.setEnabled(True)

            root = QgsProject.instance().layerTreeRoot()
            for tree_layer in root.findLayers():
                if tree_layer.isVisible():
                    layer = tree_layer.layer()
                    if layer is not None and layer.type() == LAYER_VECTOR:
                        settings = QgsSnappingConfig.IndividualLayerSettings(
                            True, snap_vertex, 10, snap_unit
                        )
                        self.snapping_config.setIndividualLayerSettings(layer, settings)

            self.snapping_utils.setConfig(self.snapping_config)
        except Exception as e:
            # Snapping is a convenience, not a requirement: never block the tool.
            QgsMessageLog.logMessage(
                f"Snapping setup skipped: {e}", "Equalyzer", Qgis.MessageLevel.Warning
            )
        self.snap_indicator = QgsSnapIndicator(canvas)
        iface.messageBar().pushInfo(
            "Equalyzer",
            "Click two points to define the cut direction (snaps to vertices). "
            "Right-click or Escape to abort."
        )

    def canvasMoveEvent(self, event):
        map_pos = self.toMapCoordinates(event.pos())
        match = self.snapping_utils.snapToMap(map_pos)
        self.snap_indicator.setMatch(match)
        if len(self.points) == 1:
            self.rubber_band.reset(GEOM_LINE)
            self.rubber_band.addPoint(self.points[0])
            self.rubber_band.addPoint(match.point() if match.isValid() else map_pos)

    def canvasReleaseEvent(self, event):
        try:
            if event.button() == Qt.MouseButton.RightButton:
                self._abort()
                return
            map_pos = self.toMapCoordinates(event.pos())
            match = self.snapping_utils.snapToMap(map_pos)
            point = match.point() if match.isValid() else map_pos
            self.points.append(point)
            if len(self.points) == 1:
                iface.messageBar().pushInfo("Equalyzer", "Click second point for direction line.")
            elif len(self.points) == 2:
                self.rubber_band.reset(GEOM_LINE)
                self.canvas.unsetMapTool(self)
                self.callback(self.points)
        except Exception as e:
            iface.messageBar().pushCritical(
                "Equalyzer",
                f"Direction line capture failed: {e}"
            )
            self._abort()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._abort()

    def _abort(self):
        self.rubber_band.reset(GEOM_LINE)
        self.points = []
        self.canvas.unsetMapTool(self)
        self.callback(None)


class PointSelectTool(QgsMapToolEmitPoint):
    """Interactive tool: user clicks a point to choose the starting side."""

    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        iface.messageBar().pushInfo(
            "Equalyzer",
            "Click inside a polygon to choose the starting side. "
            "Right-click or Escape to abort."
        )

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.canvas.unsetMapTool(self)
            self.callback(None)
            return
        point = self.toMapCoordinates(event.pos())
        self.canvas.unsetMapTool(self)
        self.callback(point)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.canvas.unsetMapTool(self)
            self.callback(None)


# ---------------------------------------------------------------------------
# Preview Dialog
# ---------------------------------------------------------------------------

class SplitPreviewDialog(QDialog):
    """
    Dialog that lets the user configure split parameters, draw a direction
    line, optionally pick a starting side, preview the result as rubber bands,
    and then apply or cancel.
    """

    def __init__(self, parent, mode, layer, polygon_features, da,
                 project_unit, unit_abbrev, crs_area_unit,
                 register_preview_band=None, unregister_preview_band=None):
        super().__init__(parent)
        self.mode = mode
        self.layer = layer
        self.polygon_features = polygon_features
        self.da = da
        self.project_unit = project_unit
        self.unit_abbrev = unit_abbrev
        self.crs_area_unit = crs_area_unit
        self.settings = QSettings()
        self.precision_value = self._load_precision_setting()

        # State
        self.direction_points = None   # [QgsPointXY, QgsPointXY] in layer CRS
        self.start_point = None        # QgsPointXY in layer CRS (or None)
        self.auto_picked = False       # polygon came from the direction line
        self.preview_bands = []        # list of QgsRubberBand
        self.preview_parts_by_feature = None
        self.active_tool = None        # currently active interactive map tool
        self._defer_close_cleanup = False
        self.register_preview_band = register_preview_band
        self.unregister_preview_band = unregister_preview_band

        self._build_ui()
        self._update_status()

    # ------------------------------------------------------------------
    # Persistent settings
    # ------------------------------------------------------------------

    def _settings_int(self, key, default):
        try:
            value = self.settings.value(key, default)
            if value is None:
                return int(default)
            return int(value)
        except Exception:
            return int(default)

    def _load_precision_setting(self):
        return max(1, min(5, self._settings_int("Equalyzer/precision", 3)))

    def _save_precision_setting(self):
        try:
            self.settings.setValue("Equalyzer/precision", int(self.precision_spin.value()))
        except Exception:
            pass

    def _restore_dialog_geometry(self):
        try:
            geometry = self.settings.value("Equalyzer/dialogGeometry")
            if geometry:
                self.restoreGeometry(geometry)
        except Exception:
            pass

    def _save_dialog_geometry(self):
        try:
            self.settings.setValue("Equalyzer/dialogGeometry", self.saveGeometry())
        except Exception:
            pass

    def _on_precision_changed(self, value):
        self.precision_value = int(value)
        self._save_precision_setting()
        self._clear_preview()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle("Equalyzer – Configure Split")
        self.setMinimumWidth(420)
        main_layout = QVBoxLayout(self)

        # ---- Info label ----
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        main_layout.addWidget(self.info_label)
        self._refresh_info_label()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(sep)

        # ---- Parameter group ----
        param_group = QGroupBox("Split Parameters")
        param_layout = QFormLayout(param_group)

        if self.mode == "area":
            self.area_spin = QDoubleSpinBox()
            self.area_spin.setRange(0.01, 1e12)
            self.area_spin.setDecimals(2)
            self.area_spin.setValue(1000.0)
            self.area_spin.setSuffix(f"  {self.unit_abbrev}")
            self.area_spin.setSingleStep(100.0)
            self.area_spin.valueChanged.connect(self._clear_preview)
            param_layout.addRow(f"Target area per part:", self.area_spin)
        else:
            self.parts_spin = QSpinBox()
            self.parts_spin.setRange(2, 10000)
            self.parts_spin.setValue(2)
            self.parts_spin.valueChanged.connect(self._clear_preview)
            param_layout.addRow("Number of parts:", self.parts_spin)

        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(1, 5)
        self.precision_spin.setValue(self.precision_value)
        self.precision_spin.setSingleStep(1)
        self.precision_spin.setToolTip(
            "Controls the connected concave splitter resolution.\n"
            "1 = faster/coarser, 3 = default, 5 = slower/finer and usually more even areas."
        )
        self.precision_spin.valueChanged.connect(self._on_precision_changed)
        param_layout.addRow("Precision:", self.precision_spin)

        main_layout.addWidget(param_group)

        # ---- Direction group ----
        dir_group = QGroupBox("Cut Direction")
        dir_layout = QVBoxLayout(dir_group)

        self.dir_status_label = QLabel("No direction line drawn yet.")
        self.dir_status_label.setWordWrap(True)
        dir_layout.addWidget(self.dir_status_label)

        draw_btn = QPushButton("✏  Draw Direction Line…")
        draw_btn.setToolTip(
            "Click two points on the map to define the direction of the cuts.\n"
            "Cuts will be made parallel to this line."
        )
        draw_btn.clicked.connect(self._draw_direction)
        dir_layout.addWidget(draw_btn)

        main_layout.addWidget(dir_group)

        # ---- Starting side group ----
        side_group = QGroupBox("Starting Side")
        side_layout = QVBoxLayout(side_group)

        self.side_status_label = QLabel(
            "No starting side selected (will use default: left/bottom side)."
        )
        self.side_status_label.setWordWrap(True)
        side_layout.addWidget(self.side_status_label)

        side_btn_layout = QHBoxLayout()
        self.pick_side_btn = QPushButton("📍  Pick Starting Side…")
        self.pick_side_btn.setToolTip(
            "Click inside a polygon to indicate which side to start cutting from."
        )
        self.pick_side_btn.clicked.connect(self._pick_start_side)
        side_btn_layout.addWidget(self.pick_side_btn)

        clear_side_btn = QPushButton("✖  Clear")
        clear_side_btn.setFixedWidth(70)
        clear_side_btn.clicked.connect(self._clear_start_side)
        side_btn_layout.addWidget(clear_side_btn)
        side_layout.addLayout(side_btn_layout)

        main_layout.addWidget(side_group)

        # ---- Preview group ----
        prev_group = QGroupBox("Preview")
        prev_layout = QVBoxLayout(prev_group)

        self.preview_status_label = QLabel("Press 'Preview' to see the split result on the map.")
        self.preview_status_label.setWordWrap(True)
        prev_layout.addWidget(self.preview_status_label)

        prev_btn_layout = QHBoxLayout()
        self.preview_btn = QPushButton("👁  Preview")
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self._run_preview)
        prev_btn_layout.addWidget(self.preview_btn)

        clear_prev_btn = QPushButton("✖  Clear Preview")
        clear_prev_btn.clicked.connect(self._clear_preview)
        prev_btn_layout.addWidget(clear_prev_btn)
        prev_layout.addLayout(prev_btn_layout)

        main_layout.addWidget(prev_group)

        # ---- Buttons ----
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(sep2)

        btn_box = QDialogButtonBox()
        self.apply_btn = btn_box.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setEnabled(False)
        btn_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self._on_cancel)
        main_layout.addWidget(btn_box)

        self._restore_dialog_geometry()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_display_area(self, area_sq_m_or_crs):
        """Convert area from da.measureArea() result to project display units."""
        if self.da.willUseEllipsoid():
            # da returns sq metres when using ellipsoid
            return QgsUnitTypes.fromUnitToUnitFactor(
                AREA_SQUARE_METERS, self.project_unit
            ) * area_sq_m_or_crs
        else:
            return QgsUnitTypes.fromUnitToUnitFactor(
                self.crs_area_unit, self.project_unit
            ) * area_sq_m_or_crs

    def _line_length(self, p1, p2):
        """Return Euclidean length between two QgsPointXY points."""
        return math.hypot(p2.x() - p1.x(), p2.y() - p1.y())

    def _normalize_line_angle(self, angle_deg):
        """Normalize line orientation angle to [0, 180)."""
        angle = angle_deg % 180.0
        return angle + 180.0 if angle < 0 else angle

    def _refresh_info_label(self):
        """Show what will be split, or how to choose it."""
        if not self.polygon_features:
            self.info_label.setText(
                "<b>No polygon selected.</b> Draw the direction line across the "
                "polygon you want to split and it will be picked automatically."
            )
            return
        total_area = sum(
            self.da.measureArea(f.geometry().makeValid())
            for f in self.polygon_features
        )
        total_area_display = self._to_display_area(total_area)
        picked = " (picked from the direction line)" if self.auto_picked else ""
        self.info_label.setText(
            f"<b>{len(self.polygon_features)}</b> polygon(s) selected{picked} — "
            f"total area: <b>{total_area_display:.4f} {self.unit_abbrev}</b>"
        )

    def _pick_feature_under_line(self):
        return pick_feature_under_line(self.layer, self.direction_points)

    def _auto_select_from_direction(self):
        """Pick and select a polygon when the user did not select one."""
        feature = None
        try:
            feature = self._pick_feature_under_line()
        except Exception as e:
            dbg(f"auto-pick failed: {e!r}")

        if feature is None:
            QMessageBox.warning(
                self, "No polygon found",
                "The direction line does not cross a polygon in the active layer.\n\n"
                "Draw it across the polygon you want to split, or select the "
                "polygon yourself before opening Equalyzer."
            )
            return

        self.polygon_features = [feature]
        self.auto_picked = True
        try:
            self.layer.selectByIds([feature.id()])
        except Exception as e:
            dbg(f"selectByIds failed: {e!r}")
        dbg(f"auto-picked feature fid={feature.id()} from the direction line")
        self._refresh_info_label()

    def _update_status(self):
        if self.direction_points:
            p1, p2 = self.direction_points
            direction_angle = math.degrees(math.atan2(
                p2.y() - p1.y(), p2.x() - p1.x()
            ))
            cut_angle = self._normalize_line_angle(direction_angle)
            self.dir_status_label.setText(
                "✔ Direction line set — "
                f"drawn: {direction_angle:.1f}°, "
                f"cuts: {cut_angle:.1f}° (parallel)"
            )
            has_polygon = bool(self.polygon_features)
            self.preview_btn.setEnabled(has_polygon)
            self.apply_btn.setEnabled(has_polygon)
        else:
            self.dir_status_label.setText("No direction line drawn yet.")
            self.preview_btn.setEnabled(False)
            self.apply_btn.setEnabled(False)

        if self.start_point:
            self.side_status_label.setText(
                f"✔ Starting side set at ({self.start_point.x():.2f}, {self.start_point.y():.2f})"
            )
        else:
            self.side_status_label.setText(
                "No starting side selected (will use default: left/bottom side)."
            )

    # ------------------------------------------------------------------
    # Interactive map actions
    # ------------------------------------------------------------------

    def _restore_dialog_focus(self):
        """Ensure dialog is visible and focused after interactive map tools."""
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _on_tool_deactivated(self, tool):
        """Fallback restoration when a map tool deactivates without callback."""
        if self.active_tool is tool:
            self.active_tool = None
            self._restore_dialog_focus()
            self._update_status()

    def _on_direction_captured(self, points):
        """Callback from LineDrawTool; stores points in layer CRS and restores UI."""
        try:
            if points and len(points) == 2:
                # Transform from map CRS to layer CRS if needed
                map_crs = iface.mapCanvas().mapSettings().destinationCrs()
                layer_crs = self.layer.crs()
                if map_crs != layer_crs:
                    transform = QgsCoordinateTransform(
                        map_crs, layer_crs, QgsProject.instance()
                    )
                    direction_points = [
                        transform.transform(points[0]),
                        transform.transform(points[1])
                    ]
                else:
                    direction_points = points

                # Guard against degenerate/too-short line
                if self._line_length(direction_points[0], direction_points[1]) < 1e-9:
                    self.direction_points = None
                    QMessageBox.warning(
                        self,
                        "Invalid Direction Line",
                        "The selected direction line is too short.\n"
                        "Please click two distinct points."
                    )
                else:
                    self.direction_points = direction_points
            else:
                self.direction_points = None
        except Exception as e:
            self.direction_points = None
            QMessageBox.critical(
                self,
                "Equalyzer Error",
                f"Failed to capture direction line: {e}"
            )
        finally:
            if self.direction_points and not self.polygon_features:
                self._auto_select_from_direction()
            self.active_tool = None
            self._restore_dialog_focus()
            self._update_status()

    def _on_start_side_captured(self, point):
        """Callback from PointSelectTool; stores validated start point and restores UI."""
        try:
            if point is not None:
                map_crs = iface.mapCanvas().mapSettings().destinationCrs()
                layer_crs = self.layer.crs()
                if map_crs != layer_crs:
                    transform = QgsCoordinateTransform(
                        map_crs, layer_crs, QgsProject.instance()
                    )
                    point = transform.transform(point)
                # Validate: must be inside one of the selected polygons
                clicked_geom = QgsGeometry.fromPointXY(point)
                if not self.polygon_features:
                    QMessageBox.warning(
                        self, "No polygon yet",
                        "Draw the direction line first, so Equalyzer knows which "
                        "polygon you mean."
                    )
                    self.start_point = None
                elif any(f.geometry().contains(clicked_geom) for f in self.polygon_features):
                    self.start_point = point
                else:
                    QMessageBox.warning(
                        self, "Invalid Point",
                        "The clicked point is not inside any selected polygon.\n"
                        "Please click inside a polygon."
                    )
                    self.start_point = None
            else:
                self.start_point = None
        except Exception as e:
            self.start_point = None
            QMessageBox.critical(
                self,
                "Equalyzer Error",
                f"Failed to pick starting side: {e}"
            )
        finally:
            self.active_tool = None
            self._restore_dialog_focus()
            self._update_status()

    def _draw_direction(self):
        self._clear_preview()
        self.hide()

        tool = LineDrawTool(iface.mapCanvas(), self._on_direction_captured)
        tool.deactivated.connect(lambda: self._on_tool_deactivated(tool))
        self.active_tool = tool
        iface.mapCanvas().setMapTool(tool)

    def _pick_start_side(self):
        self._clear_preview()
        self.hide()

        tool = PointSelectTool(iface.mapCanvas(), self._on_start_side_captured)
        tool.deactivated.connect(lambda: self._on_tool_deactivated(tool))
        self.active_tool = tool
        iface.mapCanvas().setMapTool(tool)

    def _clear_start_side(self):
        self.start_point = None
        self._update_status()

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _remove_preview_bands(self):
        scene = iface.mapCanvas().scene()
        for band in self.preview_bands:
            try:
                band.reset()
            except Exception:
                pass
            try:
                scene.removeItem(band)
            except Exception:
                pass
            if self.unregister_preview_band:
                try:
                    self.unregister_preview_band(band)
                except Exception:
                    pass
            try:
                band.deleteLater()
            except Exception:
                pass
        self.preview_bands.clear()

    def _clear_preview(self):
        self._remove_preview_bands()
        self.preview_parts_by_feature = None
        self.preview_status_label.setText(
            "Press 'Preview' to see the split result on the map."
        )
        self._update_status()

    def _run_preview(self):
        self._clear_preview()
        if not self.direction_points:
            return

        splitter = _SplitEngine(
            layer=self.layer,
            polygon_features=self.polygon_features,
            da=self.da,
            project_unit=self.project_unit,
            unit_abbrev=self.unit_abbrev,
            crs_area_unit=self.crs_area_unit,
            direction_points=self.direction_points,
            start_point=self.start_point,
            mode=self.mode,
            target_value=(
                self.area_spin.value() if self.mode == "area"
                else self.parts_spin.value()
            ),
            precision=self.precision_spin.value()
        )

        try:
            parts_by_feature = splitter.compute_parts_by_feature()
        except Exception as e:
            QMessageBox.warning(self, "Preview Error", str(e))
            return

        all_parts = []
        for _, parts in parts_by_feature:
            all_parts.extend(parts)

        if not all_parts:
            self.preview_parts_by_feature = None
            self.preview_status_label.setText("No parts produced — check your parameters.")
            return

        self.preview_parts_by_feature = parts_by_feature

        canvas = iface.mapCanvas()
        map_crs = canvas.mapSettings().destinationCrs()
        layer_crs = self.layer.crs()
        need_transform = (map_crs != layer_crs)
        if need_transform:
            transform = QgsCoordinateTransform(
                layer_crs, map_crs, QgsProject.instance()
            )

        # Colour palette (cycles)
        colours = [
            QColor(255, 100, 100, 160),
            QColor(100, 180, 255, 160),
            QColor(100, 255, 150, 160),
            QColor(255, 220, 80, 160),
            QColor(220, 100, 255, 160),
            QColor(255, 160, 60, 160),
            QColor(60, 220, 220, 160),
        ]

        total_parts = len(all_parts)
        area_texts = []
        for i, part_geom in enumerate(all_parts):
            band = QgsRubberBand(canvas, GEOM_POLYGON)
            setattr(band, "_equalyzer_preview_band", True)
            colour = colours[i % len(colours)]
            band.setColor(colour)
            band.setFillColor(colour)
            band.setWidth(1)

            display_geom = QgsGeometry(part_geom)
            if need_transform:
                display_geom.transform(transform)
            band.setToGeometry(display_geom, None)
            self.preview_bands.append(band)
            if self.register_preview_band:
                try:
                    self.register_preview_band(band)
                except Exception:
                    pass

            area_raw = self.da.measureArea(part_geom)
            area_disp = self._to_display_area(area_raw)
            area_texts.append(f"Part {i+1}: {area_disp:.4f} {self.unit_abbrev}")

        summary = f"Preview: {total_parts} part(s)\n" + "\n".join(area_texts[:20])
        if total_parts > 20:
            summary += f"\n… and {total_parts - 20} more"
        fallback_messages = getattr(splitter, "fallback_messages", [])
        if fallback_messages:
            summary += "\n\nFallback used:\n" + "\n".join(fallback_messages[:3])
        self.preview_status_label.setText(summary)
        self.apply_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def get_parameters(self):
        """Return a dict of all configured parameters."""
        return {
            "direction_points": self.direction_points,
            "start_point": self.start_point,
            "preview_parts_by_feature": self.preview_parts_by_feature,
            "target_value": (
                self.area_spin.value() if self.mode == "area"
                else self.parts_spin.value()
            ),
            "precision": self.precision_spin.value()
        }

    def _on_cancel(self):
        self._save_precision_setting()
        self._save_dialog_geometry()
        self._clear_preview()
        self.reject()

    def accept(self):
        # Keep accept path minimal to avoid modal-close deadlocks on some setups.
        try:
            iface.messageBar().pushInfo("Equalyzer", "Apply clicked — starting split.")
        except Exception:
            pass
        self._save_precision_setting()
        self._save_dialog_geometry()
        self._defer_close_cleanup = True
        super().accept()

    def closeEvent(self, event):
        if self.active_tool and iface.mapCanvas().mapTool() == self.active_tool:
            iface.mapCanvas().unsetMapTool(self.active_tool)
        self.active_tool = None
        self._save_precision_setting()
        self._save_dialog_geometry()
        # Do not perform scene mutations on accept-close path (can deadlock).
        if not self._defer_close_cleanup:
            self._remove_preview_bands()
        super().closeEvent(event)


class BayPlanDialog(QDialog):
    """Confirm how a selection of parking strips will be cut into bays.

    Nothing is drawn or typed: every strip is measured with its own oriented
    bounding box, the short side decides whether the bays lie head to tail or
    side by side, and the number of bays follows from the length.
    """

    def __init__(self, parent, layer, polygon_features, da, unit_abbrev, measure=None):
        super().__init__(parent)
        self.layer = layer
        self.polygon_features = polygon_features
        self.da = da
        self.unit_abbrev = unit_abbrev
        self.measure = measure
        self.settings = QSettings()

        sample = None
        for feature in polygon_features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                sample = geometry.centroid().asPoint()
                break
        self.to_work, self.from_work = (None, None)
        if sample is not None:
            self.to_work, self.from_work, _work = metric_work_transforms(layer, sample)
        self.plans = []
        self.fallback_direction = None

        self._build_ui()
        self._recalculate()

    # ------------------------------------------------------------------

    def _spin(self, key, default, suffix):
        box = QDoubleSpinBox()
        box.setRange(0.5, 50.0)
        box.setDecimals(2)
        box.setSingleStep(0.1)
        box.setSuffix(f"  {suffix}")
        try:
            box.setValue(float(self.settings.value(key, default)))
        except Exception:
            box.setValue(default)
        box.valueChanged.connect(self._recalculate)
        return box

    def _build_ui(self):
        self.setWindowTitle("Equalyzer – Parking bays")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)

        size_group = QGroupBox("Bay size")
        size_form = QFormLayout(size_group)
        self.length_spin = self._spin("Equalyzer/bayLength", DEFAULT_BAY_LENGTH, "m")
        self.width_spin = self._spin("Equalyzer/bayWidth", DEFAULT_BAY_WIDTH, "m")
        self.min_length_spin = self._spin(
            "Equalyzer/minBayLength", DEFAULT_MIN_BAY_LENGTH, "m")
        self.min_width_spin = self._spin(
            "Equalyzer/minBayWidth", DEFAULT_MIN_BAY_WIDTH, "m")
        size_form.addRow("Bay length (along the car):", self.length_spin)
        size_form.addRow("Shortest acceptable length:", self.min_length_spin)
        size_form.addRow("Bay width (across the car):", self.width_spin)
        size_form.addRow("Narrowest acceptable width:", self.min_width_spin)

        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem("Automatic (from the strip depth)", None)
        self.orientation_combo.addItem("Parallel parking (langs)", PARALLEL)
        self.orientation_combo.addItem("Perpendicular parking (haaks)", PERPENDICULAR)
        self.orientation_combo.currentIndexChanged.connect(self._recalculate)
        size_form.addRow("Orientation:", self.orientation_combo)
        layout.addWidget(size_group)

        output_group = QGroupBox("What to do with the result")
        output_form = QFormLayout(output_group)
        self.output_combo = QComboBox()
        self.output_combo.addItem("Collect the bays in the Split Parts layer", "collect")
        self.output_combo.addItem(
            "Collect them there and delete the original polygon", "collect_delete")
        self.output_combo.addItem(
            "Replace the original in this layer with the bays", "replace")
        stored = self.settings.value("Equalyzer/bayOutput", "collect")
        index = self.output_combo.findData(stored)
        self.output_combo.setCurrentIndex(index if index >= 0 else 0)
        output_form.addRow(self.output_combo)
        layout.addWidget(output_group)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox()
        self.create_btn = buttons.addButton("Create bays", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn = buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.create_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------

    def _recalculate(self):
        bay_length = self.length_spin.value()
        bay_width = self.width_spin.value()
        min_length = self.min_length_spin.value()
        min_width = self.min_width_spin.value()
        orientation = self.orientation_combo.currentData()

        self.plans = []
        self.fallback_direction = None
        for feature in self.polygon_features:
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                self.plans.append((feature, None))
                continue
            axis = None
            try:
                measured_geom = to_work_crs(geom, self.to_work)
                axis = strip_axis(
                    measured_geom,
                    None if self.to_work is not None else self.measure,
                )
            except Exception as e:
                dbg(f"strip_axis failed for fid={feature.id()}: {e!r}")
            if axis is None:
                self.plans.append((feature, None))
                continue
            cut_points, length, depth, fit = axis
            if self.fallback_direction is None:
                if self.from_work is not None:
                    try:
                        cut_points = [self.from_work.transform(p) for p in cut_points]
                    except Exception:
                        pass
                self.fallback_direction = cut_points
            self.plans.append((feature, plan_bays(
                length, depth, bay_length=bay_length, bay_width=bay_width,
                fit=fit, orientation=orientation,
                min_bay_length=min_length, min_bay_width=min_width,
            )))

        self._render_summary()

    def _render_summary(self):
        lines = []
        total_bays = 0
        usable = 0
        warnings = []
        for feature, plan in self.plans[:12]:
            if plan is None:
                lines.append(f"<b>{feature.id()}</b>: could not be measured")
                continue
            lines.append(f"<b>{feature.id()}</b>: {plan.describe(self.unit_abbrev)}")
        for feature, plan in self.plans:
            if plan is None or not plan.ok:
                continue
            usable += 1
            total_bays += plan.count
            for warning in plan.warnings:
                warnings.append(f"{feature.id()}: {warning}")

        extra = len(self.plans) - 12
        if extra > 0:
            lines.append(f"… and {extra} more strip(s)")

        header = (f"<b>{usable}</b> of {len(self.plans)} strip(s) give "
                  f"<b>{total_bays}</b> bays in total.")
        body = header + "<br><br>" + "<br>".join(lines)
        if warnings:
            shown = "<br>".join(warnings[:6])
            more = "" if len(warnings) <= 6 else f"<br>… and {len(warnings) - 6} more"
            body += f"<br><br><b>Check:</b><br>{shown}{more}"
        self.summary_label.setText(body)
        self.create_btn.setEnabled(usable > 0)

    # ------------------------------------------------------------------

    def get_parameters(self):
        """Parameters in the shape the shared apply path expects."""
        self.settings.setValue("Equalyzer/bayLength", self.length_spin.value())
        self.settings.setValue("Equalyzer/bayWidth", self.width_spin.value())
        self.settings.setValue("Equalyzer/minBayLength", self.min_length_spin.value())
        self.settings.setValue("Equalyzer/minBayWidth", self.min_width_spin.value())
        choice = self.output_combo.currentData()
        self.settings.setValue("Equalyzer/bayOutput", choice)
        return {
            # Only used as a starting value; the engine takes the direction
            # from each strip's own axis.
            "direction_points": self.fallback_direction,
            "start_point": None,
            "preview_parts_by_feature": None,
            "target_value": 0,
            "precision": 3,
            "replace_in_source": choice == "replace",
            "delete_source": choice in ("replace", "collect_delete"),
            "bay_settings": {
                "bay_length": self.length_spin.value(),
                "bay_width": self.width_spin.value(),
                "min_bay_length": self.min_length_spin.value(),
                "min_bay_width": self.min_width_spin.value(),
                "orientation": self.orientation_combo.currentData(),
                "auto_direction": True,
                "measure": self.measure,
            },
        }


# ---------------------------------------------------------------------------
# Split Engine
# ---------------------------------------------------------------------------

class _SplitEngine:
    """
    Pure computation class — no UI.  Given all parameters, computes the
    split geometries.
    """

    def __init__(self, layer, polygon_features, da, project_unit, unit_abbrev,
                 crs_area_unit, direction_points, start_point, mode, target_value,
                 precision=3, bay_settings=None):
        self.layer = layer
        self.polygon_features = polygon_features
        self.da = da
        self.project_unit = project_unit
        self.unit_abbrev = unit_abbrev
        self.crs_area_unit = crs_area_unit
        self.direction_points = direction_points   # [pt_a, pt_b] in layer CRS
        self.start_point = start_point             # QgsPointXY in layer CRS or None
        self.mode = mode
        self.target_value = target_value           # area value or num_parts
        try:
            self.precision = max(1, min(5, int(precision)))
        except Exception:
            self.precision = 3
        self.fallback_messages = []
        self._current_total_area = 0.0
        self.bay_settings = dict(bay_settings or {})
        # In parking-bay mode every strip gets the direction of its own axis,
        # so a whole selection can be cut in one go.
        self.auto_direction = bool(
            self.bay_settings.get("auto_direction", mode == "bays")
        )
        self.bay_plans = {}          # fid -> BayPlan, for reporting

        # Work frame: angles and distances are meaningless in degrees.
        sample = None
        for feature in polygon_features:
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                sample = geometry.centroid().asPoint()
                break
        self.to_work, self.from_work, work_crs = (None, None, None)
        if sample is not None:
            self.to_work, self.from_work, work_crs = metric_work_transforms(layer, sample)
        self.work_da = None
        if work_crs is not None:
            self.work_da = QgsDistanceArea()
            self.work_da.setSourceCrs(work_crs, QgsProject.instance().transformContext())
            dbg(f"working in {work_crs.authid()} because {layer.crs().authid()} "
                "is a geographic CRS")

        # Derived: angles from the user-drawn direction line in layer CRS
        pt_a, pt_b = direction_points
        dx = pt_b.x() - pt_a.x()
        dy = pt_b.y() - pt_a.y()
        if math.hypot(dx, dy) < 1e-9:
            raise ValueError("Direction line is too short. Please draw two distinct points.")

        # User-drawn line is the cut direction reference; cuts are parallel to it.
        self.direction_line_angle_deg = math.degrees(math.atan2(dy, dx))
        self.cut_line_angle_deg = self.direction_line_angle_deg

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def compute_parts_by_feature(self):
        """Return list of tuples: (source_feature, [QgsGeometry parts])."""
        parts_by_feature = []
        _dbg_counters.clear()
        dbg(f"compute_parts_by_feature: {len(self.polygon_features)} feature(s), "
            f"mode={self.mode}, target_value={self.target_value}, "
            f"cut_angle={getattr(self, 'cut_line_angle_deg', None)}")
        for feature in self.polygon_features:
            fid = feature.id()
            source_geom = feature.geometry()
            if source_geom is None:
                dbg(f"  fid={fid}: SKIPPED - no geometry")
                continue
            geom = QgsGeometry(source_geom)
            if geom.isEmpty():
                dbg(f"  fid={fid}: SKIPPED - empty geometry")
                continue
            dbg(f"  fid={fid}: wkbType={geom.wkbType()}")
            try:
                if not geom.isGeosValid():
                    dbg(f"  fid={fid}: geometry invalid, running makeValid()")
                    geom = geom.makeValid()
            except Exception as e:
                dbg(f"  fid={fid}: makeValid() raised {e!r}")
            if geom.isEmpty():
                dbg(f"  fid={fid}: SKIPPED - empty after makeValid()")
                continue
            try:
                if not geom.isGeosValid():
                    dbg(f"  fid={fid}: SKIPPED - still not GEOS-valid after makeValid()")
                    continue
            except Exception as e:
                dbg(f"  fid={fid}: isGeosValid() raised {e!r}")
            total_area = self.da.measureArea(geom)
            self._current_total_area = total_area
            dbg(f"  fid={fid}: total_area={total_area!r} "
                f"(ellipsoid={self.da.willUseEllipsoid()})")
            if total_area <= 0:
                dbg(f"  fid={fid}: SKIPPED - measured area <= 0")
                continue

            plan = None
            if self.auto_direction or self.mode == "bays":
                plan = self._measure_strip(feature, to_work_crs(geom, self.to_work))

            if self.mode == "area":
                target_area = self._input_area_to_da_units(self.target_value)
                if target_area <= 0:
                    raise ValueError("Target area must be positive.")
                if target_area >= total_area * 0.999:
                    # Target area >= polygon area → return whole polygon
                    parts_by_feature.append((feature, [geom]))
                    continue
            else:
                if self.mode == "bays":
                    if plan is None or not plan.ok:
                        dbg(f"  fid={fid}: SKIPPED - no usable bay plan")
                        continue
                    num_parts = plan.count
                else:
                    num_parts = int(self.target_value)
                if num_parts <= 1:
                    parts_by_feature.append((feature, [geom]))
                    continue
                target_area = total_area / num_parts

            # Determine sweep direction for this polygon
            sweep_from_low = self._should_sweep_from_low(geom)

            dbg(f"  fid={fid}: target_area={target_area!r}, sweep_from_low={sweep_from_low}")
            if self.mode in ("count", "bays"):
                parts = self._split_in_work_frame(geom, num_parts, sweep_from_low)
            else:
                parts = self._split_geom_connected_area(geom, target_area, sweep_from_low)
            dbg(f"  fid={fid}: splitter returned {len(parts or [])} part(s)")

            parts = [self._clean_geometry(p) for p in parts if p is not None and not p.isEmpty()]
            dbg(f"  fid={fid}: {len(parts)} part(s) after cleaning")
            parts = self._decompose_multiparts(parts)
            dbg(f"  fid={fid}: {len(parts)} part(s) after multipart decomposition")
            areas = [self.da.measureArea(p) for p in parts]
            parts = [p for p, a in zip(parts, areas) if a > self._tiny_area()]
            dbg(f"  fid={fid}: {len(parts)} part(s) after the dust filter "
                f"(> {self._tiny_area():.6g}); "
                f"areas={[round(a, 6) for a in areas[:10]]}")
            if parts:
                parts_by_feature.append((feature, parts))

        return parts_by_feature

    def compute_parts(self):
        """Return list of QgsGeometry parts (all polygons, all features)."""
        parts_by_feature = self.compute_parts_by_feature()
        all_parts = []
        for _, parts in parts_by_feature:
            all_parts.extend(parts)

        return all_parts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_direction_from_points(self, points):
        """Point the cut lines along the given pair of points."""
        pt_a, pt_b = points
        dx = pt_b.x() - pt_a.x()
        dy = pt_b.y() - pt_a.y()
        if math.hypot(dx, dy) < 1e-9:
            return False
        self.direction_line_angle_deg = math.degrees(math.atan2(dy, dx))
        self.cut_line_angle_deg = self.direction_line_angle_deg
        return True

    def _measure_strip(self, feature, geom):
        """Set this feature's cut direction and, in bay mode, plan its bays."""
        fid = feature.id()
        axis = None
        try:
            measure = None if self.to_work is not None else self.bay_settings.get("measure")
            axis = strip_axis(geom, measure)
        except Exception as e:
            dbg(f"  fid={fid}: strip_axis raised {e!r}")

        if axis is None:
            self._record_fallback(
                f"Feature {fid}: could not measure the strip; the drawn "
                "direction was used instead."
            )
            return None

        cut_points, length, depth, fit = axis
        dbg(f"  fid={fid}: strip {length:.2f} x {depth:.2f} m, "
            f"fit={fit if fit is None else round(fit, 3)}, "
            f"cut direction {axis_angle_degrees(cut_points):.2f}deg")
        if self.auto_direction and not self._set_direction_from_points(cut_points):
            self._record_fallback(
                f"Feature {fid}: strip is too small to take a direction from."
            )

        if self.mode != "bays":
            return None

        plan = plan_bays(
            length, depth,
            bay_length=self.bay_settings.get("bay_length", DEFAULT_BAY_LENGTH),
            bay_width=self.bay_settings.get("bay_width", DEFAULT_BAY_WIDTH),
            fit=fit,
            orientation=self.bay_settings.get("orientation"),
            min_bay_length=self.bay_settings.get(
                "min_bay_length", DEFAULT_MIN_BAY_LENGTH),
            min_bay_width=self.bay_settings.get(
                "min_bay_width", DEFAULT_MIN_BAY_WIDTH),
        )
        self.bay_plans[fid] = plan
        dbg(f"  fid={fid}: {plan.describe()}")
        for warning in plan.warnings:
            self._record_fallback(f"Feature {fid}: {warning}")
        return plan

    def _tiny_area(self):
        """Numerical-dust threshold, relative to the polygon being split.

        Never use an absolute value here: measureArea() returns square metres
        on the ellipsoid but square CRS units otherwise, and in a geographic
        CRS one square degree is some 10^10 square metres.
        """
        return max(self._current_total_area, 0.0) * 1e-9

    def _split_in_work_frame(self, geom, num_parts, sweep_from_low):
        """Split in a metric frame, then bring the parts back to the layer CRS.

        While this runs, self.da measures planar metres in the work frame, so
        every area test inside the splitting code stays in one unit system.
        """
        if self.to_work is None:
            return self._split_equal_parts(geom, num_parts, sweep_from_low)

        work_geom = to_work_crs(geom, self.to_work)
        saved_da, saved_total = self.da, self._current_total_area
        if self.work_da is not None:
            self.da = self.work_da
        self._current_total_area = work_geom.area()
        try:
            parts = self._split_equal_parts(work_geom, num_parts, sweep_from_low)
        finally:
            self.da, self._current_total_area = saved_da, saved_total

        return [to_work_crs(part, self.from_work) for part in parts]

    def _split_equal_parts(self, geom, num_parts, sweep_from_low):
        """Count mode: exact parallel slices of equal area.

        The cumulative-area profile gives the cut positions in closed form, so
        the parts are equal to within rounding.  The connected partitioner is
        only used when exact slices would fall apart into separate pieces,
        which happens on polygons that are concave across the cut direction.
        """
        try:
            result = split_into_equal_parts(
                geom, num_parts, self.cut_line_angle_deg,
                self.da.measureArea, sweep_from_low=sweep_from_low,
            )
        except Exception as e:
            dbg(f"    exact slicing raised {e!r}")
            self._record_fallback(
                f"Exact slicing was not possible ({e}); the connected "
                "partitioner was used, so the areas are approximate."
            )
            return self._split_geom_connected_count(geom, num_parts, sweep_from_low)

        dbg(f"    exact slicing: {result.summary()}")

        if result.disconnected:
            self._record_fallback(
                f"{len(result.disconnected)} of {num_parts} parts would consist of "
                "several separate pieces in this cut direction, so the connected "
                "partitioner was used and the areas are approximate. Rotating the "
                "direction line often avoids this."
            )
            return self._split_geom_connected_count(geom, num_parts, sweep_from_low)

        if result.max_deviation > 1e-4:
            self._record_fallback(
                f"Largest deviation from the equal share: "
                f"{result.max_deviation * 100.0:.3f}%."
            )
        return result.parts

    def _input_area_to_da_units(self, value):
        """Convert user-entered area (in project display units) to da.measureArea() units."""
        if self.da.willUseEllipsoid():
            # da.measureArea returns sq metres
            return QgsUnitTypes.fromUnitToUnitFactor(
                self.project_unit, AREA_SQUARE_METERS
            ) * value
        else:
            return QgsUnitTypes.fromUnitToUnitFactor(
                self.project_unit, self.crs_area_unit
            ) * value

    def _should_sweep_from_low(self, geom):
        """
        Determine whether to sweep from the low end (yMin in rotated space)
        or the high end (yMax in rotated space).

        The drawn line defines the cut direction.
        Cuts are parallel to the drawn line.

        If a start_point was provided, we project it onto the sweep axis in
        rotated space and start from whichever end it is closer to.

        Default (no start_point): sweep from low (left/bottom).
        """
        if self.start_point is None:
            return True  # default: sweep from low end

        center = geom.boundingBox().center()
        # Rotate the start_point into the polygon's rotated frame
        # QgsGeometry.rotate uses clockwise-positive degrees. To align cut lines
        # with X axis, rotate by +cut_line_angle.
        rot_angle = self.cut_line_angle_deg
        rot_start = self._rotate_point(self.start_point, center, rot_angle)

        # Get the rotated bounding box
        rotated_geom = QgsGeometry(geom)
        rotated_geom.rotate(rot_angle, center)
        bbox = rotated_geom.boundingBox()

        mid_y = (bbox.yMinimum() + bbox.yMaximum()) / 2.0
        # If the rotated start point is below the midline → sweep from low
        return rot_start.y() <= mid_y

    def _rotate_point(self, point, center, angle_deg):
        """Rotate point using QGIS geometry rotation semantics."""
        g = QgsGeometry.fromPointXY(point)
        g.rotate(angle_deg, center)
        return g.asPoint()

    def _split_geom(self, geom, target_area, sweep_from_low):
        """
        Split geom into parts of approximately target_area each.

        Cut lines are parallel to the user-drawn direction line.
        We rotate the geometry so cut lines are horizontal,
        then sweep vertically (binary search on Y).

        sweep_from_low=True  → start from yMinimum, cut upward
        sweep_from_low=False → start from yMaximum, cut downward
        """
        parts = []
        remaining = QgsGeometry(geom)
        center = geom.boundingBox().center()
        # QgsGeometry.rotate is clockwise-positive.
        # Rotate by +cut angle so desired cut direction becomes horizontal.
        rot_angle = self.cut_line_angle_deg

        max_iterations = 500  # safety cap
        iteration = 0
        tried_sweep_flip = False

        while iteration < max_iterations:
            iteration += 1
            remaining_area = self.da.measureArea(remaining)
            if remaining_area < target_area * 0.01:
                break

            # Stop rule differs by mode.
            # - count mode: keep practical 1.5× threshold for last piece
            # - area mode: use tighter threshold to avoid stopping too early
            if self.mode in ("count", "bays"):
                if remaining_area <= target_area * 1.5:
                    parts.append(remaining)
                    remaining = QgsGeometry()
                    break
            else:
                if remaining_area <= target_area * 1.05:
                    parts.append(remaining)
                    remaining = QgsGeometry()
                    break

            # Rotate remaining geometry
            rotated = QgsGeometry(remaining)
            rotated.rotate(rot_angle, center)
            bbox = rotated.boundingBox()

            # Binary search for the Y cut position
            best_y = self._binary_search_y(
                rotated, bbox, target_area, sweep_from_low, n_iter=50
            )

            # Build the clip rectangle in rotated space
            if sweep_from_low:
                clip_rect = QgsRectangle(
                    bbox.xMinimum() - 1, bbox.yMinimum() - 1,
                    bbox.xMaximum() + 1, best_y
                )
            else:
                clip_rect = QgsRectangle(
                    bbox.xMinimum() - 1, best_y,
                    bbox.xMaximum() + 1, bbox.yMaximum() + 1
                )

            clip_geom = QgsGeometry.fromRect(clip_rect)
            # Rotate clip back to original space
            clip_geom.rotate(-rot_angle, center)

            # Intersect and difference
            new_part = remaining.intersection(clip_geom)
            new_part = self._clean_geometry(new_part)
            leftover = remaining.difference(clip_geom)
            leftover = self._clean_geometry(leftover)

            new_part_area = self.da.measureArea(new_part)
            if new_part.isEmpty() or new_part_area < target_area * 0.01:
                # Direction-dependent robustness: one retry with opposite sweep.
                if self.mode == "area" and not tried_sweep_flip:
                    sweep_from_low = not sweep_from_low
                    tried_sweep_flip = True
                    continue
                # Something went wrong; keep the remainder as-is
                parts.append(remaining)
                remaining = QgsGeometry()
                break

            parts.append(new_part)
            remaining = leftover

            if remaining.isEmpty():
                break

        # Handle any leftover
        if not remaining.isEmpty():
            leftover_area = self.da.measureArea(remaining)
            if leftover_area > self._tiny_area():
                if parts and leftover_area < target_area * 0.5:
                    # Merge tiny leftover into last part
                    parts[-1] = parts[-1].combine(remaining)
                else:
                    parts.append(remaining)

        return parts

    def _record_fallback(self, message):
        if message not in self.fallback_messages:
            self.fallback_messages.append(message)

    def _strict_area_tolerance(self, target_area, total_area=None):
        try:
            scale = max(abs(float(target_area)), 1.0)
        except Exception:
            scale = 1.0
        return max(scale * 1e-12, 1e-6)

    def _split_geom_strict_area(self, geom, target_area, sweep_from_low):
        """Split by repeated exact straight cuts without strip fragmentation."""
        if geom is None or geom.isEmpty():
            return []

        total_area = self.da.measureArea(geom)
        if total_area <= 0 or target_area <= 0:
            return []
        if target_area >= total_area * 0.999:
            return [geom]

        tol = self._strict_area_tolerance(target_area, total_area)
        parts = []
        remaining = QgsGeometry(geom)
        guard = 0
        max_parts = 10000

        while guard < max_parts:
            remaining_area = self.da.measureArea(remaining)
            if remaining_area <= tol:
                break
            if remaining_area <= target_area + tol:
                parts.append(remaining)
                break

            split = self._strict_split_one_part(
                remaining, target_area, sweep_from_low, remaining_area
            )
            if split is None:
                return None

            new_part, remaining = split
            if new_part is None or new_part.isEmpty() or remaining is None or remaining.isEmpty():
                return None

            parts.append(new_part)
            guard += 1

        if guard >= max_parts:
            return None

        return parts if parts else [geom]

    def _split_geom_strict_count(self, geom, num_parts, sweep_from_low):
        """Split into equal-count parts by exact straight cuts."""
        if geom is None or geom.isEmpty():
            return []
        if num_parts <= 1:
            return [geom]

        total_area = self.da.measureArea(geom)
        if total_area <= 0:
            return []

        target_area = total_area / float(num_parts)
        tol = self._strict_area_tolerance(target_area, total_area)
        parts = []
        remaining = QgsGeometry(geom)

        for _idx in range(num_parts - 1):
            remaining_area = self.da.measureArea(remaining)
            if remaining_area <= target_area + tol:
                return None

            split = self._strict_split_one_part(
                remaining, target_area, sweep_from_low, remaining_area
            )
            if split is None:
                return None

            new_part, remaining = split
            if new_part is None or new_part.isEmpty() or remaining is None or remaining.isEmpty():
                return None
            parts.append(new_part)

        if remaining is None or remaining.isEmpty():
            return None
        parts.append(remaining)
        return parts if len(parts) == num_parts else None

    def _strict_split_one_part(self, geom, target_area, sweep_from_low, total_area=None):
        """Find one exact cut and return (target_side, remainder), or None."""
        if geom is None or geom.isEmpty() or target_area <= 0:
            return None

        measured_total = total_area if total_area is not None else self.da.measureArea(geom)
        if measured_total <= target_area:
            return None

        center = geom.boundingBox().center()
        rot_angle = self.cut_line_angle_deg
        rotated = QgsGeometry(geom)
        rotated.rotate(rot_angle, center)
        bbox = rotated.boundingBox()
        if bbox.yMaximum() - bbox.yMinimum() <= 1e-12:
            return None

        low = bbox.yMinimum()
        high = bbox.yMaximum()
        tol = self._strict_area_tolerance(target_area, measured_total)
        n_iter = 70 + (self.precision * 20)
        best = None
        best_err = None

        for _ in range(n_iter):
            mid = (low + high) / 2.0
            evaluated = self._evaluate_strict_cut_at_y(
                geom, center, rot_angle, bbox, mid, sweep_from_low
            )

            if evaluated is None:
                side_area = self._temporary_clip_area_at_y(
                    geom, center, rot_angle, bbox, mid, sweep_from_low
                )
                strict_ok = False
                target_side = None
                remainder = None
            else:
                target_side, remainder, side_area, strict_ok = evaluated

            if side_area is None:
                return None

            if strict_ok:
                err = abs(side_area - target_area)
                if best is None or err < best_err:
                    best = (target_side, remainder)
                    best_err = err

            if side_area < target_area:
                if sweep_from_low:
                    low = mid
                else:
                    high = mid
            else:
                if sweep_from_low:
                    high = mid
                else:
                    low = mid

        if best is None or best_err is None or best_err > tol:
            return None

        part, remainder = best
        if abs(self.da.measureArea(part) - target_area) > tol:
            return None
        if remainder is None or remainder.isEmpty() or self.da.measureArea(remainder) <= tol:
            return None

        return QgsGeometry(part), QgsGeometry(remainder)

    def _evaluate_strict_cut_at_y(self, geom, center, rot_angle, bbox, y, sweep_from_low):
        pieces = self._split_geometry_by_rotated_y_line(geom, center, rot_angle, bbox, y)
        if not pieces or len(pieces) < 2:
            return None

        target_group = []
        other_group = []
        for piece in pieces:
            if piece is None or piece.isEmpty():
                continue
            cy = self._rotated_geom_center_y(piece, center, rot_angle)
            if sweep_from_low:
                goes_to_target = cy <= y
            else:
                goes_to_target = cy >= y
            if goes_to_target:
                target_group.append(piece)
            else:
                other_group.append(piece)

        if not target_group or not other_group:
            return None

        side_area = sum(self.da.measureArea(g) for g in target_group)
        strict_ok = (
            len(target_group) == 1 and
            len(other_group) == 1 and
            self._is_strict_split_piece(target_group[0]) and
            self._is_strict_split_piece(other_group[0])
        )

        target_side = QgsGeometry(target_group[0]) if strict_ok else None
        remainder = QgsGeometry(other_group[0]) if strict_ok else None
        return target_side, remainder, side_area, strict_ok

    def _split_geometry_by_rotated_y_line(self, geom, center, rot_angle, bbox, y):
        width = max(1e-9, bbox.xMaximum() - bbox.xMinimum())
        height = max(1e-9, bbox.yMaximum() - bbox.yMinimum())
        margin = max(width, height, 1.0) * 4.0

        p1_rot = QgsPointXY(bbox.xMinimum() - margin, y)
        p2_rot = QgsPointXY(bbox.xMaximum() + margin, y)
        p1 = self._rotate_point(p1_rot, center, -rot_angle)
        p2 = self._rotate_point(p2_rot, center, -rot_angle)

        split_geom = QgsGeometry(geom)
        result = self._call_split_geometry(split_geom, p1, p2)
        if result is None:
            return []

        if not isinstance(result, tuple) or len(result) < 2:
            return []
        if not self._split_result_is_success(result[0]):
            return []

        new_geoms = result[1] or []
        pieces = [QgsGeometry(split_geom)]
        for new_geom in new_geoms:
            try:
                pieces.append(QgsGeometry(new_geom))
            except Exception:
                pass

        min_area = self.da.measureArea(geom) * 1e-12
        return [
            p for p in pieces
            if p is not None and not p.isEmpty() and self.da.measureArea(p) > min_area
        ]

    def _call_split_geometry(self, geom, p1, p2):
        """Call splitGeometry across QGIS 3/4 point overload differences."""
        attempts = []
        try:
            attempts.append([QgsPoint(p1.x(), p1.y()), QgsPoint(p2.x(), p2.y())])
        except Exception:
            pass
        try:
            attempts.append([QgsPointXY(p1), QgsPointXY(p2)])
        except Exception:
            try:
                attempts.append([QgsPointXY(p1.x(), p1.y()), QgsPointXY(p2.x(), p2.y())])
            except Exception:
                pass

        errors = []
        for split_line in attempts:
            kind = type(split_line[0]).__name__ if split_line else "empty"
            for args in ((split_line, False), (split_line, False, True)):
                try:
                    return geom.splitGeometry(*args)
                except Exception as e:
                    errors.append(f"{kind}/{len(args)} args: {e!r}")
        dbg_limited("splitGeometry", 3,
                    "splitGeometry failed for every overload: " + " | ".join(errors))
        return None

    def _split_result_is_success(self, result_code):
        try:
            if result_code == Qgis.GeometryOperationResult.Success:
                return True
        except Exception:
            pass
        try:
            return int(result_code) == 0
        except Exception:
            return str(result_code).endswith("Success")

    def _rotated_geom_center_y(self, geom, center, rot_angle):
        rotated = QgsGeometry(geom)
        rotated.rotate(rot_angle, center)
        return rotated.boundingBox().center().y()

    def _is_strict_split_piece(self, geom):
        if geom is None or geom.isEmpty():
            return False
        flat = QgsWkbTypes.flatType(geom.wkbType())
        if flat == WKB_POLYGON:
            return self.da.measureArea(geom) > self._tiny_area()
        if flat == WKB_MULTIPOLYGON:
            try:
                return len(geom.asMultiPolygon()) == 1 and self.da.measureArea(geom) > self._tiny_area()
            except Exception:
                return False
        return False

    def _temporary_clip_area_at_y(self, geom, center, rot_angle, bbox, y, sweep_from_low):
        if sweep_from_low:
            rect = QgsRectangle(
                bbox.xMinimum() - 1.0, bbox.yMinimum() - 1.0,
                bbox.xMaximum() + 1.0, y
            )
        else:
            rect = QgsRectangle(
                bbox.xMinimum() - 1.0, y,
                bbox.xMaximum() + 1.0, bbox.yMaximum() + 1.0
            )
        try:
            clip_geom = QgsGeometry.fromRect(rect)
            clip_geom.rotate(-rot_angle, center)
            clipped = geom.intersection(clip_geom)
            return self.da.measureArea(clipped) if clipped and not clipped.isEmpty() else 0.0
        except Exception:
            return None

    def _split_geom_connected_area(self, geom, target_area, sweep_from_low):
        """Target-area mode.

        This mode is deliberately different from count mode.  If the user asks
        for 40 000 m² pieces from a 135 000 m² polygon, the expected result is
        roughly 40 000 + 40 000 + 40 000 + 15 000 m².

        The preferred path repeatedly makes exact straight cuts using
        QgsGeometry.splitGeometry(), which preserves the original boundary
        except at real cut intersections.  If those cuts cannot keep both sides
        as single connected polygons, this falls back to the connected graph
        partition with explicit target weights:
          [target, target, ..., final remainder].
        """
        if geom is None or geom.isEmpty():
            return []

        total_area = self.da.measureArea(geom)
        if total_area <= 0 or target_area <= 0:
            return []
        if target_area >= total_area * 0.999:
            return [geom]

        strict = self._split_geom_strict_area(geom, target_area, sweep_from_low)
        dbg(f"    strict area split -> {None if strict is None else len(strict)}")
        if strict is not None:
            return strict

        self._record_fallback(
            "Strict straight-cut splitting was not possible for at least one polygon; "
            "the connected fallback was used and may add short boundary segments."
        )

        full_count = int(math.floor(total_area / target_area))
        remainder = total_area - (full_count * target_area)

        if full_count <= 0:
            return [geom]

        # Avoid creating a nearly-zero final remainder.  In that case the small
        # surplus is intentionally absorbed into the last full target part.
        min_remainder = max(target_area * 0.03, total_area * 1e-7)
        target_areas = [float(target_area)] * full_count
        if remainder > min_remainder:
            target_areas.append(float(remainder))
        else:
            target_areas[-1] += float(remainder)

        if len(target_areas) <= 1:
            return [geom]

        weighted = self._split_geom_connected_weighted(geom, target_areas, sweep_from_low)
        if weighted and len(weighted) == len(target_areas):
            return weighted

        # Last resort: keep the old target-area sweep, but collapse numerical
        # dust into adjacent parts so target-area mode never emits pointless
        # tiny intermediate features.
        fallback = self._split_geom(geom, target_area, sweep_from_low)
        return self._cleanup_area_mode_fallback(fallback, target_area, total_area)

    def _cleanup_area_mode_fallback(self, parts, target_area, total_area):
        clean = []
        tiny_limit = max(target_area * 0.20, total_area * 1e-7)
        for part in parts:
            if part is None or part.isEmpty():
                continue
            singles = self._decompose_multiparts([self._clean_geometry(part)])
            for g in singles:
                if g is None or g.isEmpty():
                    continue
                area = self.da.measureArea(g)
                if area <= tiny_limit and clean:
                    clean[-1] = self._clean_geometry(clean[-1].combine(g))
                else:
                    clean.append(g)
        if len(clean) >= 2 and self.da.measureArea(clean[-1]) <= tiny_limit:
            clean[-2] = self._clean_geometry(clean[-2].combine(clean[-1]))
            clean = clean[:-1]
        return clean

    def _split_geom_connected_weighted(self, geom, target_areas, sweep_from_low):
        """Split a polygon into connected parts with per-region target areas."""
        if geom is None or geom.isEmpty():
            return []
        if not target_areas:
            return []
        if len(target_areas) == 1:
            return [geom]

        total_area = self.da.measureArea(geom)
        if total_area <= 0:
            return []

        # Normalise targets to exactly the measured area.  This prevents the
        # weighted quality score from chasing a rounding discrepancy.
        target_sum = sum(float(v) for v in target_areas)
        if target_sum <= 0:
            return []
        scale = total_area / target_sum
        targets = [float(v) * scale for v in target_areas]
        num_parts = len(targets)
        smallest_target = max(min(targets), 1e-9)

        center = geom.boundingBox().center()
        rot_angle = self.cut_line_angle_deg
        rotated = QgsGeometry(geom)
        rotated.rotate(rot_angle, center)
        rotated = self._clean_geometry(rotated)
        if rotated.isEmpty():
            return []

        candidates = []
        max_candidates = max(4, min(16, 4 + self.precision * 3))
        for step_factor in self._precision_step_factors():
            # Use the smallest target as the graph scale so the final remainder
            # can still be represented by enough cells.
            nodes, adjacency = self._build_strip_fragment_graph(rotated, smallest_target, step_factor)
            if not nodes or len(nodes) < num_parts:
                continue
            for seed_offset in self._precision_seed_offsets():
                parts_try = self._partition_nodes_into_weighted_regions(
                    nodes, adjacency, targets, sweep_from_low, seed_offset
                )
                if len(parts_try) != num_parts:
                    continue
                quality = self._weighted_partition_quality(parts_try, targets)
                candidates.append((quality, parts_try))
                candidates.sort(key=lambda item: item[0])
                if len(candidates) > max_candidates:
                    candidates = candidates[:max_candidates]

        if not candidates:
            return []

        best_candidate = None
        for quality, parts_try in candidates:
            polished = self._polish_adjacent_parts_weighted(parts_try, targets, sweep_from_low)
            polished_quality = self._weighted_partition_quality(polished, targets)
            candidate = (polished_quality, polished)
            if best_candidate is None or candidate[0] < best_candidate[0]:
                best_candidate = candidate

        parts_rot = best_candidate[1]
        out_parts = []
        for g in parts_rot:
            if g is None or g.isEmpty():
                continue
            back = QgsGeometry(g)
            back.rotate(-rot_angle, center)
            back = self._clean_geometry(back)
            single_parts = self._decompose_multiparts([back])
            if len(single_parts) != 1:
                if single_parts:
                    back = max(single_parts, key=lambda gg: self.da.measureArea(gg))
                else:
                    continue
            else:
                back = single_parts[0]
            out_parts.append(back)

        return out_parts

    def _split_geom_connected_count(self, geom, num_parts, sweep_from_low):
        """Split a polygon into exactly num_parts connected parts using a graph
        partition built from thin strip fragments in the rotated frame."""
        if geom is None or geom.isEmpty() or num_parts <= 1:
            return [geom] if geom and not geom.isEmpty() else []

        total_area = self.da.measureArea(geom)
        if total_area <= 0:
            return []
        target_area = total_area / float(num_parts)

        strict = self._split_geom_strict_count(geom, num_parts, sweep_from_low)
        dbg(f"    strict count split -> {None if strict is None else len(strict)}")
        if strict is not None:
            return strict

        self._record_fallback(
            "Strict straight-cut splitting was not possible for at least one polygon; "
            "the connected fallback was used and may add short boundary segments."
        )

        center = geom.boundingBox().center()
        rot_angle = self.cut_line_angle_deg
        rotated = QgsGeometry(geom)
        rotated.rotate(rot_angle, center)
        rotated = self._clean_geometry(rotated)
        if rotated.isEmpty():
            return []

        # Try several strip resolutions and keep a short list of the best
        # exact-count connected candidates.  Finer is not always better on
        # narrow-neck concave polygons, so quality must be measured instead
        # of assumed.  The best candidates are then polished by exact pairwise
        # re-splitting of neighbouring regions.
        candidates = []
        max_candidates = max(3, min(12, 3 + self.precision * 2))
        for step_factor in self._precision_step_factors():
            nodes, adjacency = self._build_strip_fragment_graph(rotated, target_area, step_factor)
            if not nodes or len(nodes) < num_parts:
                continue
            for seed_offset in self._precision_seed_offsets():
                parts_try = self._partition_nodes_into_regions(
                    nodes, adjacency, num_parts, sweep_from_low, seed_offset
                )
                if len(parts_try) != num_parts:
                    continue
                quality = self._partition_quality(parts_try, target_area)
                candidates.append((quality, parts_try))
                candidates.sort(key=lambda item: item[0])
                if len(candidates) > max_candidates:
                    candidates = candidates[:max_candidates]

        if not candidates:
            # Last resort: legacy splitter.
            return self._split_geom(geom, target_area, sweep_from_low)

        best_candidate = None
        for quality, parts_try in candidates:
            polished = self._polish_adjacent_parts_exact(parts_try, target_area, sweep_from_low)
            polished_quality = self._partition_quality(polished, target_area)
            candidate = (polished_quality, polished)
            if best_candidate is None or candidate[0] < best_candidate[0]:
                best_candidate = candidate

        parts_rot = best_candidate[1]

        out_parts = []
        for g in parts_rot:
            back = QgsGeometry(g)
            back.rotate(-rot_angle, center)
            back = self._clean_geometry(back)
            single_parts = self._decompose_multiparts([back])
            if len(single_parts) != 1:
                # if numerical artifacts create a multipart, keep the largest
                if single_parts:
                    back = max(single_parts, key=lambda gg: self.da.measureArea(gg))
                else:
                    continue
            else:
                back = single_parts[0]
            out_parts.append(back)

        return out_parts

    def _polish_adjacent_parts_exact(self, parts, target_area, sweep_from_low):
        """Improve area balance by re-splitting adjacent connected regions.

        The graph partition guarantees connectivity but its area precision is
        limited by whole strip-fragment moves.  This polishing pass takes each
        neighbouring pair, unions it, and uses a binary-search horizontal cut
        in the already-rotated coordinate frame to give one side an exact
        target area.  The pair is accepted only if both resulting pieces remain
        single connected polygons and the area error decreases.
        """
        if not parts or len(parts) < 2:
            return parts

        parts = [QgsGeometry(p) for p in parts if p is not None and not p.isEmpty()]
        if len(parts) < 2:
            return parts

        # Keep a stable sweep order.  This is important because only adjacent
        # pieces are pair-polished.
        parts = self._sort_rotated_parts_by_sweep(parts, sweep_from_low)

        passes = 1 + (self.precision * 2)
        if self.precision >= 4:
            passes += 4
        if self.precision >= 5:
            passes += 6

        min_improvement = target_area * 1e-5
        for pass_idx in range(passes):
            changed = False
            # Alternate directions: forward makes the lower/earlier side exact;
            # backward makes the upper/later side exact.  This prevents all
            # accumulated error being pushed into only the final part.
            if pass_idx % 2 == 0:
                indices = range(0, len(parts) - 1)
                target_first = True
            else:
                indices = range(len(parts) - 2, -1, -1)
                target_first = False

            for i in indices:
                a = parts[i]
                b = parts[i + 1]
                if a is None or b is None or a.isEmpty() or b.isEmpty():
                    continue

                before = (
                    abs(self.da.measureArea(a) - target_area) +
                    abs(self.da.measureArea(b) - target_area)
                )
                if before <= min_improvement:
                    continue

                if target_first:
                    split = self._resplit_pair_targeting_side(a, b, target_area)
                    if split is None:
                        continue
                    new_a, new_b = split
                else:
                    split = self._resplit_pair_targeting_side(b, a, target_area)
                    if split is None:
                        continue
                    new_b, new_a = split

                after = (
                    abs(self.da.measureArea(new_a) - target_area) +
                    abs(self.da.measureArea(new_b) - target_area)
                )
                if after + min_improvement < before:
                    parts[i] = new_a
                    parts[i + 1] = new_b
                    changed = True

            if not changed:
                break

        return parts

    def _sort_rotated_parts_by_sweep(self, parts, sweep_from_low):
        def cy(geom):
            try:
                return geom.centroid().asPoint().y()
            except Exception:
                return geom.boundingBox().center().y()
        return sorted(parts, key=cy, reverse=not sweep_from_low)

    def _resplit_pair_targeting_side(self, target_side_geom, other_side_geom, target_area):
        """Return (new_target_side, new_other_side), or None if the pair cannot
        be safely re-split without creating a multipart/disconnected piece."""
        pair_union = self._clean_geometry(target_side_geom.combine(other_side_geom))
        if pair_union is None or pair_union.isEmpty():
            return None
        if not self._is_single_connected_polygon(pair_union):
            return None

        total = self.da.measureArea(pair_union)
        if total <= 0 or target_area <= 0 or target_area >= total:
            return None

        try:
            target_cy = target_side_geom.centroid().asPoint().y()
            other_cy = other_side_geom.centroid().asPoint().y()
        except Exception:
            target_cy = target_side_geom.boundingBox().center().y()
            other_cy = other_side_geom.boundingBox().center().y()

        from_low = target_cy <= other_cy
        split = self._split_rotated_geom_by_horizontal_area(pair_union, target_area, from_low)
        if split is None:
            return None
        new_target, new_other = split

        if not self._is_single_connected_polygon(new_target):
            return None
        if not self._is_single_connected_polygon(new_other):
            return None
        return new_target, new_other

    def _split_rotated_geom_by_horizontal_area(self, geom, target_area, from_low):
        """Binary-search a horizontal cut in rotated coordinates.

        Returns (target_side, remainder) where target_side is the low-side or
        high-side piece according to from_low.  The best connected result found
        during the search is returned, not just the final binary-search state.
        """
        bbox = geom.boundingBox()
        low = bbox.yMinimum()
        high = bbox.yMaximum()
        if high - low <= 1e-9:
            return None

        n_iter = 55 + (self.precision * 15)
        best = None
        best_err = None

        for _ in range(n_iter):
            mid = (low + high) / 2.0
            if from_low:
                rect = QgsRectangle(
                    bbox.xMinimum() - 1.0, bbox.yMinimum() - 1.0,
                    bbox.xMaximum() + 1.0, mid
                )
            else:
                rect = QgsRectangle(
                    bbox.xMinimum() - 1.0, mid,
                    bbox.xMaximum() + 1.0, bbox.yMaximum() + 1.0
                )

            clip = QgsGeometry.fromRect(rect)
            side = self._clean_geometry(geom.intersection(clip))
            rem = self._clean_geometry(geom.difference(clip))
            side_area = self.da.measureArea(side) if side and not side.isEmpty() else 0.0

            if side is not None and rem is not None and not side.isEmpty() and not rem.isEmpty():
                if self._is_single_connected_polygon(side) and self._is_single_connected_polygon(rem):
                    err = abs(side_area - target_area)
                    if best is None or err < best_err:
                        best = (side, rem)
                        best_err = err

            # Monotonic area update is valid even when the temporary geometry is
            # disconnected; connectedness is only required for accepted results.
            if side_area < target_area:
                if from_low:
                    low = mid
                else:
                    high = mid
            else:
                if from_low:
                    high = mid
                else:
                    low = mid

        if best is None:
            return None
        return best

    def _is_single_connected_polygon(self, geom):
        if geom is None or geom.isEmpty():
            return False
        try:
            cleaned = self._clean_geometry(geom)
        except Exception:
            cleaned = geom
        if cleaned is None or cleaned.isEmpty():
            return False
        parts = self._decompose_multiparts([cleaned])
        return len(parts) == 1 and not parts[0].isEmpty() and self.da.measureArea(parts[0]) > self._tiny_area()

    def _partition_quality(self, parts, target_area):
        """Lower is better. Penalise bad area balance, missing parts and multipart results."""
        if not parts:
            return (999999.0, 999999.0, 999999.0)
        deviations = []
        multipart_penalty = 0
        for part in parts:
            if part is None or part.isEmpty():
                deviations.append(999999.0)
                multipart_penalty += 1
                continue
            singles = self._decompose_multiparts([part])
            if len(singles) != 1:
                multipart_penalty += 10
            area = self.da.measureArea(part)
            deviations.append(abs(area - target_area) / max(target_area, 1e-9))
        return (multipart_penalty, max(deviations), sum(deviations))

    def _precision_step_factors(self):
        """Return strip-resolution trials for the selected precision.
        Lower values mean finer strips. More trials improve area balance but
        increase preview/apply time."""
        tables = {
            1: (1.10, 0.90, 0.75),
            2: (1.05, 0.85, 0.70, 0.55),
            3: (1.00, 0.80, 0.65, 0.50, 0.35, 0.25),
            4: (0.95, 0.75, 0.60, 0.48, 0.38, 0.30, 0.24, 0.19),
            5: (0.90, 0.72, 0.58, 0.46, 0.36, 0.28, 0.22, 0.17, 0.13, 0.10),
        }
        return tables.get(self.precision, tables[3])

    def _precision_seed_offsets(self):
        """Try several initial seed placements at higher precision.
        This often improves balance on C/L-shaped polygons where one seed
        location can trap a narrow lobe into an undersized region."""
        tables = {
            1: (0.50,),
            2: (0.40, 0.50, 0.60),
            3: (0.30, 0.40, 0.50, 0.60, 0.70),
            4: (0.25, 0.35, 0.45, 0.50, 0.55, 0.65, 0.75),
            5: (0.20, 0.28, 0.36, 0.44, 0.50, 0.56, 0.64, 0.72, 0.80),
        }
        return tables.get(self.precision, tables[3])

    def _build_strip_fragment_graph(self, rotated_geom, target_area, step_factor=1.0):
        bbox = rotated_geom.boundingBox()
        height = max(1e-9, bbox.yMaximum() - bbox.yMinimum())
        base = math.sqrt(max(target_area, 1e-9))

        min_divisor = 80.0 + (float(self.precision) * 60.0)
        step = max(height / min_divisor, min(height / 12.0, (base / 8.0) * step_factor))
        if step <= 0:
            step = height / 50.0
        if step <= 0:
            step = 1.0

        y_levels = set([bbox.yMinimum(), bbox.yMaximum()])
        for y in self._collect_polygon_y_values(rotated_geom):
            if bbox.yMinimum() <= y <= bbox.yMaximum():
                y_levels.add(float(y))

        y = bbox.yMinimum()
        guard = 0
        while y < bbox.yMaximum() - 1e-9 and guard < 5000:
            y_levels.add(float(y))
            y += step
            guard += 1
        y_levels.add(bbox.yMaximum())
        ys = sorted(y_levels)

        nodes = {}
        strips = []
        node_id = 1
        min_area = target_area * 1e-8

        for sidx in range(len(ys) - 1):
            y0 = ys[sidx]
            y1 = ys[sidx + 1]
            if y1 - y0 <= 1e-9:
                continue
            rect = QgsRectangle(bbox.xMinimum() - 1.0, y0, bbox.xMaximum() + 1.0, y1)
            strip_geom = rotated_geom.intersection(QgsGeometry.fromRect(rect))
            strip_geom = self._clean_geometry(strip_geom)
            frag_ids = []
            for frag in self._decompose_multiparts([strip_geom]):
                frag = self._clean_geometry(frag)
                area = self.da.measureArea(frag)
                if frag.isEmpty() or area <= min_area:
                    continue
                c = frag.centroid().asPoint()
                nodes[node_id] = {
                    'geom': frag,
                    'area': area,
                    'cy': c.y(),
                    'cx': c.x(),
                    'strip': len(strips),
                }
                frag_ids.append(node_id)
                node_id += 1
            strips.append(frag_ids)

        adjacency = {i: set() for i in nodes.keys()}
        # Connect fragments in neighbouring strips without using QgsGeometry.boundary(),
        # which is not available in older QGIS Python APIs.  For horizontal strip
        # fragments, shared connectedness is well represented by:
        #   1) positive X-range overlap, and
        #   2) near-zero geometry distance.
        # This avoids false adjacency across gaps while staying compatible with
        # QGIS 3.16+.
        bbox_width = max(1e-9, bbox.xMaximum() - bbox.xMinimum())
        bbox_height = max(1e-9, bbox.yMaximum() - bbox.yMinimum())
        tol = max(bbox_width, bbox_height) * 1e-9
        if tol <= 0:
            tol = 1e-8

        for sidx in range(len(strips) - 1):
            for a in strips[sidx]:
                ga = nodes[a]['geom']
                ba = ga.boundingBox()
                for b in strips[sidx + 1]:
                    gb = nodes[b]['geom']
                    bb = gb.boundingBox()
                    x_overlap = min(ba.xMaximum(), bb.xMaximum()) - max(ba.xMinimum(), bb.xMinimum())
                    if x_overlap <= tol:
                        continue
                    try:
                        if ga.distance(gb) <= max(tol, 1e-8):
                            adjacency[a].add(b)
                            adjacency[b].add(a)
                    except Exception:
                        # Fallback to a conservative QGIS predicate check.
                        try:
                            if ga.touches(gb) or ga.intersects(gb):
                                adjacency[a].add(b)
                                adjacency[b].add(a)
                        except Exception:
                            pass

        return nodes, adjacency

    def _collect_polygon_y_values(self, geom):
        vals = []
        if geom is None or geom.isEmpty():
            return vals
        try:
            flat = QgsWkbTypes.flatType(geom.wkbType())
            polys = []
            if flat == WKB_POLYGON:
                polys = [geom.asPolygon()]
            elif flat == WKB_MULTIPOLYGON:
                polys = geom.asMultiPolygon()
            for poly in polys:
                for ring in poly:
                    for pt in ring:
                        vals.append(pt.y())
        except Exception:
            pass
        return vals

    def _node_sort_key(self, node_data, sweep_from_low):
        if sweep_from_low:
            return (node_data['strip'], node_data['cy'], node_data['cx'])
        return (-node_data['strip'], -node_data['cy'], node_data['cx'])

    def _pick_seed_nodes(self, nodes, num_parts, sweep_from_low, seed_offset=0.5):
        ordered = sorted(nodes.keys(), key=lambda i: self._node_sort_key(nodes[i], sweep_from_low))
        total_area = sum(nodes[i]['area'] for i in ordered)
        if total_area <= 0:
            return ordered[:num_parts]

        seeds = []
        cum = 0.0
        seed_offset = max(0.05, min(0.95, float(seed_offset)))
        thresholds = [((idx + seed_offset) / float(num_parts)) * total_area for idx in range(num_parts)]
        tidx = 0
        for node_id in ordered:
            cum += nodes[node_id]['area']
            while tidx < len(thresholds) and cum >= thresholds[tidx]:
                if node_id not in seeds:
                    seeds.append(node_id)
                tidx += 1
        # Fill any missing seeds with unchosen nodes in order.
        for node_id in ordered:
            if len(seeds) >= num_parts:
                break
            if node_id not in seeds:
                seeds.append(node_id)
        return seeds[:num_parts]

    def _pick_weighted_seed_nodes(self, nodes, targets, sweep_from_low, seed_offset=0.5):
        ordered = sorted(nodes.keys(), key=lambda i: self._node_sort_key(nodes[i], sweep_from_low))
        total_area = sum(nodes[i]['area'] for i in ordered)
        if total_area <= 0:
            return ordered[:len(targets)]

        target_sum = sum(float(t) for t in targets)
        if target_sum <= 0:
            return ordered[:len(targets)]

        seed_offset = max(0.05, min(0.95, float(seed_offset)))
        thresholds = []
        acc = 0.0
        for target in targets:
            thresholds.append(((acc + (seed_offset * float(target))) / target_sum) * total_area)
            acc += float(target)

        seeds = []
        cum = 0.0
        tidx = 0
        for node_id in ordered:
            cum += nodes[node_id]['area']
            while tidx < len(thresholds) and cum >= thresholds[tidx]:
                if node_id not in seeds:
                    seeds.append(node_id)
                tidx += 1

        for node_id in ordered:
            if len(seeds) >= len(targets):
                break
            if node_id not in seeds:
                seeds.append(node_id)
        return seeds[:len(targets)]

    def _partition_nodes_into_weighted_regions(self, nodes, adjacency, targets, sweep_from_low, seed_offset=0.5):
        all_ids = set(nodes.keys())
        num_parts = len(targets)
        if len(all_ids) < num_parts:
            return []

        seed_ids = self._pick_weighted_seed_nodes(nodes, targets, sweep_from_low, seed_offset)
        if len(seed_ids) != num_parts:
            return []

        region_nodes = [set([sid]) for sid in seed_ids]
        region_areas = [nodes[sid]['area'] for sid in seed_ids]
        unassigned = set(all_ids) - set(seed_ids)

        guard = 0
        while unassigned and guard < len(all_ids) * 30:
            guard += 1
            progressed = False

            # Grow the most under-filled region first.
            region_order = sorted(
                range(num_parts),
                key=lambda rid: (region_areas[rid] / max(targets[rid], 1e-9), rid)
            )
            for rid in region_order:
                frontier = self._region_frontier(region_nodes[rid], adjacency, unassigned)
                if not frontier:
                    continue
                nid = self._best_frontier_node(nodes, frontier, region_areas[rid], targets[rid], sweep_from_low)
                if nid is None:
                    continue
                region_nodes[rid].add(nid)
                region_areas[rid] += nodes[nid]['area']
                unassigned.remove(nid)
                progressed = True
                if not unassigned:
                    break

            if not progressed:
                forced = self._force_attach_unassigned_weighted(
                    nodes, adjacency, region_nodes, region_areas, unassigned, targets, sweep_from_low
                )
                if not forced:
                    break

        self._rebalance_regions_weighted(nodes, adjacency, region_nodes, region_areas, targets)

        geoms = []
        for rnodes in region_nodes:
            g = self._union_node_geometries(nodes, rnodes)
            if g is None or g.isEmpty():
                return []
            single = self._decompose_multiparts([g])
            if len(single) != 1:
                return []
            geoms.append(single[0])
        return geoms

    def _force_attach_unassigned_weighted(self, nodes, adjacency, region_nodes, region_areas,
                                          unassigned, targets, sweep_from_low):
        best = None
        for rid, rnodes in enumerate(region_nodes):
            frontier = self._region_frontier(rnodes, adjacency, unassigned)
            if not frontier:
                continue
            nid = self._best_frontier_node(nodes, frontier, region_areas[rid], targets[rid], sweep_from_low)
            if nid is None:
                continue
            cand = (abs((region_areas[rid] + nodes[nid]['area']) - targets[rid]) / max(targets[rid], 1e-9), rid, nid)
            if best is None or cand < best:
                best = cand
        if best is None:
            return False
        _, rid, nid = best
        region_nodes[rid].add(nid)
        region_areas[rid] += nodes[nid]['area']
        unassigned.remove(nid)
        return True

    def _rebalance_regions_weighted(self, nodes, adjacency, region_nodes, region_areas, targets):
        improved = True
        iteration = 0
        max_iterations = 250 + (self.precision * 250)
        while improved and iteration < max_iterations:
            iteration += 1
            improved = False

            receivers = sorted(
                range(len(region_nodes)),
                key=lambda rid: ((region_areas[rid] - targets[rid]) / max(targets[rid], 1e-9), rid)
            )
            donors = list(reversed(receivers))

            for recv in receivers:
                for donor in donors:
                    if donor == recv:
                        continue
                    if region_areas[donor] <= targets[donor] * 0.98:
                        continue
                    if region_areas[recv] >= targets[recv] * 1.02:
                        continue
                    move = self._find_best_boundary_move_weighted(
                        nodes, adjacency, region_nodes, region_areas, donor, recv, targets
                    )
                    if move is None:
                        continue
                    nid = move
                    region_nodes[donor].remove(nid)
                    region_nodes[recv].add(nid)
                    region_areas[donor] -= nodes[nid]['area']
                    region_areas[recv] += nodes[nid]['area']
                    improved = True
                    break
                if improved:
                    break

    def _find_best_boundary_move_weighted(self, nodes, adjacency, region_nodes, region_areas,
                                          donor, recv, targets):
        donor_nodes = region_nodes[donor]
        recv_nodes = region_nodes[recv]
        if len(donor_nodes) <= 1:
            return None

        before = (
            abs(region_areas[donor] - targets[donor]) / max(targets[donor], 1e-9) +
            abs(region_areas[recv] - targets[recv]) / max(targets[recv], 1e-9)
        )

        candidates = []
        for nid in donor_nodes:
            if not any(nb in recv_nodes for nb in adjacency.get(nid, set())):
                continue
            donor_after = donor_nodes - {nid}
            if not self._node_set_is_connected(donor_after, adjacency):
                continue

            d_area = region_areas[donor] - nodes[nid]['area']
            r_area = region_areas[recv] + nodes[nid]['area']
            after = (
                abs(d_area - targets[donor]) / max(targets[donor], 1e-9) +
                abs(r_area - targets[recv]) / max(targets[recv], 1e-9)
            )
            if after + 1e-9 < before:
                candidates.append((after, nodes[nid]['area'], nid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def _weighted_partition_quality(self, parts, targets):
        if not parts or len(parts) != len(targets):
            return (999999.0, 999999.0, 999999.0)
        deviations = []
        multipart_penalty = 0
        for part, target in zip(parts, targets):
            if part is None or part.isEmpty():
                deviations.append(999999.0)
                multipart_penalty += 1
                continue
            singles = self._decompose_multiparts([part])
            if len(singles) != 1:
                multipart_penalty += 10
            area = self.da.measureArea(part)
            deviations.append(abs(area - target) / max(target, 1e-9))
        return (multipart_penalty, max(deviations), sum(deviations))

    def _polish_adjacent_parts_weighted(self, parts, targets, sweep_from_low):
        if not parts or len(parts) < 2 or len(parts) != len(targets):
            return parts

        paired = [(QgsGeometry(p), float(t)) for p, t in zip(parts, targets)
                  if p is not None and not p.isEmpty()]
        if len(paired) < 2:
            return [p for p, _ in paired]

        def cy(pair):
            geom, _target = pair
            try:
                return geom.centroid().asPoint().y()
            except Exception:
                return geom.boundingBox().center().y()

        paired = sorted(paired, key=cy, reverse=not sweep_from_low)
        parts = [p for p, _ in paired]
        targets = [t for _p, t in paired]

        passes = 1 + (self.precision * 2)
        if self.precision >= 4:
            passes += 5
        if self.precision >= 5:
            passes += 8

        min_improvement = 1e-5
        for pass_idx in range(passes):
            changed = False
            indices = range(0, len(parts) - 1) if pass_idx % 2 == 0 else range(len(parts) - 2, -1, -1)

            for i in indices:
                a = parts[i]
                b = parts[i + 1]
                ta = targets[i]
                tb = targets[i + 1]
                if a is None or b is None or a.isEmpty() or b.isEmpty():
                    continue

                before = (
                    abs(self.da.measureArea(a) - ta) / max(ta, 1e-9) +
                    abs(self.da.measureArea(b) - tb) / max(tb, 1e-9)
                )

                if pass_idx % 2 == 0:
                    split = self._resplit_pair_targeting_side(a, b, ta)
                    if split is None:
                        continue
                    new_a, new_b = split
                else:
                    split = self._resplit_pair_targeting_side(b, a, tb)
                    if split is None:
                        continue
                    new_b, new_a = split

                after = (
                    abs(self.da.measureArea(new_a) - ta) / max(ta, 1e-9) +
                    abs(self.da.measureArea(new_b) - tb) / max(tb, 1e-9)
                )
                if after + min_improvement < before:
                    parts[i] = new_a
                    parts[i + 1] = new_b
                    changed = True

            if not changed:
                break

        return parts

    def _partition_nodes_into_regions(self, nodes, adjacency, num_parts, sweep_from_low, seed_offset=0.5):
        all_ids = set(nodes.keys())
        if len(all_ids) < num_parts:
            return []

        seed_ids = self._pick_seed_nodes(nodes, num_parts, sweep_from_low, seed_offset)
        if len(seed_ids) != num_parts:
            return []

        region_nodes = [set([sid]) for sid in seed_ids]
        region_areas = [nodes[sid]['area'] for sid in seed_ids]
        unassigned = set(all_ids) - set(seed_ids)
        target_area = sum(nodes[i]['area'] for i in all_ids) / float(num_parts)

        guard = 0
        while unassigned and guard < len(all_ids) * 20:
            guard += 1
            progressed = False

            region_order = sorted(
                range(num_parts),
                key=lambda rid: (region_areas[rid] / max(target_area, 1e-9), rid)
            )
            for rid in region_order:
                frontier = self._region_frontier(region_nodes[rid], adjacency, unassigned)
                if not frontier:
                    continue
                nid = self._best_frontier_node(nodes, frontier, region_areas[rid], target_area, sweep_from_low)
                if nid is None:
                    continue
                region_nodes[rid].add(nid)
                region_areas[rid] += nodes[nid]['area']
                unassigned.remove(nid)
                progressed = True
                if not unassigned:
                    break

            if not progressed:
                # Force-attach the remaining nodes to the nearest reachable region.
                forced = self._force_attach_unassigned(nodes, adjacency, region_nodes, region_areas, unassigned, target_area, sweep_from_low)
                if not forced:
                    break

        # Rebalance using boundary-node moves.
        self._rebalance_regions(nodes, adjacency, region_nodes, region_areas, target_area)

        geoms = []
        for rnodes in region_nodes:
            g = self._union_node_geometries(nodes, rnodes)
            if g is None or g.isEmpty():
                return []
            single = self._decompose_multiparts([g])
            if len(single) != 1:
                return []
            geoms.append(single[0])
        return geoms

    def _region_frontier(self, region_node_ids, adjacency, unassigned):
        frontier = set()
        for nid in region_node_ids:
            for nb in adjacency.get(nid, set()):
                if nb in unassigned:
                    frontier.add(nb)
        return frontier

    def _best_frontier_node(self, nodes, frontier, current_area, target_area, sweep_from_low):
        if not frontier:
            return None
        def score(nid):
            n = nodes[nid]
            overshoot = max(0.0, current_area + n['area'] - target_area)
            undershoot = abs((current_area + n['area']) - target_area)
            skey = self._node_sort_key(n, sweep_from_low)
            return (overshoot, undershoot, skey, n['area'])
        return min(frontier, key=score)

    def _force_attach_unassigned(self, nodes, adjacency, region_nodes, region_areas, unassigned, target_area, sweep_from_low):
        # Attach one unassigned node that is adjacent to any region if possible.
        best = None
        for rid, rnodes in enumerate(region_nodes):
            frontier = self._region_frontier(rnodes, adjacency, unassigned)
            if not frontier:
                continue
            nid = self._best_frontier_node(nodes, frontier, region_areas[rid], target_area, sweep_from_low)
            if nid is None:
                continue
            cand = (abs((region_areas[rid] + nodes[nid]['area']) - target_area), rid, nid)
            if best is None or cand < best:
                best = cand
        if best is None:
            return False
        _, rid, nid = best
        region_nodes[rid].add(nid)
        region_areas[rid] += nodes[nid]['area']
        unassigned.remove(nid)
        return True

    def _rebalance_regions(self, nodes, adjacency, region_nodes, region_areas, target_area):
        improved = True
        iteration = 0
        max_iterations = 150 + (self.precision * 170)
        while improved and iteration < max_iterations:
            iteration += 1
            improved = False
            region_order = sorted(range(len(region_nodes)), key=lambda rid: region_areas[rid])
            receivers = region_order
            donors = list(reversed(region_order))
            for recv in receivers:
                for donor in donors:
                    if donor == recv or region_areas[donor] <= region_areas[recv]:
                        continue
                    move = self._find_best_boundary_move(nodes, adjacency, region_nodes, region_areas, donor, recv, target_area)
                    if move is None:
                        continue
                    nid = move
                    region_nodes[donor].remove(nid)
                    region_nodes[recv].add(nid)
                    region_areas[donor] -= nodes[nid]['area']
                    region_areas[recv] += nodes[nid]['area']
                    improved = True
                    break
                if improved:
                    break

    def _find_best_boundary_move(self, nodes, adjacency, region_nodes, region_areas, donor, recv, target_area):
        donor_nodes = region_nodes[donor]
        recv_nodes = region_nodes[recv]
        if len(donor_nodes) <= 1:
            return None

        candidates = []
        for nid in donor_nodes:
            if not any(nb in recv_nodes for nb in adjacency.get(nid, set())):
                continue
            donor_after = donor_nodes - {nid}
            if not self._node_set_is_connected(donor_after, adjacency):
                continue
            before = abs(region_areas[donor] - target_area) + abs(region_areas[recv] - target_area)
            after = abs((region_areas[donor] - nodes[nid]['area']) - target_area) + abs((region_areas[recv] + nodes[nid]['area']) - target_area)
            if after + 1e-9 < before:
                candidates.append((after, nodes[nid]['area'], nid))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def _node_set_is_connected(self, node_ids, adjacency):
        node_ids = set(node_ids)
        if not node_ids:
            return True
        root = next(iter(node_ids))
        stack = [root]
        seen = {root}
        while stack:
            cur = stack.pop()
            for nb in adjacency.get(cur, set()):
                if nb in node_ids and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        return len(seen) == len(node_ids)

    def _union_node_geometries(self, nodes, node_ids):
        node_ids = list(node_ids)
        if not node_ids:
            return QgsGeometry()
        g = QgsGeometry(nodes[node_ids[0]]['geom'])
        for nid in node_ids[1:]:
            g = g.combine(nodes[nid]['geom'])
            g = self._clean_geometry(g)
        return self._clean_geometry(g)

    def _binary_search_y(self, rotated_geom, bbox, target_area,
                         sweep_from_low, n_iter=50):
        """
        Binary search for the Y coordinate that clips exactly target_area
        from the rotated geometry.

        sweep_from_low=True:  clip from yMin up to result_y
        sweep_from_low=False: clip from result_y up to yMax
        """
        low = bbox.yMinimum()
        high = bbox.yMaximum()
        best = high if sweep_from_low else low

        for _ in range(n_iter):
            mid = (low + high) / 2.0
            if sweep_from_low:
                clip_rect = QgsRectangle(
                    bbox.xMinimum() - 1, bbox.yMinimum() - 1,
                    bbox.xMaximum() + 1, mid
                )
            else:
                clip_rect = QgsRectangle(
                    bbox.xMinimum() - 1, mid,
                    bbox.xMaximum() + 1, bbox.yMaximum() + 1
                )
            clip_geom = QgsGeometry.fromRect(clip_rect)
            temp = rotated_geom.intersection(clip_geom)
            temp = self._clean_geometry(temp)
            temp_area = self.da.measureArea(temp)

            if sweep_from_low:
                # Increasing mid increases clipped area.
                if temp_area < target_area:
                    low = mid
                else:
                    high = mid
                    best = mid
            else:
                # Increasing mid decreases clipped area.
                if temp_area < target_area:
                    high = mid
                else:
                    low = mid
                    best = mid

        return best

    def _decompose_multiparts(self, geoms):
        decomposed = []
        for geom in geoms:
            if geom is None or geom.isEmpty():
                continue
            if geom.isMultipart():
                for part in geom.asMultiPolygon():
                    g = QgsGeometry.fromPolygonXY(part)
                    if not g.isEmpty():
                        decomposed.append(g)
            elif geom.wkbType() == WKB_GEOMETRYCOLLECTION:
                for subgeom in geom.constGet():
                    g = QgsGeometry(subgeom)
                    if g.type() == GEOM_POLYGON and not g.isEmpty():
                        decomposed.append(g)
            else:
                decomposed.append(geom)
        return decomposed

    def _clean_geometry(self, geom):
        if geom is None or geom.isEmpty():
            return QgsGeometry()
        wkb = geom.wkbType()
        if wkb == WKB_GEOMETRYCOLLECTION or \
                QgsWkbTypes.flatType(wkb) == WKB_GEOMETRYCOLLECTION:
            cleaned = geom.buffer(0, 0)
            if cleaned and not cleaned.isEmpty() and \
                    QgsWkbTypes.flatType(cleaned.wkbType()) != WKB_GEOMETRYCOLLECTION:
                return cleaned
            parts = []
            for subgeom in geom.constGet():
                g = QgsGeometry(subgeom)
                if g.type() == GEOM_POLYGON:
                    parts.append(g)
            if parts:
                union_geom = parts[0]
                for part in parts[1:]:
                    union_geom = union_geom.combine(part)
                return union_geom
            return QgsGeometry()
        return geom


# ---------------------------------------------------------------------------
# Main Plugin Class
# ---------------------------------------------------------------------------

class PolygonSplitter:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.actions = []
        self.menu = "&Equalyzer"
        self.toolbar = self.iface.addToolBar("Equalyzer")
        self._preview_global_bands = set()
        self._signals_connected = False
        self._active_split_dialogs = set()

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), 'icon_area.png')
        self.equal_area_action = QAction(
            QIcon(icon_path),
            "Split into Equal Areas",
            self.iface.mainWindow()
        )
        self.equal_area_action.triggered.connect(lambda: self.start_split('area'))
        self.iface.addPluginToMenu(self.menu, self.equal_area_action)
        self.toolbar.addAction(self.equal_area_action)
        self.actions.append(self.equal_area_action)

        icon_path = os.path.join(os.path.dirname(__file__), 'icon_count.png')
        self.equal_parts_action = QAction(
            QIcon(icon_path),
            "Split into Equal Parts",
            self.iface.mainWindow()
        )
        self.equal_parts_action.triggered.connect(lambda: self.start_split('count'))
        self.iface.addPluginToMenu(self.menu, self.equal_parts_action)
        self.toolbar.addAction(self.equal_parts_action)
        self.actions.append(self.equal_parts_action)

        icon_path = os.path.join(os.path.dirname(__file__), 'icon_count.png')
        self.bays_action = QAction(
            QIcon(icon_path),
            "Split into Parking Bays",
            self.iface.mainWindow()
        )
        self.bays_action.triggered.connect(self.start_bays)
        self.iface.addPluginToMenu(self.menu, self.bays_action)
        self.toolbar.addAction(self.bays_action)
        self.actions.append(self.bays_action)

        try:
            iface.messageBar().pushInfo(
                "Equalyzer",
                f"Loaded v1.12.0 from {os.path.abspath(__file__)}"
            )
        except Exception:
            pass

        if not self._signals_connected:
            try:
                QgsProject.instance().cleared.connect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                QgsProject.instance().readProject.connect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                self.iface.newProjectCreated.connect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                self.iface.projectRead.connect(self._clear_global_preview_bands)
            except Exception:
                pass
            self._signals_connected = True

    def unload(self):
        self._clear_global_preview_bands()

        if self._signals_connected:
            try:
                QgsProject.instance().cleared.disconnect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                QgsProject.instance().readProject.disconnect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                self.iface.newProjectCreated.disconnect(self._clear_global_preview_bands)
            except Exception:
                pass
            try:
                self.iface.projectRead.disconnect(self._clear_global_preview_bands)
            except Exception:
                pass
            self._signals_connected = False

        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

    def _register_preview_band(self, band):
        if band is not None:
            self._preview_global_bands.add(band)

    def _unregister_preview_band(self, band):
        if band in self._preview_global_bands:
            self._preview_global_bands.remove(band)

    def _clear_global_preview_bands(self, *args):
        scene = iface.mapCanvas().scene()

        # First clear tracked bands.
        for band in list(self._preview_global_bands):
            try:
                band.reset()
            except Exception:
                pass
            try:
                scene.removeItem(band)
            except Exception:
                pass
            try:
                band.deleteLater()
            except Exception:
                pass
        self._preview_global_bands.clear()

        # Then clear any stale Equalyzer-marked bands left in scene.
        try:
            for item in list(scene.items()):
                if getattr(item, "_equalyzer_preview_band", False):
                    try:
                        scene.removeItem(item)
                    except Exception:
                        pass
        except Exception:
            pass

    def _log(self, message, level=Qgis.MessageLevel.Info, to_bar=False):
        QgsMessageLog.logMessage(message, "Equalyzer", level)
        if to_bar or level in (Qgis.MessageLevel.Warning, Qgis.MessageLevel.Critical):
            try:
                if level == Qgis.MessageLevel.Critical:
                    iface.messageBar().pushCritical("Equalyzer", message)
                elif level == Qgis.MessageLevel.Warning:
                    iface.messageBar().pushWarning("Equalyzer", message)
                else:
                    iface.messageBar().pushInfo("Equalyzer", message)
            except Exception:
                pass

    def _extract_multipolygon_parts(self, geom):
        """Return list of single-part multipolygon geometries extracted from geom."""
        if geom is None:
            return []

        g = QgsGeometry(geom)
        if g.isEmpty():
            return []

        try:
            if not g.isGeosValid():
                g = g.makeValid()
        except Exception:
            pass

        if g.isEmpty():
            return []

        flat = QgsWkbTypes.flatType(g.wkbType())
        out = []

        if flat == WKB_POLYGON:
            poly = g.asPolygon()
            if poly:
                mp = QgsGeometry.fromMultiPolygonXY([poly])
                if not mp.isEmpty():
                    out.append(mp)
        elif flat == WKB_MULTIPOLYGON:
            for poly in g.asMultiPolygon():
                if poly:
                    mp = QgsGeometry.fromMultiPolygonXY([poly])
                    if not mp.isEmpty():
                        out.append(mp)
        elif flat == WKB_GEOMETRYCOLLECTION:
            try:
                for sub in g.constGet():
                    out.extend(self._extract_multipolygon_parts(QgsGeometry(sub)))
            except Exception:
                cleaned = g.buffer(0, 0)
                if cleaned and not cleaned.isEmpty() and \
                        QgsWkbTypes.flatType(cleaned.wkbType()) != WKB_GEOMETRYCOLLECTION:
                    out.extend(self._extract_multipolygon_parts(cleaned))

        return out

    def _add_output_layer_with_fallback(self, output_layer):
        """
        Try to add in-memory layer first.
        If that fails, write a temporary GeoJSON and load it via OGR.

        Returns: (result_layer_or_None, note_or_None)
        """
        self._log("Trying to add in-memory output layer.")
        added = QgsProject.instance().addMapLayer(output_layer)
        if added is not None and added.isValid():
            self._log("In-memory output layer added successfully.")
            return added, None

        self._log(
            "Adding in-memory layer failed; attempting GeoJSON fallback.",
            Qgis.MessageLevel.Warning
        )

        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"equalyzer_split_{uuid.uuid4().hex[:10]}.geojson"
        )

        err_code, err_msg = compat_write_vector(output_layer, tmp_path, "GeoJSON")

        if err_code != QgsVectorFileWriter.WriterError.NoError:
            return None, f"Fallback GeoJSON write failed (code={err_code}): {err_msg}"

        file_layer = QgsVectorLayer(tmp_path, "Split Parts", "ogr")
        if not file_layer.isValid():
            return None, f"Fallback GeoJSON layer invalid: {tmp_path}"

        added_file = QgsProject.instance().addMapLayer(file_layer)
        if added_file is None or not added_file.isValid():
            return None, f"Fallback file layer could not be added: {tmp_path}"

        self._log(f"Fallback output layer added from temporary file: {tmp_path}", Qgis.MessageLevel.Warning)
        return added_file, f"Output added via fallback file: {tmp_path}"

    def _restore_active_layer(self, layer):
        """Put the focus back on the layer that was split.

        Adding the output layer makes it the active one, which breaks the
        rhythm of drawing a strip, splitting it, drawing the next.
        """
        try:
            if layer is not None and layer.id() in QgsProject.instance().mapLayers():
                self.iface.setActiveLayer(layer)
        except Exception as e:
            dbg(f"restoring the active layer failed: {e!r}")

    def _edit_source_layer(self, layer, work, description):
        """Run `work(layer)` inside one undoable edit command.

        If the layer is already in edit mode the change joins that session and
        the user commits it themselves, so it stays undoable. Otherwise a
        session is opened and committed here.
        """
        started = False
        if not layer.isEditable():
            if not layer.startEditing():
                return False, "The source layer could not be opened for editing."
            started = True

        ok = False
        try:
            layer.beginEditCommand(description)
            ok = bool(work(layer))
            layer.endEditCommand()
        except Exception as e:
            try:
                layer.destroyEditCommand()
            except Exception:
                pass
            if started:
                layer.rollBack()
            return False, f"Editing the source layer failed: {e}"

        if not ok:
            if started:
                layer.rollBack()
            return False, f"Editing the source layer failed: {layer.commitErrors()}"

        if started:
            if not layer.commitChanges():
                errors = "; ".join(layer.commitErrors())
                layer.rollBack()
                return False, f"Changes could not be saved: {errors}"
            return True, "saved"
        return True, "pending"

    def _copyable_attributes(self, layer):
        """Field indexes worth copying: everything except the primary key.

        A GeoPackage carries its fid in the field list; copying it onto several
        new features would collide, so those are left for the provider to fill.
        """
        try:
            keys = set(layer.primaryKeyAttributes())
        except Exception:
            keys = set()
        return [i for i in range(len(layer.fields())) if i not in keys]

    def _replace_in_source_layer(self, layer, parts_by_feature):
        """Put the parts into the source layer and remove the originals."""
        indexes = self._copyable_attributes(layer)
        fields = layer.fields()

        new_features = []
        source_ids = []
        for feature, parts in parts_by_feature:
            source_ids.append(feature.id())
            for part in parts:
                if part is None or part.isEmpty():
                    continue
                for geom in self._extract_multipolygon_parts(part):
                    if geom.isEmpty():
                        continue
                    new_feature = QgsFeature(fields)
                    new_feature.setGeometry(geom)
                    for index in indexes:
                        try:
                            new_feature.setAttribute(index, feature.attribute(index))
                        except Exception:
                            pass
                    new_features.append(new_feature)

        if not new_features:
            return False, "No parts to write back.", 0

        def work(target):
            return target.addFeatures(new_features) and target.deleteFeatures(source_ids)

        ok, state = self._edit_source_layer(
            layer, work, f"Equalyzer: replace {len(source_ids)} polygon(s) by "
                         f"{len(new_features)} part(s)"
        )
        return ok, state, len(new_features)

    def _delete_source_features(self, layer, polygon_features):
        ids = [f.id() for f in polygon_features]
        if not ids:
            return True, "saved"
        return self._edit_source_layer(
            layer, lambda target: target.deleteFeatures(ids),
            f"Equalyzer: remove {len(ids)} split polygon(s)"
        )

    def _create_temporary_output_layer(self, source_layer, parts_by_feature,
                                       da, project_unit, unit_abbrev,
                                       crs_area_unit):
        """Last-resort output path: build and add a fresh temporary memory layer."""
        crs_authid = source_layer.crs().authid()
        temp_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_authid}", "Split Parts (temp)", "memory")
        if not temp_layer.isValid():
            return None, "Emergency temp layer could not be created"

        provider = temp_layer.dataProvider()
        temp_fields, temp_attribute_map = build_output_fields(source_layer)
        provider.addAttributes(temp_fields)
        temp_layer.updateFields()

        if da.willUseEllipsoid():
            area_factor = QgsUnitTypes.fromUnitToUnitFactor(
                AREA_SQUARE_METERS, project_unit
            )
        else:
            area_factor = QgsUnitTypes.fromUnitToUnitFactor(crs_area_unit, project_unit)

        feats = []
        part_counter = 0

        for feature, parts in parts_by_feature:
            source_fid = feature.id()
            for part in parts:
                if part is None or part.isEmpty():
                    continue

                for multi_geom in self._extract_multipolygon_parts(part):
                    if multi_geom.isEmpty():
                        continue

                    part_counter += 1
                    area_display = da.measureArea(multi_geom) * area_factor

                    f = QgsFeature(temp_layer.fields())
                    f.setGeometry(multi_geom)
                    f.setAttribute("source_fid", int(source_fid) if source_fid is not None else -1)
                    f.setAttribute("part_id", part_counter)
                    f.setAttribute("area_val", float(area_display))
                    f.setAttribute("area_txt", f"{area_display:.4f} {unit_abbrev}")
                    for source_index, output_name in temp_attribute_map:
                        try:
                            f.setAttribute(output_name, feature.attribute(source_index))
                        except Exception:
                            pass
                    feats.append(f)

        if not feats:
            return None, "Emergency temp layer had no valid features to write"

        add_res = provider.addFeatures(feats)
        add_ok = bool(add_res[0]) if isinstance(add_res, tuple) else bool(add_res)
        temp_layer.updateExtents()
        if not add_ok or temp_layer.featureCount() == 0:
            return None, f"Emergency temp layer write failed: {provider.lastError()}"

        added = QgsProject.instance().addMapLayer(temp_layer)
        if added is None or not added.isValid():
            return None, "Emergency temp layer addMapLayer failed"

        return added, "Output added via emergency temporary memory layer"

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def start_split(self, mode):
        try:
            self._log(f"start_split called (mode={mode})", Qgis.MessageLevel.Info, to_bar=True)
            # Defensive cleanup before a new run.
            self._clear_global_preview_bands()
            self._run_split(mode)
        except Exception as e:
            QMessageBox.critical(None, "Equalyzer Error", str(e))

    def start_bays(self):
        try:
            self._clear_global_preview_bands()
            self._run_bays()
        except Exception as e:
            QMessageBox.critical(None, "Equalyzer Error", str(e))

    def _run_bays(self):
        """Cut parking strips into bays.

        With a selection it works straight away. Without one, draw a line over
        the strip you mean: the line only points at the polygon, the cut
        direction still comes from the strip's own axis.
        """
        layer = self.iface.activeLayer()
        if not layer or layer.type() != LAYER_VECTOR:
            raise Exception("Please select a vector layer first.")

        polygon_features = [
            f for f in layer.selectedFeatures()
            if f.geometry() is not None and f.geometry().type() == GEOM_POLYGON
        ]

        if polygon_features:
            self._open_bay_dialog(layer, polygon_features)
            return

        self._bay_pick_layer = layer
        self._bay_pick_tool = LineDrawTool(iface.mapCanvas(), self._on_bay_line_captured)
        iface.mapCanvas().setMapTool(self._bay_pick_tool)
        try:
            iface.messageBar().pushInfo(
                "Equalyzer",
                "Draw a line across the parking strip you want to divide."
            )
        except Exception:
            pass

    def _on_bay_line_captured(self, points):
        tool = getattr(self, "_bay_pick_tool", None)
        layer = getattr(self, "_bay_pick_layer", None)
        self._bay_pick_tool = None
        self._bay_pick_layer = None
        try:
            if tool is not None:
                iface.mapCanvas().unsetMapTool(tool)
        except Exception:
            pass

        if not points or layer is None:
            return

        feature = None
        try:
            feature = pick_feature_under_line(layer, points)
        except Exception as e:
            dbg(f"bay pick failed: {e!r}")

        if feature is None:
            QMessageBox.warning(
                None, "Equalyzer",
                "That line does not cross a polygon in the active layer.\n\n"
                "Draw it across the strip you want to divide, or select the "
                "strips yourself."
            )
            return

        try:
            layer.selectByIds([feature.id()])
        except Exception as e:
            dbg(f"selectByIds failed: {e!r}")
        self._open_bay_dialog(layer, [feature])

    def _open_bay_dialog(self, layer, polygon_features):
        measure = metric_length_measure(layer)
        if layer.crs().mapUnits() != DISTANCE_METERS:
            try:
                iface.messageBar().pushInfo(
                    "Equalyzer",
                    "This layer is not in a metric CRS, so strips are measured "
                    "and cut in the matching UTM zone and transformed back."
                )
            except Exception:
                pass

        da = make_distance_area(layer)

        project_unit = QgsProject.instance().areaUnits()
        unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_unit)

        dlg = BayPlanDialog(
            parent=self.iface.mainWindow(),
            layer=layer,
            polygon_features=polygon_features,
            da=da,
            unit_abbrev="m",
            measure=measure,
        )
        self._active_split_dialogs.add(dlg)
        setattr(dlg, "_equalyzer_apply_handled", False)
        dlg.accepted.connect(
            lambda d=dlg, lyr=layer, feats=polygon_features, dist=da,
                   punit=project_unit, uabbr=unit_abbrev:
            self._on_split_dialog_accepted(
                d, "bays", lyr, feats, dist, punit, uabbr, AREA_SQUARE_METERS
            )
        )
        dlg.finished.connect(lambda _res, d=dlg: self._on_split_dialog_finished(d))
        dlg.setModal(True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _run_split(self, mode):
        self._log(f"Entered _run_split (mode={mode})", Qgis.MessageLevel.Info, to_bar=True)
        layer = self.iface.activeLayer()
        if not layer or layer.type() != LAYER_VECTOR:
            raise Exception("Please select a vector layer first.")

        if layer.selectedFeatureCount() == 0:
            # Nothing selected: the direction line will pick the polygon it
            # runs through, and the dialog selects that feature in the layer.
            polygon_features = []
        else:
            polygon_features = [
                f for f in layer.selectedFeatures()
                if f.geometry() is not None
                and f.geometry().type() == GEOM_POLYGON
            ]
            if not polygon_features:
                raise Exception("No polygon features found in the selection.")

        # Set up distance/area measurement
        da = make_distance_area(layer)

        project_unit = QgsProject.instance().areaUnits()
        unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_unit)

        crs_distance_unit = layer.crs().mapUnits()
        if crs_distance_unit == DISTANCE_METERS:
            crs_area_unit = AREA_SQUARE_METERS
        elif crs_distance_unit == DISTANCE_FEET:
            crs_area_unit = AREA_SQUARE_FEET
        elif crs_distance_unit == DISTANCE_DEGREES:
            crs_area_unit = AREA_SQUARE_DEGREES
        else:
            crs_area_unit = AREA_SQUARE_METERS

        # Show the preview dialog
        dlg = SplitPreviewDialog(
            parent=self.iface.mainWindow(),
            mode=mode,
            layer=layer,
            polygon_features=polygon_features,
            da=da,
            project_unit=project_unit,
            unit_abbrev=unit_abbrev,
            crs_area_unit=crs_area_unit,
            register_preview_band=self._register_preview_band,
            unregister_preview_band=self._unregister_preview_band
        )
        self._active_split_dialogs.add(dlg)
        setattr(dlg, "_equalyzer_apply_handled", False)

        dlg.accepted.connect(
            lambda d=dlg, m=mode, lyr=layer,
                   dist=da, punit=project_unit, uabbr=unit_abbrev, carea=crs_area_unit:
            self._on_split_dialog_accepted(
                d, m, lyr, list(d.polygon_features), dist, punit, uabbr, carea
            )
        )
        dlg.finished.connect(lambda _res, d=dlg: self._on_split_dialog_finished(d))

        dlg.setModal(True)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._log("Split dialog opened (non-blocking mode).", Qgis.MessageLevel.Info, to_bar=True)

    def _on_split_dialog_finished(self, dlg):
        if dlg in self._active_split_dialogs:
            self._active_split_dialogs.remove(dlg)
        try:
            dlg.deleteLater()
        except Exception:
            pass

    def _on_split_dialog_accepted(self, dlg, mode, layer, polygon_features,
                                  da, project_unit, unit_abbrev, crs_area_unit):
        if getattr(dlg, "_equalyzer_apply_handled", False):
            return
        setattr(dlg, "_equalyzer_apply_handled", True)

        self._log("Apply callback entered.", Qgis.MessageLevel.Info, to_bar=True)

        try:
            params = dlg.get_parameters()
        except Exception as e:
            QMessageBox.critical(None, "Equalyzer Error", f"Failed to read dialog parameters: {e}")
            return

        if not polygon_features:
            QMessageBox.warning(
                None, "Equalyzer",
                "No polygon was selected or found under the direction line."
            )
            return

        try:
            self._clear_global_preview_bands()
            self._run_split_apply_from_params(
                mode, layer, polygon_features, da,
                project_unit, unit_abbrev, crs_area_unit,
                params
            )
        except Exception as e:
            QMessageBox.critical(None, "Equalyzer Error", f"Apply callback failed: {e}")

    def _run_split_apply_from_params(self, mode, layer, polygon_features, da,
                                     project_unit, unit_abbrev, crs_area_unit,
                                     params):
        if not params["direction_points"]:
            QMessageBox.warning(None, "Equalyzer", "No direction line was drawn. Operation cancelled.")
            return

        self._log("Apply started. Computing split parts…", Qgis.MessageLevel.Info, to_bar=True)

        # Deterministic apply path: always recompute from final dialog params.
        # This avoids any dependency on preview-cache/dialog lifecycle ordering.
        pt_a, pt_b = params["direction_points"]
        direction_angle = math.degrees(math.atan2(
            pt_b.y() - pt_a.y(),
            pt_b.x() - pt_a.x()
        ))
        self._log(
            f"Apply direction diagnostics -> drawn: {direction_angle:.2f}°, "
            f"effective cut: {direction_angle:.2f}° (parallel)",
            Qgis.MessageLevel.Info
        )

        engine = _SplitEngine(
            layer=layer,
            polygon_features=polygon_features,
            da=da,
            project_unit=project_unit,
            unit_abbrev=unit_abbrev,
            crs_area_unit=crs_area_unit,
            direction_points=params["direction_points"],
            start_point=params["start_point"],
            mode=mode,
            target_value=params["target_value"],
            precision=params.get("precision", 3),
            bay_settings=params.get("bay_settings")
        )

        try:
            parts_by_feature = engine.compute_parts_by_feature()
        except Exception as e:
            QMessageBox.critical(None, "Equalyzer Error", f"Splitting failed: {e}")
            return
        method_fallback_messages = list(getattr(engine, "fallback_messages", []))

        all_parts = []
        for _, parts in parts_by_feature:
            all_parts.extend(parts)

        if not all_parts:
            QMessageBox.warning(None, "Equalyzer", "No parts were produced. Check your parameters.")
            return

        self._log(f"Split computation produced {len(all_parts)} part(s).", Qgis.MessageLevel.Info, to_bar=True)
        if method_fallback_messages:
            self._log(
                "Strict split fallback used: " + " ".join(method_fallback_messages),
                Qgis.MessageLevel.Warning,
                to_bar=True
            )

        if params.get("replace_in_source"):
            ok, state, written = self._replace_in_source_layer(layer, parts_by_feature)
            if not ok:
                QMessageBox.critical(None, "Equalyzer Error", state)
                return
            try:
                layer.removeSelection()
                layer.triggerRepaint()
            except Exception:
                pass
            tail = ("" if state != "pending" else
                    "\n\nThe layer was already in edit mode, so the change is in its "
                    "edit buffer: undo with Ctrl+Z or save it yourself.")
            QMessageBox.information(
                None, "Equalyzer",
                f"Replaced {len(polygon_features)} polygon(s) in '{layer.name()}' "
                f"by {written} part(s)." + tail
            )
            self._log(f"Replaced in source layer: {written} part(s).",
                      Qgis.MessageLevel.Info, to_bar=True)
            self._restore_active_layer(layer)
            return

        # Output layer: reuse the Split Parts layer of this source layer when
        # there is one, so repeated splits collect in a single layer.
        output_fields, attribute_map = build_output_fields(layer)
        field_names = [f.name() for f in output_fields]

        output_layer = find_split_parts_layer(layer, field_names)
        reused = output_layer is not None
        existing_count = output_layer.featureCount() if reused else 0

        if not reused:
            crs_authid = layer.crs().authid()
            output_layer = QgsVectorLayer(
                f"MultiPolygon?crs={crs_authid}",
                f"Split Parts – {layer.name()}",
                "memory"
            )
            if not output_layer.isValid():
                QMessageBox.critical(None, "Equalyzer Error", "Failed to create output memory layer.")
                return
            output_layer.dataProvider().addAttributes(output_fields)
            output_layer.updateFields()
            output_layer.setCustomProperty("equalyzer/split_parts", "1")
            output_layer.setCustomProperty("equalyzer/source_layer", layer.id())

        provider = output_layer.dataProvider()

        if reused:
            # The layer is already in the project; nothing to add.
            result_layer = output_layer
            memory_added = True
            self._log(
                f"Appending to existing layer '{output_layer.name()}' "
                f"({existing_count} part(s) already present).",
                Qgis.MessageLevel.Info, to_bar=True
            )
        else:
            # Add layer first so Apply always has a visible target layer in project.
            result_layer = QgsProject.instance().addMapLayer(output_layer)
            memory_added = (
                result_layer is not None and result_layer.isValid() and
                output_layer.id() in QgsProject.instance().mapLayers()
            )
            if memory_added:
                self._log("Output layer inserted into project. Writing features…", Qgis.MessageLevel.Info, to_bar=True)
            else:
                self._log(
                    "Output layer could not be inserted before write; will use fallback path if needed.",
                    Qgis.MessageLevel.Warning,
                    to_bar=True
                )

        features_to_add = []
        part_counter = 0

        if da.willUseEllipsoid():
            area_factor = QgsUnitTypes.fromUnitToUnitFactor(
                AREA_SQUARE_METERS, project_unit
            )
        else:
            area_factor = QgsUnitTypes.fromUnitToUnitFactor(crs_area_unit, project_unit)

        for feature, parts in parts_by_feature:
            source_fid = feature.id()
            part_id = 0                      # numbered per source polygon
            for part in parts:
                if part is None or part.isEmpty():
                    continue

                for multi_geom in self._extract_multipolygon_parts(part):
                    if multi_geom.isEmpty():
                        continue

                    part_counter += 1
                    part_id += 1
                    area_display = da.measureArea(multi_geom) * area_factor

                    feat = QgsFeature(output_layer.fields())
                    feat.setGeometry(multi_geom)
                    feat.setAttribute("source_fid", int(source_fid) if source_fid is not None else -1)
                    feat.setAttribute("part_id", part_id)
                    feat.setAttribute("area_val", float(area_display))
                    feat.setAttribute("area_txt", f"{area_display:.4f} {unit_abbrev}")
                    for source_index, output_name in attribute_map:
                        try:
                            feat.setAttribute(output_name, feature.attribute(source_index))
                        except Exception:
                            pass
                    features_to_add.append(feat)

        self._log(f"Prepared {len(features_to_add)} feature(s) for output write.", Qgis.MessageLevel.Info, to_bar=True)

        if features_to_add:
            add_result = provider.addFeatures(features_to_add)
            if isinstance(add_result, tuple):
                add_ok = bool(add_result[0])
            else:
                add_ok = bool(add_result)
        else:
            add_ok = False
            self._log(
                "Primary output prepared 0 features; forcing emergency fallback path.",
                Qgis.MessageLevel.Warning
            )

        output_layer.updateExtents()
        added_count = max(0, output_layer.featureCount() - existing_count)

        if not add_ok and features_to_add:
            self._log(
                f"Provider addFeatures returned failure. Provider error: {provider.lastError()}",
                Qgis.MessageLevel.Warning
            )
        elif not features_to_add:
            self._log("Skipped provider.addFeatures because feature list is empty.", Qgis.MessageLevel.Warning)

        self._log(f"Output layer feature count after write: {added_count}")

        fallback_note = None
        if added_count == 0:
            detail = provider.lastError() if provider.lastError() else "Unknown provider error"
            self._log(
                f"Primary output write produced zero features ({detail}); trying emergency temp layer.",
                Qgis.MessageLevel.Warning,
                to_bar=True
            )

            # Remove empty primary layer if it was inserted.
            try:
                # Never remove a layer we were only appending to.
                if (memory_added and not reused
                        and output_layer.id() in QgsProject.instance().mapLayers()):
                    QgsProject.instance().removeMapLayer(output_layer.id())
                    memory_added = False
            except Exception:
                pass

            result_layer, fallback_note = self._create_temporary_output_layer(
                layer, parts_by_feature, da, project_unit, unit_abbrev, crs_area_unit
            )
            if result_layer is None:
                QMessageBox.warning(
                    None,
                    "Equalyzer",
                    "No valid polygon features could be written to the output layer.\n"
                    f"Provider error: {detail}"
                )
                return
            added_count = result_layer.featureCount()
            memory_added = result_layer is not None and result_layer.isValid()
            self._log(fallback_note, Qgis.MessageLevel.Warning, to_bar=True)

        if added_count > 0 and not memory_added:
            self._log(
                "Memory layer add after write failed; trying fallback loader.",
                Qgis.MessageLevel.Warning,
                to_bar=True
            )
            result_layer, fallback_note = self._add_output_layer_with_fallback(output_layer)
            if result_layer is None:
                result_layer, temp_note = self._create_temporary_output_layer(
                    layer, parts_by_feature, da, project_unit, unit_abbrev, crs_area_unit
                )
                if result_layer is None:
                    QMessageBox.critical(
                        None,
                        "Equalyzer Error",
                        "Failed to add output layer to the project.\n"
                        f"Details: {fallback_note}"
                    )
                    return
                fallback_note = temp_note
                self._log(fallback_note, Qgis.MessageLevel.Warning, to_bar=True)

        if result_layer is None:
            QMessageBox.warning(
                None, "Equalyzer",
                "The parts were computed but no output layer is available to show them."
            )
            return

        # Add area labels
        label_settings = QgsPalLayerSettings()
        label_settings.enabled = True
        label_settings.isExpression = False
        label_settings.fieldName = "area_txt"
        text_format = QgsTextFormat()
        text_format.setSize(10)
        text_format.setColor(QColor(Qt.GlobalColor.darkRed))
        label_settings.setFormat(text_format)
        result_layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        result_layer.setLabelsEnabled(True)
        result_layer.triggerRepaint()

        # Count total parts
        total_parts = added_count
        result_msg = (
            f"Split {len(polygon_features)} polygon(s) into {total_parts} part(s).\n"
        )
        if reused:
            result_msg += (
                f"Added to '{output_layer.name()}', which now holds "
                f"{output_layer.featureCount()} part(s).\n"
            )

        if params.get("delete_source") and added_count > 0:
            ok, state = self._delete_source_features(layer, polygon_features)
            if ok:
                try:
                    layer.removeSelection()
                    layer.triggerRepaint()
                except Exception:
                    pass
                result_msg += (
                    f"Removed {len(polygon_features)} original polygon(s) from "
                    f"'{layer.name()}'."
                    + ("" if state != "pending" else
                       " That layer was already in edit mode, so the removal sits in "
                       "its edit buffer.")
                    + "\n"
                )
            else:
                result_msg += f"The originals were kept: {state}\n"
        if mode == "area":
            result_msg += f"Target area per part: {params['target_value']:.4f} {unit_abbrev}"
        elif mode == "bays":
            settings = params.get("bay_settings", {})
            result_msg += (
                f"Bay size: {settings.get('bay_length', DEFAULT_BAY_LENGTH):.2f} x "
                f"{settings.get('bay_width', DEFAULT_BAY_WIDTH):.2f} m, "
                "direction taken from each strip."
            )
        else:
            result_msg += f"Requested parts per polygon: {int(params['target_value'])}"

        if method_fallback_messages:
            result_msg += "\n\nFallback used:\n" + "\n".join(method_fallback_messages)

        if fallback_note:
            result_msg += f"\n\n{fallback_note}"

        self._log("Apply completed. Output layer created.", Qgis.MessageLevel.Info, to_bar=True)
        self._restore_active_layer(layer)
        QMessageBox.information(None, "Equalyzer – Done", result_msg)
