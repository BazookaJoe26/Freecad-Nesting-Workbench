# SPDX-License-Identifier: LGPL-2.1-or-later
"""Bounded, process-local cache of immutable polygon rotation data."""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

from shapely.affinity import rotate


@dataclass(frozen=True)
class RotationData:
    polygon: object
    extents: tuple


class RotationCache:
    def __init__(self, max_entries=1024):
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = RLock()

    def get(self, polygon, angle, origin=None):
        # Keep the original polygon alive in the entry: Python cannot reuse
        # its id for different geometry while the entry is cached. Labels and
        # rounded angles are deliberately not used as geometry identities.
        key = (id(polygon), float(angle), origin)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[1]

            center = polygon.centroid
            pivot = (center.x, center.y) if origin is None else origin
            rotated = rotate(polygon, angle, origin=pivot)
            if origin is None:
                center = rotated.centroid
                pivot = (center.x, center.y)
            x0, y0, x1, y1 = rotated.bounds
            data = RotationData(rotated, (x0 - pivot[0], y0 - pivot[1],
                                          x1 - pivot[0], y1 - pivot[1]))
            self._entries[key] = (polygon, data)
            if len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return data
