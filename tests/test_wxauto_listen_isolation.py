import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


def _load_wxauto_module():
    package_name = "_isolated_wxauto"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package

    uia = types.ModuleType(f"{package_name}.uiautomation")
    uia.WindowControl = object
    sys.modules[uia.__name__] = uia

    modules = {}
    for name in ("languages", "utils", "elements", "errors", "color"):
        module = types.ModuleType(f"{package_name}.{name}")
        modules[name] = module
        sys.modules[module.__name__] = module
    modules["utils"].wxlog = logging.getLogger("wxauto-isolation-test")
    modules["elements"].WeChatBase = object
    modules["elements"].ChatWnd = object
    modules["errors"].TargetNotFoundError = RuntimeError

    path = Path(__file__).parents[1] / "wxauto" / "wxauto.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.wxauto", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class GetListenMessageIsolationTest(unittest.TestCase):
    def test_one_chat_exception_does_not_hide_other_chat_messages(self):
        module = _load_wxauto_module()
        failed = Mock(who="Failed", savepic=False, savefile=False, savevoice=False)
        failed.GetNewMessage = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("closed"))
        healthy = Mock(who="Healthy", savepic=False, savefile=False, savevoice=False)
        healthy.GetNewMessage = lambda **_kwargs: ["message"]
        wx = module.WeChat.__new__(module.WeChat)
        wx.listen = {"failed": failed, "healthy": healthy}

        messages = wx.GetListenMessage()

        self.assertEqual(messages, {healthy: ["message"]})
        self.assertIn("failed", wx.listen_errors)


if __name__ == "__main__":
    unittest.main()
