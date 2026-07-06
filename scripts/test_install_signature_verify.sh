#!/bin/bash
# Dev test for install.sh's verify_release_signature.
#
# Extracts the real function from install.sh, stubs the network (curl)
# and the apt path (ensure_minisign), signs fixtures with a THROWAWAY
# minisign keypair, and proves the abort/continue matrix:
#
#   valid sig + valid sha           -> continue
#   tampered checksums.txt          -> abort, tarball removed
#   tampered tarball (sha mismatch) -> abort, tarball removed
#   sha mismatch + ALLOW_UNSIGNED   -> abort (escape hatch never covers sha)
#   bad sig + ALLOW_UNSIGNED        -> continue (sha still checked)
#   unsigned release                -> warn + continue
#   unsigned + REQUIRE_SIGNED       -> abort
#   minisign unavailable            -> warn + continue
#   minisign unavailable + REQUIRE  -> abort
#
# Requires: bash, minisign, sha256sum. Run from anywhere:
#   bash scripts/test_install_signature_verify.sh

set -euo pipefail

INSTALL_SH="$(cd "$(dirname "$0")/.." && pwd)/install.sh"

for tool in minisign sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "SKIP: $tool not installed (required for this test)" >&2
    exit 0
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Throwaway keypair — explicit paths so ~/.minisign is never touched.
minisign -G -W -p "$WORK/test.pub" -s "$WORK/test.key" >/dev/null 2>&1
TEST_PUBKEY="$(sed -n '2p' "$WORK/test.pub")"

# Fixture release: a fake tarball + checksums.txt signed by the test key.
TARBALL_NAME="jarvis-node-9.9.9-arm64.tar.gz"
FIXTURES="$WORK/fixtures"
mkdir -p "$FIXTURES"
head -c 4096 /dev/urandom > "$FIXTURES/$TARBALL_NAME"
(cd "$FIXTURES" && sha256sum "$TARBALL_NAME" > checksums.txt)
minisign -S -s "$WORK/test.key" -m "$FIXTURES/checksums.txt" >/dev/null 2>&1

# Tampered variant: appended line invalidates the signature but leaves
# the tarball's own checksum line intact (exercises the ALLOW_UNSIGNED
# path all the way through the sha256 check).
TAMPERED="$WORK/tampered"
mkdir -p "$TAMPERED"
cp "$FIXTURES/$TARBALL_NAME" "$FIXTURES/checksums.txt.minisig" "$TAMPERED/"
{ cat "$FIXTURES/checksums.txt"; echo "0000 evil-extra-asset"; } > "$TAMPERED/checksums.txt"

# Unsigned variant: release assets exist but no .minisig.
UNSIGNED="$WORK/unsigned"
mkdir -p "$UNSIGNED"
cp "$FIXTURES/$TARBALL_NAME" "$FIXTURES/checksums.txt" "$UNSIGNED/"

# The function under test, verbatim from install.sh.
VERIFY_FN="$(sed -n '/^verify_release_signature() {/,/^}/p' "$INSTALL_SH")"
if [ -z "$VERIFY_FN" ]; then
  echo "FAIL: could not extract verify_release_signature from $INSTALL_SH" >&2
  exit 1
fi

PASS=0
FAIL=0

# run_case <name> <fixture_dir> <expect_exit> <expect_tarball_kept> <expect_output_re> [env...]
run_case() {
  local name="$1" fixture_dir="$2" expect_exit="$3" expect_kept="$4" expect_re="$5"
  shift 5

  local case_dir="$WORK/case-$name"
  mkdir -p "$case_dir"
  local tarball_path="$case_dir/jarvis-node.tarball.tmp"
  cp "$FIXTURES/$TARBALL_NAME" "$tarball_path"

  local out ec=0
  out="$(env "$@" bash -c "
    set -uo pipefail
    REPO='alexberardi/jarvis-node-setup'
    TAG='v9.9.9'
    MINISIGN_PUBKEY='$TEST_PUBKEY'
    info()  { printf 'INFO: %s\n' \"\$1\"; }
    warn()  { printf 'WARN: %s\n' \"\$1\"; }
    error() { printf 'ERROR: %s\n' \"\$1\"; exit 1; }
    ensure_minisign() { [ \"\${STUB_NO_MINISIGN:-0}\" != 1 ]; }
    curl() {
      local out='' url=''
      while [ \$# -gt 0 ]; do
        case \"\$1\" in
          -o) out=\"\$2\"; shift 2 ;;
          http*) url=\"\$1\"; shift ;;
          *) shift ;;
        esac
      done
      local name=\"\${url##*/}\"
      if [ -f \"$fixture_dir/\$name\" ]; then
        cp \"$fixture_dir/\$name\" \"\$out\"
        return 0
      fi
      return 22
    }
    $VERIFY_FN
    verify_release_signature '$TARBALL_NAME' '$tarball_path'
  " 2>&1)" || ec=$?

  local ok=1
  [ "$ec" -eq "$expect_exit" ] || { echo "  exit: got $ec, want $expect_exit"; ok=0; }
  if [ "$expect_kept" = "1" ]; then
    [ -f "$tarball_path" ] || { echo "  tarball: removed, expected kept"; ok=0; }
  else
    [ ! -f "$tarball_path" ] || { echo "  tarball: kept, expected removed"; ok=0; }
  fi
  if ! grep -qE "$expect_re" <<< "$out"; then
    echo "  output: missing /$expect_re/"
    ok=0
  fi

  if [ "$ok" -eq 1 ]; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name"
    awk '{ print "    | " $0 }' <<< "$out"
    FAIL=$((FAIL + 1))
  fi
}

run_case valid_sig_valid_sha        "$FIXTURES" 0 1 'sha256 verified'
run_case tampered_checksums_aborts  "$TAMPERED" 1 0 'ERROR: Signature verification FAILED'
run_case badsig_allow_unsigned      "$TAMPERED" 0 1 'WARN: WARNING: checksums.txt signature is INVALID' \
  JARVIS_ALLOW_UNSIGNED_UPDATE=1
run_case unsigned_warns_continues   "$UNSIGNED" 0 1 'WARN: WARNING: release v9.9.9 is UNSIGNED'
run_case unsigned_require_aborts    "$UNSIGNED" 1 0 'ERROR: Release v9.9.9 has no checksums' \
  JARVIS_REQUIRE_SIGNED_UPDATE=1
run_case no_minisign_warns          "$FIXTURES" 0 1 'WARN: WARNING: minisign could not be installed' \
  STUB_NO_MINISIGN=1
run_case no_minisign_require_aborts "$FIXTURES" 1 0 'ERROR: minisign could not be installed' \
  STUB_NO_MINISIGN=1 JARVIS_REQUIRE_SIGNED_UPDATE=1

# sha-mismatch cases: point the fixtures at a checksums.txt whose tarball
# line no longer matches the bytes on disk.
MISMATCH="$WORK/mismatch"
mkdir -p "$MISMATCH"
cp "$FIXTURES/checksums.txt" "$FIXTURES/checksums.txt.minisig" "$MISMATCH/"
head -c 4096 /dev/urandom > "$MISMATCH/$TARBALL_NAME"
mismatch_case() {
  local name="$1" expect_re="$2"; shift 2
  local case_dir="$WORK/case-$name"
  mkdir -p "$case_dir"
  local tarball_path="$case_dir/jarvis-node.tarball.tmp"
  cp "$MISMATCH/$TARBALL_NAME" "$tarball_path"
  local out ec=0
  out="$(env "$@" bash -c "
    set -uo pipefail
    REPO='alexberardi/jarvis-node-setup'
    TAG='v9.9.9'
    MINISIGN_PUBKEY='$TEST_PUBKEY'
    info()  { printf 'INFO: %s\n' \"\$1\"; }
    warn()  { printf 'WARN: %s\n' \"\$1\"; }
    error() { printf 'ERROR: %s\n' \"\$1\"; exit 1; }
    ensure_minisign() { return 0; }
    curl() {
      local out='' url=''
      while [ \$# -gt 0 ]; do
        case \"\$1\" in
          -o) out=\"\$2\"; shift 2 ;;
          http*) url=\"\$1\"; shift ;;
          *) shift ;;
        esac
      done
      local name=\"\${url##*/}\"
      if [ -f \"$MISMATCH/\$name\" ]; then
        cp \"$MISMATCH/\$name\" \"\$out\"
        return 0
      fi
      return 22
    }
    $VERIFY_FN
    verify_release_signature '$TARBALL_NAME' '$tarball_path'
  " 2>&1)" || ec=$?
  if [ "$ec" -eq 1 ] && [ ! -f "$tarball_path" ] && grep -qE "$expect_re" <<< "$out"; then
    echo "PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $name (exit=$ec)"
    awk '{ print "    | " $0 }' <<< "$out"
    FAIL=$((FAIL + 1))
  fi
}
mismatch_case tampered_tarball_aborts 'ERROR: Tarball sha256 mismatch'
mismatch_case sha_mismatch_ignores_allow_unsigned 'ERROR: Tarball sha256 mismatch' \
  JARVIS_ALLOW_UNSIGNED_UPDATE=1

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
