"""Chrome CDP 启动/检测工具

用于检查 Chrome 远程调试端口状态，或自动启动 Chrome。

用法:
  python scripts/chrome.py          # 检测状态，未启动则自动启动
  python scripts/chrome.py check    # 仅检测状态
  python scripts/chrome.py path     # 显示 Chrome 可执行文件路径
"""

import sys
import os
import subprocess
import json
import urllib.request
import urllib.error


CHROME_PATHS = [
    # Windows 常见安装路径
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

CDP_PORT = 9222
CHROME_DATA_DIR = os.path.join(os.path.expanduser("~"), "chrome-profile")


def sp(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode(), **kwargs, flush=True)


def find_chrome():
    """查找系统已安装的 Chrome 可执行文件"""
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    # Windows: 尝试注册表查询
    if sys.platform == "win32":
        try:
            import winreg
            for key in [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                r"SOFTWARE\Google\Chrome\Path",
            ]:
                for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                    try:
                        with winreg.OpenKey(root, key) as reg_key:
                            path, _ = winreg.QueryValueEx(reg_key, "")
                            if os.path.isfile(path):
                                return path
                    except (OSError, FileNotFoundError):
                        continue
        except ImportError:
            pass
    return None


def check_cdp(port=CDP_PORT):
    """检查 Chrome CDP 是否可用，返回 (ok: bool, info: str)"""
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=3)
        data = json.loads(resp.read().decode())
        browser = data.get("Browser", "unknown")
        return True, browser
    except urllib.error.URLError:
        return False, "CDP port unreachable"
    except Exception as e:
        return False, str(e)


def launch_chrome(chrome_path, port=CDP_PORT):
    """启动 Chrome 并开启远程调试端口"""
    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={CHROME_DATA_DIR}",
    ]
    sp(f"Launching Chrome: {chrome_path}")
    sp(f"  Port: {port}, User data: {CHROME_DATA_DIR}")
    try:
        if sys.platform == "win32":
            subprocess.Popen(cmd, close_fds=True)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        sp(f"Error launching Chrome: {e}")
        return False


def main():
    args = [a.lower() for a in sys.argv[1:]]

    # 仅显示路径
    if "path" in args:
        chrome = find_chrome()
        if chrome:
            sp(chrome)
        else:
            sp("Chrome not found")
            sys.exit(1)
        return

    # 仅检测状态
    ok, info = check_cdp()
    if ok:
        sp(f"Chrome CDP is running: {info}")
        sp(f"  Connect at: http://localhost:{CDP_PORT}")
        if "check" not in args:
            sp("\nReady to use. Run:")
            sp(f'  python scripts/main.py sl "keyword | year | sort | count"')
        return

    if "check" in args:
        sp("Chrome CDP is NOT running.")
        sp(f"  Start Chrome manually with:")
        chrome = find_chrome()
        if chrome:
            sp(f'  "{chrome}" --remote-debugging-port={CDP_PORT} --remote-allow-origins=* --user-data-dir="{CHROME_DATA_DIR}"')
        else:
            sp(f"  google-chrome --remote-debugging-port={CDP_PORT}")
        sys.exit(1)

    # 检测 + 自动启动
    sp("Chrome CDP not detected. Attempting to start...")
    chrome = find_chrome()
    if not chrome:
        sp("ERROR: Chrome not found. Please install Chrome or start it manually:")
        sp(f"  google-chrome --remote-debugging-port={CDP_PORT} --remote-allow-origins=*")
        sys.exit(1)

    if launch_chrome(chrome):
        sp("Chrome launched. Waiting for CDP...")
        import time
        for i in range(10):
            time.sleep(1)
            ok, info = check_cdp()
            if ok:
                sp(f"Chrome CDP ready: {info}")
                return
        sp("Timeout: Chrome started but CDP not responding.")
        sp("Try running manually:")
        sp(f'  "{chrome}" --remote-debugging-port={CDP_PORT} --remote-allow-origins=* --user-data-dir="{CHROME_DATA_DIR}"')
        sys.exit(1)
    else:
        sp("Failed to launch Chrome.")
        sys.exit(1)


if __name__ == "__main__":
    main()
