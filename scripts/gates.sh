#!/usr/bin/env bash
# pitoolbox - alle controles die CI ook draait, in dezelfde volgorde (CODE_GUIDELINES.md).
#
# Gebruik:  scripts/gates.sh            alles
#           scripts/gates.sh --no-net   zonder de controles die internet nodig hebben (uv audit)
#
# Exitcode 0 alleen als elke gedraaide controle slaagt. Controles die hier niet kunnen draaien
# (tool ontbreekt, --no-net) staan aan het eind als "overgeslagen", zodat een groene run nooit
# stilzwijgend minder controleert dan CI.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

no_net=0
for arg in "$@"; do
    case "$arg" in
        --no-net) no_net=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "onbekende optie: $arg" >&2; exit 2 ;;
    esac
done

failed=()
skipped=()

run() {
    local name="$1"
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok"
    else
        echo "    MISLUKT ($name)"
        failed+=("$name")
    fi
}

run "ruff check" uv run --frozen ruff check src tests scripts
run "ruff format" uv run --frozen ruff format --check src tests scripts
run "mypy" uv run --frozen mypy
run "pytest" uv run --frozen pytest -q

if (( no_net )); then
    skipped+=("uv audit (--no-net)")
else
    # `uv audit` is nog experimenteel in uv 0.13; de preview-vlag zet alleen de waarschuwing uit.
    run "uv audit" uv audit --locked --preview-features audit-command
fi

# Geen shellcheck op PATH: de vastgepinde binary uit PyPI (shellcheck-py) via uvx, als er
# internet is. Dan draait ook tests/test_deploy.py::test_install_sh_shellcheck mee.
SHELLCHECK_PY="shellcheck-py==0.11.0.1"
if command -v shellcheck >/dev/null; then
    run "shellcheck" shellcheck deploy/install.sh scripts/gates.sh
elif (( ! no_net )) && sc_path="$(uvx --from "$SHELLCHECK_PY" python -c 'import shutil; print(shutil.which("shellcheck") or "")')" && [[ -n "$sc_path" ]]; then
    run "shellcheck" "$sc_path" deploy/install.sh scripts/gates.sh
    run "pytest shellcheck" env PATH="$(dirname "$sc_path"):$PATH" \
        uv run --frozen pytest -q tests/test_deploy.py -k shellcheck
else
    skipped+=("shellcheck (niet geïnstalleerd; zonder --no-net haalt uvx $SHELLCHECK_PY)")
fi

echo
if (( ${#skipped[@]} )); then
    printf 'overgeslagen: %s\n' "${skipped[@]}"
fi
if (( ${#failed[@]} )); then
    printf 'MISLUKT: %s\n' "${failed[@]}"
    exit 1
fi
echo "alle gedraaide controles geslaagd"
