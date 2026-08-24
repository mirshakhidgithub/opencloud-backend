"""
Set a price list from the command line.

The platform-wide default list is deliberately not editable through the cabinet
— one account's administrator must not be able to change what every other
account pays — so it is set here, where changing it takes shell access.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.billing.models import Resource, Tariff, TariffRate


class Command(BaseCommand):
    help = 'Create or update a tariff. Prices are per unit per MONTH.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--account',
            default='',
            help='Account this list applies to. Omit for the platform-wide default.',
        )
        parser.add_argument('--currency', default='UZS')
        parser.add_argument('--name', help='Label for the list (default: derived from the account)')
        parser.add_argument(
            '--price',
            action='append',
            default=[],
            metavar='resource=amount',
            help=f'Repeatable. Resources: {", ".join(r.value for r in Resource)}',
        )
        parser.add_argument(
            '--keep-others',
            action='store_true',
            help='Leave resources that were not given alone instead of clearing them.',
        )

    def handle(self, *args, **options):
        known = {resource.value for resource in Resource}
        prices = {}

        for raw in options['price']:
            if '=' not in raw:
                raise CommandError(f'--price expects resource=amount, got {raw!r}')

            resource, _, amount = raw.partition('=')
            resource = resource.strip()
            if resource not in known:
                raise CommandError(f'Unknown resource {resource!r}. Known: {", ".join(sorted(known))}')

            try:
                value = float(amount)
            except ValueError:
                raise CommandError(f'Price for {resource} is not a number: {amount!r}')

            if value < 0:
                raise CommandError(f'Price for {resource} is negative')

            prices[resource] = value

        if not prices:
            raise CommandError('Give at least one --price resource=amount')

        account = options['account'].strip()
        name = options.get('name') or (f'{account} price list' if account else 'Default price list')

        tariff, created = Tariff.objects.update_or_create(
            account=account,
            defaults={'name': name, 'currency': options['currency'].strip(), 'is_active': True},
        )

        for resource, value in prices.items():
            TariffRate.objects.update_or_create(
                tariff=tariff, resource=resource, defaults={'price_per_month': value}
            )

        if not options['keep_others']:
            TariffRate.objects.filter(tariff=tariff).exclude(resource__in=prices).delete()

        scope = f'account {account}' if account else 'platform default'
        self.stdout.write(self.style.SUCCESS(f'{"Created" if created else "Updated"} {scope} ({tariff.currency}):'))
        for rate in tariff.rates.all():
            self.stdout.write(f'  {rate.resource:12} {rate.price_per_month} {tariff.currency} per unit per month')

        unpriced = sorted(known - set(prices))
        if unpriced:
            self.stdout.write(
                self.style.WARNING(f'  not charged (measured but no price): {", ".join(unpriced)}')
            )
