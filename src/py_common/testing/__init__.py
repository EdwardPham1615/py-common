"""Shared test utilities: persistence fakes, a JWT token factory, route assertions.

Import submodules directly so consumers only pay for the extras they use:

- ``py_common.testing.fakes`` — no extra dependencies beyond core
- ``py_common.testing.tokens`` — requires the ``security`` extra
- ``py_common.testing.routes`` — requires the ``http`` extra
"""
