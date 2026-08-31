# Pixel Buds for Omarchy

Pixel Buds battery and listening-mode control, right in the Omarchy bar.

![screenshot](screenshot.png)

## Features

- **Per-bud and case battery** with charging and in-case state. The case only
  reports while a bud is docked (it has no radio of its own), so the last
  reading is cached and shown with a "last seen" age — same trick Android uses.
- **Listening-mode panel**: Off / Noise Cancelling / Transparency / Adaptive,
  clickable or keyboard-navigable.
- **Quick ANC cycling**: right- or middle-click the bar icon to cycle modes
  without opening the panel.
- **Event-driven**: subscribes to BlueZ D-Bus signals via `gdbus`, so the icon
  appears within about a second of the buds connecting — and while they're
  disconnected the plugin polls nothing at all.

The bar icon is the closed charging case, drawn to the proportions of the real
thing.

## Requirements

- `pbpctrl` on your `PATH` (the plugin also looks in `~/.local/bin` and
  `~/.cargo/bin`). Install with `cargo install pbpctrl` or from the AUR.
- BlueZ (`bluetoothctl`) and glib2 (`gdbus`) — both ship with Omarchy.
- Pixel Buds supported by `pbpctrl` (Pixel Buds Pro generation).

## Install

```bash
omarchy plugin add https://github.com/rdoupe/omarchy-pixelbuds.git --enable
```

## Settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `pollIntervalSec` | 30 | Battery/ANC poll interval while connected (the panel polls faster while open) |

Configure via the bar widget settings or directly in
`~/.config/omarchy/shell.json`.

## IPC

The widget exposes the IPC target `douper.pixelbuds` with methods `open`,
`close`, `toggle`, `refresh`, `cycleAnc`, and `setAnc(mode)` where mode is one
of `off`, `active`, `aware`, `adaptive`:

```bash
omarchy-shell douper.pixelbuds cycleAnc
omarchy-shell douper.pixelbuds setAnc aware
```

Handy for keybindings.

## How it works

`status.sh` first does a cheap `bluetoothctl` check for a connected pair; only
then does it talk to the buds over RFCOMM via `pbpctrl` for battery, placement,
and ANC state. Connect/disconnect detection is event-driven: a `gdbus` signal
subscription on `org.bluez` triggers a refresh the moment any device's
`Connected` state flips, with a short follow-up pass once the buds' RFCOMM
channel settles.

## License

[MIT](LICENSE)
