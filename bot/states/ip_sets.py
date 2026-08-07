from aiogram.fsm.state import State, StatesGroup


class AddIpSet(StatesGroup):
    waiting_addresses = State()
    waiting_tag = State()
