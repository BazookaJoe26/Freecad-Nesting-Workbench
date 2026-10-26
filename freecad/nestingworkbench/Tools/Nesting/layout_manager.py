# SPDX-License-Identifier: LGPL-2.1-or-later
"""
LayoutManager - Handles creation, cloning, and management of Layout objects.

This class is responsible for:
- Creating new layouts with master shapes and part instances
- Cloning layouts for GA population members
- Deleting layouts and all their child objects
- Calculating layout efficiency

Separates layout management from the nesting algorithm for cleaner architecture.
"""

import FreeCAD
import copy
import random
import math
import time
from .shape_preparer import ShapePreparer
from ...datatypes.shape import Shape
from ...freecad_helpers import recursive_delete
from ...rotation_constraints import constrain_angle, rotation_angles
from .cancellation import check_cancel
from .algorithms.genetic_utils import (
    largest_open_area,
    compute_layout_fitness,
    set_warning_logger,
    SHAPELY_AVAILABLE,
)

set_warning_logger(lambda m: FreeCAD.Console.PrintWarning(m + "\n"))


class Layout:
    """
    Represents a single layout attempt (population member in GA).
    Contains references to the FreeCAD objects and the parts list.
    
    Attributes:
        genes: List of (part_id, angle) tuples representing the ordering and rotation
               of parts. Can be used to recreate the exact same layout.
    """
    def __init__(self, layout_group, parts_group, parts, master_shapes_group=None):
        self.layout_group = layout_group  # The Layout_xxx group object
        self.parts_group = parts_group    # The PartsToPlace group
        self.parts = parts                # List of Shape objects for nesting
        self.master_shapes_group = master_shapes_group
        self.sheets = []                  # Filled after nesting
        self.fitness = float('inf')
        self.efficiency = 0.0
        self.genes = []                   # (part_id, angle) tuples - the "DNA" of this layout
        self.direction = None             # (dx, dy) unit vector; None = use global search_direction
        self.result = None                # compact result for a geometry-only candidate
    
    @property
    def name(self):
        return self.layout_group.Label if self.layout_group else getattr(self, '_name', 'unknown')

class LayoutManager:
    """
    Manages layout creation, cloning, and deletion.
    Acts as a factory for Layout objects used in nesting.
    """
    
    def __init__(self, doc, processed_shape_cache=None, rng=None, geometry_only=False, cancel_callback=None):
        self.doc = doc
        self.processed_shape_cache = processed_shape_cache or {}
        self._layout_counter = 0
        self.rng = rng or random
        self.geometry_only = geometry_only
        self.document_layout = None
        self.document_layouts = []
        self.cancel_callback = cancel_callback
        self._last_gui_poll = 0.0
        self.part_catalog = None
        self.geometry_payload = None
        self.geometries = None
        from .algorithms.rotation_cache import RotationCache
        self.rotation_cache = RotationCache()

    def _check_cancel(self):
        check_cancel(self.cancel_callback)
        # Preparation runs on the GUI thread. Yield between complete objects
        # so a queued Cancel click can actually set the cancellation flag.
        if self.cancel_callback is not None and FreeCAD.GuiUp:
            from PySide import QtCore
            app = QtCore.QCoreApplication.instance()
            now = time.monotonic()
            if app and QtCore.QThread.currentThread() == app.thread() and now - self._last_gui_poll >= 0.05:
                self._last_gui_poll = now
                app.processEvents(QtCore.QEventLoop.AllEvents, 5)
        check_cancel(self.cancel_callback)
    
    def create_layout(self, name, master_shapes_map, quantities, ui_params, 
                      chromosome_ordering=None, member_idx: int = 0) -> Layout:
        self._check_cancel()
        if not self.geometry_only:
            return self._create_document_layout(name, master_shapes_map, quantities, ui_params,
                                                chromosome_ordering, member_idx)

        from .ga_snapshot import SnapshotShape, build_part_catalog
        if self.document_layout is None:
            # One document sandbox supplies master geometry, fill spawners and
            # the final winner's objects. No candidate owns document objects.
            self.document_layout = self._create_document_layout(
                'Layout_GA_Workspace', master_shapes_map, quantities, ui_params)
            self.part_catalog, self.geometry_payload = build_part_catalog(self.document_layout.parts)
            self.geometries = {self.part_catalog[p.id].geometry_key: p.original_polygon
                               for p in self.document_layout.parts}
            self.geometries.update({self.part_catalog[p.id].unbuffered_geometry_key: p.unbuffered_polygon
                                    for p in self.document_layout.parts
                                    if self.part_catalog[p.id].unbuffered_geometry_key is not None})

        parts = []
        for snap in self.part_catalog.values():
            self._check_cancel()
            parts.append(SnapshotShape(snap, self.geometries, self.rotation_cache))
        if chromosome_ordering:
            parts = self._apply_ordering(parts, chromosome_ordering)
        layout = Layout(None, None, parts)
        layout._name = name
        layout.member_idx = member_idx
        if chromosome_ordering:
            layout.genes = [(p.id, p.angle) for p in parts if not p.fill_sheet]
        return layout

    def materialize_layout(self, layout, ui_params):
        """Attach the winning geometry result to the single document sandbox.

        Must run on the main thread, before fill placement or drawing.
        """
        if not self.geometry_only or layout is None:
            return layout
        from .ga_snapshot import apply_result
        if layout.result is None:
            raise ValueError('Cannot materialize an unevaluated GA candidate')
        target = self.document_layout
        apply_result(target, layout.result, ui_params, cancel_callback=self._check_cancel)
        target.direction = layout.direction
        target.member_idx = layout.member_idx
        return target

    def _create_document_layout(self, name, master_shapes_map, quantities, ui_params,
                                chromosome_ordering=None, member_idx=0):
        """
        Creates a new layout with master shapes and part instances.
        
        Args:
            name: Name for the layout (e.g., "Layout_GA_1")
            master_shapes_map: Dict mapping labels to FreeCAD shape objects
            quantities: Dict mapping labels to (quantity, rotation_steps)
            ui_params: UI parameters dict
            chromosome_ordering: Optional list of (part_id, angle) tuples for ordering
            member_idx: Index of layout within population for deterministic tracking
            
        Returns:
            Layout object containing the layout group and prepared parts
        """
        # Create layout group
        layout_group = self.doc.addObject("App::DocumentObjectGroup", name)
        layout_group.Label = name
        if getattr(layout_group, "ViewObject", None) is not None:
            layout_group.ViewObject.Visibility = True
        
        # Create parts bin
        parts_group = self.doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
        layout_group.addObject(parts_group)
        
        # Create shape preparer for this layout
        preparer = ShapePreparer(self.doc, self.processed_shape_cache, cancel_callback=self._check_cancel)
        
        # Prepare parts (creates masters and instances)
        try:
            parts = preparer.prepare_parts(
                ui_params, quantities, master_shapes_map,
                layout_group, parts_group
            )
        except Exception:
            recursive_delete(self.doc, layout_group)
            raise
        
        # Get master shapes group
        master_shapes_group = None
        for child in layout_group.Group:
            if child.Label == "MasterShapes":
                master_shapes_group = child
                break
        
        # Apply chromosome ordering if provided
        if chromosome_ordering and parts:
            parts = self._apply_ordering(parts, chromosome_ordering)
        
        self._layout_counter += 1
        
        layout = Layout(layout_group, parts_group, parts, master_shapes_group)
        self.document_layouts.append(layout)
        layout.member_idx = member_idx
        if chromosome_ordering and parts:
            # Genotype drives nesting: only genes for parts that actually exist,
            # and never for fill parts (they are placed greedily after the genome)
            layout.genes = [(p.id, getattr(p, '_angle', 0)) for p in parts
                            if getattr(p, 'fill_sheet', False) is not True]
        return layout
    
    def _apply_ordering(self, parts, chromosome_ordering):
        """
        Reorders and rotates parts according to a chromosome.
        
        Args:
            parts: List of Shape objects
            chromosome_ordering: List of (part_id, angle) tuples
            
        Returns:
            Reordered list of Shape objects with rotations applied
        """
        if not chromosome_ordering:
            return parts
        
        # Build a map of part id -> part
        parts_map = {p.id: p for p in parts}
        
        ordered_parts = []
        for part_id, angle in chromosome_ordering:
            self._check_cancel()
            if part_id in parts_map:
                part = parts_map[part_id]
                if angle is not None:
                    angle = constrain_angle(part, angle)
                    part.set_rotation(angle)
                    part.gene_angle = angle
                ordered_parts.append(part)
        
        # Parts not in the chromosome (fill parts) tag along at the end in
        # their original relative order; the nester's fill phase handles them.
        ordered_ids = {p.id for p in ordered_parts}
        ordered_parts.extend(p for p in parts if p.id not in ordered_ids)
        
        return ordered_parts
    
    def delete_layout(self, layout, verbose=False):
        """
        Removes a layout group and ALL its children from the document.
        Must recursively delete children first since FreeCAD doesn't do this automatically.
        
        Args:
            layout: Layout object to delete
            verbose: If True, log deletion
        """
        if not layout:
            return
            
        # Check if already deleted
        if hasattr(layout, '_deleted') and layout._deleted:
            return
        
        # Get the group object before we mark it deleted
        group_obj = None
        try:
            if layout.layout_group:
                group_obj = layout.layout_group
        except Exception:
            pass  # Stale layout_group reference during cleanup
        
        layout_label = layout.name if hasattr(layout, 'name') else "unknown"
        
        # Mark as deleted immediately to prevent re-entry
        layout._deleted = True
        if layout in self.document_layouts:
            self.document_layouts.remove(layout)
        layout.layout_group = None
        layout.sheets = []
        layout.parts = []
        
        # Recursively delete the group and all children
        if group_obj:
            recursive_delete(self.doc, group_obj)
            if verbose:
                FreeCAD.Console.PrintMessage(f"  Deleted: {layout_label}\n")

    

    
    def calculate_efficiency(self, layout, sheet_width, sheet_height,
                             compactness_weight=0.0) -> tuple:
        """
        Calculates the packing efficiency of a layout.
        
        Args:
            layout: Layout object with sheets populated
            sheet_width: Width of each sheet
            sheet_height: Height of each sheet
            compactness_weight: 0 disables the open-area term (default); higher
                                values weight the largest-open-area deficit into the last-sheet
                                tie-break as a blend that never exceeds one sheet's area
            
        Returns:
            (fitness, efficiency_percent) tuple
        """
        fitness, efficiency = compute_layout_fitness(
            layout.sheets, sheet_width, sheet_height, compactness_weight=compactness_weight
        )
        layout.fitness = fitness
        layout.efficiency = efficiency
        return fitness, efficiency
    
    def create_ga_population(self, master_shapes_map, quantities, ui_params, 
                             population_size, rotation_steps=1, verbose=False) -> list:
        """
        Creates a population of layouts for genetic algorithm.
        
        Args:
            master_shapes_map: Dict mapping labels to FreeCAD shape objects
            quantities: Dict mapping labels to (quantity, rotation_steps)
            ui_params: UI parameters dict
            population_size: Number of layouts to create
            rotation_steps: Number of rotation steps for random rotations
            
        Returns:
            List of Layout objects
        """
        population = []
        
        for i in range(population_size):
            self._check_cancel()
            name = f"Layout_GA_{i+1}"
            
            # Create the layout
            layout = self.create_layout(name, master_shapes_map, quantities, ui_params, member_idx=i)
            
            if layout.parts and i > 0:  # First layout keeps original ordering
                regular = [p for p in layout.parts if getattr(p, 'fill_sheet', False) is not True]
                fill = [p for p in layout.parts if getattr(p, 'fill_sheet', False) is True]

                # Shuffle the regular parts order; fill parts stay at the tail
                self.rng.shuffle(regular)
                layout.parts = regular + fill

                # Apply random rotations (regular parts only — fill parts keep
                # their full rotation sweep and never get a gene_angle pin)
                for part in regular:
                    angle = self.rng.choice(rotation_angles(part, rotation_steps))
                    part.set_rotation(angle)
                    part.gene_angle = angle

                # Record genotype so nest() runs with sort=False and this ordering survives
                layout.genes = [(p.id, getattr(p, '_angle', 0)) for p in regular]

                # Per-layout search direction
                if ui_params.get('use_random_direction', False):
                    angle_rad = self.rng.uniform(0, 2 * math.pi)
                    layout.direction = (math.cos(angle_rad), math.sin(angle_rad))
                else:
                    layout.direction = None
            
            population.append(layout)
            if verbose:
                FreeCAD.Console.PrintMessage(f"Created layout {name} with {len(layout.parts)} parts\n")
        
        return population
    
