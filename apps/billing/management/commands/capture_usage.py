"""Take today's usage snapshot without waiting for a worker."""

from datetime import date

from django.core.management.base import BaseCommand

from apps.billing.collector import capture


class Command(BaseCommand):
    help = 'Measure every project and store one usage snapshot per project for a day.'

    def add_arguments(self, parser):
        parser.add_argument('--date', help='ISO date to record the measurement under (default: today, UTC)')

    def handle(self, *args, **options):
        taken_on = date.fromisoformat(options['date']) if options.get('date') else None
        result = capture(taken_on)
        self.stdout.write(
            self.style.SUCCESS(
                f"{result['date']}: {result['projects']} projects recorded, {result['unattributed']} without an account"
            )
        )
