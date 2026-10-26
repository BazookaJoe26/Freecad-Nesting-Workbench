# SPDX-License-Identifier: LGPL-2.1-or-later
import random
import math
import FreeCAD
from .base_nester import BaseNester
from ....rotation_constraints import rotation_angles
from ....edge_spacing import part_edge_extents

class PhysicsNester(BaseNester):
    """
    A packer that uses a simple physics simulation.
    Parts are spawned at a random location and then moved in a specified
    direction until they collide with the sheet edge or another part.
    """

    def __init__(self, width, height, rotation_steps=1, **kwargs):
        super().__init__(width, height, rotation_steps, **kwargs)
        self.physics_direction = kwargs.get("physics_direction", (0, -1))
        self.max_spawn_count = kwargs.get("max_spawn_count", 100)
        self.max_nesting_steps = kwargs.get("max_nesting_steps", 500)

    def _spawn_part_on_sheet(self, shape, sheet):
        """Tries to place a shape at a random location without initial collision."""
        for _ in range(self.max_spawn_count):
            shape.set_rotation(random.choice(rotation_angles(shape, self.rotation_steps)))

            x0, y0, x1, y1 = part_edge_extents(shape, shape.angle)
            edge = sheet.edge_spacing
            min_x, max_x = edge - x0, self._bin_width - edge - x1
            min_y, max_y = edge - y0, self._bin_height - edge - y1
            if min_x > max_x + 1e-7 or min_y > max_y + 1e-7:
                continue
            target_x = random.uniform(min_x, max(min_x, max_x))
            target_y = random.uniform(min_y, max(min_y, max_y))
            center = shape.centroid
            shape.move(target_x - center.x, target_y - center.y)

            if sheet.is_placement_valid(shape):
                return shape
        return None

    def _try_place_part_on_sheet(self, part_to_place, sheet):
        """Tries to place a single shape on a given sheet using physics movement."""
        spawned_part = self._spawn_part_on_sheet(part_to_place, sheet)
        
        if spawned_part:
            if self.physics_direction is None:
                angle_rad = random.uniform(0, 2 * math.pi)
                part_direction = (math.cos(angle_rad), math.sin(angle_rad))
            else:
                part_direction = self.physics_direction
            
            return self._move_until_collision(spawned_part, sheet, part_direction)
        else:
            return None

    def _apply_physics_to_part(self, part, sheet, direction):
        """Moves a part in a direction until it hits an obstacle."""
        for _ in range(self.max_nesting_steps):
            if self.cancel_callback and self.cancel_callback():
                break

            dx = direction[0] * self.step_size
            dy = direction[1] * self.step_size

            part.move(dx, dy)

            if self.update_callback:
                self.update_callback(part, sheet)

            is_valid = sheet.is_placement_valid(part, part_to_ignore=part)
            if not is_valid:
                part.move(-dx, -dy)
                if self.update_callback:
                    self.update_callback(part, sheet)
                break

    def _move_until_collision(self, part, sheet, direction):
        """Orchestrates physics movement and annealing ('shaking') to find a dense spot."""
        cycle = 0
        while cycle < self.max_nesting_steps:
            if self.cancel_callback and self.cancel_callback():
                break

            cycle += 1

            pre_physics_x, pre_physics_y, _, _ = part.bounding_box()
            self._apply_physics_to_part(part, sheet, direction)
            post_physics_x, post_physics_y, _, _ = part.bounding_box()
            
            # Check if physics movement actually changed position
            physics_moved = abs(post_physics_x - pre_physics_x) > 1e-6 or abs(post_physics_y - pre_physics_y) > 1e-6

            if physics_moved:
                continue

            # If physics alone can't move the part, try to shake it free (annealing)
            # This now returns True if a breakthrough (better spot) was found.
            shake_moved = self._anneal_part(part, sheet, direction, 
                             rotate_enabled=self.anneal_rotate_enabled, 
                             translate_enabled=self.anneal_translate_enabled)
            
            if shake_moved:
                continue

            # No movement possible after both physics and annealing
            break
        else:
            self.log(f"Part {part.id} reached the {self.max_nesting_steps}-cycle cap before settling.")

        return part
