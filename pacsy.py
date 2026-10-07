#!/usr/bin/env python3
"""Pacma-sy - CachyOS pacman UI with a Pac-Man themed dashboard.

The real pacman transaction runs in a PTY. Pacma-sy only renders a dashboard
around it; keyboard input is forwarded unchanged to pacman.
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

RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
CYAN="\033[96m"; BLUE="\033[94m"; GREEN="\033[92m"; YELLOW="\033[93m"
RED="\033[91m"; MAGENTA="\033[95m"; WHITE="\033[97m"; GRAY="\033[90m"

ANSI_RE=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PCT_RE=re.compile(r"(?<!\d)(\d{1,3})%")
SPEED_RE=re.compile(r"(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB)/s")
TIME_RE=re.compile(r"(?<!\d)(\d{2}:\d{2})(?!\d)")
SIZE_LABEL_RE=re.compile(r"^(Total Download Size|Total Installed Size|Net Upgrade Size):\s*([0-9.,]+\s*(?:KiB|MiB|GiB))",re.I)
PKG_LINE_RE=re.compile(r"^\s*(?P<repo>[A-Za-z0-9_.+-]+)/(?P<name>[^\s]+)\s+(?P<old>\S+)\s+->\s+(?P<new>\S+)")
DOWNLOAD_RE=re.compile(r"(?P<name>[A-Za-z0-9@._+:-]+(?:-[0-9][A-Za-z0-9._:+~-]*)?-x86_64(?:\.pkg\.tar\.[a-z0-9]+)?)")

Point=Tuple[int,int]
PAC_FRAMES=("◕","◔")
REPOS=("core","extra","multilib","cachyos")

def seed_for(text:str)->int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8],"big")

def strip_ansi(text:str)->str:
    return ANSI_RE.sub("",text)

class Maze:
    def __init__(self,seed:int,width:int=35,height:int=7)->None:
        self.width=width if width%2 else width-1
        self.height=height if height%2 else height-1
        self.rng=random.Random(seed)
        self.grid=[["#"]*self.width for _ in range(self.height)]
        self._generate()
        self.start=(1,1); self.goal=(self.height-2,self.width-2)
        self.path=self._path(self.start,self.goal)
        self.power={(1,self.width-2),(self.height-2,1),self.goal}

    def _generate(self)->None:
        stack=[(1,1)]; self.grid[1][1]="."
        while stack:
            y,x=stack[-1]; options=[]
            for dy,dx in ((0,2),(2,0),(0,-2),(-2,0)):
                ny,nx=y+dy,x+dx
                if 1<=ny<self.height-1 and 1<=nx<self.width-1 and self.grid[ny][nx]=="#":
                    options.append((ny,nx))
            if not options:
                stack.pop(); continue
            ny,nx=self.rng.choice(options)
            self.grid[(y+ny)//2][(x+nx)//2]="."
            self.grid[ny][nx]="."
            stack.append((ny,nx))
        for _ in range(max(4,self.width//4)):
            y=self.rng.randrange(1,self.height-1,2); x=self.rng.randrange(1,self.width-1,2)
            dy,dx=self.rng.choice(((0,1),(1,0),(0,-1),(-1,0)))
            if 0<=y+dy<self.height and 0<=x+dx<self.width:
                self.grid[y+dy][x+dx]="."

    def floor(self,p:Point)->bool:
        y,x=p
        return 0<=y<self.height and 0<=x<self.width and self.grid[y][x]!="#"

    def neighbours(self,p:Point)->List[Point]:
        y,x=p; out=[]
        for q in ((y,x+1),(y+1,x),(y,x-1),(y-1,x)):
            if self.floor(q): out.append(q)
        return out

    def _path(self,start:Point,goal:Point)->List[Point]:
        q=deque([start]); parent={start:None}
        while q:
            p=q.popleft()
            if p==goal: break
            for n in self.neighbours(p):
                if n not in parent:
                    parent[n]=p; q.append(n)
        if goal not in parent: return [start]
        path=[]; cur=goal
        while cur is not None:
            path.append(cur); cur=parent[cur]
        return list(reversed(path))

@dataclass
class Package:
    repo:str
    name:str
    old:str=""
    new:str=""
    percent:int=0
    speed:str=""
    eta:str=""
    size:str=""
    seed:int=0
    maze:Maze|None=None
    ghosts:List[Point]=field(default_factory=list)
    updated:float=0.0
    finished:bool=False

    def __post_init__(self)->None:
        self.seed=seed_for(f"{self.repo}/{self.name}")
        self.maze=Maze(self.seed)
        rng=random.Random(self.seed)
        choices=self.maze.path[max(1,len(self.maze.path)//5):-2] or self.maze.path
        self.ghosts=[rng.choice(choices) for _ in range(3)]

    def pac(self)->Point:
        path=self.maze.path if self.maze else [(1,1)]
        i=min(len(path)-1,int((self.percent/100)*max(0,len(path)-1)))
        return path[i]

    def tick(self)->None:
        if not self.maze: return
        target=self.pac()
        for i,pos in enumerate(self.ghosts):
            choices=self.maze.neighbours(pos)
            if not choices: continue
            rng=random.Random(self.seed+i*1009+int(time.monotonic()*6))
            if rng.random()<0.78:
                choices.sort(key=lambda p:abs(p[0]-target[0])+abs(p[1]-target[1]))
                self.ghosts[i]=rng.choice(choices[:min(2,len(choices))])
            else:
                self.ghosts[i]=rng.choice(choices)

@dataclass
class RepoLane:
    repo:str
    current:str=""
    percent:int=0
    speed:str=""
    eta:str=""
    packages:int=0

class Pacsy:
    def __init__(self,demo:bool=False)->None:
        self.demo=demo
        self.packages:Dict[str,Package]={}
        self.order:List[str]=[]
        self.lanes={r:RepoLane(r) for r in REPOS}
        self.total_percent=0
        self.total_count=0
        self.total_size=""
        self.total_installed=""
        self.total_net=""
        self.status="Warte auf pacman ..."
        self.prompt=""
        self.demo_start=time.monotonic()

    def add_pkg(self,repo:str,name:str,old:str="",new:str="")->Package:
        key=f"{repo}/{name}"
        if key not in self.packages:
            self.packages[key]=Package(repo,name,old,new)
            self.order.append(key)
            self.lanes.setdefault(repo,RepoLane(repo)).packages+=1
        p=self.packages[key]
        if old: p.old=old
        if new: p.new=new
        return p

    def parse(self,text:str)->None:
        clean=strip_ansi(text)
        for raw in re.split(r"[\r\n]+",clean):
            line=raw.strip()
            if not line: continue
            smeta=SIZE_LABEL_RE.match(line)
            if smeta:
                label,value=smeta.group(1).lower(),smeta.group(2)
                if "download" in label: self.total_size=value
                elif "installed" in label: self.total_installed=value
                elif "net" in label: self.total_net=value
            low=line.lower()
            if ":: retrieving packages" in low:
                self.status="Pakete werden empfangen ..."
            elif ":: processing package changes" in low:
                self.status="Paketänderungen werden verarbeitet ..."
            elif ":: resolving dependencies" in low:
                self.status="Abhängigkeiten werden aufgelöst ..."
            elif ":: looking for conflicting packages" in low:
                self.status="Nach in Konflikt stehenden Paketen wird gesucht ..."
            elif ":: starting full system upgrade" in low:
                self.status="Vollständige Systemaktualisierung wird gestartet ..."

            m=PKG_LINE_RE.match(line)
            if m:
                self.add_pkg(m.group("repo"),m.group("name"),m.group("old"),m.group("new"))
                self.total_count=len(self.packages)
                self.prompt="Installation fortsetzen? [J/n]"
                continue

            pct=PCT_RE.search(line)
            if not pct: continue
            value=max(0,min(100,int(pct.group(1))))
            before=line[:pct.start()].strip()
            dm=DOWNLOAD_RE.search(before)
            key=None
            if dm:
                filename=re.sub(r"\.pkg\.tar\.[a-z0-9]+$","",dm.group("name"))
                parts=filename.rsplit("-",2)
                name=parts[0] if len(parts)>=3 else filename
                key=next((k for k in reversed(self.order) if k.endswith("/"+name) or k.endswith("/"+filename)),None)
            if key is None and self.order:
                key=self.order[-1]
            if key is None: continue
            p=self.packages[key]
            p.percent=value; p.updated=time.monotonic(); p.finished=value>=100
            sm=SPEED_RE.search(line); tm=TIME_RE.search(line)
            if sm: p.speed=sm.group(0)
            if tm: p.eta=tm.group(1)
            lane=self.lanes.setdefault(p.repo,RepoLane(p.repo))
            lane.current=p.name; lane.percent=value; lane.speed=p.speed; lane.eta=p.eta
            self.total_percent=max(self.total_percent,value)

    def demo_tick(self)->None:
        names=[["extra","adwaita-cursors","1.0.0-1","1.1.0-1"],["extra","adwaita-icon-theme","1.1.0-1","1.2.0-1"],["extra","at-spi2-core","1.2.0-1","1.3.0-1"],["cachyos","ca-certificates-mozilla","1.3.0-1","1.4.0-1"],["cachyos","dconf","1.4.0-1","1.5.0-1"],["cachyos","expat","1.5.0-1","1.6.0-1"],["cachyos","faad2","1.6.0-1","1.7.0-1"],["cachyos","firefox","1.7.0-1","1.8.0-1"],["extra","firefox-i18n-de","1.8.0-1","1.9.0-1"],["extra","gdk-pixbuf2","1.9.0-1","1.10.0-1"],["cachyos","glib-networking","1.10.0-1","1.11.0-1"],["extra","glibmm-2.68","1.11.0-1","1.12.0-1"],["extra","glycin","1.12.0-1","1.13.0-1"],["cachyos","groff","1.13.0-1","1.14.0-1"],["extra","gsettings-desktop-schemas","1.14.0-1","1.15.0-1"],["extra","gsettings-system-schemas","1.15.0-1","1.16.0-1"],["extra","gst-libav","1.16.0-1","1.17.0-1"],["extra","gst-plugins-base","1.17.0-1","1.18.0-1"],["extra","gst-plugins-base-libs","1.18.0-1","1.19.0-1"],["extra","gst-plugins-bad","1.19.0-1","1.20.0-1"],["extra","gst-plugins-bad-libs","1.20.0-1","1.21.0-1"],["extra","gst-plugins-good","1.21.0-1","1.22.0-1"],["extra","gtk4","1.22.0-1","1.23.0-1"],["cachyos","gtk3","1.23.0-1","1.24.0-1"],["extra","gtkmm-4.0","1.24.0-1","1.25.0-1"],["cachyos","harfbuzz","1.25.0-1","1.26.0-1"],["core","hwdata","1.26.0-1","1.27.0-1"],["multilib","lib32-expat","1.27.0-1","1.28.0-1"],["extra","libcups","1.28.0-1","1.29.0-1"],["cachyos","libqalculate","1.29.0-1","1.30.0-1"],["cachyos","libqrsvg","1.30.0-1","1.31.0-1"],["extra","libpsl","1.31.0-1","1.32.0-1"],["cachyos","libxv","1.32.0-1","1.33.0-1"],["cachyos","nss","1.33.0-1","1.34.0-1"],["cachyos","opencode","1.34.0-1","1.35.0-1"],["cachyos","openssl","1.35.0-1","1.36.0-1"],["extra","python-gobject","1.36.0-1","1.37.0-1"],["extra","tinyspng","1.37.0-1","1.38.0-1"],["extra","xorgproto","1.38.0-1","1.39.0-1"],["extra","mesa","1.39.0-1","1.40.0-1"],["extra","libreoffice-fresh","1.40.0-1","1.41.0-1"],["cachyos","linux-cachyos","1.41.0-1","1.42.0-1"],["cachyos","systemd","1.42.0-1","1.43.0-1"],["core","pacman","1.43.0-1","1.44.0-1"]]
        t=time.monotonic()-self.demo_start
        for i,(repo,name,old,new) in enumerate(names):
            p=self.add_pkg(repo,name,old,new)
            p.percent=int((t*(5.5+i*.65)+i*11)%101)
            p.speed=f"{275+i*71} KiB/s"; p.eta=f"00:{max(1,12-int(p.percent/9)):02d}"
            p.size=f"{0.2+i*0.19:.2f} MiB"; p.updated=time.monotonic(); p.finished=p.percent>=100
            l=self.lanes[repo]; l.current=p.name; l.percent=max(l.percent,p.percent); l.speed=p.speed; l.eta=p.eta
        self.total_percent=int((t*4.7)%101)
        self.total_count=43; self.total_size="214,85 MiB"; self.total_installed="798,73 MiB"; self.total_net="-1,08 MiB"
        self.status="Pakete werden empfangen ..."; self.prompt="Installation fortsetzen? [J/n]"

    def lane_game(self, seed:int, percent:int, width:int, palette:tuple[str,str,str])->str:
        """Reference-style Pac-Man track: walls, pellets, Pac-Man, ghosts and fruit."""
        width=max(18,width)
        pct=max(0,min(100,percent))
        now=time.monotonic()
        pac=min(width-1,int(pct*(width-1)/100))
        g1=int((now*4.8+seed%width)%width)
        g2=int((now*3.4+(seed//7)%width)%width)
        fruit=int((width*.78+now*1.1+seed%9)%width)
        a,b,ghost=palette

        cells=[]
        for i in range(width):
            if i==pac:
                cells.append(f"{YELLOW}{PAC_FRAMES[int(now*9)%2]}{RESET}")
            elif i==g1:
                cells.append(f"{ghost}●{RESET}")
            elif i==g2:
                cells.append(f"{MAGENTA}●{RESET}")
            elif i==fruit:
                cells.append(f"{RED}◆{RESET}")
            elif i < pac:
                cells.append(f"{a}·{RESET}")
            elif i in (2,width-3):
                cells.append(f"{WHITE}●{RESET}")
            else:
                cells.append(f"{b}·{RESET}")

        top=f"{a}╭{'─'*width}╮{RESET}"
        mid=f"{a}│{RESET}{''.join(cells)}{a}│{RESET}"
        bottom=f"{a}╰{'─'*width}╯{RESET}"
        return [top,mid,bottom]

    def one_line_game(self,p:Package,width:int,palette:tuple[str,str,str])->str:
        return self.lane_game(p.seed,p.percent,width,palette)[1]

    @staticmethod
    def _vlen(text:str)->int:
        return len(strip_ansi(text))

    @classmethod
    def _fit(cls,text:str,width:int)->str:
        if cls._vlen(text)<=width:
            return text
        return strip_ansi(text)[:max(1,width-1)]+"…"

    def _panel(self,title:str,body:List[str],width:int,accent:str=CYAN)->List[str]:
        width=max(40,width)
        inner=width-2
        title_clean=strip_ansi(title)[:max(8,inner-8)]
        top=f"╭─ {title_clean} "+"─"*max(1,width-len(title_clean)-4)+"╮"
        out=[f"{accent}{top}{RESET}"]
        out.extend(self._fit(x,inner) for x in body)
        out.append(f"{accent}╰{'─'*(width-2)}╯{RESET}")
        return out

    def package_table(self,rows:int,width:int)->List[str]:
        pkgs=[self.packages[k] for k in self.order[-rows:]]
        if not pkgs:
            return [f"{DIM}Noch keine Pakete von pacman empfangen.{RESET}"]

        if width<100:
            out=[
                f"{BOLD}{CYAN}Paket{RESET}                 {BOLD}ALT → NEU{RESET}        {BOLD}DL{RESET}"
            ]
            for p in pkgs:
                change=f"{p.old[:9]}→{p.new[:9]}"
                out.append(
                    f"{p.repo[:7]:<7}/{p.name[:17]:<17} "
                    f"{change:<19} {p.size or '--':>9}"
                )
            return out

        out=[
            f"{DIM}{'Paket':<32} {'Alte Version':<15} {'Neue Version':<15} {'Netto':>10} {'Download':>12}{RESET}"
        ]
        for p in pkgs:
            net="0,00 MiB" if p.old==p.new else (p.size or "--")
            out.append(
                f"{p.repo+'/'+p.name:<32} {p.old:<15} "
                f"{GREEN}{p.new:<15}{RESET} {YELLOW}{net:>10}{RESET} {p.size or '--':>12}"
            )
        return out

    def render(self)->str:
        cols, rows = shutil.get_terminal_size((80,40))
        compact = cols < 105
        width = max(40, cols - 4 if compact else cols - 2)
        inner = width - 2
        now = time.monotonic()
        lines = []

        # Mobile/Termux: stack the reference layout. Nothing is allowed to
        # become a horizontal multi-line row.
        if compact:
            mode = "DEMO" if self.demo else "LIVE"
            lines.append(CYAN + "╭─ " + BOLD + "CACHYOS PACMA-SY" + RESET + "  " +
                         DIM + mode + " • pacman -Syu" + RESET +
                         " " + "─" * max(1, width-29) + "╮" + RESET)
            lines.append(CYAN + "│" + RESET + " " + YELLOW +
                         PAC_FRAMES[int(now*9)%2] + RESET + " " +
                         BOLD + "pacman Systemaktualisierung" + RESET)
            lines.append(CYAN + "│" + RESET + " " + DIM +
                         self.status[:max(1, inner-2)] + RESET)
            lines.append(CYAN + "╰" + "─"*(width-2) + "╯" + RESET)
            lines.append("")

            barw = max(18, min(30, width-34))
            filled = int(barw * self.total_percent / 100)
            bar = GREEN + "━"*filled + RESET + YELLOW + PAC_FRAMES[int(now*9)%2] + RESET
            bar += GRAY + "─"*max(0, barw-filled-1) + RESET
            lines.append(CYAN + "◆" + RESET + " " + BOLD +
                         "Gesamtfortschritt" + RESET + " " +
                         "%3d%% " % self.total_percent + bar)
            lines.append("")

            # Four repository lanes, each with its own Pac-Man game.
            lines.append(CYAN + "╭─ " + BOLD +
                         "Repository / Download-Fortschritt" + RESET +
                         " " + "─"*max(1,width-37) + "╮" + RESET)
            colors = {"core":BLUE, "extra":YELLOW, "multilib":RED, "cachyos":CYAN}
            for repo in REPOS:
                lane = self.lanes[repo]
                rc = colors[repo]
                name = (lane.current or "Warte ...")[:18]
                stat = "%9s %5s %3d%%" % (
                    lane.speed or "--", lane.eta or "--:--", lane.percent)
                lines.append(rc + "│" + RESET + " " + rc +
                             ("% -8s" % repo) + RESET + " " +
                             ("% -18s" % name) + " " + stat)
                game = self.lane_game(seed_for(repo), lane.percent,
                                      max(20,width-8), (rc,rc,rc))[1]
                lines.append(rc + "│" + RESET + "  " + game)
            lines.append(CYAN + "╰" + "─"*(width-2) + "╯" + RESET)
            lines.append("")

            # Compact package table.
            count = self.total_count or len(self.packages)
            lines.append(YELLOW + "╭─ " + BOLD + "◕ Pakete (%d)" % count +
                         RESET + " " + "─"*max(1,width-17) + "╮" + RESET)
            visible = [self.packages[k] for k in self.order[-7:]]
            if not visible:
                lines.append(YELLOW + "│" + RESET + " " +
                             DIM + "Warte auf Paketdaten von pacman ..." + RESET)
            else:
                lines.append(YELLOW + "│" + RESET + " " +
                             DIM + "Paket                     ALT → NEU       DL" + RESET)
                for p in visible:
                    label = (p.repo + "/" + p.name)[:25]
                    change = (p.old[:7] + "→" + p.new[:7])[:17]
                    lines.append(YELLOW + "│" + RESET + " " +
                                 "%-25s %-17s %9s" %
                                 (label, change, p.size or "--"))
            lines.append(YELLOW + "╰" + "─"*(width-2) + "╯" + RESET)
            lines.append("")

            # Download lanes: one package = one separate Pac-Man game.
            lines.append(CYAN + "╭─ " + BOLD +
                         "◕ Downloadfortschritt" + RESET + " " +
                         "─"*max(1,width-26) + "╮" + RESET)
            if not visible:
                lines.append(CYAN + "│" + RESET + " " +
                             DIM + "Pac-Man wartet auf das erste Paket ..." + RESET)
            else:
                for p in visible[-5:]:
                    lines.append(CYAN + "│" + RESET + " " +
                                 "%-18s %3d%% %9s %5s" %
                                 (p.name[:18], p.percent,
                                  p.speed or "--", p.eta or "--:--"))
                    palette = ((YELLOW,GRAY,YELLOW) if p.seed%4 == 1 else
                               (RED,GRAY,RED) if p.seed%4 == 2 else
                               (MAGENTA,GRAY,MAGENTA) if p.seed%4 == 3 else
                               (CYAN,GRAY,CYAN))
                    game = self.one_line_game(p, max(20,width-8), palette)
                    lines.append(CYAN + "│" + RESET + "  " + game)
            lines.append(CYAN + "╰" + "─"*(width-2) + "╯" + RESET)
            lines.append("")
            lines.append(CYAN + "◆" + RESET + " " + BOLD + "Gesamt" + RESET +
                         " (%d/%d)  DL %s  INST %s  Netto %s  %3d%%" %
                         (len(self.packages), count, self.total_size or "--",
                          self.total_installed or "--", self.total_net or "--",
                          self.total_percent))
            if self.last_pacman:
                lines.append(DIM + "pacman: " + self.last_pacman[:max(1,width-8)] + RESET)
            return "\n".join(lines)

        # Desktop: keep the reference's dense composition.
        mode = "DEMO" if self.demo else "LIVE • pacman -Syu"
        lines.append(CYAN + "╭─ " + BOLD + "CACHYOS PACMA-SY" + RESET +
                     "  " + DIM + mode + RESET + " " +
                     "─"*max(1,inner-31) + "╮" + RESET)
        lines.append(CYAN + "│" + RESET + " " + YELLOW +
                     PAC_FRAMES[int(now*9)%2] + RESET + " " +
                     BOLD + "Systemaktualisierung" + RESET + "   " +
                     self.status[:max(10,inner-30)])
        lines.append(CYAN + "╰" + "─"*(width-2) + "╯" + RESET)
        lines.append("")

        barw = max(20,min(44,inner-27))
        filled = int(barw*self.total_percent/100)
        lines.append(CYAN + "◆" + RESET + " " + BOLD +
                     "Gesamtfortschritt" + RESET + " %3d%% " % self.total_percent +
                     GREEN + "━"*filled + RESET + YELLOW +
                     PAC_FRAMES[int(now*9)%2] + RESET + GRAY +
                     "─"*max(0,barw-filled-1) + RESET)
        lines.append("")

        colors = {"core":BLUE,"extra":YELLOW,"multilib":RED,"cachyos":CYAN}
        repo_body = []
        for repo in REPOS:
            lane = self.lanes[repo]
            rc = colors[repo]
            game = self.lane_game(seed_for(repo),lane.percent,
                                  max(18,min(34,inner-43)),(rc,rc,rc))[1]
            repo_body.append(rc + repo.ljust(8) + RESET + " " +
                             (lane.current or "Warte ...")[:16].ljust(16) + " " +
                             (lane.speed or "--").rjust(9) + " " +
                             (lane.eta or "--:--").rjust(5) + " " +
                             ("%3d%% " % lane.percent) + game)
        lines += self._panel("Repository / Download-Fortschritt",repo_body,width)
        lines.append("")

        count = self.total_count or len(self.packages)
        lines += self._panel("◕  Pakete (%d)" % count,
                             self.package_table(9,width),width,YELLOW)
        lines.append("")

        visible = [self.packages[k] for k in self.order[-8:]]
        body = []
        for p in visible:
            palette = ((YELLOW,GRAY,YELLOW) if p.seed%4 == 1 else
                       (RED,GRAY,RED) if p.seed%4 == 2 else
                       (MAGENTA,GRAY,MAGENTA) if p.seed%4 == 3 else
                       (CYAN,GRAY,CYAN))
            body.append(p.name[:24].ljust(24) + " " +
                        (p.speed or "--").rjust(9) + " " +
                        (p.eta or "--:--").rjust(5) + " " +
                        ("%3d%% " % p.percent) +
                        self.one_line_game(p,max(24,width-46),palette))
        if not body:
            body = ["Pac-Man wartet auf das erste Paket ..."]
        lines += self._panel("◕  " + (self.prompt or "Downloadfortschritt"),
                             body,width)
        lines.append("")
        lines.append(CYAN + "◆" + RESET + " " + BOLD + "Gesamt" + RESET +
                     " (%d/%d)  Download %s  Installiert %s  Netto %s  %3d%%" %
                     (len(self.packages),count,self.total_size or "--",
                      self.total_installed or "--",self.total_net or "--",
                      self.total_percent))
        return "\n".join(lines)

    def screen(self)->None:
        sys.stdout.write("\033[2J\033[H\033[?25l"+self.render()+"\033[0m"); sys.stdout.flush()

    def run_demo(self)->int:
        try:
            while True:
                self.demo_tick(); self.screen(); time.sleep(.12)
        except KeyboardInterrupt:
            return 130
        finally:
            sys.stdout.write("\033[0m\033[?25h\n"); sys.stdout.flush()

    def run_live(self)->int:
        if not shutil.which("pacman") or not shutil.which("sudo"):
            print("pacsy: pacman und sudo werden benötigt.",file=sys.stderr); return 127
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("pacsy: Live-Modus benötigt ein interaktives TTY.",file=sys.stderr); return 2
        # Authenticate before entering the raw PTY dashboard. Otherwise the
        # renderer continuously clears sudo's password prompt and can make a
        # correct password look like it was rejected.
        print(f"{YELLOW}sudo authentication required...{RESET}")
        auth=os.system("sudo -v")
        if auth != 0:
            print("pacsy: sudo authentication failed.",file=sys.stderr)
            return 1

        pid,fd=os.forkpty()
        if pid==0: os.execvp("sudo",["sudo","-n","pacman","-Syu"])
        old=termios.tcgetattr(sys.stdin); tty.setraw(sys.stdin.fileno())
        try:
            while True:
                readable,_,_=select.select([fd,sys.stdin],[],[],.08)
                if fd in readable:
                    try: data=os.read(fd,65536)
                    except OSError: data=b""
                    if not data: break
                    self.parse(data.decode("utf-8",errors="replace"))
                if sys.stdin in readable:
                    data=os.read(sys.stdin.fileno(),4096)
                    if data: os.write(fd,data)
                self.screen()
                try:
                    waited,status=os.waitpid(pid,os.WNOHANG)
                    if waited==pid: return os.waitstatus_to_exitcode(status)
                except ChildProcessError: return 0
        except KeyboardInterrupt:
            try: os.kill(pid,signal.SIGINT)
            except ProcessLookupError: pass
            return 130
        finally:
            termios.tcsetattr(sys.stdin,termios.TCSADRAIN,old)
            try: os.close(fd)
            except OSError: pass
            sys.stdout.write("\033[0m\033[?25h\n"); sys.stdout.flush()

def main()->int:
    parser=argparse.ArgumentParser(description="CachyOS Pac-Man themed pacman wrapper")
    parser.add_argument("--demo",action="store_true"); args=parser.parse_args()
    app=Pacsy(args.demo); return app.run_demo() if args.demo else app.run_live()

if __name__=="__main__":
    raise SystemExit(main())
