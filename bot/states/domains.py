from aiogram.fsm.state import State, StatesGroup


class DomainFSM(StatesGroup):
    waiting_manual_domain = State()
    waiting_new_a_name = State()
    waiting_new_a_ip = State()
    waiting_edit_ip = State()
