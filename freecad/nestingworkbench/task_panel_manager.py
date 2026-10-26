# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/task_panel_manager.py

"""
This module contains the NestingTaskPanel class, which is responsible for
creating, showing, and managing the lifecycle of the FreeCAD Task Panel.
"""

import FreeCAD
import FreeCADGui
from PySide import QtCore

# Import the UI panel class
from .Tools.Nesting.ui_nesting import NestingPanel

class NestingTaskPanel:
    """Manages the FreeCAD Task Panel dialog."""
    def __init__(self, cleanup_callback=None):
        self.form = NestingPanel()
        self._cleanup_callback = cleanup_callback
        self._reject_pending = False
        self.task_widget = FreeCADGui.Control.showDialog(self)
    
    def accept(self):
        """Called by FreeCAD when the dialog's 'OK' button is clicked."""
        accepted = True
        try:
            if hasattr(self.form, "accept"):
                accepted = self.form.accept()
            return accepted is not False
        finally:
            if accepted is not False:
                self.cleanup()

    def reject(self):
        """Called by FreeCAD when the dialog is closed or 'Cancel' is clicked."""
        rejected = True
        try:
            if hasattr(self.form, "reject"):
                rejected = self.form.reject()
            if rejected is False:
                # Keep the panel and its signal handlers alive until the worker
                # has removed its sandbox; closing it early can strand the worker.
                if not getattr(self, '_reject_pending', False):
                    self._reject_pending = True
                    QtCore.QTimer.singleShot(50, self._finish_reject)
                return False
            return True
        finally:
            if rejected is not False:
                self.cleanup()

    def _finish_reject(self):
        self._reject_pending = False
        if self.reject():
            FreeCADGui.Control.closeDialog()

    def cleanup(self):
        """Resets the command's panel instance to allow it to be reopened."""
        if self._cleanup_callback:
            self._cleanup_callback()
