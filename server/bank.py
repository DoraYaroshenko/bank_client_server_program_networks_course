"""In-memory bank: accounts, authentication, balances, transfers, history,
calendar-monthly standing orders and loans.

Designed for a single-threaded select()-based server: no locks. All timestamps
come from the bank's own ``SimTime`` clock. Due standing orders and loan
repayments are processed when the main loop calls ``run_due()`` (and after
the operator advances the bank date via ``on_console_line``).
"""
from __future__ import annotations

import logging
import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Set, Dict, Optional, List
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from server_utils import SimTime

log = logging.getLogger("bank")

import random
import string

type ClientId = str
type ActionId = str


def create_id() -> str:
    characters = string.ascii_letters + string.digits
    new_id = ''.join(random.choices(characters, k=16))
    return new_id


def create_client_id() -> str:
    characters = string.ascii_uppercase + string.digits
    new_id = ''.join(random.choices(characters, k=10))
    return new_id


def add_one_month(dt, day_of_month):
    year = dt.year + (dt.month // 12)
    month = (dt.month % 12) + 1
    # Find the last valid day of the target month
    max_day = calendar.monthrange(year, month)[1]
    # day_of_month is the standing order's own day, so Jan 31 -> Feb 28 -> Mar 31
    day = min(day_of_month, max_day)
    return dt.replace(year=year, month=month, day=day)


class Bank:
    class LoginResult(Enum):
        SUCCESS = 0
        WRONG_PASSWORD = 1
        UNKNOWN_CLIENT_ID = 2

    @dataclass
    class PastActions:
        incoming_actions: Set[Action]
        outgoing_actions: Set[Action]

    def __init__(self, start_date: date):
        self.clock = SimTime(start_date=start_date)
        self.actions: Dict[ActionId, Action] = {}
        self.clients: Dict[ClientId, Client] = {}
        self.future_actions: Dict[date, Set[ActionId]] = defaultdict(set)
        # the last day do_today ran, so every day runs exactly once
        self.last_day_done: date = start_date

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now())

    def announce_date(self):
        print(f"Bank date: {self.clock.today()} (press Enter for the next day)", flush=True)

    def register_client(self, name: str, password: str) -> ClientId:
        # name: 3-20 letters, digits or _, password: at least 4 characters. "" means rejected
        if re.fullmatch(r"[A-Za-z0-9_]{3,20}", name) is None or len(password) < 4:
            return ""
        new_client_id: ClientId = create_client_id()
        while new_client_id in self.clients.keys():
            new_client_id = create_client_id()
        new_client = Client(
            client_id=new_client_id,
            name=name,
            password=password
        )
        self.clients[new_client_id] = new_client
        return new_client_id

    def login(self, client_id: ClientId, password: str) -> LoginResult:
        if client_id not in self.clients.keys():
            return Bank.LoginResult.UNKNOWN_CLIENT_ID
        client = self.clients[client_id]

        if password != client.password:
            return Bank.LoginResult.WRONG_PASSWORD

        return Bank.LoginResult.SUCCESS

    def get_past_actions(self, client_id: ClientId) -> PastActions:
        # TODO what if client_id does not exist
        client = self.clients[client_id]
        return Bank.PastActions(
            incoming_actions={self.actions[action_id] for action_id in client.incoming_actions},
            outgoing_actions={self.actions[action_id] for action_id in client.outgoing_actions},
        )

    def get_active_actions(self, client_id: ClientId) -> Set[Action]:
        client = self.clients[client_id]
        return {self.actions[action_id] for action_id in client.active_actions}

    def get_balance(self, client_id: ClientId) -> float:
        return self.clients[client_id].balance

    def get_name(self, client_id: ClientId) -> str:
        return self.clients[client_id].name

    def do_action(self, action_id: ActionId) -> bool:
        action = self.actions[action_id]
        result = action.do(self)
        next_action = result.future_action
        if next_action is not None:
            self.actions[next_action.action.action_id] = next_action.action
            self.future_actions[next_action.do_at].add(next_action.action.action_id)
        return result.is_successful

    def deposit(self, client_id: ClientId, amount: float) -> bool:
        new_deposit_id = create_id()
        new_deposit = Deposit(action_id=new_deposit_id, created_by_client=client_id, amount=amount,
                              date_created=self.clock.today(), created_by_action=None)
        self.actions[new_deposit_id] = new_deposit
        return self.do_action(new_deposit_id)

    def withdraw(self, client_id: ClientId, amount: float) -> bool:
        new_withdrawal_id = create_id()
        new_withdrawal = Withdrawal(action_id=new_withdrawal_id, created_by_client=client_id, amount=amount,
                                    date_created=self.clock.today(), created_by_action=None)
        self.actions[new_withdrawal_id] = new_withdrawal
        return self.do_action(new_withdrawal_id)

    def transfer(self, from_client_id: ClientId, to_client_id: ClientId, amount: float) -> bool:
        new_transfer_id = create_id()
        new_transfer = Transfer(
            action_id=new_transfer_id,
            created_by_client=from_client_id,
            to_client=to_client_id,
            amount=amount,
            date_created=self.clock.today(),
            created_by_action=None
        )
        self.actions[new_transfer_id] = new_transfer
        return self.do_action(new_transfer_id)

    def loan(self, client_id: ClientId, amount: float) -> ActionId:
        new_loan_id = create_id()
        new_loan = TakeLoan(action_id=new_loan_id, date_created=self.clock.today(), created_by_client=client_id,
                            created_by_action=None, amount=amount)
        self.actions[new_loan_id] = new_loan
        if self.do_action(new_loan_id):
            # the id of the scheduled repayment, which is the id the active loans list shows
            return new_loan._return_loan_id
        return ""

    def create_standing_order(self, from_client_id: ClientId, to_client_id: ClientId, amount: float,
                              number_of_payments: Optional[int]) -> ActionId:
        new_standing_order_id = create_id()
        new_standing_order = StandingOrder(action_id=new_standing_order_id, amount=amount,
                                           created_by_client=from_client_id, number_of_payments=number_of_payments,
                                           created_by_action=None, to_client=to_client_id,
                                           date_created=self.clock.today())
        self.actions[new_standing_order_id] = new_standing_order
        if self.do_action(new_standing_order_id):
            return new_standing_order_id
        return ""

    def cancel_standing_order(self, client_id: ClientId, standing_order_id: ActionId) -> bool:
        if client_id not in self.clients.keys():
            return False
        client = self.clients[client_id]

        if standing_order_id not in client.active_actions:
            return False

        # the next payment is scheduled in future_actions and was created by the active standing order
        for action_ids in self.future_actions.values():
            for action_id in action_ids:
                next_payment = self.actions[action_id]
                if isinstance(next_payment, StandingOrder) and next_payment._created_by_action == standing_order_id:
                    action_ids.remove(action_id)
                    client.active_actions.remove(standing_order_id)
                    return True
        return False

    def do_today(self):
        #TODO: return statuses for all actions
        # run every day since the last run, each day once (none if the date didn't change)
        while self.last_day_done < self.clock.today():
            self.last_day_done += timedelta(days=1)
            for client in self.clients.values():
                if client.balance<0:
                    new_minus = Minus(action_id=create_id(),created_by_action=None,created_by_client=client.client_id, date_created=self.last_day_done, amount=-client.balance*0.0005)
                    self.actions[new_minus.action_id] = new_minus
                    self.do_action(new_minus.action_id)
            for action_id in self.future_actions[self.last_day_done]:
                self.do_action(action_id)
            self.future_actions.pop(self.last_day_done)

class Client:
    def __init__(self, client_id: ClientId, name: str, password: str):
        self.client_id: ClientId = client_id
        self.name: str = name
        self.password: str = password
        self.balance: float = 0
        self.incoming_actions: Set[ActionId] = set()
        self.outgoing_actions: Set[ActionId] = set()
        self.active_actions: Set[ActionId] = set()


class Action(ABC):
    ACTION_TYPE = "ACTION"

    @dataclass
    class FutureAction:
        do_at: date
        action: Action

    @dataclass
    class ActionResult:
        is_successful: bool
        future_action: Optional[Action.FutureAction]

        @classmethod
        def failed_action(cls) -> Action.ActionResult:
            return Action.ActionResult(
                is_successful=False,
                future_action=None
            )

        @classmethod
        def successful_action_no_future_action(cls) -> Action.ActionResult:
            return Action.ActionResult(
                is_successful=True,
                future_action=None
            )

    def __init__(
            self,
            action_id: ActionId,
            date_created: date,
            created_by_client: ClientId,
            amount: float,
            created_by_action: Optional[ActionId]
    ):
        self.action_id: ActionId = action_id
        self._date_created: date = date_created
        self._created_by_client: ClientId = created_by_client
        self._amount: float = amount
        self._created_by_action: Optional[ActionId] = created_by_action
        self._done_at: Optional[datetime] = None

    @abstractmethod
    def do(self, bank: Bank) -> ActionResult:
        pass

    def action_type(self) -> str:
        return self.ACTION_TYPE


class Deposit(Action):
    ACTION_TYPE = "DEPOSIT"

    def do(self, bank: Bank) -> Action.ActionResult:
        if self._amount <= 0:
            return Action.ActionResult.failed_action()

        client = bank.clients[self._created_by_client]
        client.incoming_actions.add(self.action_id)
        client.balance += self._amount
        self._done_at = bank.now()
        return Action.ActionResult.successful_action_no_future_action()


class Withdrawal(Action):
    ACTION_TYPE = "WITHDRAWAL"

    def do(self, bank: Bank) -> Action.ActionResult:
        if self._amount <= 0:
            return Action.ActionResult.failed_action()
        client = bank.clients[self._created_by_client]
        client.outgoing_actions.add(self.action_id)
        client.balance -= self._amount
        self._done_at = bank.now()
        return Action.ActionResult.successful_action_no_future_action()

class Minus(Action):
    ACTION_TYPE = "MINUS"

    def do(self, bank: Bank):
        client = bank.clients[self._created_by_client]
        client.outgoing_actions.add(self.action_id)
        client.balance-=self._amount
        self._done_at = bank.now()
        return Action.ActionResult.successful_action_no_future_action()


class Transfer(Action):
    ACTION_TYPE = "TRANSFER"

    def __init__(
            self,
            action_id: ActionId,
            date_created: date,
            created_by_client: ClientId,
            amount: float,
            created_by_action: Optional[ActionId],
            to_client: ClientId
    ):
        super().__init__(action_id, date_created, created_by_client, amount, created_by_action)
        self._to_client = to_client

    def do(self, bank: Bank) -> Action.ActionResult:
        if self._to_client not in bank.clients:
            return Action.ActionResult.failed_action()
        from_client = bank.clients[self._created_by_client]
        to_client = bank.clients[self._to_client]
        if self._amount <= 0:
            return Action.ActionResult.failed_action()
        from_client.outgoing_actions.add(self.action_id)
        to_client.incoming_actions.add(self.action_id)
        from_client.balance -= self._amount
        to_client.balance += self._amount
        self._done_at = bank.now()
        return Action.ActionResult.successful_action_no_future_action()


class ReturnLoan(Action):
    ACTION_TYPE = "LOAN_RETURN"

    def do(self, bank: Bank) -> Action.ActionResult:
        client = bank.clients[self._created_by_client]
        client.outgoing_actions.add(self.action_id)
        client.active_actions.remove(self.action_id)
        client.balance -= self._amount
        self._done_at = bank.now()
        return Action.ActionResult.successful_action_no_future_action()


class TakeLoan(Action):
    ACTION_TYPE = "TAKE_LOAN"

    def do(self, bank: Bank) -> Action.ActionResult:
        client = bank.clients[self._created_by_client]
        if self._amount <= 0:
            return Action.ActionResult.failed_action()
        # cant take loan with minus
        if client.balance < 0:
            return Action.ActionResult.failed_action()
        client.incoming_actions.add(self.action_id)
        client.balance += self._amount
        self._done_at = bank.now()
        return_loan_action = ReturnLoan(
            action_id=create_id(),
            date_created=bank.clock.today(),
            created_by_client=self._created_by_client,
            created_by_action=self.action_id,
            amount=self._amount * 1.05  # 5% interest
        )
        client.active_actions.add(return_loan_action.action_id)
        self._return_loan_id = return_loan_action.action_id
        return Action.ActionResult(
            is_successful=True,
            future_action=Action.FutureAction(
                action=return_loan_action,
                do_at=self._date_created + timedelta(days=30)
            )
        )


class StandingOrder(Transfer):
    ACTION_TYPE = "STANDING_ORDER"

    def __init__(
            self,
            action_id: ActionId,
            date_created: date,
            created_by_client: ClientId,
            amount: float,
            created_by_action: Optional[ActionId],
            to_client: ClientId,
            number_of_payments: Optional[int],
            day_of_month: Optional[int] = None
    ):
        super().__init__(action_id, date_created, created_by_client, amount, created_by_action, to_client)
        self._number_of_payments = number_of_payments
        # the day of the month the payments are made on (the day the standing order was created)
        self._day_of_month = day_of_month if day_of_month is not None else date_created.day

    def do(self, bank: Bank) -> Action.ActionResult:
        if self._to_client not in bank.clients:
            return Action.ActionResult.failed_action()
        from_client = bank.clients[self._created_by_client]
        to_client = bank.clients[self._to_client]

        if self._amount <= 0:
            return Action.ActionResult.failed_action()

        from_client.balance -= self._amount
        to_client.balance += self._amount
        from_client.outgoing_actions.add(self.action_id)
        to_client.incoming_actions.add(self.action_id)
        new_number_of_payments = self._number_of_payments - 1 if self._number_of_payments is not None else None
        # the previous payment is no longer the active one (the first payment has no previous one)
        if self._created_by_action is not None:
            from_client.active_actions.remove(self._created_by_action)
        self._done_at = bank.now()
        next_payment = StandingOrder(
            action_id=create_id(),
            created_by_client=self._created_by_client,
            date_created=bank.clock.today(),
            created_by_action=self.action_id,
            amount=self._amount,
            to_client=self._to_client,
            number_of_payments=self._number_of_payments - 1 if self._number_of_payments is not None else None,
            day_of_month=self._day_of_month
        )
        if new_number_of_payments is not None and new_number_of_payments <= 0:
            return Action.ActionResult.successful_action_no_future_action()
        else:
            from_client.active_actions.add(self.action_id)
        return Action.ActionResult(
            is_successful=True,
            future_action=Action.FutureAction(
                action=next_payment,
                do_at=add_one_month(bank.clock.today(), self._day_of_month)
            )
        )

