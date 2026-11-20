"""A transport module for the resolver test: ``pkg.module`` with no attr
means "call its build"."""


class FakeTransport:
    paused_reason = ""

    async def __call__(self, prompt, *, agent, task, timeout, expect_json):
        return "fake"


def build():
    return FakeTransport()
