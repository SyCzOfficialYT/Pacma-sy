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
        names=[
            ("extra","firefox","157.0.1-1","157.0.1-1"),("cachyos","opencode","0.1.2-1","0.2.0-1"),
            ("core","gtk4","4.24.5-1","4.24.6-1"),("extra","gtk3","3.24.50-1","3.24.51-1"),
            ("extra","glib2","2.86.1-1","2.86.2-1"),("cachyos","libqalculate","5.13.0-1","5.13.1-1"),
            ("extra","mesa","26.2.2-1","26.2.3-1"),("multilib","lib32-expat","2.8.5-1","2.9.0-1"),
            ("extra","openssl","10.5p1-1","10.6p1-1"),("extra","gst-plugins-base","1.28.7-2","1.28.7-3")]
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

    def lane(self,lane:RepoLane,width:int)->str:
        colors={"core":BLUE,"extra":YELLOW,"multilib":RED,"cachyos":CYAN}
        c=colors.get(lane.repo,CYAN)
        name=(lane.current or "Warte ...")[:18]; pct=lane.percent
        barw=max(12,min(25,width-34)); filled=int(barw*pct/100)
        dots="·"*filled+" "*max(0,barw-filled)
        return f"{c}{lane.repo:<9}{RESET} {name:<18} {lane.speed or '--':>10} {lane.eta or '--:--':>5} {c}┌{dots}┐{RESET} {pct:3d}%"

    def package_table(self,rows:int,width:int)->List[str]:
        title=f"{YELLOW}{PAC_FRAMES[int(time.monotonic()*5)%2]}{RESET}  {CYAN}{BOLD}Pakete ({len(self.packages)}){RESET}"
        selected=[self.packages[k] for k in self.order[-min(rows,8):]]
        if width<100:
            out=[title]
            for p in selected:
                status="↑" if p.old!=p.new else "="
                out.append(f"{status} {p.repo}/{p.name[:24]:<24} {p.old[:11]:>11} → {p.new[:11]:<11} {p.size or '--':>9}")
            return out
        out=[title,f"{DIM}{'Paket':<34} {'Alte Version':<16} {'Neue Version':<16} {'Netto':>10} {'Download':>12}{RESET}"]
        for p in selected:
            net="0,00 MiB" if p.old==p.new else (p.size or "--")
            out.append(f"{WHITE}{p.repo+'/'+p.name:<34}{RESET} {p.old:<16} {GREEN}{'↑ '+p.new:<16}{RESET} {YELLOW}{net:>10}{RESET} {p.size or '--':>12}")
        return out

    def one_line_game(self,p:Package,width:int)->str:
        length=max(12,min(34,width)); filled=int(length*p.percent/100)
        if p.percent>=100:
            return f"{GREEN}┌"+"─"*length+f"┐{RESET} {GREEN}★ 100%{RESET}"
        ghost_pos=max(0,min(length-1,int(((p.percent+32)%100)/100*length)))
        chars=[]
        for i in range(length):
            if i==filled: chars.append(f"{YELLOW}{PAC_FRAMES[int(time.monotonic()*8)%2]}{RESET}")
            elif i==ghost_pos: chars.append(f"{RED}A{RESET}")
            elif i<filled: chars.append(f"{YELLOW}·{RESET}")
            else: chars.append(f"{GRAY}·{RESET}")
        return f"{CYAN}┌"+"".join(chars)+f"┐{RESET}"

    def master_bar(self,width:int)->str:
        if self.total_percent>=100: return f"{GREEN}"+"━"*width+f"{RESET} {GREEN}★{RESET}"
        n=int(width*self.total_percent/100)
        return f"{GREEN}"+"━"*n+f"{YELLOW}{PAC_FRAMES[int(time.monotonic()*8)%2]}{RESET}{GRAY}"+"─"*max(0,width-n-1)+f"{RESET}"

    def _box(self, title:str, body:List[str], width:int, accent:str=CYAN)->List[str]:
        width=max(24,width)
        clean_title=title[:width-6]
        top=f"╭─ {clean_title} "+"─"*max(1,width-len(clean_title)-4)+"╮"
        bottom="╰"+"─"*(width-2)+"╯"
        out=[f"{accent}{top[:width]}{RESET}"]
        for raw in body:
            # ANSI-aware enough for this renderer: content itself is kept short
            # so Android/Termux never receives an overlong physical line.
            out.append(raw[:width])
        out.append(f"{accent}{bottom}{RESET}")
        return out

    def render(self)->str:
        cols,rows=shutil.get_terminal_size((80,40))
        # Never force a 70-column minimum. The physical terminal width is the
        # source of truth; overlong ANSI lines were causing the broken wrapping
        # visible on Android/Termux.
        cols=max(48,cols)
        compact=cols<96
        inner=cols-2
        lines=[]

        header_title="CACHYOS PACMA-SY"
        header_sub="pacman Systemaktualisierung"
        mode="live PTY / Pac-Man"
        if compact:
            lines.append(f"{CYAN}╭─◈ {header_title} "+"─"*max(1,inner-5-len(header_title))+"╮{RESET}")
            lines.append(f"{CYAN}│{RESET} {YELLOW}{PAC_FRAMES[int(time.monotonic()*8)%2]}{RESET} {BOLD}{header_sub}{RESET}")
            lines.append(f"{CYAN}╰"+"─"*(inner-2)+"╯{RESET}")
        else:
            lines.append(f"{CYAN}{BOLD}╭─◈ {header_title} "+"─"*max(1,inner-6-len(header_title))+"╮{RESET}")
            lines.append(f"{CYAN}│{RESET} {YELLOW}{PAC_FRAMES[int(time.monotonic()*8)%2]}{RESET} {BOLD}{header_sub}{RESET} {DIM}{mode}{RESET}")
            lines.append(f"{CYAN}╰"+"─"*(inner-2)+"╯{RESET}")

        lines.append("")
        status=self.status[:max(18,cols-20)]
        lines.append(f"{CYAN}◆{RESET} {BOLD}Systemstatus{RESET}  {status}")
        lines.append(f"{GREEN}◆{RESET} Gesamtfortschritt {self.total_percent:3d}% {self.master_bar(min(32,max(10,cols-38)))}")
        lines.append("")

        repo_body=[]
        for repo in REPOS:
            lane=self.lanes[repo]
            if compact:
                c={"core":BLUE,"extra":YELLOW,"multilib":RED,"cachyos":CYAN}.get(repo,CYAN)
                barw=max(10,min(24,cols-45))
                filled=int(barw*lane.percent/100)
                bar="·"*filled+"·"*max(0,barw-filled)
                repo_body.append(
                    f"{c}{repo:<8}{RESET} {lane.current[:14]:<14} "
                    f"{lane.percent:3d}% {c}│{bar}│{RESET}"
                )
            else:
                repo_body.append("  "+self.lane(lane,cols))
        lines += self._box("Repository / Download-Fortschritt",repo_body,cols)

        lines.append("")
        table_width=cols
        if compact:
            table_body=self.package_table(min(5,max(2,rows//12)),cols)
        else:
            table_body=self.package_table(8,cols)
        # package_table itself switches to compact columns below 100 columns.
        lines += self._box(f"{PAC_FRAMES[int(time.monotonic()*5)%2]}  Pakete ({len(self.packages)})",table_body,table_width)

        lines.append("")
        game_body=[]
        visible=[self.packages[k] for k in self.order[-(6 if not compact else 4):]]
        if not visible:
            game_body=[f"{DIM}Pac-Man wartet auf das erste Paket ...{RESET}"]
        elif compact:
            barw=max(14,min(28,cols-43))
            for p in visible:
                stats=f"{p.percent:3d}%"
                game_body.append(
                    f"{CYAN}{p.name[:12]:<12}{RESET} {stats} "
                    f"{self.one_line_game(p,barw)}"
                )
        else:
            barw=max(20,cols-48)
            for p in visible:
                stats=f"{p.speed or '--':>10} {p.eta or '--:--':>5} {p.percent:3d}%"
                game_body.append(
                    f"{CYAN}{p.name[:26]:<26}{RESET} {stats}  {self.one_line_game(p,barw)}"
                )
        lines += self._box(
            f"{PAC_FRAMES[int(time.monotonic()*5)%2]}  {self.prompt or 'Downloadfortschritt'}",
            game_body, cols
        )

        lines.append("")
        total=f"◆ Gesamt ({len(self.packages)}/{self.total_count or len(self.packages)})"
        if compact:
            lines.append(
                f"{CYAN}{total}{RESET}  DL {self.total_size or '--'}  "
                f"INST {self.total_installed or '--'}  {self.total_percent:3d}%"
            )
        else:
            lines.append(
                f"{CYAN}{total}{RESET}  Download {self.total_size or '--'}  "
                f"Installiert {self.total_installed or '--'}  Netto {self.total_net or '--'}  "
                f"{self.total_percent:3d}%"
            )
        lines.append(f"{DIM}Ctrl+C beendet die Ansicht; Eingaben gehen direkt an pacman.{RESET}")
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
        pid,fd=os.forkpty()
        if pid==0: os.execvp("sudo",["sudo","pacman","-Syu"])
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
