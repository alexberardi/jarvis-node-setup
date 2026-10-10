"""Import openWakeWord without its training-only dependencies.

``openwakeword/__init__.py`` (0.6.0) eagerly does
``from openwakeword.custom_verifier_model import train_custom_verifier``,
and that module imports scipy and scikit-learn at top level. Custom
verifier models are a training-time feature; the node never passes
``custom_verifier_models`` to ``Model``. On a Pi the two imports cost
~25 MB RSS and ~2.5 s of startup for nothing.

Importing this module first puts a stub in ``sys.modules`` so the
package ``__init__`` binds the stub instead. Import it before any
``import openwakeword...`` in a node process::

    import core.oww_import  # noqa: F401  (must precede openwakeword)
    from openwakeword.model import Model

The stub never replaces an already-imported real module, so tools that
import openwakeword first (training scripts) keep the real thing.
"""

import sys
import types

_MODULE = "openwakeword.custom_verifier_model"


def _train_custom_verifier(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError(
        "openwakeword custom verifier training is not available on the node "
        "(core.oww_import stubs it to keep scipy/scikit-learn out of memory); "
        "train verifiers with tools/wake_model_training instead"
    )


def make_stub() -> types.ModuleType:
    """Build the stand-in for ``openwakeword.custom_verifier_model``."""
    stub = types.ModuleType(_MODULE)
    stub.__doc__ = "Stub installed by core.oww_import (training-only module)."
    stub.train_custom_verifier = _train_custom_verifier  # type: ignore[attr-defined]
    return stub


def install_stub() -> None:
    """Register the stub unless the module is already loaded."""
    if _MODULE not in sys.modules:
        sys.modules[_MODULE] = make_stub()


install_stub()
