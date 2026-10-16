#!/usr/bin/env python
"""创建本地开发用的示例账号（普通用户 / 管理员）。

用法（在仓库根目录执行）：
    backend/.venv/Scripts/python.exe scripts/seed_users.py     # Windows
    backend/.venv/bin/python     scripts/seed_users.py         # macOS / Linux

行为：
  - 幂等：以邮箱为准，已存在则只重置密码，不会重复建号；
  - 不触碰 .env 里 ADMIN_EMAIL / ADMIN_PASSWORD 对应的那个初始管理员；
  - 结束时用 bcrypt 复核一遍，确认密码真的能登录。

仅用于本地开发。脚本依赖 STORE_BACKEND=sql，内存模式（memory）下进程退出即丢失。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
# Settings 的 env_file=".env" 是相对路径，必须先切到 backend/ 才能读到 DATABASE_URL
os.chdir(BACKEND_DIR)

from app.config import get_settings  # noqa: E402
from app.core.security import hash_password, verify_password  # noqa: E402
from app.store import get_store  # noqa: E402

# ---------------------------------------------------------------- 示例账号清单
# 按需增删；email 是幂等判断依据，username 用于登录展示
ACCOUNTS: list[dict[str, str]] = [
    {
        "username": "demo_admin",
        "email": "demo.admin@agent.local",
        "password": "Admin@123456",
        "role": "admin",
    },
    {
        "username": "demo_user",
        "email": "demo.user@agent.local",
        "password": "User@123456",
        "role": "user",
    },
    {
        "username": "alice",
        "email": "alice@agent.local",
        "password": "Alice@123456",
        "role": "user",
    },
    {
        "username": "bob",
        "email": "bob@agent.local",
        "password": "Bob@123456",
        "role": "user",
    },
]


async def _seed() -> int:
    settings = get_settings()

    if settings.store_backend.strip().lower() != "sql":
        print(f"[X]  当前 STORE_BACKEND={settings.store_backend}，内存模式下账号无法持久化。")
        print("     请在 backend/.env 中设置 STORE_BACKEND=sql 后重试。")
        return 1

    store = get_store()
    try:
        existing = await store.list_users()
    except Exception as exc:  # noqa: BLE001 - 命令行脚本，直接把原因打给使用者
        print(f"[X]  连接数据库失败：{exc}")
        print("     若提示表不存在，先执行：cd backend && .venv/Scripts/python -m alembic upgrade head")
        return 1

    by_email = {user.email.lower(): user for user in existing}
    target = settings.database_url.split("@")[-1]
    print(f"==> 数据库 {target}，现有账号 {len(existing)} 个")

    created: list[tuple[str, str, str, str]] = []
    for acc in ACCOUNTS:
        user = by_email.get(acc["email"].lower())
        if user is None:
            user = await store.create_user(
                username=acc["username"],
                email=acc["email"],
                password_hash=hash_password(acc["password"]),
                role=acc["role"],
            )
            state = "已创建"
        else:
            await store.update_password(user.id, hash_password(acc["password"]))
            state = "已存在"
            if user.role != acc["role"]:
                state = f"已存在（角色为 {user.role}，未改动）"
        created.append((state, user.username, user.email, acc["password"]))

    print("\n==> 复核")
    failed = 0
    for state, username, email, password in created:
        user = await store.get_user_by_email(email)
        ok = user is not None and verify_password(password, user.password_hash)
        failed += 0 if ok else 1
        print(f"  [{'OK' if ok else 'X '}]  {state:<28} {username:<12} {email:<26} {password}  ({user.role})")

    if failed:
        print(f"\n[X]  {failed} 个账号复核失败")
        return 1

    print("\n[OK] 示例账号就绪，可用「用户名或邮箱 + 密码」登录")
    print("     提示：以上密码仅用于本地开发，切勿用于任何线上环境。")
    return 0


async def main() -> int:
    settings = get_settings()
    try:
        return await _seed()
    finally:
        # 不释放连接池的话，事件循环关闭时 aiomysql 会在 __del__ 里抛 "Event loop is closed"
        if settings.store_backend.strip().lower() == "sql":
            from app.db.base import dispose_engine

            await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
