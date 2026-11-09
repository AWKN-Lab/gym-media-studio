"""edge-tts 合成子进程（被 core/voice.py 调用，不要把 asyncio 放回主进程）。

为什么要单开一个进程：Windows 上某些 Python 构建建不出本地回环
（socket.socketpair 回退实现卡在 socket.accept()），asyncio 建事件循环就会永久挂住。
放进子进程后，父进程可以用硬超时把它 kill 掉，整条出片流程不会被一个配音拖死。

用法：python edge_worker.py <job.json>
job.json: {"text": "...", "voice": "zh-CN-YunxiNeural", "rate": "+5%", "out": "...mp3"}
退出码：0 成功 / 2 参数错 / 3 未装 edge-tts / 4 合成报错 / 5 产出无效
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python edge_worker.py <job.json>", file=sys.stderr)
        return 2
    try:
        job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"读不了任务文件：{type(e).__name__}: {e}", file=sys.stderr)
        return 2
    try:
        import edge_tts
    except ImportError as e:
        print(f"导入 edge_tts 失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 3

    async def _go():
        comm = edge_tts.Communicate(job["text"], job["voice"], rate=job.get("rate", ""))
        await comm.save(job["out"])

    try:
        asyncio.run(_go())
    except Exception as e:
        print(f"合成报错：{type(e).__name__}: {e}", file=sys.stderr)
        return 4
    out = Path(job["out"])
    if not (out.exists() and out.stat().st_size > 1000):
        print(f"没产出有效音频：{out}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
