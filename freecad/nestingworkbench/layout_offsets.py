# SPDX-License-Identifier: LGPL-2.1-or-later
"""Expression-backed layout offsets, in FreeCAD's internal length unit (mm)."""

import math
import FreeCAD

from .constants import LAYOUT_OFFSET_PROPERTIES


def evaluate_length(evaluator, text, label, minimum=None, maximum=None):
    """Evaluate a length using FreeCAD's parser and enforce the field's limits."""
    expression = text.strip().lstrip("=").strip() or "0"
    try:
        value = evaluator.evalExpression(expression)
        if isinstance(value, FreeCAD.Units.Quantity):
            if value.Unit not in (FreeCAD.Units.Unit(), FreeCAD.Units.Quantity("1 mm").Unit):
                raise ValueError("expected a length or a number in mm")
            value = value.Value
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("expected a finite length")
        if minimum is not None and value < minimum:
            raise ValueError(f"must be at least {minimum:g} mm")
        if maximum is not None and value > maximum:
            raise ValueError(f"must be at most {maximum:g} mm")
        return value
    except Exception as exc:
        raise ValueError(f"{label}: {exc}") from exc


def evaluate_offsets(doc, texts, context=None):
    """Validate all inputs before changing a layout or starting a worker."""
    evaluator = context
    temporary = evaluator is None
    if temporary:
        evaluator = doc.addObject("App::FeaturePython", "NestingOffsetEvaluation")
    values = []
    expressions = []
    try:
        for axis, text in zip("XYZ", texts):
            expression = text.strip().lstrip("=").strip() or "0"
            value = evaluate_length(evaluator, expression, f"{axis} offset")
            values.append(value)
            expressions.append(expression)
    finally:
        if temporary:
            doc.removeObject(evaluator.Name)
    return values, expressions


def layout_offset(layout):
    return FreeCAD.Vector(*(float(getattr(layout, prop, 0)) for prop in LAYOUT_OFFSET_PROPERTIES))


def sheet_placement_objects(sheet_group):
    """Yield world placements, stopping at App::Part containers (children are local)."""
    for child in sheet_group.Group:
        if hasattr(child, "Placement"):
            yield child
        elif hasattr(child, "Group"):
            yield from sheet_placement_objects(child)


def bind_layout_offset(obj, layout):
    if not hasattr(obj, "NestingLayout"):
        obj.addProperty("App::PropertyLinkHidden", "NestingLayout", "Nesting", "Layout providing XYZ offsets")
        obj.setEditorMode("NestingLayout", 2)
    obj.NestingLayout = layout
    set_layout_placement(obj, obj.Placement.copy())


def set_layout_placement(obj, placement):
    """Move an object without losing its live offset when manually nesting/stacking."""
    layout = getattr(obj, "NestingLayout", None)
    if layout is None:
        obj.Placement = placement
        return
    local = placement.Base - layout_offset(layout)
    obj.Placement = placement
    for axis, prop, value in zip("xyz", LAYOUT_OFFSET_PROPERTIES, (local.x, local.y, local.z)):
        # The layout contains this object. A normal back-reference would form
        # a group dependency cycle; href keeps this property-only link valid.
        obj.setExpression(f"Placement.Base.{axis}", f"href({layout.Name}.{prop}) + ({value:.17g} mm)")


def apply_layout_offsets(layout, params):
    """Store source expressions and bind the finished sheets, parts and labels."""
    values = params.get("layout_offset", (0, 0, 0))
    expressions = params.get("offset_expressions", [str(v) for v in values])
    for prop, value, expression in zip(LAYOUT_OFFSET_PROPERTIES, values, expressions):
        if not hasattr(layout, prop):
            layout.addProperty("App::PropertyDistance", prop, "Layout", "Offset of the complete layout")
        layout.setExpression(prop, None)
        setattr(layout, prop, value)
        if expression:
            layout.setExpression(prop, expression)
    layout.Document.recompute()
    # Drawing used a snapshot of the inputs. Recover local coordinates from that
    # snapshot even if an external variable changed while nesting was running.
    drawn_offset = FreeCAD.Vector(*values)
    current_offset = layout_offset(layout)
    for sheet in layout.Group:
        if sheet.Label.startswith("Sheet_"):
            for obj in sheet_placement_objects(sheet):
                placement = obj.Placement.copy()
                placement.Base += current_offset - drawn_offset
                obj.Placement = placement
                bind_layout_offset(obj, layout)
