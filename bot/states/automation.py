from aiogram.fsm.state import State, StatesGroup


class AvailGroupFSM(StatesGroup):
    """FSM for creating a new 'Поддержание доступности IP' automation group."""
    choosing_panel = State()
    choosing_tag = State()
    choosing_source_type = State()   # user sets vs managed pool
    choosing_ip_sets = State()       # multi-select with checkmarks (sets mode)
    choosing_pool = State()          # pick managed pool (pool mode)
    choosing_distribution = State()
    choosing_interval = State()
    confirming = State()
