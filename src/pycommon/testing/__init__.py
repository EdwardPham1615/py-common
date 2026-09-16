"""Shared test utilities: persistence fakes, a JWT token factory, route assertions.

Import submodules directly so consumers only pay for the extras they use:

- ``pycommon.testing.fakes`` — no extra dependencies beyond core
- ``pycommon.testing.tokens`` — requires the ``security`` extra
- ``pycommon.testing.routes`` — requires the ``http`` extra
"""
