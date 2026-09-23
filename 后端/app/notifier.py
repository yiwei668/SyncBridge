"""Windows 系统通知（Toast）三层降级。

设计要点：
- 主方案 windows-toasts（WinRT / Action Center 原生，带声音、可点击打开客户端），
  但非打包桌面应用必须注册 AUMID + 开始菜单快捷方式才会被系统识别并显示，
  因此 ensure_aumid_registered() 在首次调用时自动完成一次性注册（依赖 pywin32）。
- 兜底 BurntToast（PowerShell 模块，自身会注册 AUMID，开箱即用），
  再兜底 win10toast（旧托盘气泡，Win10+ 可能不显示，仅作最后手段）。
- 仅在 sys.platform == "win32" 下启用真实通知；其他平台（如 Linux 沙箱）仅打印，便于测试。
"""

from __future__ import annotations

import os
import subprocess
import sys

APP_ID = "SyncBridge"
AUMID = "SyncBridge.SyncBridgeNotification"

_SHORTCUT_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs", "SyncBridge",
)
_SHORTCUT_PATH = os.path.join(_SHORTCUT_DIR, "SyncBridge.lnk")

# AppUserModel.ID 的 PROPERTYKEY：{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, pid=5
_AUMID_PKEY = ("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)

_aumid_done = False


def ensure_aumid_registered() -> None:
    """为非打包的 Python 进程注册 AUMID + 开始菜单快捷方式（一次性）。

    仅 Windows 下执行；缺少 pywin32 时静默跳过——此时 windows-toasts 可能弹不出，
    会自然降级到 BurntToast / win10toast。
    """
    global _aumid_done
    if _aumid_done:
        return
    _aumid_done = True
    if sys.platform != "win32":
        return
    try:
        import pythoncom  # type: ignore
        from win32com.propsys import propsys  # type: ignore
        import win32com.client  # type: ignore
    except Exception:
        return
    try:
        os.makedirs(_SHORTCUT_DIR, exist_ok=True)
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(_SHORTCUT_PATH)
        shortcut.TargetPath = sys.executable
        shortcut.WorkingDirectory = os.path.dirname(sys.executable)
        shortcut.Description = "SyncBridge 通知源"
        shortcut.Save()

        pstore = propsys.SHGetPropertyStoreFromParsingName(
            _SHORTCUT_PATH, None, propsys.GPS_READWRITE, pythoncom.IID_IPropertyStore
        )
        pstore.SetValue(_AUMID_PKEY, propsys.PROPVARIANTType(AUMID))
        pstore.Commit()
    except Exception:
        # 注册失败不影响降级链路
        pass


def _toast_winrt(title: str, message: str, launch: str | None) -> None:
    from windows_toasts import ToastText2, WindowsToaster  # type: ignore

    ensure_aumid_registered()
    toaster = WindowsToaster(APP_ID)
    toast = ToastText2()
    toast.text_field = title
    toast.text_field_2 = message
    if launch:
        toast.launch = launch
    toaster.show_toast(toast)


def _toast_burnt(title: str, message: str, launch: str | None) -> None:  # noqa: ARG001
    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'").replace("\n", "  ")
    ps = (
        "Import-Module BurntToast -ErrorAction Stop; "
        f'New-BurntToastNotification -Text "{safe_title}", "{safe_msg}"'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _toast_win10(title: str, message: str, launch: str | None) -> None:  # noqa: ARG001
    from win10toast import ToastNotifier  # type: ignore

    ToastNotifier().show_toast(title, message, duration=12, threaded=True)


def show_windows_toast(title: str, message: str, launch: str | None = None) -> bool:
    """弹出 Windows 系统通知。成功返回 True，全部方案失败返回 False。

    顺序：windows-toasts → BurntToast → win10toast。非 Windows 平台打印到 stdout。
    """
    if sys.platform != "win32":
        print(f"[notifier][debug] {title} | {message}")
        return True

    errors: list[tuple[str, str]] = []
    for fn in (_toast_winrt, _toast_burnt, _toast_win10):
        try:
            fn(title, message, launch)
            return True
        except Exception as exc:  # 单方案失败，尝试下一个
            errors.append((fn.__name__, repr(exc)))
    print(f"[notifier] 所有通知方案均失败：{errors}")
    return False
