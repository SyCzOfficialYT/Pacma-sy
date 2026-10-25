#!/usr/bin/env python3
"""Pacsy Shell — full-screen CachyOS terminal dashboard. Python stdlib only."""
from __future__ import annotations

import curses
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

CYAN = 51
TEAL = 43
YELLOW = 220
GREEN = 82
ORANGE = 208
WHITE = 255
MUTED = 244
BG = 16

PACSY = [
    "          ▄████▄",
    "       ▄██████████▄",
    "      ██████████████",
    "     ████████    ████",
    "     ███████      ▀▀",
    "      ████████",
    "       ▀████████",
    "          ▀████",
]
LOGO = ["██████╗  █████╗  ██████╗███████╗██╗   ██╗", "██╔══██╗██╔══██╗██╔════╝██╔════╝╚██╗ ██╔╝", "██████╔╝███████║██║     ███████╗ ╚████╔╝ ", "██╔═══╝ ██╔══██║██║     ╚════██║  ╚██╔╝  ", "██║     ██║  ██║╚██████╗███████║   ██║   ", "╚═╝     ╚═╝  ╚═╝ ╚═════╝╚══════╝   ╚═╝   "]

def command(*args: str) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True, timeout=1).strip()
    except Exception:
        return "unavailable"

def cpu_percent() -> str:
    try:
        a = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = list(map(int, a)); idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        time.sleep(.12)
        b = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals2 = list(map(int, b)); idle2 = vals2[3] + (vals2[4] if len(vals2) > 4 else 0)
        total2 = sum(vals2)
        return f"{100 * (1 - (idle2-idle) / max(1,total2-total)):.0f}%"
    except Exception:
        return "n/a"

def memory() -> tuple[str, str]:
    try:
        m = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1); m[k] = int(v.strip().split()[0])
        total = m["MemTotal"] / 1024 / 1024
        used = (m["MemTotal"] - m.get("MemAvailable", 0)) / 1024 / 1024
        return f"{used:.1f} GiB", f"{total:.1f} GiB"
    except Exception:
        return "n/a", "n/a"

def uptime() -> str:
    try:
        s = int(float(Path("/proc/uptime").read_text().split()[0]))
        return f"{s//3600}h {(s%3600)//60:02d}m"
    except Exception:
        return "n/a"

def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    for n, color in [(1,CYAN),(2,TEAL),(3,YELLOW),(4,GREEN),(5,ORANGE),(6,WHITE),(7,MUTED)]:
        curses.init_pair(n, color, -1)

def put(stdscr, y: int, x: int, text: str, pair: int = 6, attr: int = 0, maxw: int | None = None) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w: return
    text = str(text).replace("\t", "    ")
    limit = max(0, w - x - 1)
    if maxw is not None: limit = min(limit, maxw)
    try: stdscr.addnstr(y, x, text, limit, curses.color_pair(pair) | attr)
    except curses.error: pass

def panel(stdscr, y: int, x: int, h: int, w: int, title: str, pair: int = 1) -> None:
    if h < 2 or w < 4: return
    put(stdscr,y,x,"╭"+"─"*(w-2)+"╮",pair,maxw=w)
    put(stdscr,y,x+2," "+title+" ",pair,curses.A_BOLD,maxw=w-4)
    for yy in range(y+1,y+h-1): put(stdscr,yy,x,"│",pair); put(stdscr,yy,x+w-1,"│",pair)
    put(stdscr,y+h-1,x,"╰"+"─"*(w-2)+"╯",pair,maxw=w)

def draw(stdscr) -> None:
    curses.curs_set(0); stdscr.nodelay(True); init_colors()
    started = time.monotonic()
    while True:
        stdscr.erase()
        h,w = stdscr.getmaxyx()
        if h < 24 or w < 88:
            put(stdscr,0,0,"PACSY SHELL — bitte Terminal auf mindestens 88x24 vergrößern",3,curses.A_BOLD)
            put(stdscr,2,0,"Q = Beenden")
            stdscr.refresh()
            if stdscr.getch() in (ord("q"),ord("Q")): break
            time.sleep(.15); continue

        left = max(42, int(w*.57)); right = w-left-3
        top_h = max(10, int(h*.42))
        mid_y = top_h+1
        mid_h = max(8, int(h*.34))
        bottom_y = mid_y+mid_h+1
        bottom_h = h-bottom_y-1

        # Header
        put(stdscr,0,1,"◕",3,curses.A_BOLD); put(stdscr,0,3,"PACSY",1,curses.A_BOLD)
        put(stdscr,0,10,"C A C H Y O S   //   SHELL",2,curses.A_BOLD)
        put(stdscr,0,max(1,w-27),time.strftime("%a %d %b  %H:%M:%S"),1)
        put(stdscr,1,1,"● 1   ○ 2   ○ 3   ○ 4",2)
        put(stdscr,1,max(1,w-28),"fish · kitty · CachyOS",7)
        try: stdscr.hline(2,0,"─",w,curses.color_pair(1))
        except curses.error: pass

        # System panel
        panel(stdscr,3,1,top_h,left-1,"PACSY // SYSTEM OVERVIEW")
        logo_x=3; logo_y=5
        for i,line in enumerate(PACSY[:max(1,min(len(PACSY),top_h-5))]):
            put(stdscr,logo_y+i,logo_x,line,3,curses.A_BOLD,maxw=21)
        put(stdscr,logo_y+len(PACSY)+1,logo_x,"P A C S Y",1,curses.A_BOLD)
        info_x=26
        user= os.environ.get("USER","user")
        details=[
            (f"{user}@{socket.gethostname()}",1),
            ("OS      "+platform.system()+" / CachyOS",2),
            ("Kernel  "+platform.release(),6),
            ("Shell   "+os.environ.get("SHELL","fish").split("/")[-1],6),
            ("Uptime  "+uptime(),6),
            ("CPU     "+cpu_percent(),3),
        ]
        used,total=memory()
        details += [("RAM     "+used+" / "+total,6),("Packages "+command("bash","-lc","pacman -Qq | wc -l")+" (pacman)",6)]
        try:
            load=Path("/proc/loadavg").read_text().split()[:3]
            details.append(("Load    "+" / ".join(load),6))
        except Exception: pass
        for i,(txt,pair) in enumerate(details[:max(1,top_h-3)]):
            put(stdscr,4+i,info_x,txt,pair,curses.A_BOLD if i==0 else 0,maxw=left-info_x-3)
        palette="  ".join(["■","■","■","■","■","■"])
        put(stdscr,3+top_h-2,4,palette,1)

        # Monitor / network panels
        rx=left+1
        panel(stdscr,3,rx,top_h,right,"LIVE MONITOR")
        stats=[("CPU",cpu_percent(),1),("RAM",used+" / "+total,2),("UPTIME",uptime(),3),("HOST",socket.gethostname(),1)]
        for i,(label,value,pair) in enumerate(stats):
            yy=5+i*2
            if yy < 3+top_h-1:
                put(stdscr,yy,rx+2,label,7,curses.A_BOLD)
                put(stdscr,yy,rx+12,value,pair,curses.A_BOLD,maxw=right-15)
                barw=max(5,right-18)
                pct=0
                if label=="CPU":
                    try: pct=int(value.rstrip("%"))
                    except Exception: pct=0
                elif label=="RAM":
                    try: pct=int(float(used.split()[0])/float(total.split()[0])*100)
                    except Exception: pct=0
                put(stdscr,yy+1,rx+2,"━"*max(1,int(barw*pct/100))+"─"*max(0,barw-int(barw*pct/100)),pair,maxw=barw)

        # File/project panel
        fw=left-1
        panel(stdscr,mid_y,1,mid_h,fw,"HOME // PROJECTS")
        home=Path.home()
        entries=["Desktop/","Dokumente/","Downloads/","Bilder/","Videos/","Musik/","Projekte/"," .config/"," .local/","Games/","Scripts/"]
        try:
            dirs=sorted([p.name+"/" for p in home.iterdir() if p.is_dir()],key=str.lower)
            entries=(dirs or entries)[:max(1,mid_h-3)]
        except Exception: entries=entries[:max(1,mid_h-3)]
        for i,item in enumerate(entries[:mid_h-2]):
            pair=3 if item.strip("/").lower() in ("projekte","projects") else 1
            put(stdscr,mid_y+1+i,3,("▸ " if pair==3 else "▰ ")+item,pair,maxw=fw-4)
        col=fw//2+1
        projects=["ai-gateway/","OmainLoveBot/","NerdQAxe/","Fixedcoin/","Minecraft/","Pacma-sy/"]
        for i,item in enumerate(projects[:mid_h-2]):
            put(stdscr,mid_y+1+i,col,item,2,maxw=fw-col-2)

        # Activity / quick actions
        panel(stdscr,mid_y, left+1,mid_h,right,"PACSY // QUICK STATUS")
        quick=[("01","System updates","pacman -Syu"),("02","Terminal","kitty"),("03","Network",command("bash","-lc","ip -brief route | head -1")),("04","Session","Q to quit · R refresh")]
        for i,(n,label,value) in enumerate(quick[:mid_h-2]):
            put(stdscr,mid_y+1+i*2,rx+2,n,3,curses.A_BOLD)
            put(stdscr,mid_y+1+i*2,rx+6,label,1,curses.A_BOLD,maxw=right-9)
            if mid_y+2+i*2 < mid_y+mid_h-1: put(stdscr,mid_y+2+i*2,rx+6,value,7,maxw=right-9)

        # Prompt footer
        panel(stdscr,bottom_y,1,max(3,bottom_h),w-2,"PACSY TERMINAL")
        put(stdscr,bottom_y+1,3,f"{user}@{socket.gethostname()}  ~/Projekte  ❯",1,curses.A_BOLD)
        put(stdscr,bottom_y+2,3,"Pacsy Shell is a live system dashboard — press Q to exit.",7)
        put(stdscr,bottom_y+3,3,"Next: richer telemetry, configurable widgets and matching Kitty theme.",2)
        put(stdscr,bottom_y+1,max(3,w-22),f"SESSION {int(time.monotonic()-started):04d}s",3)
        stdscr.refresh()
        key=stdscr.getch()
        if key in (ord("q"),ord("Q")): break
        time.sleep(.5)

def main() -> None:
    try: curses.wrapper(draw)
    except KeyboardInterrupt: pass

if __name__=="__main__":
    main()
