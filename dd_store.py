# -*- coding: utf-8 -*-
"""图片落地、24 小时清理、每日额度计数。"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Store:
    def __init__(self, data_dir: str, out_dir: str, keep_hours: int = 24):
        self.data_dir = Path(data_dir)
        self.out_dir = Path(out_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.keep_seconds = keep_hours * 3600
        self.counter_file = self.data_dir / "daily_count.json"

    # ---------- 每日额度 ----------

    def _load(self) -> dict:
        try:
            return json.loads(self.counter_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict):
        try:
            self.counter_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def used_today(self, user_id: str) -> int:
        day = time.strftime("%Y-%m-%d")
        data = self._load()
        return int(data.get(day, {}).get(str(user_id), 0))

    def remaining_today(self, user_id: str, limit: int) -> int | None:
        if limit <= 0:
            return None
        return max(0, limit - self.used_today(user_id))

    def incr_today(self, user_id: str, n: int = 1):
        day = time.strftime("%Y-%m-%d")
        data = self._load()
        # 只保留最近 7 天
        for k in list(data.keys()):
            if k != day and k < time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400)):
                data.pop(k, None)
        bucket = data.setdefault(day, {})
        bucket[str(user_id)] = int(bucket.get(str(user_id), 0)) + n
        self._save(data)

    # ---------- 文件 ----------

    def new_path(self, ext: str = ".png") -> Path:
        return self.out_dir / f"{int(time.time())}_{int(time.time() * 1000) % 1000}{ext}"

    def cleanup(self):
        now = time.time()
        try:
            for p in self.out_dir.iterdir():
                if p.is_file() and now - p.stat().st_mtime > self.keep_seconds:
                    p.unlink(missing_ok=True)
        except Exception:
            pass
