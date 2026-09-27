"""
Scheduled backups, told apart from the snapshots a client takes.

Some clients are backed up by us: a protection group snapshots their volumes on a
schedule. An account can be set not to count those — hidden from its cabinet and
left off its bill — because they are a service we run, not storage the client
chose to keep. The snapshots the client takes by hand are theirs, and are shown
and billed either way.

The cloud marks the difference itself: a snapshot a protection group took names
that group. That mark is the whole test — there is no naming convention to guess.
"""

from .models import AccountSettings


def is_backup(snapshot: dict) -> bool:
    """Whether a protection group took this snapshot. Volume and machine snapshots alike."""
    return bool(snapshot.get('protectionGroupId') or snapshot.get('protectionGroup'))


def uncounted(domain_ids) -> set[str]:
    """Which of these accounts have their backups out of view and off the bill."""
    wanted = {domain_id for domain_id in domain_ids if domain_id}
    if not wanted:
        return set()

    return set(
        AccountSettings.objects.filter(domain_id__in=wanted, count_backup_snapshots=False).values_list(
            'domain_id', flat=True
        )
    )


def counts_backups(domain_id: str) -> bool:
    return domain_id not in uncounted([domain_id])
