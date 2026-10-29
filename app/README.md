# katet.app

The board in a native macOS window, the coordinator's state in the menu bar, and notifications
when something needs you.

```bash
katet app build      # builds and installs ~/Applications/katet.app (opens any workspace)
```

- **Start:** open `katet` from `~/Applications` (or Spotlight). It opens the last workspace, or asks
  for one (a folder with `katet.conf`; an empty folder can be turned into one on the spot). Other ways
  to open a workspace: the **Workspace** menu with the recent ones, a folder dropped on the Dock icon,
  or `open -a katet ~/work`. For the open workspace it uses a running board server on the port from
  `katet.conf` or starts its own, and starts the coordinator if none runs.
- **Quit (⌘Q):** the app and the board server it started stop; **the coordinator keeps running**.
  Stop it from the menu-bar icon (*Stop Coordinator* waits for a running worker; while it waits,
  *Kill the Worker and Stop Now* is offered), or `kill -INT $(cat .coordinator/coordinator.pid)`.
- **Closing the window** only hides it; the menu-bar icon and notifications stay. Click the Dock
  icon or *Open Board* to get it back.
- **Menu-bar icon:** the `k` mark. A dot and a number appear when something needs you (stuck or
  looping tasks, `coordinator.tools` not readable); `…` while a worker runs; dimmed when no
  coordinator runs. Brand sources: `assets/brand/`, built files: `app/icon/`.
- **Notifications:** a worker replied, a new question or step for you, a task stuck in
  in-progress, a possible worker loop, `coordinator.tools` not readable. Clicking one opens the task.
- **Logs:** `.coordinator/coordinator.out` (coordinator started by the app), `.coordinator/board.out`
  (board server started by the app).

The window shows the same `board/index.html` as the browser, so board changes need no rebuild —
only changes to `app/main.swift` do; `katet app build` quits a running copy and relaunches it.
The framework path is baked in at build time (`KatetFramework` in `Info.plist`); rebuild after moving
it. `--id` sets the bundle id (default `local.katet`).
