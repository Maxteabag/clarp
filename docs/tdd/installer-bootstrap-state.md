# Archive bootstrap, staging cleanup, and update identity

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R15** `tests/regression/test_installer_failures.py::test_quick_start_removes_its_downloaded_source_tree`: exec replaces get.sh, discarding its EXIT cleanup trap. Temporary source and persisted source metadata must be handled together; do not merely delete a path an updater still needs.
- **R16** `tests/regression/test_installer_failures.py::test_advertised_pipe_install_preserves_interactive_terminal_input`: The advertised pipe hands non-TTY stdin to setup. The failure is real; the fix test needs a controlling terminal, not only openpty file descriptors, before testing /dev/tty handoff.
- **R19** `tests/regression/test_installer_failures.py::test_failure_before_activation_removes_the_incomplete_release_directory`: Environment setup can fail after moving staging into releases and before installing rollback cleanup. Incomplete directories accumulate; retain active/previous releases and remove only the failed staged release.
- **R36** `tests/unit/test_clarp_admin.py::test_doctor_checks_the_backend_selected_at_setup`: Doctor accepts any installed Claude/Codex binary even when setup chose the missing one. Adapted the new runtime service mock so the test now reaches the intended configured-backend assertion.
- **R40** `tests/unit/test_server_update.py::test_in_app_update_remote_has_the_same_canonical_fallback_as_admin`: The in-app updater and admin updater resolve absent remote metadata differently. Use the same canonical fallback while preserving explicit persisted/source overrides.

## Implementation and verification

Keep the downloader cleanup trap alive without persisting a deleted source checkout. Test pipe installation with a controlling terminal and noninteractive flags. Define and wire an archive identity from the downloader, clean only failed staged releases, check the configured backend in doctor, and share the existing update-remote resolver. Preserve active/previous releases and both service metadata.

## Qualified or excluded claims

- **R17** (needs-contract): Archives currently report unknown-dirty, but CLARP_VERSION is not an install.sh input contract. Test an explicit supplied archive identity wired from the downloader instead of assuming Docker build args apply to host installs.
