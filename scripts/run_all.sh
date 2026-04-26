#!/usr/bin/env bash
# walk-the-talk 一键全流程脚本
#   ingest → extract → verify → report
# 用法:
#   ./scripts/run_all.sh                                # 用默认值（中芯国际）
#   ./scripts/run_all.sh --clean                        # 清空 _walk_the_talk/ 全量重跑
#   ./scripts/run_all.sh -d <data_dir> -t <ticker> -c <company>
#   DATA_DIR=... TICKER=... COMPANY=... ./scripts/run_all.sh
#
# 环境变量（可被 CLI 参数覆盖）:
#   DATA_DIR       数据目录（含 <year>.html）
#   TICKER         股票代码
#   COMPANY        公司中文名
#   EMBEDDER       bge | hash（默认 bge）
#   MAX_WORKERS    extract 并发度（默认 5）
#   MAX_ITERS      verify per-claim 迭代上限（默认 3）
#   YEARS          extract/verify 限制年份（逗号分隔；空 = 全部）
#   CLAIM_IDS      verify 限制 claim_id（逗号分隔；空 = 全部）
#   CURRENT_FY     verify / report 当前 FY（空 = 自动从 financials.db 检测）
#   REPORT_OUT     report 输出文件名（默认 report.md，落 WORK_DIR/）
#   SKIP_INGEST    设为 1 跳过 ingest
#   SKIP_EXTRACT   设为 1 跳过 extract
#   SKIP_VERIFY    设为 1 跳过 verify
#   SKIP_REPORT    设为 1 跳过 report
#   NO_RESUME      设为 1 给 ingest/extract/verify 都加 --no-resume（强制重跑所有 claim）
#   DEBUG          设为 1 给 extract / verify 加 --debug

set -euo pipefail

# ---------- 默认参数 ----------
DATA_DIR="${DATA_DIR:-/Users/alfy/Desktop/股票/中芯国际}"
TICKER="${TICKER:-688981}"
COMPANY="${COMPANY:-中芯国际}"
EMBEDDER="${EMBEDDER:-bge}"
MAX_WORKERS="${MAX_WORKERS:-5}"
MAX_ITERS="${MAX_ITERS:-3}"
YEARS="${YEARS:-}"
CLAIM_IDS="${CLAIM_IDS:-}"
CURRENT_FY="${CURRENT_FY:-}"
REPORT_OUT="${REPORT_OUT:-report.md}"
SKIP_INGEST="${SKIP_INGEST:-0}"
SKIP_EXTRACT="${SKIP_EXTRACT:-0}"
SKIP_VERIFY="${SKIP_VERIFY:-0}"
SKIP_REPORT="${SKIP_REPORT:-0}"
NO_RESUME="${NO_RESUME:-0}"
DEBUG="${DEBUG:-0}"
CLEAN=0

# ---------- CLI 参数 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--data-dir)   DATA_DIR="$2"; shift 2 ;;
        -t|--ticker)     TICKER="$2"; shift 2 ;;
        -c|--company)    COMPANY="$2"; shift 2 ;;
        --embedder)      EMBEDDER="$2"; shift 2 ;;
        --max-workers)   MAX_WORKERS="$2"; shift 2 ;;
        --max-iters)     MAX_ITERS="$2"; shift 2 ;;
        --years)         YEARS="$2"; shift 2 ;;
        --claim-ids)     CLAIM_IDS="$2"; shift 2 ;;
        --current-fy)    CURRENT_FY="$2"; shift 2 ;;
        --report-out)    REPORT_OUT="$2"; shift 2 ;;
        --clean)         CLEAN=1; shift ;;
        --skip-ingest)   SKIP_INGEST=1; shift ;;
        --skip-extract)  SKIP_EXTRACT=1; shift ;;
        --skip-verify)   SKIP_VERIFY=1; shift ;;
        --skip-report)   SKIP_REPORT=1; shift ;;
        --no-resume)     NO_RESUME=1; shift ;;
        --debug)         DEBUG=1; shift ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | head -n -2
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 2
            ;;
    esac
done

# ---------- 路径 / venv ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -d "${REPO_ROOT}/.venv" ]]; then
    echo "✗ 找不到 ${REPO_ROOT}/.venv，先建虚拟环境并 pip install -e ." >&2
    exit 1
fi
# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"

if [[ ! -d "${DATA_DIR}" ]]; then
    echo "✗ DATA_DIR 不存在: ${DATA_DIR}" >&2
    exit 1
fi

WORK_DIR="${DATA_DIR}/_walk_the_talk"

# ---------- 工具函数 ----------
HR='────────────────────────────────────────────────────────────────'

phase_banner() {
    local title="$1"
    echo
    echo "${HR}"
    echo "▶ ${title}"
    echo "${HR}"
}

run_phase() {
    local title="$1"
    shift
    phase_banner "${title}"
    echo "$ $*"
    local t0
    t0="$(date +%s)"
    if "$@"; then
        local t1
        t1="$(date +%s)"
        echo
        echo "✓ ${title} 完成（耗时 $((t1 - t0))s）"
    else
        local rc=$?
        echo
        echo "✗ ${title} 失败（exit ${rc}）" >&2
        exit ${rc}
    fi
}

# ---------- 配置回显 ----------
phase_banner "配置"
cat <<EOF
DATA_DIR     ${DATA_DIR}
WORK_DIR     ${WORK_DIR}
TICKER       ${TICKER}
COMPANY      ${COMPANY}
EMBEDDER     ${EMBEDDER}
MAX_WORKERS  ${MAX_WORKERS}    (extract)
MAX_ITERS    ${MAX_ITERS}    (verify)
YEARS        ${YEARS:-<all>}
CLAIM_IDS    ${CLAIM_IDS:-<all>}
CURRENT_FY   ${CURRENT_FY:-<auto>}
REPORT_OUT   ${REPORT_OUT}
CLEAN        ${CLEAN}
NO_RESUME    ${NO_RESUME}
SKIP         ingest=${SKIP_INGEST} extract=${SKIP_EXTRACT} verify=${SKIP_VERIFY} report=${SKIP_REPORT}
DEBUG        ${DEBUG}
EOF

# ---------- 清理 ----------
if [[ "${CLEAN}" == "1" ]]; then
    if [[ -d "${WORK_DIR}" ]]; then
        phase_banner "清理 _walk_the_talk/"
        echo "$ rm -rf ${WORK_DIR}"
        rm -rf "${WORK_DIR}"
        echo "✓ 已删除"
    else
        echo "（${WORK_DIR} 不存在，无需清理）"
    fi
fi

OVERALL_T0="$(date +%s)"

# ---------- INGEST ----------
if [[ "${SKIP_INGEST}" == "1" ]]; then
    echo "（跳过 ingest）"
else
    INGEST_ARGS=(
        ingest "${DATA_DIR}"
        --ticker "${TICKER}"
        --company "${COMPANY}"
        --embedder "${EMBEDDER}"
    )
    [[ "${NO_RESUME}" == "1" ]] && INGEST_ARGS+=(--no-resume)
    run_phase "PHASE 1/4 · INGEST" walk-the-talk "${INGEST_ARGS[@]}"
fi

# ---------- EXTRACT ----------
if [[ "${SKIP_EXTRACT}" == "1" ]]; then
    echo "（跳过 extract）"
else
    EXTRACT_ARGS=(
        extract "${DATA_DIR}"
        --ticker "${TICKER}"
        --company "${COMPANY}"
        --max-workers "${MAX_WORKERS}"
    )
    [[ -n "${YEARS}" ]] && EXTRACT_ARGS+=(--years "${YEARS}")
    [[ "${NO_RESUME}" == "1" ]] && EXTRACT_ARGS+=(--no-resume)
    [[ "${DEBUG}" == "1" ]] && EXTRACT_ARGS+=(--debug)
    run_phase "PHASE 2/4 · EXTRACT" walk-the-talk "${EXTRACT_ARGS[@]}"
fi

# ---------- VERIFY ----------
if [[ "${SKIP_VERIFY}" == "1" ]]; then
    echo "（跳过 verify）"
else
    VERIFY_ARGS=(
        verify "${DATA_DIR}"
        --ticker "${TICKER}"
        --company "${COMPANY}"
        --max-iters "${MAX_ITERS}"
    )
    [[ -n "${YEARS}" ]]      && VERIFY_ARGS+=(--years "${YEARS}")
    [[ -n "${CLAIM_IDS}" ]]  && VERIFY_ARGS+=(--claim-ids "${CLAIM_IDS}")
    [[ -n "${CURRENT_FY}" ]] && VERIFY_ARGS+=(--current-fy "${CURRENT_FY}")
    [[ -n "${EMBEDDER}" ]]   && VERIFY_ARGS+=(--embedder "${EMBEDDER}")
    [[ "${NO_RESUME}" == "1" ]] && VERIFY_ARGS+=(--no-resume)
    [[ "${DEBUG}" == "1" ]]  && VERIFY_ARGS+=(--debug)
    run_phase "PHASE 3/4 · VERIFY" walk-the-talk "${VERIFY_ARGS[@]}"
fi

# ---------- REPORT ----------
if [[ "${SKIP_REPORT}" == "1" ]]; then
    echo "（跳过 report）"
else
    REPORT_ARGS=(
        report "${DATA_DIR}"
        --ticker "${TICKER}"
        --company "${COMPANY}"
        --out "${REPORT_OUT}"
    )
    [[ -n "${CURRENT_FY}" ]] && REPORT_ARGS+=(--current-fy "${CURRENT_FY}")
    run_phase "PHASE 4/4 · REPORT" walk-the-talk "${REPORT_ARGS[@]}"
fi

OVERALL_T1="$(date +%s)"

phase_banner "全部完成"
echo "总耗时 $((OVERALL_T1 - OVERALL_T0))s"
echo
echo "产物在 ${WORK_DIR}/"
echo "  ├─ chunks (Chroma)        : chroma/"
echo "  ├─ BM25 索引              : bm25.pkl"
echo "  ├─ 财务库                 : financials.db"
echo "  ├─ LLM prompt 缓存        : llm_cache.db"
echo "  ├─ 前瞻断言               : claims.json"
echo "  ├─ 验证结果               : verdicts.json"
echo "  └─ 最终报告               : ${REPORT_OUT}"
