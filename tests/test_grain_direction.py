# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run with FreeCAD's Python: python -m unittest discover -s tests -p test_grain_direction.py."""

import os
import pickle
import random
import tempfile
import unittest
from types import SimpleNamespace

import FreeCAD as App
import Part
import shapely
from shapely.geometry import box

from freecad.nestingworkbench.freecad_helpers import grain_rotation_angles, get_up_direction_rotation
from freecad.nestingworkbench.rotation_constraints import rotation_angles, placement_angles
from freecad.nestingworkbench.Tools.Nesting.algorithms.genetic_utils import mutate_genes
from freecad.nestingworkbench.Tools.Nesting.algorithms.nesting_strategy import Nester
from freecad.nestingworkbench.Tools.Nesting.algorithms.physics_nester import PhysicsNester
from freecad.nestingworkbench.Tools.Nesting.ga_snapshot import (
    PartSnapshot, SnapshotShape, build_part_catalog, snapshot_member, nest_from_snapshot,
)


def make_part(grain_angles=(90.0, 270.0), steps=4, gene=None, part_id="GrainTest_1"):
    return SnapshotShape(PartSnapshot(
        part_id=part_id, type_label="GrainTest", polygon_wkb=shapely.to_wkb(box(-4, -1, 4, 1)),
        gene_angle=gene, rotation_steps=steps, spacing=0, deflection=0.05,
        simplification=1.0, fill_sheet=False, grain_angles=grain_angles,
    ))


class GrainDirectionTests(unittest.TestCase):
    def test_all_up_directions_align_grain_to_both_sheet_axes(self):
        axes = {"X": App.Vector(1, 0, 0), "Y": App.Vector(0, 1, 0), "Z": App.Vector(0, 0, 1)}
        for up in ("Z+", "Z-", "Y+", "Y-", "X+", "X-"):
            for grain, vector in axes.items():
                for sheet in ("X", "Y"):
                    with self.subTest(up=up, grain=grain, sheet=sheet):
                        if up[0] == grain:
                            with self.assertRaisesRegex(ValueError, "perpendicular"):
                                grain_rotation_angles(up, grain, sheet)
                            continue
                        angles = grain_rotation_angles(up, grain, sheet)
                        self.assertAlmostEqual(angles[1] - angles[0], 180)
                        for angle in angles:
                            projected = get_up_direction_rotation(up).multVec(vector)
                            aligned = App.Rotation(App.Vector(0, 0, 1), angle).multVec(projected)
                            self.assertAlmostEqual(aligned.y if sheet == "X" else aligned.x, 0)
                            self.assertAlmostEqual(aligned.z, 0)

    def test_none_keeps_rotation_settings(self):
        self.assertIsNone(grain_rotation_angles("Z+", "None"))
        self.assertEqual(rotation_angles(make_part(None)), [0, 90, 180, 270])
        self.assertEqual(rotation_angles(make_part(None, steps=1)), [0])
        self.assertEqual(rotation_angles(make_part(None, steps=0)), [0])

    def test_optional_sheet_grain_uses_normal_rotations_with_part_grain_set(self):
        for sheet in (None, 'None'):
            angles = grain_rotation_angles('Z+', 'Y', sheet)
            self.assertIsNone(angles)
            part = make_part(angles)
            self.assertEqual(rotation_angles(part), [0, 90, 180, 270])
            sheets, unplaced = Nester(9, 3, 4, quiet=True).nest([part])
            self.assertFalse(unplaced)
            self.assertEqual(len(sheets), 1)

    def test_grain_overrides_even_incompatible_rotation_steps_and_genes(self):
        for steps in (0, 1, 3, 7, 360):
            part = make_part(steps=steps, gene=120)
            self.assertEqual(rotation_angles(part), [90, 270])
            self.assertEqual(placement_angles(part), [90])

    def test_constrained_part_is_left_unplaced_instead_of_turned_cross_grain(self):
        for angles, expected in ((None, 1), ((90, 270), 0)):
            sheets, unplaced = Nester(9, 3, 4, quiet=True).nest([make_part(angles)])
            self.assertEqual(len(sheets), expected)
            self.assertEqual(len(unplaced), 1 - expected)

    def test_grain_can_require_rotation_when_global_rotation_is_disabled(self):
        sheets, unplaced = Nester(3, 9, 1, quiet=True).nest([make_part(steps=1)])
        self.assertFalse(unplaced)
        self.assertEqual(len(sheets), 1)
        self.assertIn(sheets[0].parts[0].angle, (90, 270))

    def test_workers_preserve_grain_in_standalone_and_shared_catalog_tasks(self):
        part = make_part(gene=120)
        layout = SimpleNamespace(parts=[part], genes=[(part.id, 120)], direction=(0, -1))
        params = {'sheet_width': 3, 'sheet_height': 9, 'rotation_steps': 3}
        catalog, payload = build_part_catalog(layout.parts)
        geometries = {key: shapely.from_wkb(wkb) for key, wkb in payload.items()}
        for use_catalog in (False, True):
            with self.subTest(catalog=use_catalog):
                task = snapshot_member(layout, params, 0, 0, 123,
                                       part_catalog=catalog if use_catalog else None)
                task = pickle.loads(pickle.dumps(task))
                result = nest_from_snapshot(task, catalog if use_catalog else None, geometries)
                self.assertFalse(result.unplaced_ids)
                self.assertEqual(len(result.placements), 1)
                self.assertIn(result.placements[0][-1], (90, 270))

    def test_mutation_can_choose_both_grain_angles_with_global_rotation_disabled(self):
        options = {"grain": (90, 270), "free": (0,)}
        rng = random.Random(42)
        seen = set()
        for _ in range(40):
            mutated = dict(mutate_genes([("grain", 90), ("free", 0)], 1, 1,
                                        rng=rng, angle_options=options))
            self.assertEqual(mutated['free'], 0)
            self.assertIn(mutated['grain'], options['grain'])
            seen.add(mutated['grain'])
        self.assertEqual(seen, {90, 270})

    def test_physics_spawn_and_shaking_never_try_cross_grain(self):
        part = make_part(steps=360)
        tried = []
        set_rotation = part.set_rotation

        def record(angle, reposition=True):
            tried.append(angle)
            set_rotation(angle, reposition)

        part.set_rotation = record
        random_state = random.getstate()
        try:
            random.seed(42)
            nester = PhysicsNester(4, 10, 360, max_nesting_steps=3,
                                   anneal_steps=3, anneal_rot_steps=8)
            sheets, unplaced = nester.nest([part])
            self.assertFalse(unplaced)
            nester._anneal_part(part, sheets[0], (0, -1))
        finally:
            random.setstate(random_state)
        self.assertTrue(tried)
        self.assertTrue(set(tried).issubset({90, 270}), tried)

    def test_fill_copies_keep_the_grain_constraint(self):
        counter = [0]

        def spawn():
            counter[0] += 1
            part = make_part(steps=3, part_id=f"GrainFill_{counter[0]}")
            part.fill_sheet = True
            part.master_label = "GrainFill"
            part.spawn_next = spawn
            return part

        sheets, _ = Nester(4, 16, 3, quiet=True).nest([spawn()])
        placed = [p for s in sheets for p in s.parts]
        self.assertGreater(len(placed), 1)
        self.assertTrue(all(p.angle in (90, 270) for p in placed))


@unittest.skipUnless(App.GuiUp, "Document preparation requires the FreeCAD GUI; run in FreeCAD's Python console.")
class GrainDocumentTests(unittest.TestCase):
    def setUp(self):
        from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
        self.previous = App.ActiveDocument
        self.doc = App.newDocument("NestingGrainTest")
        self.preparer = ShapePreparer(self.doc, {})
        self.source = self.doc.addObject("Part::Feature", "Board")
        self.source.Shape = Part.makeBox(8, 2, 1)
        self.params = {
            'sheet_width': 10, 'sheet_height': 20, 'sheet_thickness': 1,
            'sheet_grain_direction': 'Y', 'spacing': 0, 'rotation_steps': 3,
            'deflection': 0.05, 'simplification': 0.01, 'add_labels': False,
            'font_path': '', 'show_bounds': True, 'label_height': 0, 'label_size': 10,
        }
        self.quantities = {'Board': {
            'quantity': 2, 'rotation_steps': 3, 'up_direction': 'Z+',
            'grain_direction': 'X', 'fill_sheet': True,
        }}

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def prepare(self, source=None):
        source = source or self.source
        layout = self.doc.addObject("App::DocumentObjectGroup", "Layout_Grain")
        parts_group = self.doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
        layout.addObject(parts_group)
        parts = self.preparer.prepare_parts(self.params, self.quantities,
                                            {source.Label: source}, layout, parts_group)
        return layout, parts

    def make_controller(self):
        from PySide import QtWidgets
        from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
        ui = SimpleNamespace()
        ui.shape_table = QtWidgets.QTableWidget(0, 6)
        ui.nest_button = QtWidgets.QPushButton()
        ui.status_label = QtWidgets.QLabel()
        ui.selected_shapes_to_process = []
        ui.add_part_row = lambda *args, **kwargs: NestingPanel.add_part_row(ui, *args, **kwargs)
        ui.algorithm_dropdown = QtWidgets.QComboBox()
        ui.algorithm_dropdown.addItems(['Minkowski', 'Physics'])
        ui.sheet_grain_direction_combo = QtWidgets.QComboBox()
        ui.sheet_grain_direction_combo.addItems(['None', 'X', 'Y'])
        ui.custom_edge_spacing_checkbox = QtWidgets.QCheckBox()
        ui.edge_spacing_input = QtWidgets.QDoubleSpinBox()
        ui.rotation_angles = [360, 180, 120, 90]
        for name in ('sheet_width_input', 'sheet_height_input', 'sheet_thickness_input',
                     'part_spacing_input', 'simplification_input', 'label_size_input',
                     'minkowski_generations_input', 'minkowski_population_size_input',
                     'minkowski_direction_dial', 'deflection_input',
                     'minkowski_rotation_steps_slider', 'physics_rotation_steps_slider'):
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(0, 10000)
            setattr(ui, name, widget)
        for axis in 'xyz':
            setattr(ui, f'offset_{axis}_input', QtWidgets.QLineEdit())
        controller = NestingController.__new__(NestingController)
        controller.ui, controller.doc = ui, self.doc
        return controller

    def test_preparation_fill_spawners_and_reloaded_masters_keep_grain(self):
        layout, parts = self.prepare()
        self.assertEqual(len(parts), 3)  # two required, one fill seed
        self.assertTrue(all(p.grain_angles == (90, 270) for p in parts))
        extra = parts[-1].spawn_next()
        self.assertEqual(extra.grain_angles, (90, 270))
        masters = next(c for c in layout.Group if c.Label.startswith("MasterShapes"))
        container = masters.Group[0]
        self.assertEqual(container.GrainDirection, 'X')
        master = next(c for c in container.Group if c.Label.startswith("master_shape_"))
        reloaded, parts = self.prepare(master)
        self.assertTrue(all(p.grain_angles == (90, 270) for p in parts))
        new_masters = next(c for c in reloaded.Group if c.Label.startswith("MasterShapes"))
        self.assertEqual(new_masters.Group[0].GrainDirection, 'X')

    def test_cached_geometry_does_not_retain_old_grain_constraints(self):
        _, constrained = self.prepare()
        self.quantities['Board']['grain_direction'] = 'None'
        _, free = self.prepare()
        self.assertTrue(all(p.grain_angles is None for p in free))
        self.assertTrue(all(p.grain_angles == (90, 270) for p in constrained))

    def test_ga_generations_preserve_grain_in_offspring_and_immigrants(self):
        from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator
        from freecad.nestingworkbench.Tools.Nesting.layout_manager import LayoutManager
        coordinator = GACoordinator(self.doc, self.preparer)
        coordinator.rng = random.Random(42)
        manager = LayoutManager(self.doc, rng=coordinator.rng, geometry_only=True)
        coordinator.layout_manager = manager
        master_map = {self.source.Label: self.source}
        layouts = manager.create_ga_population(master_map, self.quantities, self.params, 4, 3)
        for generation in range(3):
            _, cancelled = coordinator._run_generation(
                layouts, generation, 3, self.params, 3, {}, False, lambda: False, False)
            self.assertFalse(cancelled)
            for layout in layouts:
                self.assertTrue(layout.genes)
                self.assertTrue(all(angle in (90, 270) for _, angle in layout.genes))
            layouts.sort(key=lambda layout: layout.fitness)
            if generation < 2:
                layouts = coordinator._build_next_generation(
                    generation, layouts, layouts[:2], master_map, self.quantities,
                    self.params, 3, 1.0, 0.3, False)
        winner = manager.materialize_layout(layouts[0], self.params)
        self.assertTrue(winner.sheets)
        self.assertTrue(all(p.angle in (90, 270) for s in winner.sheets for p in s.parts))

    def test_grain_metadata_survives_document_save_and_reopen(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob
        layout, _ = self.prepare()
        job = NestingJob.__new__(NestingJob)
        job.params = self.params
        job._apply_properties(layout)
        layout_name = layout.Name
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "Grain.FCStd")
            self.doc.recompute()
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            layout = self.doc.getObject(layout_name)
            self.assertEqual(layout.SheetGrainDirection, 'Y')
            masters = next(c for c in layout.Group if c.Label.startswith("MasterShapes"))
            self.assertEqual(masters.Group[0].GrainDirection, 'X')
            controller = self.make_controller()
            controller._load_params_from_layout(layout)
            controller._load_shapes_from_layout(layout)
            self.assertEqual(controller.ui.sheet_grain_direction_combo.currentText(), 'Y')
            self.assertEqual(controller.ui.shape_table.cellWidget(0, 4).currentData(), 'X')
            _, quantities, _, _ = controller._collect_job_parameters(self.params)
            self.assertEqual(quantities['Board']['grain_direction'], 'X')
            self.assertTrue(quantities['Board']['fill_sheet'])

    def test_ui_rejects_parallel_up_and_grain_without_dropping_the_part(self):
        controller = self.make_controller()
        controller.load_shapes([self.source])
        table = controller.ui.shape_table
        grain = table.cellWidget(0, 4)
        self.assertEqual(grain.currentData(), 'None')
        self.assertFalse(grain.model().item(grain.findData('Z')).isEnabled())
        grain.setCurrentIndex(grain.findData('X'))
        table.cellWidget(0, 3).setCurrentText('X+')
        self.assertFalse(grain.model().item(grain.findData('X')).isEnabled())
        with self.assertRaisesRegex(ValueError, 'Board:.*perpendicular'):
            controller._collect_job_parameters(self.params)
        self.assertEqual(grain.currentData(), 'X')

    def test_reopening_older_master_defaults_to_unrestricted_grain(self):
        layout, _ = self.prepare()
        masters = next(c for c in layout.Group if c.Label.startswith('MasterShapes'))
        masters.Group[0].removeProperty('GrainDirection')
        controller = self.make_controller()
        missing, dropped = controller._load_shapes_from_layout(layout)
        self.assertFalse(dropped)
        self.assertTrue(any('grain direction' in text for text in missing))
        self.assertEqual(controller.ui.shape_table.cellWidget(0, 4).currentData(), 'None')


if __name__ == '__main__':
    unittest.main()
