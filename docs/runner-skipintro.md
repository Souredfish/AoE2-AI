# Runner-only AoE2 startup intro skip

The runner passes the fixed `SKIPINTRO` token only in its Steam `-applaunch` process invocation. It does not edit Steam's per-user `LaunchOptions` (`localconfig.vdf`), game files, or evaluation config. Launching AoE2 normally from the Steam library therefore uses the user's unchanged launch settings and should retain the intro.

Steam documents forwarding an app command line through its Steam URL launch flow in `ISteamApps::GetLaunchCommandLine`; Steam's command-line launcher also accepts app launch arguments. AoE2's `SKIPINTRO` token is game-specific and is not an official AoE2-supported API. A Windows check has shown the token works when stored in Steam's persistent app option, but the runner-only transport still requires the test below to verify that Steam forwards it without a confirmation dialog.

## Windows acceptance check

1. Ensure AoE2's Steam **Launch Options** field is empty (or at least contains no `SKIPINTRO`) and record a hash/copy of the relevant `localconfig.vdf` for a before/after comparison. Do not edit it for this test.
2. With the game closed, run the runner's normal startup path. Confirm the launch command contains exactly the runner-only `SKIPINTRO`, Steam starts the configured game, no confirmation/login/update dialog appears, no intro video window appears, and the main menu becomes ready through the normal process/window gate.
3. Stop/close the game. Launch it from the Steam Library without the runner. Confirm the intro video appears and the game reaches the menu.
4. Confirm `localconfig.vdf` is byte-identical and no persistent LaunchOptions value was added. Do not click or bypass any prompt; stop and report if one appears. Do not run a match for this launch-option check.

The Windows test should note whether Steam was already running, process ancestry, game version, prompt/video-window observations, and menu readiness for each launch. The argument can only affect a process launched by the runner; it cannot add a skip to a game process that was already started from Steam.
