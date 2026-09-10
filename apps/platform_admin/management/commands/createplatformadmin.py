"""
Create the first operator. There is no self-service sign-up and no seed row —
the panel starts with nobody in it, and this command is the only way in.

    python manage.py createplatformadmin --email me@opencloud.uz --name "..." --role OWNER

The password is prompted for, never passed as an argument: a shell history file
is not where it belongs. TOTP is not set up here — the first sign-in walks the
operator through enrolment, so the secret only ever exists on their own device.

`--access-code` prompts for a static access code as well, which replaces TOTP
for this operator (see `setplatformadmincode`).
"""

from django.core.management.base import BaseCommand, CommandError

from apps.platform_admin.auth_views import access_code_problem, password_problem
from apps.platform_admin.management.prompts import ask_twice
from apps.platform_admin.models import ROLE_CHOICES, PlatformAdmin


class Command(BaseCommand):
    help = 'Create a platform operator (admin panel sign-in).'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True)
        parser.add_argument('--name', default='')
        parser.add_argument('--role', default='OWNER', choices=[r[0] for r in ROLE_CHOICES])
        parser.add_argument(
            '--access-code',
            action='store_true',
            help='Also prompt for a static access code, used instead of TOTP.',
        )

    def handle(self, *args, **options):
        email = options['email'].strip().lower()

        if PlatformAdmin.objects.filter(email=email).exists():
            raise CommandError(f'{email} already exists.')

        password = ask_twice('Password')

        problem = password_problem(password)
        if problem:
            raise CommandError(problem)

        admin = PlatformAdmin(email=email, name=options['name'] or email, role=options['role'])
        admin.set_password(password)

        if options['access_code']:
            code = ask_twice('Access code')
            problem = access_code_problem(admin, code)
            if problem:
                raise CommandError(problem)
            admin.set_access_code(code)

        admin.save()

        second_factor = (
            'Signs in with the static access code instead of TOTP.'
            if admin.uses_access_code
            else 'Two-factor enrolment happens on the first sign-in.'
        )
        self.stdout.write(self.style.SUCCESS(f'Created {admin.email} ({admin.role}). {second_factor}'))
