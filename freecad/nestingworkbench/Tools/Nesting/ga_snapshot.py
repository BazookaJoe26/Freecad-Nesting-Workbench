# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Pure-geometry snapshot structures and marshaling for GA population members.
Enables nesting population members in worker processes without FreeCAD dependencies.
"""
from dataclasses import dataclass, replace
import time
import zlib
import random
import shapely
from shapely.affinity import rotate, translate
from .algorithms.nesting_strategy import Nester
from .algorithms.genetic_utils import compute_layout_fitness
from .algorithms.rotation_cache import RotationCache
from ...rotation_constraints import constrain_angle
from .cancellation import check_cancel
from ...edge_spacing import effective_edge_spacing

UNPLACED_PENALTY_FACTOR = 10.0  # each unplaced part costs 10 sheet-areas of fitness


@dataclass(frozen=True)
class PartSnapshot:
    part_id: str          # was Shape.id
    type_label: str       # was source_freecad_object.Label — the NFP cache key field
    polygon_wkb: bytes | None  # standalone tasks only; catalog tasks use geometry_key
    gene_angle: float | None  # None means "no gene — sweep all rotations"
    rotation_steps: int
    spacing: float
    deflection: float
    simplification: float
    fill_sheet: bool
    geometry_key: int | None = None
    grain_angles: tuple | None = None
    unbuffered_wkb: bytes | None = None
    unbuffered_geometry_key: int | None = None


@dataclass(frozen=True)
class PartGene:
    """Per-member data; geometry and metadata live in the run's catalog."""
    part_id: str
    angle: float | None


@dataclass(frozen=True)
class MemberTask:
    member_idx: int
    generation: int
    rng_seed: int          # derived from (seed, generation, member_idx) for reproducible worker-process randomness
    parts: tuple           # PartGene with a catalog, or standalone PartSnapshot
    direction: tuple | None
    sheet_width: float
    sheet_height: float
    rotation_steps: int
    compactness_weight: float
    sort: bool = False
    edge_spacing: float = 0.0


@dataclass(frozen=True)
class MemberResult:
    member_idx: int
    fitness: float
    efficiency: float
    sheet_count: int
    placements: tuple      # (part_id, sheet_index, x, y, angle)
    unplaced_ids: tuple
    genes: tuple
    elapsed: float


class SnapshotShape:
    """Pure-geometry Shape stand-in reconstructed from PartSnapshot for worker nesting."""

    def __init__(self, snap: PartSnapshot, geometries=None, rotation_cache=None):
        self.id = snap.part_id
        self.type_label = snap.type_label
        self.rotation_steps = snap.rotation_steps
        self.spacing = snap.spacing
        self.deflection = snap.deflection
        self.simplification = snap.simplification
        self.fill_sheet = snap.fill_sheet
        self._rotation_cache = rotation_cache
        self.grain_angles = snap.grain_angles
        if snap.gene_angle is not None:
            self.gene_angle = constrain_angle(self, snap.gene_angle)
            self._angle = self.gene_angle
        else:
            self._angle = constrain_angle(self, 0.0)

        self.original_polygon = (geometries[snap.geometry_key] if snap.geometry_key is not None
                                 else shapely.from_wkb(snap.polygon_wkb))
        self.unbuffered_polygon = (
            geometries[snap.unbuffered_geometry_key] if snap.unbuffered_geometry_key is not None
            else shapely.from_wkb(snap.unbuffered_wkb) if snap.unbuffered_wkb is not None else None)
        if self._angle != 0.0:
            self.polygon = self._rotated_polygon(self._angle)
        else:
            self.polygon = self.original_polygon
        self.source_freecad_object = None

    def _rotated_polygon(self, angle):
        if self._rotation_cache is not None:
            return self._rotation_cache.get(self.original_polygon, angle).polygon
        return rotate(self.original_polygon, angle, origin='centroid')

    @property
    def area(self):
        polygon = self.unbuffered_polygon if self.unbuffered_polygon is not None else self.polygon
        return polygon.area if polygon is not None else 0.0

    @property
    def centroid(self):
        return self.polygon.centroid if self.polygon else None

    @property
    def angle(self):
        return self._angle

    def set_rotation(self, angle, reposition=True):
        if self.original_polygon:
            if reposition:
                min_x, min_y, _, _ = self.bounding_box()
            self._angle = angle
            self.polygon = self._rotated_polygon(angle)
            if reposition:
                self.move_to(min_x, min_y)

    def move(self, dx, dy):
        if self.polygon:
            self.polygon = translate(self.polygon, xoff=dx, yoff=dy)

    def move_to(self, x, y):
        if self.polygon:
            min_x, min_y, _, _ = self.bounding_box()
            self.move(x - min_x, y - min_y)

    def bounding_box(self):
        if not self.polygon:
            return (0.0, 0.0, 0.0, 0.0)
        min_x, min_y, max_x, max_y = self.polygon.bounds
        return min_x, min_y, max_x - min_x, max_y - min_y

    def get_final_placement(self, origin=(0, 0, 0)):
        return None


def build_part_catalog(parts):
    """Serialize each shared master geometry once for the lifetime of a run."""
    catalog, geometry_payload, geometry_ids = {}, {}, {}
    def intern(polygon):
        if polygon is None:
            return None
        identity = id(polygon)
        if identity not in geometry_ids:
            key = len(geometry_ids)
            geometry_ids[identity] = key
            geometry_payload[key] = shapely.to_wkb(polygon)
        return geometry_ids[identity]

    for part in parts:
        key = intern(part.original_polygon)
        unbuffered_key = intern(getattr(part, 'unbuffered_polygon', None))
        catalog[part.id] = PartSnapshot(
            part.id, part.type_label, None, None, int(part.rotation_steps or 1),
            float(part.spacing), float(part.deflection), float(part.simplification),
            bool(part.fill_sheet), key,
            grain_angles=getattr(part, 'grain_angles', None),
            unbuffered_geometry_key=unbuffered_key)
    return catalog, geometry_payload


def snapshot_member(layout, ui_params: dict, gen: int, idx: int, seed: int,
                    search_direction=(0, -1), part_catalog=None) -> MemberTask:
    """Snapshots one population layout into a FreeCAD-free MemberTask."""
    part_snapshots = []
    # Regular parts only for generations (fill deferred to winner)
    regular_parts = [p for p in layout.parts if not getattr(p, 'fill_sheet', False)]
    for p in regular_parts:
        # None and 0.0 are different: None means "no gene, sweep every
        # rotation"; 0.0 means "the chromosome pins this part to 0deg".
        # An `or` chain collapses them and silently kills the sweep.
        _gene = getattr(p, 'gene_angle', None)
        if part_catalog is not None:
            if p.id not in part_catalog:
                raise KeyError(f"Part {p.id!r} is missing from the worker catalog")
            part_snapshots.append(PartGene(p.id, None if _gene is None else float(_gene)))
            continue
        snap = PartSnapshot(
            part_id=p.id,
            type_label=getattr(p, 'type_label', None) or (p.source_freecad_object.Label if getattr(p, 'source_freecad_object', None) else "unknown"),
            polygon_wkb=shapely.to_wkb(p.original_polygon),
            gene_angle=None if _gene is None else float(_gene),
            rotation_steps=int(getattr(p, 'rotation_steps', 1) or 1),
            spacing=float(getattr(p, 'spacing', 0.0) or 0.0),
            deflection=float(getattr(p, 'deflection', 0.05) or 0.05),
            simplification=float(getattr(p, 'simplification', 1.0) or 1.0),
            fill_sheet=bool(getattr(p, 'fill_sheet', False)),
            grain_angles=getattr(p, 'grain_angles', None),
            unbuffered_wkb=(shapely.to_wkb(p.unbuffered_polygon)
                            if getattr(p, 'unbuffered_polygon', None) is not None else None),
        )
        part_snapshots.append(snap)

    rng_seed = zlib.crc32(f"{seed}:{gen}:{idx}".encode())
    sort = False if getattr(layout, 'genes', None) else True

    return MemberTask(
        member_idx=idx,
        generation=gen,
        rng_seed=rng_seed,
        parts=tuple(part_snapshots),
        direction=(layout.direction if layout.direction is not None
                   else search_direction),
        sheet_width=float(ui_params.get('sheet_width', 300.0)),
        sheet_height=float(ui_params.get('sheet_height', 300.0)),
        rotation_steps=int(ui_params.get('rotation_steps', 1)),
        compactness_weight=float(ui_params.get('compactness_weight', 0.0)),
        sort=sort,
        edge_spacing=effective_edge_spacing(ui_params),
    )


def nest_from_snapshot(task: MemberTask, part_catalog=None, geometries=None,
                       rotation_cache=None, cancel_callback=None, progress_callback=None,
                       log_callback=None, quiet=True) -> MemberResult:
    """Nests a single MemberTask in pure geometry (worker process or serial)."""
    t0 = time.perf_counter()
    rotation_cache = rotation_cache if rotation_cache is not None else RotationCache()
    shapes = []
    for part in task.parts:
        check_cancel(cancel_callback)
        snap = replace(part_catalog[part.part_id], gene_angle=part.angle) if isinstance(part, PartGene) else part
        shapes.append(SnapshotShape(snap, geometries, rotation_cache))

    rng = random.Random(task.rng_seed)
    nester_kwargs = {
        'width': task.sheet_width,
        'height': task.sheet_height,
        'rotation_steps': task.rotation_steps,
        'edge_spacing': task.edge_spacing,
        'quiet': quiet,
        'rng': rng,
        'rotation_cache': rotation_cache,
        'cancel_callback': cancel_callback,
        'progress_callback': progress_callback,
        'log_callback': log_callback,
    }
    nester_kwargs['search_direction'] = task.direction

    nester = Nester(**nester_kwargs)

    sheets, unplaced = nester.nest(shapes, sort=task.sort)
    check_cancel(cancel_callback)

    fitness, efficiency = compute_layout_fitness(
        sheets, task.sheet_width, task.sheet_height, compactness_weight=task.compactness_weight
    )

    unplaced_regular = [p for p in unplaced if not getattr(p, 'fill_sheet', False)]
    if unplaced_regular:
        fitness += len(unplaced_regular) * task.sheet_width * task.sheet_height * UNPLACED_PENALTY_FACTOR

    placements = []
    for s_idx, s in enumerate(sheets):
        for p in s.parts:
            placements.append((p.shape.id, s_idx, p.x, p.y, p.angle))

    # Genes record the order the nester CONSUMED parts, not the input order.
    # Member 0 of every population nests with sort=True, so the nester picks
    # the order itself; an input-order chromosome then fails to reproduce its
    # own layout and every child bred from the champion inherits a genome that
    # nests worse than the parent it came from.
    gene_map = {p.shape.id: getattr(p.shape, '_angle', 0.0) for s in sheets for p in s.parts}
    gene_ids = {p.id for p in shapes if not getattr(p, 'fill_sheet', False)}
    angles = {p.id: getattr(p, '_angle', 0.0) for p in shapes}
    ordered = [pid for pid in getattr(nester, 'last_consumption_order', [])
               if pid in gene_ids]
    seen_ids = set(ordered)
    ordered += [p.id for p in shapes if p.id in gene_ids and p.id not in seen_ids]
    genes = tuple((pid, gene_map.get(pid, angles.get(pid, 0.0))) for pid in ordered)

    unplaced_ids = tuple(p.id for p in unplaced)
    elapsed = time.perf_counter() - t0

    return MemberResult(
        member_idx=task.member_idx,
        fitness=fitness,
        efficiency=efficiency,
        sheet_count=len(sheets),
        placements=tuple(placements),
        unplaced_ids=unplaced_ids,
        genes=genes,
        elapsed=elapsed,
    )


def apply_result(layout, result: MemberResult, ui_params: dict | None = None, cancel_callback=None):
    """
    Main-thread only. Writes MemberResult back onto live FreeCAD Shape objects,
    reconstructing Sheet and PlacedPart instances.
    Replaces ga_coordinator.py:426-442.
    """
    from ...datatypes.sheet import Sheet
    from ...datatypes.placed_part import PlacedPart

    width = ui_params.get('sheet_width', 300.0) if ui_params else 300.0
    height = ui_params.get('sheet_height', 300.0) if ui_params else 300.0
    spacing = ui_params.get('spacing', 0.0) if ui_params else 0.0

    original_parts_map = {p.id: p for p in layout.parts}

    sheet_placements = {}
    for part_id, sheet_idx, x, y, angle in result.placements:
        sheet_placements.setdefault(sheet_idx, []).append((part_id, x, y, angle))

    missing = []
    sheets = []
    num_sheets = max(result.sheet_count, (max(sheet_placements.keys()) + 1) if sheet_placements else 0)
    for s_idx in range(num_sheets):
        check_cancel(cancel_callback)
        sheet = Sheet(s_idx, width, height, spacing=spacing,
                      edge_spacing=effective_edge_spacing(ui_params or {}))
        for part_id, x, y, angle in sheet_placements.get(s_idx, []):
            check_cancel(cancel_callback)
            original_part = original_parts_map.get(part_id)
            if original_part is None:
                missing.append(part_id)
                continue
            original_part.set_rotation(angle, reposition=False)
            curr = original_part.polygon.centroid
            original_part.move(x - curr.x, y - curr.y)
            original_part.placement = original_part.get_final_placement(sheet.get_origin())
            placed = PlacedPart(original_part)
            sheet.add_part(placed)
        sheets.append(sheet)

    if missing:
        import FreeCAD  # main-thread only; module stays FreeCAD-free for workers
        FreeCAD.Console.PrintWarning(
            f"[GA] {len(missing)} placement(s) had no matching part and were dropped: "
            f"{', '.join(map(str, missing[:5]))}{' …' if len(missing) > 5 else ''}\n")


    layout.sheets = sheets
    layout.unplaced = [p for p in layout.parts if p.id in result.unplaced_ids]
    layout.fitness = result.fitness
    layout.efficiency = result.efficiency
    layout.genes = list(result.genes)
