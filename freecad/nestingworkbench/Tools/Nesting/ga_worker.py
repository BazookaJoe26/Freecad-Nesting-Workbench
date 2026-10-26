# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Worker process entry points for GA population member nesting.
Executes in worker processes without FreeCAD dependencies.
"""
from ...datatypes.shape import Shape
from .ga_snapshot import MemberTask, MemberResult, nest_from_snapshot
from .algorithms.rotation_cache import RotationCache
import shapely

_part_catalog = None
_geometries = None
_rotation_cache = None


def init_worker(cache_payload, part_catalog=None, geometry_payload=None):
    """
    Initializer for ProcessPoolExecutor workers.
    Assigns the precomputed NFP cache payload to Shape.nfp_cache.
    Guarantees cross-platform (fork and spawn) cache availability.
    """
    global _part_catalog, _geometries, _rotation_cache
    _part_catalog = part_catalog
    _geometries = {key: shapely.from_wkb(wkb) for key, wkb in (geometry_payload or {}).items()}
    _rotation_cache = RotationCache()
    if cache_payload is not None:
        with Shape.nfp_cache_lock:
            Shape.nfp_cache.clear()
            Shape.nfp_cache.update(cache_payload)


def worker_ping():
    """Start-up probe: answering at all proves the worker process runs."""
    return True


def worker_nest(task: MemberTask) -> MemberResult:
    """
    Nests a single population member task in pure geometry.
    """
    return nest_from_snapshot(task, _part_catalog, _geometries, _rotation_cache)
