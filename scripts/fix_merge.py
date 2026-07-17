"""DEPRECATED:切段逻辑已合入 download_assets_multi / concat_videos。

使用 render_cn_chat.py 的 --segment + --cut 参数替代:

  python scripts/render_cn_chat.py \
    --download <VOD_URL> \
    --segment 1:21:13-1:38:06 \
    --segment 1:42:05-2:17:43 \
    --cut 21:01-22:59 \
    --download-output-fps 60 \
    --download-encoder auto

本文件仅保留用于历史参考，不再维护。
"""

import sys

print("fix_merge.py 已废弃，请改用 render_cn_chat.py --segment --cut", file=sys.stderr)
sys.exit(1)
