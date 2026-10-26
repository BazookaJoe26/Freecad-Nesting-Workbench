# SPDX-License-Identifier: LGPL-2.1-or-later
"""Edge clearance must be independent of padded part-to-part collisions."""
import pickle
import os
import random
import tempfile
import unittest
import zlib
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from types import SimpleNamespace

import FreeCAD as App
import Part
import shapely
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, box

from freecad.nestingworkbench.edge_spacing import part_edge_bounds
from freecad.nestingworkbench.Tools.Nesting.algorithms.nesting_strategy import Nester
from freecad.nestingworkbench.Tools.Nesting.algorithms.physics_nester import PhysicsNester
from freecad.nestingworkbench.Tools.Nesting.ga_snapshot import (
    PartSnapshot, SnapshotShape, build_part_catalog, snapshot_member, nest_from_snapshot,
)
from freecad.nestingworkbench.Tools.Nesting.ga_worker import init_worker, worker_nest


def make_part(width=40, height=50, spacing=12, number=1, outline=None, angle=None):
    raw = outline if outline is not None else box(0, 0, width, height)
    buffered = raw.buffer(spacing / 2, join_style=1)
    center = buffered.centroid
    raw = translate(raw, xoff=-center.x, yoff=-center.y)
    buffered = translate(buffered, xoff=-center.x, yoff=-center.y)
    label = f'EdgeTest_{zlib.crc32(shapely.to_wkb(buffered))}'
    return SnapshotShape(PartSnapshot(
        f'{label}_{number}', label, shapely.to_wkb(buffered), angle, 1,
        spacing, 0.05, 0.01, False, unbuffered_wkb=shapely.to_wkb(raw)))


def material_polygon(part):
    pivot = part.original_polygon.centroid
    outline = rotate(part.unbuffered_polygon, part.angle, origin=(pivot.x, pivot.y))
    center = part.polygon.centroid
    return translate(outline, xoff=center.x - pivot.x, yoff=center.y - pivot.y)


class EdgeSpacingTests(unittest.TestCase):
    def assertInside(self, part, width, height, edge=0):
        x0, y0, x1, y1 = part_edge_bounds(part)
        self.assertGreaterEqual(x0 + 1e-6, edge)
        self.assertGreaterEqual(y0 + 1e-6, edge)
        self.assertLessEqual(x1, width - edge + 1e-6)
        self.assertLessEqual(y1, height - edge + 1e-6)

    def test_parts_can_fit_sheet_exactly_despite_collision_padding(self):
        sheets, unplaced = Nester(40, 50, quiet=True).nest([make_part()])
        self.assertFalse(unplaced)
        self.assertInside(sheets[0].parts[0].shape, 40, 50)
        self.assertAlmostEqual(sheets[0].used_area, 40 * 50)

    def test_part_spacing_is_kept_between_parts_at_flush_sheet_edges(self):
        sheets, unplaced = Nester(92, 50, quiet=True).nest([make_part(number=i) for i in (1, 2)])
        self.assertFalse(unplaced)
        self.assertEqual(len(sheets), 1)
        first, second = [p.shape for p in sheets[0].parts]
        self.assertInside(first, 92, 50)
        self.assertInside(second, 92, 50)
        self.assertAlmostEqual(material_polygon(first).distance(material_polygon(second)), 12, places=5)

    def test_custom_edge_spacing_can_be_less_than_half_part_spacing(self):
        sheets, unplaced = Nester(44, 54, edge_spacing=2, quiet=True).nest([make_part()])
        self.assertFalse(unplaced)
        self.assertInside(sheets[0].parts[0].shape, 44, 54, edge=2)
        self.assertAlmostEqual(part_edge_bounds(sheets[0].parts[0].shape)[0], 2)

    def test_parts_too_large_for_custom_margin_are_not_placed(self):
        sheets, unplaced = Nester(43, 54, edge_spacing=2, quiet=True).nest([make_part()])
        self.assertFalse(sheets)
        self.assertEqual(len(unplaced), 1)

    def test_asymmetric_rotated_outline_uses_buffered_pivot(self):
        outline = Polygon([(0, 0), (40, 0), (40, 10), (10, 10), (10, 50), (0, 50)])
        part = make_part(outline=outline, angle=37)
        rotated = material_polygon(part)
        x0, y0, x1, y1 = rotated.bounds
        width, height = x1 - x0 + 6, y1 - y0 + 6
        sheets, unplaced = Nester(width, height, edge_spacing=3, quiet=True).nest([part])
        self.assertFalse(unplaced)
        self.assertInside(sheets[0].parts[0].shape, width, height, edge=3)

    def test_physics_spawn_and_shaking_respect_material_edges(self):
        for edge in (0, 2):
            with self.subTest(edge=edge):
                state = random.getstate()
                try:
                    random.seed(42)
                    nester = PhysicsNester(40 + edge * 2, 50 + edge * 2, edge_spacing=edge,
                                           max_spawn_count=20, max_nesting_steps=2, anneal_steps=3)
                    sheets, unplaced = nester.nest([make_part()])
                finally:
                    random.setstate(state)
                self.assertFalse(unplaced)
                self.assertInside(sheets[0].parts[0].shape, 40 + edge * 2, 50 + edge * 2, edge)

    def test_fill_can_use_flush_edges_without_losing_part_spacing(self):
        count = [0]
        def spawn():
            count[0] += 1
            part = make_part(20, 20, 6, count[0])
            part.fill_sheet = True
            part.master_label = 'EdgeFill'
            part.spawn_next = spawn
            return part
        sheets, _ = Nester(46, 20, quiet=True).nest([spawn()])
        self.assertEqual(len(sheets[0].parts), 2)
        first, second = [p.shape for p in sheets[0].parts]
        self.assertAlmostEqual(material_polygon(first).distance(material_polygon(second)), 6, places=5)

    def test_catalog_and_standalone_snapshots_preserve_edge_clearance(self):
        part = make_part()
        layout = SimpleNamespace(parts=[part], genes=[], direction=(0, -1))
        catalog, payload = build_part_catalog([part])
        geometries = {key: shapely.from_wkb(wkb) for key, wkb in payload.items()}
        for enabled in (False, True):
            params = dict(sheet_width=44, sheet_height=54, rotation_steps=1,
                          custom_edge_spacing=enabled, edge_spacing=2)
            for compact in (False, True):
                with self.subTest(enabled=enabled, compact=compact):
                    task = snapshot_member(layout, params, 0, 0, 42, part_catalog=catalog if compact else None)
                    task = pickle.loads(pickle.dumps(task))
                    result = nest_from_snapshot(task, catalog, geometries)
                    self.assertFalse(result.unplaced_ids)
                    self.assertEqual(task.edge_spacing, 2 if enabled else 0)
                    restored = make_part()
                    _, _, x, y, angle = result.placements[0]
                    restored.set_rotation(angle, reposition=False)
                    center = restored.centroid
                    restored.move(x - center.x, y - center.y)
                    self.assertInside(restored, 44, 54, edge=2 if enabled else 0)

    def test_spawned_worker_keeps_unbuffered_geometry_and_margin(self):
        part = make_part()
        layout = SimpleNamespace(parts=[part], genes=[], direction=(0, -1))
        params = dict(sheet_width=44, sheet_height=54, custom_edge_spacing=True, edge_spacing=2)
        catalog, payload = build_part_catalog([part])
        task = snapshot_member(layout, params, 0, 0, 42, part_catalog=catalog)
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'),
                                 initializer=init_worker, initargs=({}, catalog, payload)) as pool:
            result = pool.submit(worker_nest, task).result(timeout=30)
        self.assertFalse(result.unplaced_ids)
        self.assertEqual(len(result.placements), 1)


@unittest.skipUnless(App.GuiUp, 'Document preparation requires FreeCAD GUI view providers.')
class EdgeSpacingDocumentTests(unittest.TestCase):
    def setUp(self):
        self.previous = App.ActiveDocument
        self.doc = App.newDocument('NestingEdgeSpacingTest')

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def test_reloaded_master_preserves_actual_outline_and_zero_edge_margin(self):
        from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
        source = self.doc.addObject('Part::Feature', 'Board')
        source.Shape = Part.makeBox(40, 50, 2)
        params = dict(spacing=12, deflection=0.05, simplification=0.01,
                      rotation_steps=1, add_labels=False, font_path='')
        quantities = {'Board': dict(quantity=1)}
        preparer = ShapePreparer(self.doc, {})
        def prepare(obj):
            layout = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Edge')
            group = self.doc.addObject('App::DocumentObjectGroup', 'PartsToPlace')
            layout.addObject(group)
            return preparer.prepare_parts(params, quantities, {obj.Label: obj}, layout, group)[0]
        first = prepare(source)
        master = next(c for c in first.master_container.Group if c.Label.startswith('master_shape_'))
        for part in (first, prepare(master)):
            self.assertIsNotNone(part.unbuffered_polygon)
            sheets, unplaced = Nester(40, 50, quiet=True).nest([part])
            self.assertFalse(unplaced)
            self.assertEqual(len(sheets), 1)

    def test_custom_edge_spacing_is_saved_with_the_layout(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob
        layout = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Edge')
        params = dict(sheet_width=100, sheet_height=80, spacing=12, sheet_thickness=2,
                      custom_edge_spacing=True, edge_spacing=3.5, font_path='',
                      show_bounds=False, add_labels=False, label_height=0, label_size=10,
                      rotation_steps=4)
        job = NestingJob.__new__(NestingJob)
        job.params = params
        job._apply_properties(layout)
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as directory:
            self.assertTrue(os.path.realpath(directory).startswith(
                os.path.realpath(os.path.dirname(__file__)) + os.sep))
            path = os.path.join(directory, 'Edge.FCStd')
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            layout = self.doc.getObject('Layout_Edge')
            self.assertTrue(layout.CustomEdgeSpacing)
            self.assertAlmostEqual(float(layout.EdgeSpacing), 3.5)


if __name__ == '__main__':
    unittest.main()
