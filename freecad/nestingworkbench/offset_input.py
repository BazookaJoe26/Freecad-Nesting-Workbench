# SPDX-License-Identifier: LGPL-2.1-or-later
"""Native FreeCAD quantity editor with its built-in expression (fx) dialog."""

from PySide import QtCore, QtWidgets
import FreeCAD
import FreeCADGui

from .layout_offsets import evaluate_length


def _remove_parameters(doc, name):
    try:
        if FreeCAD.getDocument(doc.Name) is doc and doc.getObject(name):
            doc.removeObject(name)
    except (RuntimeError, ReferenceError):
        pass  # The document may have closed before the panel was destroyed.


class LengthInput(QtWidgets.QWidget):
    """Expose numeric values separately from formulas, using FreeCAD's own UI."""

    textChanged = QtCore.Signal(str)
    valueChanged = QtCore.Signal(float)

    def __init__(self, doc, prop, parent=None, value=0, minimum=None, maximum=None, label=None):
        super().__init__(parent)
        self._doc, self._prop = doc, prop
        self._minimum, self._maximum = minimum, maximum
        self._label = label or prop
        self._loading = False
        self._invalid_text = None
        self._parameters = doc.addObject("App::FeaturePython", "NestingOffsetInput")
        self._parameters.addProperty("App::PropertyDistance", prop, "Offsets")
        if self._parameters.ViewObject:
            self._parameters.ViewObject.ShowInTree = False
        name = self._parameter_name = self._parameters.Name
        self.destroyed.connect(lambda *_: _remove_parameters(doc, name))
        self._loader = FreeCADGui.UiLoader()
        self.spinbox = self._loader.createWidget("Gui::QuantitySpinBox", self)
        self.spinbox.setProperty("unit", "mm")
        self.spinbox.setProperty("singleStep", 1.0)
        if minimum is not None:
            self.spinbox.setProperty("minimum", minimum)
        if maximum is not None:
            self.spinbox.setProperty("maximum", maximum)
        self.binding = FreeCADGui.ExpressionBinding(self.spinbox)
        self.binding.bind(self._parameters, prop)
        self.binding.setAutoApply(True)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.spinbox)
        self.setFocusProxy(self.spinbox)
        self.spinbox.textChanged.connect(self._changed)
        self.spinbox.showFormulaDialog.connect(self._formula_dialog_changed)
        self.setValue(value)

    def _formula_dialog_changed(self, opened):
        if not opened:
            self._changed()

    def _changed(self, *_):
        if not self._loading:
            self._invalid_text = None
            self.textChanged.emit(self.text())
            self.valueChanged.emit(self.value())

    def value(self):
        """Return a number in mm, independent of FreeCAD's display locale."""
        return float(self.spinbox.property("rawValue"))

    def expression(self):
        return self._invalid_text or self.spinbox.property("expression") or ""

    def validatedValue(self, context=None):
        return evaluate_length(context or self._parameters, self.text(), self._label,
                               self._minimum, self._maximum)

    def text(self):
        """Canonical preference/job input; the visible editor displays the value."""
        if not self.spinbox.hasAcceptableInput():
            return self.spinbox.text()
        return self.expression() or f"{self.value():.17g} mm"

    def setValue(self, value):
        value = float(value)
        if self._minimum is not None:
            value = max(value, self._minimum)
        if self._maximum is not None:
            value = min(value, self._maximum)
        self._loading = True
        try:
            self._invalid_text = None
            self._parameters.setExpression(self._prop, None)
            setattr(self._parameters, self._prop, float(value))
            self.spinbox.setProperty("rawValue", float(value))
        finally:
            self._loading = False
        self.textChanged.emit(self.text())
        self.valueChanged.emit(self.value())

    def setExpression(self, expression):
        expression = expression.strip().lstrip("=").strip()
        if not expression:
            self.setValue(self.value())
            return
        value = evaluate_length(self._parameters, expression, self._label,
                                self._minimum, self._maximum)
        self._loading = True
        try:
            self._parameters.setExpression(self._prop, expression)
            self._invalid_text = None
            self.spinbox.setProperty("rawValue", value)
        finally:
            self._loading = False
        self.textChanged.emit(self.text())
        self.valueChanged.emit(self.value())

    def setText(self, text):
        """Restore earlier preferences, including the former free-text fields."""
        text = text.strip().lstrip("=").strip() or "0 mm"
        try:
            quantity = FreeCAD.Units.Quantity(text)
            if quantity.Unit not in (FreeCAD.Units.Unit(), FreeCAD.Units.Quantity("1 mm").Unit):
                raise ValueError("expected a length")
            self.setValue(quantity.Value)
        except Exception:
            try:
                self.setExpression(text)
            except ValueError:
                # Preserve unavailable saved variables for validation.
                self._invalid_text = text
                self.spinbox.setToolTip(text)
                self.textChanged.emit(text)

    def cleanup(self):
        _remove_parameters(self._doc, self._parameter_name)


# Keep the original import available for existing offset callers.
OffsetInput = LengthInput
