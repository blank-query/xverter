"""xverter - Xbox 360 game format converter.

Any format in, any format out, verified at every step.
"""
__version__ = "1.4.0"

from .api import ConvertError, convert, probe

__all__ = ["convert", "probe", "ConvertError", "__version__"]
