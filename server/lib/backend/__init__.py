"""Backends as strategies: one class per coding CLI.

The host never spells a backend's name; it asks the backend object to do the
work. ``base.Backend`` lists the operations the host needs, the per-CLI
modules implement them, ``registry`` hands out the singletons and
``lib.backends`` stays the import path callers use. The contract is
``docs/architecture/backend-strategy.md``.
"""
