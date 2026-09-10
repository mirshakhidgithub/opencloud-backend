"""Secret prompts shared by the operator commands."""

from getpass import getpass

from django.core.management.base import CommandError


def ask_twice(label: str) -> str:
    """Prompted, never an argument: a shell history file is not where a secret
    belongs. Asked twice because nobody can see what they typed."""
    value = getpass(f'{label}: ')
    if value != getpass(f'{label} (again): '):
        raise CommandError(f'The two {label.lower()}s do not match.')
    return value
