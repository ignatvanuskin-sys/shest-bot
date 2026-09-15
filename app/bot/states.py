"""aiogram FSM states for the lead collection flow."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LeadForm(StatesGroup):
    # No state == Idle.
    Collecting = State()
    # Extraction of the buffered text is in flight (the LLM call takes tens of
    # seconds on free models). While in this state no second extraction may start:
    # it used to produce two cards and «Добавить» under the first one saved the
    # second lead's data.
    Processing = State()
    Reviewing = State()
    EditingField = State()
    ConfirmingDuplicate = State()
    ManualEntry = State()
