from aiogram.fsm.state import State, StatesGroup


class ManagedPoolFSM(StatesGroup):
    """FSM for creating a new managed IP pool."""
    choosing_name = State()
    choosing_ip_sets = State()
    choosing_tag = State()
    choosing_threshold = State()
    choosing_interval = State()
    confirming = State()
