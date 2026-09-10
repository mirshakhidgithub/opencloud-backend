"""
Give an operator a static access code, or take it away.

    python manage.py setplatformadmincode --email me@opencloud.uz
    python manage.py setplatformadmincode --email me@opencloud.uz --clear

While set, the code is that operator's second factor INSTEAD of TOTP; nobody
else is affected. Clearing it puts them back on TOTP — onto the authenticator
they already enrolled, if any, otherwise through enrolment on the next sign-in.

Command line only, on purpose: the panel has no screen that weakens a second
factor, so a stolen operator session cannot trade one away either.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.platform_admin.auth_views import access_code_problem
from apps.platform_admin.management.prompts import ask_twice
from apps.platform_admin.models import PlatformAdmin


class Command(BaseCommand):
    help = "Set or clear a platform operator's static access code (used instead of TOTP)."

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--clear', action='store_true', help='Remove the code; the operator returns to TOTP.')

    def handle(self, *args, **options):
        email = options['email'].strip().lower()

        admin = PlatformAdmin.objects.filter(email=email).first()
        if admin is None:
            raise CommandError(f'No operator {email}.')

        if options['clear']:
            admin.set_access_code(None)
            admin.save(update_fields=['access_code_hash'])
            self.stdout.write(self.style.SUCCESS(f'{admin.email} signs in with TOTP again.'))
            return

        code = ask_twice('Access code')
        problem = access_code_problem(admin, code)
        if problem:
            raise CommandError(problem)

        admin.set_access_code(code)
        admin.save(update_fields=['access_code_hash'])
        self.stdout.write(self.style.SUCCESS(f'{admin.email} now signs in with the static access code instead of TOTP.'))
