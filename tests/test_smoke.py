"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import mailroom  # noqa: F401

    assert mailroom.__version__
