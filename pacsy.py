#!/usr/bin/env python3
"""Pacma-sy: Pac-Man styled live pacman UI for CachyOS/Arch.

Pure stdlib implementation. pacman stays attached to a PTY so the real
transaction and interactive prompts remain intact.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
GRAY = "\033[90m"

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PROGRESS_RE = re.compile(r"(\d{1,3})%")
PKG_RE = re.compile(
    r"([A-Za-z0-9@._+:-]+(?:-[0-9][A-Za-z0-9._:+~-]*)?-x86_64(?:\.pkg\.tar\.[a-z0-9]+)?)"
)
SPEED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB)/s")
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB)")
TIME_RE = re.compile(r"(\d{2}:\d{2})")

# Single-cell glyphs only. This keeps the renderer stable on narrow Android
# terminals where emoji can occupy two columns.
PAC = "◕"
PAC_ALT = "◔"
GHOSTS = ("A", "B", "M")

Point = Tuple[int, int]


def stable_seed(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def neighbours(p: Point, height: int, width: int) -> List[Point]:
    y, x = p
    candidates = [(y, x + 1), (y + 1, x), (y, x - 1), (y - 1, x)]
    return [(ny, nx) for ny, nx in candidates if 0 <= ny < height and 0 <= nx < width]


class Maze:
    """Generate a connected, random Pac-Man-like maze for one package."""

    def __init__(self, seed: int, width: int = 31, height: int = 11) -> None:
        self.width = width if width % 2 == 1 else width - 1
        self.height = height if height % 2 == 1 else height - 1
        self.rng = random.Random(seed)
        self.grid = [["#" for _ in range(self.width)] for _ in range(self.height)]
        self._generate()
        self.start = (1, 1)
        self.goal = (self.height - 2, self.width - 2)
        self.path = self._shortest_path(self.start, self.goal)
        self.power = {
            (1, self.width - 2),
            (self.height - 2, 1),
            self.goal,
        }

    def _generate(self) -> None:
        stack = [(1, 1)]
        self.grid[1][1] = "."
        while stack:
            y, x = stack[-1]
            choices = []
            for dy, dx in ((0, 2), (2, 0), (0, -2), (-2, 0)):
                ny, nx = y + dy, x + dx
                if 1 <= ny < self.height - 1 and 1 <= nx < self.width - 1:
                    if self.grid[ny][nx] == "#":
                        choices.append((ny, nx))
            if not choices:
                stack.pop()
                continue
            ny, nx = self.rng.choice(choices)
            self.grid[(y + ny) // 2][(x + nx) // 2] = "."
            self.grid[ny][nx] = "."
            stack.append((ny, nx))

        # Add a few loops so it feels closer to a Pac-Man board than a
        # strict perfect maze.
        for _ in range(max(3, self.width // 5)):
            y = self.rng.randrange(1, self.height - 1, 2)
            x = self.rng.randrange(1, self.width - 1, 2)
            for dy, dx in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                ny, nx = y + dy, x + dx
                if 1 <= ny < self.height - 1 and 1 <= nx < self.width - 1:
                    self.grid[ny][nx] = "."

    def _shortest_path(self, start: Point, goal: Point) -> List[Point]:
        queue = deque([start])
        parent = {start: None}
        while queue:
            current = queue.popleft()
            if current == goal:
                break
            for nxt in neighbours(current, self.height, self.width):
                y, x = nxt
                if self.grid[y][x] == "#" or nxt in parent:
                    continue
                parent[nxt] = current
                queue.append(nxt)

        if goal not in parent:
            return [start]

        path = []
        current = goal
        while current is not None:
            path.append(current)
            current = parent[current]
        return list(reversed(path))

    def floor(self, p: Point) -> bool:
        y, x = p
        return 0 <= y < self.height and 0 <= x < self.width and self.grid[y][x] != "#"

    def legal(self, p: Point) -> List[Point]:
        return [n for n in neighbours(p, self.height, self.width) if self.floor(n)]


@dataclass
class PackageState:
    name: str
    percent: int = 0
    speed: str = ""
    eta: str = ""
    size: str = ""
    seed: int = 0
    maze: Maze | None = None
    ghosts: List[Point] = field(default_factory=list)
    ghost_colors: List[str] = field(default_factory=lambda: [RED, MAGENTA, CYAN])
    last_update: float = 0.0
    finished_at: float = 0.0

    def __post_init__(self) -> None:
        self.seed = stable_seed(self.name)
        self.maze = Maze(self.seed)
        rng = random.Random(self.seed ^ 0xC0FFEE)
        candidates = [
            p for p in self.maze.path[2:-2]
            if p not in self.maze.power
        ]
        if not candidates:
            candidates = [self.maze.start]
        self.ghosts = [rng.choice(candidates) for _ in range(3)]

    @property
    def path(self) -> List[Point]:
        return self.maze.path if self.maze else []

    def pac_pos(self) -> Point:
        if not self.path:
            return (1, 1)
        idx = min(len(self.path) - 1, int(self.percent * (len(self.path) - 1) / 100))
        return self.path[idx]

    def tick_ghosts(self) -> None:
        if not self.maze:
            return
        target = self.pac_pos()
        for i, pos in enumerate(self.ghosts):
            legal = self.maze.legal(pos)
            if not legal:
                continue
            rng = random.Random(self.seed + i * 7919 + int(time.monotonic() * 5))
            # Mostly chase Pac-Man, occasionally make a random turn.
            if rng.random() < 0.72:
                legal.sort(key=lambda p: abs(p[0] - target[0]) + abs(p[1] - target[1]))
                best = legal[: min(2, len(legal))]
                self.ghosts[i] = rng.choice(best)
            else:
                self.ghosts[i] = rng.choice(legal)


class Pacsy:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.packages: Dict[str, PackageState] = {}
        self.order: List[str] = []
        self.total_percent = 0
        self.total_hint = ""
        self.started = time.monotonic()
        self.demo_start = self.started

    def add_package(self, name: str) -> PackageState:
        if name not in self.packages:
            self.packages[name] = PackageState(name)
            self.order.append(name)
        return self.packages[name]

    def parse(self, text: str) -> None:
        clean = ANSI_RE.sub("", text)
        self.total_hint = self.total_hint

        # Pacman uses carriage returns for live download updates. Treat each
        # rendered line independently instead of accumulating old frames.
        for line in re.split(r"[\r\n]+", clean):
            line = line.strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if not m:
                if ":: Retrieving packages" in line:
                    self.total_hint = "Pakete werden empfangen ..."
                elif ":: Processing package changes" in line:
                    self.total_hint = "Paketänderungen werden verarbeitet ..."
                elif "Synchronizing" in line:
                    self.total_hint = "Paketdatenbanken werden synchronisiert ..."
                elif "upgraded" in line.lower():
                    self.total_hint = "Systemaktualisierung läuft ..."
                continue

            pct = max(0, min(100, int(m.group(1))))
            prefix = line[:m.start()].strip()
            candidates = PKG_RE.findall(prefix)
            if candidates:
                name = candidates[-1]
            else:
                tokens = prefix.split()
                name = tokens[0] if tokens else "package"

            state = self.add_package(name)
            state.percent = pct
            state.last_update = time.monotonic()
            if pct >= 100 and not state.finished_at:
                state.finished_at = time.monotonic()

            sm = SPEED_RE.search(line)
            if sm:
                state.speed = sm.group(0)
            tm = TIME_RE.search(line)
            if tm:
                state.eta = tm.group(1)
            sizes = SIZE_RE.findall(line)
            if sizes:
                state.size = f"{sizes[-1][0]} {sizes[-1][1]}"

            self.total_percent = max(self.total_percent, pct)

    def demo_tick(self) -> None:
        elapsed = time.monotonic() - self.demo_start
        names = [
            "firefox-157.0.1-1-x86_64",
            "gtk4-4.24.1-1-x86_64",
            "openssl-3.5.8-1-x86_64",
            "glib2-2.86.1-1-x86_64",
            "linux-cachyos-6.17-1-x86_64",
            "cachyos-settings-1.0-1-x86_64",
            "mesa-26.2.1-1-x86_64",
            "gcc-15.2.1-1-x86_64",
        ]
        for i, name in enumerate(names):
            state = self.add_package(name)
            state.percent = int((elapsed * (7 + i * 1.4) + i * 16) % 101)
            state.speed = f"{250 + i * 83} KiB/s"
            state.eta = f"00:{max(1, 19 - int(state.percent / 6)):02d}"
            state.size = f"{0.4 + i * 0.37:.1f} MiB"
            state.last_update = time.monotonic()
        self.total_percent = int((elapsed * 5) % 101)
        self.total_hint = "Pakete werden empfangen ..."

    def visible(self, limit: int) -> List[PackageState]:
        return [self.packages[n] for n in self.order[-limit:]]

    def render_lane(self, state: PackageState) -> List[str]:
        assert state.maze is not None
        state.tick_ghosts()
        pac = state.pac_pos()
        ghosts = {p: i for i, p in enumerate(state.ghosts)}

        title = f"{CYAN}{state.name[:34]}{RESET}  {state.percent:3d}%"
        out = [title]

        for y, row in enumerate(state.maze.grid):
            line = []
            for x, cell in enumerate(row):
                pos = (y, x)
                if pos == pac:
                    glyph = PAC if int(time.monotonic() * 8) % 2 else PAC_ALT
                    line.append(f"{YELLOW}{glyph}{RESET}")
                elif pos in ghosts:
                    i = ghosts[pos]
                    line.append(f"{state.ghost_colors[i]}{GHOSTS[i]}{RESET}")
                elif pos in state.maze.power:
                    line.append(f"{WHITE}◆{RESET}")
                elif cell == ".":
                    line.append(f"{YELLOW}·{RESET}")
                else:
                    line.append(f"{BLUE}█{RESET}")
            out.append("".join(line))

        if state.percent >= 100:
            meta = f"{GREEN}{BOLD}★ LEVEL CLEAR ★{RESET}  {state.size or '--'}"
        else:
            meta = (
                f"{DIM}{state.size or '--':>8}  "
                f"{state.speed or '--':<11}  ETA {state.eta or '--:--'}{RESET}"
            )
        out.append(meta)
        return out

    @staticmethod
    def header(cols: int) -> List[str]:
        inner = max(52, min(cols - 2, 76))
        top = "╭" + "─" * (inner - 2) + "╮"
        middle = (
            "│ "
            + f"{YELLOW}◕{RESET} {BOLD}pacman Systemaktualisierung{RESET}"
            + " " * max(1, inner - 36)
            + f"{DIM}live PTY / Pac-Man mode{RESET} │"
        )
        bottom = "╰" + "─" * (inner - 2) + "╯"
        return [
            f"{CYAN}{BOLD}{top}{RESET}",
            f"{CYAN}{BOLD}{middle}{RESET}",
            f"{CYAN}{BOLD}{bottom}{RESET}",
        ]

    def render(self) -> str:
        cols, rows = shutil.get_terminal_size((80, 40))
        cols = max(60, cols)
        rows = max(20, rows)

        lines = [
            f"{CYAN}{BOLD}CACHYOS PACMA-SY{RESET}",
            *self.header(cols),
            "",
            f"{CYAN}◆{RESET} {BOLD}Systemstatus{RESET}  "
            f"{self.total_hint or 'Warte auf pacman ...'}",
            f"{GREEN}◆{RESET} Gesamtfortschritt {self.total_percent:3d}% "
            f"{self.progress_bar(self.total_percent, min(34, max(16, cols - 34)))}",
            "",
        ]

        lane_height = 15
        available = max(1, rows - len(lines) - 4)
        limit = max(1, min(3, available // lane_height))
        states = self.visible(limit)

        if states:
            for index, state in enumerate(states):
                lines.extend(self.render_lane(state))
                if index != len(states) - 1:
                    lines.append("")
        else:
            lines.extend([
                f"{DIM}Noch keine Downloaddaten.{RESET}",
                f"{YELLOW}◕{RESET}  · · · · · · · · · · · · · · · · ·  "
                f"{GRAY}Pac-Man wartet auf das erste Paket ...{RESET}",
            ])

        lines.extend([
            "",
            f"{BLUE}◆{RESET} {BOLD}Pakete:{RESET} {len(self.packages):02d}   "
            f"{CYAN}Pac-Man:{RESET} Downloadfortschritt = Level-Fortschritt",
            f"{DIM}Ctrl+C beendet die Ansicht; Eingaben gehen direkt an pacman.{RESET}",
        ])
        return "\n".join(lines)

    @staticmethod
    def progress_bar(pct: int, width: int) -> str:
        width = max(10, width)
        filled = int(width * pct / 100)
        if pct >= 100:
            return f"{GREEN}{'█' * width}{RESET}"
        return (
            f"{GREEN}{'█' * filled}{RESET}"
            f"{YELLOW}{PAC}{RESET}"
            f"{GRAY}{'·' * max(0, width - filled - 1)}{RESET}"
        )

    def screen(self) -> None:
        # Clear and redraw the complete frame. Do not slice strings containing
        # ANSI escapes: doing so was the cause of the broken terminal layout.
        sys.stdout.write("\033[2J\033[H\033[?25l")
        sys.stdout.write(self.render())
        sys.stdout.write("\033[0m")
        sys.stdout.flush()

    def run_demo(self) -> None:
        try:
            while True:
                self.demo_tick()
                self.screen()
                time.sleep(0.14)
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write("\033[0m\033[?25h\n")
            sys.stdout.flush()

    def run_live(self) -> int:
        if not shutil.which("pacman"):
            print("pacsy: pacman wurde nicht gefunden.", file=sys.stderr)
            return 127
        if not shutil.which("sudo"):
            print("pacsy: sudo wurde nicht gefunden.", file=sys.stderr)
            return 127
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("pacsy: Live-Modus benötigt ein interaktives TTY.", file=sys.stderr)
            return 2

        pid, fd = os.forkpty()
        if pid == 0:
            os.execvp("sudo", ["sudo", "pacman", "-Syu"])

        old = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())

        try:
            while True:
                r, _, _ = select.select([fd, sys.stdin], [], [], 0.08)

                if fd in r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    self.parse(data.decode("utf-8", errors="replace"))

                if sys.stdin in r:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if data:
                        os.write(fd, data)

                self.screen()

                try:
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        return os.waitstatus_to_exitcode(status)
                except ChildProcessError:
                    return 0

        except KeyboardInterrupt:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            return 130
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
            try:
                os.close(fd)
            except OSError:
                pass
            sys.stdout.write("\033[0m\033[?25h\n")
            sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CachyOS Pac-Man themed pacman wrapper"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="show animated demo without running pacman",
    )
    args = parser.parse_args()

    app = Pacsy(demo=args.demo)
    return app.run_demo() if args.demo else app.run_live()


if __name__ == "__main__":
    raise SystemExit(main())
