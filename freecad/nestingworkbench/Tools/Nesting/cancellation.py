# SPDX-License-Identifier: LGPL-2.1-or-later
"""Cooperative cancellation shared by document and geometry operations."""


class NestingCancelled(Exception):
    """An intentional abort, never a geometry failure to log or cache."""


def check_cancel(cancel_callback):
    if cancel_callback is not None and cancel_callback():
        raise NestingCancelled()
