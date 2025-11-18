from qgis.PyQt.QtCore import Qt, QEventLoop
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QInputDialog
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsGeometry, QgsWkbTypes,
    QgsFeature, QgsUnitTypes, QgsDistanceArea,
    QgsVectorLayerSimpleLabeling, QgsPalLayerSettings,
    QgsTextFormat, QgsRectangle, QgsPointXY, QgsSnappingConfig,
    QgsTolerance, QgsMapLayer, QgsCoordinateTransform
)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand, QgsSnapIndicator
from qgis.utils import iface
import math
import os

class LineDrawTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        self.points = []
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self.rubber_band.setColor(Qt.red)
        self.rubber_band.setWidth(2)
        
        self.snapping_utils = canvas.snappingUtils()
        self.snapping_config = QgsSnappingConfig()
        self.snapping_config.setMode(QgsSnappingConfig.AdvancedConfiguration)
        self.snapping_config.setEnabled(True)
        
        root = QgsProject.instance().layerTreeRoot()
        visible_layers = []
        for tree_layer in root.findLayers():
            if tree_layer.isVisible():
                layer = tree_layer.layer()
                if layer is not None and layer.type() == QgsMapLayer.VectorLayer:
                    visible_layers.append(layer)
        
        for layer in visible_layers:
            settings = QgsSnappingConfig.IndividualLayerSettings(
                True, QgsSnappingConfig.Vertex, 10, QgsTolerance.Pixels
            )
            self.snapping_config.setIndividualLayerSettings(layer, settings)
        
        self.snapping_utils.setConfig(self.snapping_config)
        self.snap_indicator = QgsSnapIndicator(canvas)
        iface.messageBar().pushInfo("Action Required", "Click two points to draw the CUT DIRECTION (cuts will be parallel to this line)")

    def canvasMoveEvent(self, event):
        map_pos = self.toMapCoordinates(event.pos())
        match = self.snapping_utils.snapToMap(map_pos)
        self.snap_indicator.setMatch(match)
        if len(self.points) == 1:
            self.rubber_band.reset(QgsWkbTypes.LineGeometry)
            self.rubber_band.addPoint(self.points[0])
            if match.isValid():
                self.rubber_band.addPoint(match.point())
            else:
                self.rubber_band.addPoint(map_pos)

    def canvasReleaseEvent(self, event):
        map_pos = self.toMapCoordinates(event.pos())
        match = self.snapping_utils.snapToMap(map_pos)
        point = match.point() if match.isValid() else map_pos
        self.points.append(point)
        if len(self.points) == 1:
            self.rubber_band.addPoint(point)
            iface.messageBar().pushInfo("Action Required", "Click second point for direction line")
        elif len(self.points) == 2:
            self.rubber_band.reset()
            self.canvas.unsetMapTool(self)
            self.callback(self.points)

class PointSelectTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.callback = callback
        iface.messageBar().pushInfo("Action Required", "Click on the polygon to choose the STARTING side")

    def canvasReleaseEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        self.canvas.unsetMapTool(self)
        self.callback(point)

class PolygonSplitter:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.actions = []
        self.menu = "&Equalyzer"
        self.toolbar = self.iface.addToolBar("Equalyzer")
        self.current_points = None
        self.clicked_point = None

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

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar

    def start_split(self, mode):
        try:
            self.mode = mode
            self.split_polygon()
        except Exception as e:
            QMessageBox.critical(None, "Error", str(e))

    def decompose_multiparts(self, geoms):
        decomposed = []
        for geom in geoms:
            if geom.isMultipart():
                for part in geom.asMultiPolygon():
                    decomposed.append(QgsGeometry.fromPolygonXY(part))
            elif geom.wkbType() == QgsWkbTypes.GeometryCollection:
                for subgeom in geom.constGet():
                    g = QgsGeometry(subgeom)
                    if g.type() == QgsWkbTypes.PolygonGeometry:
                        decomposed.append(g)
            else:
                decomposed.append(geom)
        return decomposed

    def clean_geometry(self, geom):
        if not geom:
            return QgsGeometry()
        
        # Basic validity check and fix
        if not geom.isGeosValid():
            geom = geom.makeValid()
            
        if geom.wkbType() == QgsWkbTypes.GeometryCollection:
            # Try buffering to merge touching parts or clean artifacts
            cleaned = geom.buffer(0, 0)
            if cleaned and cleaned.wkbType() != QgsWkbTypes.GeometryCollection and not cleaned.isEmpty():
                return cleaned
            
            # Manual extraction if buffer fails
            parts = []
            for subgeom in geom.constGet():
                g = QgsGeometry(subgeom)
                if g.type() == QgsWkbTypes.PolygonGeometry:
                    parts.append(g)
            
            if parts:
                union_geom = parts[0]
                for part in parts[1:]:
                    union_geom = union_geom.combine(part)
                return union_geom
                
        return geom

    def get_line_points(self):
        loop = QEventLoop()
        def callback(points):
            self.current_points = points
            loop.quit()
        tool = LineDrawTool(self.canvas, callback)
        self.canvas.setMapTool(tool)
        tool.deactivated.connect(loop.quit)
        loop.exec_()

    def get_clicked_point(self):
        loop = QEventLoop()
        def callback(point):
            self.clicked_point = point
            loop.quit()
        tool = PointSelectTool(self.canvas, callback)
        self.canvas.setMapTool(tool)
        tool.deactivated.connect(loop.quit)
        loop.exec_()

    def split_polygon(self):
        # 1. VALIDATION AND SETUP
        layer = self.iface.activeLayer()
        if not layer or layer.type() != QgsMapLayer.VectorLayer:
            raise Exception("Please select a vector layer.")

        if layer.selectedFeatureCount() != 1:
            if layer.selectedFeatureCount() == 0 and layer.featureCount() == 1:
                 # Convenience: Auto-select if it's the only feature
                feature = next(layer.getFeatures())
                layer.select(feature.id())
            else:
                raise Exception("Please select exactly one polygon.")

        selected_feature = layer.selectedFeatures()[0]
        original_geom = selected_feature.geometry().makeValid()
        if original_geom.isEmpty():
            raise Exception("Invalid geometry selected.")

        # Setup Area Calculation
        da = QgsDistanceArea()
        da.setEllipsoid(QgsProject.instance().ellipsoid())
        da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())

        # Calculate Areas
        original_area_measured = da.measureArea(original_geom)
        project_area_unit = QgsProject.instance().areaUnits()
        
        # Helper for unit conversion
        def get_conversion_factor():
            if da.willUseEllipsoid():
                return QgsUnitTypes.fromUnitToUnitFactor(QgsUnitTypes.AreaSquareMeters, project_area_unit)
            else:
                crs_distance_unit = layer.crs().mapUnits()
                if crs_distance_unit == QgsUnitTypes.DistanceMeters:
                    src = QgsUnitTypes.AreaSquareMeters
                elif crs_distance_unit == QgsUnitTypes.DistanceFeet:
                    src = QgsUnitTypes.AreaSquareFeet
                elif crs_distance_unit == QgsUnitTypes.DistanceDegrees:
                    # Fallback for lat/lon without ellipsoid (rare/inaccurate but needed to prevent crash)
                    src = QgsUnitTypes.AreaSquareMeters 
                else:
                    src = QgsUnitTypes.AreaSquareMeters
                return QgsUnitTypes.fromUnitToUnitFactor(src, project_area_unit)

        factor = get_conversion_factor()
        original_area_display = factor * original_area_measured
        unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_area_unit)

        # 2. USER INPUT (TARGETS)
        if self.mode == "area":
            prompt = (
                f"Total area: {original_area_display:.2f} {unit_abbrev}\n"
                f"Enter target area per part ({unit_abbrev}):"
            )
            expected_area_input, ok = QInputDialog.getDouble(
                None, "Equal Area", prompt, value=original_area_display/2, min=0.001, max=original_area_display, decimals=2
            )
            if not ok or expected_area_input <= 0:
                return
            
            # Convert back to layer units/meters for processing
            expected_area_map_units = expected_area_input / factor
            estimated_parts = math.ceil(original_area_measured / expected_area_map_units)
            
            if estimated_parts > 500:
                if QMessageBox.question(None, "High Count", f"This will create ~{estimated_parts} parts. Continue?") != QMessageBox.Yes:
                    return

        else: # Equal Parts
            max_parts = max(2, min(2000, int(original_area_measured / 0.0001)))
            num_parts, ok = QInputDialog.getInt(
                None, "Equal Parts", f"Total area: {original_area_display:.2f}\nEnter number of parts:",
                value=2, min=2, max=max_parts
            )
            if not ok: return
            expected_area_map_units = original_area_measured / num_parts

        # 3. DIRECTION AND STARTING SIDE
        self.get_line_points()
        if not self.current_points or len(self.current_points) != 2:
            return # User cancelled

        point_a, point_b = self.current_points
        
        # Transform line to Layer CRS
        xform = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(), layer.crs(), QgsProject.instance())
        point_a = xform.transform(point_a)
        point_b = xform.transform(point_b)

        # Get Clicked Point (Start Side)
        self.get_clicked_point()
        if not self.clicked_point:
            return
        
        clicked_point_map = xform.transform(self.clicked_point)
        if not original_geom.intersects(QgsGeometry.fromPointXY(clicked_point_map)):
            # Fallback: Check distance if not strictly intersecting (snapping tolerance)
            if original_geom.distance(QgsGeometry.fromPointXY(clicked_point_map)) > 0:
                # Use a lenient buffer for the check
                if not original_geom.buffer(da.convertLengthMeasurement(1, QgsUnitTypes.DistanceMeters), 5).intersects(QgsGeometry.fromPointXY(clicked_point_map)):
                    raise Exception("Selected point is not on the polygon.")

        # 4. CALCULATE ROTATION
        # Calculate angle of the drawn line
        dx = point_b.x() - point_a.x()
        dy = point_b.y() - point_a.y()
        angle_rad = math.atan2(dy, dx)
        angle_deg_ccw = math.degrees(angle_rad)
        
        # QGIS rotation is Clockwise.
        # To align a line at angle alpha (CCW) to the X-axis, we rotate by alpha (CW).
        rotation_to_flat = angle_deg_ccw
        center = original_geom.boundingBox().center()

        # Check Starting Side
        # We rotate everything so the cut lines become horizontal.
        # The splitter sweeps from Bottom (Y-min) to Top (Y-max).
        # We want the CLICKED side to be at the Bottom.
        
        # Test rotation
        test_click = QgsGeometry.fromPointXY(clicked_point_map)
        test_click.rotate(rotation_to_flat, center)
        
        test_poly = QgsGeometry(original_geom)
        test_poly.rotate(rotation_to_flat, center)
        poly_center_y = test_poly.boundingBox().center().y()
        click_y = test_click.asPoint().y()

        # If clicked point is above the center, flip everything 180 degrees
        # so the clicked point becomes the "bottom"
        if click_y > poly_center_y:
            final_rotation = rotation_to_flat + 180
        else:
            final_rotation = rotation_to_flat

        # 5. SPLITTING ALGORITHM
        def split_recursive(geom, target_area_unit, rotation, center_pt):
            parts = []
            remaining = geom
            
            # Safety break for infinite loops
            max_iter = 2000 
            count = 0
            
            current_total = da.measureArea(remaining)
            
            while current_total > target_area_unit * 1.01 and count < max_iter:
                count += 1
                
                # Rotate to processing space (Horizontal Cuts)
                work_geom = QgsGeometry(remaining)
                work_geom.rotate(rotation, center_pt)
                bbox = work_geom.boundingBox()
                
                min_y = bbox.yMinimum()
                max_y = bbox.yMaximum()
                
                # Binary search for the cut line (Y-coordinate)
                # We look for a Y that gives us exactly 'target_area_unit' in the bottom part
                low = min_y
                high = max_y
                best_cut_y = max_y
                
                for _ in range(25): # 25 iterations is enough precision
                    mid = (low + high) / 2
                    # Create a clipping rectangle from Bottom to Mid
                    rect = QgsRectangle(bbox.xMinimum(), min_y, bbox.xMaximum(), mid)
                    clipper = QgsGeometry.fromRect(rect)
                    
                    # Calculate area of intersection
                    # Note: We intersect in rotated space, calculating area is valid 
                    # provided we treat units consistently. Area is invariant under rotation.
                    # But simpler to rotate back for area measure if using ellipsoid?
                    # Actually, simple area is invariant. Geodesic might vary slightly if large extent.
                    # To be safe with 'da', we un-rotate the candidate.
                    
                    candidate_part = work_geom.intersection(clipper)
                    
                    # Un-rotate to measure area correctly on the earth
                    candidate_measure = QgsGeometry(candidate_part)
                    candidate_measure.rotate(-rotation, center_pt)
                    
                    measured = da.measureArea(candidate_measure)
                    
                    if measured < target_area_unit:
                        low = mid
                    else:
                        high = mid
                        best_cut_y = mid
                
                # Perform the actual cut at best_cut_y
                final_rect = QgsRectangle(bbox.xMinimum(), min_y, bbox.xMaximum(), best_cut_y)
                final_clipper = QgsGeometry.fromRect(final_rect)
                final_clipper.rotate(-rotation, center_pt) # Rotate clipper back to real world
                
                cut_part = remaining.intersection(final_clipper)
                cut_part = self.clean_geometry(cut_part)
                
                if cut_part.isEmpty() or da.measureArea(cut_part) < (target_area_unit * 0.01):
                    # Failed to cut a significant chunk (topology issue?)
                    break
                
                parts.append(cut_part)
                remaining = remaining.difference(final_clipper)
                remaining = self.clean_geometry(remaining)
                current_total = da.measureArea(remaining)

            # Add the last piece
            if not remaining.isEmpty() and da.measureArea(remaining) > 0.0001:
                # If the last piece is tiny (floating point noise), merge to previous
                if parts and da.measureArea(remaining) < (target_area_unit * 0.05):
                    parts[-1] = parts[-1].combine(remaining)
                else:
                    parts.append(remaining)
            
            return parts

        # Run the splitter
        try:
            split_parts = split_recursive(original_geom, expected_area_map_units, final_rotation, center)
            split_parts = self.decompose_multiparts(split_parts)
        except Exception as e:
            raise Exception(f"Splitting process failed: {str(e)}")

        # 6. OUTPUT GENERATION
        output_layer = QgsVectorLayer(f"Polygon?crs={layer.crs().authid()}", "Split Results", "memory")
        prov = output_layer.dataProvider()
        prov.addAttributes(layer.fields())
        output_layer.updateFields()
        
        orig_attrs = selected_feature.attributes()
        feats = []
        
        for part in split_parts:
            ft = QgsFeature(output_layer.fields())
            ft.setGeometry(part)
            ft.setAttributes(orig_attrs)
            feats.append(ft)
            
        prov.addFeatures(feats)
        output_layer.updateExtents()
        QgsProject.instance().addMapLayer(output_layer)
        
        # Add Labels
        lbl = QgsPalLayerSettings()
        lbl.fieldName = f"concat(round($area * {factor}, 2), ' {unit_abbrev}')"
        lbl.isExpression = True
        lbl.enabled = True
        txt = QgsTextFormat()
        txt.setSize(10)
        txt.setColor(Qt.black)
        lbl.setFormat(txt)
        output_layer.setLabeling(QgsVectorLayerSimpleLabeling(lbl))
        output_layer.setLabelsEnabled(True)
        
        iface.messageBar().pushSuccess("Success", f"Created {len(split_parts)} parts.")