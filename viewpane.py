#!/home/tbozeman/.local/bin/python3.10

from __future__ import annotations

import argparse
import curses
import logging
import subprocess
import time
import traceback
from pathlib import Path
from typing import Iterable, TYPE_CHECKING, Union

from stransi import Ansi
from stransi.attribute import SetAttribute, Attribute
from stransi.color import SetColor, ColorRole
from stransi.instruction import Instruction

if TYPE_CHECKING:
    CursesWindow = curses._CursesWindow
    # that thing what comes outta `Ansi.instructions()`
    InstructionStream = Iterable[Union[str, Instruction]]
    # a list of text/attr pairs
    OutputStream = Iterable[tuple[str, int]]
    # a dictionary mapping foreground/background pairs to curses color registry ids
    ColorMap = dict[tuple[int, int], int]

root = Path(__file__).parent
logfile = f"{root}/viewpane.log"
# logging.basicConfig(filename=logfile, format="[%(asctime)s %(levelname)-8s %(name)s] %(message)s", level=logging.DEBUG)
logging.basicConfig(handlers=[logging.NullHandler()])
logger = logging.getLogger("viewpane")

DEFAULT_DRAW_RATE = 2
DEFAULT_READ_RATE = 0.05


class StransiInstructionStreamTranslator:
    """
    A class for translating a stream of stransi instructions into a stream of
    text/attr pairs suitable for use in `stdscr.addstr(y, x, str, attr)`.

    This is a class instead of a function, because the stream needs to keep
    track of some state between invocations, including the current attr of the
    stream, and what color pairs known to curses.
    """

    def __init__(self, init_attr: int = 0, init_color_num: int = None, color_map: ColorMap = None):
        """
        initialize a new StransiInstructionStreamTranslator. you can specify a
        starting attr state and/or initial color map here.
        """

        # init_attr can include a color num, but our self._attr should not

        if init_color_num is not None:
            self._color_num: int = init_color_num
        else:
            # extract starting color number from our attr; may well be 0
            self._color_num = curses.pair_number(init_attr)

        # remove any prior color number from our attr
        self._attr = init_attr & ~(curses.A_COLOR)

        self._color_map = color_map or {}

    def translate_ansi_instruction_stream(self, stream: InstructionStream) -> OutputStream:
        """
        Translate a stransi instruction stream into a sequence of attr/text pairs
        (with the intention of being fed to `stdscr.addstr(y, x, text, attr)`).
        The stream is assumed to start with no attributes set.
        """
        # okay so, we scan through the instruction stream, keeping track of our FG
        # & BG colors, adding in other attributes as they appear, and resetting on
        # A_NORMAL. yeah?

        attr = self._attr
        color_num = self._color_num
        fg, bg = curses.pair_content(color_num)

        for item in stream:
            if isinstance(item, str):
                # we've got a string; time to emit!
                # combine attr & color right as they go out
                yield (item, attr | curses.color_pair(color_num))
            elif isinstance(item, SetColor):
                if item.role == ColorRole.FOREGROUND:
                    fg = item.color.code
                elif item.role == ColorRole.BACKGROUND:
                    bg = item.color.code
                else:
                    logger.warning("unrecognized setcolor instruction: %s", item)
                    continue

                if (fg, bg) in self._color_map:
                    color_num = self._color_map[(fg, bg)]
                else:
                    # get the next available color number
                    color_num = (max(self._color_map.values()) + 1) if self._color_map else 1
                    # tell curses about it
                    curses.init_pair(color_num, fg, bg)
                    # store the mapping
                    self._color_map[(fg, bg)] = color_num

            elif isinstance(item, SetAttribute):
                if item.attribute == Attribute.NORMAL:
                    # reset!
                    color_num = 0
                    attr = 0
                elif item.attribute == Attribute.BLINK:
                    attr |= curses.A_BLINK
                elif item.attribute == Attribute.BOLD:
                    attr |= curses.A_BOLD
                elif item.attribute == Attribute.DIM:
                    attr |= curses.A_DIM
                elif item.attribute == Attribute.HIDDEN:
                    attr |= curses.A_INVIS
                elif item.attribute == Attribute.ITALIC:
                    attr |= curses.A_ITALIC
                elif item.attribute == Attribute.REVERSE:
                    attr |= curses.A_REVERSE
                elif item.attribute == Attribute.UNDERLINE:
                    attr |= curses.A_UNDERLINE
                elif item.attribute == Attribute.NEITHER_BOLD_NOR_DIM:
                    attr &= ~(curses.A_BOLD | curses.A_DIM)
                elif item.attribute == Attribute.NOT_BLINK:
                    attr &= ~curses.A_BLINK
                elif item.attribute == Attribute.NOT_HIDDEN:
                    attr &= ~curses.A_INVIS
                elif item.attribute == Attribute.NOT_ITALIC:
                    attr &= ~curses.A_ITALIC
                elif item.attribute == Attribute.NOT_REVERSE:
                    attr &= ~curses.A_REVERSE
                elif item.attribute == Attribute.NOT_UNDERLINE:
                    attr &= ~curses.A_UNDERLINE

            else:
                logger.warning("unrecognized instruction: %s", item)
                continue

        # all done; save our pieces before leaving
        self._attr = attr
        self._color_num = color_num


class PadManager:
    """Write Ansi lines into a curses pad, and keep track of its location """
    def __init__(self, stdscr: CursesWindow, pad: CursesWindow, coords: tuple[int, int] = (0, 0), color_map: ColorMap = None):
        """
        Initialize a new PadWriter.

        :param pad: the pad to print into
        :param coords: the initial (y, x) coordinates of the top left corner of the pad
        :param color_map: initial ColorMap dictionary passed to StransiInstructionStreamTranslator
        """
        self._stdscr = stdscr
        self._pad = pad
        self._coords = coords
        self._color_map = color_map or {}
        self._translator = StransiInstructionStreamTranslator(color_map=self._color_map)

    def write(self, ansi_lines: list[Ansi]) -> None:
        """
        write a sequence of ansi lines to the pad, starting at 0, 0. clears any
        prior pad contents. lines are assumed to not have embedded newlines.
        """

        logger.info("writing %s lines into pad", len(ansi_lines))

        self._resize(ansi_lines)
        self._pad.clear()

        for y, line in enumerate(ansi_lines):
            # ansi translation magic
            instructions = line.instructions()
            # turn our instructions into text/attr pairs
            stream = iter(self._translator.translate_ansi_instruction_stream(instructions))
            # get the first pair and put it explicitly at (y, 0)
            text, attr = next(stream)
            self._pad.addstr(y, 0, text, attr)
            # the rest are positioned implicitly
            for text, attr in stream:
                self._pad.addstr(text, attr)

    def _resize(self, ansi_lines: list[Ansi]) -> None:
        """
        make our pad fit these lines
        """

        lines_y = len(ansi_lines)
        lines_x = max(map(ansi_length, ansi_lines))

        self._pad.resize(lines_y, lines_x + 1)

    def move_by(self, move_y: int, move_x: int):
        """shift the pad, but stay inside the borders"""
        y, x = self._coords
        pad_y, pad_x = self._pad.getmaxyx()
        # only move if the resulting coordinates are still inside the pad
        if (move_y > 0 and y + move_y < pad_y) or (move_y < 0 and y + move_y >= 0):
            y += move_y
        if (move_x > 0 and x + move_x < pad_x) or (move_x < 0 and x + move_x >= 0):
            x += move_x
        logger.info("moving pad coordinates to (%s, %s)", y, x)
        self._coords = (y, x)

    def refresh(self):
        """display the pad"""
        logger.info("drawing pad")
        y, x = self._coords
        # self._stdscr.clear()
        self._stdscr.erase()
        self._stdscr.noutrefresh()
        self._pad.noutrefresh(y, x, 0, 0, curses.LINES - 1, curses.COLS - 1)
        self._stdscr.move(curses.LINES-1, 0)
        self._stdscr.noutrefresh()
        curses.doupdate()


def win_main(stdscr: CursesWindow, command: list[str], draw_rate=None):

    draw_rate = draw_rate or DEFAULT_DRAW_RATE

    curses.use_default_colors()
    curses.halfdelay(1)

    pad = curses.newpad(24, 80)
    pad.keypad(True)
    manager = PadManager(stdscr, pad)

    # v_shift = 10
    # h_shift = 20
    v_shift = 1
    h_shift = 10

    running = True

    def draw():
        logger.info("calling draw!")
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = proc.stdout.decode("utf-8")
        lines = [Ansi(line) for line in text.splitlines()]

        manager.write(lines)
        manager.refresh()

    draw()

    mark = time.monotonic()
    while True:
        now = time.monotonic()
        if (now - mark) > draw_rate:
            mark = now
            draw()

        try:
            c = pad.getkey()
        except curses.error as exc:
            if exc.args == ('no input',):
                # logger.debug("no input")
                continue
            else:
                raise

        if c == 'q':
            # time to goodbye
            break
        elif c == 'KEY_UP':
            manager.move_by(-v_shift, 0)
            manager.refresh()
        elif c == 'KEY_DOWN':
            manager.move_by(v_shift, 0)
            manager.refresh()
        elif c == 'KEY_LEFT':
            manager.move_by(0, -h_shift)
            manager.refresh()
        elif c == 'KEY_RIGHT':
            manager.move_by(0, h_shift)
            manager.refresh()
        elif c == 'KEY_RESIZE':
            logger.info("refreshing for resize")
            curses.update_lines_cols()
            manager.refresh()



def ansi_length(ansi: Ansi):
    """get the printing length of an Ansi line"""
    return sum(len(item) for item in ansi.instructions() if isinstance(item, str))


def main():
    logger.info("starting")

    parser = argparse.ArgumentParser(description="A quick little program somewhere between `watch` and `less`")
    parser.add_argument("-d", "--delay", type=float, help="How frequently to run the watched command")
    parser.add_argument("command", nargs="+", help="the command to watch")

    args = parser.parse_args()

    try:
        curses.wrapper(win_main, args.command, draw_rate=args.delay)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("caught fatal error: %s", exc)
        traceback.print_exc()
    finally:
        logger.info("stopping")


if __name__ == "__main__":
    main()
