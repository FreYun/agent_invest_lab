"""pytest 配置: 将 scripts/ 加入 sys.path, 使测试能直接 import scoring 等模块。"""

import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
