# Read-only terminal result capture PoC (Windows)

This opt-in diagnostic captures one match's raw AoE2Control terminal APIs. It does not make results ledger-eligible and does not modify the standard evaluation config. Keep the replay on the Windows machine; do not upload the replay or game files.

## Run one isolated match

1. Update/deploy the exact reviewed branch head. In CONTROL, assign `evolab_driver` to Player 1, enable its `Read-only result capture PoC` setting, and confirm **Modules See Everything** is enabled. The PoC needs that permission to read both players' `CURRENT_SCORE`.
2. Run from the repository root:

   ```powershell
   python .\ai_lab\auto_runner.py --capture-only
   ```

   This selects at most one pending scheduled match. It records the normal `match_prepared` and `recording_associated` runner evidence, then skips report parsing, result-ledger writes, and fitness input. Do not use ordinary runner mode for this diagnostic. The Lua PoC logs one `EVOLAB_RESULT_CAPTURE_V1:` line at the end and does not dispatch the next match. Confirm that line exists and that the log does not show the regular “本局结束，自动开下一局” message.
3. Copy the complete single sentinel line from CONTROL's DEBUG Log into `observation.txt` in the repository root, preserving the line. Do not include a second capture line. Note the exact associated replay path and generation from the runner console / `lab_data\runner_evidence\gen_N.jsonl`.
4. Set the two executable paths to the installed files and collect their Windows ProductVersion values. Then run the local validator, substituting the actual generation and replay path:

   ```powershell
   $GameExe = 'C:\path\to\AoE2DE_s.exe'
   $ControlExe = 'C:\path\to\AoE2Control.exe'
   $GameVersion = (Get-Item $GameExe).VersionInfo.ProductVersion
   $ControlVersion = (Get-Item $ControlExe).VersionInfo.ProductVersion
   python .\ai_lab\result_capture.py `
     --observation .\observation.txt `
     --runner-evidence .\lab_data\runner_evidence\gen_N.jsonl `
     --recording 'C:\path\to\exact-associated-match.aoe2record' `
     --game-version $GameVersion `
     --control-version $ControlVersion `
     --modules-see-everything-confirmed `
     --output .\lab_data\runner_evidence\result_capture_poc.jsonl
   ```

   Omit `--modules-see-everything-confirmed` unless the operator verified that permission in CONTROL; validation must then reject. A successful record contains the exact runner match/slot/installed `.per` identities, replay SHA-256, collector UTC timestamp, versions, raw API values and `ledger_eligible=false`. The replay's size and mtime must still match runner association evidence when hashed.

`captured_at_utc` is the validator's collection time after the match, not a guaranteed callback-time clock. The Lua log's `capture_sequence` and game-time value identify the terminal capture; the PoC rejects a sequence other than the first invocation and rejects missing/failed APIs, missing permission, winner disagreement, ambiguous replay association, or duplicate match IDs. Restore the setting to false after the run. This experiment does not establish an authoritative result source and must not be used to score or evolve genomes.
