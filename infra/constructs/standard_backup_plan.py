"""A standard daily/weekly/monthly AWS Backup plan.

Every backup'd EFS in the repo runs on this same cadence:
- daily snapshots retained for 10 days
- weekly snapshots (Sundays) retained for 4 weeks
- monthly snapshots (1st of the month) retained for 3 months

Times are staggered so the three jobs don't kick off simultaneously
when their schedules overlap (e.g. a Sunday-the-1st). Callers still
attach resources by calling `.backup_plan.add_selection(...)` --
nothing about resource selection belongs in this construct.
"""

from aws_cdk import (
    Duration,
    aws_backup as backup,
    aws_events as events,
)
from constructs import Construct


class StandardBackupPlan(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        backup_plan_name: str,
        backup_vault: backup.IBackupVault,
    ) -> None:
        super().__init__(scope, construct_id)

        self.backup_plan = backup.BackupPlan(
            self,
            "Plan",
            backup_plan_name=backup_plan_name,
            backup_vault=backup_vault,
        )
        self.backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="daily-10-days",
                schedule_expression=events.Schedule.cron(minute="0", hour="5"),
                delete_after=Duration.days(10),
            )
        )
        self.backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="weekly-4-weeks",
                schedule_expression=events.Schedule.cron(
                    minute="0", hour="6", week_day="SUN"
                ),
                delete_after=Duration.days(28),
            )
        )
        self.backup_plan.add_rule(
            backup.BackupPlanRule(
                rule_name="monthly-3-months",
                schedule_expression=events.Schedule.cron(minute="0", hour="7", day="1"),
                delete_after=Duration.days(90),
            )
        )
