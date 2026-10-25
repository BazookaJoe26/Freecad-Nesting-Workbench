# SPDX-License-Identifier: LGPL-2.1-or-later
"""Real FreeCAD geometry regressions; run from the FreeCAD Python console:

import runpy
runpy.run_path(r"C:/repos/Freecad-Nesting-Workbench/tests/test_geometry_bounds.py", run_name="__main__")

Requires FreeCAD with the workbench's dependencies (including Shapely).
All document objects are created in temporary documents and removed afterwards.
"""

from pathlib import Path
import sys
import unittest

import FreeCAD as App
import Part
import freecad

# Test this checkout even when an older addon is installed as nestingworkbench.
CHECKOUT = Path(__file__).resolve().parents[1]
freecad.__path__ = [str(CHECKOUT / "freecad")] + list(freecad.__path__)

from freecad.nestingworkbench.freecad_helpers import get_geometry_bounding_box
from freecad.nestingworkbench.datatypes.shape import Shape
from freecad.nestingworkbench.Tools.Nesting.algorithms import shape_processor
from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
from freecad.nestingworkbench.Tools.Cam.cam_manager import CAMManager


def trimmed_plate():
    """A valid 12 mm plate whose underlying NURBS surface extends beyond it."""
    sphere = Part.makeSphere(100, App.Vector(0, 0, 120)).toNurbs()
    return sphere.common(Part.makeBox(180, 180, 12, App.Vector(-90, -90, 120)))


class GeometryBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plate = trimmed_plate()

    def setUp(self):
        self.previous_doc = App.ActiveDocument
        self.doc = App.newDocument("NestingBoundsRegression")

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous_doc:
            App.setActiveDocument(self.previous_doc.Name)

    def feature(self, name="Plate", shape=None):
        obj = self.doc.addObject("Part::Feature", name)
        obj.Shape = self.plate.copy() if shape is None else shape
        return obj

    def assert_z(self, shape, low, high):
        box = get_geometry_bounding_box(shape)
        self.assertAlmostEqual(box.ZMin, low, places=5)
        self.assertAlmostEqual(box.ZMax, high, places=5)

    def prepare(self, source, up="Z+", cache=None):
        layout = self.doc.addObject("App::DocumentObjectGroup", "Layout_Test")
        parts = self.doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
        settings = dict(spacing=2.0, deflection=0.3, simplification=0.1,
                        rotation_steps=4, add_labels=False, font_path="")
        label = source.Label.removeprefix("master_shape_")
        quantities = {label: dict(quantity=1, up_direction=up)}
        preparer = ShapePreparer(self.doc, {} if cache is None else cache)
        instances = preparer.prepare_parts(
            settings, quantities, {source.Label: source}, layout, parts
        )
        self.assertEqual(len(instances), 1, "Part preparation failed")
        return instances[0]

    def test_trimmed_surface_bounds_ignore_copy_and_display_mesh(self):
        self.assertTrue(self.plate.isValid())
        copied = self.plate.copy()
        # The fixture must actually expose the oversized-box regression.
        self.assertGreater(copied.BoundBox.ZLength, 12.1)
        for meshed in (False, True):
            with self.subTest(meshed=meshed):
                shape = self.plate.copy()
                if meshed:
                    shape.tessellate(0.3)
                self.assert_z(shape, 120, 132)
                box = get_geometry_bounding_box(shape)
                self.assertAlmostEqual(box.XMin, -90, places=5)
                self.assertAlmostEqual(box.XMax, 90, places=5)

    def test_ordinary_plate_and_compound(self):
        plate = Part.makeBox(80, 60, 12, App.Vector(20, -30, 120))
        for shape in (plate, Part.Compound([plate])):
            self.assert_z(shape, 120, 132)

    def test_prepare_centers_trimmed_plate_for_all_up_directions(self):
        for up in ("Z+", "Z-", "X+", "X-", "Y+", "Y-"):
            with self.subTest(up=up):
                rotation = {
                    "X": App.Rotation(App.Vector(0, 1, 0), 90),
                    "Y": App.Rotation(App.Vector(1, 0, 0), 90),
                    "Z": App.Rotation(),
                }[up[0]]
                shape = self.plate.transformGeometry(rotation.toMatrix())
                instance = self.prepare(self.feature("Plate", shape), up)
                self.assert_z(instance.fc_object.Shape, -6, 6)
                self.assert_z(instance.fc_object.BoundaryObject.Shape, 0, 0)
                mesh, _ = instance.fc_object.Shape.tessellate(0.3)
                self.assertAlmostEqual(min(v.z for v in mesh), -6, places=5)
                self.assertAlmostEqual(max(v.z for v in mesh), 6, places=5)

    def test_reloading_corrected_master_preserves_height(self):
        first = self.prepare(self.feature())
        master = next(o for o in first.master_container.Group
                      if o.Label.startswith("master_shape_"))
        reloaded = self.prepare(master)
        self.assert_z(reloaded.fc_object.Shape, -6, 6)

    def test_master_fallback_uses_trimmed_bounds(self):
        source = self.feature()
        wrapper = Shape(source)
        shape_processor.create_single_nesting_part(
            wrapper, source, 2.0, deflection=0.3, simplification=0.1
        )
        # A cached wrapper without source_centroid exercises the fallback.
        wrapper.source_centroid = None
        preparer = ShapePreparer(self.doc, {})
        masters = self.doc.addObject("App::DocumentObjectGroup", "MasterShapes")
        master, _ = preparer._handle_new_master(
            source, source.Label, {source.Label: dict(quantity=1)}, wrapper,
            2.0, 0.3, 0.1, ("fallback",), masters, False
        )
        self.assert_z(master.Shape, -6, 6)

    def test_cam_geometry_aligns_with_stock(self):
        # Run the actual CAM geometry path, stopping before GUI job creation.
        from unittest.mock import patch
        from Path.Main.Gui import Job as PathJobGui

        layout = self.doc.addObject("App::DocumentObjectGroup", "Layout_CAM")
        for prop, value in (("SheetWidth", 300.0), ("SheetHeight", 400.0),
                            ("SheetThickness", 12.0)):
            layout.addProperty("App::PropertyFloat", prop)
            setattr(layout, prop, value)
        sheet = self.doc.addObject("App::DocumentObjectGroup", "Sheet_1")
        layout.addObject(sheet)
        shapes = self.doc.addObject("App::DocumentObjectGroup", "Shapes_1")
        sheet.addObject(shapes)
        container = self.doc.addObject("App::Part", "nested_Plate_1")
        shapes.addObject(container)
        container.addObject(self.feature("part_Plate_1"))
        # Captures the real generated compound; no tools or jobs are created.
        with patch.object(PathJobGui, "Create", return_value=None) as create:
            CAMManager(layout)._create_job_for_sheet(
                sheet, include_labels=False, include_outlines=False
            )
        self.assertEqual(create.call_count, 1)
        compound = self.doc.getObject("CAM_Parts_Sheet_1")
        self.assertIsNotNone(compound)
        self.assert_z(compound.Shape, -12, 0)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GeometryBoundsTests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise AssertionError("Geometry bounds regressions failed")
