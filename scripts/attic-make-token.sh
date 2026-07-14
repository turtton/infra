#!/usr/bin/env bash
# Generate an Attic cache token by exec-ing into the attic server pod.
#
# Usage:
#   scripts/attic-make-token.sh --sub <SUBJECT> --validity <VALIDITY> \
#     (--admin | <permission-options>)
#
# Permission options (repeatable, each requires a pattern):
#   --pull <PATTERN>
#   --push <PATTERN>
#   --delete <PATTERN>
#   --create-cache <PATTERN>
#   --configure-cache <PATTERN>
#   --configure-cache-retention <PATTERN>
#   --destroy-cache <PATTERN>
#
# The token is printed to stdout exactly as returned by atticadm.
# Progress and error messages go to stderr.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: attic-make-token.sh --sub <SUBJECT> --validity <VALIDITY> (--admin | <permission-options>)

Required:
  --sub <SUBJECT>       Token subject
  --validity <VALIDITY> Token validity period (e.g. 30d, 90d, 1y)

Permission options (repeatable, each requires a pattern):
  --pull <PATTERN>
  --push <PATTERN>
  --delete <PATTERN>
  --create-cache <PATTERN>
  --configure-cache <PATTERN>
  --configure-cache-retention <PATTERN>
  --destroy-cache <PATTERN>

  --admin               Shortcut for all seven permissions with pattern '*'
                        Mutually exclusive with granular permission flags.

Examples:
  scripts/attic-make-token.sh --sub ci --validity 90d --admin
  scripts/attic-make-token.sh --sub deploy --validity 30d --push 'my-cache' --pull 'my-cache'
EOF
}

error() {
    echo "error: $*" >&2
    exit 2
}

sub=""
validity=""
admin=false
attic_args=()

while [[ $# -gt 0 ]]; do
    arg="$1"
    eq_value=false
    if [[ "$arg" == --*=* ]]; then
        eq_value=true
        opt="${arg%%=*}"
        val="${arg#*=}"
    else
        opt="$arg"
        val=""
    fi

    case "$opt" in
        -h|--help)
            usage
            exit 0
            ;;
        --sub)
            if [[ -n "$sub" ]]; then
                error "--sub can only be specified once"
            fi
            if $eq_value; then
                if [[ -z "$val" ]]; then
                    error "--sub value cannot be empty"
                fi
            else
                if [[ $# -lt 2 ]]; then
                    error "--sub requires a value"
                fi
                val="$2"
                shift
                if [[ -z "$val" ]]; then
                    error "--sub value cannot be empty"
                fi
            fi
            sub="$val"
            shift
            ;;
        --validity)
            if [[ -n "$validity" ]]; then
                error "--validity can only be specified once"
            fi
            if $eq_value; then
                if [[ -z "$val" ]]; then
                    error "--validity value cannot be empty"
                fi
            else
                if [[ $# -lt 2 ]]; then
                    error "--validity requires a value"
                fi
                val="$2"
                shift
                if [[ -z "$val" ]]; then
                    error "--validity value cannot be empty"
                fi
            fi
            validity="$val"
            shift
            ;;
        --admin)
            if $eq_value; then
                error "--admin does not accept a value"
            fi
            if $admin; then
                error "--admin can only be specified once"
            fi
            if [[ ${#attic_args[@]} -gt 0 ]]; then
                error "--admin is mutually exclusive with granular permission flags"
            fi
            admin=true
            shift
            ;;
        --pull|--push|--delete|--create-cache|--configure-cache|--configure-cache-retention|--destroy-cache)
            if $admin; then
                error "granular permission flags are mutually exclusive with --admin"
            fi
            if $eq_value; then
                if [[ -z "$val" ]]; then
                    error "$opt value cannot be empty"
                fi
            else
                if [[ $# -lt 2 ]]; then
                    error "$opt requires a value"
                fi
                val="$2"
                shift
                if [[ -z "$val" ]]; then
                    error "$opt value cannot be empty"
                fi
            fi
            attic_args+=("$opt" "$val")
            shift
            ;;
        --*)
            error "unknown option: $1"
            ;;
        *)
            error "unexpected argument: $1"
            ;;
    esac
done

if [[ -z "$sub" ]]; then
    error "--sub is required"
fi

if [[ -z "$validity" ]]; then
    error "--validity is required"
fi

if ! $admin && [[ ${#attic_args[@]} -eq 0 ]]; then
    error "at least one permission flag or --admin is required"
fi

if $admin; then
    attic_args=(
        --pull '*'
        --push '*'
        --delete '*'
        --create-cache '*'
        --configure-cache '*'
        --configure-cache-retention '*'
        --destroy-cache '*'
    )
fi

attic_args+=("--sub" "$sub" "--validity" "$validity")

if ! command -v kubectl >/dev/null 2>&1; then
    echo "error: kubectl not found in PATH" >&2
    exit 127
fi

remote_program=$(cat <<'REMOTE_EOF'
if [ -z "${DATABASE_URI:-}" ]; then
    echo "error: DATABASE_URI is not set" >&2
    exit 1
fi
db_url="$DATABASE_URI"
case "$db_url" in
    postgresql://*)
        db_url="postgres://${db_url#postgresql://}"
        ;;
esac
export ATTIC_SERVER_DATABASE_URL="$db_url"
exec atticadm make-token -f /etc/attic/server.toml "$@"
REMOTE_EOF
)

kubectl wait \
    --namespace attic \
    --for=condition=Available \
    deployment/attic \
    --timeout=120s >&2

kubectl exec \
    --namespace attic \
    deployment/attic \
    --container attic \
    -- \
    sh -c "$remote_program" \
    sh \
    "${attic_args[@]}"
