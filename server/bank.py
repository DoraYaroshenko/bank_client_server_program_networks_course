"""In-memory bank: accounts, authentication, balances, transfers, history,
calendar-monthly standing orders and loans.

Designed for a single-threaded select()-based server: no locks. All timestamps
come from the bank's own ``SimTime`` clock. Due standing orders and loan
repayments are processed when the main loop calls ``run_due()`` (and after
the operator advances the bank date via ``on_console_line``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from server_utils import SimTime

log = logging.getLogger("bank")

import random
import string

class Bank:
    def __init__(self, start_date: date):
        self.clock = SimTime(start_date=start_date)
        self.clients = {}

    class Client:
        def __init__(self, client_id, name,  password, balance):
            self.client_id = client_id
            self.name = name
            self.password = password
            self.balance = balance
            self.withdrawals = []
            self.deposits = []
            self.transfers = []
            self.standing_orders = []
            self.loans = []

    class Action:
        def __init__(self, amount, time):
            self.amount = amount
            self.time = time

    class Transfer(Action):
        def __init__(self, amount, time, from_client_id, to_client_id):
            super().__init__(amount, time)
            self.from_client_id = from_client_id
            self.to_client_id = to_client_id

    class StandingOrder(Transfer):
        def __init__(self, amount, time, from_client_id, to_client_id, day_of_month, number_of_transfers=-1):
            super().__init__(amount,time, from_client_id,to_client_id)
            self.day_of_month = day_of_month
            self.number_of_transfers = number_of_transfers

    class Loan(Action):
        def __init__(self, amount, time, return_time):
            super().__init__(amount,time)
            self.return_time = return_time

    def register_client(self, name, password):
        characters = string.ascii_letters + string.digits
        client_id = ''.join(random.choices(characters, k=10))
        new_client = self.Client(client_id, name, password, 0)
        self.clients[client_id] = new_client
        return client_id

    def login(self, client_id, password):
        if client_id not in self.clients.keys():
            return False
        if password!=self.clients[client_id].password:
            return False
        return True

    def deposit_or_withdraw(self, client_id, amount):
        time = self.clock.now()
        new_deposit_or_withdrawal = self.Action(amount,time)
        self.clients[client_id].balance += amount
        if amount>0:
            self.clients[client_id].deposits.append(new_deposit_or_withdrawal)
        else:
            self.clients[client_id].withdrawals.append(new_deposit_or_withdrawal)

    def transfer(self, amount, from_client_id, to_client_id):
        if amount<=0:
            return False
        time = self.clock.now()
        new_transfer = self.Transfer(amount,time,from_client_id,to_client_id)
        self.clients[to_client_id].balance += amount
        self.clients[from_client_id].balance-=amount
        self.clients[to_client_id].transfers.append(new_transfer)
        self.clients[from_client_id].transfers.append(new_transfer)
        return True

    def create_standing_order(self, amount, from_client_id, to_client_id,number_of_transfers):
        if amount<=0:
            return False
        time = self.clock.now()
        day = self.clock.today().day
        new_standing_order = self.StandingOrder(amount, time, from_client_id,to_client_id,day,number_of_transfers)
        self.clients[from_client_id].standing_orders.append(new_standing_order)
        return True

    def apply_standing_order(self, standing_order:StandingOrder):
        # check the day on server
        self.transfer(standing_order.amount, standing_order.from_client_id,standing_order.to_client_id)

    def cancel_standing_order(self, standing_order:StandingOrder):
        self.clients[standing_order.from_client_id].standing_orders.remove(standing_order)

    def show_all_standing_orders(self,client_id):
        return self.clients[client_id].standing_orders

    def take_loan(self, client_id,amount):
        time = self.clock.now()
        return_time = datetime.fromtimestamp(time).date().today()+timedelta(days=30)
        new_loan = self.Loan(amount,time, return_time)
        self.clients[client_id].loans.append(new_loan)
        self.clients[client_id].balance+=amount

    def return_loan(self, client_id, loan):
        self.clients[client_id].loans.remove(loan)
        self.clients[client_id].balance-=loan.amount+((loan.amount/100)*5)

    def show_all_loans(self,client_id):
        return self.clients[client_id].loans

    def minus(self,client_id):
        self.clients[client_id].balance -= (self.clients[client_id].balance/100)*0.05

    def is_in_minus(self,client_id):
        return self.clients[client_id].balance<0