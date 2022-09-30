#!/usr/bin/env python

import curses
from collections import defaultdict
from pprint import pprint

def win_main(stdscr):

    pad = curses.newpad(100, 300)
    pad.keypad(True)
    curses.halfdelay(1)

    coords = 0, 0

    # the lines we put into the pad, as we create them
    lines = []

    # make a mapping from int value (from getch response) to variable name (from curses module)
    key_lookup = defaultdict(list)
    for name, value in vars(curses).items():
        if isinstance(value, int):
            key_lookup[value].append(name)

    pad.refresh(0, 0, 0, 0, curses.LINES - 1, curses.COLS - 1)

    running = True
    while running:
        for _ in range(50):
            try:
                # item = pad.getkey()
                item = pad.get_wch()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                return (exc, type(exc))

            line = f'{item!r}\n'
            lines.append(line)
            pad.addstr(line)

            y = max(0, len(lines) - curses.LINES)
            pad.refresh(y, 0, 0, 0, curses.LINES - 1, curses.COLS - 1)

            stritem = item
            if isinstance(item, int):
                stritem = chr(item)
            if stritem == 'q':
                running = False
                break

        max_y, max_x = pad.getmaxyx()
        pad.resize(max_y + 50, max_x)

    return lines

if __name__ == '__main__':
    lines = curses.wrapper(win_main)
    pprint(lines)
