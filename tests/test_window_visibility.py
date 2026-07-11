import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


def _load_utils(win32gui, win32con):
    package_name = "_isolated_visibility"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.uiautomation"] = types.ModuleType(
        f"{package_name}.uiautomation")

    dependencies = {
        "win32clipboard": types.ModuleType("win32clipboard"),
        "win32process": types.ModuleType("win32process"),
        "win32gui": win32gui,
        "win32api": types.ModuleType("win32api"),
        "win32con": win32con,
        "pyperclip": types.ModuleType("pyperclip"),
        "psutil": types.ModuleType("psutil"),
        "winreg": types.ModuleType("winreg"),
    }
    pil = types.ModuleType("PIL")
    pil.ImageGrab = types.SimpleNamespace()
    dependencies["PIL"] = pil
    for name, module in dependencies.items():
        sys.modules[name] = module

    path = Path(__file__).parents[1] / "wxauto" / "utils.py"
    spec = importlib.util.spec_from_file_location(f"{package_name}.utils", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WindowVisibilityTest(unittest.TestCase):
    def test_background_visible_window_is_not_activated_or_restacked(self):
        gui = types.ModuleType("win32gui")
        gui.IsWindow = Mock(return_value=True)
        gui.IsIconic = Mock(return_value=False)
        gui.IsWindowVisible = Mock(return_value=True)
        gui.ShowWindow = Mock()
        gui.SetForegroundWindow = Mock()
        gui.BringWindowToTop = Mock()
        con = types.ModuleType("win32con")
        con.SW_SHOWNOACTIVATE = 4
        module = _load_utils(gui, con)

        self.assertTrue(module.EnsureWindowVisibleNoActivate(123))
        gui.ShowWindow.assert_not_called()
        gui.SetForegroundWindow.assert_not_called()
        gui.BringWindowToTop.assert_not_called()

    def test_minimized_window_is_restored_without_activation(self):
        gui = types.ModuleType("win32gui")
        gui.IsWindow = Mock(return_value=True)
        gui.IsIconic = Mock(side_effect=[True, False])
        gui.IsWindowVisible = Mock(return_value=True)
        gui.ShowWindow = Mock()
        con = types.ModuleType("win32con")
        con.SW_SHOWNOACTIVATE = 4
        module = _load_utils(gui, con)

        self.assertTrue(module.EnsureWindowVisibleNoActivate(456))
        gui.ShowWindow.assert_called_once_with(456, con.SW_SHOWNOACTIVATE)

    def test_activation_joins_input_threads_and_confirms_foreground(self):
        gui = types.ModuleType("win32gui")
        gui.IsWindow = Mock(return_value=True)
        gui.IsIconic = Mock(return_value=False)
        gui.IsWindowVisible = Mock(return_value=True)
        gui.GetForegroundWindow = Mock(side_effect=[999, 999, 123])
        gui.BringWindowToTop = Mock()
        gui.SetForegroundWindow = Mock()
        con = types.ModuleType("win32con")
        con.SW_RESTORE = 9
        con.SW_SHOW = 5
        module = _load_utils(gui, con)

        user32 = types.SimpleNamespace(
            GetWindowThreadProcessId=Mock(side_effect=lambda hwnd, _pid: {
                123: 20, 999: 30}[hwnd]),
            AttachThreadInput=Mock(return_value=1),
            keybd_event=Mock(),
        )
        module.ctypes.windll = types.SimpleNamespace(
            user32=user32,
            kernel32=types.SimpleNamespace(GetCurrentThreadId=Mock(return_value=10)),
        )

        self.assertTrue(module.ActivateWindow(123))
        self.assertEqual(user32.keybd_event.call_count, 2)
        self.assertEqual(
            [call.args for call in user32.AttachThreadInput.call_args_list[:3]],
            [(10, 30, True), (10, 20, True), (20, 30, True)],
        )
        self.assertEqual(
            [call.args for call in user32.AttachThreadInput.call_args_list[-3:]],
            [(20, 30, False), (10, 20, False), (10, 30, False)],
        )

    def test_activation_failure_is_bounded_to_three_attempts(self):
        gui = types.ModuleType("win32gui")
        gui.IsWindow = Mock(return_value=True)
        gui.IsIconic = Mock(return_value=False)
        gui.IsWindowVisible = Mock(return_value=True)
        gui.GetForegroundWindow = Mock(return_value=999)
        gui.BringWindowToTop = Mock()
        gui.SetForegroundWindow = Mock()
        con = types.ModuleType("win32con")
        con.SW_RESTORE = 9
        con.SW_SHOW = 5
        module = _load_utils(gui, con)
        module.time.sleep = Mock()
        user32 = types.SimpleNamespace(
            GetWindowThreadProcessId=Mock(side_effect=lambda hwnd, _pid: {
                123: 20, 999: 30}[hwnd]),
            AttachThreadInput=Mock(return_value=1),
            keybd_event=Mock(),
        )
        module.ctypes.windll = types.SimpleNamespace(
            user32=user32,
            kernel32=types.SimpleNamespace(GetCurrentThreadId=Mock(return_value=10)),
        )

        self.assertFalse(module.ActivateWindow(123))
        self.assertEqual(gui.SetForegroundWindow.call_count, 3)
        self.assertEqual(module.time.sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
