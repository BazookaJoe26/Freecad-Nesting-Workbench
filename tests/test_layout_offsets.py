# SPDX-License-Identifier: LGPL-2.1-or-later
"""Integration tests to run in FreeCAD's Python environment with unittest."""

import os
import tempfile
import unittest
from types import SimpleNamespace

import FreeCAD as App
import Part

from freecad.nestingworkbench.layout_offsets import (
    apply_layout_offsets, evaluate_offsets, set_layout_placement,
)
from freecad.nestingworkbench.datatypes.sheet import Sheet
from freecad.nestingworkbench.Tools.Stacker.stacker import SheetStacker


class LayoutOffsetTests(unittest.TestCase):
    def setUp(self):
        self.previous = App.ActiveDocument
        self.doc = App.newDocument("NestingOffsetTest")
        self.layout = self.doc.addObject("App::DocumentObjectGroup", "Layout_Test")
        self.spreadsheet = self.doc.addObject("Spreadsheet::Sheet", "Spreadsheet")
        self.spreadsheet.set("A1", "12 mm")
        self.spreadsheet.setAlias("A1", "OffsetX")
        self.doc.recompute()

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def assertVector(self, value, expected):
        self.assertLess((value - App.Vector(*expected)).Length, 1e-7)

    def make_sheet(self, index=0, cached=True):
        shape = self.doc.addObject("Part::Feature", "Part")
        shape.Shape = Part.makeBox(10, 10, 2)
        boundary = self.doc.addObject("Part::Feature", "Bounds")
        boundary.Shape = Part.makePlane(10, 10)
        shape.addProperty("App::PropertyLink", "BoundaryObject")
        shape.BoundaryObject = boundary
        local = App.Vector(index * 110 + 20, 30, 0)
        data = SimpleNamespace(
            id=f"Part{index}", fc_object=shape, label_text=None,
            placement=App.Placement(local, App.Rotation()) if cached else None,
            get_final_placement=lambda origin: App.Placement(origin + App.Vector(20, 30, 0), App.Rotation()),
        )
        sheet = Sheet(index, 100, 80, 10)
        sheet.parts = [SimpleNamespace(shape=data)]
        sheet.draw(self.doc, {'layout_offset': (12, -20, 5)}, self.layout)
        group = next(c for c in self.layout.Group if c.Label == f"Sheet_{index+1}")
        outline = next(c for c in group.Group if hasattr(c, 'Placement'))
        shapes = next(c for c in group.Group if c.Label.startswith("Shapes_"))
        container = shapes.Group[0]
        return outline, container, shape

    def apply(self):
        apply_layout_offsets(self.layout, {
            'layout_offset': (12, -20, 5),
            'offset_expressions': ('Spreadsheet.OffsetX', '-2 cm', '5 mm'),
        })
        self.doc.recompute()

    def test_lengths_units_and_spreadsheet_aliases(self):
        values, expressions = evaluate_offsets(self.doc, ['=-2 cm', 'Spreadsheet.OffsetX + 5 mm', '3'])
        self.assertEqual(values, [-20, 17, 3])
        self.assertEqual(expressions[0], '-2 cm')
        self.assertFalse(any(o.Name.startswith('NestingOffsetEvaluation') for o in self.doc.Objects))

    def test_invalid_inputs_leave_no_temporary_objects(self):
        for text in ['Missing.Value', '1 deg', '1 / 0', '1 mm * 1 mm']:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Y offset'):
                evaluate_offsets(self.doc, ['0', text, '0'])
            self.assertFalse(any(o.Name.startswith('NestingOffsetEvaluation') for o in self.doc.Objects))

    def test_drawing_cached_and_uncached_placements(self):
        for index, cached in enumerate((True, False)):
            outline, container, shape = self.make_sheet(index, cached)
            self.assertVector(outline.Placement.Base, (index * 110 + 12, -20, 5))
            self.assertVector(container.Placement.Base, (index * 110 + 32, 10, 5))
            self.assertVector(shape.Placement.Base, (0, 0, 0))

    def test_variable_updates_move_whole_layout_once(self):
        outline, container, shape = self.make_sheet()
        label = self.doc.addObject('Part::Feature', 'Label')
        label.Shape = Part.makePlane(2, 1)
        label.Placement.Base = App.Vector(1, 2, 0.1)
        container.addObject(label)
        self.apply()
        self.spreadsheet.set('A1', '40 mm')
        self.doc.recompute()
        self.assertVector(outline.Placement.Base, (40, -20, 5))
        self.assertVector(container.Placement.Base, (60, 10, 5))
        self.assertVector(label.Placement.Base, (1, 2, 0.1))
        self.assertVector(shape.Placement.Base, (0, 0, 0))
        self.assertVector(label.getGlobalPlacement().Base, (61, 12, 5.1))

    def test_manual_move_preserves_variable_binding(self):
        _, container, _ = self.make_sheet()
        self.apply()
        placement = container.Placement.copy()
        placement.Base += App.Vector(7, 8, 0)
        set_layout_placement(container, placement)
        self.spreadsheet.set('A1', '40 mm')
        self.doc.recompute()
        self.assertVector(container.Placement.Base, (67, 18, 5))

    def test_variable_change_during_nesting(self):
        outline, container, _ = self.make_sheet()
        self.spreadsheet.set('A1', '40 mm')
        self.doc.recompute()
        self.apply()
        self.assertVector(outline.Placement.Base, (40, -20, 5))
        self.assertVector(container.Placement.Base, (60, 10, 5))

    def test_stack_unstack_preserves_offsets_and_child_coordinates(self):
        self.make_sheet()
        outline, container, shape = self.make_sheet(1)
        self.apply()
        self.layout.addProperty('App::PropertyLength', 'SheetWidth')
        self.layout.SheetWidth = 100
        self.layout.addProperty('App::PropertyLength', 'PartSpacing')
        self.layout.PartSpacing = 10
        stacker = SheetStacker(self.layout)
        stacker.toggle_stack()
        self.assertVector(container.Placement.Base, (32, 10, 5))
        self.assertVector(shape.Placement.Base, (0, 0, 0))
        self.spreadsheet.set('A1', '40 mm')
        self.doc.recompute()
        stacker.toggle_stack()
        self.assertVector(outline.Placement.Base, (150, -20, 5))
        self.assertVector(container.Placement.Base, (170, 10, 5))
        self.assertVector(shape.Placement.Base, (0, 0, 0))

    def test_save_and_reload_keeps_expressions(self):
        outline, container, _ = self.make_sheet()
        self.apply()
        container_name = container.Name
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'offsets.FCStd')
            self.doc.saveAs(filename)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(filename)
            self.doc.Spreadsheet.set('A1', '40 mm')
            self.doc.recompute()
            self.assertVector(self.doc.getObject(container_name).Placement.Base, (60, 10, 5))
            self.assertEqual(dict(self.doc.Layout_Test.ExpressionEngine)['OffsetX'], 'Spreadsheet.OffsetX')

    def test_panel_restores_expressions_and_legacy_zero_offsets(self):
        from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
        panel = NestingPanel()
        try:
            self.make_sheet()
            self.apply()
            panel.controller._load_params_from_layout(self.layout)
            self.assertEqual(panel.offset_x_input.text(), 'Spreadsheet.OffsetX')
            self.assertEqual(panel.offset_y_input.value(), -20)
            self.assertEqual(panel.offset_y_input.expression(), '')
            legacy = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Legacy')
            panel.controller._load_params_from_layout(legacy)
            self.assertEqual(panel.offset_x_input.text(), '0 mm')
            self.assertEqual(panel.offset_y_input.text(), '0 mm')
            self.assertEqual(panel.offset_z_input.text(), '0 mm')
        finally:
            panel.deleteLater()

    def test_invalid_panel_input_preserves_existing_document(self):
        from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
        panel = NestingPanel()
        try:
            panel.offset_x_input.setText('Missing.Value')
            before = [o.Name for o in self.doc.Objects]
            panel.controller.execute_nesting()
            self.assertEqual(before, [o.Name for o in self.doc.Objects])
            self.assertIn('X offset', panel.status_label.text())
            self.assertFalse(panel.controller.is_running)
        finally:
            panel.deleteLater()

    def test_committing_a_nesting_job_binds_to_the_final_layout(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob
        final_layout = self.layout
        self.layout = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Temporary')
        outline, container, _ = self.make_sheet()
        parts_bin = self.doc.addObject('App::DocumentObjectGroup', 'PartsToPlace')
        self.layout.addObject(parts_bin)
        params = {
            'sheet_width': 100, 'sheet_height': 80, 'spacing': 10,
            'sheet_thickness': 2, 'font_path': '', 'show_bounds': False,
            'add_labels': False, 'label_height': 0.1, 'label_size': 10,
            'rotation_steps': 4, 'layout_offset': (12, -20, 5),
            'offset_expressions': ('Spreadsheet.OffsetX', '-2 cm', '5 mm'),
        }
        job = NestingJob.from_ga_result(self.doc, final_layout, params, None, self.layout, parts_bin, [])
        job.commit()
        self.layout = final_layout
        self.spreadsheet.set('A1', '40 mm')
        self.doc.recompute()
        self.assertVector(outline.Placement.Base, (40, -20, 5))
        self.assertVector(container.Placement.Base, (60, 10, 5))
        self.assertEqual(container.NestingLayout, final_layout)
        self.assertIsNone(self.doc.getObject('Layout_Temporary'))

    def test_numeric_offsets_remain_numbers_without_formulas(self):
        from freecad.nestingworkbench.offset_input import OffsetInput
        field = OffsetInput(self.doc, 'OffsetX')
        try:
            field.setValue(-12.5)
            self.assertEqual(field.value(), -12.5)
            self.assertEqual(field.expression(), '')
            field.spinbox.stepUp()
            self.assertEqual(field.value(), -11.5)
            apply_layout_offsets(self.layout, {
                'layout_offset': (field.value(), 0, 0), 'offset_expressions': ('', '', ''),
            })
            self.assertEqual(float(self.layout.OffsetX), -11.5)
            self.assertFalse(self.layout.ExpressionEngine)
        finally:
            field.cleanup()
            field.deleteLater()

    def test_native_fx_dialog_sets_a_formula_and_displays_its_value(self):
        from PySide import QtCore, QtWidgets, QtGui
        from freecad.nestingworkbench.offset_input import OffsetInput
        field = OffsetInput(self.doc, 'OffsetX')
        dialog = None
        try:
            field.show()
            field.setValue(10)
            # '=' opens the same native dialog as clicking the fx icon.
            QtWidgets.QApplication.sendEvent(field.spinbox, QtGui.QKeyEvent(
                QtCore.QEvent.KeyPress, QtCore.Qt.Key_Equal, QtCore.Qt.NoModifier, '='))
            dialog = field.spinbox.findChild(QtWidgets.QDialog)
            self.assertIsNotNone(dialog)
            self.assertEqual(dialog.metaObject().className(), 'Gui::Dialog::DlgExpressionInput')
            editor = dialog.findChild(QtWidgets.QWidget, 'expression')
            editor.setProperty('plainText', 'Spreadsheet.OffsetX + 5 mm')
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(400, loop.quit)
            loop.exec_()
            dialog.accept()
            self.assertEqual(field.expression(), 'Spreadsheet.OffsetX + 5 mm')
            self.assertEqual(field.value(), 17)
            self.assertNotIn('Spreadsheet', field.spinbox.text())
            self.assertTrue(field.spinbox.isReadOnly())
            field.setExpression('')
            self.assertEqual(field.value(), 17)
            self.assertFalse(field.spinbox.isReadOnly())
        finally:
            if dialog:
                dialog.reject()
            field.hide()
            field.cleanup()
            field.deleteLater()

    def test_numeric_entry_respects_the_widget_locale(self):
        from PySide import QtCore, QtWidgets
        from freecad.nestingworkbench.offset_input import OffsetInput
        field = OffsetInput(self.doc, 'OffsetX')
        try:
            edit = field.spinbox.findChild(QtWidgets.QLineEdit)
            edit.setText(field.spinbox.locale().toString(-12.5, 'f', 1) + ' mm')
            QtCore.QMetaObject.invokeMethod(field.spinbox, 'handlePendingEmit', QtCore.Qt.DirectConnection)
            self.assertEqual(field.value(), -12.5)
            self.assertEqual(field.text(), '-12.5 mm')
            self.assertEqual(field.expression(), '')
        finally:
            field.cleanup()
            field.deleteLater()

    def test_closing_panel_removes_expression_editor_helpers(self):
        from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
        panel = NestingPanel()
        try:
            panel.offset_x_input.setValue(4)
            panel.controller.on_panel_closed()
            self.assertFalse(any(o.Name.startswith('NestingOffsetInput') for o in self.doc.Objects))
            self.assertEqual(self.layout.ExpressionEngine, [])
        finally:
            panel.deleteLater()

    def test_simulation_uses_xyz_offsets(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_logic import _bind_sim_update
        _, container, shape = self.make_sheet()
        # Use an independent transient shape so the committed container is untouched.
        data = SimpleNamespace(
            fc_object=shape, placement=None,
            get_final_placement=lambda origin: App.Placement(origin + App.Vector(20, 30, 0), App.Rotation()),
        )
        nester = SimpleNamespace()
        _bind_sim_update(nester, (12, -20, 5))
        nester.update_callback(data, Sheet(1, 100, 80, 10))
        self.assertVector(shape.BoundaryObject.Placement.Base, (142, 10, 5))
        self.assertVector(self.doc.getObject('sim_sheet_boundary_1').Placement.Base, (122, -20, 5))


if __name__ == '__main__':
    unittest.main()
