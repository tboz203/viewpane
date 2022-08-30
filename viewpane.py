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


DRAW_RATE = 2
READ_RATE = 0.05


class StransiInstructionStreamTranslator:
    """
    A class for translating a stream of stransi instructions into a stream of
    text/attr pairs suitable for use in `stdscr.addstr(y, x, str, attr)`.

    This is a class instead of a function, because the stream needs to keep
    track of some state between invocations, including the current attr of the
    stream, and what color pairs known to curses.
    """

    def __init__(self, init_attr: int = 0, init_color_num: int = None, color_map: ColorMap = {}):
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

        self._color_map = dict(color_map)

    def translate_ansi_instruction_stream(self, stream: InstructionStream) -> OutputStream:
        """
        Translate a stransi instruction stream into a sequence of attr/text pairs
        (with the intention of being fed to `stdstc.addstr(y, x, text, attr)`).
        Unless specified otherwise, the stream is assumed to start with a blank
        attribute.
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


class Printer:
    def __init__(self, screen: CursesWindow, color_map: ColorMap = None):
        self._screen = screen
        self._color_map = color_map or {}

    def print(self, ansi_lines: list[Ansi]) -> None:
        """
        print a sequence of ansi lines to the curses window, starting at 0, 0.
        lines are assumed to not have embedded newlines or to wrap across
        multiple lines.
        """

        translator = StransiInstructionStreamTranslator(color_map=self._color_map)

        height, width = self._screen.getmaxyx()
        line_nos = range(height - 1)

        # like `enumerate(ansi_lines)`, but stop at the edge of the screen
        for y, line in zip(line_nos, ansi_lines):
            # ansi translation magic
            instructions = line.instructions()
            # turn our instructions into text/attr pairs
            translated_stream = translator.translate_ansi_instruction_stream(instructions)
            # cut off the x-dimension excess
            trimmed_stream = trim_stream(translated_stream, width)
            # get the first pair and put it explicitly at (y, 0)
            iterated_stream = iter(trimmed_stream)
            text, attr = next(iterated_stream)
            self._screen.addstr(y, 0, text, attr)
            # the rest are positioned implicitly
            for text, attr in iterated_stream:
                self._screen.addstr(text, attr)


def trim_stream(stream: OutputStream, length: int) -> OutputStream:
    """
    given a stream of output text/attr pairs, modify and return the stream such
    that the resulting text is at most `length` characters long. this function
    consumes the entire stream, regardless of whether or not the entire stream
    is included in the output, so that an underlying StransiInstructionStream-
    -Translator correctly processes all instructions in its input stream
    """
    idx = 0
    for text, attr in stream:
        if len(text) + idx < length:
            idx += len(text)
            yield text, attr
        else:
            remainder = length - idx
            trimmed = text[:remainder]
            idx += remainder
            yield trimmed, attr
            break

    list(stream)


def win_main(stdscr: CursesWindow, command: list[str], draw_rate=None, read_rate=None):

    draw_rate = draw_rate or DRAW_RATE
    read_rate = read_rate or READ_RATE

    curses.use_default_colors()
    stdscr.nodelay(True)

    # with open(root.joinpath("bigtext"), "r", encoding="utf-8") as fin:
    #     text = fin.read()

    printer = Printer(stdscr)

    running = True

    while running:
        loop_start = time.monotonic()

        curses.update_lines_cols()
        stdscr.clear()

        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        text = proc.stdout.decode("utf-8")

        lines = [Ansi(line) for line in text.splitlines()]

        printer.print(lines)
        max_y, _ = stdscr.getmaxyx()
        stdscr.move(max_y - 1, 0)
        stdscr.refresh()

        while True:
            c = stdscr.getch()
            if c == ord("q") or c == ord("Q"):
                # time to goodbye
                running = False
                break

            now = time.monotonic()
            if (now - loop_start) > draw_rate:
                # time to draw
                break

            time.sleep(read_rate)


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
