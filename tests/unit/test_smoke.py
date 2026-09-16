import unifi_mcp


def test_package_imports_with_version() -> None:
    assert isinstance(unifi_mcp.__version__, str)
    assert unifi_mcp.__version__
