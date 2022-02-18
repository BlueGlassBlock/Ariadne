import os
import sys

from graia.ariadne.util import Dummy

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..")))

import time

from devtools import debug

from graia.ariadne.message.chain import MessageChain
from graia.ariadne.message.commander import Arg, Commander, Slot
from graia.ariadne.message.element import At, Plain
from graia.ariadne.message.parser.literature import (
    BoxParameter,
    Literature,
    ParamPattern,
)
from graia.ariadne.message.parser.twilight import (
    ArgumentMatch,
    FullMatch,
    Sparkle,
    Twilight,
    WildcardMatch,
)

RUN = 20000

if __name__ == "__main__":
    print("Run 1:")
    li = Literature(".test", arguments={"f": BoxParameter(["foo"])})
    twi = Twilight([FullMatch(".test"), "foo" @ ArgumentMatch("--foo", "-f")])
    msg = MessageChain.create(".test", " --foo ", At(123))
    debug(li.parse_message(li.prefix_match(msg)))
    st = time.time()
    for _ in range(RUN):
        li.parse_message(li.prefix_match(msg))
    ed = time.time()

    print(f"Literature: {RUN / (ed-st):.2f}msg/s")

    debug(twi.generate(msg))
    print(repr(twi.generate(msg)))
    st = time.time()
    for _ in range(RUN):
        twi.generate(msg)
    ed = time.time()

    print(f"Twilight: {RUN / (ed-st):.2f}msg/s")

    print("Run 2:")

    twi = Twilight([FullMatch(".test"), WildcardMatch()])
    debug(li.parse_message(li.prefix_match(msg)))
    st = time.time()
    for _ in range(RUN):
        li.parse_message(li.prefix_match(msg))
    ed = time.time()

    print(f"Literature: {RUN / (ed-st):.2f}msg/s")

    debug(twi.generate(msg))

    debug(twi.generate(msg))

    st = time.time()
    for _ in range(RUN):
        twi.generate(msg)
    ed = time.time()

    print(f"Twilight: {RUN / (ed-st):.2f}msg/s")
