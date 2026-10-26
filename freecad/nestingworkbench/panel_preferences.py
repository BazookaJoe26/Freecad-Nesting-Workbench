# SPDX-License-Identifier: LGPL-2.1-or-later
"""Remember panel inputs independently of running or accepting a nesting job."""
import hashlib
import json
import os

import FreeCAD
from .constants import *
from .ui_helpers import closest_angle_index


# Keep existing preference keys so settings from earlier versions still load.
FLOAT_FIELDS = {
    'sheet_width_input': PROP_SHEET_WIDTH,
    'sheet_height_input': PROP_SHEET_HEIGHT,
    'sheet_thickness_input': PROP_SHEET_THICKNESS,
    'part_spacing_input': PROP_PART_SPACING,
    'edge_spacing_input': PROP_EDGE_SPACING,
    'deflection_input': PROP_DEFLECTION_ANGLE,
    'simplification_input': PROP_SIMPLIFICATION,
    'label_size_input': PROP_LABEL_SIZE,
    'label_height_input': PROP_LABEL_HEIGHT,
    'minkowski_compactness_input': 'GACompactnessWeight',
    'physics_step_size_input': 'PhysicsStepSize',
    'physics_improvement_threshold_input': 'PhysicsStabilityTolerance',
    'physics_anneal_min_amp': 'PhysicsAnnealMinAmp',
    'physics_anneal_max_amp': 'PhysicsAnnealMaxAmp',
    'physics_anneal_rot_min': 'PhysicsAnnealRotMin',
    'physics_anneal_rot_max': 'PhysicsAnnealRotMax',
}
INT_FIELDS = {
    'minkowski_population_size_input': PROP_POPULATION_SIZE,
    'minkowski_generations_input': PROP_GENERATIONS,
    'minkowski_direction_dial': PROP_NESTING_DIRECTION,
    'physics_direction_dial': 'PhysicsDirection',
    'physics_max_spawn_input': 'PhysicsMaxSpawnCount',
    'physics_max_nesting_steps_input': 'PhysicsMaxNestingSteps',
    'physics_anneal_steps_input': 'PhysicsAnnealSteps',
    'physics_anneal_rot_steps': 'PhysicsAnnealRotSteps',
}
BOOL_FIELDS = {
    'custom_edge_spacing_checkbox': PROP_CUSTOM_EDGE_SPACING,
    'add_labels_checkbox': PROP_ADD_LABELS,
    'show_bounds_checkbox': PROP_SHOW_BOUNDS,
    'verbose_logging_checkbox': 'VerboseLogging',
    'simulate_nesting_checkbox': 'SimulateNesting',
    'clear_cache_checkbox': 'ClearNFPCache',
    'sound_checkbox': 'PlaySound',
    'minkowski_random_checkbox': 'MinkowskiRandomDirection',
    'physics_random_checkbox': 'PhysicsRandomDirection',
    'anneal_rotate_checkbox': 'PhysicsAnnealRotate',
    'anneal_translate_checkbox': 'PhysicsAnnealTranslate',
    'anneal_random_shake_checkbox': 'PhysicsRandomShakeDirection',
}
COMBO_FIELDS = {
    'algorithm_dropdown': PROP_ALGORITHM,
    'sheet_grain_direction_combo': PROP_SHEET_GRAIN_DIRECTION,
    'physics_anneal_curve_type': 'PhysicsAnnealCurveType',
    'physics_anneal_rot_curve_type': 'PhysicsAnnealRotCurveType',
}
TEXT_FIELDS = {f'offset_{axis}_input': prop
               for axis, prop in zip('xyz', LAYOUT_OFFSET_PROPERTIES)}
ROTATION_FIELDS = (
    ('minkowski_rotation_steps_slider', 'MinkowskiRotationSteps', MINKOWSKI_ROTATION_PRESETS),
    ('physics_rotation_steps_slider', 'PhysicsRotationSteps', PHYSICS_ROTATION_PRESETS),
)


def _prefs(prefs):
    return prefs if prefs is not None else FreeCAD.ParamGet(PREFS_PATH)


def load_panel_settings(panel, prefs=None):
    prefs = _prefs(prefs)
    for fields, kind, read, write in (
        (FLOAT_FIELDS, 'Float', 'value', 'setValue'),
        (INT_FIELDS, 'Int', 'value', 'setValue'),
        (BOOL_FIELDS, 'Bool', 'isChecked', 'setChecked'),
        (COMBO_FIELDS, 'String', 'currentText', 'setCurrentText'),
        (TEXT_FIELDS, 'String', 'text', 'setText'),
    ):
        for name, key in fields.items():
            widget = getattr(panel, name, None)
            if widget is not None:
                value = getattr(prefs, 'Get' + kind)(key, getattr(widget, read)())
                getattr(widget, write)(value)
    for name, key, angles in ROTATION_FIELDS:
        widget = getattr(panel, name, None)
        if widget is not None:
            steps = prefs.GetInt(key, int(360 / angles[widget.value()]))
            if steps > 0:
                widget.setValue(closest_angle_index(angles, 360.0 / steps))
    for name, (prop, _) in SHEET_LENGTH_FIELDS.items():
        expression = prefs.GetString(prop + 'Expression', '')
        if expression:
            getattr(panel, name).setText(expression)
    font = prefs.GetString('FontPath', getattr(panel, 'selected_font_path', ''))
    panel.selected_font_path = font
    if hasattr(panel, 'font_label'):
        panel.font_label.setText(os.path.basename(font))


def save_panel_settings(panel, prefs=None):
    prefs = _prefs(prefs)
    for fields, kind, read in (
        (FLOAT_FIELDS, 'Float', 'value'),
        (INT_FIELDS, 'Int', 'value'),
        (BOOL_FIELDS, 'Bool', 'isChecked'),
        (COMBO_FIELDS, 'String', 'currentText'),
        (TEXT_FIELDS, 'String', 'text'),
    ):
        for name, key in fields.items():
            widget = getattr(panel, name, None)
            if widget is not None:
                getattr(prefs, 'Set' + kind)(key, getattr(widget, read)())
    for name, key, angles in ROTATION_FIELDS:
        widget = getattr(panel, name, None)
        if widget is not None:
            prefs.SetInt(key, int(360 / angles[widget.value()]))
    prefs.SetString('FontPath', getattr(panel, 'selected_font_path', ''))
    for name, (prop, _) in SHEET_LENGTH_FIELDS.items():
        prefs.SetString(prop + 'Expression', getattr(panel, name).expression())


def connect_panel_settings(panel, callback):
    """Connect after initial restoration so startup cannot overwrite saved values."""
    for fields, signal in (
        (FLOAT_FIELDS, 'valueChanged'), (INT_FIELDS, 'valueChanged'),
        (BOOL_FIELDS, 'toggled'), (COMBO_FIELDS, 'currentIndexChanged'),
        (TEXT_FIELDS, 'textChanged'),
    ):
        for name in fields:
            getattr(getattr(panel, name), signal).connect(callback)
    for name, _, _ in ROTATION_FIELDS:
        getattr(panel, name).valueChanged.connect(callback)


def _part_key(obj):
    # Document Uid survives save/reopen and Save As. Internal object names
    # distinguish same-labelled parts; new unsaved documents have distinct Uids.
    if obj is None or not getattr(obj, 'Document', None):
        return None
    identity = f'{obj.Document.Uid}:{obj.Name}'
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def load_part_settings(obj, prefs=None):
    key = _part_key(obj)
    if key is None:
        return {}
    try:
        data = json.loads(_prefs(prefs).GetGroup('Parts').GetString(key, '{}'))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    validators = {
        'quantity': lambda v: type(v) is int and 1 <= v <= 500,
        'rotation_steps': lambda v: type(v) is int and 0 <= v <= 360,
        'override_rotation': lambda v: type(v) is bool,
        'fill_sheet': lambda v: type(v) is bool,
        'up_direction': lambda v: v in ('Z+', 'Z-', 'Y+', 'Y-', 'X+', 'X-'),
        'grain_direction': lambda v: v in ('None', 'X', 'Y', 'Z'),
    }
    return {name: data[name] for name, valid in validators.items()
            if name in data and valid(data[name])}


def save_part_settings(obj, settings, prefs=None):
    key = _part_key(obj)
    if key is not None:
        _prefs(prefs).GetGroup('Parts').SetString(key, json.dumps(settings))
