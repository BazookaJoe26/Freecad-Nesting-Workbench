# SPDX-License-Identifier: LGPL-2.1-or-later
"""Preference round-trips using FreeCAD's Python and real Qt widgets offscreen."""
import os
import tempfile
import unittest
from unittest.mock import patch

import FreeCAD as App
import Part
from PySide import QtWidgets
from freecad.nestingworkbench import panel_preferences as preferences
from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController


class MemoryPreferences:
    def __init__(self):
        self.values, self.groups = {}, {}

    def GetGroup(self, key):
        return self.groups.setdefault(key, MemoryPreferences())

    def __getattr__(self, method):
        if method.startswith('Get'):
            return lambda key, default: self.values.get(key, default)
        if method.startswith('Set'):
            return lambda key, value: self.values.__setitem__(key, value)
        raise AttributeError(method)


class PanelPreferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
            ['NestingPreferenceTests', '-platform', 'offscreen'])

    def setUp(self):
        self.previous = App.ActiveDocument
        self.doc = App.newDocument('NestingPreferencesTest')
        self.source = self.doc.addObject('Part::Feature', 'Board')
        self.source.Shape = Part.makeBox(8, 2, 1)
        self.prefs = MemoryPreferences()
        self.panels = []
        self.pref_patch = patch.object(preferences, '_prefs', side_effect=lambda value: value if value is not None else self.prefs)
        self.selection_patch = patch.object(NestingController, 'load_selection')
        self.pref_patch.start()
        self.selection_patch.start()

    def tearDown(self):
        for panel in self.panels:
            panel.deleteLater()
        self.pref_patch.stop()
        self.selection_patch.stop()
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def panel(self):
        panel = NestingPanel()
        self.panels.append(panel)
        return panel

    def snapshot(self, panel):
        result = {}
        for fields, read in (
            (preferences.FLOAT_FIELDS, 'value'), (preferences.INT_FIELDS, 'value'),
            (preferences.BOOL_FIELDS, 'isChecked'), (preferences.COMBO_FIELDS, 'currentText'),
            (preferences.TEXT_FIELDS, 'text'),
        ):
            result.update({name: getattr(getattr(panel, name), read)() for name in fields})
        result.update({name: getattr(panel, name).value() for name, _, _ in preferences.ROTATION_FIELDS})
        result['font'] = panel.selected_font_path
        return result

    def test_all_inputs_are_remembered_without_running_or_accepting_a_job(self):
        first = self.panel()
        for fields in (preferences.FLOAT_FIELDS, preferences.INT_FIELDS):
            for name in fields:
                widget = getattr(first, name)
                widget.setValue(widget.value() + 1)
        for name in preferences.BOOL_FIELDS:
            widget = getattr(first, name)
            widget.setChecked(not widget.isChecked())
        for name in preferences.COMBO_FIELDS:
            widget = getattr(first, name)
            widget.setCurrentIndex((widget.currentIndex() + 1) % widget.count())
        for name in preferences.TEXT_FIELDS:
            getattr(first, name).setText('Missing.Offset + 5 mm')
        for name, _, _ in preferences.ROTATION_FIELDS:
            getattr(first, name).setValue(2)
        with patch.object(QtWidgets.QFileDialog, 'getOpenFileName', return_value=('C:/Fonts/MyFont.ttf', '')):
            first.select_font_file()
        expected = self.snapshot(first)
        second = self.panel()
        self.assertEqual(self.snapshot(second), expected)
        self.assertEqual(second.font_label.text(), 'MyFont.ttf')
        self.assertTrue(second.physics_settings_group.isVisibleTo(second))
        self.assertFalse(second.minkowski_settings_group.isVisibleTo(second))

    def test_cancel_keeps_last_settings_and_zero_and_false_values(self):
        first = self.panel()
        first.sheet_grain_direction_combo.setCurrentText('Y')
        first.label_height_input.setValue(0)
        first.add_labels_checkbox.setChecked(False)
        first.physics_anneal_rot_steps.setValue(0)
        first.physics_anneal_steps_input.setValue(0)
        self.assertTrue(first.reject())
        second = self.panel()
        self.assertEqual(second.sheet_grain_direction_combo.currentText(), 'Y')
        self.assertEqual(second.label_height_input.value(), 0)
        self.assertEqual(second.physics_anneal_steps_input.value(), 0)
        self.assertEqual(second.physics_anneal_rot_steps.value(), 0)
        self.assertFalse(second.add_labels_checkbox.isChecked())
        self.assertFalse(second.label_size_input.isEnabled())

    def test_existing_preference_keys_are_still_loaded(self):
        self.prefs.SetFloat('SheetWidth', 1234)
        self.prefs.SetInt('MinkowskiRotationSteps', 8)
        self.prefs.SetInt('PhysicsRotationSteps', 12)
        self.prefs.SetFloat('PhysicsAnnealMaxAmp', 37)
        self.prefs.SetBool('VerboseLogging', True)
        panel = self.panel()
        self.assertEqual(panel.sheet_width_input.value(), 1234)
        self.assertEqual(panel.rotation_angles[panel.minkowski_rotation_steps_slider.value()], 45)
        self.assertEqual(preferences.PHYSICS_ROTATION_PRESETS[panel.physics_rotation_steps_slider.value()], 30)
        self.assertEqual(panel.physics_anneal_max_amp.value(), 37)
        self.assertTrue(panel.verbose_logging_checkbox.isChecked())

    def sheet_formulas(self, panel):
        sheet = self.doc.addObject('Spreadsheet::Sheet', 'Spreadsheet')
        for row, (alias, value) in enumerate((('Width', 200), ('Height', 150),
                                             ('Thickness', 18), ('Gap', 6)), 1):
            sheet.set(f'A{row}', f'{value} mm')
            sheet.setAlias(f'A{row}', alias)
        self.doc.recompute()
        for name, alias in (('sheet_width_input', 'Width'), ('sheet_height_input', 'Height'),
                            ('sheet_thickness_input', 'Thickness'), ('part_spacing_input', 'Gap')):
            getattr(panel, name).setExpression('Spreadsheet.' + alias)
        return sheet

    def test_all_four_sheet_lengths_use_native_fx_fields_and_numeric_values(self):
        panel = self.panel()
        for name, value in (('sheet_width_input', 200), ('sheet_height_input', 150),
                            ('sheet_thickness_input', 18), ('part_spacing_input', 0)):
            with self.subTest(field=name):
                widget = getattr(panel, name)
                widget.setValue(value)
                self.assertEqual(widget.value(), value)
                self.assertEqual(widget.expression(), '')
                self.assertEqual(widget.spinbox.metaObject().className(), 'Gui::QuantitySpinBox')
                self.assertTrue(widget.binding.isBound())

    def test_sheet_formulas_are_remembered_when_closing_without_nesting(self):
        first = self.panel()
        self.sheet_formulas(first)
        first.reject()
        second = self.panel()
        for name, (prop, _) in preferences.SHEET_LENGTH_FIELDS.items():
            with self.subTest(field=name):
                widget = getattr(second, name)
                self.assertTrue(widget.expression().startswith('Spreadsheet.'))
                self.assertEqual(widget.expression(), self.prefs.GetString(prop + 'Expression', ''))
                self.assertGreater(widget.validatedValue(), 0)

    def test_nesting_reevaluates_current_sheet_variables(self):
        panel = self.panel()
        sheet = self.sheet_formulas(panel)
        sheet.set('A1', '240 mm')
        sheet.set('A2', '175 mm')
        sheet.set('A3', '12 mm')
        sheet.set('A4', '8 mm')
        self.doc.recompute()
        values = panel.controller._collect_ui_params()
        self.assertEqual([values[k] for k in ('sheet_width', 'sheet_height', 'sheet_thickness', 'spacing')],
                         [240, 175, 12, 8])
        self.assertEqual(values['length_expressions']['PartSpacing'], 'Spreadsheet.Gap')

    def test_sheet_formulas_preserve_ranges_and_reject_invalid_updated_values(self):
        panel = self.panel()
        sheet = self.sheet_formulas(panel)
        for row, label in ((1, 'Sheet Width'), (2, 'Sheet Height'),
                           (3, 'Sheet Thickness'), (4, 'Part Spacing')):
            with self.subTest(field=label):
                original = str(sheet.get(f'A{row}'))
                try:
                    sheet.set(f'A{row}', '=-1 mm')
                    self.doc.recompute()
                    before = [o.Name for o in self.doc.Objects]
                    panel.controller.execute_nesting()
                    self.assertIn(label, panel.status_label.text())
                    self.assertFalse(panel.controller.is_running)
                    self.assertEqual(before, [o.Name for o in self.doc.Objects])
                finally:
                    sheet.set(f'A{row}', original)
                    self.doc.recompute()
        panel.part_spacing_input.setValue(0)
        self.assertEqual(panel.controller._collect_ui_params()['spacing'], 0)

    def test_layout_preserves_sheet_formulas_and_the_geometry_snapshot(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob
        panel = self.panel()
        sheet = self.sheet_formulas(panel)
        layout = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Test')
        job = NestingJob.__new__(NestingJob)
        job.params = panel.controller._collect_ui_params()
        job._apply_properties(layout)
        sheet.set('A1', '240 mm')
        self.doc.recompute()
        # Existing geometry/stock dimensions stay consistent until re-nesting.
        self.assertEqual(float(layout.SheetWidth), 200)
        panel.controller._load_params_from_layout(layout)
        self.assertEqual(panel.sheet_width_input.expression(), 'Spreadsheet.Width')
        self.assertEqual(panel.controller._collect_ui_params()['sheet_width'], 240)
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'sheet-expressions.FCStd')
            self.doc.saveAs(filename)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(filename)
            second = self.panel()
            second.controller._load_params_from_layout(self.doc.Layout_Test)
            for name in preferences.SHEET_LENGTH_FIELDS:
                self.assertTrue(getattr(second, name).expression().startswith('Spreadsheet.'))
            self.assertEqual(second.controller._collect_ui_params()['sheet_width'], 240)

    def test_replacing_sheet_formulas_with_numbers_clears_saved_expressions(self):
        panel = self.panel()
        self.sheet_formulas(panel)
        for name in preferences.SHEET_LENGTH_FIELDS:
            getattr(panel, name).setValue(20)
        second = self.panel()
        for name, (prop, _) in preferences.SHEET_LENGTH_FIELDS.items():
            self.assertEqual(getattr(second, name).expression(), '')
            self.assertEqual(getattr(second, name).value(), 20)
            self.assertEqual(self.prefs.GetString(prop + 'Expression', ''), '')

    def test_sheet_grain_can_be_disabled_and_stays_disabled_on_reopen(self):
        first = self.panel()
        self.assertEqual(first.sheet_grain_direction_combo.currentText(), 'None')
        first.sheet_grain_direction_combo.setCurrentText('Y')
        first.sheet_grain_direction_combo.setCurrentText('None')
        first.controller.load_shapes([self.source])
        first.shape_table.cellWidget(0, 4).setCurrentIndex(3)  # stored Z grain
        _, quantities, _, _ = first.controller._collect_job_parameters(
            {'rotation_steps': 4, 'sheet_grain_direction': 'None'})
        self.assertEqual(quantities[self.source.Label]['grain_direction'], 'Z')
        second = self.panel()
        self.assertEqual(second.sheet_grain_direction_combo.currentText(), 'None')
        second.controller.load_shapes([self.source])
        self.assertEqual(second.shape_table.cellWidget(0, 4).currentData(), 'Z')

    def test_custom_edge_spacing_is_optional_and_remembers_its_value(self):
        first = self.panel()
        self.assertFalse(first.custom_edge_spacing_checkbox.isChecked())
        self.assertFalse(first.edge_spacing_input.isEnabled())
        first.custom_edge_spacing_checkbox.setChecked(True)
        self.assertTrue(first.edge_spacing_input.isEnabled())
        first.edge_spacing_input.setValue(3.5)
        second = self.panel()
        self.assertTrue(second.custom_edge_spacing_checkbox.isChecked())
        self.assertEqual(second.edge_spacing_input.value(), 3.5)
        params = second.controller._collect_ui_params()
        self.assertTrue(params['custom_edge_spacing'])
        self.assertEqual(second.controller._prepare_algo_kwargs(params)['edge_spacing'], 3.5)
        second.custom_edge_spacing_checkbox.setChecked(False)
        params = second.controller._collect_ui_params()
        self.assertEqual(second.controller._prepare_algo_kwargs(params)['edge_spacing'], 0)
        third = self.panel()
        self.assertFalse(third.custom_edge_spacing_checkbox.isChecked())
        self.assertFalse(third.edge_spacing_input.isEnabled())
        self.assertEqual(third.edge_spacing_input.value(), 3.5)

    def test_part_options_return_when_selecting_the_same_object(self):
        first = self.panel()
        first.controller.load_shapes([self.source])
        table = first.shape_table
        table.cellWidget(0, 1).setValue(7)
        table.cellWidget(0, 2).findChild(QtWidgets.QCheckBox).setChecked(True)
        table.cellWidget(0, 2).findChild(QtWidgets.QSpinBox).setValue(2)
        table.cellWidget(0, 3).setCurrentText('Y-')
        table.cellWidget(0, 4).setCurrentIndex(3)  # Z grain
        table.cellWidget(0, 5).setChecked(True)
        remembered = preferences.load_part_settings(self.source)
        self.assertEqual(remembered['grain_direction'], 'Z')
        second = self.panel()
        second.controller.load_shapes([self.source])
        table = second.shape_table
        self.assertEqual(table.cellWidget(0, 1).value(), 7)
        self.assertEqual(table.cellWidget(0, 2).findChild(QtWidgets.QSpinBox).value(), 2)
        self.assertTrue(table.cellWidget(0, 2).findChild(QtWidgets.QCheckBox).isChecked())
        self.assertEqual(table.cellWidget(0, 3).currentText(), 'Y-')
        self.assertEqual(table.cellWidget(0, 4).currentData(), 'Z')
        self.assertTrue(table.cellWidget(0, 5).isChecked())

    def test_part_preferences_survive_document_save_and_reopen(self):
        preferences.save_part_settings(self.source, {'grain_direction': 'Y', 'quantity': 3})
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            self.assertTrue(os.path.realpath(directory).startswith(
                os.path.realpath(os.path.dirname(__file__)) + os.sep))
            path = os.path.join(directory, 'Board.FCStd')
            self.doc.recompute()
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            self.assertEqual(preferences.load_part_settings(self.doc.getObject('Board')),
                             {'grain_direction': 'Y', 'quantity': 3})

    def test_part_names_and_documents_do_not_share_preferences(self):
        other = self.doc.addObject('Part::Feature', 'Other')
        other.Label = self.source.Label
        preferences.save_part_settings(self.source, {'quantity': 6})
        self.assertFalse(preferences.load_part_settings(other))
        another_doc = App.newDocument('AnotherPreferencesTest')
        try:
            another = another_doc.addObject('Part::Feature', 'Board')
            self.assertFalse(preferences.load_part_settings(another))
        finally:
            App.closeDocument(another_doc.Name)
            App.setActiveDocument(self.doc.Name)

    def test_saved_layout_options_take_precedence_over_last_object_options(self):
        preferences.save_part_settings(self.source, {'grain_direction': 'Y', 'quantity': 8})
        panel = self.panel()
        panel.controller.load_shapes(
            [self.source], is_reloading_layout=True,
            initial_quantities={self.source.Label: 2},
            initial_grain_directions={self.source.Label: 'X'})
        self.assertEqual(panel.shape_table.cellWidget(0, 1).value(), 2)
        self.assertEqual(panel.shape_table.cellWidget(0, 4).currentData(), 'X')


if __name__ == '__main__':
    unittest.main()
