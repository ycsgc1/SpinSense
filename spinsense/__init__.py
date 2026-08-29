"""Domain logic shared by both SpinSense processes.

The engine (`core/`) and the web backend (`gui/`) run as separate processes and
were, until now, separate import roots — so anything both needed got written
twice. That is how the *SOUR* mislabelling became possible: album-title
vocabulary lived only in `gui/reconcile.py`, and the engine, which does the
metadata lookup, had no access to it.

Everything here is pure or purely-network domain code with no framework
dependency, importable from either side. Both entry points get the repository
root on `sys.path` (`PYTHONPATH=/app` in the image, `conftest.py` under test).
"""
