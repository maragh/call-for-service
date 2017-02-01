import datetime as dt
import pandas as pd
from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.models import (CallLog, Transaction, CallUnit, ShiftUnit, Shift,
                         update_materialized_views)
from officer_allocation.models import (OfficerActivityType)

def isnan(x):
    return x is None or (type(x) == float and math.isnan(x))

def safe_datetime(x):
    if x is pd.NaT:
        return None
    return x

def safe_sorted(coll):
    return sorted(x for x in coll if not isnan(x))


class Command(BaseCommand):
    help = "Load officer allocation data from CSV files."

    def add_arguments(self, parser):
        parser.add_argument('--call-log-file', type=str, required=True,
                            help='The file containing the call log data.')
        parser.add_argument('--shift-file', type=str, required=True,
                            help='The file containing the shift data.')

    def log(self, message):
        if self.start_time:
            current_time = dt.datetime.now()
            period = current_time - self.start_time
        else:
            period = dt.timedelta(0)
        print("[{:7.2f}] {}".format(period.total_seconds(), message))

    def handle(self, *args, **options):
        self.start_time = dt.datetime.now()

        self.batch_size = 2000
        self.log("Loading call log CSV")
        self.call_log = pd.read_csv(options['call_log_file'],
                              parse_dates=['Timestamp'],
                              dtype={'Internal ID': str, 'Transaction': str,
                                     'Unit': str})

        self.log("CSV loaded")

        self.create_transactions()

        self.log("Loading shift CSV")
        self.shifts = pd.read_csv(options['shift_file'],
                                 parse_dates=['In Timestamp', 'Out Timestamp'],
                                 dtype={'Unit': str})

        self.create_units()

        self.create_call_log()
        self.create_shifts()

        self.create_officer_activity_types()

        self.log("Updating materialized views")
        update_materialized_views()

    def create_transactions(self):
        self.log("Creating transactions")
        df = self.call_log

        transaction_tuples = [x for x in pd.DataFrame(
            df.groupby('Transaction Code')['Transaction Text'].min()).itertuples()
                              if x[0]]
        transactions = [Transaction.objects.get_or_create(code=t[0], defaults={'descr': t[1]})[0]
                        for t in transaction_tuples]
        transaction_map = {t.code: t.transaction_id for t in transactions}
        df['Transaction ID'] = df['Transaction Code'].apply(lambda x: transaction_map.get(x),
                                         convert_dtype=False)

    def create_units(self):
        self.log("Creating units")

        unit_series = pd.concat([self.call_log['Unit'], self.shifts['Unit']])

        unit_names = safe_sorted(unit_series.unique())
        units = [CallUnit.objects.get_or_create(descr=name)[0]
                 for name in unit_names]
        unit_map = {u.descr: u.call_unit_id for u in units}
        self.call_log['Unit ID'] = self.call_log['Unit'].apply(lambda x: unit_map.get(x),
                                                               convert_dtype=False)
        self.shifts['Unit ID'] = self.shifts['Unit'].apply(lambda x: unit_map.get(x),
                                                         convert_dtype=False)

    def create_call_log(self):
        start = 0
        while start < len(self.call_log):
            batch = self.call_log[start:start + self.batch_size]
            call_logs = []

            for idx, c in batch.iterrows():
                call_log = CallLog(call_id=c['Internal ID'],
                                   call_unit_id=c['Unit ID'],
                                   time_recorded=safe_datetime(c['Timestamp']),
                                   transaction_id=c['Transaction ID'])
                call_logs.append(call_log)

            CallLog.objects.bulk_create(call_logs)
            self.log("CallLog {}-{} created".format(start, start+len(batch)))
            start += self.batch_size

    def create_shifts(self):
        self.log("Creating shifts")

        # Some weirdness necessary here for backwards compatibility --
        # Durham had shift <-> shift_unit as a 1 <-> many, where there's a shift
        # for each unit and a shift_unit for each officer (since there can be multiple
        # officers per unit).  We don't care about that anymore, since we don't get
        # officer information; just create a shift for each new shift_unit to avoid
        # issues with the rest of the code base expecting each shift_unit to correspond to
        # a shift.
        start = 0
        while start < len(self.shifts):
            batch = self.shifts[start:start + self.batch_size]
            shift_units = []

            for idx, s in batch.iterrows():
                # Can't create the shifts in bulk, since we need their PKs to link
                # them to the shift_units
                shift = Shift.objects.create()
                shift_unit = ShiftUnit(in_time=s['In Timestamp'],
                                       out_time=s['Out Timestamp'],
                                       call_unit_id=s['Unit ID'],
                                       shift=shift)
                shift_units.append(shift_unit)

            ShiftUnit.objects.bulk_create(shift_units)
            self.log("ShiftUnit {}-{} created".format(start, start+len(batch)))
            start += self.batch_size


    def create_officer_activity_types(self):
        self.log("Creating officer activity types...")
        types = [
            'IN CALL - CITIZEN INITIATED',
            'IN CALL - SELF INITIATED',
            'IN CALL - DIRECTED PATROL',
            'OUT OF SERVICE',
            'ON DUTY'
        ]

        for t in types:
            OfficerActivityType.objects.get_or_create(descr=t)
