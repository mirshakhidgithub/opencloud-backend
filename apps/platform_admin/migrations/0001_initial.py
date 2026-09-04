"""Operators and their action log."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='PlatformAdmin',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('name', models.CharField(max_length=255)),
                ('password_hash', models.CharField(max_length=255)),
                (
                    'role',
                    models.CharField(
                        choices=[
                            ('OWNER', 'Owner'),
                            ('OPS', 'Operations'),
                            ('SUPPORT', 'Support'),
                            ('FINANCE', 'Finance'),
                        ],
                        default='SUPPORT',
                        max_length=16,
                    ),
                ),
                ('is_active', models.BooleanField(default=True)),
                ('totp_secret', models.CharField(blank=True, max_length=255)),
                ('totp_confirmed_at', models.DateTimeField(blank=True, null=True)),
                ('failed_attempts', models.PositiveSmallIntegerField(default=0)),
                ('locked_until', models.DateTimeField(blank=True, null=True)),
                ('last_login_at', models.DateTimeField(blank=True, null=True)),
                ('last_login_ip', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'platform_admins',
                'ordering': ['email'],
            },
        ),
        migrations.CreateModel(
            name='AdminAction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_email', models.EmailField(max_length=254)),
                ('action', models.CharField(max_length=64)),
                ('target_account', models.CharField(blank=True, max_length=255)),
                ('target_type', models.CharField(blank=True, max_length=64)),
                ('target_id', models.CharField(blank=True, max_length=128)),
                ('target_name', models.CharField(blank=True, max_length=255)),
                (
                    'outcome',
                    models.CharField(
                        choices=[('SUCCESS', 'Success'), ('FAILURE', 'Failure')], default='SUCCESS', max_length=16
                    ),
                ),
                ('error_code', models.CharField(blank=True, max_length=64)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user_agent', models.CharField(blank=True, max_length=512)),
                (
                    'actor',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='actions',
                        to='platform_admin.platformadmin',
                    ),
                ),
            ],
            options={
                'db_table': 'platform_admin_actions',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='adminaction',
            index=models.Index(fields=['action', '-created_at'], name='pa_action_created_idx'),
        ),
        migrations.AddIndex(
            model_name='adminaction',
            index=models.Index(fields=['target_account', '-created_at'], name='pa_target_created_idx'),
        ),
    ]
