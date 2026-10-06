# Read-only terminal result capture PoC (Windows)

This opt-in diagnostic captures one match's raw AoE2Control terminal APIs. It does not make results ledger-eligible, does not modify the standard evaluation config, and does not change ordinary runner behavior while the CONTROL setting is off. Keep the replay on the Windows machine; do not upload the replay or game files.

AoE2Control's supported IPC API connects Lua modules to same-PC programs through a Windows named pipe. Its docs describe `IPC.StartServer` / `IPC.Send`, the outgoing module envelope, and a Python client using `pywin32`: [IPC API](https://aoe2control.github.io/ipc-api/). The Lua sandbox does not expose general file APIs, so the module sends the exact sentinel through IPC and the Python runner persists it. `Log()` remains a human-readable DEBUG fallback only; Headless stdout is not the capture channel.

## Run one isolated match

1. In CONTROL, assign `evolab_driver` to Player 1, enable its `Read-only result capture PoC` setting, and confirm **Modules See Everything** is enabled. The PoC needs that permission to read both players' `CURRENT_SCORE`. Install the Windows Python dependency if it is not already present:

   ```powershell
   python -m pip install -r .\ai_lab\requirements.txt
   ```

2. From the repository root, run the single-match read-only path:

   ```powershell
   python .\ai_lab\auto_runner.py --capture-only
   ```

   This selects at most one pending scheduled match. The runner connects to `\\.\pipe\EvoLabResultCaptureV1`, waits for the module's `capture_ready` IPC envelope, then sends the targeted `bind_match` message and requires a matching `match_bound` acknowledgment within 20 seconds. The Lua module creates that server only when the PoC setting is on, announces readiness only while a client is connected, sends the exact `EVOLAB_RESULT_CAPTURE_V1:` sentinel over IPC at game end, and does not dispatch the next match. The runner records `match_prepared` and `recording_associated`, then skips report parsing, result-ledger writes, and fitness input. If readiness/acknowledgment times out, or the IPC frame is absent, duplicated, malformed, from the wrong module, or mismatched to the match, capture is rejected; it must not be replaced with a hand-copied DEBUG line.

3. The raw source frame is stored first as one row in a separate JSONL file under `lab_data\runner_evidence\result_capture_raw_<match_id>.jsonl`. After the replay is uniquely associated, the runner appends one second row with the same `match_id`, A/B slots, installed `.per` SHA-256 values, replay record ID, and replay SHA-256. The first row contains the original sentinel string verbatim, source module/player, receipt time, and sentinel SHA-256. Existing files are never overwritten; duplicate/missing/invalid source or association rows fail validation. The converted diagnostic output is a different JSONL and retains `ledger_eligible=false`.

4. Set the two executable paths to the installed files and collect their Windows ProductVersion values. Use the exact generation and replay path from `recording_associated`, and the matching raw-capture filename printed by the runner:

   ```powershell
   $GameExe = 'C:\path\to\AoE2DE_s.exe'
   $ControlExe = 'C:\path\to\AoE2Control.exe'
   $GameVersion = (Get-Item $GameExe).VersionInfo.ProductVersion
   $ControlVersion = (Get-Item $ControlExe).VersionInfo.ProductVersion
   $MatchId = 'g0-m0001'
   python .\ai_lab\result_capture.py `
     --raw-capture ".\lab_data\runner_evidence\result_capture_raw_$MatchId.jsonl" `
     --runner-evidence .\lab_data\runner_evidence\gen_N.jsonl `
     --recording 'C:\path\to\exact-associated-match.aoe2record' `
     --game-version $GameVersion `
     --control-version $ControlVersion `
     --modules-see-everything-confirmed `
     --output .\lab_data\runner_evidence\result_capture_poc.jsonl
   ```

   Omit `--modules-see-everything-confirmed` unless the operator verified that permission in CONTROL; validation must then reject. A successful record binds the raw source row to exactly one runner match/slot/installed `.per` association and the unchanged replay's SHA-256. It records the collector UTC time, versions, raw API values, and `ledger_eligible=false`.

`received_at_utc` is the IPC collector's receipt time, not a guaranteed callback-time clock. The Lua observation's sequence and game-time value identify the terminal capture; the validator rejects a sequence other than the first invocation, missing/failed APIs, missing permission, winner disagreement, ambiguous replay association, altered/duplicate raw rows, or duplicate match IDs. The raw JSONL is create-once and append-only by the runner; its SHA-256 detects payload changes but is not a digital signature. Restore the CONTROL setting to false after the run. This experiment does not establish an authoritative result source and must not be used to score or evolve genomes.
