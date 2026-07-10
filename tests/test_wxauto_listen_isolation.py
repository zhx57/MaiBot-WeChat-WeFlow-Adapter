import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def _load_elements_module():
    package_name = "_isolated_elements"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package

    uia = types.ModuleType(f"{package_name}.uiautomation")
    uia.WindowControl = Mock
    uia.PaneControl = Mock(return_value=SimpleNamespace())
    sys.modules[uia.__name__] = uia

    for name in ("languages", "utils", "color", "errors"):
        module = types.ModuleType(f"{package_name}.{name}")
        sys.modules[module.__name__] = module
    sys.modules[f"{package_name}.utils"].wxlog = logging.getLogger(
        "elements-isolation-test")

    path = Path(__file__).parents[1] / "wxauto" / "elements.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.elements", path)
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

    def test_rebuilt_chat_waits_for_stable_message_baseline(self):
        module = _load_elements_module()

        def item(runtime_id):
            return SimpleNamespace(
                ControlTypeName="ListItemControl",
                GetRuntimeId=lambda: [runtime_id],
            )

        history = item(1)
        new_message = item(2)
        chat = module.ChatWnd.__new__(module.ChatWnd)
        chat.who = "Target"
        chat.usedmsgid = []
        chat._baseline_pending = True
        chat._baseline_candidate = None
        chat._baseline_stable_polls = 0
        chat.C_MsgList = SimpleNamespace(GetChildren=Mock(side_effect=[
            [], [history], [history], [history, new_message],
        ]))
        chat._getmsgs = Mock(return_value=["new"])

        self.assertEqual(chat.GetNewMessage(), [])
        self.assertEqual(chat.GetNewMessage(), [])
        self.assertEqual(chat.GetNewMessage(), [])
        self.assertEqual(chat.GetNewMessage(), ["new"])
        chat._getmsgs.assert_called_once_with([new_message], False, False, False)


if __name__ == "__main__":
    unittest.main()
