"""Put the repository root on sys.path for this test suite.

Both processes import the shared `spinsense` package while running from their
own directory — the engine as a script from `core/`, the backend as a uvicorn
app from `gui/`. In the image `PYTHONPATH=/app` supplies this; under test,
pytest collects conftest files before anything else.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
