# SPDX-License-Identifier: LGPL-2.1-or-later
"""Real FreeCAD stock geometry, simulation and export regressions."""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import FreeCAD as App
from freecad.nestingworkbench.freecad_helpers import create_sheet_shape
from freecad.nestingworkbench.datatypes.sheet import Sheet
from freecad.nestingworkbench.Tools.Exporter.exporter import SheetExporter
from freecad.nestingworkbench.Tools.Stacker.stacker import SheetStacker


class SheetThicknessTests(unittest.TestCase):
    def setUp(self):
        self.previous = App.ActiveDocument
        self.doc = App.newDocument('NestingSheetThicknessTest')
        self.layout = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Test')
        self.params = {'sheet_thickness': 12, 'layout_offset': (12, -20, 5)}

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def draw_sheet(self, index=0):
        Sheet(index, 100, 80, 10).draw(self.doc, self.params, self.layout)
        group = next(c for c in self.layout.Group if c.Label == f'Sheet_{index + 1}')
        return next(c for c in group.Group if c.Label.startswith('Sheet_Boundary_'))

    def assert_stock(self, obj, thickness=12, origin=(12, -20, 5)):
        shape = obj.Shape
        self.assertTrue(shape.isValid())
        self.assertEqual(len(shape.Solids), 1)
        self.assertAlmostEqual(shape.Volume, 100 * 80 * thickness)
        bounds = shape.BoundBox
        self.assertAlmostEqual(bounds.XMin, origin[0])
        self.assertAlmostEqual(bounds.YMin, origin[1])
        self.assertAlmostEqual(bounds.ZMax, origin[2] + thickness / 2)
        self.assertAlmostEqual(bounds.ZMin, origin[2] - thickness / 2)
        self.assertAlmostEqual(bounds.Center.z, origin[2])

    def test_finished_sheet_has_equal_thickness_above_and_below_the_nesting_plane(self):
        self.assert_stock(self.draw_sheet())

    def test_stack_unstack_keeps_solid_geometry_and_thickness(self):
        self.draw_sheet()
        second = self.draw_sheet(1)
        self.layout.addProperty('App::PropertyLength', 'SheetWidth')
        self.layout.SheetWidth = 100
        self.layout.addProperty('App::PropertyLength', 'PartSpacing')
        self.layout.PartSpacing = 10
        stacker = SheetStacker(self.layout)
        stacker.toggle_stack()
        self.assert_stock(second)
        stacker.toggle_stack()
        self.assert_stock(second, origin=(122, -20, 5))

    def test_simulation_uses_and_updates_selected_thickness(self):
        from freecad.nestingworkbench.Tools.Nesting import nesting_logic
        nester = SimpleNamespace()
        sheet = Sheet(0, 100, 80)
        with patch.object(Sheet, '_draw_single_part'), patch.object(
                nesting_logic, 'FreeCADGui', SimpleNamespace(updateGui=lambda: None)):
            nesting_logic._bind_sim_update(nester, (12, -20, 5), sheet_thickness=12)
            nester.update_callback(object(), sheet)
            stock = self.doc.getObject('sim_sheet_boundary_0')
            self.assert_stock(stock)
            nesting_logic._bind_sim_update(nester, (12, -20, 5), sheet_thickness=18)
            nester.update_callback(object(), sheet)
            self.assert_stock(stock, thickness=18)

    def test_simulation_recenters_existing_one_sided_stock(self):
        import Part
        sheet = Sheet(0, 100, 80)
        with patch.object(Sheet, '_draw_single_part'):
            sheet.draw(self.doc, self.params, transient_part=object())
            stock = self.doc.getObject('sim_sheet_boundary_0')
            stock.Shape = Part.makeBox(100, 80, 12, App.Vector(0, 0, -12))
            stock.Placement = App.Placement(App.Vector(12, -20, 5), App.Rotation())
            sheet.draw(self.doc, self.params, transient_part=object())
            self.assert_stock(stock)

    def test_dxf_exports_a_single_flat_sheet_outline(self):
        import importDXF
        self.draw_sheet()
        with patch.object(importDXF, 'export') as export:
            SheetExporter(self.layout).export_sheets('', delete_generated_objects=False)
        self.assertEqual(export.call_count, 1)
        outline = export.call_args.args[0][0]
        self.assertEqual(len(outline.Shape.Edges), 4)
        self.assertEqual(len(outline.Shape.Solids), 0)
        self.assertAlmostEqual(outline.Shape.Area, 100 * 80)
        self.assertAlmostEqual(outline.Shape.BoundBox.ZLength, 0)
        self.assertAlmostEqual(outline.Shape.BoundBox.XMin, 12)
        self.assertAlmostEqual(outline.Shape.BoundBox.YMin, -20)
        self.assertAlmostEqual(outline.Shape.BoundBox.ZMin, 5)

    @unittest.skipUnless(App.GuiUp, 'Manual sheet creation requires GUI view providers.')
    def test_manually_added_sheet_uses_saved_thickness(self):
        from freecad.nestingworkbench.Tools.ManualNester.manual_nester_tool import ManualNesterToolObserver
        self.draw_sheet()
        self.layout.addProperty('App::PropertyLength', 'SheetThickness')
        self.layout.SheetThickness = 12
        for axis, value in zip('XYZ', (12, -20, 5)):
            self.layout.addProperty('App::PropertyDistance', f'Offset{axis}')
            setattr(self.layout, f'Offset{axis}', value)
        observer = SimpleNamespace(layout_group=self.layout, new_objects=[],
                                   _get_sheet_dimensions=lambda: (100, 80))
        group = ManualNesterToolObserver._add_new_sheet(observer)
        stock = next(c for c in group.Group if c.Label.startswith('Sheet_Boundary_'))
        self.doc.recompute()
        self.assert_stock(stock, origin=(122, -20, 5))

    def test_thickness_survives_document_save_and_reopen(self):
        stock_name = self.draw_sheet().Name
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            self.assertTrue(os.path.realpath(directory).startswith(
                os.path.realpath(os.path.dirname(__file__)) + os.sep))
            path = os.path.join(directory, 'Sheet.FCStd')
            self.doc.recompute()
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            self.assert_stock(self.doc.getObject(stock_name))

    def test_invalid_thickness_is_rejected(self):
        for thickness in (0, -1, float('nan'), float('inf')):
            with self.subTest(thickness=thickness), self.assertRaises(ValueError):
                create_sheet_shape(100, 80, thickness)


if __name__ == '__main__':
    unittest.main()
