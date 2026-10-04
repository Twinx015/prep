"""Entry point for `python -m harpy`.

This doubles as the PyInstaller entry script (see harpy.spec). A frozen entry
script is executed as ``__main__`` with no package context, so the package has
to be imported by absolute name here -- ``harpy/main.py`` uses relative imports
and therefore cannot be frozen on its own.
"""

import sys

from harpy.main import main

if __name__ == "__main__":
    sys.exit(main())