from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install" / "devloop.sh"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")


def _function(source: str, name: str) -> str:
    """Extract definitions only: never source or execute the installer file."""
    lines = source.splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _probe_script(scenario: str) -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    definitions = "\n".join(
        _function(source, name)
        for name in (
            "cleanup_candidate",
            "validate_release_command",
            "initialize_user_state",
            "recover_transaction",
            "main",
        )
    )
    # All filesystem, process and installation seams below are in-memory adapters.
    # In particular, never load clone_candidate/install_runtime/transaction.py.
    return (
        r"""
set -euo pipefail
PATH=''
readonly PATH
INSTALL_DIR=/__devloop_bash_probe_no_filesystem__/install
SCRIPT_DIR=/__devloop_bash_probe_no_filesystem__/source/install
CANDIDATE_DIR=''
CANDIDATE_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TRANSACTION_ID=probe-transaction
ROLLBACK=0
INSTALL_SKILLS=1
DEVLOOP_TESTING=1
PROBE_LAYOUT=0
PROBE_JOURNAL=0
PROBE_FAILURE=''
PROBE_ACTION=NEEDS_ADOPTION
event() { printf 'EVENT:%s\n' "$*" >&2; }
die() { event "error:$*"; exit 1; }
log() { event "log:$*"; }
find_python() { printf '%s\n' probe_python; }
parse_args() { :; }
absolute_install_dir() { :; }
begin_transaction() { event begin; }
begin_legacy_migration() { event legacy; }
current_release() { printf '%s\n' "$INSTALL_DIR/releases/$CANDIDATE_COMMIT"; }
clone_candidate() {
  CANDIDATE_DIR=/__devloop_bash_probe_no_filesystem__/.install.candidate-probe
  event clone
}
install_runtime() {
  event runtime
  if [[ "$PROBE_FAILURE" == runtime ]]; then return 91; fi
}
install_capabilities() { event capabilities; }
command() {
  if [[ "$*" == '-v git' ]]; then return 0; fi
  event "unexpected-command:$*"
  return 97
}
git() { event "unexpected-git:$*"; return 97; }
rm() { event "delete:$*"; }
mkdir() { event "unexpected-mkdir:$*"; return 97; }
python() { event "unexpected-python:$*"; return 97; }
python3() { event "unexpected-python3:$*"; return 97; }
probe_python() {
  if [[ "$1" == -B ]]; then shift; fi
  local operation="$2"
  if [[ "$1" == -m ]]; then operation="$3"; fi
  event "$operation"
  if [[ "$PROBE_FAILURE" == "$operation" ]]; then
    if [[ "$operation" == recover ]]; then
      printf '%s\t%s\n' "$PROBE_ACTION" "$INSTALL_DIR/releases/$CANDIDATE_COMMIT"
    fi
    return 91
  fi
  case "$operation" in
    prepare) printf '%s\n' "$INSTALL_DIR/releases/$CANDIDATE_COMMIT" ;;
    recover) printf '%s\t%s\n' "$PROBE_ACTION" "$INSTALL_DIR/releases/$CANDIDATE_COMMIT" ;;
    publish|commit|abort|prepare-user-state) ;;
    *) event "unexpected-python-operation:$operation"; return 97 ;;
  esac
}
[() {
  if [[ "$#" == 3 && "$1" == -f ]]; then
    case "$2" in
      "$INSTALL_DIR/bootstrap/layout.json"|"$INSTALL_DIR/bootstrap/transaction.py")
        [[ "$PROBE_LAYOUT" == 1 ]] ;;
      "$INSTALL_DIR/bootstrap/install-transaction.json") [[ "$PROBE_JOURNAL" == 1 ]] ;;
      "$INSTALL_DIR/bootstrap/current.json") return 1 ;;
      *) event "unexpected-file-test:$2"; exit 97 ;;
    esac
  elif [[ "$#" == 3 && "$1" == -d ]]; then
    [[ "$2" == /__devloop_bash_probe_no_filesystem__/.install.candidate-probe ]]
  else
    builtin [ "$@"
  fi
}
readonly -f event die log find_python parse_args absolute_install_dir begin_transaction
readonly -f begin_legacy_migration current_release clone_candidate install_runtime
readonly -f install_capabilities command git rm mkdir python python3 probe_python '['
"""
        + definitions
        + "\n"
        + scenario
        + "\n"
    )


@unittest.skipUnless(BASH, "Bash is required for the in-memory wrapper probes")
class PortableBashRecoverySafetyTests(unittest.TestCase):
    def run_probe(self, scenario: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH"}
            and not key.startswith("BASH_FUNC_")
            and not key.upper().startswith("GIT_")
        }
        script = _probe_script(scenario)
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-s"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            cwd=ROOT,
            env=environment,
            timeout=10,
        )
        self.assertNotIn("unexpected-", result.stderr, result.stderr)
        self.assertNotIn("command not found", result.stderr, result.stderr)
        return result

    def test_prepare_failure_retains_transaction_candidate_for_retry(self) -> None:
        result = self.run_probe("PROBE_FAILURE=prepare\nmain")
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertIn("EVENT:prepare\n", result.stderr)
        self.assertNotIn("EVENT:delete:", result.stderr)
        self.assertNotIn("EVENT:prepare-user-state\n", result.stderr)
        self.assertNotIn("EVENT:commit\n", result.stderr)
        self.assertNotIn("EVENT:capabilities\n", result.stderr)

    def test_recovery_adoption_failure_stops_before_activation_and_new_clone(self) -> None:
        result = self.run_probe(
            "PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_FAILURE=prepare-user-state\nmain"
        )
        self.assertNotIn("EVENT:commit\n", result.stderr)
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertIn("EVENT:prepare-user-state\n", result.stderr)
        self.assertNotIn("EVENT:capabilities\n", result.stderr)
        self.assertNotIn("EVENT:clone\n", result.stderr)

    def test_failed_recovery_command_does_not_use_partial_success_output(self) -> None:
        result = self.run_probe("PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_FAILURE=recover\nmain")
        self.assertEqual(result.returncode, 91, result.stderr)
        self.assertIn("EVENT:recover\n", result.stderr)
        self.assertNotIn("EVENT:prepare-user-state\n", result.stderr)
        self.assertNotIn("EVENT:commit\n", result.stderr)
        self.assertNotIn("EVENT:capabilities\n", result.stderr)
        self.assertNotIn("EVENT:clone\n", result.stderr)

    def test_failed_recovery_commit_does_not_install_capabilities_or_clone(self) -> None:
        for action in ("NEEDS_ADOPTION", "READY_TO_SWITCH"):
            with self.subTest(action=action):
                result = self.run_probe(
                    f"PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_ACTION={action}\n"
                    "PROBE_FAILURE=commit\nmain"
                )
                self.assertEqual(result.returncode, 91, result.stderr)
                self.assertIn("EVENT:commit\n", result.stderr)
                self.assertNotIn("EVENT:capabilities\n", result.stderr)
                self.assertNotIn("EVENT:clone\n", result.stderr)

    def test_recovery_failures_propagate_when_main_is_itself_conditional(self) -> None:
        for operation in ("recover", "prepare-user-state", "commit"):
            with self.subTest(operation=operation):
                result = self.run_probe(
                    f"PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_FAILURE={operation}\n"
                    "if main; then event unexpected-success; else exit $?; fi"
                )
                self.assertEqual(result.returncode, 91, result.stderr)
                self.assertNotIn("EVENT:capabilities\n", result.stderr)
                self.assertNotIn("EVENT:clone\n", result.stderr)

    def test_successful_recovery_finishes_without_a_new_candidate(self) -> None:
        for action in ("NEEDS_ADOPTION", "READY_TO_SWITCH", "COMPLETE"):
            with self.subTest(action=action):
                result = self.run_probe(
                    f"PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_ACTION={action}\nmain"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("EVENT:capabilities\n", result.stderr)
                self.assertNotIn("EVENT:clone\n", result.stderr)
                self.assertNotIn("EVENT:delete:", result.stderr)
                self.assertEqual(
                    "EVENT:prepare-user-state\n" in result.stderr, action == "NEEDS_ADOPTION"
                )
                self.assertEqual("EVENT:commit\n" in result.stderr, action != "COMPLETE")
                if action == "NEEDS_ADOPTION":
                    self.assertLess(
                        result.stderr.index("EVENT:prepare-user-state\n"),
                        result.stderr.index("EVENT:commit\n"),
                    )
                if action != "COMPLETE":
                    self.assertLess(
                        result.stderr.index("EVENT:commit\n"),
                        result.stderr.index("EVENT:capabilities\n"),
                    )

    def test_failed_adoption_can_be_retried_without_cloning_or_early_activation(self) -> None:
        result = self.run_probe(
            "PROBE_LAYOUT=1\nPROBE_JOURNAL=1\nPROBE_FAILURE=prepare-user-state\n"
            "if main; then event unexpected-success; else event retained-for-retry; fi\n"
            "PROBE_FAILURE=''\nmain"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        before_retry, after_retry = result.stderr.split("EVENT:retained-for-retry\n")
        self.assertNotIn("EVENT:commit\n", before_retry)
        self.assertNotIn("EVENT:capabilities\n", before_retry)
        self.assertIn("EVENT:prepare-user-state\n", after_retry)
        self.assertIn("EVENT:commit\n", after_retry)
        self.assertIn("EVENT:capabilities\n", after_retry)
        self.assertNotIn("EVENT:clone\n", result.stderr)
        self.assertNotIn("EVENT:delete:", result.stderr)

    def test_no_journal_starts_a_new_candidate_and_commits(self) -> None:
        result = self.run_probe("PROBE_LAYOUT=1\nmain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("EVENT:recover\n", result.stderr)
        self.assertIn("EVENT:clone\n", result.stderr)
        self.assertIn("EVENT:prepare-user-state\n", result.stderr)
        self.assertIn("EVENT:commit\n", result.stderr)
        self.assertIn("EVENT:capabilities\n", result.stderr)
        self.assertNotIn("EVENT:delete:", result.stderr)

    def test_failure_before_prepare_still_cleans_the_wrapper_owned_candidate(self) -> None:
        for operation in ("runtime", "publish"):
            with self.subTest(operation=operation):
                result = self.run_probe(f"PROBE_FAILURE={operation}\nmain")
                self.assertEqual(result.returncode, 91, result.stderr)
                self.assertIn(
                    "EVENT:delete:-rf -- /__devloop_bash_probe_no_filesystem__", result.stderr
                )
                self.assertNotIn("EVENT:prepare\n", result.stderr)
                self.assertNotIn("EVENT:prepare-user-state\n", result.stderr)
                self.assertNotIn("EVENT:commit\n", result.stderr)
                self.assertNotIn("EVENT:capabilities\n", result.stderr)


if __name__ == "__main__":
    unittest.main()
