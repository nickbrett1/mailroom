"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import mailroom

    assert mailroom.__version__
