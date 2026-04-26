#!/usr/bin/env bash
# gen-adapter-fixture.sh
# DESC: Generate or update tests/fixtures/adapters/<adapter>/<case>/expected.json
# Usage: ./dev gen-adapter-fixture <adapter> <case>
# Dependencies: uv
# Idempotent: Yes (verifies byte-identical output on two runs)
source "$(dirname "$0")/lib/dev.sh"

usage() {
    echo "Usage: ./dev gen-adapter-fixture <adapter> <case>"
    echo ""
    echo "  adapter  Adapter name (e.g. claude_code, aider)"
    echo "  case     Case name (e.g. minimal, subagent)"
    echo ""
    echo "Runs adapter.parse() on the input fixture, writes expected.json."
    echo "Verifies output is byte-identical across two runs (non-determinism check)."
    exit 1
}

main() {
    local adapter="${1:-}"
    local case_name="${2:-}"
    [[ -z "$adapter" || -z "$case_name" ]] && usage

    cd "$DEV_ROOT"
    ensure_venv

    local case_dir="tests/fixtures/adapters/${adapter}/${case_name}"
    if [[ ! -d "$case_dir" ]]; then
        log_error "Case directory not found: ${case_dir}"
        exit 1
    fi

    local out="${case_dir}/expected.json"

    # Python script: run adapter, serialize, idempotence-check, write output
    uv run python - "$adapter" "$case_name" "$out" << 'PYEOF'
import dataclasses, importlib, json, sqlite3, sys, tempfile
from pathlib import Path

from siftd.domain.source import Source

adapter_name, case_name, out_path_str = sys.argv[1], sys.argv[2], sys.argv[3]
out_path = Path(out_path_str)
# CWD-relative path so workspace_path and path hashes are stable
case_dir = Path("tests/fixtures/adapters") / adapter_name / case_name

setup_sql = case_dir / "setup.sql"
if setup_sql.exists():
    # SQL-backed fixture: materialize a temp DB once and reuse across both runs
    tmp_db = Path(tempfile.mkdtemp()) / f"{adapter_name}_{case_name}.db"
    conn = sqlite3.connect(str(tmp_db))
    conn.executescript(setup_sql.read_text())
    conn.close()
    source = Source(kind="sqlite", location=tmp_db)
else:
    candidates = [f for f in case_dir.iterdir() if f.is_file() and f.name != "expected.json"]
    if not candidates:
        print(f"ERROR: No input fixture in {case_dir}", file=sys.stderr)
        sys.exit(1)
    source = Source(kind="file", location=candidates[0])

adapter = importlib.import_module(f"siftd.adapters.{adapter_name}")

def run():
    convs = list(adapter.parse(source))
    return json.dumps(
        json.loads(json.dumps([dataclasses.asdict(c) for c in convs], sort_keys=True)),
        indent=2, sort_keys=True
    ) + "\n"

run1 = run()
run2 = run()
if run1 != run2:
    print(f"ERROR: Non-deterministic output for {adapter_name}/{case_name}", file=sys.stderr)
    print("First run and second run differ — fix the adapter before generating fixtures.", file=sys.stderr)
    sys.exit(1)

out_path.write_text(run1)
convs = json.loads(run1)
print(f"Written: {out_path} ({len(convs)} conversation(s))")
PYEOF
}

main "$@"
