#!/usr/local/bin/python3
"""
A quick little program somewhere between `watch` and `less`
"""

from __future__ import annotations

import argparse
import curses
import logging
import re
import subprocess
import time
import traceback
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal, Optional, Union

from stransi import Ansi
from stransi.attribute import Attribute, SetAttribute
from stransi.color import ColorRole, SetColor
from stransi.instruction import Instruction

if TYPE_CHECKING:
    CursesWindow = curses._CursesWindow
    # that thing what comes outta `Ansi.instructions()`
    InstructionStream = Iterable[Union[str, Instruction]]
    # a list of text/attr pairs
    OutputStream = Iterable[tuple[str, int]]
    # a dictionary mapping foreground/background pairs to curses color registry ids
    ColorMap = dict[tuple[int, int], int]

    Num = Union[float, int]
    Boundary = Literal["max", "min"]
    IntBoundary = Union[int, Boundary]
    FloatBoundary = Union[float, Boundary]
    NumBoundary = Union[Num, Boundary]

# root = Path(__file__).parent
root = Path(".")
logfile = root.joinpath("viewpane.log")
logging.root.addHandler(logging.NullHandler())
logging.basicConfig(
    filename=logfile.as_posix(),
    format="[%(asctime)s %(levelname)-8s %(name)s] %(message)s",
    level=logging.DEBUG,
)
logger = logging.getLogger("viewpane")

DEFAULT_DRAW_RATE = 2
DEFAULT_CHECK_RATE = 0.2


class StransiInstructionStreamTranslator:
    """
    A class for translating a stream of stransi instructions into a stream of
    text/attr pairs suitable for use in `stdscr.addstr(y, x, str, attr)`.

    This is a class instead of a function, because the stream needs to keep
    track of some state between invocations, including the current attr of the
    stream, and what color pairs known to curses.
    """

    def __init__(
        self,
        init_attr: int = 0,
        init_color_num: Optional[int] = None,
        color_map: Optional[ColorMap] = None,
    ):
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
                if not item.color:
                    logger.warning("colorless color??")
                    continue
                if item.role == ColorRole.FOREGROUND:
                    fg = item.color.ansi256.code
                elif item.role == ColorRole.BACKGROUND:
                    bg = item.color.ansi256.code
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
    """Write Ansi lines into a curses pad, and keep track of its location"""

    def __init__(
        self,
        stdscr: CursesWindow,
        pad: Optional[CursesWindow] = None,
        coords: tuple[int, int] = (0, 0),
        color_map: Optional[ColorMap] = None,
    ):
        """
        Initialize a new PadWriter.

        :param pad: the pad to print into
        :param coords: the initial (y, x) coordinates of the top left corner of the pad
        :param color_map: initial ColorMap dictionary passed to StransiInstructionStreamTranslator
        """
        self.stdscr: CursesWindow = stdscr
        if not pad:
            pad = curses.newpad(24, 80)
            pad.keypad(True)
        self.pad: CursesWindow = pad
        self.coords: tuple[int, int] = coords
        self.color_map: ColorMap = color_map or {}
        self.translator = StransiInstructionStreamTranslator(color_map=self.color_map)

    def write(self, ansi_lines: list[Ansi]) -> None:
        """
        write a sequence of ansi lines to the pad, starting at 0, 0. clears any
        prior pad contents. lines are assumed to not have embedded newlines.
        """

        logger.info("writing %s lines into pad", len(ansi_lines))

        self._resize(ansi_lines)
        self.pad.erase()

        for y, line in enumerate(ansi_lines):
            # ansi translation magic
            instructions = line.instructions()
            # turn our instructions into text/attr pairs
            stream = iter(self.translator.translate_ansi_instruction_stream(instructions))
            # get the first pair and put it explicitly at (y, 0)
            try:
                text, attr = next(stream)
                self.pad.addstr(y, 0, text, attr)
            except StopIteration:
                continue
            # the rest are positioned implicitly
            for text, attr in stream:
                self.pad.addstr(text, attr)

        logger.debug("lines written")

    def _resize(self, ansi_lines: list[Ansi]) -> None:
        """make our pad fit these lines"""
        lines_y = len(ansi_lines) or 1
        lines_x = max(map(ansi_length, ansi_lines)) if ansi_lines else 1

        logger.debug("resizing pad to %s, %s", lines_y, lines_x)
        self.pad.resize(lines_y, lines_x + 1)

    def move_by(self, move_y: int, move_x: int):
        """shift the pad, but stay inside the borders"""
        y, x = self.coords
        max_y, max_x = self.pad.getmaxyx()
        # move, but stay inside our pad
        y = int(bound(0, y + move_y, max_y - 1))
        x = int(bound(0, x + move_x, max_x - 1))
        logger.debug("moving pad coordinates to (%s, %s)", y, x)
        self.coords = (y, x)

    def jump_to(self, new_y: Optional[IntBoundary], new_x: Optional[IntBoundary]) -> None:
        """jump to specific coordinates, or a specific edge"""
        old_y, old_x = self.coords
        max_y, max_x = self.pad.getmaxyx()

        new_y = new_y if new_y is not None else old_y
        new_x = new_x if new_x is not None else old_x

        y = int(bound(0, new_y, max_y - 1))
        x = int(bound(0, new_x, max_x - 1))

        self.coords = (y, x)

    def refresh(self):
        """display the pad"""
        logger.info("refreshing the pad")
        y, x = self.coords
        self.stdscr.erase()
        self.stdscr.noutrefresh()
        self.pad.noutrefresh(y, x, 0, 0, curses.LINES - 1, curses.COLS - 1)
        self.stdscr.move(curses.LINES - 1, 0)
        self.stdscr.noutrefresh()
        curses.doupdate()


class Action(Enum):
    """The set of actions we can perform."""

    SHIFT_UP = "shift_up"
    SHIFT_DOWN = "shift_down"
    SHIFT_LEFT = "shift_left"
    SHIFT_RIGHT = "shift_right"

    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    PAGE_LEFT = "page_left"
    PAGE_RIGHT = "page_right"

    HALF_PAGE_UP = "half_page_up"
    HALF_PAGE_DOWN = "half_page_down"
    HALF_PAGE_LEFT = "half_page_left"
    HALF_PAGE_RIGHT = "half_page_right"

    JUMP_TOP = "jump_top"
    JUMP_BOTTOM = "jump_bottom"
    JUMP_LEFT = "jump_left"
    JUMP_RIGHT = "jump_right"

    QUIT = "quit"
    RESIZE = "resize"

    # ADD_CHORD = "add_chord"
    # CLEAR_CHORD = "clear_chord"
    # DO_CHORD = "do_chord"


class Viewpane:
    # V_SHIFT = 10
    # H_SHIFT = 20
    V_SHIFT = 1
    H_SHIFT = 10

    KEYMAP = {
        # Special
        "q": Action.QUIT,
        "KEY_RESIZE": Action.RESIZE,
        # keyboard movement keys
        "KEY_UP": Action.SHIFT_UP,
        "KEY_DOWN": Action.SHIFT_DOWN,
        "KEY_LEFT": Action.SHIFT_LEFT,
        "KEY_RIGHT": Action.SHIFT_RIGHT,
        "KEY_PPAGE": Action.PAGE_UP,
        "KEY_NPAGE": Action.PAGE_DOWN,
        "KEY_HOME": Action.JUMP_TOP,
        "KEY_END": Action.JUMP_BOTTOM,
        # VIM line-wise movement
        "h": Action.SHIFT_LEFT,
        "j": Action.SHIFT_DOWN,
        "k": Action.SHIFT_UP,
        "l": Action.SHIFT_RIGHT,
        # VIM page movement
        "u": Action.HALF_PAGE_UP,
        "d": Action.HALF_PAGE_DOWN,
        "H": Action.PAGE_LEFT,
        "J": Action.PAGE_DOWN,
        "K": Action.PAGE_UP,
        "L": Action.PAGE_RIGHT,
        # VIM jump
        "g": Action.JUMP_TOP,
        "G": Action.JUMP_BOTTOM,
        "0": Action.JUMP_LEFT,
        "$": Action.JUMP_RIGHT,
    }

    def __init__(
        self,
        window: CursesWindow,
        command: Union[str, list[str]],
        draw_rate: Optional[Num] = None,
        check_rate: Optional[Num] = None,
    ) -> None:
        self.window = window

        # command may be passed as a list of tokens, or as a single string
        if isinstance(command, list):
            command = quote_str_list(command)

        self.command = command

        self.draw_rate = draw_rate or DEFAULT_DRAW_RATE
        self.check_rate = check_rate or DEFAULT_CHECK_RATE

        logger.debug("draw rate is %s", draw_rate)
        logger.debug("check rate is %s", check_rate)

        curses.use_default_colors()
        curses.halfdelay(int(self.check_rate * 10))

        self.manager = PadManager(self.window)

    def run(self):
        self.draw()

        mark = time.monotonic()
        while True:
            # logger.debug("top of tight loop")
            now = time.monotonic()
            # if enough time has elapsed, redraw
            if (now - mark) > self.draw_rate:
                mark = now
                self.draw()

            # check for keypress
            action = self.read_and_interpret_keypress()
            if action == "quit":
                break

    def draw(self) -> None:
        """Execute the given command, write it into the PadManager, and refresh it."""

        logger.debug("calling command (%s)!", self.command)
        status, text = subprocess.getstatusoutput(self.command)
        if status:
            raise RuntimeError("command failed", self.command, text)
        lines = [Ansi(line) for line in text.splitlines()]

        self.manager.write(lines)
        self.manager.refresh()

    def read_and_interpret_keypress(self) -> Optional[str]:
        if not (maybe_key := self.check_keypress()):
            return

        if not (action := self.interpret_keypress(maybe_key)):
            return

        if result := self.perform_action(action):
            return result

    def check_keypress(self) -> Optional[str]:
        """check for a keypress without blocking"""
        try:
            return self.manager.pad.getkey()
        except curses.error as exc:
            if exc.args == ("no input",):
                return None
            else:
                raise

    def interpret_keypress(self, key: str) -> Optional[Action]:
        """Determine the appropriate action for a given keypress."""
        return self.KEYMAP.get(key)

    def perform_action(self, action: Action) -> Optional[str]:
        """
        Perform an action. If furthur handling is required, returns an
        appropriate string. Otherwise returns None.
        """

        if action == Action.QUIT:
            return "quit"

        if action == Action.RESIZE:
            curses.update_lines_cols()

        elif action == Action.SHIFT_UP:
            self.manager.move_by(-self.V_SHIFT, 0)
        elif action == Action.SHIFT_DOWN:
            self.manager.move_by(self.V_SHIFT, 0)
        elif action == Action.SHIFT_LEFT:
            self.manager.move_by(0, -self.H_SHIFT)
        elif action == Action.SHIFT_RIGHT:
            self.manager.move_by(0, self.H_SHIFT)

        elif action == Action.PAGE_UP:
            self.manager.move_by(-curses.LINES, 0)
        elif action == Action.PAGE_DOWN:
            self.manager.move_by(curses.LINES, 0)
        elif action == Action.PAGE_LEFT:
            self.manager.move_by(0, -curses.COLS)
        elif action == Action.PAGE_RIGHT:
            self.manager.move_by(0, curses.COLS)

        elif action == Action.HALF_PAGE_UP:
            self.manager.move_by((-curses.LINES // 2), 0)
        elif action == Action.HALF_PAGE_DOWN:
            self.manager.move_by((curses.LINES // 2), 0)
        elif action == Action.HALF_PAGE_LEFT:
            self.manager.move_by(0, (-curses.COLS // 2))
        elif action == Action.HALF_PAGE_RIGHT:
            self.manager.move_by(0, (curses.COLS // 2))

        elif action == Action.JUMP_TOP:
            self.manager.jump_to("min", None)
        elif action == Action.JUMP_BOTTOM:
            self.manager.jump_to("max", None)
        elif action == Action.JUMP_LEFT:
            self.manager.jump_to(None, "min")
        elif action == Action.JUMP_RIGHT:
            self.manager.jump_to(None, "max")

        else:
            raise ValueError("Invalid action", action)

        self.manager.refresh()


def ansi_length(ansi: Ansi):
    """get the printing length of an Ansi line"""
    return sum(len(item) for item in ansi.instructions() if isinstance(item, str))


def quote_str_list(values: list[str]) -> str:
    """
    Quote a list of strings for a shell, preserving whitespace but still
    allowing for shell parameter expansion.
    """
    output = []
    for item in values:
        # using `re` is the fastest way to do this; i checked
        if re.search("[\\s'\"\\\\]", item):
            item = '"' + re.sub(r'("|\\)', r"\\\1", item) + '"'
        output.append(item)
    return " ".join(output)


def bound(lower: Num, value: NumBoundary, upper: Num) -> Num:
    """
    Return `value` (which may be either a number or the literals
    'max' or 'min'), bounded within `lower` and `upper`, inclusive.
    """
    if value == "max":
        return upper
    if value == "min":
        return lower
    if isinstance(value, (int, float)):
        return min(max(lower, value), upper)
    raise ValueError("Invalid value", value)


def main():
    """Program Main: parse args, start curses, and handle exceptions."""
    logger.info("starting")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--delay", type=float, help="How frequently to run the watched command")

    parser.add_argument("command", nargs="*", help="the command to watch")
    parser.add_argument("-c", "--command", dest="command_str", help="the command to watch (as a quoted string)")

    args = parser.parse_args()

    # we require command XOR command_str
    if bool(args.command) == bool(args.command_str):
        parser.error("Must pass `command` or `--command`, but not both")

    try:
        curses.wrapper(win_main, args.command or args.command_str, draw_rate=args.delay)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("caught fatal error: %s", exc)
        traceback.print_exc()
    finally:
        logger.info("stopping")


def win_main(
    stdscr: CursesWindow,
    command: Union[str, list[str]],
    draw_rate: Union[int, float, None] = None,
    check_rate: Union[int, float, None] = None,
) -> None:
    viewpane = Viewpane(stdscr, command, draw_rate, check_rate)
    viewpane.run()


if __name__ == "__main__":
    main()
