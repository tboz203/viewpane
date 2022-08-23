#!/usr/bin/env python

import argparse
import curses
import logging
import subprocess
import time
from pathlib import Path

from stransi import Ansi

root = Path(__file__).parent
logfile = f"{root}/curses_test.log"
logging.basicConfig(filename=logfile, format="[%(asctime)s %(levelname)-8s %(name)s] %(message)s", level=logging.DEBUG)
logger = logging.getLogger('curses_test')

RATE = 0.25

def win_main(stdscr: curses._CursesWindow):
    stdscr.nodelay(True)
    stdscr.clear()

    # proc = subprocess.run(["git", "la", "--color"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # text = proc.stdout.decode('utf-8')

    with open(root.joinpath('bigtext'), 'r', encoding='utf-8') as fin:
        text = fin.read()

    lines = text.splitlines()

    # curses.update_lines_cols()
    # for y, line in zip(range(curses.LINES), lines):
    #     stdscr.addstr(y, 0, line)
    #     stdscr.refresh()
    #     time.sleep(RATE)

    stdscr.addstr(0, 0, text[:3000])

    while True:
        # stdscr.clear()
        # ...
        # stdscr.refresh()

        c = stdscr.getch()
        if c == ord('q') or c == ord('Q'):
            break

        time.sleep(RATE)

def display_ansi_lines(screen: curses._CursesWindow, ansi_lines: list[Ansi]) -> None:
    """
    print a sequence of ansi lines to the curses window. lines are assumed to
    not have embedded newlines or to wrap across multiple lines. 
    """

    for y, line in enumerate(ansi_lines):
        pass

def main():
    logger.info("starting")
    try:
        curses.wrapper(win_main)
    except Exception as exc:
        logger.exception("caught fatal error: %s", exc)
    finally:
        logger.info("stopping")

if __name__ == '__main__':
    main()
