#!/usr/bin/env bash
# =============================================================================
# git-keeper.sh — 无人值守、最保守的 git 自动备份/同步
#
# 由 systemd timer 定时拉起。设计原则：保守边界用代码焊死，绝不押注 LLM 判断。
# claude 只负责生成语义化 commit message（可降级）；add/commit/push/pull 的安全
# 边界全部由本脚本保证。
#
# 硬护栏（任意一条违反即跳过对应动作并记日志，绝不破坏仓库）：
#   1. flock 防重入：timer 重叠也不并发。
#   2. 仓库处于「合并/冲突态」(unmerged paths) → 完全不提交，只 fetch + 告警留给人。
#   3. 只 `git add -u`（已跟踪文件的改动），绝不 `git add -A`（避开未跟踪脏文件，
#      如 .venv / research 临时产物）。
#   4. pull 只 `--ff-only`，非快进就停，绝不强 merge/rebase。
#   5. push 绝不 `--force`。非快进失败只记日志，不重试不强推。
#   6. 只操作当前 checkout 的分支及其 upstream。
#
# 用法：
#   DRY_RUN=1 scripts/git-keeper.sh        # 演练，不 commit/push，只打印将做什么
#   scripts/git-keeper.sh                   # 实跑
# 环境变量：
#   GITKEEPER_REPO   仓库路径（默认脚本所在仓库）
#   GITKEEPER_CLAUDE 1=用 claude 写 message（默认 1），0=只用固定模板
#   DRY_RUN          1=演练
# =============================================================================
set -uo pipefail

REPO="${GITKEEPER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
USE_CLAUDE="${GITKEEPER_CLAUDE:-1}"
DRY_RUN="${DRY_RUN:-0}"
CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude}"
LOGFILE="${GITKEEPER_LOG:-/tmp/git-keeper.log}"
# 要推送的 remote（空格分隔，默认仅 origin）。如 "origin github"。
REMOTES="${GITKEEPER_REMOTES:-origin}"
# 敏感文件熔断豁免正则（命中的路径不触发熔断），默认空。
GK_ALLOW="${GITKEEPER_ALLOW:-}"

# 同时输出到终端/journal 与文件日志（tail -f /tmp/git-keeper.log 即可看）。
log() { printf '%s [git-keeper] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOGFILE"; }
run() { if [ "$DRY_RUN" = "1" ]; then log "DRY-RUN > $*"; else "$@"; fi; }

# 敏感熔断：扫描所有未提交的已跟踪改动（vs HEAD），命中即返回 1（熔断）。
# 只报告命中的「文件名」与「指纹类型」，绝不打印密钥本身到日志。
# 文件名抓密钥文件，内容指纹抓密钥值（前缀精确，低误报）。
sensitive_scan() {
  local files fname_hits content_type tracked
  tracked="$(git diff --name-only HEAD 2>/dev/null)"
  [ -z "$tracked" ] && return 0
  # 应用 allowlist 豁免
  if [ -n "$GK_ALLOW" ]; then
    files="$(printf '%s\n' "$tracked" | grep -ivE "$GK_ALLOW" || true)"
  else
    files="$tracked"
  fi
  [ -z "$files" ] && return 0
  # 1) 文件名/路径模式（高危密钥文件）
  fname_hits="$(printf '%s\n' "$files" | grep -iE '(\.env($|\.)|id_rsa|\.pem$|\.p12$|\.pfx$|\.keystore$|\.key$|(^|/)[^/]*(load_key|_key\.sh|credential|secrets?)[^/]*$)' || true)"
  if [ -n "$fname_hits" ]; then
    log "🚨 敏感熔断：命中疑似密钥文件，本次不 commit/push（留给人工）："
    printf '%s\n' "$fname_hits" | while IFS= read -r f; do [ -n "$f" ] && log "    - $f"; done
    return 1
  fi
  # 2) 暂存/工作区内容里的密钥指纹（只判命中、不打印匹配行）
  if git diff HEAD -- $(printf '%s ' $files) 2>/dev/null \
       | grep -qE 'glpat-[A-Za-z0-9_-]{15,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xoxb-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{30,}|warp_[A-Za-z0-9]{20,}'; then
    log "🚨 敏感熔断：改动内容疑似含明文密钥/凭据，本次不 commit/push（留给人工）。"
    return 1
  fi
  return 0
}

cd "$REPO" 2>/dev/null || { log "FATAL: 仓库路径不存在: $REPO"; exit 1; }

# --- 0. flock 防重入 -------------------------------------------------------
LOCK="/tmp/git-keeper-$(echo "$REPO" | md5sum | cut -c1-8).lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  log "另一实例正在运行（lock=$LOCK），本次跳过。"
  exit 0
fi

# --- 1. 基本校验 -----------------------------------------------------------
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "FATAL: 不是 git 仓库: $REPO"; exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  log "处于 detached HEAD，最保守起见不动，退出。"; exit 0
fi
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
log "仓库=$REPO 分支=$BRANCH upstream=${UPSTREAM:-（无）} DRY_RUN=$DRY_RUN"

# --- 2. 冲突态检测：有未合并文件则完全不提交 --------------------------------
# 处于 merge/rebase/cherry-pick 中，或有 unmerged paths（UU/AA/UD/DU/AU/UA），
# 一律视为需人工处理，只 fetch + 告警。
GITDIR="$(git rev-parse --git-dir)"
IN_PROGRESS=""
[ -d "$GITDIR/rebase-merge" ] || [ -d "$GITDIR/rebase-apply" ] && IN_PROGRESS="rebase"
[ -f "$GITDIR/MERGE_HEAD" ] && IN_PROGRESS="merge"
[ -f "$GITDIR/CHERRY_PICK_HEAD" ] && IN_PROGRESS="cherry-pick"
UNMERGED="$(git status --porcelain | grep -E '^(DD|AU|UD|UA|DU|AA|UU) ' || true)"
if [ -n "$IN_PROGRESS" ] || [ -n "$UNMERGED" ]; then
  log "⚠️  仓库处于冲突/未完成态（in_progress=${IN_PROGRESS:-无}，unmerged 文件 $(printf '%s\n' "$UNMERGED" | grep -c . || echo 0) 个）。"
  log "⚠️  最保守策略：不 add/commit/push，留给人工处理。仅尝试 fetch。"
  run git fetch --quiet origin 2>&1 | sed 's/^/    /' || true
  log "完成（冲突态，仅 fetch）。"
  exit 0
fi

# --- 3. 同步：fetch + ff-only ---------------------------------------------
if [ -n "$UPSTREAM" ]; then
  if git fetch --quiet origin 2>/dev/null; then
    BEHIND="$(git rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo 0)"
    if [ "$BEHIND" -gt 0 ]; then
      log "落后 upstream $BEHIND 个提交，尝试 ff-only 快进。"
      if run git merge --ff-only "$UPSTREAM" 2>&1 | sed 's/^/    /'; then
        log "已快进到 $UPSTREAM。"
      else
        log "⚠️  无法 ff-only 快进（本地与远程分叉）。不强 merge/rebase，留给人工。"
      fi
    else
      log "本地不落后 upstream，无需同步。"
    fi
  else
    log "⚠️  fetch 失败（网络？），跳过同步，继续本地备份。"
  fi
fi

# --- 4. 备份提交：只 add -u（已跟踪文件改动）--------------------------------
CHANGED="$(git status --porcelain --untracked-files=no | grep -E '^[ MARCD]' || true)"
if [ -z "$CHANGED" ]; then
  log "已跟踪文件无改动，无需备份提交。"
else
  N="$(printf '%s\n' "$CHANGED" | grep -c . || echo 0)"
  log "检测到 $N 个已跟踪文件有改动，纳入备份（add -u）。未跟踪文件一律忽略。"
  if sensitive_scan; then
  run git add -u
  # 暂存后再确认确有内容（DRY_RUN 下 add 未执行，用 diff 工作区近似）
  if [ "$DRY_RUN" = "1" ]; then
    DIFF_FOR_MSG="$(git diff --stat -- $(git diff --name-only) | tail -40)"
  else
    if git diff --cached --quiet; then
      log "暂存区为空（可能都是被 .gitignore 的改动），跳过提交。"
      DIFF_FOR_MSG=""
    else
      DIFF_FOR_MSG="$(git diff --cached --stat | tail -40)"
    fi
  fi

  if [ -n "$DIFF_FOR_MSG" ]; then
    # --- 4a. commit message：claude 生成，失败降级 ---
    MSG=""
    if [ "$USE_CLAUDE" = "1" ] && [ -x "$CLAUDE_BIN" ]; then
      PROMPT="你是 git 助手。下面是 git diff --cached --stat 的输出，请只输出一行中文 commit message（conventional commits 风格，如 chore(backup): ...），不要解释、不要引号、不要代码块：

$DIFF_FOR_MSG"
      MSG="$(timeout 60 "$CLAUDE_BIN" -p "$PROMPT" --max-turns 1 2>/dev/null | head -1 | sed 's/^["`]*//; s/["`]*$//' | cut -c1-100)"
    fi
    if [ -z "$MSG" ]; then
      MSG="chore(backup): git-keeper 自动备份 $(date '+%Y-%m-%d %H:%M')"
      log "claude 未生成 message（不可用/超时/关闭），回退固定模板。"
    fi
    log "commit message: $MSG"
    run git commit -m "$MSG" --no-verify 2>&1 | sed 's/^/    /' || log "⚠️ commit 失败。"
  fi
  else
    log "↑ 敏感熔断生效：本次不提交、不推送新内容（之前的合法提交仍会在 push 段推送）。"
  fi
fi

# --- 5. push：遍历所有配置的 remote，绝不 force ------------------------------
for R in $REMOTES; do
  if ! git remote get-url "$R" >/dev/null 2>&1; then
    log "⚠️  remote '$R' 不存在，跳过。"
    continue
  fi
  # 该 remote 上同名分支落后多少（无远程分支则视为需要推）
  RAHEAD="$(git rev-list --count "$R/$BRANCH..HEAD" 2>/dev/null || echo 1)"
  if [ "$RAHEAD" = "0" ]; then
    log "remote '$R' 已是最新，无需 push。"
    continue
  fi
  log "push 到 '$R'（普通，绝不 force）..."
  if run git push "$R" "$BRANCH" 2>&1 | sed 's/^/    /'; then
    log "  '$R' push 完成。"
  else
    log "⚠️  '$R' push 失败（非快进/网络/鉴权）。不 force，留给人工。"
  fi
done

log "完成。"
