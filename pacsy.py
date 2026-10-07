#!/usr/bin/env python3
"""Pacma-sy: Pac-Man styled live pacman UI for CachyOS/Arch.

Pure stdlib implementation. Live mode keeps pacman attached to a PTY so
interactive prompts and the real pacman transaction remain intact.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import signal
import select
import sys
import time
import tty
import termios
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
PKG_RE = re.compile(r"([A-Za-z0-9@._+:-]+(?:-[0-9][A-Za-z0-9._:+~-]*)?-x86_64(?:\.pkg\.tar\.[a-z0-9]+)?)")
SPEED_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB)/s")
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB)")
TIME_RE = re.compile(r"(\d{2}:\d{2})")

GHOSTS = ["👻", "👾", "🟥", "🟦"]
PAC = "◕"
PAC_ALT = "◔"

# Small maze template. Dots are converted into a deterministic shuffled path
# for each package, making every package display its own little game.
BASE_MAZE = [
    "###############################",
    "#........#.......#............#",
    "#.#####..#.#####.#..#####....#",
    "#.......#.........#.......#...#",
    "#.#####.#.#######.#.#####.#..#",
    "#.........#.....#.............#",
    "###############################",
]

@dataclass
class PackageState:
    name: str
    percent: int = 0
    speed: str = ""
    eta: str = ""
    size: str = ""
    seed: int = 0
    path: List[Tuple[int, int]] = field(default_factory=list)
    ghosts: List[Tuple[int, int]] = field(default_factory=list)
    last_update: float = 0.0

    def __post_init__(self) -> None:
        self.seed = hash(self.name) & 0xFFFFFFFF
        rng = random.Random(self.seed)
        dots = [
            (y, x)
            for y, row in enumerate(BASE_MAZE)
            for x, cell in enumerate(row)
            if cell == "."
        ]
        rng.shuffle(dots)
        # Build a long deterministic route through the dot cells.
        self.path = self._route(dots)
        rng.shuffle(dots)
        self.ghosts = [dots[i % len(dots)] for i in range(min(3, len(dots)))]

    @staticmethod
    def _route(points: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        if not points:
            return []
        # Prefer a path-like ordering rather than a random teleporting route.
        remaining = set(points)
        current = points[0]
        route = [current]
        remaining.remove(current)
        while remaining:
            y, x = current
            candidates = sorted(
                remaining,
                key=lambda p: abs(p[0] - y) + abs(p[1] - x),
            )
            # Occasionally choose the second/third nearest point to avoid
            # every generated maze looking exactly the same.
            pick = min(2, len(candidates) - 1)
            nxt = candidates[random.Random((x << 16) ^ y ^ len(remaining)).randint(0, pick)]
            route.append(nxt)
            remaining.remove(nxt)
            current = nxt
        return route

    def pac_pos(self) -> Tuple[int, int]:
        if not self.path:
            return (1, 1)
        idx = min(len(self.path) - 1, int((self.percent / 100) * (len(self.path) - 1)))
        return self.path[idx]

    def tick_ghosts(self) -> None:
        if not self.path:
            return
        rng = random.Random(self.seed + int(time.monotonic() * 3))
        positions = set(self.path)
        self.ghosts = [rng.choice(self.path) for _ in self.ghosts]


class Pacsy:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.packages: Dict[str, PackageState] = {}
        self.order: List[str] = []
        self.current_output = ""
        self.total_percent = 0
        self.total_hint = ""
        self.running = True
        self.started = time.monotonic()
        self.demo_start = self.started

    def add_package(self, name: str) -> PackageState:
        if name not in self.packages:
            self.packages[name] = PackageState(name)
            self.order.append(name)
        return self.packages[name]

    def parse(self, text: str) -> None:
        clean = ANSI_RE.sub("", text).replace("\r", "\n")
        self.current_output = (self.current_output + clean)[-10000:]

        # Match the last package-looking token before a progress percentage.
        for line in clean.splitlines():
            m = PROGRESS_RE.search(line)
            if not m:
                continue
            pct = max(0, min(100, int(m.group(1))))
            prefix = line[:m.start()].strip()
            candidates = PKG_RE.findall(prefix)
            if candidates:
                name = candidates[-1]
            else:
                # Pacman output commonly prints the package filename directly.
                tokens = prefix.split()
                name = tokens[0] if tokens else "package"
            state = self.add_package(name)
            state.percent = pct
            sm = SPEED_RE.search(line)
            state.speed = sm.group(0) if sm else state.speed
            tm = TIME_RE.search(line)
            state.eta = tm.group(1) if tm else state.eta
            sizes = SIZE_RE.findall(line)
            if sizes:
                state.size = " ".join(sizes[-1])
            state.last_update = time.monotonic()

        # Overall pacman progress if TotalDownload is enabled.
        percentages = PROGRESS_RE.findall(clean)
        if percentages:
            self.total_percent = int(percentages[-1])

        if ":: Retrieving packages" in clean:
            self.total_hint = "Pakete werden empfangen ..."
        elif ":: Processing package changes" in clean:
            self.total_hint = "Paketänderungen werden verarbeitet ..."
        elif "upgraded" in clean.lower():
            self.total_hint = "Systemaktualisierung läuft ..."
        elif "Synchronizing" in clean:
            self.total_hint = "Paketdatenbanken werden synchronisiert ..."

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
            s = self.add_package(name)
            s.percent = int((elapsed * (8 + i * 2) + i * 13) % 101)
            s.speed = f"{250 + i * 83} KiB/s"
            s.eta = f"00:{max(1, 19 - int(s.percent / 6)):02d}"
            s.size = f"{0.4 + i * 0.37:.1f} MiB"
        self.total_percent = int((elapsed * 5) % 101)
        self.total_hint = "Pakete werden empfangen ..."

    def visible(self, limit: int) -> List[PackageState]:
        names = self.order[-limit:]
        return [self.packages[n] for n in names]

    def render_lane(self, state: PackageState, width: int) -> List[str]:
        # Maze is compact enough for a phone/SSH terminal.
        maze = [list(row) for row in BASE_MAZE]
        pac_y, pac_x = state.pac_pos()
        ghost_positions = set(state.ghosts)

        # Keep a dynamic ghost movement independent of download progress.
        state.tick_ghosts()

        out: List[str] = []
        top = f"{CYAN}{state.name[:30]:30}{RESET} {state.percent:3d}%"
        out.append(top[:width])

        for y, row in enumerate(maze):
            line = ""
            for x, cell in enumerate(row):
                if (y, x) == (pac_y, pac_x):
                    glyph = f"{YELLOW}{PAC if int(time.monotonic()*8)%2 else PAC_ALT}{RESET}"
                elif (y, x) in ghost_positions:
                    idx = list(ghost_positions).index((y, x)) % len(GHOSTS)
                    glyph = [RED, BLUE, MAGENTA][idx] + GHOSTS[idx] + RESET
                elif cell == ".":
                    glyph = f"{YELLOW}·{RESET}"
                else:
                    glyph = f"{BLUE}█{RESET}"
                line += glyph
            out.append(line[:width])
        meta = f"{DIM}{state.size:>8}  {state.speed:<11}  ETA {state.eta or '--:--'}{RESET}"
        out.append(meta[:width])
        return out

    def render(self) -> str:
        cols, rows = shutil.get_terminal_size((120, 48))
        cols = max(80, cols)
        lanes = 3 if rows >= 42 else 2
        states = self.visible(lanes)

        lines = [
            f"{CYAN}{BOLD}╭─◈ CACHYOS PACMA-SY ───────────────────────────────────────────────────────────────╮{RESET}",
            f"{CYAN}{BOLD}│{RESET} {YELLOW}◕{RESET} {BOLD}pacman Systemaktualisierung{RESET}    {DIM}live PTY renderer / Pac-Man mode{RESET}",
            f"{CYAN}{BOLD}╰───────────────────────────────────────────────────────────────────────────────────╯{RESET}",
            "",
            f"{CYAN}◆{RESET} {BOLD}Systemstatus{RESET}  {self.total_hint or 'Warte auf pacman ...'}",
            f"{GREEN}◆{RESET} Gesamtfortschritt  {self.total_percent:3d}%  {self.progress_bar(self.total_percent, min(42, cols-32))}",
            "",
        ]

        if states:
            for s in states:
                lane = self.render_lane(s, cols)
                lines.extend(lane)
                lines.append("")
        else:
            lines.extend([
                f"{DIM}Noch keine Downloaddaten. Pac-Man wartet auf das erste Paket ...{RESET}",
                "",
                f"{YELLOW}◕{RESET}  • • • • • • • • • • • • • • • • • •  {RED}👻{RESET}",
                "",
            ])

        lines.extend([
            f"{BLUE}◆{RESET} {BOLD}Pakete{RESET}: {len(self.packages):02d}    "
            f"{CYAN}Pac-Man{RESET} sammelt Punkte proportional zum echten Downloadfortschritt.",
            f"{DIM}Ctrl+C beendet die Ansicht; Eingaben werden an pacman weitergereicht.{RESET}",
        ])

        return "\n".join(lines)

    @staticmethod
    def progress_bar(pct: int, width: int) -> str:
        width = max(10, width)
        filled = int(width * pct / 100)
        return f"{GREEN}" + "·" * filled + f"{YELLOW}◕{RESET}" + f"{GRAY}" + "·" * max(0, width-filled-1) + RESET

    def screen(self) -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(self.render())
        sys.stdout.flush()

    def run_demo(self) -> None:
        try:
            while True:
                self.demo_tick()
                self.screen()
                time.sleep(0.12)
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

        pid, fd = os.forkpty()
        if pid == 0:
            os.execvp("sudo", ["sudo", "pacman", "-Syu"])

        old = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
        os.write(fd, b"\x1b[?25l")

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
                    text = data.decode("utf-8", errors="replace")
                    self.parse(text)
                    self.screen()
                if sys.stdin in r:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if data:
                        os.write(fd, data)

                # Keep ghosts animated even when pacman is quiet.
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
    parser = argparse.ArgumentParser(description="CachyOS Pac-Man themed pacman wrapper")
    parser.add_argument("--demo", action="store_true", help="show animated demo without running pacman")
    args = parser.parse_args()

    app = Pacsy(demo=args.demo)
    if args.demo:
        app.run_demo()
        return 0
    return app.run_live()


if __name__ == "__main__":
    raise SystemExit(main())
