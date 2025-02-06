from qgis.PyQt.QtCore import Qt, QEventLoop, QVariant
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QInputDialog, QPushButton
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsGeometry, QgsWkbTypes,
    QgsField, QgsFeature, QgsUnitTypes, QgsDistanceArea,
    QgsVectorLayerSimpleLabeling, QgsPalLayerSettings,
    QgsTextFormat, QgsRectangle, QgsPointXY, QgsSnappingConfig,
    QgsTolerance, QgsMapLayer
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
                if layer.type() == QgsMapLayer.VectorLayer:
                    visible_layers.append(layer)
        
        for layer in visible_layers:
            settings = QgsSnappingConfig.IndividualLayerSettings(
                True, QgsSnappingConfig.Vertex, 10, QgsTolerance.Pixels
            )
            self.snapping_config.setIndividualLayerSettings(layer, settings)
        
        self.snapping_utils.setConfig(self.snapping_config)
        self.snap_indicator = QgsSnapIndicator(canvas)
        iface.messageBar().pushInfo("Action Required", "Click two points to draw the direction line (snaps to visible layers' points)")

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
        iface.messageBar().pushInfo("Action Required", "Click on the polygon to choose starting side")

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
            else:
                decomposed.append(geom)
        return decomposed

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
        layer = self.iface.activeLayer()
        if not layer or layer.type() != QgsMapLayer.VectorLayer:
            raise Exception("Please select a vector layer.")

        if layer.selectedFeatureCount() == 0:
            if layer.featureCount() == 1:
                feature = next(layer.getFeatures())
                if feature.geometry().type() == QgsWkbTypes.PolygonGeometry:
                    layer.select(feature.id())
                    self.iface.messageBar().pushInfo("Notice", "Auto-selected the only polygon in layer")
                else:
                    raise Exception("Layer contains a single feature, but it's not a polygon.")
            else:
                raise Exception("Please select exactly one polygon.")

        if layer.selectedFeatureCount() != 1:
            raise Exception("Please select exactly one polygon.")

        selected_feature = layer.selectedFeatures()[0]
        original_geom = selected_feature.geometry().makeValid()
        if original_geom.isEmpty() or not original_geom.isGeosValid():
            raise Exception("Invalid geometry selected.")

        # Initialize distance area for accurate area measurement
        da = QgsDistanceArea()
        da.setEllipsoid(QgsProject.instance().ellipsoid())
        da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())

        # Calculate original area in the appropriate units (square meters or CRS units)
        original_area_measured = da.measureArea(original_geom)

        # Convert original area to project units for display
        project_area_unit = QgsProject.instance().areaUnits()
        if da.willUseEllipsoid():
            original_area = QgsUnitTypes.fromUnitToUnitFactor(QgsUnitTypes.AreaSquareMeters, project_area_unit) * original_area_measured
        else:
            crs_distance_unit = layer.crs().mapUnits()
            if crs_distance_unit == QgsUnitTypes.DistanceMeters:
                crs_area_unit = QgsUnitTypes.AreaSquareMeters
            elif crs_distance_unit == QgsUnitTypes.DistanceFeet:
                crs_area_unit = QgsUnitTypes.AreaSquareFeet
            else:
                crs_area_unit = QgsUnitTypes.AreaSquareMeters
            original_area = QgsUnitTypes.fromUnitToUnitFactor(crs_area_unit, project_area_unit) * original_area_measured

        if self.mode == "area":
            # Get project unit abbreviation for display
            project_unit = QgsProject.instance().areaUnits()
            unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_unit)  # e.g., "m²", "ha"
            prompt = (
                f"Total area: {original_area:.2f} {unit_abbrev}\n"
                f"Enter target area per part ({unit_abbrev}):"
            )

            expected_area_input, ok = QInputDialog.getDouble(
                None, 
                "Equal Area", 
                prompt,  # Now includes total area
                value=1000.0, 
                min=0.1, 
                max=original_area, 
                decimals=1
            )

            if not ok or expected_area_input <= 0:
                return

            # Calculate estimated parts before splitting
            estimated_parts = math.ceil(original_area / expected_area_input)
            
            # Show confirmation if estimate exceeds 1000
            if estimated_parts > 1000:
                msg = QMessageBox(
                    QMessageBox.Warning,
                    "High Partition Count",
                    f"Estimated {estimated_parts} parts. This may cause performance issues.\nProceed?",
                    QMessageBox.Yes | QMessageBox.No
                )
                if msg.exec_() == QMessageBox.No:
                    self.iface.messageBar().pushInfo("Cancelled", "Operation aborted by user")
                    return

            # Convert user input from project units to measurement units (square meters or CRS units)
            if da.willUseEllipsoid():
                expected_area = QgsUnitTypes.fromUnitToUnitFactor(project_area_unit, QgsUnitTypes.AreaSquareMeters) * expected_area_input
            else:
                expected_area = QgsUnitTypes.fromUnitToUnitFactor(project_area_unit, crs_area_unit) * expected_area_input
            num_parts = None
        else:
            # Calculate max parts based on project units
            max_parts = max(2, min(1000, int(original_area / 0.1)))  # Ensures min 2 parts
            project_unit = QgsProject.instance().areaUnits()
            unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_unit)
            prompt = (
                f"Total area: {original_area:.2f} {unit_abbrev}\n"
                "Enter number of parts:"
            )

            num_parts, ok = QInputDialog.getInt(
                None, 
                "Equal Parts", 
                prompt,  # Now includes total area
                value=2, 
                min=2, 
                max=max_parts  # Uses project units for min/max
            )
            if not ok or num_parts < 1:
                return
            expected_area = original_area_measured / num_parts

        # Get direction line
        self.get_line_points()
        if not self.current_points or len(self.current_points) != 2:
            raise Exception("Direction line not properly drawn!")
        
        point_a, point_b = self.current_points

        # Handle area mode specific logic
        if self.mode == "area":
            self.get_clicked_point()
            if not self.clicked_point:
                raise Exception("No point selected!")
            if not original_geom.intersects(QgsGeometry.fromPointXY(self.clicked_point)):
                raise Exception("Clicked point is not on the polygon!")

            center = QgsPointXY((point_a.x() + point_b.x())/2, (point_a.y() + point_b.y())/2)
            dx = point_b.x() - point_a.x()
            dy = point_b.y() - point_a.y()
            angle_rad = math.atan2(dy, dx)
            angle_deg = math.degrees(angle_rad)
            clicked_geom = QgsGeometry.fromPointXY(self.clicked_point)
            clicked_geom.rotate(angle_deg, center)
            rotated_clicked = clicked_geom.asPoint()
            rotated_original = QgsGeometry(original_geom)
            rotated_original.rotate(angle_deg, center)
            bbox = rotated_original.boundingBox()
            mid_y = (bbox.yMinimum() + bbox.yMaximum()) / 2
            if rotated_clicked.y() > mid_y:
                point_a, point_b = point_b, point_a

        # Calculate rotation parameters
        center = QgsPointXY((point_a.x() + point_b.x())/2, (point_a.y() + point_b.y())/2)
        dx = point_b.x() - point_a.x()
        dy = point_b.y() - point_a.y()
        angle_rad = math.atan2(dy, dx)
        angle_deg = -math.degrees(angle_rad)

        # Splitting logic with correct area measurements
        def split_geometry(geom, angle_deg, center_point, target_area):
            parts = []
            remaining_geom = geom
            total_area = da.measureArea(remaining_geom)
            while total_area >= target_area * 0.99:
                rotated_geom = QgsGeometry(remaining_geom)
                rotated_geom.rotate(-angle_deg, center_point)
                bbox = rotated_geom.boundingBox()
                low = bbox.yMinimum()
                high = bbox.yMaximum()
                best_y = high
                for _ in range(20):
                    mid = (low + high) / 2
                    clip_rect = QgsRectangle(-1e6, -1e6, 1e6, mid)
                    clip_geom = QgsGeometry.fromRect(clip_rect)
                    temp_part = rotated_geom.intersection(clip_geom)
                    temp_area = da.measureArea(temp_part)
                    if temp_area < target_area:
                        low = mid
                    else:
                        high = mid
                        best_y = high
                final_clip = QgsGeometry.fromRect(QgsRectangle(-1e6, -1e6, 1e6, best_y))
                final_clip.rotate(angle_deg, center_point)
                lower_part = remaining_geom.intersection(final_clip)
                upper_part = remaining_geom.difference(final_clip)
                lower_area = da.measureArea(lower_part)
                if lower_part.isEmpty() or lower_area < target_area * 0.95:
                    break
                parts.append(lower_part)
                remaining_geom = upper_part
                total_area = da.measureArea(remaining_geom)
            if not remaining_geom.isEmpty() and da.measureArea(remaining_geom) > 0.01:
                parts.append(remaining_geom)
            return parts

        try:
            split_parts = split_geometry(original_geom, angle_deg, center, expected_area)
            split_parts = self.decompose_multiparts(split_parts)
        except Exception as e:
            raise Exception(f"Splitting failed: {str(e)}")

        # Create output layer with corrected area calculation
        crs = layer.crs().authid()
        output_layer = QgsVectorLayer(f"Polygon?crs={crs}", "Split Parts", "memory")
        provider = output_layer.dataProvider()
        provider.addAttributes(layer.fields())
        output_layer.updateFields()

        project_unit = QgsProject.instance().areaUnits()
        unit_abbrev = QgsUnitTypes.toAbbreviatedString(project_unit)

        original_attributes = selected_feature.attributes()

        for part in split_parts:
            feat = QgsFeature(output_layer.fields())
            feat.setGeometry(part)
            area = da.measureArea(part)
            if da.willUseEllipsoid():
                converted_area = QgsUnitTypes.fromUnitToUnitFactor(QgsUnitTypes.AreaSquareMeters, project_unit) * area
            else:
                crs_distance_unit = output_layer.crs().mapUnits()
                if crs_distance_unit == QgsUnitTypes.DistanceMeters:
                    crs_area_unit = QgsUnitTypes.AreaSquareMeters
                elif crs_distance_unit == QgsUnitTypes.DistanceFeet:
                    crs_area_unit = QgsUnitTypes.AreaSquareFeet
                else:
                    crs_area_unit = QgsUnitTypes.AreaSquareMeters
                converted_area = QgsUnitTypes.fromUnitToUnitFactor(crs_area_unit, project_unit) * area
            
            new_attributes = original_attributes.copy()
            feat.setAttributes(new_attributes)
            provider.addFeature(feat)

        output_layer.updateExtents()
        QgsProject.instance().addMapLayer(output_layer)

        # Configure labels with correct area units
        label_settings = QgsPalLayerSettings()
        label_settings.enabled = True
        label_settings.isExpression = True
        label_settings.fieldName = f"concat(round($area, 2), ' {unit_abbrev}')"
        text_format = QgsTextFormat()
        text_format.setSize(15)
        text_format.setColor(Qt.red)
        label_settings.setFormat(text_format)
        output_layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        output_layer.setLabelsEnabled(True)
        output_layer.triggerRepaint()

        result_msg = f"Created {len(split_parts)} polygons\n"
        if self.mode == "area":
            result_msg += f"Target area: {expected_area_input:.1f} {unit_abbrev}\n"
            if len(split_parts) > 1:
                remainder_area = QgsUnitTypes.fromUnitToUnitFactor(
                    QgsUnitTypes.AreaSquareMeters if da.willUseEllipsoid() else crs_area_unit,
                    project_unit
                ) * da.measureArea(split_parts[-1])
                result_msg += f"Remainder area: {remainder_area:.1f} {unit_abbrev}"
        else:
            result_msg += f"Requested parts: {num_parts}\n"
            avg_area = original_area / num_parts
            result_msg += f"Average area: {avg_area:.1f} {unit_abbrev}\n"
            if len(split_parts) != num_parts:
                result_msg += f"Note: Split into {len(split_parts)} parts due to geometry constraints"

        QMessageBox.information(None, "Success", result_msg)
