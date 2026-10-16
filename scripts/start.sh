#!/usr/bin/env bash
#
# Agent App 一键启动脚本（macOS / Linux 版，Windows 请用 scripts/start.ps1）
#
# 用法：
#   ./scripts/start.sh                  # 准备环境并启动前后端
#   ./scripts/start.sh --skip-install   # 跳过依赖安装（仍做一次轻量探测）
#   ./scripts/start.sh --target backend # 只启动后端
#   ./scripts/start.sh --setup          # 只准备环境，不启动
#   ./scripts/start.sh --stop           # 停止上次启动的服务
#
# 说明：启动前检查端口占用并同步端口到 .env；日志在 .run/*.log；
#       启动后轮询健康检查，Ctrl+C 统一停止（Ctrl+C 退出码 130，服务异常退出 1）。
#
set -euo pipefail

TARGET="both"
BACKEND_PORT=8000
FRONTEND_PORT=3000
SKIP_INSTALL=0
SETUP_ONLY=0
DO_STOP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)       TARGET="$2"; shift 2 ;;
    --backend-port) BACKEND_PORT="$2"; shift 2 ;;
    --frontend-port) FRONTEND_PORT="$2"; shift 2 ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --setup)        SETUP_ONLY=1; shift ;;
    --stop)         DO_STOP=1; shift ;;
    -h|--help)      sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
RUN_DIR="$ROOT/.run"
PID_FILE="$RUN_DIR/pids"

VENV_DIR="$BACKEND_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

C_CYAN='\033[36m'; C_GREEN='\033[32m'; C_YELLOW='\033[33m'; C_RED='\033[31m'; C_GRAY='\033[90m'; C_OFF='\033[0m'
step() { printf "\n${C_CYAN}==> %s${C_OFF}\n" "$1"; }
ok()   { printf "  ${C_GREEN}[OK]${C_OFF} %s\n" "$1"; }
warn() { printf "  ${C_YELLOW}[!]${C_OFF}  %s\n" "$1"; }
err()  { printf "  ${C_RED}[X]${C_OFF}  %s\n" "$1"; }

mkdir -p "$RUN_DIR"

# ---------------------------------------------------------------------------
# 端口检查（避免「端口被占用 → 服务启动即静默退出」）
# ---------------------------------------------------------------------------
port_holder() {
  # 输出占用者描述；查不到则无输出（始终返回 0，避免 set -e 误伤）
  local port="$1" out
  command -v lsof >/dev/null 2>&1 || return 0
  out="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -n "$out" ]] || return 0
  printf '%s\n' "$out" | awk 'NR==2 {printf "%s (PID %s)", $1, $2}'
  return 0
}

port_in_use() {
  local port="$1" out
  if command -v ss >/dev/null 2>&1; then
    out="$(ss -ltn 2>/dev/null || true)"
  elif command -v netstat >/dev/null 2>&1; then
    out="$(netstat -ltn 2>/dev/null || true)"
  elif command -v lsof >/dev/null 2>&1; then
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then return 0; else return 1; fi
  else
    return 1
  fi

  # 先取全量输出再匹配，避免 grep -q 提前退出触发 pipefail
  if grep -qE "[:.]$port[[:space:]]" <<<"$out"; then
    return 0
  fi
  return 1
}

assert_port_available() {
  local port="$1" label="$2" opt="$3"
  port_in_use "$port" || return 0
  local who
  who="$(port_holder "$port")"
  [[ -n "$who" ]] || who='未知进程'
  echo
  err "$label 端口 $port 已被占用（$who）。请先释放该端口，或用 $opt 指定其他端口"
  exit 1
}

# ---------------------------------------------------------------------------
# .env 读写（端口联动 + 默认密钥检查）
# ---------------------------------------------------------------------------
get_env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  sed -n -E "s|^[[:space:]]*${key}[[:space:]]*=(.*)$|\1|p" "$file" | head -n 1
}

set_env_value() {
  local file="$1" key="$2" value="$3"
  [[ -f "$file" ]] || return 0
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    # 用 | 作分隔符，避免 URL 里的 / 与 sed 冲突
    sed -i.bak -E "s|^[[:space:]]*${key}[[:space:]]*=.*$|${key}=${value}|" "$file"
    rm -f "${file}.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# 仅当原值为本机地址时改写；用户自定义过的值只提示、不覆盖
sync_local_url() {
  local file="$1" key="$2" port="$3"
  local current target
  current="$(get_env_value "$file" "$key")"
  [[ -n "$current" ]] || return 0
  target="http://localhost:$port"
  [[ "$current" == "$target" ]] && return 0

  if [[ "$current" =~ ^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?/?$ ]]; then
    set_env_value "$file" "$key" "$target"
    ok "$key: $current -> $target"
  else
    warn "$key 已自定义为 $current，脚本未改动；请自行确认其中包含 $target"
  fi
}

sync_port_config() {
  if [[ -f "$BACKEND_DIR/.env" ]]; then
    sync_local_url "$BACKEND_DIR/.env" 'CORS_ORIGINS' "$FRONTEND_PORT"
    sync_local_url "$BACKEND_DIR/.env" 'APP_BASE_URL' "$FRONTEND_PORT"
  fi
  if [[ -f "$FRONTEND_DIR/.env.local" ]]; then
    sync_local_url "$FRONTEND_DIR/.env.local" 'NEXT_PUBLIC_API_BASE_URL' "$BACKEND_PORT"
  fi
}

check_default_secrets() {
  local file="$BACKEND_DIR/.env"
  [[ -f "$file" ]] || return 0
  local warned=0
  if [[ "$(get_env_value "$file" 'JWT_SECRET')" == 'dev-secret-change-me' ]]; then
    warn 'JWT_SECRET 仍是默认值，建议替换为随机长字符串（生成方式见 .env.example 注释）'
    warned=1
  fi
  if [[ "$(get_env_value "$file" 'ENCRYPTION_KEY')" == 'dev-encryption-key-change-me' ]]; then
    warn 'ENCRYPTION_KEY 仍是默认值，建议替换为随机长字符串（生成方式见 .env.example 注释）'
    warned=1
  fi
  if [[ -z "$(get_env_value "$file" 'ADMIN_PASSWORD')" ]]; then
    warn 'ADMIN_PASSWORD 为空，启动时不会自动创建管理员账号'
    warned=1
  fi
  [[ "$warned" == "1" ]] && echo
  return 0
}

# ---------------------------------------------------------------------------
# 停止上次启动的服务
# ---------------------------------------------------------------------------
# 校验 PID 仍是当初启动的那个进程（避免 PID 复用误杀）
process_matches() {
  local pid="$1" label="$2"
  local cmd
  cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ -n "$cmd" ]] || return 1
  case "$label" in
    backend)  [[ "$cmd" == *uvicorn* ]] ;;
    frontend) [[ "$cmd" == *npm* || "$cmd" == *node* || "$cmd" == *next* ]] ;;
    *)        return 0 ;;
  esac
}

# 递归终止进程树（连带 uvicorn worker / next 的 node 子进程），不依赖 setsid
kill_tree() {
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

stop_tree() {
  local pid="$1" label="$2"
  [[ -n "$pid" ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  if ! process_matches "$pid" "$label"; then
    warn "PID $pid 已不是启动时的 $label 进程，跳过以免误杀"
    return 0
  fi
  printf "${C_GRAY}  停止 %s (PID %s)${C_OFF}\n" "$label" "$pid"
  kill_tree "$pid"
}

stop_previous() {
  [[ -f "$PID_FILE" ]] || { warn '没有找到上次启动记录的进程'; return 0; }
  step '停止上次启动的服务'
  # 文件格式：backend=<pid> / frontend=<pid>
  while IFS='=' read -r label pid; do
    [[ -n "${pid:-}" ]] || continue
    stop_tree "$pid" "$label"
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  ok '已清理'
}

if [[ "$DO_STOP" == "1" ]]; then
  stop_previous
  exit 0
fi

stop_previous

printf "\n  Agent App 快速启动\n  ------------------\n"
printf "${C_GRAY}  后端: http://localhost:%s      (接口文档 /docs)${C_OFF}\n" "$BACKEND_PORT"
[[ "$TARGET" != "backend" ]] && printf "${C_GRAY}  前端: http://localhost:%s${C_OFF}\n" "$FRONTEND_PORT"
echo

# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------
init_backend() {
  step '准备后端环境'
  if [[ ! -x "$VENV_PY" ]]; then
    command -v python3 >/dev/null 2>&1 || { err '未找到 python3，请先安装 Python 3.11+'; exit 1; }
    echo "  创建虚拟环境 .venv ..."
    python3 -m venv "$VENV_DIR"
  fi
  ok "$("$VENV_PY" --version 2>&1)"

  if [[ ! -f "$BACKEND_DIR/.env" ]]; then
    cp "$BACKEND_DIR/.env.example" "$BACKEND_DIR/.env"
    ok '已根据 .env.example 生成 backend/.env'
  else
    ok 'backend/.env 已存在'
  fi

  if [[ "$SKIP_INSTALL" == "1" ]]; then
    warn '已跳过后端依赖安装检查 (--skip-install)'; return 0
  fi
  if ! "$VENV_PY" -c 'import fastapi, uvicorn, sqlalchemy' >/dev/null 2>&1; then
    echo "  安装后端依赖 (requirements-dev.txt) ..."
    "$VENV_PY" -m pip install --upgrade pip >/dev/null
    "$VENV_PY" -m pip install -r "$BACKEND_DIR/requirements-dev.txt"
  fi
  ok '后端依赖就绪'
}

init_frontend() {
  step '准备前端环境'
  command -v npm >/dev/null 2>&1 || { err '未找到 npm，请先安装 Node.js 18+'; exit 1; }
  ok "Node $(node --version) / npm $(npm --version)"

  if [[ ! -f "$FRONTEND_DIR/.env.local" ]]; then
    cp "$FRONTEND_DIR/.env.local.example" "$FRONTEND_DIR/.env.local"
    ok '已根据 .env.local.example 生成 frontend/.env.local'
  else
    ok 'frontend/.env.local 已存在'
  fi

  if [[ "$SKIP_INSTALL" == "1" ]]; then
    warn '已跳过前端依赖安装检查 (--skip-install)'; return 0
  fi
  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "  安装前端依赖 (npm install) ..."
    (cd "$FRONTEND_DIR" && npm install)
  fi
  ok '前端依赖就绪'
}

# 端口预检：占用则直接失败，避免服务启动后立刻静默退出
if [[ "$TARGET" != "frontend" ]]; then assert_port_available "$BACKEND_PORT" '后端' '--backend-port'; fi
if [[ "$TARGET" != "backend" ]];  then assert_port_available "$FRONTEND_PORT" '前端' '--frontend-port'; fi

[[ "$TARGET" != "frontend" ]] && init_backend
[[ "$TARGET" != "backend" ]]  && init_frontend

step '同步端口配置'
sync_port_config
check_default_secrets

if [[ "$SETUP_ONLY" == "1" ]]; then
  echo; ok '环境准备完成（未启动服务）'; exit 0
fi

# ---------------------------------------------------------------------------
# 启动服务
# ---------------------------------------------------------------------------
BACKEND_PID=""
FRONTEND_PID=""
EXIT_CODE=0
READY_TIMEOUT=40

cleanup() {
  echo
  step '停止服务'
  stop_tree "$BACKEND_PID" 'backend'
  stop_tree "$FRONTEND_PID" 'frontend'
  rm -f "$PID_FILE"
  ok '已全部停止'
}

on_signal() {
  cleanup
  exit 130
}
trap on_signal INT TERM

port_listening() {
  local port="$1"
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    return 0
  fi
  return 1
}

# 就绪判定：先等端口监听（稳定），再做一次宽松的 HTTP 确认
# （next dev 首屏要现编译，用短超时做 HTTP 轮询会误判为「未就绪」）
wait_ready() {
  local name="$1" url="$2" port="$3" i=0
  while [[ "$i" -lt "$READY_TIMEOUT" ]]; do
    port_listening "$port" && break
    i=$((i + 1))
    sleep 1
  done

  if ! port_listening "$port"; then
    warn "$name 在 ${READY_TIMEOUT}s 内未监听端口 $port，请查看 .run/$name.log"
    return 0
  fi

  if command -v curl >/dev/null 2>&1; then
    if curl -fsS -o /dev/null --max-time 20 "$url" 2>/dev/null; then
      ok "$name 就绪: $url"
      return 0
    fi
    ok "$name 已在端口 $port 监听（首次访问可能仍在编译）: $url"
    return 0
  fi

  ok "$name 端口已监听: $port"
  return 0
}

step '启动服务'

if [[ "$TARGET" != "frontend" ]]; then
  # 子 shell 用 exec 直接变成服务进程（$! 即真实 PID）；子进程由 kill_tree 递归回收，不依赖 setsid
  ( cd "$BACKEND_DIR" && exec "$VENV_PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT" ) \
    > "$RUN_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
  ok "后端已启动 (PID $BACKEND_PID)，日志: .run/backend.log"
fi

if [[ "$TARGET" != "backend" ]]; then
  ( cd "$FRONTEND_DIR" && exec npm run dev -- -p "$FRONTEND_PORT" ) \
    > "$RUN_DIR/frontend.log" 2>&1 &
  FRONTEND_PID=$!
  ok "前端已启动 (PID $FRONTEND_PID)，日志: .run/frontend.log"
fi

{
  [[ -n "$BACKEND_PID" ]]  && echo "backend=$BACKEND_PID"
  [[ -n "$FRONTEND_PID" ]] && echo "frontend=$FRONTEND_PID"
} > "$PID_FILE"

echo
step '等待服务就绪'
if [[ -n "$BACKEND_PID" ]]; then
  wait_ready 'backend' "http://127.0.0.1:$BACKEND_PORT/api/v1/health" "$BACKEND_PORT"
fi
if [[ -n "$FRONTEND_PID" ]]; then
  wait_ready 'frontend' "http://127.0.0.1:$FRONTEND_PORT/" "$FRONTEND_PORT"
fi

echo
printf "  ${C_YELLOW}服务运行中，按 Ctrl+C 停止全部服务。${C_OFF}\n"
[[ "$TARGET" != "backend" ]] && printf "  浏览器打开: http://localhost:%s\n" "$FRONTEND_PORT"

# 任一服务退出则整体收尾
while true; do
  sleep 1
  if [[ -n "$BACKEND_PID" ]] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    warn "backend 进程已退出 (PID $BACKEND_PID)，请查看 .run/backend.log"; BACKEND_PID=""
  fi
  if [[ -n "$FRONTEND_PID" ]] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    warn "frontend 进程已退出 (PID $FRONTEND_PID)，请查看 .run/frontend.log"; FRONTEND_PID=""
  fi
  if [[ -z "$BACKEND_PID" && -z "$FRONTEND_PID" ]]; then
    err '所有服务均已退出'; EXIT_CODE=1; break
  fi
done

cleanup
exit "$EXIT_CODE"
