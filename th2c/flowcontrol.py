from tornado import gen
from tornado.locks import Condition

from .config import DEFAULT_WINDOW_SIZE


class FlowControlWindow(object):

    __slots__ = ['condition', 'value']

    def __init__(self, initial_value=DEFAULT_WINDOW_SIZE):
        self.condition = Condition()
        self.value = initial_value

    @gen.coroutine
    def available(self, timeout=None):
        if self.value > 0:
            raise gen.Return(self.value)

        yield self.condition.wait(timeout=timeout)
        raise gen.Return(self.value)

    def consume(self, n):
        """Tries to consume n from value"""
        consumed = min(self.value, n)
        self.value -= consumed
        return consumed

    def produce(self, n):
        self.value += n
        self.condition.notify_all()
