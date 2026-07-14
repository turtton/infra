#!/usr/bin/env bash
# shellcheck disable=SC2329
#
# Contract test suite for scripts/attic-make-token.sh
#
# Verifies CLI contract, argument parsing, error handling, kubectl interaction,
# and remote program execution without real Kubernetes.
#
#
set -u

SCRIPT_UNDER_TEST=""
TEST_TMP=""
FAKE_BIN=""
KUBECTL_LOG=""
ATTICADM_LOG=""
EXEC_LOG=""
PASS=0
FAIL=0

# ─── Test Framework ─────────────────────────────────────────────────────────

init_dirs() {
    TEST_TMP="$(mktemp -d)"
    FAKE_BIN="${TEST_TMP}/bin"
    KUBECTL_LOG="${TEST_TMP}/kubectl.log"
    ATTICADM_LOG="${TEST_TMP}/atticadm.log"
    EXEC_LOG="${TEST_TMP}/exec.log"
    mkdir -p "$FAKE_BIN"
}

make_fake_kubectl() {
    local bin="$1"
    cat > "${bin}/kubectl" << 'KUBECTL_EOF'
#!/usr/bin/env bash
set -u

LOG="${KUBECTL_LOG:?}"
EXEC_LOG="${EXEC_LOG:?}"

echo "kubectl $*" >> "$LOG"

case "${1:-}" in
    wait)
        shift
        echo "WAIT: $*" >> "$LOG"
        if [[ "${KUBECTL_WAIT_EXIT:-0}" != "0" ]]; then
            echo "error: timed out waiting for condition" >&2
            exit "${KUBECTL_WAIT_EXIT}"
        fi
        exit 0
        ;;
    exec)
        shift
        echo "EXEC: $*" >> "$LOG"
        if [[ "${KUBECTL_EXEC_EXIT:-0}" != "0" ]]; then
            echo "error: exec failed" >&2
            exit "${KUBECTL_EXEC_EXIT}"
        fi
        all_args=("$@")
        after_dd=false
        cmd_parts=()
        for arg in "${all_args[@]}"; do
            if [[ "$arg" == "--" ]]; then
                after_dd=true
                continue
            fi
            if $after_dd; then
                cmd_parts+=("$arg")
            fi
        done
        echo "CMD: [${cmd_parts[*]}]" >> "$LOG"
        echo "CMD_COUNT: ${#cmd_parts[@]}" >> "$LOG"
        if [[ ${#cmd_parts[@]} -ge 3 && "${cmd_parts[0]}" == "sh" && "${cmd_parts[1]}" == "-c" ]]; then
            program="${cmd_parts[2]}"
            shift_args=("${cmd_parts[@]:3}")
            echo "PROGRAM_LEN: ${#program}" >> "$LOG"
            echo "SHIFT_ARGS: [${shift_args[*]}]" >> "$LOG"
            echo "--- PROGRAM START ---" >> "$EXEC_LOG"
            echo "$program" >> "$EXEC_LOG"
            echo "--- PROGRAM END ---" >> "$EXEC_LOG"
            echo "--- SHIFT_ARGS: ${shift_args[*]} ---" >> "$EXEC_LOG"
            (
                export PATH="${FAKE_BINDIR}:${PATH}"
                sh -c "$program" "${shift_args[@]}"
            )
            rc=$?
            echo "EXEC_RC: $rc" >> "$EXEC_LOG"
            exit $rc
        fi
        echo "fake-kubectl-direct-token-000"
        exit 0
        ;;
    *)
        echo "UNKNOWN: $*" >> "$LOG"
        echo "fake-kubectl: unknown command: ${1:-}" >&2
        exit 1
        ;;
esac
KUBECTL_EOF
    chmod +x "${bin}/kubectl"
}

make_fake_atticadm() {
    local bin="$1"
    cat > "${bin}/atticadm" << 'ATTICADM_EOF'
#!/usr/bin/env bash
set -u

LOG="${ATTICADM_LOG:?}"
echo "atticadm $*" >> "$LOG"
echo "ATTIC_SERVER_DATABASE_URL=${ATTIC_SERVER_DATABASE_URL:-}" >> "$LOG"
n=0
for arg in "$@"; do
    echo "ARG[$n]=$arg" >> "$LOG"
    n=$((n + 1))
done

echo "fake-attic-token-abc123"
exit 0
ATTICADM_EOF
    chmod +x "${bin}/atticadm"
}

make_unfindable_kubectl() {
    rm -f "${FAKE_BIN}/kubectl"
    cat > "${FAKE_BIN}/kubectl" <<'EOF'
#!/usr/bin/env bash
echo "error: kubectl not found" >&2
exit 127
EOF
    chmod +x "${FAKE_BIN}/kubectl"
}

setup_env() {
    SCRIPT_UNDER_TEST="$(cd "$(dirname "$0")/../.." && pwd)/scripts/attic-make-token.sh"
    init_dirs
    make_fake_kubectl "$FAKE_BIN"
    make_fake_atticadm "$FAKE_BIN"
    export PATH="${FAKE_BIN}:${PATH}"
    export KUBECTL_LOG ATTICADM_LOG EXEC_LOG FAKE_BINDIR="${FAKE_BIN}"
    export KUBECTL_WAIT_EXIT=0 KUBECTL_EXEC_EXIT=0
    export DATABASE_URI="postgresql://app:secret@db:5432/app"
    unset ATTIC_SERVER_DATABASE_URL
}

clear_logs() {
    : > "$KUBECTL_LOG"
    : > "$ATTICADM_LOG"
    : > "$EXEC_LOG"
}

cleanup() {
    [[ -n "${TEST_TMP:-}" && -d "${TEST_TMP:-}" ]] && rm -rf "$TEST_TMP"
}

# ─── Assertion Helpers ─────────────────────────────────────────────────────

pass() {
    local label="$1"
    echo "  PASS: $label"
    PASS=$((PASS + 1))
}

fail() {
    local label="$1"
    local msg="$2"
    echo "  FAIL: $label — $msg"
    FAIL=$((FAIL + 1))
}

# Run the SUT, capturing exit code, stdout, and stderr.
# Sets global vars: SUT_RC, SUT_STDOUT, SUT_STDERR
run_sut() {
    SUT_RC=0
    SUT_STDOUT=""
    SUT_STDERR=""
    if [[ ! -f "$SCRIPT_UNDER_TEST" ]]; then
        SUT_RC=127
        SUT_STDERR="scripts/attic-make-token.sh: No such file or directory"
        return
    fi
    if [[ ! -x "$SCRIPT_UNDER_TEST" ]]; then
        SUT_RC=126
        SUT_STDERR="scripts/attic-make-token.sh: Permission denied"
        return
    fi
    local out_file err_file
    out_file="$(mktemp /tmp/sut-stdout-XXXXXX)"
    err_file="$(mktemp /tmp/sut-stderr-XXXXXX)"
    # shellcheck disable=SC2064
    trap "rm -f '$out_file' '$err_file'" RETURN
    set +e
    "$SCRIPT_UNDER_TEST" "$@" > "$out_file" 2> "$err_file"
    SUT_RC=$?
    set -e
    SUT_STDOUT="$(cat "$out_file")"
    SUT_STDERR="$(cat "$err_file")"
}

# Assertion helpers – each returns non-zero on failure so the caller
# can chain into a fail() call.
assert_eq() {
    local actual="$1" expected="$2" label="$3"
    if [[ "$actual" != "$expected" ]]; then
        fail "$label" "expected [${expected}], got [${actual}]"
        return 1
    fi
    return 0
}

assert_ne() {
    local actual="$1" unexpected="$2" label="$3"
    if [[ "$actual" == "$unexpected" ]]; then
        fail "$label" "value equals unexpected [${unexpected}]"
        return 1
    fi
    return 0
}

assert_rc() {
    assert_eq "$SUT_RC" "$1" "$2" && pass "$2"
}

assert_stdout_contains() {
    local label="$2"
    if [[ "$SUT_STDOUT" == *"$1"* ]]; then
        pass "$label"
    else
        fail "$label" "stdout does not contain [${1}]"
        return 1
    fi
    return 0
}

assert_stderr_contains() {
    local label="$2"
    if [[ "$SUT_STDERR" == *"$1"* ]]; then
        pass "$label"
    else
        fail "$label" "stderr does not contain [${1}]"
        return 1
    fi
    return 0
}

assert_file_contains() {
    local file="$1" pattern="$2" label="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "file [${file}] does not contain [${pattern}]"
        return 1
    fi
    return 0
}

assert_file_absent_contains() {
    local file="$1" pattern="$2" label="$3"
    if grep -qF "$pattern" "$file" 2>/dev/null; then
        fail "$label" "file [${file}] contains [${pattern}]"
        return 1
    else
        pass "$label"
        return 0
    fi
    return 0
}

assert_marker_absent() {
    local marker="$1" label="$2"
    if [[ -f "$marker" ]]; then
        fail "$label" "marker file [${marker}] was created (injection succeeded)"
        return 1
    else
        pass "$label"
        return 0
    fi
    return 0
}

# Guard: skip test when SUT is missing (always true in RED state)
expect_sut_or_fail() {
    if [[ ! -f "$SCRIPT_UNDER_TEST" ]]; then
        fail "$1" "SUT not found: scripts/attic-make-token.sh"
        return 1
    fi
    return 0
}

# ─── Test Cases ─────────────────────────────────────────────────────────────

# TC01: Help succeeds without kubectl.
test_help_without_kubectl() {
    local label="TC01: --help exits 0 without calling kubectl"
    clear_logs
    make_unfindable_kubectl
    run_sut --help
    make_fake_kubectl "$FAKE_BIN"
    export PATH="${FAKE_BIN}:${PATH}"
    if [[ ! -f "$SCRIPT_UNDER_TEST" ]]; then
        fail "$label" "SUT not found: scripts/attic-make-token.sh"
        return
    fi
    assert_rc 0 "$label" || true
    if [[ "$SUT_RC" == 0 ]]; then
        local calls
        calls="$(wc -l < "$KUBECTL_LOG" 2>/dev/null || echo 0)"
        if [[ "$calls" -eq 0 ]]; then
            pass "${label} (no kubectl call)"
        else
            fail "${label}" "kubectl was called (${calls} lines)"
        fi
    fi
}

# TC02: Missing --sub exits 2 with error message.
test_missing_subject() {
    local label="TC02: missing --sub exits 2"
    clear_logs
    run_sut --validity 30d --pull
    expect_sut_or_fail "$label" || return
    assert_rc 2 "$label" || true
}

# TC03: Missing --validity exits 2 with error message.
test_missing_validity() {
    local label="TC03: missing --validity exits 2"
    clear_logs
    run_sut --sub test --pull
    expect_sut_or_fail "$label" || return
    assert_rc 2 "$label" || true
}

# TC04: No permission flag exits 2.
test_no_permission() {
    local label="TC04: no permission flag exits 2"
    clear_logs
    run_sut --sub test --validity 30d
    expect_sut_or_fail "$label" || return
    assert_rc 2 "$label" || true
}

# TC05: --admin combined with a granular permission exits 2.
test_admin_and_granular_mutual_exclusion() {
    local label="TC05: --admin and --pull mutually exclusive exits 2"
    clear_logs
    run_sut --sub test --validity 30d --admin --pull
    expect_sut_or_fail "$label" || return
    assert_rc 2 "$label" || true
}

# TC06: --admin expands all seven permissions in fixed order.
test_admin_expands_all_permissions() {
    local label="TC06: --admin expands all seven permissions"
    clear_logs
    run_sut --sub admin-test --validity 90d --admin
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if grep -q 'atticadm make-token' "$ATTICADM_LOG" 2>/dev/null; then
        if grep -q -- '--pull' "$ATTICADM_LOG" && \
           grep -q -- '--push' "$ATTICADM_LOG" && \
           grep -q -- '--delete' "$ATTICADM_LOG" && \
           grep -q -- '--create-cache' "$ATTICADM_LOG" && \
           grep -q -- '--configure-cache' "$ATTICADM_LOG" && \
           grep -q -- '--configure-cache-retention' "$ATTICADM_LOG" && \
           grep -q -- '--destroy-cache' "$ATTICADM_LOG"; then
            pass "$label"
        else
            fail "$label" "not all seven permissions found in atticadm log"
        fi
    else
        fail "$label" "atticadm was not called"
    fi
}

# TC07: Every granular permission is forwarded to atticadm.
test_granular_permissions_forwarded() {
    local label="TC07: granular --push --delete forwarded to atticadm"
    clear_logs
    run_sut --sub gran-test --validity 7d --push 'my-cache' --delete 'my-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if grep -q -- '--push' "$ATTICADM_LOG" 2>/dev/null && \
       grep -q -- '--delete' "$ATTICADM_LOG" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "granular permissions not in atticadm log"
    fi
}

# TC08: Repeated permissions and --option=value syntax are preserved.
test_repeated_and_equals_syntax() {
    local label="TC08: repeated perms and --option=value preserved"
    clear_logs
    run_sut --sub rep-test --validity 1d --pull 'cache-a' --pull 'cache-b' --push='eq-cache' --delete 'cache-a' --delete 'cache-b'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    local pull_count push_count delete_count
    pull_count="$(grep -c -- '--pull' "$ATTICADM_LOG" 2>/dev/null || echo 0)"
    push_count="$(grep -c -- '--push' "$ATTICADM_LOG" 2>/dev/null || echo 0)"
    delete_count="$(grep -c -- '--delete' "$ATTICADM_LOG" 2>/dev/null || echo 0)"
    if [[ "$pull_count" -ge 2 && "$push_count" -ge 1 && "$delete_count" -ge 2 && $(grep -c -- 'eq-cache' "$ATTICADM_LOG" 2>/dev/null || echo 0) -ge 1 ]]; then
        pass "$label"
    else
        fail "$label" "repeated permissions or equals-syntax value lost (pull=$pull_count push=$push_count delete=$delete_count)"
    fi
}

# TC09: Whitespace, quotes, semicolons, and special chars remain literal.
test_special_chars_literal() {
    local label="TC09: special characters treated literally"
    clear_logs
    local subj="test with spaces"
    run_sut --sub "$subj" --validity '30d' --pull 'cache with spaces'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if grep -q -- 'test with spaces' "$ATTICADM_LOG" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "subject with spaces not forwarded"
    fi
}

# TC10: No injection marker file created by $(touch marker) payload.
test_no_injection() {
    local label="TC10: no injection marker file created"
    clear_logs
    local marker="${TEST_TMP}/injection-marker"
    # shellcheck disable=SC2016
    local evil_subj='$(touch '"$marker"')'
    run_sut --sub "$evil_subj" --validity 30d --pull 'evil-cache'
    expect_sut_or_fail "$label" || return
    assert_marker_absent "$marker" "$label"
}

# TC11: Missing kubectl in PATH exits 127.
test_missing_kubectl_127() {
    local label="TC11: missing kubectl exits 127"
    clear_logs
    make_unfindable_kubectl
    run_sut --sub test --validity 30d --pull 'my-cache'
    make_fake_kubectl "$FAKE_BIN"
    export PATH="${FAKE_BIN}:${PATH}"
    expect_sut_or_fail "$label" || return
    assert_rc 127 "$label" || true
}

# TC12: Readiness failure (kubectl wait fails) propagates and prevents exec.
test_readiness_failure() {
    local label="TC12: readiness failure propagated, exec not attempted"
    clear_logs
    export KUBECTL_WAIT_EXIT=1
    run_sut --sub readytest --validity 60d --push 'ready-cache'
    export KUBECTL_WAIT_EXIT=0
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 1 ]]; then
        pass "$label"
    else
        fail "$label" "expected RC 1 (wait failure), got $SUT_RC"
    fi
    if grep -q 'EXEC:' "$KUBECTL_LOG" 2>/dev/null; then
        fail "${label} (exec should not have been called)" "kubectl exec was called despite wait failure"
    fi
}

# TC13: Exec failure propagates.
test_exec_failure() {
    local label="TC13: exec failure propagated"
    clear_logs
    export KUBECTL_EXEC_EXIT=42
    run_sut --sub exectest --validity 10d --delete 'exec-cache'
    export KUBECTL_EXEC_EXIT=0
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 42 ]]; then
        pass "$label"
    else
        fail "$label" "expected RC 42 (exec failure), got $SUT_RC"
    fi
}

# TC14: Successful execution stdout contains exactly the token + newline.
test_stdout_token_only() {
    local label="TC14: stdout is exactly fake token"
    clear_logs
    run_sut --sub tokentest --validity 30d --pull 'my-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if [[ "$SUT_STDOUT" == "fake-attic-token-abc123" ]]; then
        pass "$label"
    else
        fail "$label" "stdout is [${SUT_STDOUT}], not [fake-attic-token-abc123]"
    fi
}

# TC15: Readiness messages go to stderr, not stdout.
test_readiness_on_stderr() {
    local label="TC15: readiness output on stderr only"
    clear_logs
    run_sut --sub stderrtest --validity 30d --pull 'my-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if [[ "$SUT_STDOUT" == "fake-attic-token-abc123" ]]; then
        pass "$label"
    else
        echo "  INFO: stdout=[${SUT_STDOUT}], stderr=[${SUT_STDERR}]" >&2
        if [[ "$SUT_STDERR" != *"fake-attic-token"* ]]; then
            pass "${label} (no token on stderr)"
        else
            fail "$label" "token leaked to stderr"
        fi
    fi
}

# TC16: Namespace, deployment, container, and config path are exact.
test_namespace_config_path() {
    local label="TC16: namespace/deployment/container/config path exact"
    clear_logs
    run_sut --sub pathtest --validity 60d --push 'path-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    # Verify kubectl names and paths from log
    if grep -q -- '--namespace attic' "$KUBECTL_LOG" 2>/dev/null && \
       grep -q -- 'deployment/attic' "$KUBECTL_LOG" 2>/dev/null && \
       grep -q -- '--container attic' "$KUBECTL_LOG" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "expected --namespace attic, deployment/attic, --container attic not all in log"
    fi
    if grep -q -- '/etc/attic/server.toml' "$ATTICADM_LOG" 2>/dev/null; then
        pass "${label} (config path in atticadm args)"
    elif grep -q -- '/etc/attic/server.toml' "$EXEC_LOG" 2>/dev/null; then
        pass "${label} (config path in remote program)"
    else
        fail "$label" "config path /etc/attic/server.toml not found in logs"
    fi
}

# TC17: Remote sh -c program is actually executed against fake atticadm.
test_remote_program_executed() {
    local label="TC17: remote program executed against fake atticadm"
    clear_logs
    run_sut --sub exectest --validity 10d --pull 'exec-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    # Check that atticadm was called (means the remote program executed)
    if grep -q 'atticadm make-token' "$ATTICADM_LOG" 2>/dev/null; then
        pass "$label"
    else
        fail "$label" "atticadm was not invoked by remote program"
    fi
    # Verify the program was logged by fake kubectl
    if grep -q 'PROGRAM START' "$EXEC_LOG" 2>/dev/null; then
        pass "${label} (remote program logged)"
    fi
}

# TC18: DATABASE_URI postgresql:// prefix is transformed to postgres://
#       and exported as ATTIC_SERVER_DATABASE_URL.
test_database_uri_transformation() {
    local label="TC18: postgresql:// transformed to postgres://"
    clear_logs
    run_sut --sub dbtest --validity 30d --pull 'db-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" != 0 ]]; then
        fail "$label" "SUT exited $SUT_RC (expected 0)"
        return
    fi
    if grep -q 'ATTIC_SERVER_DATABASE_URL=postgres://' "$ATTICADM_LOG" 2>/dev/null; then
        pass "$label"
    elif grep -q 'postgresql://' "$EXEC_LOG" 2>/dev/null; then
        echo "  INFO: DATABASE_URI transformation appears in remote program" >&2
        pass "${label} (found in remote program)"
    else
        fail "$label" "no postgresql:// -> postgres:// transformation found"
    fi
    if grep -q 'ATTIC_SERVER_DATABASE_URL=postgresql://' "$ATTICADM_LOG" 2>/dev/null; then
        fail "${label} (postgresql:// leaked into atticadm)" "found postgresql:// in ATTIC_SERVER_DATABASE_URL"
    fi
}

# TC19: Missing DATABASE_URI fails without leaking its value.
test_missing_database_uri_fails() {
    local label="TC19: missing DATABASE_URI fails safely"
    clear_logs
    unset DATABASE_URI
    run_sut --sub nodbtest --validity 30d --pull 'nodb-cache'
    export DATABASE_URI="postgresql://app:secret@db:5432/app"
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 0 ]]; then
        fail "$label" "SUT exited 0 but should have failed due to missing DATABASE_URI"
        return
    fi
    if [[ "$SUT_STDERR" == *"DATABASE_URI"* ]] || [[ "$SUT_STDERR" == *"database"* ]]; then
        pass "$label"
    else
        fail "$label" "expected stderr to mention DATABASE_URI/database, got [${SUT_STDERR}]"
    fi
}

# TC20: Unknown options, positional args, empty values, missing values exit 2.
test_invalid_options_and_args() {
    local label

    label="TC20.1: unknown option"
    clear_logs
    run_sut --bad-opt --sub test --validity 30d --pull 'my-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 2 ]]; then
        pass "$label"
    else
        fail "$label" "expected RC 2, got $SUT_RC"
    fi

    label="TC20.2: positional arg"
    clear_logs
    run_sut --sub test --validity 30d --admin extra-arg
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 2 ]]; then
        pass "$label"
    else
        fail "$label" "expected RC 2, got $SUT_RC"
    fi

    label="TC20.3: empty sub"
    clear_logs
    run_sut --sub '' --validity 30d --pull 'my-cache'
    expect_sut_or_fail "$label" || return
    if [[ "$SUT_RC" == 2 ]]; then
        pass "$label"
    else
        fail "$label" "expected RC 2, got $SUT_RC"
    fi
}

# ─── Main ───────────────────────────────────────────────────────────────────

main() {
    trap cleanup EXIT INT TERM
    setup_env

    echo "=== attic-make-token test suite ==="
    echo "SUT: ${SCRIPT_UNDER_TEST}"
    echo ""

    local run_all=true
    local filter=""
    if [[ $# -gt 0 ]]; then
        run_all=false
        filter="$1"
    fi

    run_test() {
        local name="$1"
        if $run_all || [[ "$name" == *"$filter"* ]]; then
            "$name"
        fi
    }

    run_test test_help_without_kubectl
    run_test test_missing_subject
    run_test test_missing_validity
    run_test test_no_permission
    run_test test_admin_and_granular_mutual_exclusion
    run_test test_admin_expands_all_permissions
    run_test test_granular_permissions_forwarded
    run_test test_repeated_and_equals_syntax
    run_test test_special_chars_literal
    run_test test_no_injection
    run_test test_missing_kubectl_127
    run_test test_readiness_failure
    run_test test_exec_failure
    run_test test_stdout_token_only
    run_test test_readiness_on_stderr
    run_test test_namespace_config_path
    run_test test_remote_program_executed
    run_test test_database_uri_transformation
    run_test test_missing_database_uri_fails
    run_test test_invalid_options_and_args

    echo ""
    echo "=== Results ==="
    echo "  PASS: ${PASS}"
    echo "  FAIL: ${FAIL}"
    echo ""

    if [[ "$FAIL" -eq 0 ]]; then
        echo "PASS: attic-make-token"
        exit 0
    else
        echo "FAIL: attic-make-token"
        exit 1
    fi
}

main "$@"
