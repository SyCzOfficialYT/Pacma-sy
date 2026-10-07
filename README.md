# Pacma-sy 🟡👻

A CachyOS / Arch Linux `pacman -Syu` wrapper with a live Pac-Man inspired terminal UI.

## Features

- Real `pacman -Syu` execution through a PTY, so the package manager keeps a real TTY.
- Live package/download progress parsing.
- A separate randomly generated mini Pac-Man maze for every visible package.
- Pac-Man moves through the pellet path according to the package's real download percentage.
- Ghosts move continuously while the download is running.
- Different maze seeds per package, so the games are not identical.
- Works without third-party Python packages.
- `--demo` mode for previewing the UI without touching the system.
- Falls back gracefully when terminal dimensions are small.

## Install

```bash
git clone https://github.com/SyCzOfficialYT/Pacma-sy.git
cd Pacma-sy
chmod +x install.sh
./install.sh
```

Then run:

```bash
pacsy
```

Preview only:

```bash
pacsy --demo
```

The live mode runs the normal `sudo pacman -Syu` transaction; it does not modify pacman's configuration.

## Requirements

- CachyOS / Arch Linux
- Python 3.10+
- pacman
- sudo
- A terminal with ANSI escape support

Pacman itself normally exposes individual package progress bars while downloading, which Pacma-sy uses as its live progress source. The wrapper keeps pacman attached to a pseudo-terminal so its interactive behaviour is preserved.

## Controls

During a real transaction, keyboard input is forwarded to pacman. This means confirmation prompts still behave like normal pacman prompts.

- `Ctrl+C` — interrupt
- Normal pacman confirmation/input — forwarded directly

## Architecture

```
pacsy
  │
  ├── PTY
  │    └── sudo pacman -Syu
  │
  ├── parser
  │    ├── package name
  │    ├── percentage
  │    └── download statistics
  │
  └── renderer
       ├── CachyOS header
       ├── repository status
       ├── package table
       └── independent Pac-Man mini-games
```

## License

MIT
