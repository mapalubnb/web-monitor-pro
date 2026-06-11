import builtins

import pytest

from src.main import App


def test_start_ws_raises_when_lark_sdk_is_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "lark_oapi":
            raise ImportError("missing lark")
        return real_import(name, *args, **kwargs)

    app = object.__new__(App)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="lark-oapi"):
        app._start_ws()
