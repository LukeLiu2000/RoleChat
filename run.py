"""RoleChat 启动入口（源码运行与 PyInstaller 打包后均可使用）。

数据目录与配置文件的位置：
- 源码运行：项目根目录下的 data/ 与 config.json
- exe 运行：exe 所在目录下的 data/ 与 config.json（请把 exe 放在项目目录内运行，
  以满足“所有数据保存在工作区内”的要求）
可用环境变量：ROLECHAT_DATA_DIR、ROLECHAT_CONFIG 覆盖路径（测试用）。
命令行参数：--no-browser 启动时不自动打开浏览器；--port N 指定端口。
"""
import os
import sys

_PROJECT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from app import server  # noqa: E402


def _project_dir():
    if getattr(sys, "frozen", False):  # PyInstaller onefile：exe 所在目录
        return os.path.dirname(sys.executable)
    return _PROJECT


def main():
    project = _project_dir()
    data_root = os.environ.get("ROLECHAT_DATA_DIR") or os.path.join(project, "data")
    config_path = os.environ.get("ROLECHAT_CONFIG") or os.path.join(project, "config.json")
    if getattr(sys, "frozen", False):
        static_root = os.path.join(getattr(sys, "_MEIPASS", project), "static")  # 打包内的静态资源
    else:
        static_root = os.path.join(project, "app", "static")
    port = None
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            port = None
    server.main(static_root=static_root, data_root=data_root, config_path=config_path,
                no_browser="--no-browser" in sys.argv, port=port)


if __name__ == "__main__":
    main()
