#!/usr/bin/env python

import argparse
import sys
import socketserver

from code import InteractiveConsole


class InteractiveServer(socketserver.BaseRequestHandler):
    def handle(self):
        file = self.request.makefile(mode='rw')
        shell = Shell(file)
        try:
            shell.interact()
        except SystemExit:
            pass


class Shell(InteractiveConsole):
    def __init__(self, file):
        self.file = sys.stdout = file
        InteractiveConsole.__init__(self)
        return

    def write(self, data):
        self.file.write(data)
        self.file.flush()

    def raw_input(self, prompt=""):
        self.write(prompt)
        return self.file.readline()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="start an interactive Python REPL that receives commands from a network connection"
    )
    parser.add_argument("-H", "--host", default="127.0.0.1")
    parser.add_argument("-p", "--port", type=int, default=9999)

    args = parser.parse_args()
    address = (args.host, args.port)

    server = socketserver.TCPServer(address, InteractiveServer)
    server.serve_forever()
