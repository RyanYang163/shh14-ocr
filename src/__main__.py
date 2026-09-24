#!/usr/bin/env python3
"""zipapp 入口。

构建时 ``tools/build_deb.py`` 会把整个 ``src/`` 打成单文件 ``bin/<appid>``
（shebang + zip），所以这个文件就是 deb 包里那个可执行程序的入口。

开发时也可以直接跑：
    python3 src/__main__.py --tcp 127.0.0.1:18014 --data-dir ./data
"""

import os
import sys

# 直接以脚本方式运行时（python src/__main__.py），zipapp 的 sys.path 布局不成立，
# 需要手动把 src/ 加进导入路径。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
