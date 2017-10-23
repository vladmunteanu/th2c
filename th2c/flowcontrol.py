from tornado import gen
from tornado.locks import Condition


class FlowControlWindow(object):

    def __init__(self, initial_value=0):
        self.value = initial_value
        self.condition = Condition()

    @gen.coroutine
    def consume(self, amount):
        if self.value > amount:
            self.condition.notify_all()

        yield self.condition.wait()

        consumed = min(self.value, amount)
        self.value -= consumed

        raise gen.Return(consumed)

    def produce(self, amount):
        self.value += amount
        self.condition.notify_all()
