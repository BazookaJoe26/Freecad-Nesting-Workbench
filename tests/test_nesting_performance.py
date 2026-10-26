# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run with FreeCAD's bundled Python; GUI cases also run inside FreeCAD."""
from dataclasses import replace
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import pickle
import random
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import FreeCAD as App
import shapely
from shapely.affinity import rotate
from shapely.geometry import Polygon, box

from freecad.nestingworkbench.Tools.Nesting.algorithms.rotation_cache import RotationCache
from freecad.nestingworkbench.Tools.Nesting.ga_snapshot import (
    PartSnapshot, SnapshotShape, build_part_catalog, snapshot_member, nest_from_snapshot,
)
from freecad.nestingworkbench.Tools.Nesting.ga_worker import init_worker, worker_nest
from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator, enumerate_nfp_jobs
from freecad.nestingworkbench.Tools.Nesting.layout_manager import Layout, LayoutManager
from freecad.nestingworkbench.Tools.Nesting.cancellation import NestingCancelled


def make_parts():
    polygons = [box(0, 0, 20, 12),
                Polygon([(0, 0), (18, 0), (18, 6), (7, 6), (7, 20), (0, 20)]),
                Polygon([(0, 0), (38, 0), (38, 38), (0, 38)],
                        [[(8, 8), (8, 30), (30, 30), (30, 8)]])]
    parts = []
    for i, poly in enumerate(polygons):
        snap = PartSnapshot(f'part_{i}_0', f'type_{i}', shapely.to_wkb(poly),
                            None, 4, 0.0, 0.05, 0.1, False)
        first = SnapshotShape(snap)
        parts.append(first)
        for j in range(1, 3):
            part = SnapshotShape(replace(snap, part_id=f'part_{i}_{j}'))
            part.original_polygon = first.original_polygon
            part.polygon = first.original_polygon
            parts.append(part)
    return parts


PARAMS = dict(sheet_width=65.0, sheet_height=55.0, spacing=0.0,
              rotation_steps=4, compactness_weight=0.3, algorithm='Minkowski',
              add_labels=False, font_path='', show_bounds=False, sheet_thickness=2,
              deflection=0.05, simplification=0.1, label_height=0.0, label_size=3.0)


class GeometryPerformanceTests(unittest.TestCase):
    def assertResultEqual(self, actual, expected):
        self.assertEqual(replace(actual, elapsed=0), replace(expected, elapsed=0))

    def test_rotation_cache_reuses_only_identical_geometry_and_angle(self):
        cache = RotationCache(max_entries=2)
        polygon = make_parts()[1].original_polygon
        with patch(RotationCache.__module__ + '.rotate', wraps=rotate) as rotate_call:
            data = cache.get(polygon, 37.12345)
            self.assertIs(data, cache.get(polygon, 37.12345))
            self.assertEqual(rotate_call.call_count, 1)
            expected = rotate(polygon, 37.12345, origin='centroid')
            self.assertTrue(data.polygon.equals_exact(expected, 0))
            center = expected.centroid
            x0, y0, x1, y1 = expected.bounds
            self.assertEqual(data.extents, (x0-center.x, y0-center.y, x1-center.x, y1-center.y))
            cache.get(polygon, 37.12346)
            cache.get(box(0, 0, 7, 9), 37.12345)
            cache.get(polygon, 37.12345)
            self.assertEqual(rotate_call.call_count, 4)  # bounded eviction, no rounded-angle alias
            self.assertEqual(len(cache._entries), 2)

    def test_compact_tasks_match_standalone_and_share_geometry(self):
        parts = make_parts()
        catalog, payload = build_part_catalog(parts)
        self.assertEqual(len(payload), 3)
        geometries = {key: shapely.from_wkb(wkb) for key, wkb in payload.items()}
        cache = RotationCache()
        first, second = [SnapshotShape(catalog[p.id], geometries, cache) for p in parts[:2]]
        self.assertIs(first.original_polygon, second.original_polygon)
        for direction in [(0, -1), (-1, 0), None]:
            for pinned in [None, 0.0, 90.0]:
                for part in parts:
                    if pinned is None:
                        part.__dict__.pop('gene_angle', None)
                    else:
                        part.gene_angle = pinned
                layout = SimpleNamespace(parts=parts, genes=[] if pinned is None else [('set', pinned)],
                                         direction=direction)
                full = snapshot_member(layout, PARAMS, 0, 1, 42, search_direction=direction)
                with patch('shapely.to_wkb', side_effect=AssertionError('per-member serialization')):
                    compact = snapshot_member(layout, PARAMS, 0, 1, 42,
                                              search_direction=direction, part_catalog=catalog)
                expected = nest_from_snapshot(full)
                with patch('shapely.from_wkb', side_effect=AssertionError('per-member decoding')):
                    actual = nest_from_snapshot(compact, catalog, geometries, cache)
                self.assertResultEqual(actual, expected)

    def test_compact_task_size_does_not_scale_with_polygon_vertices(self):
        poly = shapely.Point(0, 0).buffer(10, quad_segs=128)
        snap = PartSnapshot('round', 'round', shapely.to_wkb(poly), None, 4, 0, 0.05, 0.1, False)
        part = SnapshotShape(snap)
        layout = SimpleNamespace(parts=[part], genes=[], direction=None)
        catalog, _ = build_part_catalog([part])
        full = snapshot_member(layout, PARAMS, 0, 0, 1)
        compact = snapshot_member(layout, PARAMS, 0, 0, 1, part_catalog=catalog)
        self.assertLess(len(pickle.dumps(compact)), len(pickle.dumps(full)) / 10)

    def test_spawned_workers_match_serial_catalog_results(self):
        parts = make_parts()
        layout = SimpleNamespace(parts=parts, genes=[], direction=(0, -1))
        catalog, payload = build_part_catalog(parts)
        task = snapshot_member(layout, PARAMS, 1, 2, 1234, part_catalog=catalog)
        geometries = {key: shapely.from_wkb(wkb) for key, wkb in payload.items()}
        expected = nest_from_snapshot(task, catalog, geometries)
        with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn'),
                                 initializer=init_worker, initargs=({}, catalog, payload)) as pool:
            futures = [pool.submit(worker_nest, task) for _ in range(3)]
            for future in futures:
                self.assertResultEqual(future.result(timeout=30), expected)

    def test_parallel_generation_and_serial_fallback_keep_same_winner(self):
        winners = []
        for parallel in (False, True):
            manager = LayoutManager(None, rng=random.Random(123), geometry_only=True)
            seed = Layout(None, None, make_parts())
            with patch.object(manager, '_create_document_layout', return_value=seed):
                layouts = manager.create_ga_population({}, {}, PARAMS, 4, 4)
            coordinator = GACoordinator(None, SimpleNamespace(processed_shape_cache={}))
            coordinator.layout_manager = manager
            coordinator.rng = manager.rng
            coordinator.seed = 87
            pool = None
            if parallel:
                pool = ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn'),
                                           initializer=init_worker,
                                           initargs=({}, manager.part_catalog, manager.geometry_payload))
            try:
                for gen in range(3):
                    coordinator._run_generation(layouts, gen, 3, PARAMS, 4, {},
                                                False, lambda: False, False, pool=pool)
                    layouts.sort(key=lambda item: item.fitness)
                    if gen < 2:
                        layouts = coordinator._build_next_generation(
                            gen, layouts, layouts[:2], {}, {}, PARAMS, 4, 0.1, 0.15, False)
                winners.append(layouts[0].result)
            finally:
                if pool is not None:
                    pool.shutdown()
        self.assertResultEqual(winners[0], winners[1])

    def test_candidates_and_generations_create_only_one_document_template(self):
        seed = Layout(None, None, make_parts())
        manager = LayoutManager(None, rng=random.Random(123), geometry_only=True)
        with patch.object(manager, '_create_document_layout', return_value=seed) as create:
            layouts = manager.create_ga_population({}, {}, PARAMS, 5, 4)
            coordinator = GACoordinator(None, SimpleNamespace(processed_shape_cache={}))
            coordinator.layout_manager = manager
            coordinator.seed = 99
            coordinator.rng = manager.rng
            for gen in range(3):
                _, cancelled = coordinator._run_generation(layouts, gen, 3, PARAMS, 4, {},
                                                            False, lambda: False, False)
                self.assertFalse(cancelled)
                layouts.sort(key=lambda item: item.fitness)
                if gen < 2:
                    layouts = coordinator._build_next_generation(
                        gen, layouts, layouts[:2], {}, {}, PARAMS, 4, 0.1, 0.15, False)
            self.assertEqual(create.call_count, 1)
            self.assertTrue(all(item.layout_group is None for item in layouts))
            winner = min(layouts, key=lambda item: item.fitness)
            result = winner.result
            materialized = manager.materialize_layout(winner, PARAMS)
            self.assertIs(materialized, seed)
            self.assertEqual(materialized.fitness, result.fitness)
            self.assertEqual(sum(len(s.parts) for s in materialized.sheets), len(result.placements))
            by_id = {p.shape.id: p for s in materialized.sheets for p in s.parts}
            for part_id, sheet_idx, x, y, angle in result.placements:
                self.assertAlmostEqual(by_id[part_id].x, x)
                self.assertAlmostEqual(by_id[part_id].y, y)
                self.assertEqual(by_id[part_id].angle, angle)

    def test_serial_fallback_observes_cancel_during_member(self):
        parts = make_parts()
        layout = SimpleNamespace(parts=parts, genes=[], direction=(0, -1))
        task = snapshot_member(layout, PARAMS, 0, 0, 1)
        calls = []
        def cancel():
            calls.append(True)
            return len(calls) > 2
        with self.assertRaises(NestingCancelled):
            nest_from_snapshot(task, cancel_callback=cancel)
        self.assertGreater(len(calls), 2)


@unittest.skipUnless(App.GuiUp, 'requires FreeCAD GUI document view providers')
class DocumentPerformanceTests(unittest.TestCase):
    def setUp(self):
        import Part
        self.previous = App.ActiveDocument
        self.doc = App.newDocument('NestingPerformanceTest')
        self.source = self.doc.addObject('Part::Feature', 'PerfSource')
        self.source.Shape = Part.makeBox(20, 12, 2)
        self.doc.recompute()
        self.quantities = {self.source.Label: dict(quantity=5, rotation_steps=4)}
        self.masters = {self.source.Label: self.source}

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def test_document_count_is_constant_across_generations_and_winner_draws(self):
        coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
        counts = []
        original = coordinator._run_generation
        def evaluate(*args, **kwargs):
            counts.append(len(self.doc.Objects))
            return original(*args, **kwargs)
        with patch.object(coordinator, '_run_generation', side_effect=evaluate), \
             patch('os.cpu_count', return_value=1):
            job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                                  dict(generations=3, population_size=4, random_seed=99), False)
        self.assertIsNotNone(job)
        self.assertEqual(len(counts), 3)
        self.assertEqual(len(set(counts)), 1)
        self.assertEqual(sum(len(sheet.parts) for sheet in job.sheets), 5)
        self.assertEqual(len([o for o in self.doc.Objects if o.Name.startswith('Layout_GA')]), 1)
        job.cleanup()
        self.assertEqual([o.Name for o in self.doc.Objects], [self.source.Name])

    def test_fill_only_and_preparation_cancel_clean_up(self):
        coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
        quantities = {self.source.Label: dict(quantity=1, rotation_steps=4, fill_sheet=True)}
        with patch('os.cpu_count', return_value=1):
            job = coordinator.run(None, PARAMS, quantities, self.masters, {},
                                  dict(generations=2, population_size=3, random_seed=77), False)
        self.assertIsNotNone(job)
        self.assertGreater(sum(len(sheet.parts) for sheet in job.sheets), 1)
        job.cleanup()
        with patch.object(coordinator, '_precompute_all_nfps', side_effect=RuntimeError('test failure')):
            job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                                  dict(generations=2, population_size=3), False)
        self.assertIsNone(job)
        self.assertEqual([o.Name for o in self.doc.Objects], [self.source.Name])

    def test_memory_and_document_candidates_produce_same_generations(self):
        outcomes, measurements = [], []
        for memory_only in (False, True):
            manager = LayoutManager(self.doc, rng=random.Random(77), geometry_only=memory_only)
            coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
            coordinator.layout_manager = manager
            coordinator.rng = manager.rng
            coordinator.seed = 77
            start = time.perf_counter()
            layouts = manager.create_ga_population(self.masters, self.quantities, PARAMS, 5, 4)
            object_count = len(self.doc.Objects)
            generation_results = []
            for gen in range(3):
                coordinator._run_generation(layouts, gen, 3, PARAMS, 4, {}, False, lambda: False, False)
                layouts.sort(key=lambda item: item.fitness)
                generation_results.append([(l.fitness, l.efficiency, l.genes) for l in layouts])
                if gen < 2:
                    layouts = coordinator._build_next_generation(
                        gen, layouts, layouts[:2], self.masters, self.quantities, PARAMS,
                        4, 0.1, 0.15, False)
            outcomes.append(generation_results)
            measurements.append((time.perf_counter()-start, object_count))
            for layout in layouts:
                manager.delete_layout(layout)
            if manager.document_layout:
                manager.delete_layout(manager.document_layout)
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertLess(measurements[1][1], measurements[0][1])
        print('GA comparison (3 generations, population 5): document / memory seconds and objects:', measurements)

    def test_worker_start_failure_filters_cache_and_falls_back(self):
        from freecad.nestingworkbench.datatypes.shape import Shape
        module = GACoordinator.__module__
        stale_key = ('unrelated_test_shape', 'unused', 0, 0, 0, 0)
        Shape.nfp_cache[stale_key] = {'error': 'not part of this run'}
        coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
        try:
            with patch('os.cpu_count', return_value=2), \
                 patch(module + '.start_worker_pool', side_effect=RuntimeError('test startup failure')) as start:
                job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                                      dict(generations=2, population_size=3, random_seed=99), False)
            self.assertIsNotNone(job)
            self.assertTrue(coordinator._serial_fallback)
            sent_cache = start.call_args.args[1]
            self.assertNotIn(stale_key, sent_cache)
            self.assertTrue(sent_cache)
            self.assertIs(start.call_args.args[3], coordinator.layout_manager.part_catalog)
            job.cleanup()
        finally:
            Shape.nfp_cache.pop(stale_key, None)

    def test_cancel_before_first_generation_removes_workspace(self):
        coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
        job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                              dict(generations=2, population_size=3, cancel_callback=lambda: True), False)
        self.assertIsNone(job)
        self.assertEqual([o.Name for o in self.doc.Objects], [self.source.Name])

    def test_main_thread_draw_dispatch_materializes_the_winner(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
        coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))
        proxy = SimpleNamespace(_worker=SimpleNamespace(
            coordinator=coordinator, notify_draw_complete=lambda: None))
        coordinator.draw_callback = lambda payload: NestingController._handle_draw_request(proxy, payload)
        with patch('os.cpu_count', return_value=1):
            job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                                  dict(generations=2, population_size=3, random_seed=99), False)
        self.assertIsNotNone(job)
        self.assertEqual(sum(len(sheet.parts) for sheet in job.sheets), 5)
        job.cleanup()

    def test_single_pass_preserves_progress_and_simulation_uses_document_layouts(self):
        from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
        progress = []
        coordinator = GACoordinator(self.doc, ShapePreparer(self.doc, {}))
        job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                              dict(generations=1, population_size=1, random_seed=99,
                                   progress_callback=lambda *args: progress.append(args)), False)
        self.assertIsNotNone(job)
        self.assertEqual(len(progress), 5)
        job.cleanup()
        job = coordinator.run(None, PARAMS, self.quantities, self.masters, {},
                              dict(generations=1, population_size=1, random_seed=99), True)
        self.assertIsNotNone(job)
        self.assertFalse(coordinator.layout_manager.geometry_only)
        self.assertEqual(sum(len(sheet.parts) for sheet in job.sheets), 5)
        job.cleanup()


if __name__ == '__main__':
    unittest.main()
