# SPDX-License-Identifier: LGPL-2.1-or-later
"""Cancellation regressions; run with FreeCAD's Python or inside its GUI."""
from concurrent.futures import Future
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import FreeCAD as App
import shapely
from shapely.geometry import box

from freecad.nestingworkbench.Tools.Nesting.cancellation import NestingCancelled, check_cancel
from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator, wait_for_any
from freecad.nestingworkbench.Tools.Nesting.ga_snapshot import PartSnapshot, SnapshotShape
from freecad.nestingworkbench.Tools.Nesting.algorithms.minkowski_engine import MinkowskiEngine, compute_and_cache_nfp
from freecad.nestingworkbench.Tools.Nesting.algorithms.nesting_strategy import PlacementOptimizer
from freecad.nestingworkbench.datatypes.shape import Shape


class CancellationTests(unittest.TestCase):
    def test_nfp_precomputation_cancels_without_draining_all_jobs(self):
        stop = threading.Event()
        started, active = [], []
        lock = threading.Lock()
        coordinator = GACoordinator(None, SimpleNamespace(processed_shape_cache={}))
        jobs = {('cancel_test', i): (None, None, 0) for i in range(100)}

        def compute(*args, cancel_callback=None, **kwargs):
            with lock:
                started.append(args[4])
                active.append(args[4])
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    check_cancel(cancel_callback)
                    stop.wait(0.005)
                self.fail('NFP did not receive cancellation')
            finally:
                with lock:
                    active.remove(args[4])

        module = GACoordinator.__module__
        engine_module = compute_and_cache_nfp.__module__
        timer = threading.Timer(0.05, stop.set)
        timer.start()
        start = time.perf_counter()
        try:
            with patch(module + '.enumerate_nfp_jobs', return_value=jobs), \
                 patch(engine_module + '.compute_and_cache_nfp', side_effect=compute):
                with self.assertRaises(NestingCancelled):
                    coordinator._precompute_all_nfps([], stop.is_set)
        finally:
            stop.set()
            timer.join()
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertGreater(len(started), 0)
        self.assertLessEqual(len(started), 2)
        self.assertFalse(active, 'No cache-writing thread may survive the run')

    def test_cancelled_nfp_is_not_cached_as_a_geometry_error(self):
        part = SimpleNamespace(original_polygon=box(0, 0, 10, 10))
        key = ('cancel_nfp', 'cancel_nfp', 0, 0, 0, 0)
        log = Mock()
        module = compute_and_cache_nfp.__module__
        try:
            with patch(module + '.minkowski_utils.minkowski_sum', side_effect=NestingCancelled):
                with self.assertRaises(NestingCancelled):
                    compute_and_cache_nfp(part, 0, part, 0, key, log=log)
            self.assertNotIn(key, Shape.nfp_cache)
            log.assert_not_called()
            self.assertIn('polygon', compute_and_cache_nfp(part, 0, part, 0, key, log=log))
        finally:
            Shape.nfp_cache.pop(key, None)

    def test_rotation_sweep_stops_before_next_angle_or_placement(self):
        stop = threading.Event()
        engine = MinkowskiEngine(100, 100, 5, cancel_callback=stop.is_set)
        optimizer = PlacementOptimizer(engine, 4, (0, -1))
        part = SnapshotShape(PartSnapshot('cancel_part', 'cancel_part', shapely.to_wkb(box(0, 0, 10, 8)),
                                          None, 4, 0, 0.05, 0.1, False))
        original = part.polygon
        def evaluate(*args):
            stop.set()
            return dict(metric=0, x=20, y=20, angle=0)
        with patch.object(optimizer, '_evaluate_rotation', side_effect=evaluate) as evaluate_call:
            with self.assertRaises(NestingCancelled):
                optimizer.find_best_placement(part, SimpleNamespace(parts=[]))
        self.assertEqual(evaluate_call.call_count, 1)
        self.assertIs(part.polygon, original)

    def test_cancel_takes_priority_over_completed_worker(self):
        finished = Future()
        finished.set_result(True)
        self.assertIsNone(wait_for_any({finished}, lambda: True))

    def test_cancel_button_does_not_acknowledge_a_pending_document_request(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
        notify = Mock()
        controller = SimpleNamespace(is_running=True, cancel_requested=False, ui=Mock(),
                                     _worker=SimpleNamespace(notify_draw_complete=notify))
        NestingController.request_cancel(controller)
        self.assertTrue(controller.cancel_requested)
        notify.assert_not_called()

    def test_cancelled_document_request_reports_cancel_but_cleanup_is_allowed(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
        notify = Mock()
        manager = Mock()
        controller = SimpleNamespace(cancel_requested=True,
                                     _worker=SimpleNamespace(notify_draw_complete=notify,
                                         coordinator=SimpleNamespace(layout_manager=manager)))
        payload = {'create_population': True}
        NestingController._handle_draw_request(controller, payload)
        self.assertIsInstance(payload['_error'], NestingCancelled)
        manager.create_ga_population.assert_not_called()
        layout = object()
        payload = dict(cleanup_layouts=True, layouts=[layout], best_layout=None)
        NestingController._handle_draw_request(controller, payload)
        self.assertNotIn('_error', payload)
        manager.delete_layout.assert_called_once_with(layout, verbose=False)
        self.assertEqual(notify.call_count, 2)

    def test_standard_panel_cancel_requests_worker_abort_before_closing(self):
        from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
        controller = Mock(is_running=True)
        form = SimpleNamespace(controller=controller)
        self.assertFalse(NestingPanel.reject(form))
        controller.request_cancel.assert_called_once()
        controller.cancel_job.assert_not_called()
        controller.on_panel_closed.assert_not_called()

    def test_task_panel_waits_asynchronously_for_worker_before_close(self):
        from freecad.nestingworkbench.task_panel_manager import NestingTaskPanel
        panel = NestingTaskPanel.__new__(NestingTaskPanel)
        panel.form = SimpleNamespace(reject=Mock(side_effect=[False, True]))
        panel.cleanup = Mock()
        callbacks = []
        module = NestingTaskPanel.__module__
        close = Mock()
        fake_qt = SimpleNamespace(QTimer=SimpleNamespace(singleShot=lambda ms, fn: callbacks.append(fn)))
        fake_gui = SimpleNamespace(Control=SimpleNamespace(closeDialog=close))
        with patch(module + '.QtCore', new=fake_qt), patch(module + '.FreeCADGui', new=fake_gui):
            self.assertFalse(panel.reject())
            panel.cleanup.assert_not_called()
            close.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks.pop()()
            panel.cleanup.assert_called_once()
            close.assert_called_once()


@unittest.skipUnless(App.GuiUp, 'requires FreeCAD GUI document view providers')
class CancellationDocumentTests(unittest.TestCase):
    def setUp(self):
        import Part
        self.previous = App.ActiveDocument
        self.doc = App.newDocument('NestingCancellationTest')
        self.source = self.doc.addObject('Part::Feature', 'CancelSource')
        self.source.Shape = Part.makeBox(20, 12, 2)
        self.doc.recompute()
        self.params = dict(sheet_width=65, sheet_height=55, spacing=0, rotation_steps=4,
                           algorithm='Minkowski', add_labels=False, font_path='', show_bounds=False,
                           sheet_thickness=2, deflection=0.05, simplification=0.1)
        self.stop = threading.Event()
        self.coordinator = GACoordinator(self.doc, SimpleNamespace(processed_shape_cache={}))

    def tearDown(self):
        App.closeDocument(self.doc.Name)
        if self.previous:
            App.setActiveDocument(self.previous.Name)

    def assertOnlySourceRemains(self):
        self.assertEqual([obj.Name for obj in self.doc.Objects], [self.source.Name])

    def run_job(self, count=5):
        with patch('os.cpu_count', return_value=1):
            return self.coordinator.run(None, self.params,
                {self.source.Label: dict(quantity=count, rotation_steps=4)},
                {self.source.Label: self.source}, {},
                dict(generations=3, population_size=3, random_seed=99,
                     cancel_callback=self.stop.is_set), False)

    def test_cancel_after_a_generation_skips_materialization_and_final_drawing(self):
        evaluate = self.coordinator._run_generation
        def abort_after_generation(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            self.stop.set()
            return result
        with patch.object(self.coordinator, '_run_generation', side_effect=abort_after_generation), \
             patch.object(self.coordinator, '_dispatch_finalize') as finalize:
            self.assertIsNone(self.run_job())
        finalize.assert_not_called()
        self.assertOnlySourceRemains()

    def test_cancel_click_is_processed_while_preparing_many_parts(self):
        from PySide import QtCore
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self.stop.set)
        timer.start(25)
        start = time.perf_counter()
        try:
            self.assertIsNone(self.run_job(count=5000))
        finally:
            timer.stop()
        self.assertTrue(self.stop.is_set())
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2)
        print(f'Cancel during preparation: {elapsed:.3f}s including cleanup')
        self.assertOnlySourceRemains()

    def test_cancel_during_final_drawing_cleans_up_partial_result(self):
        from freecad.nestingworkbench.datatypes.sheet import Sheet
        draw = Sheet.draw
        def abort_during_draw(sheet, *args, **kwargs):
            self.stop.set()
            return draw(sheet, *args, **kwargs)
        with patch.object(Sheet, 'draw', abort_during_draw):
            self.assertIsNone(self.run_job())
        self.assertOnlySourceRemains()

    def test_real_background_worker_acknowledges_cancel_and_cleans_up(self):
        from PySide import QtCore
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController, NestingWorker
        args = (None, self.params,
                {self.source.Label: dict(quantity=5000, rotation_steps=4)},
                {self.source.Label: self.source}, {},
                dict(generations=3, population_size=3, cancel_callback=self.stop.is_set), False, None)
        worker = NestingWorker(self.coordinator, args, self.stop.is_set)
        controller = SimpleNamespace(is_running=True, cancel_requested=False, ui=Mock(), _worker=worker)
        self.coordinator.worker = worker
        self.coordinator.draw_callback = worker.request_draw_on_main_thread
        worker.draw_requested.connect(lambda payload: NestingController._handle_draw_request(controller, payload))
        loop = QtCore.QEventLoop()
        results, errors, cancelled_at = [], [], []
        def finished(result):
            results.append(result)
            loop.quit()
        def failed(error):
            errors.append(error)
            loop.quit()
        def cancel():
            cancelled_at.append(time.perf_counter())
            self.stop.set()
            NestingController.request_cancel(controller)
        worker.finished_signal.connect(finished)
        worker.error_signal.connect(failed)
        cancel_timer, deadline = QtCore.QTimer(), QtCore.QTimer()
        cancel_timer.setSingleShot(True)
        deadline.setSingleShot(True)
        cancel_timer.timeout.connect(cancel)
        deadline.timeout.connect(loop.quit)
        try:
            with patch('os.cpu_count', return_value=1):
                worker.start()
                cancel_timer.start(25)
                deadline.start(5000)
                loop.exec_()
                # Allow an acknowledged worker to finish unwinding its run().
                self.assertTrue(worker.wait(1000), 'Background worker did not stop')
            self.assertFalse(errors, errors)
            self.assertEqual(results, [None])
            latency = time.perf_counter() - cancelled_at[0]
            self.assertLess(latency, 1.0)
            print(f'Background-worker cancel latency: {latency:.3f}s including cleanup')
            self.assertOnlySourceRemains()
        finally:
            cancel_timer.stop()
            deadline.stop()
            self.stop.set()
            self.coordinator.worker = None
            self.coordinator.draw_callback = None
            worker.coordinator = None

    def test_cancel_without_a_result_restores_existing_target_layout(self):
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
        target = self.doc.addObject('App::DocumentObjectGroup', 'Layout_Existing')
        sheet = self.doc.addObject('App::DocumentObjectGroup', 'Sheet_Existing')
        target.addObject(sheet)
        target.ViewObject.Visibility = False
        sheet.ViewObject.Visibility = False
        controller = NestingController.__new__(NestingController)
        controller.doc = self.doc
        controller.ui = Mock()
        controller.current_job = None
        controller.cancel_requested = True
        controller.is_running = True
        controller._worker = SimpleNamespace(run_args=(target,))
        controller._retire_worker = Mock()
        controller._on_nesting_finished(None)
        self.assertTrue(target.ViewObject.Visibility)
        self.assertTrue(sheet.ViewObject.Visibility)
        self.assertFalse(controller.is_running)
        controller._retire_worker.assert_called_once()


if __name__ == '__main__':
    unittest.main()
