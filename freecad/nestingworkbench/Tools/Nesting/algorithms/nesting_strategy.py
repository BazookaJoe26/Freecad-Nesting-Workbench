# SPDX-License-Identifier: LGPL-2.1-or-later

import math
import random
import copy
from datetime import datetime
import numpy as np

try:
    import FreeCAD
except ImportError:
    FreeCAD = None
from ....datatypes.sheet import Sheet
from ....datatypes.placed_part import PlacedPart
from ....rotation_constraints import placement_angles
from ....edge_spacing import part_edge_extents
from . import genetic_utils
from .minkowski_engine import MinkowskiEngine
from .rotation_cache import RotationCache
from ..cancellation import NestingCancelled, check_cancel

class PlacementOptimizer:
    """
    Handles the geometric logic of finding the best position for a part on a sheet.
    """
    def __init__(self, engine, rotation_steps, search_direction, log_callback=None, trial_callback=None, rng=None, rotation_cache=None):
        self.engine = engine
        self.rotation_steps = max(1, rotation_steps)
        self.search_direction = search_direction
        self.log_callback = log_callback
        # Called for each trial placement in simulation mode, as
        # (part, angle, x, y, sheet). x/y are sheet-local, so the sheet is
        # needed to draw the preview on the right sheet in a multi-sheet run.
        self.trial_callback = trial_callback
        self.rng = rng or random  # Seeded random.Random for reproducible runs, or the global module
        self.verbose = False
        self.rotation_cache = rotation_cache if rotation_cache is not None else RotationCache()

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def find_best_placement(self, part, sheet):
        """
        Evaluate permitted rotations to find the best spot.
        """
        check_cancel(self.engine.cancel_callback)
        if part.original_polygon is None and part.polygon is not None:
            part.original_polygon = part.polygon
            
        direction = self.search_direction
        if direction is None:
             angle_rad = self.rng.uniform(0, 2 * math.pi)
             direction = (math.cos(angle_rad), math.sin(angle_rad))

        best_result = {'metric': float('inf')}
        
        angles = placement_angles(part, self.rotation_steps)
        
        # Drop rotations this sheet has already proven impossible at its current
        # occupancy — placed parts have not moved since, so the verdict stands.
        # Scoped to `len(sheet.parts)`, never permanent: see
        # MinkowskiEngine.is_known_infeasible.
        n_angles_before = len(angles)
        angles = [a for a in angles if not self.engine.is_known_infeasible(part, a, sheet)]
        self.engine.count_skipped_rotations(n_angles_before - len(angles))
        if not angles:
            return None

        # Serial evaluation across rotations. Measured against a per-placement
        # ThreadPoolExecutor, serial evaluation is 2.4-3.3x faster due to
        # eliminating thread pool creation/teardown overhead and GIL contention.
        import time as _time
        t0_eval = _time.perf_counter()
        total_nfp_ms = 0.0
        total_score_ms = 0.0

        for angle in angles:
            check_cancel(self.engine.cancel_callback)
            try:
                res = self._evaluate_rotation(angle, part, sheet, direction)
                if res:
                    total_nfp_ms += res.get('_t_nfp_ms', 0)
                    total_score_ms += res.get('_t_score_ms', 0)
                    if res['metric'] < best_result['metric']:
                        best_result = res
                        if self.trial_callback and best_result.get('x') is not None:
                            self.trial_callback(part, best_result['angle'], best_result['x'], best_result['y'], sheet)
            except NestingCancelled:
                raise
            except Exception as e:
                self.log(f"Error in rotation evaluation: {e}")

        dt_eval = (_time.perf_counter() - t0_eval) * 1000
        self.log(f"[TIMING] '{getattr(part, 'id', '?')}': wall={dt_eval:.0f}ms "
                 f"nfp={total_nfp_ms:.0f}ms score={total_score_ms:.0f}ms "
                 f"({len(angles)} rotations, {len(sheet.parts)} placed)")
        if self.verbose:
            self.log(f"  -> Rotation eval: {len(angles)} rotations in {dt_eval:.1f}ms")
        best_result['_t_nfp_ms'] = total_nfp_ms
        best_result['_t_score_ms'] = total_score_ms

        if self.verbose:
            self.log(f"  -> Best result for {part.id}: {best_result}")



        check_cancel(self.engine.cancel_callback)
        if best_result.get('x') is not None:
             part.set_rotation(best_result['angle'], reposition=False)
             curr = part.centroid
             part.move(best_result['x'] - curr.x, best_result['y'] - curr.y)
             return part
        return None

    def _evaluate_rotation(self, angle, part, sheet, direction):
        """Evaluate placing *part* at *angle* on *sheet*.

        Scoring is a single gravity-direction projection (MinkowskiEngine.score_gravity),
        not a weighted multi-objective score. Returns the best candidate as a dict with
        'metric' set to inf when no candidate fits.
        """
        import time as _time, threading
        t0 = _time.perf_counter()
        thread_id = threading.current_thread().name

        if not part.original_polygon: return {'metric': float('inf')}

        # Candidate positions are centroid positions — express the rotated
        # bounds relative to the centroid for corner seeds and bounds checks.
        extents = part_edge_extents(part, angle, self.rotation_cache)
        w_bin, h_bin = self.engine.bin_width, self.engine.bin_height
        edge = sheet.edge_spacing
        if extents[2] - extents[0] > w_bin - 2 * edge + 1e-7 or extents[3] - extents[1] > h_bin - 2 * edge + 1e-7:
            return {'metric': float('inf')}
        corners = np.array([
            [edge - extents[0],         edge - extents[1]        ],
            [w_bin - edge - extents[2], edge - extents[1]        ],
            [edge - extents[0],         h_bin - edge - extents[3]],
            [w_bin - edge - extents[2], h_bin - edge - extents[3]],
        ], dtype=np.float64)

        pts_arr = self.engine.get_incremental_candidates(part, angle, sheet, corners, extents)
        t_nfp = _time.perf_counter()

        best = {'metric': float('inf')}
        if pts_arr is not None and len(pts_arr):
            valid_mask = np.ones(len(pts_arr), dtype=bool)
            best_idx, metric = MinkowskiEngine.score_gravity(pts_arr, valid_mask, direction, rng=self.rng)
            if best_idx is not None:
                best = {'x': float(pts_arr[best_idx, 0]), 'y': float(pts_arr[best_idx, 1]),
                        'angle': angle, 'metric': metric}

        # Notify better result found
        if self.trial_callback and best.get('x') is not None:
             self.trial_callback(part, angle, best['x'], best['y'], sheet)

        t_end = _time.perf_counter()
        if self.verbose:
            self.log(f"    [{thread_id}] angle={angle:.0f}: NFP={((t_nfp-t0)*1000):.1f}ms, "
                     f"score={((t_end-t_nfp)*1000):.1f}ms, total={((t_end-t0)*1000):.1f}ms")

        best['_t_nfp_ms'] = (t_nfp - t0) * 1000
        best['_t_score_ms'] = (t_end - t_nfp) * 1000
        return best

class Nester:
    """
    The main nesting algorithm class. 
    It orchestrates the nesting process using PlacementOptimizer and MinkowskiEngine.
    """
    def __init__(self, width, height, rotation_steps=1, **kwargs):
        self.bin_width = width
        self.bin_height = height
        self.spacing = kwargs.get("spacing", 0)
        self.edge_spacing = max(0.0, float(kwargs.get('edge_spacing', 0.0)))
        self.search_direction = kwargs.get("search_direction", (0, -1)) # Default Down
        
        # Logging control
        self.quiet = kwargs.get("quiet", False)  # If True, suppress per-part logs
        self.verbose = kwargs.get("verbose", False)  # If True, enable extra detailed logs
        self.log_callback = kwargs.get("log_callback")
        self.trial_callback = kwargs.get("trial_callback")  # For visualizing trial placements
        self.part_start_callback = kwargs.get("part_start_callback")  # Called when starting to place a part
        self.part_end_callback = kwargs.get("part_end_callback")  # Called after part is placed
        self.progress_callback = kwargs.get("progress_callback") # Called with (current, total)
        self.cancel_callback = kwargs.get("cancel_callback") # Called to check if nesting should abort
        self.spawn_more_callback = kwargs.get("spawn_more_callback")  # Mints fill-part instances on the main thread
        
        step_size = kwargs.get("step_size", 5.0) 
        self.engine = MinkowskiEngine(width, height, step_size, log_callback=self.log_callback, verbose=self.verbose,
                                      search_direction=self.search_direction, rng=kwargs.get("rng"),
                                      cancel_callback=self.cancel_callback)
        # quiet (multi-layout GA) silences the optimizer's per-placement [TIMING] lines
        self.optimizer = PlacementOptimizer(self.engine, rotation_steps, self.search_direction,
                                            None if self.quiet else self.log_callback,
                                            self.trial_callback, rng=kwargs.get("rng"),
                                            rotation_cache=kwargs.get("rotation_cache"))
        self.optimizer.verbose = self.verbose

        self.parts_to_place = []
        self.sheets = []
        self.update_callback = None # Can be set externally


    def log(self, message, level="message"):
        if self.log_callback:
            self.log_callback(message)
        elif FreeCAD and hasattr(FreeCAD, 'Console'):
            if level == "warning":
                FreeCAD.Console.PrintWarning(f"NESTER: {message}\n")
            else:
                FreeCAD.Console.PrintMessage(f"NESTER: {message}\n")

    def nest(self, parts, sort=True):
        """
        Main entry point for nesting.

        NOTE: GA optimization is now handled at the controller level using LayoutManager.
        This method just runs standard greedy nesting.
        """
        # Cleanup debug objects — only safe from the main thread
        try:
            from PySide.QtCore import QThread, QCoreApplication
            app = QCoreApplication.instance()
            if app and QThread.currentThread() == app.thread():
                doc = FreeCAD.ActiveDocument if FreeCAD else None
                if doc and doc.getObject("MinkowskiDebug"):
                    doc.removeObject("MinkowskiDebug")
                    doc.recompute()
        except Exception:
            pass  # Cleanup of debug objects; swallow exceptions if GUI or document is unavailable

        return self._nest_standard(parts, sort=sort)

    def _nest_standard(self, parts, sort=True, quiet=None):
        """
        Standard greedy nesting strategy.
        
        Args:
            parts: List of parts to nest
            sort: Whether to sort by area (largest first)
            quiet: If True, suppresses logging and progress callbacks. Defaults to self.quiet.
                   Simulation callbacks (part_start/update/part_end) are not gated —
                   they only exist when the user asked to watch the run.
        """
        # Use instance quiet setting if not explicitly passed
        if quiet is None:
            quiet = self.quiet
        all_parts = list(parts)
        current_parts = [p for p in all_parts if getattr(p, 'fill_sheet', False) is not True]
        fill_parts = [p for p in all_parts if getattr(p, 'fill_sheet', False) is True]
        if sort:
            current_parts.sort(key=lambda p: p.area, reverse=True)
            fill_parts.sort(key=lambda p: p.area, reverse=True)

        # The order parts were actually tried in. The GA records this as the
        # chromosome: placement order is not the same thing, because a part
        # that does not fit the current sheet can land on an earlier one, and
        # a chromosome in placement order does not reproduce its own layout.
        self.last_consumption_order = [p.id for p in current_parts]

        sheets = []
        unplaced_parts = []
        total_parts = len(current_parts)
        _part_timings = []  # (part_id, elapsed_s, placed)
        self.engine.reset_perf_stats()

        for i, part in enumerate(current_parts):
            if self.cancel_callback and self.cancel_callback():
                self.log("Nesting cancelled by user.")
                break

            if self.verbose and not quiet:
                self.log(f"Processing part {i+1}/{total_parts}: {part.id}")
            
            if not quiet and self.progress_callback:
                self.progress_callback(i + 1, total_parts, f"Placing {part.id}...")
            
            import time as _time
            _t0_part = _time.perf_counter()
            start_part_time = datetime.now()
            placed = False

            # Notify start of part placement (for highlighting master shapes)
            if self.part_start_callback:
                self.part_start_callback(part)

            for sheet_idx, sheet in enumerate(sheets):
                if (sheet.width * sheet.height - sheet.used_area) < part.area: continue

                if self._attempt_placement_on_sheet(part, sheet):
                    placed = True
                    if self.verbose and not quiet:
                        elapsed = (datetime.now() - start_part_time).total_seconds()
                        self.log(f"  -> Placed on Sheet {sheet_idx+1} ({elapsed:.4f}s)")

                    if self.update_callback:
                        self.update_callback(part, sheet)
                    break

            if not placed:
                new_sheet = Sheet(len(sheets), self.bin_width, self.bin_height, spacing=self.spacing,
                                  edge_spacing=self.edge_spacing)
                if self._attempt_placement_on_sheet(part, new_sheet):
                    sheets.append(new_sheet)
                    placed = True
                    if self.verbose and not quiet:
                        elapsed = (datetime.now() - start_part_time).total_seconds()
                        self.log(f"  -> Placed on New Sheet {len(sheets)} ({elapsed:.4f}s)")

                    if self.update_callback:
                        self.update_callback(part, new_sheet)
                else:
                    unplaced_parts.append(part)
                    if not quiet:
                        self.log(f"  -> FAILED to place in {(datetime.now() - start_part_time).total_seconds():.4f}s")

            _part_timings.append((part.id, _time.perf_counter() - _t0_part, placed))

            # Notify end of part placement (for unhighlighting master shapes)
            if self.part_end_callback:
                self.part_end_callback(part, placed)


        was_cancelled = self.cancel_callback and self.cancel_callback()
        if fill_parts and not was_cancelled:
            self._nest_fill_parts(sheets, fill_parts, unplaced_parts, quiet, _part_timings)


        if not quiet and _part_timings:
            self._log_timing_summary(_part_timings)

        return sheets, unplaced_parts

    def _nest_fill_parts(self, sheets, fill_parts, unplaced_parts, quiet, part_timings=None):
        """Round-robin fill phase: cycle through every fill-enabled part type,
        placing one instance per turn, until no type fits anywhere.

        - Only fills EXISTING sheets; never creates a new sheet (except when
          the run consists solely of fill parts and no sheet exists yet).
        - A type is retired permanently the first time a placement fails —
          failure is the signal that no remaining gap fits that type.
        - A hard per-type cap (free area / part area) guarantees termination
          even if placement erroneously keeps succeeding.
        """
        from collections import deque

        for sheet in sheets:
            if sheet.edge_spacing != self.edge_spacing:
                sheet.edge_spacing = self.edge_spacing
                sheet.__dict__.pop('_cand_cache', None)
        if not sheets:
            sheets.append(Sheet(0, self.bin_width, self.bin_height, spacing=self.spacing,
                                edge_spacing=self.edge_spacing))

        # Group by explicit master_label — NEVER parse part.id (display string,
        # no format guarantee; parsing it once caused exponential spawn growth).
        queues, spawners, order = {}, {}, []
        for part in fill_parts:
            part_type = getattr(part, 'master_label', None) or part.id
            if part_type not in queues:
                queues[part_type] = deque()
                spawners[part_type] = getattr(part, 'spawn_next', None)
                order.append(part_type)
            queues[part_type].append(part)

        total_area = sum(s.width * s.height for s in sheets)
        caps = {t: int(total_area / max(queues[t][0].area, 1e-9)) + 2 for t in order}
        attempts = {t: 0 for t in order}

        active = deque(order)
        while active:
            if self.cancel_callback and self.cancel_callback():
                self.log("Nesting cancelled by user.")
                break

            part_type = active.popleft()
            queue = queues[part_type]

            if not queue:
                spawn_fn = spawners[part_type]
                if spawn_fn is None or attempts[part_type] >= caps[part_type]:
                    continue  # type exhausted — do not re-queue
                try:
                    new_part = (self.spawn_more_callback(spawn_fn)
                                if self.spawn_more_callback else spawn_fn())
                except NestingCancelled:
                    raise
                except Exception as e:
                    self.log(f"Could not spawn fill part '{part_type}': {e}", level="warning")
                    continue
                if new_part is None:
                    continue
                queue.append(new_part)

            part = queue.popleft()
            attempts[part_type] += 1

            if self.part_start_callback:
                self.part_start_callback(part)

            import time as _time
            _t0_part = _time.perf_counter()
            placed = False
            for sheet in sheets:
                if (sheet.width * sheet.height - sheet.used_area) < part.area:
                    continue
                if self._attempt_placement_on_sheet(part, sheet):
                    placed = True
                    if self.update_callback:
                        self.update_callback(part, sheet)
                    break

            _dt_part = _time.perf_counter() - _t0_part
            if part_timings is not None:
                part_timings.append((part.id, _dt_part, placed))
            if not quiet:
                self.log(f"[TIMING] fill '{part.id}' ({part_type}): "
                         f"{_dt_part * 1000:.0f}ms {'placed' if placed else 'FAILED'} "
                         f"(attempt {attempts[part_type]})")

            if self.part_end_callback:
                self.part_end_callback(part, placed)

            if placed:
                active.append(part_type)  # round-robin: give the next type a turn
            else:
                unplaced_parts.append(part)
                if not quiet:
                    self.log(f"Fill type '{part_type}' retired after {attempts[part_type]} attempts.")

    def _log_timing_summary(self, part_timings):
        total_s = sum(t for _, t, _ in part_timings)
        cache = self.engine.get_perf_stats()
        total_lookups = cache['cache_hits'] + cache['cache_misses']
        hit_pct = cache['cache_hits'] / total_lookups * 100 if total_lookups else 0
        self.log(
            f"[TIMING] {len(part_timings)} parts in {total_s:.2f}s | "
            f"NFP cache: {cache['cache_hits']} hits ({hit_pct:.0f}%) / "
            f"{cache['cache_misses']} misses, compute={cache['nfp_compute_ms']:.0f}ms"
            f", rotations skipped: {cache['rotations_skipped']}"
        )
        slowest = sorted(part_timings, key=lambda x: -x[1])[:5]
        self.log("[TIMING] Slowest: " + ", ".join(
            f"{pid}={t:.2f}s{'(unplaced)' if not ok else ''}" for pid, t, ok in slowest
        ))

    def _attempt_placement_on_sheet(self, part, sheet):
        """Delegates to PlacementOptimizer."""
        placed_part = self.optimizer.find_best_placement(part, sheet)
        
        if placed_part:
            # We trust the PlacementOptimizer (and NFP engine) to have found a valid spot.
            placed_part.placement = placed_part.get_final_placement(sheet.get_origin())
            new_placed_part = PlacedPart(placed_part)
            sheet.add_part(new_placed_part)
            return True
        return False
