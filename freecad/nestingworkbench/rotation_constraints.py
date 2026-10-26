# SPDX-License-Identifier: LGPL-2.1-or-later
"""Rotation choices shared by nesting algorithms and FreeCAD-free GA workers."""


def rotation_angles(part, fallback_steps=1):
    """Grain alignment takes precedence over the ordinary rotation step grid."""
    grain_angles = getattr(part, 'grain_angles', None)
    if grain_angles is not None:
        return list(grain_angles)
    steps = getattr(part, 'rotation_steps', None)
    if steps is None:
        steps = fallback_steps
    steps = max(1, steps)
    return [i * (360.0 / steps) for i in range(steps)]


def constrain_angle(part, angle):
    """Project a GA gene or physics perturbation onto the nearest grain angle."""
    grain_angles = getattr(part, 'grain_angles', None)
    if grain_angles is None:
        return angle
    return min(grain_angles, key=lambda a: abs((a - angle + 180.0) % 360.0 - 180.0))


def placement_angles(part, fallback_steps=1):
    """A chromosome pins one legal rotation; seed/fill parts try every choice."""
    gene_angle = getattr(part, 'gene_angle', None)
    if gene_angle is not None:
        return [constrain_angle(part, gene_angle) % 360.0]
    return rotation_angles(part, fallback_steps)
