# SPDX-License-Identifier: LGPL-2.1-or-later
"""Sheet-edge limits use material geometry, independently of collision padding."""
from shapely.affinity import rotate


def effective_edge_spacing(params):
    return float(params.get('edge_spacing', 0.0)) if params.get('custom_edge_spacing', False) else 0.0


def part_edge_extents(part, angle, rotation_cache=None):
    """Material bounds relative to the buffered polygon's rotation centre."""
    polygon = getattr(part, 'unbuffered_polygon', None)
    if polygon is None:
        polygon = part.original_polygon
    center = part.original_polygon.centroid
    origin = (center.x, center.y)
    if rotation_cache is not None:
        return rotation_cache.get(polygon, angle, origin=origin).extents
    bounds = rotate(polygon, angle, origin=origin).bounds
    return (bounds[0] - center.x, bounds[1] - center.y,
            bounds[2] - center.x, bounds[3] - center.y)


def part_edge_bounds(part):
    extents = part_edge_extents(part, part.angle)
    center = part.polygon.centroid
    return (center.x + extents[0], center.y + extents[1],
            center.x + extents[2], center.y + extents[3])
