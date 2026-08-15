from aiogram.fsm.state import State, StatesGroup


class PingachockFSM(StatesGroup):
    waiting_url = State()
    waiting_key = State()
    selecting_nodes = State()
